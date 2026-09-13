"""Explicit ComfyUI video node backed by OA's durable video jobs.

OAVideoGenerate submits job kind ``inference`` with profile ``video`` through
the job service (abstraction.job/acceptance@1), the way abstraction-inference
CONTRACT.md "Video generation" (INF-G1..G8) defines it. The runtime routes the
job to a provider serving abstraction.inference/video@1; this node holds only
the job's request identity. It drives ComfyUI's progress bar from the job
snapshot's progress (step of steps) and shows its waiting word (``queue:<n>``),
cancels the job when the ComfyUI run is interrupted, and reads the one
delivered clip by digest through oa_content.read_delivery.

A first frame is stored in the caller's content store and named by its digest
in an ``edit`` request; the job contract makes that image the clip's first
frame. The provider owns sampling steps, guidance, flow shift, scheduler and
decoder for the chosen model (INF-G8). No graph, path, URL or engine option
crosses this node.
"""

import io
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sys
import time

try:
    from . import oa_route
except ImportError:  # a test loaded this file outside its package: no relay
    oa_route = None

try:
    from . import oa_content as _content
except ImportError:  # loaded by file path, as the tests and the runner helpers load it
    import importlib.util as _util
    _spec = _util.spec_from_file_location("oa_content", Path(__file__).with_name("oa_content.py"))
    _content = _util.module_from_spec(_spec)
    _spec.loader.exec_module(_content)

VIDEO_PROFILE = "abstraction.inference/video@1"
JOB_KIND = "inference"
RECOVERABLE_UPSTREAM = "abstraction.inference/recoverable-upstream@1"
SIZES = ("832x480", "480x832", "1280x704", "704x1280")
AUTO = "auto"
MAX_SECONDS = 10
MAX_PROMPT_BYTES = 32768
# The runtime's operation output bound for a video delivery (INF-G3).
MAX_VIDEO_BYTES = 64 * 1024 * 1024
# The runtime's default input image bound (MaxImageBytes).
MAX_FRAME_PNG_BYTES = 20 * 1024 * 1024
MAX_FRAME_PIXELS = 4096 * 4096
MAX_RESULT_DOCUMENT = 1024 * 1024
POLL_SECONDS = 1.0
BINDING_SECONDS = 60.0
# Consecutive unavailable observations tolerated before the run fails.
MAX_UNAVAILABLE = 60
OFFER_TTL_SECONDS = 30.0
# ComfyUI asks INPUT_TYPES on every /object_info and every queued prompt.
OFFER_BUDGET_SECONDS = 1.5
CANCEL_ATTEMPTS = 3
CANCEL_PAUSE_SECONDS = 1.0
# Each cancel attempt's own call budget.
CANCEL_DEADLINE_SECONDS = 5.0
# The grant a first frame needs, named in refusals and in README.md: OA stores
# it and reads it back for this program's own video job under the input:image
# profile, with content write and read on that one resource (storage STO-W12,
# inference INF-V4).
INPUT_RIGHTS = {
    "write": "abstraction.storage/content.write",
    "read": "abstraction.storage/content.read",
    "resource": "input:image",
}
_INPUT_GRANT = "%s and %s on %s" % (INPUT_RIGHTS["write"], INPUT_RIGHTS["read"], INPUT_RIGHTS["resource"])
# The grant the clip needs: OA writes the job's video under the output:video
# profile and this program reads its own result back; one Allow in OA's
# question gives both (inference INF-S7, INF-V4).
OUTPUT_RIGHTS = {
    "write": "abstraction.storage/content.write",
    "read": "abstraction.storage/content.read",
    "resource": "output:video",
}
_OUTPUT_GRANT = "%s and %s on %s" % (OUTPUT_RIGHTS["write"], OUTPUT_RIGHTS["read"], OUTPUT_RIGHTS["resource"])

LOG = logging.getLogger("abstraction")

_machine_factory = None
_offer_cache = None
_offer_error = ""


