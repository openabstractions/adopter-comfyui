"""ComfyUI Python logging through the OA logging service.

ABSTRACTION_COMFYUI_LOGGING=service attaches a ServiceHandler from the logging
package (abstraction.logging.handler) to ComfyUI's root logger. The
"OpenAbstractions for ComfyUI" extension sets it. ComfyUI's own handlers stay
as they are: the console, the log file, /internal/logs and the terminal view
keep every record. Unset leaves ComfyUI's logging unchanged.

Each record keeps its level, logger name, message, time, source and formatted
exception, cut at MAX_MESSAGE_BYTES. The extension's relay (oa_route) makes the
calls, so the service records the program OpenAbstractions knows this ComfyUI
by, the connector, as the records' verified emitter.

Fail open. install() never raises and never waits on OpenAbstractions: the
handler resolves abstraction.logging/sink@1 on its own delivery thread at the
first record. A runtime that is absent at start or goes away later leaves
ComfyUI logging as usual; the records stay queued and reach the service once
it answers [LOG-S12]. An unsupported switch value or a missing logging package
writes one line and attaches nothing.

ComfyUI's thread only converts a record and queues it. The queue holds 1024
records; a full queue drops the newest records and writes one line [LOG-S12,
LOG-S13]. Records dropped during an outage are named in one record the service
receives when it answers again. A logger that writes more than RATE records a second over a burst of
BURST is held back: one line names it, and its next record after the pause is
preceded by one naming how many were held back. Before a record leaves, its
message and exception go through OA's redaction (oa_redact.py).

status() carries the handler's counts. oa_logging.configure(factory) followed
by oa_logging.install() supplies independently configured trust.
"""
import dataclasses
import functools
import logging
import os
import threading
import time
from collections import namedtuple

try:
    from . import oa_redact, oa_route
except ImportError:  # a test loaded this file outside its package
    import importlib.util as _util
    _spec = _util.spec_from_file_location("oa_redact", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                                    "oa_redact.py"))
    oa_redact = _util.module_from_spec(_spec)
    _spec.loader.exec_module(oa_redact)
    oa_route = None

ENV = "ABSTRACTION_COMFYUI_LOGGING"
PROGRAM = "ComfyUI"
MAX_MESSAGE_BYTES = 8192
# A first delivery starts the extension's relay (oa_relay.START_SECONDS).
WRITE_TIMEOUT = 30.0
RATE = 20.0
BURST = 200
# Logger names the rate limiter tracks before it starts over.
LIMITER_NAMES = 4096

OFF = "off"
ATTACHED = "attached"
INVALID_CONFIGURATION = "invalid_configuration"
SDK_UNAVAILABLE = "sdk_unavailable"
SERVICE_UNAVAILABLE = "service_unavailable"
WRITE_FAILED = "write_failed"

LoggingStatus = namedtuple("LoggingStatus", "enabled attached code written failed dropped queued detail")
LoggingStatus.__doc__ = """Whether ComfyUI logging reaches the OA logging service.

enabled: the switch is set. attached: the handler is on the root logger.
code: OFF, ATTACHED, INVALID_CONFIGURATION, SDK_UNAVAILABLE,
SERVICE_UNAVAILABLE (attached, and the service has not answered yet) or
WRITE_FAILED (attached, and retrying a service that went away). written,
failed, dropped, queued: the handler's record counts.
"""

LOG = logging.getLogger("abstraction")
_lock = threading.Lock()
_status = LoggingStatus(False, False, OFF, 0, 0, 0, 0, "not installed")
_handler = None
_machine_factory = None


def status():
    with _lock:
        current, handler = _status, _handler
    if handler is None:
        return current
    counts = handler.counts()
    if counts.failing:
        code = WRITE_FAILED if handler.bound else SERVICE_UNAVAILABLE
        detail = counts.last_error
    else:
        code, detail = ATTACHED, current.detail
    return current._replace(code=code, written=counts.written, failed=counts.failed, dropped=counts.dropped,
                            queued=counts.queued, detail=detail)


def configure(machine_factory=None):
    """Supply independently configured trust for an embedding or a test.

    The factory returns an object with resolve_log(). The handler calls it at
    its first delivery and again to rebind when the service disappears. Call
    install() afterwards. No argument restores default installed-runtime
    discovery through the extension's relay.
    """
    global _machine_factory
    if machine_factory is not None and not callable(machine_factory):
        raise ValueError("machine_factory must be callable")
    _machine_factory = machine_factory


def _set(value):
    global _status
    with _lock:
        _status = value
    return value


def _not_attached(code, detail):
    LOG.warning("[abstraction] ComfyUI logs as usual; its records do not go to OpenAbstractions: %s", detail)
    return _set(LoggingStatus(True, False, code, 0, 0, 0, 0, detail))


class _Limiter:
    """A token bucket per logger name: RATE records a second over a burst of BURST."""

    def __init__(self, rate=RATE, burst=BURST, clock=time.monotonic):
        self.rate, self._burst, self._clock = rate, burst, clock
        self._buckets = {}

    def admit(self, name):
        """(admitted, held back before this record, the first held back record)."""
        now = self._clock()
        bucket = self._buckets.get(name)
        if bucket is None:
            if len(self._buckets) >= LIMITER_NAMES:
                self._buckets.clear()
            bucket = self._buckets[name] = [float(self._burst), now, 0]
        bucket[0] = min(float(self._burst), bucket[0] + (now - bucket[1]) * self.rate)
        bucket[1] = now
        if bucket[0] < 1.0:
            bucket[2] += 1
            return False, 0, bucket[2] == 1
        bucket[0] -= 1.0
        held, bucket[2] = bucket[2], 0
        return True, held, False


