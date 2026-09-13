"""Local release-build filesystem boundaries, including Windows deep paths.

Import installs a release-path diagnostic for extended spellings handed to child
processes. Import before modifying the environment: segments present at first
import are accepted only under that same variable. This snapshot is a diagnostic
caveat. CPython 3.12's direct CreateProcess audit command line is not reliable;
that event checks its executable, cwd and inherited environment only.
"""

from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading


def windows_build_environment(temporary: Path) -> dict[str, str]:
    """Give an owned Windows build child only actual OS and explicit temp paths."""
    temporary=plain_path(temporary)
    local=extended_path(temporary)
    if (os.name!='nt' or not temporary.is_absolute() or local.is_symlink()
            or local.is_junction() or not local.is_dir()):
        raise ValueError('Windows build requires an ordinary absolute temporary directory')
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32.dll',use_last_error=True,winmode=0x800)
    query=kernel.GetWindowsDirectoryW
    query.argtypes=[wintypes.LPWSTR,wintypes.UINT];query.restype=wintypes.UINT
    directory=ctypes.create_unicode_buffer(32768);length=query(directory,len(directory))
    if not 0<length<len(directory):raise ValueError('Windows directory proof unavailable')
    windows=Path(directory.value)
    if not windows.is_absolute() or not windows.is_dir():raise ValueError('Windows directory proof invalid')
    return {'SystemRoot':str(windows),'WINDIR':str(windows),'PATH':str(windows/'System32'),
            'TEMP':str(temporary),'TMP':str(temporary)}


def same_existing_file(left: os.PathLike[str] | str,
                       right: os.PathLike[str] | str) -> bool:
    """Compare existing filesystem identities; callers admit links and types.

    Equal bytes do not imply equal files. Lookup errors remain errors.
    """
    return os.path.samefile(extended_path(left), extended_path(right))


class OutsideRoot(ValueError):
    """An observed directory walk did not reach the admitted root identity."""


def relative_beneath_existing(path: os.PathLike[str] | str,
                              root: os.PathLike[str] | str, *, ordinary_file=False) -> Path:
    """Return an existing name's suffix beneath an existing directory identity.

    With ordinary_file, admit a file and directory without links or reparse
    points. Otherwise callers own link/type admission. This snapshot walks
    directory names; a multiply named file does not imply a unique parent.
    A ".." part is refused: the walk follows names, and a parent step in a
    name would let a path outside the root pass through the root's own name.
    """
    if any(part == ".." for part in Path(path).parts + Path(root).parts):
        raise ValueError("containment refuses a parent (..) part")
    if ordinary_file:
        ordinary_path(path, kind="file")
        ordinary_path(root, kind="directory")
    name, boundary = extended_path(path), extended_path(root)
    info = boundary.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("containment root must be a directory")
    leaf = name.stat()
    at = name if stat.S_ISDIR(leaf.st_mode) else name.parent
    suffix = [] if stat.S_ISDIR(leaf.st_mode) else [name.name]
    while True:
        if os.path.samefile(at, boundary):
            return Path(*reversed(suffix))
        parent = at.parent
        if parent == at:
            raise OutsideRoot("existing path outside admitted root")
        suffix.append(at.name)
        at = parent


def ordinary_path(path, *, kind=None, allow_missing=False):
    """Admit a name and all ancestors without links or reparse points.

    Missing suffixes are allowed only explicitly, beneath an existing directory.
    This is a snapshot admission; it does not provide write-race exclusion.
    """
    name = extended_path(path)
    missing = False
    leaf = None
    for part in (name, *name.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            if not allow_missing:
                raise
            missing = True
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0):
            raise ValueError('path is linked or contains a reparse point')
        if leaf is None:
            leaf = info
            if missing and not stat.S_ISDIR(info.st_mode):
                raise ValueError('missing suffix requires an existing directory')
            if not missing and kind == 'file' and not stat.S_ISREG(info.st_mode):
                raise ValueError('path must be an ordinary file')
            if not missing and kind == 'directory' and not stat.S_ISDIR(info.st_mode):
                raise ValueError('path must be an ordinary directory')
        elif not stat.S_ISDIR(info.st_mode):
            raise ValueError('path ancestor must be a directory')
    if leaf is None:
        raise FileNotFoundError('path has no existing ancestor')
    return plain_path(name)


