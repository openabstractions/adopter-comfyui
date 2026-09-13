"""This ComfyUI's report to its OpenAbstractions connector, and OA's picture models.

report() waits in a thread until ComfyUI's server listens, then writes

    %LOCALAPPDATA%\\OpenAbstractions\\ComfyUI\\listening.json
    {"pid": <this process>, "port": <its port>, "base": <this ComfyUI's folder>,
     "output": <its output folder>}

The connector (comfyui-connector.exe beside this file) serves this ComfyUI to
agents when the process holding that port's listener is the reported one and
the reported folder is its own. The report is removed when ComfyUI exits.

add_model_folders() gives ComfyUI the picture-model folders OpenAbstractions
Setup prepared: Setup names them in model-paths.json in the same folder, as
one oa-models group of diffusion_models, text_encoders and vae folders
(monitor/internal/setup/comfy_model_paths.go). Each folder is added to
ComfyUI's own search paths with folder_paths.add_model_folder_path; no file of
ComfyUI or of the person is written.

Once the report is written, the same thread watches for OpenAbstractions' stop
request, %LOCALAPPDATA%\\OpenAbstractions\\started\\comfyui.stop (the Apps page's
Stop, or Comfy Desktop taking over a server OA started). A request that names
this process ends ComfyUI once its prompt queue is empty
(adopters/comfyui/connector/started.go).

Standard library only; ComfyUI's own modules are imported when called; the report goes through the
pack's pinned atomic-file owner (oa_interpreter._atomic_file_owner).
"""

import atexit
import json
import logging
import os
import socket
import threading
import time

LOG = logging.getLogger("openabstractions")
HERE = os.path.dirname(os.path.abspath(__file__))
REPORT_NAME = "listening.json"
MODEL_PATHS_NAME = "model-paths.json"
MODEL_GROUP = "oa-models"
MODEL_CATEGORIES = ("diffusion_models", "text_encoders", "vae")
MODEL_PATHS_LIMIT = 64 * 1024
POLL_SECONDS = 0.5
STOP_NAME = "comfyui.stop"
STOP_LIMIT = 16 * 1024
STOP_POLL_SECONDS = 2.0


def oa_folder(environ=None):
    """%LOCALAPPDATA%\\OpenAbstractions\\ComfyUI, or None without LOCALAPPDATA."""
    local = (os.environ if environ is None else environ).get("LOCALAPPDATA")
    if not local or not os.path.isabs(local):
        return None
    return os.path.join(local, "OpenAbstractions", "ComfyUI")


def comfy_base(here=HERE):
    """This ComfyUI's folder: the one whose custom_nodes holds this pack."""
    return os.path.dirname(os.path.dirname(os.path.realpath(here)))


def listening_port(server, args=None):
    """The port ComfyUI's PromptServer listens on, or None while it does not
    listen. PromptServer sets its port once its first site listens; the
    command line's port is the fallback for a server that keeps none."""
    port = getattr(server, "port", None)
    if type(port) is not int and args is not None:
        port = getattr(args, "port", None)
    if type(port) is not int or not 0 < port < 65536:
        return None
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return port
    except OSError:
        return None


def _atomic_change():
    """The pack's pinned atomic-file owner's change(path, edit)."""
    if __package__:
        from . import oa_interpreter
    else:
        import importlib.util
        spec = importlib.util.spec_from_file_location("oa_listening_interpreter", os.path.join(HERE, "oa_interpreter.py"))
        oa_interpreter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(oa_interpreter)
    return oa_interpreter._atomic_file_owner().change


def write_report(folder, port, base, output, pid=None, change=None):
    """Write the report whole through the atomic-file owner; returns its path."""
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, REPORT_NAME)
    raw = json.dumps({"pid": os.getpid() if pid is None else pid, "port": port, "base": base,
                      "output": output or ""}).encode("utf-8") + b"\n"
    (change or _atomic_change())(path, lambda previous: raw)
    return path


def remove_report(path, pid=None):
    """Remove the report when it is still this process's."""
    try:
        with open(path, "rb") as stream:
            current = json.loads(stream.read(MODEL_PATHS_LIMIT))
        if isinstance(current, dict) and current.get("pid") == (os.getpid() if pid is None else pid):
            os.remove(path)
    except (OSError, ValueError):
        pass