UNREDACTED = "a record OpenAbstractions could not redact was dropped"


def _redacted(record):
    """record with its message and exception redacted. A redaction that
    fails gives the record UNREDACTED as its message and no exception: the
    delivery thread retries a record whose write raised, and the same record
    would fail the same way every time."""
    try:
        attrs = record.attrs
        if "exception" in attrs:
            attrs = dict(attrs, exception=oa_redact.text(attrs["exception"]))
        return dataclasses.replace(record, msg=oa_redact.text(record.msg), attrs=attrs)
    except Exception as error:
        LOG.warning("[abstraction] %s: %s", UNREDACTED, type(error).__name__)
        attrs = {name: value for name, value in record.attrs.items() if not name.startswith("exception")}
        return dataclasses.replace(record, msg=UNREDACTED, attrs=attrs)


class _Sink:
    """The resolved sink, made on the delivery thread at its first write.
    Every record leaves with its message and exception redacted."""

    def __init__(self, resolve, sink=None):
        self._resolve, self._sink = resolve, sink

    def write(self, record):
        if self._sink is None:
            self._sink = self._resolve()
        self._sink.write(_redacted(record))


def _handler_class(service_handler):
    class ComfyHandler(service_handler):
        """The logging package's handler with a rate limit per logger and a sink resolved
        on the delivery thread."""

        def __init__(self, resolve, limiter=None, **options):
            self._resolve = resolve
            self.bound = False
            self._limiter = limiter or _Limiter()
            super().__init__(_Sink(self._first), rebind=self._again, **options)

        def _first(self):
            sink = self._resolve()
            self.bound = True
            return sink

        def _again(self):
            return _Sink(self._resolve, self._first())

        def emit(self, record):
            admitted, held, first = self._limiter.admit(record.name)
            if not admitted:
                if first:
                    LOG.warning("[abstraction] logger %r writes over %g records a second; OpenAbstractions gets "
                                "its records again after a pause", record.name, self._limiter.rate)
                return
            if held:
                note = logging.makeLogRecord({
                    "name": record.name, "levelno": logging.WARNING, "levelname": "WARNING",
                    "msg": "%d records from this logger were held back by the rate limit", "args": (held,)})
                super().emit(note)
            super().emit(record)

    return ComfyHandler


def _failed(error, counts):
    handler = _handler
    if handler is not None and not handler.bound:
        LOG.warning("[abstraction] OA LOGGING SERVICE UNAVAILABLE (%s): ComfyUI logs as usual, and its records "
                    "reach OpenAbstractions once it answers (%d queued): %r", SERVICE_UNAVAILABLE, counts.queued,
                    error)
        return
    LOG.warning("[abstraction] LOGGING SERVICE UNREACHABLE (%s): records stay queued and are delivered when the "
                "OA logging service returns (%d queued, %d dropped): %r", WRITE_FAILED, counts.queued, counts.dropped,
                error)


def _recovered(counts):
    LOG.info("[abstraction] ComfyUI logging reaches the OA logging service again (%d dropped)", counts.dropped)


def _overflowed(counts):
    LOG.warning("[abstraction] LOGGING QUEUE FULL: the newest records are dropped until the OA logging service "
                "catches up (%d queued, %d dropped)", counts.queued, counts.dropped)


def install(limiter=None):
    """Apply ABSTRACTION_COMFYUI_LOGGING and return the resulting LoggingStatus.

    Never raises and never waits on the service. limiter replaces the rate
    limiter (tests)."""
    global _handler
    root = logging.getLogger()
    with _lock:
        previous, _handler = _handler, None
    if previous is not None:
        root.removeHandler(previous)
        previous.close(1.0)
    value = os.environ.get(ENV, "")
    if value == "":
        return _set(LoggingStatus(False, False, OFF, 0, 0, 0, 0, "%s is unset" % ENV))
    if value != "service":
        return _not_attached(INVALID_CONFIGURATION, "%s=%r; supported values are unset and \"service\"" % (ENV, value))
    try:
        from abstraction.logging.handler import ServiceHandler
        from abstraction.facade import Scope
        factory = _machine_factory
        if factory is None:
            from abstraction.facade.client import Machine as factory
            if oa_route is not None and oa_route.library() is not None:
                factory = functools.partial(factory, library=oa_route.library())
    except ImportError as error:
        return _not_attached(SDK_UNAVAILABLE, "the OA facade and logging Python packages are missing: %r" % (error,))

    def resolve():
        return factory().resolve_log(scope=Scope.LOCAL)

    handler = _handler_class(ServiceHandler)(resolve, limiter=limiter, program=PROGRAM,
                                             max_message_bytes=MAX_MESSAGE_BYTES, write_timeout=WRITE_TIMEOUT,
                                             on_failure=_failed, on_recovery=_recovered, on_overflow=_overflowed)
    _set(LoggingStatus(True, True, ATTACHED, 0, 0, 0, 0, "root logger records go to abstraction.logging/sink@1"))
    with _lock:
        _handler = handler
    root.addHandler(handler)
    LOG.info("[abstraction] ComfyUI logging goes to the OA logging service")
    return status()
