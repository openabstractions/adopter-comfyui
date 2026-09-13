"""This ComfyUI shares the card with OpenAbstractions' own engines.

The connector (comfyui-connector.exe beside this file, native-relay) holds a
lease on card:0 for each model ComfyUI loads, through the provider kit's
holder, the way OA's own engines do (connector/card.go). This module tells it
what ComfyUI does, over the relay's lines:

- before comfy.model_management.load_models_gpu loads a model, card_load
  asks for the model's size and waits for the grant; card_loaded then says
  what the load took;
- a model ComfyUI lets go itself (its own free_memory, POST /free) is
  card_unloaded, and its lease ends;
- each prompt is prompt_begin and prompt_end, so OA never frees a model a
  running prompt uses;
- a thread long-polls card_free_wait. When OA needs memory another app asked
  for, the connector asks this ComfyUI to free one model; it is freed
  between prompts, or at the load point of the running prompt when that
  load does not use it, which is where ComfyUI's own free_memory frees
  models too. Nothing is freed mid-run.

The connector answers each yield and labels each lease "ComfyUI: <model>",
which the Panel's Memory activity shows. It also writes one sentence per
yield, logged here: "OpenAbstractions freed memory in ComfyUI (Wan 2.2) for
modelhostd."

Version-gated: install() wraps only a ComfyUI whose model_management,
execution and sd modules have the names used here (v0.45.2), and only when
the connector relays the calls. Everything fails open: a connector or lease
service that cannot answer, or refuses, leaves ComfyUI loading as it always
did, with one log line per kind of answer.
"""

import itertools
import logging
import os
import queue
import sys
import threading
import time
import weakref

LOG = logging.getLogger("openabstractions")

# A model smaller than this is not leased: the Windows instrument reports no
# row below it (abstraction-resource RES-T2).
FLOOR_BYTES = 256 << 20
# card_load waits for other holders to yield; the connector bounds it at 120 s.
LOAD_SECONDS = 130.0
SHORT_SECONDS = 10.0
WAIT_MS = 20000
# How long a free waits for the running prompt to reach a boundary.
FREE_SECONDS = 25.0
RETRY_SECONDS = 5.0


class Unavailable(Exception):
    """The connector did not answer a card line."""


def relay_call(op, seconds, **fields):
    """One card line through the connector's native-relay, or Unavailable."""
    from . import oa_relay
    if oa_relay.connector() is None and not os.environ.get("ABSTRACTION_COMFYUI_NATIVE_RELAY"):
        raise Unavailable("no connector relays this ComfyUI's calls")
    library = oa_relay.library()
    if library is None:
        raise Unavailable("the relay runs on Windows")
    try:
        reply = library._current().call(op, seconds, **fields)
    except (oa_relay.Lost, TimeoutError, OSError, ValueError) as error:
        raise Unavailable(str(error) or type(error).__name__) from None
    if reply.get("status") != 0:
        raise Unavailable(reply.get("message") or "status %r" % reply.get("status"))
    return reply


