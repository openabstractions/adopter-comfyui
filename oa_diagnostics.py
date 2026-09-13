"""OA runtime readiness per capability, reported inside ComfyUI.

ABSTRACTION_COMFYUI_DIAGNOSTICS=service composes one
abstraction.facade RuntimeObservation for the contracts this adapter uses,
logs one startup line per contract, and serves the latest observation at
GET /abstraction/diagnostics. Each request to the route observes again. Unset
leaves ComfyUI unchanged: no route, no line, no runtime call. Any other value is
refused with one warning.

The observation follows the facade contract. Bootstrap is unknown: the Python
facade has no platform observation. Each capability entry carries its
ResolveRequest and, when the resolver answered, its ResolveResult. The first
transport or selection error stops querying; the entries after it keep an
absent result, meaning unobserved. A resolved entry is a candidate binding and
does not prove that a provider call will succeed.
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

ENV = "ABSTRACTION_COMFYUI_DIAGNOSTICS"
ROUTE = "/abstraction/diagnostics"
SCOPE = "local"
BUDGET_SECONDS = 2.0

OFF = "off"
SERVING = "serving"
INVALID_CONFIGURATION = "invalid_configuration"
SDK_UNAVAILABLE = "sdk_unavailable"
SERVER_UNAVAILABLE = "server_unavailable"

DiagnosticsStatus = namedtuple("DiagnosticsStatus", "enabled code route detail")
DiagnosticsStatus.__doc__ = """Whether ComfyUI serves OA runtime readiness.

enabled: the switch is set. code: OFF, SERVING, INVALID_CONFIGURATION,
SDK_UNAVAILABLE or SERVER_UNAVAILABLE. route: the served path, or None.
"""

LOG = logging.getLogger("abstraction")
_lock = threading.Lock()
_status = DiagnosticsStatus(False, OFF, None, "not installed")
_resolver_factory = None
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


def configure(resolver_factory=None):
    """Supply independently configured trust for an embedding or a test.

    resolver_factory(deadline) returns an object with resolve(ResolveRequest),
    such as the generated ResolverClient over a trusted transport. No argument
    restores default installed-runtime selection.
    """
    global _resolver_factory
    if resolver_factory is not None and not callable(resolver_factory):
        raise ValueError("resolver_factory must be callable")
    _resolver_factory = resolver_factory


def requests():
    """One ResolveRequest per contract the adapter uses, with the adapter's guarantees."""
    import abstraction.facade as wire
    import abstraction.job.acceptance as job
    admission = list(job.ADMISSION_GUARANTEES)
    wanted = (
        ("abstraction.job", "abstraction.job/acceptance@1", admission),
        ("abstraction.job", "abstraction.job/inventory@1", []),
        ("abstraction.logging", "abstraction.logging/sink@1", []),
        ("abstraction.model", "abstraction.model/resolver@1", []),
        ("abstraction.storage", "abstraction.storage/content-reader@1", []),
    )
    return [wire.ResolveRequest(capability=capability, contracts=[contract], guarantees=list(guarantees), scope=SCOPE)
            for capability, contract, guarantees in wanted]


def _default_resolver(deadline):
    import abstraction.facade as wire
    from abstraction.ipc import FrameTransport, Library
    library = oa_route.native() if oa_route is not None else Library()
    server = library.select_runtime(deadline=deadline)
    endpoint = library.runtime_endpoint()
    return wire.ResolverClient(FrameTransport(library, endpoint, server=server, deadline=deadline))


def observe(budget=BUDGET_SECONDS):
    """Return (RuntimeObservation, error). error is None or the exception that stopped querying."""
    import abstraction.facade as wire
    report = wire.RuntimeObservation(bootstrap=wire.BootstrapObservation(
                                         state="unknown", detail="the Python facade has no platform observation"),
                                     capabilities=[wire.CapabilityObservation(request=r) for r in requests()])
    deadline = time.monotonic() + budget
    try:
        resolver = (_resolver_factory or _default_resolver)(deadline)
        for entry in report.capabilities:
            if time.monotonic() >= deadline:
                raise TimeoutError("diagnostics budget of %.1f s expired" % budget)
            entry.result = resolver.resolve(entry.request)
    except Exception as error:
        return report, error
    return report, None


def to_json(report, error):
    """The route body: the contract's RuntimeObservation JSON plus a separate error."""
    from abstraction.facade import to_json as record_json
    return dict(observation=json.loads(record_json(report)), error=None if error is None else repr(error))


def summary(report, error):
    """One (contract, text) pair per capability, in request order."""
    lines = []
    for entry in report.capabilities:
        contract = entry.request.contracts[0]
        result = entry.result
        if result is None:
            lines.append((contract, "unobserved"))
        elif result.status == "resolved" and result.reference is not None:
            ref = result.reference
            lines.append((contract, "resolved (provider=%s, scope=%s, transport=%s)" % (ref.provider, ref.scope, ref.transport)))
        else:
            lines.append((contract, result.status))
    return lines


def log_readiness():
    report, error = observe()
    LOG.info("[abstraction] OA runtime readiness (bootstrap=%s)%s", report.bootstrap.state,
             "" if error is None else "; observation stopped: %r" % (error,))
    for contract, text in summary(report, error):
        LOG.info("[abstraction]   %s: %s", contract, text)
    return report, error


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
    """Apply ABSTRACTION_COMFYUI_DIAGNOSTICS. Returns the resulting DiagnosticsStatus.

    routes is an aiohttp RouteTableDef; the default is ComfyUI's PromptServer routes.
    """
    value = os.environ.get(ENV, "")
    if value == "":
        return _set(DiagnosticsStatus(False, OFF, None, "%s is unset" % ENV))
    if value != "service":
        return _not_serving(INVALID_CONFIGURATION, "%s=%r; supported values are unset and \"service\"" % (ENV, value))
    try:
        import abstraction.facade as _wire  # noqa: F401
        from abstraction.ipc import Library as _Library  # noqa: F401
        import abstraction.job.acceptance as _job  # noqa: F401
    except ImportError as error:
        return _not_serving(SDK_UNAVAILABLE, "install the OA facade Python package: %r" % (error,))
    try:
        if routes is None:
            routes = _server_routes()
    except Exception as error:
        return _not_serving(SERVER_UNAVAILABLE, "ComfyUI PromptServer routes: %r" % (error,))
    if not any(table is routes for table in _registered):
        from aiohttp import web

        @routes.get(ROUTE)
        async def diagnostics(request):
            import asyncio
            report, error = await asyncio.get_running_loop().run_in_executor(None, observe)
            return web.json_response(to_json(report, error))
        _registered.append(routes)
    log_readiness()
    return _set(DiagnosticsStatus(True, SERVING, ROUTE, "GET %s observes the OA runtime" % ROUTE))


def _not_serving(code, detail):
    LOG.warning("[abstraction] DIAGNOSTICS NOT SERVED (%s): %s", code, detail)
    return _set(DiagnosticsStatus(True, code, None, detail))
