"""config -- what this machine has been told, and which authority said so.

The Python half of config/go. Same file, same rungs, same order: the machine
file an administrator wrote, then the per-user file, then the environment for
one run. Every key carries where its answer came from, because a machine whose
store comes from the user file and whose log sink comes from the machine file
has two answers and one string can only name one of them.

An application calls ``load()`` and is told the truth about the machine it is
running on, having been configured by nobody.
"""

import ctypes
import json
import os
import queue
import sys
import threading

try:
    import abstraction_cas as cas
    import abstraction_watch
except ImportError:
    for _sibling in ("cas", "watch"):
        sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     os.pardir, os.pardir, _sibling, "python"))
    import abstraction_cas as cas
    import abstraction_watch

NAME = "abstraction"

MACHINE = "machine"
USER = "user"
ENVIRONMENT = "environment"
DEFAULT = "default"

KEYS = ("nas_store", "store", "log_sink", "log_service", "off")

ENV_VARS = {
    "nas_store": "ABSTRACTION_NAS_STORE",
    "store": "ABSTRACTION_STORE",
    "log_sink": "ABSTRACTION_LOG",
    "log_service": "ABSTRACTION_LOG_SERVICE",
}

SETTLE = 0.1
ASK_EVERY = 2.0


class Untrusted(Exception):
    """A machine-wide file no administrator wrote."""


class Origin:
    """Which authority answered for one key, and which file said so."""

    __slots__ = ("rung", "path")

    def __init__(self, rung=DEFAULT, path=""):
        self.rung, self.path = rung, path

    def __eq__(self, other):
        return (isinstance(other, Origin)
                and (self.rung, self.path) == (other.rung, other.path))

    def __hash__(self):
        return hash((self.rung, self.path))

    def __repr__(self):
        return "Origin(%r, %r)" % (self.rung, self.path)

    def __str__(self):
        return self.rung if not self.path else "%s file %s" % (self.rung, self.path)


class Config:
    """What a machine has. Every key is optional: absent means this machine
    does not have that tier, which is a normal answer and not an error."""

    def __init__(self):
        self.nas_store = ""
        self.store = ""
        self.log_sink = ""
        self.log_service = ""
        self.off = {}
        self.origins = {}

    def origin(self, key):
        """A key nothing set came from the default, which is an answer."""
        return self.origins.get(key, Origin())

    def value(self, key):
        return self.off if key == "off" else getattr(self, key)

    def _set(self, key, value, rung, path=""):
        if key == "off":
            self.off = value
        else:
            setattr(self, key, value)
        self.origins[key] = Origin(rung, path)

    def stamp(self):
        """Differs whenever the answer differs. A process that has been running
        for days asks for this rather than rebuilding everything to find out
        whether it needs to."""
        out = {}
        for key in KEYS:
            v = self.value(key)
            if v:
                out[key] = v
        # Go's encoding/json escapes these three by default, and a stamp two
        # languages spell differently is two languages that disagree about
        # whether anything changed.
        return (json.dumps(out, separators=(",", ":"))
                .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))

    def describe(self):
        if not self.origins:
            return "no configuration found; only built-in tiers are available"
        out = []
        for key in KEYS:
            v = self.value(key)
            if v:
                out.append("  %-12s %s\n%-14s from %s" % (key, v, "", self.origin(key)))
        over = overridden()
        if over:
            out.append("\nthe environment is deciding %s, whatever this file says"
                       % ", ".join(over))
        return "\n".join(out) + "\n"


def user_path():
    """Where a per-user configuration belongs on this OS, following the
    platform's own convention rather than inventing one."""
    directory = _user_config_dir()
    if directory:
        return os.path.join(directory, NAME, "config.json")
    home = os.path.expanduser("~")
    if not home or home == "~":
        return ""
    return os.path.join(home, "." + NAME, "config.json")


def machine_path():
    """Where an administrator's configuration belongs."""
    if sys.platform == "win32":
        pd = os.environ.get("ProgramData", "")
        return os.path.join(pd, NAME, "config.json") if pd else ""
    return os.path.join("/etc", NAME, "config.json")


