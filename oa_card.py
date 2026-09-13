"""This ComfyUI asks for the card before it loads weights, and gives it back.

ABSTRACTION_COMFYUI_CARD=service makes this ComfyUI hold a lease on card:0 for
the weights it has loaded, through abstraction.resource/leases@1, and take it
before the bytes are read. The service decides abstraction.resource/hold on
card:0 for this ComfyUI's program, reads who holds the card, asks the holders
it has a mechanism for, and answers acquired or a typed refusal
(abstraction-resource CONTRACT.md RES-L1, RES-L2). A refusal raises CardRefused
out of the loader, so the render stops before a byte is allocated and the
reason reaches ComfyUI's queue. Unset leaves ComfyUI unchanged: no wrapper, no
lease and no runtime call.

The seam is comfy.utils.load_torch_file because it is the one function every
model loader node reads a weights file through -- CheckpointLoaderSimple,
UNETLoader, CLIPLoader, VAELoader and the LoRA loaders all reach it -- and it
is given the path, so the amount is the files' own sizes rather than an
estimate.

One ComfyUI holds a set of leases whose combined bytes cover the weights files
it has loaded. A workflow that loads a diffusion model, a text encoder and a
VAE grows the hold by each file's positive size delta; a file that fits inside
the amount already held asks nobody. The resource service aggregates grants
for this same ComfyUI and counts measured use once (RES-L2).

A lease is renewed while ComfyUI holds the weights and released when ComfyUI
unloads them. The release seam is comfy.model_management.unload_all_models,
which is what POST /free {"unload_models": true} and ComfyUI's own
between-prompt eviction call, and what the resource service itself uses to
yield ComfyUI on another asker's behalf. The interpreter's exit releases the
rest.

ABSTRACTION_COMFYUI_CARD_RESERVE is how much of the card this ComfyUI holds
while it renders, whatever its weights weigh: the working memory a render needs
on top of them. The lease is the larger of that and the weights. The default is
0. ABSTRACTION_COMFYUI_CARD_WAIT_SECONDS is how long holders have to yield,
0..120, default 60.
"""
import atexit
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

ENV = "ABSTRACTION_COMFYUI_CARD"
RESERVE_ENV = "ABSTRACTION_COMFYUI_CARD_RESERVE"
WAIT_ENV = "ABSTRACTION_COMFYUI_CARD_WAIT_SECONDS"

# The resource a ComfyUI render needs. One card is what this release arbitrates.
RESOURCE = "card:0"

# The rights rule the service decides for this ComfyUI's program before it
# reads the table (RES-L1). Named here so the refusal can say it.
ACTION_HOLD = "abstraction.resource/hold"

# A hold smaller than this is not asked for. It is the Windows instrument's own
# row threshold (CONTRACT.md RES-T2): a hold the table would never report is
# not a hold worth arbitrating, and a workflow reads small files -- vae_approx,
# embeddings, a few-megabyte LoRA -- constantly.
FLOOR_BYTES = 256 << 20

# How long a resolve and one lease call may take.
CONTROL_BUDGET_SECONDS = 20.0

# How often a live lease is renewed. The service releases a lease not renewed
# within its DefaultLifetime of 30 s (RES-L4); a third of that survives one
# missed tick.
RENEW_SECONDS = 10.0

DEFAULT_WAIT_SECONDS = 60.0
MAX_WAIT_SECONDS = 120.0

OFF = "off"
HOLDING = "holding"
INVALID_CONFIGURATION = "invalid_configuration"
SDK_UNAVAILABLE = "sdk_unavailable"
COMFY_UNAVAILABLE = "comfy_unavailable"

CardStatus = namedtuple("CardStatus", "enabled code resource detail")
CardStatus.__doc__ = """Whether this ComfyUI asks for the card before it loads.

enabled: the switch is set. code: OFF, HOLDING, INVALID_CONFIGURATION,
SDK_UNAVAILABLE or COMFY_UNAVAILABLE. resource: the resource leased, or None.
"""