def relative_beneath_missing(path, root):
    """Admit a possibly missing suffix beneath an existing ordinary root.

    The existing-path owner validates parent syntax and asks filesystem identity.
    Only FileNotFoundError permits walking back to the nearest existing ancestor.
    """
    ordinary_path(root, kind='directory')
    try:
        result = relative_beneath_existing(path, root)
    except FileNotFoundError:
        pass
    else:
        ordinary_path(path)
        return result
    name = extended_path(path)
    ordinary_path(name, allow_missing=True)
    suffix = []
    while True:
        try:
            name.lstat()
            break
        except FileNotFoundError:
            suffix.append(name.name)
            name = name.parent
    return relative_beneath_existing(name, root).joinpath(*reversed(suffix))


def missing_child(root, relative):
    """Return a nonempty relative child after ordinary-root identity admission."""
    member = Path(relative)
    if member.anchor or not member.parts:
        raise ValueError('child must be a nonempty relative name')
    target = Path(root) / member
    suffix = relative_beneath_missing(target, root)
    if not suffix.parts:
        raise ValueError('child must be a nonempty relative name')
    return target


def extended_path(path: os.PathLike[str] | str) -> Path:
    absolute = Path(path).absolute()
    if os.name == "nt":
        name = str(absolute)
        if name.startswith("\\\\?\\"):
            return absolute
        if name.startswith("\\\\"):
            return Path("\\\\?\\UNC\\" + name[2:])
        return Path("\\\\?\\" + name)
    return absolute


def remove_tree(path: os.PathLike[str] | str) -> None:
    """Remove one owned tree without following links or losing deep Windows files."""
    target = extended_path(path)
    if not os.path.lexists(target):
        return
    info = target.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
        raise ValueError("refusing to recurse into a linked removal root")
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("removal root must be a directory")

    def writable(function, name, _error):
        os.chmod(name, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        function(name)

    if sys.version_info >= (3, 12):
        shutil.rmtree(target, onexc=writable)
    else:
        shutil.rmtree(target, onerror=writable)


@contextmanager
def owned_temporary_tree(*, prefix: str, parent: Path):
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    try:
        yield path
    finally:
        remove_tree(path)


def plain_path(path: os.PathLike[str] | str) -> Path:
    """Return the spelling a child process receives for a path ``extended_path`` may have built."""
    name = os.fspath(path)
    if name.startswith("\\\\?\\UNC\\"):
        return Path("\\\\" + name[8:])
    if name.startswith("\\\\?\\"):
        return Path(name[4:])
    return Path(name)


# Both import spellings share the installed audit hook and its thread-local role.
_python_role = getattr(sys, '_openabstractions_selected_python_role', None)
if _python_role is None:
    _python_role = threading.local()
    sys._openabstractions_selected_python_role = _python_role


def _verify_python_role(role):
    path, stream, expected, checksum = role
    ordinary_path(path)
    before = os.fstat(stream.fileno())
    current = path.stat()
    if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
            or not os.path.samestat(before, current)
            or (before.st_size, before.st_mtime_ns) != (current.st_size, current.st_mtime_ns)
            or checksum(path) != expected):
        raise ValueError('selected Python executable changed')
    after = path.stat()
    if (not os.path.samestat(before, after)
            or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)):
        raise ValueError('selected Python executable changed during verification')