def _user_config_dir():
    if sys.platform == "win32":
        return os.environ.get("AppData", "")
    if sys.platform == "darwin":
        home = os.environ.get("HOME", "")
        return os.path.join(home, "Library", "Application Support") if home else ""
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    if xdg and os.path.isabs(xdg):
        return xdg
    home = os.environ.get("HOME", "")
    return os.path.join(home, ".config") if home else ""


def _search_paths():
    """Files in increasing order of precedence, so later entries win."""
    out = []
    for path, rung in ((machine_path(), MACHINE), (user_path(), USER)):
        if path:
            out.append((path, rung))
    return out


def load():
    """The machine's configuration. Never fails: a machine with nothing set up
    is a machine with no extra tiers, which every caller already handles."""
    c = Config()
    for path, rung in _search_paths():
        _read_into(c, path, rung)
    for key, var in ENV_VARS.items():
        v = os.environ.get(var, "")
        if v:
            c._set(key, v, ENVIRONMENT)
    return c


def _read_into(c, path, rung):
    try:
        raw = cas.read(path)
    except OSError:
        return
    if raw is None:
        return
    if rung == MACHINE:
        try:
            trusted(path)
        except (Untrusted, OSError) as e:
            _ignoring(path, e)
            return
    try:
        d = json.loads(raw.decode("utf-8"))
        if not isinstance(d, dict):
            raise ValueError("not a JSON object")
    except (UnicodeDecodeError, ValueError) as e:
        # A malformed config must not take the application down, and must not
        # be silent either -- the whole point is that somebody can find out why
        # a tier is not being used.
        _ignoring(path, e)
        return
    for key in KEYS:
        v = d.get(key)
        if key == "off":
            if isinstance(v, dict) and v:
                c._set(key, v, rung, path)
        elif isinstance(v, str) and v:
            c._set(key, v, rung, path)


def _ignoring(path, why):
    sys.stderr.write("abstraction: ignoring %s: %s\n" % (path, why))


def overridden():
    """The keys the environment is deciding, whatever any file says. An
    override that nothing can see is how a person ends up editing a file and
    watching nothing change."""
    return sorted(k for k in KEYS
                  if k in ENV_VARS and os.environ.get(ENV_VARS[k], ""))


def stamp():
    return load().stamp()


def job_store():
    """Where jobs live on this machine, and what said so.

    Configuration first, then the default. An existing ~/.modelget keeps being
    the store, because moving the default on upgrade would strand whatever is
    in flight -- a store is a directory of real work, not a cache.
    """
    c = load()
    if c.store:
        return c.store, str(c.origin("store"))
    home = os.path.expanduser("~")
    if not home or home == "~":
        raise OSError("this machine has no home directory, so there is no store to default to")
    legacy = os.path.join(home, ".modelget")
    if os.path.isdir(legacy):
        return legacy, "the default; an existing .modelget"
    return os.path.join(home, "." + NAME), "the default; nothing is configured"


# ------------------------------------------------------------ ownership ---


def trusted(path):
    """Raise Untrusted unless an administrator wrote this file.

    A file under ProgramData or /etc proves nothing about who wrote it: an
    ordinary user may create there and keeps full control of what they made.
    The owner is the one thing a planter cannot choose. This is OpenSSH's
    StrictModes, and the same two checks config/go/trust_*.go make.
    """
    for p in (path, os.path.dirname(path)):
        _trusted_one(p)


if sys.platform == "win32":
    from ctypes import wintypes

    _advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p)]
    _advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p,
                                               ctypes.POINTER(wintypes.LPWSTR)]
    _kernel.LocalFree.argtypes = [ctypes.c_void_p]

    _SE_FILE_OBJECT = 1
    _OWNER_SECURITY_INFORMATION = 1
    _ADMINISTRATORS = frozenset(("S-1-5-32-544", "S-1-5-18"))

    def _owner(path):
        sid = ctypes.c_void_p()
        sd = ctypes.c_void_p()
        rc = _advapi.GetNamedSecurityInfoW(path, _SE_FILE_OBJECT,
                                           _OWNER_SECURITY_INFORMATION,
                                           ctypes.byref(sid), None, None, None,
                                           ctypes.byref(sd))
        if rc != 0:
            raise ctypes.WinError(rc)
        try:
            text = wintypes.LPWSTR()
            if not _advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                return text.value
            finally:
                _kernel.LocalFree(text)
        finally:
            _kernel.LocalFree(sd)

    def _trusted_one(p):
        sid = _owner(p)
        if sid not in _ADMINISTRATORS:
            raise Untrusted("%s is owned by %s, not by Administrators or SYSTEM, "
                            "so no administrator wrote it" % (p, sid))