Held = namedtuple("Held", "lease amount files leases")
Held.__doc__ = """What this ComfyUI holds of the card now.

lease: primary lease id, or None when nothing is held. leases: all live owned
lease ids. amount: their combined granted bytes.
files: {absolute path: size} of the weights the lease covers.
"""

LOG = logging.getLogger("abstraction")


class CardRefused(RuntimeError):
    """The card was not granted, so these weights were not loaded.

    outcome is the contract's word (holders_refused, not_permitted,
    insufficient, unavailable, invalid). asked lists every holder the service
    asked, in order, with what each answered. path and amount name what this
    render wanted.
    """

    def __init__(self, outcome, resource, amount, path, asked, detail=""):
        self.outcome, self.resource, self.amount = str(outcome), resource, int(amount)
        self.path, self.asked, self.detail = path, list(asked), detail
        super().__init__(self._sentence())

    def _sentence(self):
        who = ", ".join("%s answered %s" % (a.holder, a.answer) for a in self.asked) or "no holder was asked"
        text = "%s was not loaded: abstraction.resource/leases@1 refused %d bytes of %s (%s); %s" % (
            os.path.basename(self.path) or self.path, self.amount, self.resource, self.outcome, who)
        return text + ("; " + self.detail if self.detail else "")


class CardUnavailable(RuntimeError):
    """The lease service could not be reached, so these weights were not loaded.

    The switch asked for arbitration and it did not happen; loading anyway
    would hold the card without a grant and without a row saying so.
    """


_lock = threading.RLock()
_status = CardStatus(False, OFF, None, "not installed")
_machine_factory = None
_cancellation_factory = None
# Each lease this ComfyUI holds, as id -> granted bytes, and the weights files
# they cover. Book aggregates same-subject grants and credits measured bytes
# once (RES-L2), so incremental growth leases reserve only the positive delta.
_leases = {}
_files = {}
_renewer = None
_stop = threading.Event()
_installed = []
_growth_lock = threading.Lock()
_pending_lock = threading.Lock()
_pending_cancellations = []
_model_management = None
_generation = 0


class _LocalCancellation:
    """Test/embedding cancellation when the caller injects its own machine."""
    def __init__(self):
        self.cancelled = threading.Event()

    def signal(self):
        self.cancelled.set()

    def close(self):
        pass


def status():
    with _lock:
        return _status


def _set(value):
    global _status
    with _lock:
        _status = value
    return value


def configure(machine_factory=None, cancellation_factory=None):
    """Supply independently configured trust for an embedding or a test.

    machine_factory(deadline) returns an object with resolve_resource_leases();
    it may also accept a cancellation keyword to bind waits to ComfyUI's
    interrupt. cancellation_factory() may supply a test token.
    No argument restores default installed-runtime selection.
    """
    global _machine_factory, _cancellation_factory
    if machine_factory is not None and not callable(machine_factory):
        raise ValueError("machine_factory must be callable")
    if cancellation_factory is not None and not callable(cancellation_factory):
        raise ValueError("cancellation_factory must be callable")
    _machine_factory = machine_factory
    _cancellation_factory = cancellation_factory


def held():
    """What this ComfyUI holds of the card now."""
    with _lock:
        leases = tuple(_leases)
        return Held(leases[0] if leases else None, sum(_leases.values()), dict(_files), leases)


def _default_machine(deadline, cancellation=None):
    from abstraction.facade.client import Machine
    return Machine(deadline=deadline, cancellation=cancellation, library=oa_route and oa_route.library())


def _bind(deadline, cancellation=None):
    from abstraction.facade import Scope
    if _machine_factory is None:
        machine = _default_machine(deadline, cancellation)
    else:
        import inspect
        try:
            inspect.signature(_machine_factory).bind(deadline, cancellation=cancellation)
        except (TypeError, ValueError):
            machine = _machine_factory(deadline)
        else:
            machine = _machine_factory(deadline, cancellation=cancellation)
    return machine.resolve_resource_leases(scope=Scope.LOCAL)


