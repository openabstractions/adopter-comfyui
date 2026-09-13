"""Bounded observation of this ComfyUI caller's OA-managed downloads.

ABSTRACTION_COMFYUI_DOWNLOAD_OBSERVER=service serves POST
/abstraction/downloads/observe. The route observes an exact job RequestIdentity
through abstraction.job/operations@1. It never reads the job store or model
filesystem. A continuation is bound to the request identity and logical owner;
carrying it into another workflow returns stale before any runtime call.

Each request has a bounded wait. A lost runtime connection is re-resolved within
that wait. Observation cancellation stops only this HTTP wait and never cancels
accepted work.
"""
import hashlib
import json
import logging
import os
import sys
import threading
import time
from collections import namedtuple

try:
    from . import oa_route
except ImportError:  # a test loaded this file outside its package: no relay
    oa_route = None

ENV = "ABSTRACTION_COMFYUI_DOWNLOAD_OBSERVER"
ROUTE = "/abstraction/downloads/observe"
MAX_BODY = 4096
MAX_WAIT_MS = 30_000
CALL_ALLOWANCE_SECONDS = 2.0
POLL_SECONDS = 0.1
TERMINAL = frozenset(("complete", "failed", "cancelled"))

OFF = "off"
SERVING = "serving"
INVALID_CONFIGURATION = "invalid_configuration"
SDK_UNAVAILABLE = "sdk_unavailable"
SERVER_UNAVAILABLE = "server_unavailable"

ObserverStatus = namedtuple("ObserverStatus", "enabled code route detail")
LOG = logging.getLogger("abstraction")
_lock = threading.Lock()
_status = ObserverStatus(False, OFF, None, "not installed")
_machine_factory = None
_registered = []


def status():
    with _lock:
        return _status


def _set(value):
    global _status
    with _lock:
        _status = value
    return value


def configure(machine_factory=None):
    """Supply machine_factory(deadline) for independently trusted embedding/tests."""
    global _machine_factory
    if machine_factory is not None and not callable(machine_factory):
        raise ValueError("machine_factory must be callable")
    _machine_factory = machine_factory


def _default_machine(deadline):
    from abstraction.facade.client import Machine
    return Machine(deadline=deadline, library=oa_route and oa_route.library())


def _text(value, limit):
    return (isinstance(value, str) and 0 < len(value.encode("utf-8")) <= limit
            and "\0" not in value)


def _request(value):
    if not isinstance(value, dict) or set(value) - {"identity", "owner", "after", "wait_ms"}:
        raise ValueError("observation request has an unsupported shape")
    raw = value.get("identity")
    if not isinstance(raw, dict) or set(raw) - {"key", "history_epoch", "attempt"}:
        raise ValueError("exact request identity is required")
    key, epoch, attempt = raw.get("key"), raw.get("history_epoch"), raw.get("attempt", 0)
    if not _text(key, 128) or not _text(epoch, 128) or type(attempt) is not int or not 0 <= attempt < 2**31:
        raise ValueError("request identity is invalid")
    owner, after, wait_ms = value.get("owner", ""), value.get("after", ""), value.get("wait_ms", 0)
    if owner and not _text(owner, 128):
        raise ValueError("logical owner is invalid")
    if not isinstance(after, str) or len(after.encode("utf-8")) > 160:
        raise ValueError("observation cursor is invalid")
    if after and not owner:
        raise ValueError("continuation requires its logical owner")
    if type(wait_ms) is not int or not 0 <= wait_ms <= MAX_WAIT_MS:
        raise ValueError("wait_ms must be 0..30000")
    return key, epoch, attempt, owner, after, wait_ms