else:

    def _trusted_one(p):
        st = os.stat(p)
        if st.st_uid != 0:
            raise Untrusted("%s is owned by uid %d, not root, so no administrator "
                            "wrote it" % (p, st.st_uid))
        if st.st_mode & 0o022:
            raise Untrusted("%s is writable by others (%o), so anyone could have "
                            "written it" % (p, st.st_mode & 0o777))


# --------------------------------------------------------- subscription ---


class Subscription:
    """A live view of what this machine has been told.

    An edit this process made through cas reaches the platform's own directory
    notification like anybody else's, so there is one path here and not two:
    the directory says something moved, the answer is read again, and stamp
    decides whether it differed. That costs one settling period and covers
    every writer that has never heard of us.
    """

    def __init__(self, sub, how, stop=None):
        self._sub, self._how, self._stop = sub, how, stop

    def how(self):
        """The mechanism this subscription is being told by, so a platform that
        has to be asked is visible to a person rather than hidden in a latency."""
        return self._how

    def current(self):
        return self._sub.current()

    def next(self, timeout=None):
        return self._sub.next(timeout)

    def __iter__(self):
        return iter(self._sub)

    def close(self):
        if self._stop is not None:
            self._stop()
            self._stop = None
        self._sub.close()


def watch(budget=0.0):
    """Report the machine's answer whenever it changes.

    ``budget`` seconds with nothing changing also reports quiet, so a caller
    that wants to know the answer is still can ask for that instead of timing
    it itself.
    """
    dirs = _watchable()
    try:
        events, stop = _notify_dirs(dirs)
    except OSError as e:
        every = ASK_EVERY if budget <= 0 else min(ASK_EVERY, budget)
        return Subscription(abstraction_watch.poll(_look, every, budget),
                            "asking every %gs -- %s" % (every, e))
    c = load()
    sub = abstraction_watch.push(c, c.stamp(), budget)

    def reread():
        now = load()
        sub.post(now, now.stamp())

    teller = threading.Thread(target=abstraction_watch.settle,
                              args=(events, SETTLE, reread), daemon=True)
    teller.start()

    def close():
        stop()
        events.put(None)
        teller.join(timeout=1.0)

    return Subscription(sub, "%s on %s" % (NOTIFIER, ", ".join(dirs)), close)


def _look():
    c = load()
    return c, c.stamp()


def _watchable():
    """The directories a configuration file lives in, and only the ones that
    are there. A directory that does not exist yet cannot be watched, and a
    file appearing in one is seen at the next load rather than at once."""
    seen, out = set(), []
    for path, _ in _search_paths():
        d = os.path.dirname(path)
        if d in seen or not os.path.isdir(d):
            continue
        seen.add(d)
        out.append(d)
    return out


def _drain(events, source):
    """One notice per burst is all a subscriber wants, and settle() coalesces
    the rest, so a full queue is a queue that already says what this would."""
    for _ in source:
        try:
            events.put_nowait(object())
        except queue.Full:
            pass


