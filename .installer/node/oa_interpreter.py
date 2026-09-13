"""This ComfyUI's own program image: a copy of its interpreter in this folder.

OpenAbstractions knows an application by the file its process runs from
(identity ID-F9, VISION 2026-10-07). On Windows a venv's python.exe starts the
shared interpreter its pyvenv.cfg names as `home`, so ComfyUI runs as a file
every other Python program on the computer shares. ensure() places a real copy
of `home`'s python.exe, its python3X.dll, python3.dll and the vcruntime140
DLLs `home` carries in HELPER_FOLDER beside this file, with a pyvenv.cfg naming
the same `home`; the helper (oa_helper.py) runs from that copy and is the
program OpenAbstractions knows this ComfyUI by.

A hard link is the shared file under a second name, so every copied file is
checked by file identity, and a file already in the folder is replaced, never
written through. The copy is refreshed when one of `home`'s files changes its
size, modification time or file identity. A `home` that is not on a local
drive is refused, by the rule of identity.LocalPath.

The per-user installer runs this file with the venv's own interpreter
(`pythonw.exe -I -B oa_interpreter.py`), and main() ends that run within
SETUP_SECONDS; the extension calls ensure() when ComfyUI starts and at
Connect. The sealed Desktop pack instead verifies its prebuilt helper and
native-derived configuration without copying or writing. A helper folder
that is a junction or symbolic link is refused before
anything is written. Standard library only.
"""

import contextlib
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import sys
import threading
import tempfile

HELPER_FOLDER = "helper"
PROGRAM = "python.exe"
CONFIG = "pyvenv.cfg"
STAMP = "source.json"
ATOMIC_NAME = "oa_atomic/abstraction_cas.py"
ATOMIC_SHA256 = "de4eb02654874eafae88d1085670666dffe445e4ca8670746c9e6562eec0b71b"
UNSUPPORTED = "No app-local Python helper is packaged for this platform."

_CONFIG_LIMIT = 64 * 1024
_STAMP_LIMIT = 64 * 1024
# The files the copy takes from home: the interpreter, its DLL, the stable ABI
# DLL and the C runtime DLLs a python.org or uv-managed Python carries.
_PROGRAM_NAME = re.compile(r"python\.exe\Z", re.IGNORECASE)
_PYTHON_DLL = re.compile(r"python3\d+\.dll\Z", re.IGNORECASE)
_COPIED_DLL = re.compile(r"(?:python3\d*|vcruntime140(?:_\d+)?)\.dll\Z", re.IGNORECASE)
_HOME_KEY = re.compile(r"\s*home\s*\Z", re.IGNORECASE)  # a pyvenv.cfg key, as Python reads it
_DEVICE = re.compile("(?:CON|PRN|AUX|NUL|CONIN\\$|CONOUT\\$|(?:COM|LPT)[0-9¹²³])\\Z", re.IGNORECASE)
_DRIVE_REMOTE = 4
_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT
# The installer's bound on the copy (main); the copy itself takes about a second.
SETUP_SECONDS = 60
SEALED_PACK = "abstraction_downloads"
SEALED_CONFIG = "ComfyUI/custom_nodes/abstraction_downloads/helper/pyvenv.cfg"
SEALED_CONFIG_TEMPLATE = b"home = {backend}/standalone-env\ninclude-system-site-packages = false\n"


class InterpreterError(Exception):
    """One sentence saying why no copy could be made."""


def venv_home(prefix):
    """The `home` folder the pyvenv.cfg in prefix names, as Python reads it:
    the first `home =` line."""
    path = os.path.join(prefix, CONFIG)
    try:
        with open(path, "rb") as stream:
            data = stream.read(_CONFIG_LIMIT + 1)
    except OSError:
        raise InterpreterError("This ComfyUI runs from no venv: %s could not be read." % path) from None
    if len(data) > _CONFIG_LIMIT:
        raise InterpreterError("%s is over 64 KiB." % path)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise InterpreterError("%s is not UTF-8 text." % path) from None
    for line in text.splitlines():
        key, equals, value = line.partition("=")
        if equals and _HOME_KEY.match(key):
            home = value.strip()
            if not home or not os.path.isabs(home):
                raise InterpreterError("%s names no absolute home folder." % path)
            return home
    raise InterpreterError("%s names no home folder." % path)


