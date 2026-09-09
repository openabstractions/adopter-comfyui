"""Compare-and-set over a file. Semantics in cas/README.md."""

import contextlib
import glob
import os
import sys
import tempfile
import threading


class Moved(Exception):
    """The file changed since it was read."""


class NoValue(Exception):
    """An edit returned no value where the file holds one."""


class Reentrant(Exception):
    """A write or change ran inside an edit on the same file."""


def read(path):
    try:
        return _read(_native(path))
    except FileNotFoundError:
        return None


def write(path, base, data):
    with _locked(path):
        _replace(path, base, data)


def change(path, edit):
    with _locked(path):
        cur = read(path)
        nxt = edit(cur)
        if nxt != cur:
            _replace(path, cur, nxt)


def sweep(path):
    """Remove the temporaries a killed writer left beside path; return how many.

    Not on the write path, where it would cost every write the size of the
    directory. Holding the lock is what makes it safe: a live writer stages
    under the lock, so every temporary beside the file is a dead one.
    """
    with _locked(path):
        native = _native(path)
        gone = 0
        for orphan in glob.glob(glob.escape(native) + ".*.tmp"):
            with contextlib.suppress(OSError):
                os.unlink(orphan)
                gone += 1
        return gone


_held = threading.local()


@contextlib.contextmanager
def _locked(path):
    native = _native(path)
    holding = getattr(_held, "paths", None)
    if holding is None:
        holding = _held.paths = set()
    if native in holding:
        raise Reentrant(os.fspath(path))
    os.makedirs(os.path.dirname(native), exist_ok=True)
    fd = os.open(native + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    holding.add(native)
    try:
        _flock(fd)
        yield
    finally:
        holding.discard(native)
        os.close(fd)


def _replace(path, base, data):
    if data is None:
        raise NoValue(os.fspath(path))
    if read(path) != base:
        raise Moved(os.fspath(path))
    native = _native(path)
    directory = os.path.dirname(native)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(native) + ".", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        _rename_over(tmp, native)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    _fsync_dir(directory)


def _rename_over(tmp, native):
    for _ in range(999):
        try:
            return _rename(tmp, native)
        except _TRANSIENT:
            pass
    _rename(tmp, native)


_MAX_PATH = 260
_DERIVED = len(".XXXXXXXX.tmp")

if sys.platform == "win32":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                 wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    _k32.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                wintypes.DWORD, wintypes.LPVOID]
    _k32.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]

    _GENERIC_READ, _GENERIC_WRITE, _DELETE = 0x80000000, 0x40000000, 0x00010000
    _SHARE_ALL, _OPEN_EXISTING, _BACKUP_SEMANTICS = 0x7, 3, 0x02000000
    _INVALID_HANDLE = wintypes.HANDLE(-1).value
    _EXCLUSIVE_LOCK = 2
    _FILE_RENAME_INFO_EX = 22
    _REPLACE_IF_EXISTS, _POSIX_SEMANTICS = 1, 2
    _ERROR_INVALID_FUNCTION, _ERROR_ACCESS_DENIED = 1, 5
    _ERROR_NOT_SUPPORTED, _ERROR_INVALID_PARAMETER = 50, 87
    _NO_DIRECTORY_FLUSH = (_ERROR_INVALID_FUNCTION, _ERROR_ACCESS_DENIED, _ERROR_NOT_SUPPORTED)
    _TRANSIENT = PermissionError

    def _native(path):
        p = os.path.abspath(os.fspath(path))
        if len(p) + _DERIVED < _MAX_PATH or p.startswith("\\\\?\\"):
            return p
        return "\\\\?\\UNC" + p[1:] if p.startswith("\\\\") else "\\\\?\\" + p

    def _open_shared(path, access, flags=0):
        h = _k32.CreateFileW(path, access, _SHARE_ALL, None, _OPEN_EXISTING, flags, None)
        if h == _INVALID_HANDLE:
            raise ctypes.WinError(ctypes.get_last_error())
        return h

    def _read(path):
        fd = msvcrt.open_osfhandle(_open_shared(path, _GENERIC_READ), os.O_RDONLY)
        with os.fdopen(fd, "rb") as f:
            return f.read()

    def _flock(fd):
        overlapped = ctypes.create_string_buffer(32)
        if not _k32.LockFileEx(msvcrt.get_osfhandle(fd), _EXCLUSIVE_LOCK, 0, 1, 0, overlapped):
            raise ctypes.WinError(ctypes.get_last_error())

    def _fsync_dir(directory):
        try:
            h = _open_shared(directory, _GENERIC_WRITE, _BACKUP_SEMANTICS)
        except OSError as e:
            if e.winerror in _NO_DIRECTORY_FLUSH:
                return
            raise
        try:
            if not _k32.FlushFileBuffers(h):
                err = ctypes.get_last_error()
                if err not in _NO_DIRECTORY_FLUSH:
                    raise ctypes.WinError(err)
        finally:
            _k32.CloseHandle(h)

    def _rename_info(target):
        units = len(target.encode("utf-16-le")) // 2

        class Info(ctypes.Structure):
            _fields_ = [("Flags", wintypes.DWORD), ("RootDirectory", wintypes.HANDLE),
                        ("FileNameLength", wintypes.DWORD), ("FileName", wintypes.WCHAR * (units + 1))]
        return Info(_REPLACE_IF_EXISTS | _POSIX_SEMANTICS, None, units * 2, target)

    def _rename(tmp, native):
        h = _open_shared(tmp, _DELETE)
        try:
            info = _rename_info(native)
            err = 0 if _k32.SetFileInformationByHandle(h, _FILE_RENAME_INFO_EX, ctypes.byref(info),
                                                         ctypes.sizeof(info)) else ctypes.get_last_error()
        finally:
            _k32.CloseHandle(h)
        if err in (_ERROR_NOT_SUPPORTED, _ERROR_INVALID_PARAMETER):
            os.replace(tmp, native)
        elif err:
            raise ctypes.WinError(err)

else:
    import errno
    import fcntl

    _TRANSIENT = ()
    _NO_DIRECTORY_FLUSH = (errno.EINVAL, errno.ENOTSUP)

    def _native(path):
        return os.path.abspath(os.fspath(path))

    def _read(path):
        with open(path, "rb") as f:
            return f.read()

    def _flock(fd):
        fcntl.flock(fd, fcntl.LOCK_EX)

    def _fsync_dir(directory):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        except OSError as e:
            if e.errno not in _NO_DIRECTORY_FLUSH:
                raise
        finally:
            os.close(fd)

    def _rename(tmp, native):
        os.replace(tmp, native)