def _scope(key, epoch, attempt, owner):
    raw = json.dumps([key, epoch, attempt, owner], ensure_ascii=False,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def _cursor(key, epoch, attempt, owner, snapshot):
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return "v1.%s.%s" % (_scope(key, epoch, attempt, owner), hashlib.sha256(encoded).hexdigest())


def _snapshot_json(snapshot):
    from abstraction.job.acceptance import to_json
    return json.loads(to_json(snapshot))


def observe_download(request):
    """Observe one exact accepted download, reconnecting within its wait budget."""
    key, epoch, attempt, owner, after, wait_ms = _request(request)
    if after and not after.startswith("v1.%s." % _scope(key, epoch, attempt, owner)):
        return {"outcome": "stale", "reason": "workflow_replaced"}

    from abstraction.job.acceptance import RequestIdentity
    identity = RequestIdentity(key=key, history_epoch=epoch, attempt=attempt)
    wait_end = time.monotonic() + wait_ms / 1000
    deadline = wait_end + CALL_ALLOWANCE_SECONDS
    binding, last_error = None, None
    while True:
        try:
            if binding is None:
                from abstraction.facade import Scope
                binding = (_machine_factory or _default_machine)(deadline).resolve_jobs(scope=Scope.LOCAL)
            observed = binding.observe_work(identity)
            if observed.outcome == "observed":
                snapshot = _snapshot_json(observed.snapshot)
                actual_owner = snapshot["receipt"]["logical_owner"]
                if owner and actual_owner != owner:
                    return {"outcome": "stale", "reason": "logical_owner_changed"}
                current = _cursor(key, epoch, attempt, actual_owner, snapshot)
                result = {"outcome": "settled" if snapshot["state"] in TERMINAL else "changed",
                          "owner": actual_owner, "cursor": current, "snapshot": snapshot}
                if result["outcome"] == "settled" or current != after:
                    return result
                if time.monotonic() >= wait_end:
                    result["outcome"] = "unchanged"
                    return result
            elif observed.outcome != "unavailable":
                return {"outcome": observed.outcome}
            else:
                last_error = "operation observation unavailable"
                binding = None
        except Exception as error:
            last_error = repr(error)
            binding = None
        if time.monotonic() >= wait_end:
            return {"outcome": "unavailable", "reason": "runtime_interrupted",
                    "error": last_error}
        time.sleep(min(POLL_SECONDS, max(0, wait_end - time.monotonic())))


def _server_routes():
    server = sys.modules.get("server")
    instance = getattr(getattr(server, "PromptServer", None), "instance", None)
    if instance is None:
        raise LookupError("no running ComfyUI PromptServer in this process")
    return instance.routes


def install(routes=None):
    value = os.environ.get(ENV, "")
    if value == "":
        return _set(ObserverStatus(False, OFF, None, "%s is unset" % ENV))
    if value != "service":
        return _not_serving(INVALID_CONFIGURATION,
                            "%s=%r; supported values are unset and \"service\"" % (ENV, value))
    try:
        from abstraction.facade.jobs import Jobs as _Jobs  # noqa: F401
    except ImportError as error:
        return _not_serving(SDK_UNAVAILABLE,
                            "install the OA facade and job Python packages: %r" % (error,))
    try:
        routes = _server_routes() if routes is None else routes
    except Exception as error:
        return _not_serving(SERVER_UNAVAILABLE, "ComfyUI PromptServer routes: %r" % (error,))
    if not any(table is routes for table in _registered):
        from aiohttp import web

        @routes.post(ROUTE)
        async def observe(request):
            import asyncio
            if request.content_length is not None and request.content_length > MAX_BODY:
                raise web.HTTPRequestEntityTooLarge(max_size=MAX_BODY, actual_size=request.content_length)
            raw = await request.content.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                raise web.HTTPRequestEntityTooLarge(max_size=MAX_BODY, actual_size=len(raw))
            try:
                body = json.loads(raw)
                result = await asyncio.get_running_loop().run_in_executor(None, observe_download, body)
            except (ValueError, UnicodeError, json.JSONDecodeError) as error:
                return web.json_response({"outcome": "invalid", "error": str(error)}, status=400)
            return web.json_response(result)
        _registered.append(routes)
    LOG.info("[abstraction] POST %s observes exact OA download operations", ROUTE)
    return _set(ObserverStatus(True, SERVING, ROUTE,
                               "POST %s observes exact OA download operations" % ROUTE))


def _not_serving(code, detail):
    LOG.warning("[abstraction] DOWNLOAD OBSERVER NOT SERVED (%s): %s", code, detail)
    return _set(ObserverStatus(True, code, None, detail))