def _drive_absolute(path):
    return len(path) >= 3 and path[0].isascii() and path[0].isalpha() and path[1] == ":" and path[2] == "\\"


def _drive_type(root):
    import ctypes
    function = ctypes.windll.kernel32.GetDriveTypeW
    function.argtypes, function.restype = [ctypes.c_wchar_p], ctypes.c_uint
    return function(root)


def refused_path(path, drive_type=None):
    """Why identity.LocalPath refuses path on Windows, or None when it is on a
    local drive: a UNC, device or NT object path, a reserved device name in
    any folder, a path naming no drive letter and a network drive are
    refused. drive_type(root) answers GetDriveType; tests pass their own."""
    if "\0" in path:
        return "it holds a NUL"
    spelled = path.replace("/", "\\")
    if spelled.startswith("\\??\\"):
        return "it is an NT object path"
    if spelled.startswith("\\\\?\\"):
        spelled = spelled[4:]
        if not _drive_absolute(spelled):
            return "it is a \\\\?\\ path naming no drive letter"
    elif spelled.startswith("\\\\"):
        return "it is a UNC or \\\\.\\ device path"
    else:
        spelled = os.path.abspath(spelled)
        if spelled.startswith("\\\\"):
            return "its full path is a UNC or \\\\.\\ device path"
    if not _drive_absolute(spelled):
        return "it names no drive letter"
    for part in spelled[3:].split("\\"):
        name = re.split(r"[.:]", part, maxsplit=1)[0].rstrip(" ")
        if _DEVICE.match(name):
            return "it names the device " + part
    if (drive_type or _drive_type)(spelled[:3]) == _DRIVE_REMOTE:
        return "it is on a network drive"
    return None


def _sources(home):
    """{file name: path} of the files the copy takes from home."""
    found = {}
    try:
        with os.scandir(home) as entries:
            for entry in entries:
                if (_PROGRAM_NAME.match(entry.name) or _COPIED_DLL.match(entry.name)) and \
                        entry.is_file(follow_symlinks=False):
                    found[entry.name] = entry.path
    except OSError:
        raise InterpreterError("The interpreter folder %s could not be read." % home) from None
    if not any(_PROGRAM_NAME.match(name) for name in found) or not any(_PYTHON_DLL.match(name) for name in found):
        raise InterpreterError("The interpreter folder %s holds no python.exe with its python3X.dll." % home)
    return found


def _stamp(home, sources):
    files = {}
    for name, path in sorted(sources.items()):
        status = os.stat(path)
        files[name] = [status.st_size, status.st_mtime_ns, status.st_dev, status.st_ino]
    return {"home": home, "files": files}


def _read_stamp(folder):
    try:
        with open(os.path.join(folder, STAMP), "rb") as stream:
            data = stream.read(_STAMP_LIMIT + 1)
        return json.loads(data) if len(data) <= _STAMP_LIMIT else None
    except (OSError, ValueError):
        return None