@contextmanager
def _selected_python_executable(path, expected, checksum):
    """Admit one descriptor-pinned build Python; caller owns its checksum owner."""
    if getattr(_python_role, 'value', None) is not None:
        raise ValueError('selected Python role cannot be nested')
    ordinary = plain_path(path)
    if (os.name != 'nt' or not ordinary.is_absolute() or _marked(ordinary)
            or ordinary.name != 'python.exe'):
        raise ValueError('selected Python requires an ordinary absolute executable')
    extended = extended_path(ordinary)
    for part in (extended, *extended.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError('selected Python path is linked')
    if extended.resolve() != extended or not same_existing_file(ordinary, extended):
        raise ValueError('selected Python spelling changes identity')
    with extended.open('rb') as stream:
        role = (extended, stream, expected, checksum)
        _verify_python_role(role)
        _python_role.value = role
        try:
            yield extended
        finally:
            _python_role.value = None
            _judged_popen.value = None


def _selected_python_spawn(event, executable, argv):
    role = getattr(_python_role, 'value', None)
    if event != 'subprocess.Popen' or role is None:
        return False
    if isinstance(argv, (str, bytes)):
        argv = _command_line_arguments(_text(argv))
    if not argv or _text(argv[0]) != str(role[0]):
        return False
    if executable is not None and _text(executable) != str(role[0]):
        return False
    _verify_python_role(role)
    return True


# --- Spawn guard -----------------------------------------------------------

EXTENDED_MARKERS = ("\\\\?\\", "\\??\\")
# multiprocessing's spawn start method calls _winapi.CreateProcess directly;
# os.startfile hands the shell a program, its arguments and a folder.
_SPAWN_EVENTS = frozenset(("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn",
                           "_winapi.CreateProcess", "os.startfile", "os.startfile/2"))


class ExtendedPathSpawnRefused(subprocess.SubprocessError):
    """A child process would have received the Windows extended path spelling."""


def _text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", "surrogateescape")
    if isinstance(value, os.PathLike):
        return _text(os.fspath(value))
    return value if isinstance(value, str) else None


def _marked(value):
    text = _text(value)
    return text is not None and any(marker in text for marker in EXTENDED_MARKERS)


def _command_line_arguments(line):
    """Use the Windows system command-line API for diagnostic argument positions."""
    try:
        import ctypes
        from ctypes import wintypes
        shell = ctypes.WinDLL('shell32.dll', use_last_error=True, winmode=0x800)
        kernel = ctypes.WinDLL('kernel32.dll', use_last_error=True, winmode=0x800)
        split = shell.CommandLineToArgvW
        split.restype = ctypes.POINTER(wintypes.LPWSTR)
        split.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int))
        free = kernel.LocalFree
        free.argtypes = (ctypes.c_void_p,)
        free.restype = ctypes.c_void_p
        count = ctypes.c_int()
        vector = split(line, ctypes.byref(count))
        if not vector:
            return None
        try:
            return [vector[index] for index in range(count.value)]
        finally:
            free(ctypes.cast(vector, ctypes.c_void_p))
    except (AttributeError, OSError):
        return None

def _offending_arguments(args):
    """Return (index, argument) for each argv entry that holds an extended spelling."""
    text = _text(args)
    if text is not None:
        if not _marked(text):
            return []
        split = _command_line_arguments(text) if os.name == "nt" else None
        if split is None:
            return [(None, text)]
        found = [(index, value) for index, value in enumerate(split) if _marked(value)]
        return found or [(None, text)]
    try:
        values = list(args or ())
    except TypeError:
        return []
    return [(index, _text(value)) for index, value in enumerate(values) if _marked(value)]


def _segments(value):
    text = _text(value)
    return text.split(os.pathsep) if text is not None else []


def _variable(name):
    """An environment variable's name as the child's lookup compares it."""
    text = _text(name)
    return text.casefold() if os.name == "nt" and text is not None else text


def environment_entries(environment):
    """The (variable, segment) pairs of ``environment`` that hold the extended spelling."""
    return frozenset((_variable(name), segment) for name, value in environment.items()
                     for segment in _segments(value) if _marked(segment))


# The extended entries of the environment as this module first saw it. Both
# import names (file_boundary, scripts.file_boundary) share the first snapshot.
INHERITED_ENTRIES = getattr(sys, "_openabstractions_inherited_environment", None)
if INHERITED_ENTRIES is None:
    INHERITED_ENTRIES = environment_entries(os.environ)
    sys._openabstractions_inherited_environment = INHERITED_ENTRIES