class Lease:
    """One ComfyUI's side of its card leases."""

    def __init__(self, mm, call=relay_call):
        self.mm, self.call = mm, call
        self.names = weakref.WeakKeyDictionary()   # loaded torch module -> name
        self.labels = weakref.WeakKeyDictionary()  # loaded torch module -> title
        self.numbers = itertools.count(1)
        self.tracked = {}                          # name -> weakref of the module
        self.lock = threading.Lock()
        # The running prompt holds run; a free between prompts takes it.
        self.run = threading.RLock()
        # A queue while the running prompt waits at a load point.
        self.parked = None
        self.said = set()
        self.stopped = threading.Event()

    # -- what ComfyUI holds --------------------------------------------------

    def name(self, patcher):
        module = getattr(patcher, "model", None)
        if module is None:
            return None
        with self.lock:
            found = self.names.get(module)
            if found is None:
                found = "m%d" % next(self.numbers)
                self.names[module] = found
            return found

    def label(self, patcher, path):
        """Name a loaded model by its file, for the sentence a yield writes."""
        module = getattr(patcher, "model", None)
        if module is not None and isinstance(path, str) and path:
            with self.lock:
                self.labels[module] = os.path.splitext(os.path.basename(path))[0]

    def title(self, patcher):
        module = getattr(patcher, "model", None)
        with self.lock:
            found = self.labels.get(module) if module is not None else None
        return found or type(module).__name__

    def loaded(self):
        """{name: LoadedModel} of what ComfyUI holds now."""
        out = {}
        for loaded in list(getattr(self.mm, "current_loaded_models", [])):
            patcher = loaded.model
            if patcher is None:
                continue
            module = getattr(patcher, "model", None)
            with self.lock:
                name = self.names.get(module) if module is not None else None
            if name is not None:
                out[name] = loaded
        return out

    def _say(self, kind, message):
        if kind not in self.said:
            self.said.add(kind)
            LOG.info("[openabstractions] %s", message)

    def _send(self, op, seconds=SHORT_SECONDS, **fields):
        try:
            return self.call(op, seconds, **fields)
        except Unavailable as error:
            self._say("unavailable", "ComfyUI loads without OpenAbstractions leases: %s" % error)
            return None

    # -- load ------------------------------------------------------------------

    def before_load(self, models):
        """Ask for a lease for each model this load brings onto the card."""
        patchers = [m for m in models if getattr(m, "model", None) is not None]
        using = [n for n in (self.name(p) for p in patchers) if n]
        for patcher in patchers:
            name = self.name(patcher)
            with self.lock:
                known = name in self.tracked
            if known:
                continue
            try:
                size = int(patcher.model_size())
            except Exception:  # a model that cannot say its size is loaded without a lease
                continue
            if size < FLOOR_BYTES:
                continue
            reply = self._parked_call("card_load", name=name, title=self.title(patcher), bytes=size, using=using)
            if reply is None:
                continue
            if reply.get("granted") is True:
                with self.lock:
                    self.tracked[name] = weakref.ref(patcher.model)
            else:
                self._say("refused:" + str(reply.get("word")),
                          "OpenAbstractions did not grant ComfyUI the card (%s); ComfyUI loads anyway: %s"
                          % (reply.get("word"), reply.get("sentence")))

    def _parked_call(self, op, **fields):
        """card_load, while this prompt serves the frees asked meanwhile: the
        load point is a boundary for the models this load does not use."""
        result, done = {}, threading.Event()

        def ask():
            result["reply"] = self._send(op, LOAD_SECONDS, **fields)
            done.set()

        parked = queue.Queue()
        with self.lock:
            self.parked = parked
        try:
            threading.Thread(target=ask, name="openabstractions-card-load", daemon=True).start()
            while not done.wait(0.05):
                self._serve(parked)
            self._serve(parked)
        finally:
            with self.lock:
                self.parked = None
            self._serve(parked)
        return result.get("reply")

    def _serve(self, parked):
        while True:
            try:
                request = parked.get_nowait()
            except queue.Empty:
                return
            self._answer(request, self.free(request.get("name")))

    def after_load(self, models):
        """Say what each leased model took, and which ComfyUI let go."""
        held = self.loaded()
        for patcher in models:
            name = self.name(patcher)
            with self.lock:
                tracked = name in self.tracked
            loaded = held.get(name)
            if tracked and loaded is not None:
                try:
                    size = int(loaded.model_loaded_memory())
                except Exception:  # what the load took is display data
                    size = 0
                if size > 0:
                    self._send("card_loaded", name=name, bytes=size)
        self.sync()

    def sync(self):
        """End the lease of each model ComfyUI no longer holds."""
        held = self.loaded()
        with self.lock:
            gone = [name for name, ref in self.tracked.items() if name not in held or ref() is None]
            for name in gone:
                del self.tracked[name]
        for name in gone:
            self._send("card_unloaded", name=name)

    # -- free --------------------------------------------------------------------

    def free(self, name):
        """Free one model now; the caller is at a boundary. True when it is gone."""
        for index, loaded in enumerate(list(self.mm.current_loaded_models)):
            patcher = loaded.model
            module = getattr(patcher, "model", None) if patcher is not None else None
            with self.lock:
                found = self.names.get(module) if module is not None else None
            if found != name:
                continue
            try:
                if not loaded.model_unload():
                    return False
                self.mm.current_loaded_models.remove(loaded)
                self.mm.soft_empty_cache()
            except Exception as error:  # a free that fails leaves the lease held
                LOG.warning("[openabstractions] ComfyUI could not free a model for OpenAbstractions: %r", error)
                return False
            break
        with self.lock:
            self.tracked.pop(name, None)
        return True

    def _answer(self, request, ok):
        self._send("card_freed", request=request.get("request"), ok=bool(ok))

    def _between_prompts(self, request):
        """Free one model asked while no prompt waits at a load point."""
        deadline = time.monotonic() + FREE_SECONDS
        while time.monotonic() < deadline:
            with self.lock:
                parked = self.parked
            if parked is not None:
                parked.put(request)
                return
            if self.run.acquire(timeout=0.2):
                try:
                    self._answer(request, self.free(request.get("name")))
                finally:
                    self.run.release()
                return
        self._answer(request, False)

    def serve_frees(self):
        """The thread that takes OA's free requests and the sentences told."""
        while not self.stopped.is_set():
            reply = self._send("card_free_wait", WAIT_MS / 1000.0 + SHORT_SECONDS, ms=WAIT_MS)
            if reply is None:
                self.stopped.wait(RETRY_SECONDS)
                continue
            for sentence in reply.get("told") or []:
                if isinstance(sentence, str):
                    LOG.info("[openabstractions] %s", sentence[:300])
            for request in reply.get("free") or []:
                if isinstance(request, dict):
                    self._between_prompts(request)

    # -- prompts -----------------------------------------------------------------

    def prompt(self, run, *args, **kwargs):
        """Run one prompt: OA waits for its end before it frees a model it uses."""
        self._send("prompt_begin", LOAD_SECONDS)
        try:
            with self.run:
                return run(*args, **kwargs)
        finally:
            self._send("prompt_end")
            self.sync()