def _new_cancellation():
    if _cancellation_factory is not None:
        return _cancellation_factory()
    if _machine_factory is not None:
        return _LocalCancellation()
    if oa_route is not None:
        return oa_route.native().cancellation()
    from abstraction.ipc import Library
    return Library().cancellation()


def _register_wait(cancellation):
    event = getattr(cancellation, "cancelled", None) or threading.Event()
    with _pending_lock:
        _pending_cancellations.append((cancellation, event))
    return event


def _unregister_wait(cancellation):
    with _pending_lock:
        _pending_cancellations[:] = [(token, event) for token, event in _pending_cancellations
                                     if token is not cancellation]


def _cancel_pending_waits():
    with _pending_lock:
        pending = list(_pending_cancellations)
    for cancellation, event in pending:
        event.set()
        try:
            cancellation.signal()
        except Exception as error:
            LOG.warning("[abstraction] card wait cancellation signal failed: %r", error)


def _progress(message):
    """Show the current card admission state in ComfyUI's executing node."""
    try:
        from comfy_execution.utils import get_executing_context
        context = get_executing_context()
        node_id = getattr(context, "node_id", None) if context is not None else None
        import server
        prompt_server = getattr(server.PromptServer, "instance", None)
        send = getattr(prompt_server, "send_progress_text", None)
        if callable(send):
            send(message, node_id)
    except Exception as error:
        LOG.debug("[abstraction] card progress unavailable: %r", error)
    LOG.info("[abstraction] %s", message)


def _raise_if_cancelled(event):
    if not event.is_set():
        return
    throw_interrupted = getattr(_model_management, "throw_exception_if_processing_interrupted", None)
    if callable(throw_interrupted):
        throw_interrupted()
    raise CardUnavailable("card wait was canceled; this model was not loaded")


def _reserve():
    value = os.environ.get(RESERVE_ENV, "0").strip()
    reserve = int(value or "0")
    if reserve < 0:
        raise ValueError("%s must be a whole number of bytes" % RESERVE_ENV)
    return reserve


def _wait_ms():
    seconds = float(os.environ.get(WAIT_ENV, "") or DEFAULT_WAIT_SECONDS)
    if not 0 <= seconds <= MAX_WAIT_SECONDS:
        raise ValueError("%s must be between 0 and %g" % (WAIT_ENV, MAX_WAIT_SECONDS))
    return int(seconds * 1000)


WAITING_HELD_BY = "card:held_by:"


def waiting_sentence(word):
    """The one sentence printed for a waiting word from job's catalogue.

    abstraction-job JOB-A15 and abstraction-resource RES-L8; the Go job
    package's WaitingSentence and the OpenCode adopter print the same, each
    tested against abstraction-job's testdata/waiting-sentences.json. Empty
    for no word; an unrecognized word reads as an unnamed wait.
    """
    if not word:
        return ""
    if word.startswith(WAITING_HELD_BY) and len(word) > len(WAITING_HELD_BY):
        return "Waiting for the graphics card: held by %s." % word[len(WAITING_HELD_BY):]
    return "Waiting."


def _grant_sentence():
    """What a person runs to let this ComfyUI hold the card.

    Package loads share service.rights_program's configured relay guidance.
    A standalone embedding supplies caller-program-unavailable guidance.
    Native caller proof remains the runtime's.
    """
    if oa_route is not None:
        from .service import rights_program
        program = rights_program()
    else:
        program = None
    if program is None:
        return "the OA caller program is unavailable; no card permission command can be suggested"
    return ('this program holds no rights rule for the card. A person allows it with: '
            'openabstractions rights grant --action %s --resource %s --program "%s"' % (
                ACTION_HOLD, RESOURCE, program))