def _same_file(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _copies_are_own(folder, sources):
    """Every copy exists and is a file of its own, by file identity."""
    for name, source in sources.items():
        copy = os.path.join(folder, name)
        if not os.path.isfile(copy) or os.path.islink(copy) or _same_file(copy, source):
            return False
    return True


def _program_name(sources):
    return next(name for name in sources if _PROGRAM_NAME.match(name))


def _write(path, text):
    _atomic_file_owner().change(path, lambda previous: text.encode("utf-8"))


def _atomic_file_owner():
    """The pinned standard-library CAS source staged beside this bootstrap."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ATOMIC_NAME)
    try:
        parent = os.path.dirname(path)
        root = os.path.dirname(os.path.abspath(__file__))
        while True:
            if _reparse_point(parent):
                raise InterpreterError("The atomic-file helper crosses a linked folder.")
            if parent == root:
                break
            parent = os.path.dirname(parent)
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or _reparse_point(path) or info.st_nlink != 1:
            raise InterpreterError("The atomic-file helper is not a plain private file.")
        with open(path, "rb") as stream:
            data = stream.read(1024 * 1024 + 1)
        if hashlib.sha256(data).hexdigest() != ATOMIC_SHA256:
            raise InterpreterError("The atomic-file helper differs from the selected CAS source.")
        spec = importlib.util.spec_from_file_location("oa_private_atomic_file", path)
        owner = importlib.util.module_from_spec(spec)
        exec(compile(data, path, "exec"), owner.__dict__)
        return owner
    except OSError as error:
        raise InterpreterError("The atomic-file helper is unavailable.") from error


def _copy(folder, home, sources):
    atomic = _atomic_file_owner()
    for name, source in sorted(sources.items()):
        target = os.path.join(folder, name)
        handle, temporary = tempfile.mkstemp(prefix=name + ".", suffix=".copying", dir=folder)
        os.close(handle)
        # A fresh file, then a rename: a hard link already at target loses its
        # name and the shared file it names is never written.
        try:
            shutil.copyfile(source, temporary)
            if _same_file(temporary, source):
                raise InterpreterError("The copy of %s is the same file as the shared one." % source)
            try:
                atomic._rename_over(atomic._native(temporary), atomic._native(target))
            except PermissionError:
                raise InterpreterError("%s is in use; close every ComfyUI and start it again to refresh the helper."
                                       % target) from None
        finally:
            with contextlib.suppress(OSError):
                os.remove(temporary)
    # A file an earlier home had, such as python312.dll after an update to 3.13.
    for entry in os.scandir(folder):
        if (_PROGRAM_NAME.match(entry.name) or _COPIED_DLL.match(entry.name)) and entry.name not in sources:
            try:
                os.remove(entry.path)
            except OSError:
                pass
    _write(os.path.join(folder, CONFIG), "home = %s\ninclude-system-site-packages = false\n" % home)


def _sealed_profile(extension_folder):
    """Verify the fixed modern pack; its native launch owner holds these files.

    This path is source-owned, never selected by an environment/mode argument.
    A damaged modern pack cannot fall through to the stock copy/refresh path.
    """
    extension = os.path.abspath(extension_folder)
    if os.path.basename(extension).casefold() != SEALED_PACK:
        return None
    backend = os.path.dirname(os.path.dirname(os.path.dirname(extension)))
    expected = os.path.join(backend, "ComfyUI", "custom_nodes", SEALED_PACK)
    if not _same_file(extension, expected):
        raise InterpreterError("The sealed helper has no selected backend layout.")
    try:
        parent = extension
        while True:
            if _reparse_point(parent):
                raise ValueError("linked backend ancestor")
            above = os.path.dirname(parent)
            if above == parent:
                break
            parent = above
        with open(os.path.join(backend, "manifest.json"), "rb") as stream:
            raw = stream.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError("manifest bound")
        manifest = json.loads(raw)
        if manifest.get("id") != "win-amd" or manifest.get("manager") is not False:
            raise ValueError("selected backend")
        template = {"bytes": len(SEALED_CONFIG_TEMPLATE),
                    "sha256": hashlib.sha256(SEALED_CONFIG_TEMPLATE).hexdigest()}
        if manifest.get("helper_python_config") != {"name": SEALED_CONFIG, "template": template}:
            raise ValueError("helper profile")
        home = os.path.join(backend, "standalone-env")
        if any(character in home for character in "\0\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029") or \
                not _same_file(sys.base_prefix, home) or not any(_same_file(sys.executable, program) for program in
                    (os.path.join(home, PROGRAM), os.path.join(extension, "helper", PROGRAM))):
            raise ValueError("selected Python home")
        files = manifest["runtime_files"]
        if not isinstance(files, dict) or not 0 < len(files) <= 65536:
            raise ValueError("runtime roster bound")
        prefix = "ComfyUI/custom_nodes/" + SEALED_PACK + "/"
        selected = {name[len(prefix):]: pin for name, pin in files.items() if name.startswith(prefix)}
        if not selected or selected.get("helper/" + CONFIG) != template:
            raise ValueError("helper closure")
        actual = set()
        for parent, directories, names in os.walk(extension):
            for name in directories + names:
                path = os.path.join(parent, name)
                if _reparse_point(path):
                    raise ValueError("linked helper member")
            for name in names:
                path = os.path.join(parent, name)
                relative = os.path.relpath(path, extension).replace(os.sep, "/")
                actual.add(relative)
                pin = selected.get(relative)
                status = os.stat(path)
                if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1 or not isinstance(pin, dict):
                    raise ValueError("private helper member")
                if relative == "helper/" + CONFIG:
                    with open(path, "rb") as stream:
                        data = stream.read(_CONFIG_LIMIT + 1)
                    if data != ("home = %s\ninclude-system-site-packages = false\n" % home).encode("utf-8"):
                        raise ValueError("derived Python home")
                else:
                    digest = hashlib.sha256()
                    with open(path, "rb") as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(chunk)
                    if pin != {"bytes": status.st_size, "sha256": digest.hexdigest()}:
                        raise ValueError("helper member pin")
        required = {"__init__.py", "oa_interpreter.py", "oa_connect.py", "oa_relay.py", "oa_helper.py",
                    ATOMIC_NAME, "oa_nodes/__init__.py", "oa_client/abstraction/ipc/__init__.py",
                    "helper/python.exe", "application-manifest.json"}
        if actual != set(selected) or not required <= actual or not any(
                name.startswith("helper/") and _PYTHON_DLL.fullmatch(name[len("helper/"):]) for name in actual):
            raise ValueError("complete helper closure")
        return os.path.join(extension, "helper", PROGRAM)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise InterpreterError("The sealed OpenAbstractions helper differs from its authenticated profile.") from error


def ensure(extension_folder, prefix=None, platform=None):
    """The helper program, copied or refreshed from the `home` of the venv at
    prefix (this interpreter's venv when None). Raises InterpreterError."""
    selected_platform = platform or sys.platform
    if selected_platform == "darwin":
        return _macos_profile(extension_folder)
    if selected_platform != "win32":
        raise InterpreterError(UNSUPPORTED)
    sealed = _sealed_profile(extension_folder)
    if sealed is not None:
        return sealed
    home = venv_home(sys.prefix if prefix is None else prefix)
    reason = refused_path(home)
    if reason is not None:
        raise InterpreterError("The interpreter folder %s is refused: %s." % (home, reason))
    sources = _sources(home)
    folder = os.path.join(extension_folder, HELPER_FOLDER)
    # A junction or symbolic link at helper\ would send every write and removal
    # below to wherever it points.
    if os.path.lexists(folder) and (_reparse_point(folder) or not os.path.isdir(folder)):
        raise InterpreterError("The helper folder %s is a link or not a folder; remove it and start ComfyUI again."
                               % folder)
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as error:
        raise InterpreterError("The helper folder %s could not be made: %s." % (folder, error.strerror)) from None
    if _reparse_point(folder):
        raise InterpreterError("The helper folder %s is a link; remove it and start ComfyUI again." % folder)
    if _same_file(folder, home):
        raise InterpreterError("The helper folder %s is the interpreter folder itself." % folder)
    stamp = _stamp(home, sources)
    if _read_stamp(folder) != stamp or not _copies_are_own(folder, sources) or \
            venv_home_or_none(folder) != home:
        try:
            _copy(folder, home, sources)
        except OSError as error:
            raise InterpreterError("The interpreter could not be copied into %s: %s." % (folder, error)) from None
        if not _copies_are_own(folder, sources):
            raise InterpreterError("The copy in %s is not a file of its own." % folder)
        _write(os.path.join(folder, STAMP), json.dumps(stamp, sort_keys=True) + "\n")
    return os.path.join(folder, _program_name(sources))


def _macos_profile(extension_folder):
    """Receive the producer's prebuilt app-local host; never copy shared Python.

    This validates package integrity only. Application signing, registration and
    inference permission remain with their existing native owners.
    """
    helper = os.path.join(os.path.abspath(extension_folder), HELPER_FOLDER)
    try:
        parent = helper
        while True:
            if _reparse_point(parent):
                raise ValueError("linked helper ancestor")
            above = os.path.dirname(parent)
            if above == parent:
                break
            parent = above
        manifest_path = os.path.join(helper, "manifest.json")
        manifest_status = os.lstat(manifest_path)
        if not stat.S_ISREG(manifest_status.st_mode) or manifest_status.st_nlink != 1 or _reparse_point(manifest_path):
            raise ValueError("helper manifest is not a private file")
        with open(manifest_path, "rb") as stream:
            raw = stream.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError("helper manifest bound")
        manifest = json.loads(raw)
        files = manifest["runtime_files"]
        if (manifest.get("id") != "macos-python-host" or not isinstance(files, dict)
                or not 0 < len(files) <= 65536 or "python-host" not in files
                or not any(name.startswith("runtime/lib/python") for name in files)):
            raise ValueError("helper profile")
        actual = set()
        for folder, directories, names in os.walk(helper):
            if any(_reparse_point(os.path.join(folder, name)) for name in directories):
                raise ValueError("linked helper directory")
            for name in names:
                path = os.path.join(folder, name)
                relative = os.path.relpath(path, helper).replace(os.sep, "/")
                if relative == "manifest.json":
                    continue
                status = os.lstat(path)
                if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                    raise ValueError("helper member is not a private file")
                digest = hashlib.sha256()
                with open(path, "rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                if files.get(relative) != {"bytes": status.st_size, "sha256": digest.hexdigest()}:
                    raise ValueError("helper member differs")
                actual.add(relative)
        if actual != set(files):
            raise ValueError("helper closure differs")
        return os.path.join(helper, "python-host")
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise InterpreterError("The app-local macOS Python helper is unavailable or differs from its package.") from error


def _reparse_point(path):
    """Whether path itself is a symbolic link, a junction or another reparse point."""
    status = os.lstat(path)
    return stat.S_ISLNK(status.st_mode) or bool(getattr(status, "st_file_attributes", 0) & _REPARSE_POINT)


def venv_home_or_none(prefix):
    try:
        return venv_home(prefix)
    except InterpreterError:
        return None


def main(seconds=SETUP_SECONDS, ensure=ensure):
    """The installer's step: ensure() for this file's folder, bounded so that a
    hung disk or file lock cannot hold the install; the process ends when the
    bound passes."""
    outcome = []

    def run():
        try:
            outcome.append((0, ensure(os.path.dirname(os.path.abspath(__file__)))))
        except InterpreterError as error:
            outcome.append((1, str(error)))
        except Exception as error:  # the step reports and ends; ComfyUI's start repeats it
            outcome.append((1, "The helper copy failed: %r." % (error,)))

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    if not outcome:
        print("The helper copy did not finish in %d seconds; ComfyUI makes it when it starts." % seconds, flush=True)
        return 2
    code, text = outcome[0]
    print(text, flush=True)
    return code


if __name__ == "__main__":
    os._exit(main())
