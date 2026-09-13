"""The frontend's model downloads through the OA download service.

ABSTRACTION_COMFYUI_MODEL_DOWNLOADS=service serves POST
/openabstractions/models/download for web/oa_model_downloads.js, which takes
the missing-model dialog's Download clicks. A request names one model the way
the frontend's workflow model list does: url, name, directory and an optional
sha256 hash. The route accepts the same downloads the frontend allows (an
https URL on huggingface.co, civitai.com or civitai.red, a model file
extension and a ComfyUI model folder) and delivers the file into the folder's
first path through service.deliver. A request carries its page's Origin, and
only ComfyUI's own origin is served.

GET /openabstractions/models/download/<id> answers the delivery's state:
running, complete, waiting (OA keeps downloading after the wait), unavailable
(OA could not be reached and recorded no work) or failed. The page hands an
unavailable or failed model back to the frontend's own downloader. When OA
reports work, the route includes its phase and byte counters.
"""
import ipaddress
import logging
import os
import re
import sys
import threading
import uuid
from collections import namedtuple
from pathlib import Path
from urllib.parse import urlsplit

from ._model_delivery import ModelDestination

LOG = logging.getLogger("abstraction")

ENV = "ABSTRACTION_COMFYUI_MODEL_DOWNLOADS"
ROUTE = "/openabstractions/models/download"
MAX_BODY = 8192
# The frontend's own rule (frontend 1.58.5 isModelDownloadable).
ALLOWED_PREFIXES = ("https://huggingface.co/", "https://civitai.com/", "https://civitai.red/")
MODEL_EXTENSION = re.compile(r"\.(?:safetensors|sft|ckpt|pth|pt)\Z", re.IGNORECASE | re.ASCII)
SHA256_TYPE = re.compile(r"sha256", re.IGNORECASE | re.ASCII)
# Folders that hold ComfyUI code or settings, never a downloaded model.
NOT_MODEL_FOLDERS = frozenset(("custom_nodes", "configs"))
KEEP_FINISHED = 256

Status = namedtuple("Status", "enabled code route detail")
_lock = threading.Lock()
_status = Status(False, "off", None, "not installed")
_deliveries = {}
_registered = []


def status():
    with _lock:
        return _status


def _set(value):
    global _status
    with _lock:
        _status = value
    return value


class Refused(ValueError):
    """A request outside the frontend's own download rule."""


def _allowed_url(url):
    if not isinstance(url, str) or len(url) > 4096 or any(c in url for c in "\r\n\0"):
        return False
    prefixes = list(ALLOWED_PREFIXES)
    endpoint = os.environ.get("HF_ENDPOINT", "").rstrip("/")
    if endpoint.startswith("https://") and endpoint != os.environ.get("ABSTRACTION_COMFYUI_HUB_ENDPOINT", ""):
        prefixes.append(endpoint + "/")
    return any(url.startswith(prefix) for prefix in prefixes)


def _digest(hash_value, hash_type):
    if hash_value in (None, "") and hash_type in (None, ""):
        return ""
    if not isinstance(hash_type, str) or not SHA256_TYPE.fullmatch(hash_type):
        return ""
    from .storage_reuse import sha256_hex
    found = sha256_hex(hash_value)
    return "sha256:" + found if found else ""


def _target(request, folder_paths):
    if not isinstance(request, dict) or set(request) - {"url", "name", "directory", "hash", "hash_type"}:
        raise Refused("model download request has an unsupported shape")
    url, name, directory = request.get("url"), request.get("name"), request.get("directory")
    if not _allowed_url(url):
        raise Refused("only https downloads from huggingface.co and civitai are taken")
    if (not isinstance(name, str) or not 0 < len(name) <= 255 or "\\" in name or "\0" in name
            or not MODEL_EXTENSION.search(name)):
        raise Refused("the model name must be a model file")
    segments = name.split("/")
    if any(not part or part.startswith(".") or ":" in part for part in segments) or (
            os.name == "nt" and any(part.endswith((".", " ")) or Path(part).is_reserved()
                                    for part in segments)):
        raise Refused("the model name must stay inside its folder")
    if not isinstance(directory, str) or directory in NOT_MODEL_FOLDERS:
        raise Refused("the model folder is not a ComfyUI model folder")
    known = getattr(folder_paths, "folder_names_and_paths", {})
    if directory not in known:
        raise Refused("the model folder is not a ComfyUI model folder")
    paths = folder_paths.get_folder_paths(directory)
    if not paths:
        raise Refused("the model folder has no path")
    folder = os.path.abspath(paths[0])
    return url, folder, segments[:-1], segments[-1], _digest(request.get("hash"), request.get("hash_type"))


def target(request, folder_paths):
    """(url, folder, name, digest) of a syntactically validated request."""
    url, root, parents, name, digest = _target(request, folder_paths)
    return url, os.path.join(root, *parents), name, digest