def _output_directory():
    try:
        import folder_paths
        return os.path.realpath(folder_paths.get_output_directory())
    except Exception:  # an older ComfyUI keeps no output folder accessor
        return ""


def _server():
    import sys
    server = sys.modules.get("server")
    return getattr(getattr(server, "PromptServer", None), "instance", None)


def _args():
    try:
        from comfy.cli_args import args
        return args
    except Exception:
        return None


def stop_file(environ=None):
    """OA's stop request for this app, or None without LOCALAPPDATA."""
    folder = oa_folder(environ)
    if folder is None:
        return None
    return os.path.join(os.path.dirname(folder), "started", STOP_NAME)


# Requests older than this process's node pack are left from an earlier server.
_LOADED_MS = int(time.time() * 1000)


def process_filetime():
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    stamps = [wintypes.FILETIME() for _ in range(4)]
    if not kernel.GetProcessTimes(kernel.GetCurrentProcess(), *(ctypes.byref(stamp) for stamp in stamps)):
        return None
    created = stamps[0]
    return str((created.dwHighDateTime << 32) | created.dwLowDateTime)


def stop_requested(path, pid=None, since_ms=None, created=None):
    """Whether a fresh operator request names this PID and creation time."""
    try:
        with open(path, "rb") as stream:
            raw = stream.read(STOP_LIMIT + 1)
        if len(raw) > STOP_LIMIT:
            return False
        request = json.loads(raw)
    except (OSError, ValueError):
        return False
    if not isinstance(request, dict) or request.get("v") != 2:
        return False
    requested = request.get("requested_unix_ms")
    if type(requested) is not int or requested < (_LOADED_MS if since_ms is None else since_ms):
        return False
    processes = request.get("processes")
    created = process_filetime() if created is None else created
    current = {"pid": os.getpid() if pid is None else pid, "created": created}
    if not created or not isinstance(processes, list) or current not in processes:
        return False
    try:
        with open(os.path.splitext(path)[0] + ".json", "rb") as stream:
            raw = stream.read(STOP_LIMIT + 1)
        record = json.loads(raw) if len(raw) <= STOP_LIMIT else None
    except (OSError, ValueError):
        return False
    held = (record.get("processes") if isinstance(record, dict) and record.get("v") == 1
            and record.get("application") == "comfyui" else None)
    if (not isinstance(held, list) or not 0 < len(held) <= 8 or request.get("launcher") != held[0]
            or current not in held):
        return False
    parts = []
    for process in held:
        if not isinstance(process, dict) or type(process.get("pid")) is not int or process["pid"] <= 0:
            return False
        birth = process.get("created")
        if not isinstance(birth, str) or not 0 < len(birth) <= 20 or not birth.isascii() or not birth.isdecimal() or birth[0] == "0":
            return False
        parts.append(f"{process['pid']}:{birth}")
    occurrence = "|".join(parts)
    return request.get("occurrence") == occurrence


def queue_empty(server):
    """Whether ComfyUI runs and holds no prompt; True when it keeps no queue."""
    queue = getattr(server, "prompt_queue", None)
    remaining = getattr(queue, "get_tasks_remaining", None)
    if remaining is None:
        return True
    try:
        return remaining() == 0
    except Exception:
        return False


class AgentStop:
    """Cooperative stop on PromptServer's loop, bound to this connector launch."""

    def __init__(self, server, key, exit=os._exit):
        self.server = server
        self.key = key
        self.exit = exit
        self.pending = None

    def request(self, key, bridge_live, person_page):
        import hmac
        if (not self.key or not isinstance(key, str) or not key.isascii()
                or not hmac.compare_digest(key, self.key) or not bridge_live()):
            return "unavailable"
        if not self.ready():
            return "unavailable"
        if self.in_use(person_page):
            return "person_using"
        if self.pending is None or not self.pending():
            self.pending = bridge_live
            self.server.loop.call_later(0.5, self.drain, bridge_live, person_page)
        return "requested"

    def ready(self):
        queue = getattr(self.server, "prompt_queue", None)
        return (isinstance(getattr(self.server, "sockets", None), dict)
                and hasattr(getattr(queue, "mutex", None), "__enter__")
                and callable(getattr(queue, "get_tasks_remaining", None)))

    def in_use(self, person_page):
        return bool(self.server.sockets) or person_page()

    def drain(self, bridge_live, person_page):
        if self.pending is not bridge_live:
            return
        if not bridge_live() or not self.ready() or self.in_use(person_page):
            self.pending = None
            return
        queue = self.server.prompt_queue
        with queue.mutex:
            if queue_empty(self.server):
                LOG.info("[openabstractions] ComfyUI stops after draining the queue for its starter")
                self.exit(0)
                self.pending = None
                return
        self.server.loop.call_later(0.5, self.drain, bridge_live, person_page)