if sys.platform == "win32":
    NOTIFIER = "ReadDirectoryChangesW"

    _FILE_LIST_DIRECTORY = 1
    _SHARE_ALL = 7
    _OPEN_EXISTING = 3
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _INVALID_HANDLE = ctypes.c_void_p(-1).value
    _WATCHED = 0x1 | 0x10 | 0x8  # file name, last write, size

    def _notify_dirs(dirs):
        if not dirs:
            raise OSError("no configuration directory exists yet")
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = ctypes.c_void_p
        k32.ReadDirectoryChangesW.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
            ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p,
            ctypes.c_void_p]
        k32.CancelIoEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]

        events = queue.Queue(maxsize=1)
        open_handles = []
        for d in dirs:
            h = k32.CreateFileW(d, _FILE_LIST_DIRECTORY, _SHARE_ALL, None,
                                _OPEN_EXISTING, _FILE_FLAG_BACKUP_SEMANTICS, None)
            if h in (_INVALID_HANDLE, None):
                continue
            open_handles.append(h)
            threading.Thread(target=_drain, args=(events, _reports(k32, h)),
                             daemon=True).start()
        if not open_handles:
            raise OSError("the configuration directory cannot be opened for notification")

        def stop():
            # Cancelling is the whole of stopping: the pending read returns
            # aborted and the thread that issued it closes its own handle, so
            # no handle is closed while a read is still using it.
            for h in open_handles:
                k32.CancelIoEx(h, None)

        return events, stop

    def _reports(k32, handle):
        buf = ctypes.create_string_buffer(4096)
        got = ctypes.c_ulong()
        try:
            while k32.ReadDirectoryChangesW(handle, buf, len(buf), False,
                                            _WATCHED, ctypes.byref(got), None, None):
                yield None
        finally:
            k32.CloseHandle(handle)

elif sys.platform.startswith("linux"):
    NOTIFIER = "inotify"

    _IN_CLOSE_WRITE = 0x8
    _IN_MOVED_TO = 0x80
    _IN_CREATE = 0x100
    _IN_DELETE = 0x200
    _IN_CLOEXEC = 0o2000000

    def _notify_dirs(dirs):
        if not dirs:
            raise OSError("no configuration directory exists yet")
        libc = ctypes.CDLL(None, use_errno=True)
        fd = libc.inotify_init1(_IN_CLOEXEC)
        if fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1")
        mask = _IN_CLOSE_WRITE | _IN_MOVED_TO | _IN_CREATE | _IN_DELETE
        added = 0
        for d in dirs:
            if libc.inotify_add_watch(fd, d.encode("utf-8"), mask) >= 0:
                added += 1
        if not added:
            os.close(fd)
            raise OSError("the configuration directory cannot be watched")
        events = queue.Queue(maxsize=1)
        # A blocking read on the inotify descriptor does not return when the
        # descriptor is closed, so stopping writes a byte here instead and the
        # reader selects on both.
        wake_r, wake_w = os.pipe()
        threading.Thread(target=_drain, args=(events, _reports(fd, wake_r)),
                         daemon=True).start()

        def stop():
            os.write(wake_w, b"x")

        return events, stop

    def _reports(fd, wake_r):
        import select
        try:
            while True:
                ready, _, _ = select.select([fd, wake_r], [], [])
                if wake_r in ready:
                    return
                if not os.read(fd, 4096):
                    return
                yield None
        finally:
            os.close(fd)
            os.close(wake_r)

elif sys.platform == "darwin":
    NOTIFIER = "kqueue"

    def _notify_dirs(dirs):
        if not dirs:
            raise OSError("no configuration directory exists yet")
        import select
        kq = select.kqueue()
        fds = []
        for d in dirs:
            fds.append(os.open(d, os.O_RDONLY))
        wake_r, wake_w = os.pipe()
        watched = [select.kevent(fd, select.KQ_FILTER_VNODE,
                                 select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                                 select.KQ_NOTE_WRITE | select.KQ_NOTE_RENAME
                                 | select.KQ_NOTE_DELETE)
                   for fd in fds]
        watched.append(select.kevent(wake_r, select.KQ_FILTER_READ, select.KQ_EV_ADD))
        kq.control(watched, 0, 0)
        events = queue.Queue(maxsize=1)
        threading.Thread(target=_drain, args=(events, _reports(kq, fds, wake_r)),
                         daemon=True).start()

        def stop():
            os.write(wake_w, b"x")

        return events, stop

    def _reports(kq, fds, wake_r):
        try:
            while True:
                for ev in kq.control(None, 4, None):
                    if ev.ident == wake_r:
                        return
                yield None
        finally:
            for fd in fds:
                os.close(fd)
            os.close(wake_r)
            kq.close()

else:
    NOTIFIER = "none"

    def _notify_dirs(dirs):
        raise OSError("no directory notification on " + sys.platform)