_installed = None


def gate(mm, execution, sd):
    """The names this module wraps, or the reason this ComfyUI is not wrapped."""
    for owner, names in ((mm, ("load_models_gpu", "unload_all_models", "current_loaded_models", "soft_empty_cache", "LoadedModel")),
                         (getattr(execution, "PromptExecutor", None), ("execute",)),
                         (sd, ("load_diffusion_model", "load_checkpoint_guess_config", "load_clip"))):
        for name in names:
            if owner is None or not hasattr(owner, name):
                return "this ComfyUI has no %s" % name
    for name in ("model_unload", "model_loaded_memory"):
        if not hasattr(mm.LoadedModel, name):
            return "this ComfyUI's LoadedModel has no %s" % name
    return None


def install(mm=None, execution=None, sd=None, call=relay_call, start=True):
    """Wrap ComfyUI's loads, frees and prompts. Returns the Lease, or None."""
    global _installed
    if _installed is not None:
        return _installed
    if mm is None:
        # ComfyUI imports these before it loads custom nodes.
        mm = sys.modules.get("comfy.model_management")
        execution = sys.modules.get("execution")
        sd = sys.modules.get("comfy.sd")
    reason = gate(mm, execution, sd)
    if reason:
        LOG.info("[openabstractions] no card leases: %s", reason)
        return None
    lease = Lease(mm, call)
    load, unload_all = mm.load_models_gpu, mm.unload_all_models
    execute = execution.PromptExecutor.execute

    def load_models_gpu(models, *args, **kwargs):
        listed = list(models)
        try:
            lease.before_load(listed)
        except Exception as error:  # the lease never stops a load
            LOG.warning("[openabstractions] card lease: %r", error)
        result = load(listed, *args, **kwargs)
        try:
            lease.after_load(listed)
        except Exception as error:
            LOG.warning("[openabstractions] card lease: %r", error)
        return result

    def unload_all_models(*args, **kwargs):
        result = unload_all(*args, **kwargs)
        try:
            lease.sync()
        except Exception as error:
            LOG.warning("[openabstractions] card lease: %r", error)
        return result

    def prompt(self, *args, **kwargs):
        return lease.prompt(execute, self, *args, **kwargs)

    def labelled(original, pick):
        def wrapper(*args, **kwargs):
            result = original(*args, **kwargs)
            try:
                path = args[0] if args else None
                if isinstance(path, (list, tuple)):
                    path = path[0] if path else None
                lease.label(pick(result), path)
            except Exception:  # a label is display data
                pass
            return result
        return wrapper

    mm.load_models_gpu, mm.unload_all_models = load_models_gpu, unload_all_models
    execution.PromptExecutor.execute = prompt
    sd.load_diffusion_model = labelled(sd.load_diffusion_model, lambda patcher: patcher)
    sd.load_checkpoint_guess_config = labelled(sd.load_checkpoint_guess_config, lambda out: out[0])
    sd.load_clip = labelled(sd.load_clip, lambda clip: clip.patcher)
    if start:
        threading.Thread(target=lease.serve_frees, name="openabstractions-card-frees", daemon=True).start()
    _installed = lease
    LOG.info("[openabstractions] ComfyUI's model loads share the card with OpenAbstractions")
    return lease