def configure(machine_factory=None):
    """Supply machine_factory(deadline) for independently trusted embedding or tests."""
    global _machine_factory, _offer_cache
    if machine_factory is not None and not callable(machine_factory):
        raise ValueError("machine_factory must be callable")
    _machine_factory, _offer_cache = machine_factory, None


def _default_machine(deadline):
    from abstraction.facade.client import Machine
    return Machine(endpoint=os.environ.get("ABSTRACTION_RUNTIME_ENDPOINT") or None, deadline=deadline,
                   library=oa_route and oa_route.library())


def _factory(machine_factory):
    return machine_factory or _machine_factory or _default_machine


def offered_models(*, machine_factory=None, cache=True, budget=OFFER_BUDGET_SECONDS):
    """The model names OA offers for the video profile, first preferred.

    Each offer's first name is the name to ask for (abstraction.router/router@1
    Offer). A refused or unavailable router answers no names; the node then
    offers ``auto`` alone and reports that discovery failure at run time.
    """
    names, _ = _discover_models(machine_factory=machine_factory, cache=cache, budget=budget)
    return names


def _discover_models(*, machine_factory=None, cache=True, budget=OFFER_BUDGET_SECONDS):
    """Names and the diagnosis of this call, before another discovery runs."""
    global _offer_cache, _offer_error
    now = time.monotonic()
    if cache and _offer_cache is not None and _offer_cache[0] > now:
        return list(_offer_cache[1]), _offer_error
    names, diagnosis = [], ""
    try:
        from abstraction.facade import Scope
        from abstraction.router import OfferOutcome, OfferRequest, Placement
        route = _factory(machine_factory)(now + budget).resolve_router(scope=Scope.LOCAL)
        snapshot = route.offer(OfferRequest(profile=VIDEO_PROFILE, placement=Placement.ANY, reserve=0, fresh=False))
        if snapshot.outcome != OfferOutcome.OFFERED:
            raise RuntimeError("OA router offer %s" % snapshot.outcome)
        for offer in snapshot.offers[:64]:
            name = offer.names[0] if offer.names else ""
            if isinstance(name, str) and 0 < len(name.encode("utf-8")) <= 256 and name != AUTO and name not in names:
                names.append(name)
    except Exception as error:
        names, diagnosis = [], "%s: %s" % (type(error).__name__, error)
    _offer_error = diagnosis
    if cache:
        _offer_cache = (now + OFFER_TTL_SECONDS, tuple(names))
    return names, diagnosis


def _check(model, prompt, size, duration_ms, first_frame_png):
    if not isinstance(model, str) or not model or len(model.encode("utf-8")) > 256:
        raise ValueError("OA video generation requires a model name or auto")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ValueError("OA video generation requires a prompt of at most 32 KiB")
    if size not in SIZES:
        raise ValueError("OA video generation supports the sizes %s" % ", ".join(SIZES))
    if type(duration_ms) is not int or not 1 <= duration_ms <= MAX_SECONDS * 1000:
        raise ValueError("OA video duration must be 1 ms to %d s" % MAX_SECONDS)
    if first_frame_png is not None and (not isinstance(first_frame_png, bytes)
                                        or not first_frame_png.startswith(b"\x89PNG\r\n\x1a\n")
                                        or len(first_frame_png) > MAX_FRAME_PNG_BYTES):
        raise ValueError("OA video first frame must be one PNG of at most 20 MiB")


class _Jobs:
    """One job binding, renewed before its call budget runs out or after a failure."""

    def __init__(self, factory):
        self._factory, self._binding, self._until = factory, None, 0.0

    def get(self):
        now = time.monotonic()
        if self._binding is None or now > self._until - 10:
            from abstraction.facade import Scope
            self._until = now + BINDING_SECONDS
            self._binding = self._factory(self._until).resolve_jobs(
                guarantees=(RECOVERABLE_UPSTREAM,), scope=Scope.LOCAL)
        return self._binding

    def drop(self):
        self._binding = None


