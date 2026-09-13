"""The "Connect to OpenAbstractions" action of this ComfyUI.

POST /openabstractions/connect writes this ComfyUI's application manifest
beside this file (application-manifest.json, the Facade's
ApplicationDescriptor in its wire form) and starts the installed
OpenAbstractions Panel with `--register-application <that file>`: the Panel's
own confirmation page, which an application's installer uses too
(monitor/application_register.go). The person confirms there; this node
registers nothing and grants nothing.

The installed Panel is the program the core OpenAbstractions package records
under Software\\OpenAbstractions\\Abstraction\\Panel (value "program"), the
machine-wide one first, as the per-user application installers read it
(installer/apps/app.wxs). The manifest names this extension's helper program,
the interpreter copy in helper/ (oa_interpreter.py) that makes this ComfyUI's
OpenAbstractions calls (oa_relay.py): a program inside this extension's
folder, never the shared interpreter (VISION 2026-10-07).

Standard library only: this package runs inside any ComfyUI's own Python and
installs nothing into it.
"""

import json
import logging
import os
import subprocess
import sys
import threading

if __package__:
    from . import oa_interpreter
else:
    # The MSI runs this exact installed script with its selected private Python
    # in isolated mode. Load only its sibling; ambient packages are irrelevant.
    import importlib.util
    _path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "oa_interpreter.py")
    _spec = importlib.util.spec_from_file_location("oa_comfy_installer_interpreter", _path)
    oa_interpreter = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(oa_interpreter)

LOG = logging.getLogger("openabstractions")
ROUTE = "/openabstractions/connect"
MANIFEST_NAME = "application-manifest.json"
APPLICATION = "comfyui"
# The title Apps shows: the catalog's name for the app. A title that reads
# as OpenAbstractions is refused at registration (ReservedApplicationTitle).
TITLE = "ComfyUI"
START_GUIDANCE = "OpenAbstractions starts ComfyUI when an app opens it. While Comfy Desktop is open, start ComfyUI there yourself."
PANEL_KEY = r"Software\OpenAbstractions\Abstraction\Panel"
RELEASE_PAGE = "https://github.com/openabstractions/redist/releases/latest"
# The variables the Panel needs to find this account's folders and run; the
# rest of ComfyUI's environment (its venv, tokens, proxies) stays here.
PANEL_ENVIRONMENT = frozenset({
    "APPDATA", "COMSPEC", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "PATH", "PATHEXT",
    "PROGRAMDATA", "SYSTEMROOT", "TEMP", "TMP", "USERDOMAIN", "USERNAME", "USERPROFILE", "WINDIR",
})

HERE = os.path.dirname(os.path.abspath(__file__))

_lock = threading.Lock()
_panel = None  # the Panel process this ComfyUI started last


def helper_program(folder=None):
    """The helper program OpenAbstractions knows this ComfyUI by, copied or
    refreshed in folder (this extension's) first. Raises InterpreterError."""
    return oa_interpreter.ensure(folder or HERE)


def manifest(program):
    """This ComfyUI's application manifest for program."""
    return {"name": APPLICATION, "program": program, "title": TITLE, "start_guidance": START_GUIDANCE}


def _registry_program(root, view):
    import winreg
    try:
        with winreg.OpenKey(root, PANEL_KEY, 0, winreg.KEY_READ | view) as key:
            value, kind = winreg.QueryValueEx(key, "program")
    except OSError:
        return None
    if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) or not isinstance(value, str):
        return None
    return value


def installed_panel(read=None):
    """The installed Panel's program, or None. read(root, view) returns the
    recorded value of one registry view; tests pass their own."""
    if read is None:
        import winreg
        read = _registry_program
        views = ((winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY), (winreg.HKEY_CURRENT_USER, 0))
    else:
        views = (("HKLM", 0), ("HKCU", 0))
    for root, view in views:
        program = read(root, view)
        if program and os.path.isabs(program) and os.path.isfile(program):
            return program
    return None