def acquire_for(path):
    """Hold enough of card:0 to load the weights at path, and return the lease id.

    Returns None when the switch is off and when the whole hold is still under
    FLOOR_BYTES. Raises CardRefused on a typed refusal and CardUnavailable when
    the service could not be reached.
    """
    with _lock:
        if _status.code != HOLDING:
            return None
        if path in _files:
            return next(iter(_leases), None)
    cancellation = _new_cancellation()
    cancel_event = _register_wait(cancellation)
    try:
        while not _growth_lock.acquire(timeout=0.05):
            _raise_if_cancelled(cancel_event)
        try:
            return _acquire_for_locked(path, cancellation, cancel_event)
        finally:
            _growth_lock.release()
    finally:
        _unregister_wait(cancellation)
        cancellation.close()


def _acquire_for_locked(path, cancellation, cancel_event):
    global _generation
    with _lock:
        generation = _generation
    _raise_if_cancelled(cancel_event)
    with _lock:
        if _status.code != HOLDING:
            return None
        if path in _files:
            return next(iter(_leases), None)
        size = os.path.getsize(path)
        want = max(sum(_files.values()) + size, _reserve())
        held_bytes = sum(_leases.values())
        base_leases = dict(_leases)
    if want < FLOOR_BYTES or held_bytes >= want:
        with _lock:
            if generation != _generation:
                _raise_if_cancelled(cancel_event)
                return None
            _files[path] = size
        return next(iter(base_leases), None)

    newly_acquired = []
    try:
        remaining = want - held_bytes
        for _ in range(len(base_leases) + 2):
            _raise_if_cancelled(cancel_event)
            _progress("Waiting for card:0 capacity before loading %s; cancel this prompt to stop waiting." %
                      (os.path.basename(path) or path))
            lease = _acquire(remaining, path, cancellation)
            newly_acquired.append((lease, remaining))
            _raise_if_cancelled(cancel_event)
            with _lock:
                live_ids = tuple(_leases)
            live_total = 0
            for lease_id in live_ids:
                try:
                    result = _bind(time.monotonic() + CONTROL_BUDGET_SECONDS).renew(lease_id)
                except Exception as error:
                    raise CardUnavailable("existing card lease could not be confirmed before loading %s: %r" %
                                          (os.path.basename(path) or path, error)) from error
                applied = _renew_applied(result)
                if applied is None:
                    raise CardUnavailable("existing card lease could not be confirmed before loading %s: "
                                          "renew returned no successful result" %
                                          (os.path.basename(path) or path))
                if applied:
                    with _lock:
                        live_total += _leases.get(lease_id, 0)
                else:
                    with _lock:
                        _leases.pop(lease_id, None)
            live_total += sum(amount for _, amount in newly_acquired)
            _raise_if_cancelled(cancel_event)
            if live_total >= want:
                with _lock:
                    if generation != _generation:
                        raise CardUnavailable("the card hold was released while this model waited")
                    for lease_id, amount in newly_acquired:
                        _leases[lease_id] = amount
                    _files[path] = size
                    _start_renewer()
                _progress("Card:0 capacity granted; loading %s." % (os.path.basename(path) or path))
                return next(iter(_leases), None)
            remaining = want - live_total
        raise CardUnavailable("existing card leases expired while ComfyUI waited for additional capacity")
    except BaseException:
        # A known delta that never covered a loaded file is safe to return;
        # ambiguous transport failures carry no id and follow normal expiry.
        for lease_id, _ in newly_acquired:
            _give_back(lease_id)
        # A local interrupt may close a framed client while Acquire is still
        # producing its reply. Once known grants are returned, preserve
        # ComfyUI's cancellation outcome instead of surfacing that transport
        # close as a model-loader failure. Uncancelled service errors keep
        # their original type and message.
        if cancel_event.is_set():
            _raise_if_cancelled(cancel_event)
        raise


