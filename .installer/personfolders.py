"""Keep a Python test module away from the person's own per-user folders.

The Python twin of monitor/internal/personfolders (Go). A test module that
can reach a per-user folder, directly, through a module it imports, or in a
process it starts, imports the module fixtures:

    from personfolders import setUpModule, tearDownModule  # noqa: F401

unittest then runs the module's tests with HOME, USERPROFILE, APPDATA,
LOCALAPPDATA and XDG_{CONFIG,CACHE,DATA,STATE}_HOME inside a fresh temporary
folder laid out as a real home, restores them afterwards and removes the
folder. Windows' known folders under the profile follow USERPROFILE once
their folder exists. The toolchains' folders stay the person's: GOPATH,
GOCACHE and GOENV, RUSTUP_HOME and CARGO_HOME where they exist, so a test
that builds keeps its caches. Git's global configuration (the person's
identity among it) moves with HOME; a test that commits names its author.

A folder outside scripts/ keeps an identical copy of this file;
scripts/user_folders.py --check refuses a copy that differs and a test module
that can reach a per-user folder without the fixtures. It is a library that
tests import, with no command line.
"""
import os
import platform
import shutil
import sys
import tempfile
import time

FOLDERS = [
    os.path.join("AppData", "Roaming"), os.path.join("AppData", "Local"),
    ".config", ".cache", os.path.join(".local", "share"), os.path.join(".local", "state"),
    os.path.join("Library", "Application Support"), os.path.join("Library", "Caches"),
    os.path.join("Library", "Preferences"),
    "Desktop", "Documents", "Downloads", "Pictures",
]


def under(person):
    """The variables that move the person's per-user folders inside person."""
    return {
        "HOME": person,
        "USERPROFILE": person,
        "APPDATA": os.path.join(person, "AppData", "Roaming"),
        "LOCALAPPDATA": os.path.join(person, "AppData", "Local"),
        "XDG_CONFIG_HOME": os.path.join(person, ".config"),
        "XDG_CACHE_HOME": os.path.join(person, ".cache"),
        "XDG_DATA_HOME": os.path.join(person, ".local", "share"),
        "XDG_STATE_HOME": os.path.join(person, ".local", "state"),
    }


def _toolchains():
    """The toolchain folders the moved variables would otherwise move."""
    home = os.path.expanduser("~")
    # The folders Go's os.UserCacheDir and os.UserConfigDir name on each
    # system; a per-user folder layout, which says nothing of file identity.
    system = platform.system()
    if system == "Windows":
        cache = os.environ.get("LOCALAPPDATA", "")
        config = os.environ.get("APPDATA", "")
    elif system == "Darwin":
        cache = os.path.join(home, "Library", "Caches")
        config = os.path.join(home, "Library", "Application Support")
    else:
        cache = os.environ.get("XDG_CACHE_HOME") or os.path.join(home, ".cache")
        config = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    keep = {"GOPATH": os.path.join(home, "go")}
    if cache:
        keep["GOCACHE"] = os.path.join(cache, "go-build")
    if config:
        keep["GOENV"] = os.path.join(config, "go", "env")
    for name, folder in (("RUSTUP_HOME", ".rustup"), ("CARGO_HOME", ".cargo")):
        if os.path.isdir(os.path.join(home, folder)):
            keep[name] = os.path.join(home, folder)
    return {name: value for name, value in keep.items() if not os.environ.get(name)}


MARKER = "OA_TEST_PERSON"
_entered = []


def enter():
    """Move this process's per-user folders into a fresh temporary folder and
    return it; leave() restores them. A process that inherited a stand-in
    (MARKER, which the Go helper sets too) keeps it, and one whose environment
    names no home at all keeps its environment unchanged: a helper started
    with a reduced environment on purpose."""
    inherited = os.environ.get(MARKER)
    if inherited or not (os.environ.get("HOME") or os.environ.get("USERPROFILE")):
        _entered.append((None, {}))
        return inherited
    keep = _toolchains()
    person = tempfile.mkdtemp(prefix="oa-test-person-")
    for folder in FOLDERS:
        os.makedirs(os.path.join(person, folder), exist_ok=True)
    moved = dict(keep, **under(person))
    moved[MARKER] = person
    _entered.append((person, {name: os.environ.get(name) for name in moved}))
    os.environ.update(moved)
    return person


def leave():
    """Restore the variables the last enter() moved and remove its folder. A
    process a test started can hold a file there for a moment on Windows, so
    removal retries for ten seconds; a folder that stays is reported."""
    person, saved = _entered.pop()
    if person is None:
        return
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    deadline = time.monotonic() + 10
    while True:
        try:
            shutil.rmtree(person)
            return
        except FileNotFoundError:
            return
        except OSError as error:
            if time.monotonic() > deadline:
                print("personfolders: the stand-in person stays in the temporary folder:", error, file=sys.stderr)
                return
            time.sleep(0.2)


def setUpModule():
    """unittest's module fixture: enter()."""
    enter()


def tearDownModule():
    """unittest's module fixture: leave()."""
    leave()