def _deliver(token, url, folder, name, digest, destination):
    from . import service

    def observe(snapshot):
        progress = snapshot.progress
        with _lock:
            _deliveries[token].update(phase=str(snapshot.state), bytes_done=progress.done, bytes_total=progress.total)

    try:
        service.deliver(url, folder, name, digest=digest, _observe=observe, _destination=destination)
        state, error = "complete", ""
        LOG.info("[abstraction] %s arrived through OA from the missing-model dialog", name)
    except service.ServiceUnavailable as unreachable:
        state, error = "unavailable", str(unreachable)
        LOG.warning("[abstraction] OA is not reachable; the frontend downloads %s itself: %s", name, unreachable)
    except TimeoutError as waited:
        state, error = "waiting", str(waited)
        LOG.warning("[abstraction] %s is still downloading in OA: %s", name, waited)
    except Exception as failed:
        state, error = "failed", repr(failed)
        LOG.error("[abstraction] OA download of %s failed; the frontend downloads it itself: %r", name, failed)
    with _lock:
        _deliveries[token].update(state=state, error=error)


def start(request, folder_paths, *, run=None):
    """Validate request and start its delivery. Returns the answer body."""
    url, root, parents, name, digest = _target(request, folder_paths)
    folder = os.path.join(root, *parents)
    try:
        destination = ModelDestination(root, parents)
        try:
            with destination.hold() as files:
                if files.present(name):
                    return {"outcome": "present", "name": name}
        except FileNotFoundError:
            pass
    except (OSError, ValueError) as error:
        raise Refused("the model name must stay inside its folder") from error
    token = uuid.uuid4().hex
    with _lock:
        finished = [key for key, value in _deliveries.items() if value["state"] != "running"]
        for key in finished[:max(0, len(_deliveries) - KEEP_FINISHED)]:
            del _deliveries[key]
        _deliveries[token] = dict(state="running", name=name, error="")
    (run or _spawn)(_deliver, token, url, folder, name, digest, destination)
    return {"outcome": "accepted", "id": token, "name": name}


def _spawn(function, *args):
    threading.Thread(target=function, args=args, name="oa-model-download", daemon=True).start()


def delivery(token):
    with _lock:
        found = _deliveries.get(token)
        return dict(found) if found else None


def same_origin(headers, scheme, host, local=None):
    """True when the request's Origin is this server's own origin. local is
    the address the request arrived on; on a loopback address the Host must
    name loopback too, so a page whose name was rebound to 127.0.0.1 is
    refused although its Origin and Host agree."""
    origin = headers.get("Origin")
    if not (isinstance(origin, str) and isinstance(host, str) and origin == "%s://%s" % (scheme, host)):
        return False
    return not _loopback(local) or _loopback_name(host)


def _loopback(address):
    try:
        return ipaddress.ip_address(str(address).split("%")[0]).is_loopback
    except ValueError:
        return False


def _loopback_name(host):
    try:
        name = urlsplit("//" + host).hostname
    except ValueError:
        return False
    return name == "localhost" or _loopback(name)


def _server_routes():
    server = sys.modules.get("server")
    instance = getattr(getattr(server, "PromptServer", None), "instance", None)
    if instance is None:
        raise LookupError("no running ComfyUI PromptServer in this process")
    return instance.routes


def install(routes=None, folder_paths=None):
    value = os.environ.get(ENV, "")
    if value == "":
        return _set(Status(False, "off", None, "%s is unset" % ENV))
    if value != "service":
        LOG.warning("[abstraction] MODEL DOWNLOADS NOT SERVED: %s=%r; supported values are unset and service", ENV, value)
        return _set(Status(True, "invalid_configuration", None, "%s=%r" % (ENV, value)))
    try:
        routes = _server_routes() if routes is None else routes
        if folder_paths is None:
            import folder_paths
    except Exception as error:
        LOG.warning("[abstraction] MODEL DOWNLOADS NOT SERVED: %r", error)
        return _set(Status(True, "server_unavailable", None, repr(error)))
    if not any(table is routes for table in _registered):
        from aiohttp import web

        @routes.post(ROUTE)
        async def post(request):
            sockname = request.transport.get_extra_info("sockname") if request.transport is not None else None
            local = sockname[0] if isinstance(sockname, tuple) and sockname else None
            if not same_origin(request.headers, request.scheme, request.host, local):
                return web.json_response({"outcome": "refused", "error": "cross-origin request"}, status=403)
            raw = await request.content.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                raise web.HTTPRequestEntityTooLarge(max_size=MAX_BODY, actual_size=len(raw))
            import json
            try:
                answer = start(json.loads(raw), folder_paths)
            except (Refused, ValueError, UnicodeError) as error:
                return web.json_response({"outcome": "refused", "error": str(error)}, status=400)
            return web.json_response(answer)

        @routes.get(ROUTE + "/{token}")
        async def get(request):
            found = delivery(request.match_info["token"])
            if found is None:
                return web.json_response({"state": "unknown"}, status=404)
            return web.json_response(found)
        _registered.append(routes)
    LOG.info("[abstraction] POST %s takes the missing-model dialog's downloads", ROUTE)
    return _set(Status(True, "serving", ROUTE, "POST %s delivers through OA" % ROUTE))