def _acquire(amount, path, cancellation=None):
    """One Acquire, with its asks logged and its refusal typed."""
    wait_ms = _wait_ms()
    deadline = time.monotonic() + CONTROL_BUDGET_SECONDS + wait_ms / 1000.0
    began = time.monotonic()
    try:
        result = _bind(deadline, cancellation).acquire(RESOURCE, amount, wait_ms)
    except Exception as error:
        raise CardUnavailable(
            "%s was not loaded: abstraction.resource/leases@1 did not answer for %d bytes of %s: %r" % (
                os.path.basename(path) or path, amount, RESOURCE, error)) from error
    took = time.monotonic() - began
    outcome = str(getattr(result, "outcome", ""))
    asked = list(getattr(result, "asked", ()) or ())
    for ask in asked:
        LOG.info("[abstraction] card %s: asked %s, %s, %d bytes in %d ms",
                 RESOURCE, ask.holder, ask.answer, ask.amount, ask.took_ms)
    if outcome != "acquired" or getattr(result, "lease", None) is None:
        raise CardRefused(outcome, RESOURCE, amount, path, asked,
                          _grant_sentence() if outcome == "not_permitted" else "")
    LOG.info("[abstraction] card %s: %d bytes granted as lease %s for %s in %.3f s",
             RESOURCE, amount, result.lease.id, os.path.basename(path) or path, took)
    return result.lease.id


def _give_back(lease):
    """Release one lease. A lease the service no longer knows is already gone,
    and one this process cannot reach expires by renew_by (RES-L4); neither is
    a reason to fail an unload."""
    try:
        result = _bind(time.monotonic() + CONTROL_BUDGET_SECONDS).release(lease)
        outcome = getattr(result, "outcome", None)
        if getattr(outcome, "value", outcome) != "ok" or getattr(result, "change", None) is None:
            LOG.warning("[abstraction] card %s: releasing lease %s returned no successful result",
                        RESOURCE, lease)
            return False
        if result.change.applied:
            LOG.info("[abstraction] card %s: lease %s released", RESOURCE, lease)
        else:
            LOG.info("[abstraction] card %s: lease %s was already absent", RESOURCE, lease)
        return True
    except Exception as error:
        LOG.warning("[abstraction] card %s: releasing lease %s failed: %r", RESOURCE, lease, error)
        return False


def release_all():
    """Give back the card. Returns the lease id released, or None."""
    global _generation
    _cancel_pending_waits()
    with _lock:
        _generation += 1
        current = list(_leases)
        _leases.clear()
        _files.clear()
    if not current:
        return None
    for lease in current:
        _give_back(lease)
    return current[0]


def _start_renewer():
    """Keep the lease alive while ComfyUI holds the weights. Caller holds _lock."""
    global _renewer
    if _renewer is not None and _renewer.is_alive():
        return
    _stop.clear()
    _renewer = threading.Thread(target=_renew_forever, name="oa-card-renew", daemon=True)
    _renewer.start()


def _renew_forever():
    while not _stop.wait(RENEW_SECONDS):
        with _lock:
            current = dict(_leases)
        if not current:
            return
        for lease_id in current:
            try:
                result = _bind(time.monotonic() + CONTROL_BUDGET_SECONDS).renew(lease_id)
            except Exception as error:
                LOG.warning("[abstraction] card %s: renewing lease %s failed: %r", RESOURCE, lease_id, error)
                continue
            applied = _renew_applied(result)
            if applied is None:
                LOG.warning("[abstraction] card %s: renewing lease %s returned no successful result", RESOURCE, lease_id)
                continue
            if not applied:
                with _lock:
                    _leases.pop(lease_id, None)
                LOG.warning("[abstraction] card %s: lease %s is no longer held", RESOURCE, lease_id)


def _renew_applied(result):
    """Return the generated lease-change result's applied bit, or None."""
    outcome = getattr(result, "outcome", None)
    if getattr(outcome, "value", outcome) != "ok":
        return None
    change = getattr(result, "change", None)
    applied = getattr(change, "applied", None)
    return applied if isinstance(applied, bool) else None


