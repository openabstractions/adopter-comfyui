"""Directory custody for the ComfyUI missing-model route."""

from contextlib import ExitStack, contextmanager
import os
from pathlib import Path
import stat
import tempfile
import uuid


_FILE_READ_DATA_AND_ATTRIBUTES = 0x81
_FILE_SHARE_READ_WRITE = 3
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000


def _is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _windows_open(path, flags=0):
    import _winapi
    import msvcrt
    # Open the entry itself and allow reads/writes, while denying deletion.
    handle = _winapi.CreateFile(path, _FILE_READ_DATA_AND_ATTRIBUTES, _FILE_SHARE_READ_WRITE, 0,
                                _OPEN_EXISTING, flags | _FILE_FLAG_OPEN_REPARSE_POINT, 0)
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except OSError:
        _winapi.CloseHandle(handle)
        raise


def _windows_staging_file(path, mode, encoding):
    import _winapi
    import msvcrt
    # Request DELETE when creating the exact leaf. All subsequent effects use
    # this handle, whose sharing keeps the name from being deleted or replaced.
    read_write_delete, share_read, create_new = 0xC0010000, 1, 1
    handle = _winapi.CreateFile(path, read_write_delete, share_read, 0, create_new,
                                _FILE_FLAG_OPEN_REPARSE_POINT, 0)
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
    except OSError:
        _winapi.CloseHandle(handle)
        raise
    try:
        return os.fdopen(descriptor, mode, encoding=encoding)
    except (OSError, ValueError):
        os.close(descriptor)
        raise