def _stage_frame(factory, data):
    """Store the first frame in the caller's content store; return its digest."""
    import hashlib
    from abstraction.facade import Scope
    named = "sha256:" + hashlib.sha256(data).hexdigest()
    writer = factory(time.monotonic() + 60).resolve_storage_writer(scope=Scope.LOCAL)
    try:
        stored = writer.write(secrets.token_hex(16), named, data)
    except Exception as error:
        outcome = getattr(error, "outcome", None)
        if outcome is None:
            raise
        raise RuntimeError("OA did not store the first frame (%s): this app needs %s" % (outcome, _INPUT_GRANT)) from error
    if getattr(stored, "digest", "") != named:
        raise RuntimeError("OA content store did not keep the first frame")
    return named


def _grant_guidance(reason):
    """The grants a refused submission's reason names. A job refused for
    several grants names each word once, separated by spaces (inference
    INF-V4), and OA asks each first-use question in the same pass."""
    words = (reason or "").split()
    needed = []
    if "content:read:forbidden" in words:
        needed.append(_INPUT_GRANT)
    if "output:forbidden" in words:
        needed.append(_OUTPUT_GRANT)
    if not needed:
        return ""
    return "; this app needs %s; allow it in OA's question and run the node again" % "; and ".join(needed)


class _NotAccepted(RuntimeError):
    """The job service answered that it did not accept the submission."""


def _admit(jobs, submission, sleep):
    """Submit once; an unanswered submit is reconciled under the same identity.

    A definite refusal raises _NotAccepted. An answer that never came raises
    RuntimeError; accepted work remains owned by OA and visible in its inventory.
    """
    try:
        result = jobs.get().submit(submission)
    except Exception:
        jobs.drop()
        result = None
    for _ in range(5):
        if result is not None and result.outcome != "unknown":
            break
        sleep(POLL_SECONDS)
        try:
            result = jobs.get().reconcile(submission.identity)
        except Exception:
            jobs.drop()
            result = None
    if result is None or result.outcome == "unknown":
        raise RuntimeError("OA video admission stayed unanswered")
    if result.outcome != "accepted" or result.receipt is None:
        raise _NotAccepted("OA video admission %s: %s%s" % (result.outcome, result.reason or "no reason given",
                                                          _grant_guidance(result.reason)))
    return result.receipt


def _read_result(jobs, identity, sleep):
    data = b""
    for _ in range(MAX_UNAVAILABLE):
        read = jobs.get().read_result(identity, len(data), 65536)
        if read.outcome == "not_ready" or read.outcome == "unavailable":
            sleep(POLL_SECONDS)
            continue
        if read.outcome != "data":
            raise RuntimeError("OA video result read %s" % read.outcome)
        data += read.chunk.data
        if len(data) > MAX_RESULT_DOCUMENT:
            raise RuntimeError("OA video result document exceeds 1 MiB")
        if read.chunk.eof:
            import abstraction.inference.job as jobwire
            document = jobwire.decode(data)
            if document.result is None or document.result.profile != "video" or len(document.result.deliveries) != 1:
                raise RuntimeError("OA video result is not one video delivery")
            return document.result
    raise RuntimeError("OA video result stayed unavailable")


def _cancel(factory, identity, operation, sleep):
    """Ask the job service to cancel, a few times over a few seconds.

    Each attempt binds the job service with a 5 s call budget. A job that
    already ended needs no cancel. A cancel that never answers ``requested``
    is logged with the operation id; the OA jobs view lists the operation.
    """
    from abstraction.facade import Scope
    outcome = "no answer"
    for attempt in range(CANCEL_ATTEMPTS):
        if attempt:
            sleep(CANCEL_PAUSE_SECONDS)
        try:
            jobs = factory(time.monotonic() + CANCEL_DEADLINE_SECONDS).resolve_jobs(
                guarantees=(RECOVERABLE_UPSTREAM,), scope=Scope.LOCAL)
            outcome = jobs.cancel_work(identity).outcome
        except Exception as error:
            outcome = "%s: %s" % (type(error).__name__, error)
            continue
        if outcome in ("requested", "already_terminal"):
            return True
    LOG.warning("[abstraction] OA video operation %s was not cancelled: the cancel answered %s",
                operation, outcome)
    return False