def watch_stop(path, server, stop=None, exit=os._exit, sleep=time.sleep, created=None, change=None):
    """End ComfyUI when the current operator request still owns an empty queue."""
    announced = False
    created = process_filetime() if created is None else created
    while stop is None or not stop.is_set():
        if path and created and stop_requested(path, created=created):
            current = server() if callable(server) else server
            queue = getattr(current, "prompt_queue", None)
            mutex = getattr(queue, "mutex", None)
            if hasattr(mutex, "__enter__") and callable(getattr(queue, "get_tasks_remaining", None)):
                stopped = busy = False

                def finish(record):
                    nonlocal stopped, busy
                    if not stop_requested(path, created=created):
                        return record
                    # Queue puts and completion take this same mutex. Keep
                    # both the cohort and the empty queue held through exit.
                    with mutex:
                        busy = not queue_empty(current)
                        if not busy:
                            LOG.info("[openabstractions] ComfyUI stops after draining its queue")
                            exit(0)
                            stopped = True
                    return record

                try:
                    change = change or _atomic_change()
                    change(os.path.splitext(path)[0] + ".json", finish)
                except Exception as error:
                    LOG.warning("[openabstractions] ComfyUI could not verify its stop request: %s", error)
                if stopped:
                    return
                if busy and not announced:
                    announced = True
                    LOG.info("[openabstractions] OpenAbstractions stops ComfyUI once its current prompt ends")
        sleep(STOP_POLL_SECONDS)


_started = False


def report(environ=None, server=None, stop=None, change=None):
    """Start the thread that writes the report once ComfyUI listens. Returns
    False when there is nowhere to write it."""
    global _started
    folder = oa_folder(environ)
    if folder is None or _started:
        return False
    _started = True

    def wait():
        while stop is None or not stop.is_set():
            port = listening_port(server or _server(), _args())
            if port is not None:
                try:
                    path = write_report(folder, port, comfy_base(), _output_directory(), change=change)
                except Exception as error:  # the extension never stops ComfyUI from starting
                    LOG.warning("[openabstractions] ComfyUI could not report its port to OpenAbstractions: %s", error)
                    return
                atexit.register(remove_report, path)
                LOG.info("[openabstractions] ComfyUI listens on 127.0.0.1:%d for OpenAbstractions", port)
                watch_stop(stop_file(environ), lambda: server or _server(), stop=stop)
                return
            time.sleep(POLL_SECONDS)

    threading.Thread(target=wait, name="openabstractions-listening", daemon=True).start()
    return True


def model_folders(folder):
    """{category: folder} of Setup's oa-models group, only existing absolute
    folders of the three known categories."""
    if folder is None:
        return {}
    try:
        with open(os.path.join(folder, MODEL_PATHS_NAME), "rb") as stream:
            raw = stream.read(MODEL_PATHS_LIMIT + 1)
        if len(raw) > MODEL_PATHS_LIMIT:
            return {}
        group = json.loads(raw).get(MODEL_GROUP)
    except (OSError, ValueError, AttributeError):
        return {}
    if not isinstance(group, dict):
        return {}
    return {category: group[category] for category in MODEL_CATEGORIES
            if isinstance(group.get(category), str) and os.path.isabs(group[category]) and os.path.isdir(group[category])}


def add_model_folders(environ=None, add=None):
    """Add OA's prepared picture-model folders to ComfyUI's search paths.
    Returns the folders added."""
    folders = model_folders(oa_folder(environ))
    if not folders:
        return {}
    if add is None:
        try:
            import folder_paths
        except ImportError:
            return {}
        add = folder_paths.add_model_folder_path
    for category, path in folders.items():
        try:
            add(category, path)
        except Exception as error:  # the extension never stops ComfyUI from starting
            LOG.warning("[openabstractions] the %s folder %s is not added: %s", category, path, error)
    return folders