def _windows_stage_effect(stream, destination=None, *, replace=False):
    import ctypes
    from ctypes import wintypes
    import msvcrt
    set_info = ctypes.WinDLL("kernel32", use_last_error=True).SetFileInformationByHandle
    set_info.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    set_info.restype = wintypes.BOOL
    if destination is None:
        info_class, info = 4, wintypes.BOOL(True)  # FileDispositionInfo
    else:
        length = len(destination.encode("utf-16-le"))
        class RenameInfo(ctypes.Structure):
            _fields_ = (("replace", wintypes.DWORD), ("root", wintypes.HANDLE),
                        ("length", wintypes.DWORD), ("name", wintypes.WCHAR * (length // 2 + 1)))
        info_class, info = 3, RenameInfo(replace, None, length, destination)  # FileRenameInfo
    if not set_info(msvcrt.get_osfhandle(stream.fileno()), info_class, ctypes.byref(info), ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())


class ModelDestination:
    def __init__(self, root, parents):
        # A configured model root may intentionally alias a different disk.
        # Only the URL-selected children beneath that root must be link-free.
        self.configured = Path(root)
        self.root = Path(os.path.realpath(root))
        self.parents = parents
        anchor = self.root
        while True:
            try:
                self.observed = {len(anchor.parts) - 1: anchor.stat()}
                break
            except FileNotFoundError:
                if anchor.parent == anchor:
                    raise
                anchor = anchor.parent

    @contextmanager
    def hold(self, *, create=False):
        parts = self.root.parts + tuple(self.parents)
        path = Path(parts[0])
        parent = None
        with ExitStack() as held:
            for index, part in enumerate(parts):
                if index:
                    path /= part
                if os.name == "nt":
                    open_directory = lambda: _windows_open(os.fspath(path), _FILE_FLAG_BACKUP_SEMANTICS)
                    make_directory = lambda: os.mkdir(path)
                else:
                    name = part if index else os.fspath(path)
                    open_directory = lambda: os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    make_directory = lambda: os.mkdir(name, dir_fd=parent)
                try:
                    descriptor = open_directory()
                except FileNotFoundError:
                    if not create or index in self.observed:
                        raise
                    try:
                        make_directory()
                    except FileExistsError:
                        pass
                    descriptor = open_directory()
                held.callback(os.close, descriptor)
                info = os.fstat(descriptor)
                if _is_link(info) or not stat.S_ISDIR(info.st_mode):
                    raise ValueError("the model path crosses a linked or non-directory entry")
                known = self.observed.get(index)
                if known is not None and not os.path.samestat(info, known):
                    raise ValueError("the configured model directory changed")
                self.observed[index] = info
                parent = descriptor
            files = DeliveryFiles(os.fspath(path), None if os.name == "nt" else parent, self._verify_locator)
            self._verify_locator()
            yield files
            self._verify_locator()

    def _verify_locator(self):
        # This path is reported to ComfyUI, which discovers files by name.
        # Holding the original directory alone does not keep a POSIX name live.
        root_index = len(self.root.parts) - 1
        path = self.configured
        for index, part in enumerate((None, *self.parents), root_index):
            if part is not None:
                path /= part
            try:
                info = path.stat() if index == root_index else path.lstat()
            except OSError as error:
                raise ValueError("the configured model directory changed") from error
            if not os.path.samestat(info, self.observed[index]) or (part is not None and _is_link(info)):
                raise ValueError("the configured model directory changed")


class DeliveryFiles:
    """Local materialization operations, optionally relative to a held directory."""

    def __init__(self, directory=None, dir_fd=None, verify_locator=None):
        self.directory, self.dir_fd = directory, dir_fd
        self.verify_locator = verify_locator

    def _name(self, path):
        if self.dir_fd is not None:
            return os.path.basename(path)
        if self.directory is not None:
            return os.path.join(self.directory, os.path.basename(path))
        return path

    def lstat(self, path):
        return os.lstat(self._name(path), dir_fd=self.dir_fd)

    def present(self, path):
        try:
            info = self.lstat(path)
        except FileNotFoundError:
            return False
        if self.directory is not None and (_is_link(info) or not stat.S_ISREG(info.st_mode)):
            raise ValueError("model destination or request record is not an ordinary file")
        return True

    def open(self, path, mode, *, encoding=None):
        name = self._name(path)
        if self.dir_fd is not None:
            def opener(file, flags):
                return os.open(file, flags | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.dir_fd)
            stream = open(name, mode, encoding=encoding, opener=opener)
        elif self.directory is not None and mode.startswith("r"):
            descriptor = _windows_open(name)
            stream = os.fdopen(descriptor, mode, encoding=encoding)
        else:
            stream = open(name, mode, encoding=encoding)
        if self.directory is not None:
            info = os.fstat(stream.fileno())
            if _is_link(info) or not stat.S_ISREG(info.st_mode):
                stream.close()
                raise ValueError("model destination or request record is not an ordinary file")
        return stream

    def _verify_published(self, destination, identity):
        if not os.path.samestat(identity, self.lstat(destination)):
            raise ValueError("the published model or request record changed")
        self.verify_locator()

    @contextmanager
    def _private_staging(self):
        # Atomic publication needs the same volume. The private directory
        # excludes other accounts from changing the stage before publication.
        name = ".oa-download-staging"
        try:
            os.mkdir(name, 0o700, dir_fd=self.dir_fd)
        except FileExistsError:
            pass
        staging = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.dir_fd)
        try:
            info = os.fstat(staging)
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError("the model staging directory is not private to this account")
            yield staging
        finally:
            os.close(staging)

    @contextmanager
    def _staged_posix(self, destination, prefix, mode, encoding, keep_existing):
        temporary = prefix + uuid.uuid4().hex
        with self._private_staging() as staging:
            source = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=staging)
            with os.fdopen(source, mode, encoding=encoding) as output:
                identity = os.fstat(output.fileno())
                def stage_is_original():
                    try:
                        info = os.lstat(temporary, dir_fd=staging)
                    except OSError:
                        return False
                    return stat.S_ISREG(info.st_mode) and os.path.samestat(info, identity)

                try:
                    yield output
                    output.flush()
                    self.verify_locator()
                    if not stage_is_original():
                        raise ValueError("the staged model or request record changed")
                    name = self._name(destination)
                    if keep_existing:
                        os.link(temporary, name, src_dir_fd=staging, dst_dir_fd=self.dir_fd,
                                follow_symlinks=False)
                    else:
                        os.replace(temporary, name, src_dir_fd=staging, dst_dir_fd=self.dir_fd)
                    self._verify_published(destination, identity)
                finally:
                    if stage_is_original():
                        os.unlink(temporary, dir_fd=staging)

    @contextmanager
    def _staged_windows(self, destination, prefix, mode, encoding, keep_existing):
        temporary = self._name(prefix + uuid.uuid4().hex)
        with _windows_staging_file(temporary, mode, encoding) as output:
            published = False
            try:
                identity = os.fstat(output.fileno())
                yield output
                output.flush()
                self.verify_locator()
                _windows_stage_effect(output, self._name(destination), replace=not keep_existing)
                published = True
                self._verify_published(destination, identity)
            finally:
                if not published:
                    _windows_stage_effect(output)

    @contextmanager
    def staged(self, destination, prefix, *, mode="wb", encoding=None, keep_existing=False):
        if self.directory is not None:
            hold = self._staged_windows if self.dir_fd is None else self._staged_posix
            with hold(destination, prefix, mode, encoding, keep_existing) as output:
                yield output
            return
        with _staged_model_path(destination, prefix) as output:
            yield output


@contextmanager
def _staged_model_path(destination, prefix):
    """Stage model bytes for callers that supplied their own destination."""
    stream = tempfile.NamedTemporaryFile(
        dir=os.path.dirname(os.path.abspath(destination)), prefix=prefix, delete=False)
    try:
        with stream:
            yield stream
        os.replace(stream.name, destination)
    except BaseException:
        os.unlink(stream.name)
        raise

PATH_FILES = DeliveryFiles()
