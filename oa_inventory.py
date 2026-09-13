"""This ComfyUI process's accepted OA operations, served inside ComfyUI.

ABSTRACTION_COMFYUI_INVENTORY=service serves GET /abstraction/jobs. Each request
resolves abstraction.job/inventory@1 with the Python facade's
resolve_job_inventory and pages list_work from an empty cursor. The body lists
each operation snapshot in the contract's JSON, the last page outcome and
whether the enumeration completed. Unset leaves ComfyUI unchanged: no route and
no runtime call. Any other value is refused with one warning.

Inventory is scoped by the service to the authenticated caller, so the route
shows operations this account submitted, including ones accepted before a
ComfyUI restart. A refusal page (gap, forbidden, invalid, unavailable) ends the
listing and is returned as the outcome. Resolution or transport failure answers
503 with the error. A snapshot's label is display text; mapping an operation
back to a model uses the key in the model's .oa-request.json caller record.

configure_view(oa_jobs_view) makes the route do that mapping: the body then
also carries the rows a person reads, a change cursor a ?since= query compares
against, and the retry and cancel actions under the same path.
"""
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

ENV = "ABSTRACTION_COMFYUI_INVENTORY"
ROUTE = "/abstraction/jobs"
PAGE_LIMIT = 64
MAX_PAGES = 16
BUDGET_SECONDS = 10.0

OFF = "off"
SERVING = "serving"
INVALID_CONFIGURATION = "invalid_configuration"
SDK_UNAVAILABLE = "sdk_unavailable"
SERVER_UNAVAILABLE = "server_unavailable"

InventoryStatus = namedtuple("InventoryStatus", "enabled code route detail")
InventoryStatus.__doc__ = """Whether ComfyUI serves its OA job inventory.

enabled: the switch is set. code: OFF, SERVING, INVALID_CONFIGURATION,
SDK_UNAVAILABLE or SERVER_UNAVAILABLE. route: the served path, or None.
"""

LOG = logging.getLogger("abstraction")
_lock = threading.Lock()
_status = InventoryStatus(False, OFF, None, "not installed")
_machine_factory = None
_view = None
# Route tables this module added its route to; a table accepts a route once.
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
    """Supply independently configured trust for an embedding or a test.

    machine_factory(deadline) returns an object with resolve_job_inventory().
    No argument restores default installed-runtime selection.
    """
    global _machine_factory
    if machine_factory is not None and not callable(machine_factory):
        raise ValueError("machine_factory must be callable")
    _machine_factory = machine_factory


def configure_view(view=None):
    """Supply the Comfy-facing join, oa_jobs_view.

    With it the route adds the rows a person reads, a change cursor and the
    retry and cancel actions. Without it the route answers the operation
    listing alone, which is what a module loaded outside its package has.
    """
    global _view
    _view = view


def _default_machine(deadline):
    from abstraction.facade.client import Machine
    return Machine(deadline=deadline, library=oa_route and oa_route.library())


def _snapshot_json(snapshot):
    from abstraction.job.acceptance import to_json
    return json.loads(to_json(snapshot))


def list_operations(budget=BUDGET_SECONDS, limit=PAGE_LIMIT, max_pages=MAX_PAGES):
    """Return dict(outcome, complete, pages, operations). Resolution and transport errors raise."""
    deadline = time.monotonic() + budget
    from abstraction.facade import Scope
    inventory = (_machine_factory or _default_machine)(deadline).resolve_job_inventory(scope=Scope.LOCAL)
    operations, cursor, outcome, complete, pages = [], "", "page", False, 0
    while pages < max_pages:
        page = inventory.list_work(cursor, limit)
        pages += 1
        outcome = page.outcome
        if outcome != "page":
            break
        operations.extend(_snapshot_json(s) for s in page.snapshots)
        if page.complete:
            complete = True
            break
        cursor = page.next
        if time.monotonic() >= deadline:
            break
    return dict(outcome=outcome, complete=complete, pages=pages, operations=operations)


def listing(since=""):
    """One page-following inventory listing, joined with this ComfyUI's caller
    records when a view is configured."""
    body = list_operations()
    return body if _view is None else _view.enrich(body, since)


def cancel_work(identity, owner, budget=BUDGET_SECONDS):
    """Cancel a current inventory identity through the same selected job owner."""
    from abstraction.facade import Scope
    from abstraction.job.acceptance import RequestIdentity
    jobs = (_machine_factory or _default_machine)(time.monotonic() + budget).resolve_jobs(scope=Scope.LOCAL)
    if jobs.get_history_window().logical_owner != owner:
        return "unavailable"
    return jobs.cancel_work(RequestIdentity(**identity)).outcome


def _server_routes():
    """The running ComfyUI's route table. ComfyUI's main.py imports server before
    custom nodes load; importing it here would start ComfyUI's model management
    in a process that is not ComfyUI."""
    server = sys.modules.get("server")
    instance = getattr(getattr(server, "PromptServer", None), "instance", None)
    if instance is None:
        raise LookupError("no running ComfyUI PromptServer in this process")
    return instance.routes


def install(routes=None):
    """Apply ABSTRACTION_COMFYUI_INVENTORY. Returns the resulting InventoryStatus.

    routes is an aiohttp RouteTableDef; the default is ComfyUI's PromptServer
    routes. WEB_DIRECTORY is exported at import time, before this runs, so the
    sidebar tab is present in ComfyUI whatever this decides. GET /abstraction/jobs
    is therefore always registered -- switched off, misconfigured or the SDK
    unavailable answers 404 with the one explanation this module gives for its
    own status (the same detail sentence status() and the startup log carry),
    never ComfyUI's bare unrouted 404.
    """
    value = os.environ.get(ENV, "")
    try:
        if routes is None:
            routes = _server_routes()
    except Exception as error:
        return _not_serving(SERVER_UNAVAILABLE, "ComfyUI PromptServer routes: %r" % (error,))

    if value == "":
        outcome = InventoryStatus(False, OFF, None, "%s is unset" % ENV)
    elif value != "service":
        outcome = InventoryStatus(True, INVALID_CONFIGURATION, None,
                                  "%s=%r; supported values are unset and \"service\"" % (ENV, value))
    else:
        try:
            from abstraction.facade.jobs import Inventory as _Inventory  # noqa: F401
        except ImportError as error:
            outcome = InventoryStatus(True, SDK_UNAVAILABLE, None,
                                      "install the OA facade and job Python packages: %r" % (error,))
        else:
            outcome = InventoryStatus(True, SERVING, ROUTE, "GET %s lists this caller's OA operations" % ROUTE)

    if not any(table is routes for table in _registered):
        from aiohttp import web

        @routes.get(ROUTE)
        async def jobs(request):
            current = status()
            if current.code != SERVING:
                return web.json_response(dict(missing=True, detail=current.detail), status=404)
            import asyncio
            since = request.query.get("since", "")
            try:
                body = await asyncio.get_running_loop().run_in_executor(None, listing, since)
            except Exception as error:
                return web.json_response(dict(outcome="unresolved", error=repr(error)), status=503)
            return web.json_response(body)
        if outcome.code == SERVING and _view is not None:
            _view.register(routes)
        _registered.append(routes)

    if outcome.code == SERVING:
        LOG.info("[abstraction] GET %s lists this caller's OA operations", ROUTE)
    else:
        LOG.warning("[abstraction] INVENTORY NOT SERVED (%s): %s", outcome.code, outcome.detail)
    return _set(outcome)


def _not_serving(code, detail):
    LOG.warning("[abstraction] INVENTORY NOT SERVED (%s): %s", code, detail)
    return _set(InventoryStatus(True, code, None, detail))