def write_manifest(folder, program):
    """Read the sealed portable descriptor, or write the stock descriptor."""
    path = os.path.join(folder, MANIFEST_NAME)
    sealed = oa_interpreter._sealed_profile(folder)
    if sealed is not None:
        try:
            if not oa_interpreter._same_file(program, sealed):
                raise ValueError("selected helper differs")
            with open(path, "rb") as stream:
                raw = stream.read(65537)
            if len(raw) > 65536 or json.loads(raw) != manifest("{app}/helper/python.exe"):
                raise ValueError("portable descriptor differs")
        except (OSError, ValueError) as error:
            raise oa_interpreter.InterpreterError("The sealed application descriptor differs from its selected helper.") from error
        return path
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(manifest(program), stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    os.replace(temporary, path)
    return path


def prepare_registration(folder=None, ensure=None):
    """MSI setup creates a descriptor for the actual successful helper copy.

    It requests no registration, readiness or rights. The MSI subsequently
    revalidates its native host selection and asks the installed Panel to show
    the person's normal confirmation page.
    """
    folder = os.path.abspath(folder or HERE)
    program = (ensure or helper_program)(folder)
    sealed = oa_interpreter._sealed_profile(folder)
    helper = os.path.join(folder, oa_interpreter.HELPER_FOLDER)
    expected = os.path.join(helper, "python.exe")
    if not os.path.isfile(program) or not os.path.isdir(helper) or oa_interpreter._reparse_point(helper) or \
            oa_interpreter._reparse_point(program) or os.stat(program).st_nlink != 1 or \
            not oa_interpreter._same_file(program, expected) or \
            (sealed is None and oa_interpreter._same_file(program, sys.executable)):
        raise oa_interpreter.InterpreterError("The installed helper is missing, shared or outside this extension.")
    return write_manifest(folder, program)


def main():
    # Retain the existing setup deadline and its nonzero failure result.
    return oa_interpreter.main(ensure=lambda folder: prepare_registration(folder))


def panel_environment(source=None):
    """The named variables of source; Python on Windows spells every
    os.environ name in capitals."""
    source = os.environ if source is None else source
    return {name: value for name, value in source.items() if name in PANEL_ENVIRONMENT}


CONNECTOR = os.path.join(HERE, "comfyui-connector.exe")
REGISTER_SECONDS = 60


def register_connector(connector, run=subprocess.run):
    """Run the connector's register-self: OpenAbstractions registers the
    connector as this ComfyUI's program and the person keeps it in
    OpenAbstractions. Returns (ok, sentence)."""
    try:
        done = run([connector, "register-self"], stdin=subprocess.DEVNULL, capture_output=True,
                   timeout=REGISTER_SECONDS, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as error:
        LOG.warning("[openabstractions] the connector did not register: %s", error)
        return False, "The ComfyUI connector could not reach OpenAbstractions."
    try:
        answer = json.loads(done.stdout.decode("utf-8").strip().splitlines()[0])
    except (ValueError, IndexError, UnicodeDecodeError):
        answer = {}
    outcome = answer.get("outcome") if isinstance(answer, dict) else None
    if outcome == "applied":
        return True, "OpenAbstractions added ComfyUI; keep it in OpenAbstractions' Apps to connect it."
    if outcome == "conflict":
        # The registry answers "applied" for this connector's own entry; a
        # conflict is an earlier connector or another program holding the name.
        if answer.get("reason") == "identity":
            return False, "An earlier ComfyUI connector is in OpenAbstractions. Press Connect for ComfyUI in its Catalog to replace it."
        return False, "Another program is registered as ComfyUI in OpenAbstractions' Apps. Remove it there, then connect again."
    LOG.warning("[openabstractions] the connector did not register: %s %s", done.stdout[-512:], done.stderr[-512:])
    return False, "OpenAbstractions did not add ComfyUI (%s)." % ((answer.get("reason") if isinstance(answer, dict) else "")
                                                               or outcome or "no answer")


def connect(platform=None, panel=None, program=None, folder=None, popen=subprocess.Popen, environ=None,
            connector=None, run=subprocess.run):
    """Connect this ComfyUI to OpenAbstractions: the connector registers
    itself when it is present, else the Panel's registration confirmation
    opens for the helper program.

    Returns (ok, sentence): one sentence the sidebar shows as written."""
    global _panel
    if (platform or sys.platform) != "win32":
        return False, "Connecting from ComfyUI works on Windows in this release."
    connector = connector or (CONNECTOR if program is None and os.path.isfile(CONNECTOR) else None)
    if connector is not None:
        return register_connector(connector, run)
    panel = panel or installed_panel()
    if panel is None:
        return False, "OpenAbstractions is not installed on this computer; download it from " + RELEASE_PAGE + "."
    with _lock:
        if _panel is not None and _panel.poll() is None:
            return True, "The OpenAbstractions window is already open; confirm there."
        try:
            program = program or helper_program(folder)
        except oa_interpreter.InterpreterError as error:
            LOG.warning("[openabstractions] no helper program: %s", error)
            return False, str(error)
        try:
            path = write_manifest(folder or HERE, program)
        except (OSError, oa_interpreter.InterpreterError) as error:
            LOG.warning("[openabstractions] the application manifest could not be written: %s", error)
            return False, "This ComfyUI could not write its application manifest beside this extension."
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            _panel = popen([panel, "--register-application", path], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                           creationflags=flags, env=panel_environment(environ))
        except OSError as error:
            LOG.warning("[openabstractions] the Panel could not be started: %s", error)
            return False, "The OpenAbstractions Panel could not be started."
    return True, "The OpenAbstractions window opened; confirm there to connect this ComfyUI."


LOOPBACK = frozenset({"127.0.0.1", "::1"})


def refusal(remote, content_type):
    """(status, body) refusing a request, or None to serve it. Only this
    computer may start the Panel: a ComfyUI started with --listen 0.0.0.0
    would otherwise let another machine open a window here. A JSON content
    type cannot be sent cross-site without a CORS preflight, which ComfyUI
    does not answer by default."""
    if remote not in LOOPBACK:
        return 403, {"ok": False, "message": "Only this computer can connect this ComfyUI to OpenAbstractions."}
    if content_type != "application/json":
        return 415, {"ok": False, "message": "This action needs a JSON request."}
    return None


_registered = []


def install(routes=None):
    """Serve ROUTE on ComfyUI's own server. Returns True when it is served."""
    try:
        if routes is None:
            server = sys.modules.get("server")
            instance = getattr(getattr(server, "PromptServer", None), "instance", None)
            if instance is None:
                LOG.info("[openabstractions] no running ComfyUI server; %s is not served", ROUTE)
                return False
            routes = instance.routes
        if any(table is routes for table in _registered):
            return True
        from aiohttp import web

        @routes.post(ROUTE)
        async def connect_route(request):
            refused = refusal(request.remote, request.content_type)
            if refused is not None:
                return web.json_response(refused[1], status=refused[0])
            import asyncio
            ok, message = await asyncio.get_running_loop().run_in_executor(None, connect)
            return web.json_response({"ok": ok, "message": message})

        _registered.append(routes)
        return True
    except Exception as error:  # the extension never stops ComfyUI from starting
        LOG.warning("[openabstractions] %s is not served: %r", ROUTE, error)
        return False


if __name__ == "__main__":
    os._exit(main())