def install(utils=None, model_management=None):
    """Apply ABSTRACTION_COMFYUI_CARD. Returns the resulting CardStatus.

    utils and model_management are ComfyUI's comfy.utils and
    comfy.model_management; the default is whatever this process imported.
    """
    value = os.environ.get(ENV, "")
    if value == "":
        return _set(CardStatus(False, OFF, None, "%s is unset" % ENV))
    if value != "service":
        return _not_holding(INVALID_CONFIGURATION,
                            "%s=%r; supported values are unset and \"service\"" % (ENV, value))
    try:
        _reserve()
        _wait_ms()
    except ValueError as error:
        return _not_holding(INVALID_CONFIGURATION, str(error))
    try:
        from abstraction.resource import LeasesClient  # noqa: F401
        from abstraction.facade import Scope  # noqa: F401
    except ImportError as error:
        return _not_holding(SDK_UNAVAILABLE,
                            "install the OA facade and resource Python packages: %r" % (error,))
    if utils is None or model_management is None:
        import sys
        utils = utils if utils is not None else sys.modules.get("comfy.utils")
        model_management = model_management if model_management is not None else sys.modules.get("comfy.model_management")
    if utils is None or model_management is None or not callable(getattr(utils, "load_torch_file", None)) \
            or not callable(getattr(model_management, "unload_all_models", None)):
        return _not_holding(COMFY_UNAVAILABLE,
                            "comfy.utils.load_torch_file and comfy.model_management.unload_all_models are "
                            "not both loaded in this process")
    if not any(module is utils for module, _, _, _, _ in _installed):
        _wrap(utils, model_management)
    _set(CardStatus(True, HOLDING, RESOURCE,
                    "the weights this ComfyUI loads are held under a set of leases on %s, at least %d bytes" % (
                        RESOURCE, max(FLOOR_BYTES, _reserve()))))
    LOG.info("[abstraction] ComfyUI asks abstraction.resource/leases@1 for %s before it loads weights, "
             "and gives it back when it unloads", RESOURCE)
    return status()


def _wrap(utils, model_management):
    """Replace the loader, unload and interrupt seams, remembering originals."""
    global _model_management
    original_load = utils.load_torch_file
    original_unload = model_management.unload_all_models
    original_interrupt = getattr(model_management, "interrupt_current_processing", None)

    def load_torch_file(ckpt, *args, **kwargs):
        if isinstance(ckpt, str) and os.path.isfile(ckpt):
            acquire_for(os.path.realpath(ckpt))
        return original_load(ckpt, *args, **kwargs)

    def unload_all_models(*args, **kwargs):
        try:
            return original_unload(*args, **kwargs)
        finally:
            release_all()

    def interrupt_current_processing(value=True, *args, **kwargs):
        if not callable(original_interrupt):
            return None
        result = original_interrupt(value, *args, **kwargs)
        if value:
            _cancel_pending_waits()
        return result

    utils.load_torch_file = load_torch_file
    model_management.unload_all_models = unload_all_models
    if callable(original_interrupt):
        model_management.interrupt_current_processing = interrupt_current_processing
    _model_management = model_management
    _installed.append((utils, model_management, original_load, original_unload, original_interrupt))


def uninstall():
    """Restore ComfyUI's own two functions and give back the card.

    For a test and for an embedding that turns the switch off again; ComfyUI
    itself never calls it.
    """
    release_all()
    _stop.set()
    for utils, model_management, original_load, original_unload, original_interrupt in _installed:
        utils.load_torch_file = original_load
        model_management.unload_all_models = original_unload
        if callable(original_interrupt):
            model_management.interrupt_current_processing = original_interrupt
    del _installed[:]
    global _model_management
    _model_management = None
    _set(CardStatus(False, OFF, None, "%s is unset" % ENV))


def _not_holding(code, detail):
    LOG.warning("[abstraction] CARD NOT HELD (%s): ComfyUI loads weights without asking for %s. %s",
                code, RESOURCE, detail)
    return _set(CardStatus(True, code, None, detail))


atexit.register(release_all)