def generate(model, prompt, size, duration_ms, *, first_frame_png=None, progress=None, interrupted=None,
             machine_factory=None, sleep=time.sleep):
    """Return (bytes, media type, route) of one clip generated through an OA video job.

    progress(done, total, waiting) receives each changed snapshot; interrupted()
    is called before every observation and stops the run by raising, which
    cancels the job.
    """
    _check(model, prompt, size, duration_ms, first_frame_png)
    factory = _factory(machine_factory)
    if model == AUTO:
        offered, diagnosis = _discover_models(machine_factory=factory, cache=False, budget=5.0)
        if not offered:
            if diagnosis:
                raise RuntimeError("OA video model discovery failed: %s" % diagnosis)
            raise RuntimeError("OA offers no video model to ComfyUI; install a video model in OA setup")
        model = offered[0]
    import abstraction.inference.job as jobwire
    from abstraction.job.acceptance import RequestIdentity, Submission

    image = jobwire.ImageRequest(model=model, mode=jobwire.Mode.GENERATE, prompt=prompt, size=size,
                                 count=1, guarantees=[])
    if first_frame_png is not None:
        image.mode = jobwire.Mode.EDIT
        image.image_digest = _stage_frame(factory, first_frame_png)
        image.image_media_type = "image/png"
    spec = jobwire.encode(jobwire.Document(request=jobwire.Request(
        profile=jobwire.Profile.VIDEO, image=image, duration_ms=duration_ms)))
    jobs = _Jobs(factory)
    window = jobs.get().get_history_window()
    identity = RequestIdentity(key="comfyui-video-" + secrets.token_hex(16),
                               history_epoch=window.history_epoch, attempt=0)
    submission = Submission(identity=identity, kind=JOB_KIND, spec=spec,
                            required_guarantees=[RECOVERABLE_UPSTREAM], label="ComfyUI video")
    finished, operation, person_cancelled = False, "", False
    try:
        try:
            receipt = _admit(jobs, submission, sleep)
        except _NotAccepted:
            finished = True
            raise
        operation = receipt.operation_id
        last, unavailable = None, 0
        while True:
            if interrupted is not None:
                try:
                    interrupted()
                except BaseException:
                    person_cancelled = True
                    raise
            try:
                observed = jobs.get().observe_work(identity)
            except Exception:
                jobs.drop()
                observed = None
            if observed is None or observed.outcome == "unavailable":
                unavailable += 1
                if unavailable >= MAX_UNAVAILABLE:
                    raise RuntimeError("OA video job observation stayed unavailable")
                sleep(POLL_SECONDS)
                continue
            if observed.outcome != "observed" or observed.snapshot is None:
                raise RuntimeError("OA video job observation %s" % observed.outcome)
            unavailable = 0
            snapshot = observed.snapshot
            if snapshot.state == "complete":
                finished = True
                if progress is not None:
                    progress(1, 1, "")  # a completed job reads 1 of 1 (INF-G6)
                break
            if snapshot.state in ("failed", "cancelled"):
                finished = True
                failure = snapshot.failure
                detail = "%s (%s)" % (failure.message, failure.cause) if failure is not None else "no reason given"
                raise RuntimeError("OA video job %s: %s" % (snapshot.state, detail))
            current = (snapshot.progress.done, snapshot.progress.total, snapshot.waiting or "")
            if current != last and progress is not None:
                progress(*current)
            last = current
            sleep(POLL_SECONDS)
        result = _read_result(jobs, identity, sleep)
    finally:
        if person_cancelled and not finished:
            _cancel(factory, identity, operation or identity.key, sleep)
    delivery = result.deliveries[0]
    data = _content.read_delivery(factory(time.monotonic() + 300), delivery, _content.VIDEO_MEDIA_TYPES,
                                  MAX_VIDEO_BYTES, "video")
    if _content.video_media(data) != delivery.media_type:
        raise RuntimeError("OA video delivery declares %s and does not sniff as it" % delivery.media_type)
    return data, delivery.media_type, {"host": result.host, "model": result.model,
                                       "operation": receipt.operation_id, "digest": delivery.digest,
                                       "media_type": delivery.media_type}