def spawn_refusal(event, args, inherited=None):
    """Name what a spawn audit event would hand a child in the extended spelling, or None.

    ``inherited`` defaults to INHERITED_ENTRIES, the (variable, segment) pairs
    the session's environment held (see ``environment_entries``); a marked
    segment passes only under the variable that held it.
    """
    if event not in _SPAWN_EVENTS:
        return None
    # Audit argument shapes (CPython 3.8+ audit events table):
    #   subprocess.Popen (executable, args, cwd, env)   os.system (command,)
    #   os.exec / os.posix_spawn (path, args, env)       os.spawn (mode, path, args, env)
    #   _winapi.CreateProcess (application_name, command_line, current_directory)
    #   os.startfile (path, operation)    os.startfile/2 (path, operation, arguments, cwd, show_cmd)
    # The last three carry no environment; the child inherits os.environ.
    args = tuple(args) + (None,) * 5
    executable, argv, cwd, env = None, None, None, None
    if event == "subprocess.Popen":
        executable, argv, cwd, env = args[:4]
    elif event == "os.system":
        argv = args[0]
    elif event == "os.spawn":
        executable, argv, env = args[1:4]
    elif event == "_winapi.CreateProcess":
        # CPython 3.12.10 hands this event's command_line through a mismatched
        # format and it arrives as garbage ('\x04'); the program, folder and
        # inherited environment are what the guard can read.
        executable, cwd = args[0], args[2]
    elif event == "os.startfile":
        executable = args[0]
    elif event == "os.startfile/2":
        executable, argv, cwd = args[0], args[2], args[3]
    else:
        executable, argv, env = args[:3]
    selected_python = _selected_python_spawn(event, executable, argv)
    found = []
    if _marked(executable) and not selected_python:
        found.append("executable")
    if _marked(cwd):
        found.append("working directory")
    for index, _value in _offending_arguments(argv):
        if not (selected_python and index == 0):
            found.append("command line" if index is None else "argument " + str(index))
    # A child given no environment inherits this process's one. Entries the
    # session's environment held came from it (an installer once put a \\?\
    # entry into this machine's user PATH); tooling adds none, and copying
    # one into another variable is an addition.
    inherited = INHERITED_ENTRIES if inherited is None else inherited
    try:
        items = list((os.environ if env is None else env).items())
    except AttributeError:
        items = []
    for key, value in items:
        variable = _variable(key)
        added = [segment for segment in _segments(value)
                 if _marked(segment) and (variable, segment) not in inherited]
        if added:
            found.append("environment variable " + str(_text(key)))
    if not found:
        return None
    return ("refusing to start a child process (" + event + "): " + "; ".join(found)
            + ". The Windows extended spelling \\\\?\\ is for this process's own file I/O; CMD, CMake, "
            "MinGW, WiX, MSBuild and PowerShell 5.1 reject it. Pass the child file_boundary.plain_path(...).")


# subprocess.Popen on Windows raises its own audit event, with the child's
# real environment, and then calls _winapi.CreateProcess with the same
# executable and folder on the same thread. That call was judged with the
# environment it really passes; judging it again against os.environ would
# refuse a child whose caller handed it a cleaned environment.
_judged_popen = getattr(sys, '_openabstractions_judged_popen', None)
if _judged_popen is None:
    _judged_popen = threading.local()
    sys._openabstractions_judged_popen = _judged_popen


def _spawn_guard(event, args):
    if event not in _SPAWN_EVENTS:
        return
    if event == "_winapi.CreateProcess":
        judged, _judged_popen.value = getattr(_judged_popen, "value", None), None
        arguments = tuple(args) + (None,) * 3
        if judged is not None and judged == (_text(arguments[0]), _text(arguments[2])):
            return
    reason = spawn_refusal(event, args)
    if reason:
        raise ExtendedPathSpawnRefused(reason)
    if event == "subprocess.Popen" and os.name == "nt":
        arguments = tuple(args) + (None,) * 3
        _judged_popen.value = (_text(arguments[0]), _text(arguments[2]))


def install_spawn_guard():
    """Install the process-wide spawn guard once; audit hooks cannot be removed."""
    # Imported as both ``file_boundary`` and ``scripts.file_boundary``, this
    # module can load twice. sys holds the installed hook's refusal class, so
    # one hook runs per process and both copies name the class it raises.
    global ExtendedPathSpawnRefused
    installed = getattr(sys, "_openabstractions_spawn_guard", None)
    if installed is not None:
        ExtendedPathSpawnRefused = installed
        return
    sys.addaudithook(_spawn_guard)
    sys._openabstractions_spawn_guard = ExtendedPathSpawnRefused


install_spawn_guard()