def _waiting_text(done, total, waiting):
    match = re.fullmatch(r"queue:([1-9][0-9]{0,8})", waiting)
    if match:
        return "Queued: next" if match.group(1) == "1" else "Queued: position %s" % match.group(1)
    if waiting == "queued":
        return "Queued"
    if waiting:
        return "Waiting: %s" % waiting
    if total > 0:
        return "Step %d of %d" % (done, total)
    return "Starting"


class _ComfyProgress:
    """ComfyUI's progress bar and progress text for one node."""

    def __init__(self, node_id):
        self.node_id, self.bar = node_id, None

    def __call__(self, done, total, waiting):
        utils = sys.modules.get("comfy.utils")
        if total > 0 and utils is not None:
            if self.bar is None:
                self.bar = utils.ProgressBar(total, node_id=self.node_id)
            self.bar.update_absolute(min(done, total), total)
        server = sys.modules.get("server")
        instance = getattr(getattr(server, "PromptServer", None), "instance", None)
        if instance is not None and self.node_id is not None:
            try:
                instance.send_progress_text(_waiting_text(done, total, waiting), self.node_id)
            except Exception:
                pass


def _comfy_interrupted():
    """Raise ComfyUI's interrupt when a person stopped the run."""
    management = sys.modules.get("comfy.model_management")
    if management is not None:
        management.throw_exception_if_processing_interrupted()


def _frame_png(first_frame):
    """One ComfyUI IMAGE as an 8-bit RGB PNG."""
    if first_frame is None:
        return None
    shape = tuple(getattr(first_frame, "shape", ()))
    if len(shape) != 4 or shape[0] != 1 or shape[3] not in (3, 4):
        raise ValueError("OA video takes one image as its first frame")
    if shape[1] * shape[2] > MAX_FRAME_PIXELS:
        raise ValueError("OA video first frame exceeds 4096x4096 pixels")
    import numpy as np
    from PIL import Image
    import torch
    pixels = first_frame[0, :, :, :3].detach().cpu().clamp(0, 1).mul(255).round().to(dtype=torch.uint8)
    output = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(pixels.numpy()), "RGB").save(output, format="PNG")
    return output.getvalue()


class OAVideoGenerate:
    """A deliberate text- or image-to-video operation through OA's video jobs."""

    # Frames come from ComfyUI's own GetVideoComponents when a graph connects it.
    RETURN_TYPES = ("VIDEO", "STRING")
    RETURN_NAMES = ("video", "OA route")
    FUNCTION = "generate"
    CATEGORY = "OpenAbstractions"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"model": ([AUTO] + offered_models(), {"default": AUTO,
                                       "tooltip": "auto takes the first model OA offers for video"}),
                             "prompt": ("STRING", {"multiline": True}),
                             "size": (list(SIZES), {"default": SIZES[0]}),
                             "duration": ("INT", {"default": 2, "min": 1, "max": MAX_SECONDS, "step": 1,
                                                  "tooltip": "seconds"})},
                "optional": {"first_frame": ("IMAGE",)},
                "hidden": {"unique_id": "UNIQUE_ID"}}

    def generate(self, model, prompt, size, duration, first_frame=None, unique_id=None):
        if type(duration) is not int or not 1 <= duration <= MAX_SECONDS:
            raise ValueError("OA video duration must be 1 to %d seconds" % MAX_SECONDS)
        frame = _frame_png(first_frame)
        data, _media_type, route = generate(model, prompt, size, duration * 1000, first_frame_png=frame,
                                            progress=_ComfyProgress(unique_id), interrupted=_comfy_interrupted)
        from comfy_api.latest import InputImpl
        return InputImpl.VideoFromFile(io.BytesIO(data)), json.dumps(route, sort_keys=True)
