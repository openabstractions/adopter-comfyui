"""Explicit ComfyUI image node backed by the installed OA image service."""

import hashlib
import io
import json
import os
import re
import secrets
import time
import unicodedata
from pathlib import Path

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

IMAGE_CONTRACT = "abstraction.inference/image@1"
IMAGE_POLICY_CONTRACT = "abstraction.inference/image@2"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
# image@1 resolves each input image of at most 20 MiB (inference MaxImageBytes).
MAX_INPUT_BYTES = 20 * 1024 * 1024
MEDIA_TYPES = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}
# The grant an edit's inputs need, named in refusals and in README.md: OA stores
# the input image and its mask and reads them back for this program's own edit
# under the input:image profile, with content write and read on that one
# resource (storage STO-W12, inference INF-V4).
INPUT_RIGHTS = {
    "write": "abstraction.storage/content.write",
    "read": "abstraction.storage/content.read",
    "resource": "input:image",
}
_INPUT_GRANT = "%s and %s on %s" % (INPUT_RIGHTS["write"], INPUT_RIGHTS["read"], INPUT_RIGHTS["resource"])
# A program without that grant gets one paired first-use question, asked when
# its edit is refused for the input read or for a complete decision that reads
# not_granted, never by the refused upload itself (INF-V4). The node then
# uploads again every FIRST_USE_POLL_SECONDS for at most FIRST_USE_WAIT_SECONDS
# and starts the edit once more after an allow. A "never" answer writes
# denials, which the store reports as forbidden like a pending question, so
# the node cannot end the wait early; it waits the whole time.
FIRST_USE_WAIT_SECONDS = 120
FIRST_USE_POLL_SECONDS = 2.0
# The start refusals that ask the paired question. A refusal names every
# missing grant in one reason, separated by spaces (inference INF-V4).
_ASKING_REASONS = ("content:read:forbidden", "rights:not_granted")


def _names(reason, *words):
    """Whether a refusal reason names any of words."""
    return any(word in (reason or "").split() for word in words)


# A first image start from this app asks the person OA's image permission
# question. The runtime holds the start only briefly; a refusal still naming
# the question (rights:question_pending:<id>, inference INF-W11) makes the
# node wait up to FIRST_USE_IMAGE_WAIT_SECONDS for the person's answer through
# abstraction.asks/permission@1 and start once more, as the MCP picture tool does.
FIRST_USE_IMAGE_WAIT_SECONDS = 120
_QUESTION_PENDING = "rights:question_pending:"


def _pending_questions(reason):
    """The first-use question ids a refusal reason names, in order."""
    ids = []
    for word in (reason or "").split():
        if word.startswith(_QUESTION_PENDING) and len(word) > len(_QUESTION_PENDING):
            if word[len(_QUESTION_PENDING):] not in ids:
                ids.append(word[len(_QUESTION_PENDING):])
    return ids


def _word(value):
    return str(getattr(value, "value", value) or "")


def _await_questions(machine, ids, clock=time.monotonic):
    """refused, blocked, or answered once the person answered each question or the wait ended."""
    from abstraction.facade import Scope
    from abstraction.asks.api import PermissionApplicationClient
    permission = PermissionApplicationClient(machine.resolve_service("abstraction.asks/permission@1", scope=Scope.LOCAL))
    until = clock() + FIRST_USE_IMAGE_WAIT_SECONDS
    for question in ids:
        while True:
            left = until - clock()
            if left <= 0:
                return "answered"
            observation = permission.observe_question(question, int(min(30.0, left) * 1000)).observation
            outcome, answer = _word(observation.outcome), observation.answer
            if outcome != "pending" and not (answer is not None and answer.pending):
                break
        if outcome == "denied" and observation.policy_revision:
            return "blocked"
        if outcome == "answered" and answer is not None and answer.option in ("never", "refuse"):
            return "blocked" if answer.option == "never" else "refused"
    return "answered"


class _InputForbidden(RuntimeError):
    """The content store refused an edit input with forbidden."""


def _media_type(data):
    """The PNG, JPEG or WebP type of data by its first bytes, or ""."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _check_inputs(image, mask):
    """Check edit inputs before any service call; return the image media type."""
    if image is None:
        if mask is not None:
            raise ValueError("An OA image mask needs an input image")
        return ""
    if not isinstance(image, bytes) or not 0 < len(image) <= MAX_INPUT_BYTES:
        raise ValueError("The OA input image is empty or exceeds 20 MiB")
    media_type = _media_type(image)
    if not media_type:
        raise ValueError("The OA input image must be a PNG, JPEG or WebP image")
    if mask is not None and (not isinstance(mask, bytes) or len(mask) > MAX_INPUT_BYTES or _media_type(mask) != "image/png"):
        raise ValueError("The OA image mask must be a PNG image of at most 20 MiB")
    return media_type


def _store_input(machine, data):
    """Write one input through the caller's authorized content writer; return its digest.

    A forbidden write raises _InputForbidden, which names the input:image grant;
    any other refusal names its outcome only.
    """
    from abstraction.facade import Scope
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    try:
        stored = machine.resolve_storage_writer(scope=Scope.LOCAL).write(secrets.token_hex(16), digest, data)
    except Exception as error:
        outcome = getattr(error, "outcome", None)
        if outcome is None:
            raise
        if outcome == "forbidden":
            raise _InputForbidden("OA did not store the edit input (forbidden): this app needs %s" % _INPUT_GRANT) from error
        raise RuntimeError("OA did not store the edit input (%s)" % outcome) from error
    if stored is None or stored.digest != digest:
        raise RuntimeError("OA did not store the input image")
    return digest


def _store_inputs(machine, inputs):
    """Store each edit input; return the first forbidden refusal, or None when all are stored."""
    refused = None
    for data in inputs:
        try:
            _store_input(machine, data)
        except _InputForbidden as error:
            refused = refused or error
    return refused


def png_from_pixels(pixels):
    """Encode the first image of a ComfyUI IMAGE batch (B, H, W, C floats 0..1) as PNG."""
    import numpy as np
    from PIL import Image
    frame = np.asarray(pixels)[0]
    data = (np.clip(frame[..., :3], 0.0, 1.0) * 255.0).round().astype(np.uint8)
    output = io.BytesIO()
    Image.fromarray(data, "RGB").save(output, format="PNG")
    return output.getvalue()


def png_from_mask(mask):
    """Encode the first ComfyUI MASK (B, H, W floats 0..1, 1 marks the area to change) as the image
    contract's mask (inference CONTRACT.md INF-I1): an RGBA PNG whose fully transparent pixels mark the
    area to change. A value above 0.5 changes, as ComfyUI's own 1.0 - mask.round() reads the mask
    (0.5 rounds to 0 and keeps); every other pixel is opaque and kept."""
    import numpy as np
    from PIL import Image
    values = np.asarray(mask)
    frame = values[0] if values.ndim == 3 else values
    rgba = np.zeros(frame.shape + (4,), dtype=np.uint8)
    rgba[..., 3] = np.where(frame > 0.5, 0, 255)
    output = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(output, format="PNG")
    return output.getvalue()


def edit_inputs(pixels, mask):
    """The PNG bytes of a ComfyUI IMAGE's first frame and of an optional MASK of the same size."""
    if pixels is None:
        if mask is not None:
            raise ValueError("An OA image mask needs an input image")
        return None, None
    import numpy as np
    height, width = np.asarray(pixels).shape[1:3]
    if mask is not None:
        values = np.asarray(mask)
        mask_height, mask_width = (values[0] if values.ndim == 3 else values).shape
        if (mask_width, mask_height) != (width, height):
            raise ValueError("The OA image mask is %dx%d pixels and the image %dx%d; they must match"
                             % (mask_width, mask_height, width, height))
    return png_from_pixels(pixels), png_from_mask(mask) if mask is not None else None
# DEFAULT_SELECTION_POLICY is the selection policy this app reads for its
# image profile. The person's model choice in OpenAbstractions writes a policy
# of this name for ComfyUI's program; the node reads it by name at each run.
DEFAULT_SELECTION_POLICY = "oa-image-default"
POLICY_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


class MissingImageSelection(ValueError):
    """No image selection policy name is available to this node."""


def _choice_text(value, maximum):
    return (isinstance(value, str) and bool(value.strip()) and len(value.encode("utf-8")) <= maximum
            and not any(unicodedata.category(c) in {"Cc", "Zl", "Zp"} for c in value))


def _choose_model(machine, sleep):
    """Share this run's complete model choice; retain its reference while observing."""
    from abstraction.facade import Scope
    router = machine.resolve_router(scope=Scope.LOCAL)
    from abstraction.router import OfferRequest, Placement
    snapshot = router.offer(OfferRequest(profile="image", placement=Placement.ANY, reserve=0, fresh=True))
    if snapshot.outcome != "offered":
        raise MissingImageSelection("OA image model inventory %s; enter a model in the model field" % snapshot.outcome)
    names = [name for offer in snapshot.offers for name in offer.names]
    if any(not _choice_text(name, 64) for name in names):
        raise MissingImageSelection("OA image model names exceed the choice bounds; enter a model in the model field")
    models = list(dict.fromkeys(names))
    if not 1 <= len(models) <= 3:
        raise MissingImageSelection("OA cannot present every offered image model within the choice bounds; "
                                    "enter a model in the model field, or choose one in OA setup")
    from abstraction.asks.api import ChoiceApplicationClient, ChoiceOption, ChoiceRequest, RequestRef
    choices = ChoiceApplicationClient(machine.resolve_service("abstraction.asks/application@2", scope=Scope.LOCAL))
    now = int(time.time() * 1000)
    ref = RequestRef(key="comfyui-image-" + secrets.token_hex(16), replay_until_ms=now + 300000)
    request = ChoiceRequest(request_ref=ref, title="Choose an image model for this ComfyUI run",
                            options=[ChoiceOption(id=name, label=name) for name in models], pending_until_ms=now + 90000)
    until = time.monotonic() + 90
    observation = choices.share_choice(request, models)
    known_id = ""
    while True:
        outcome, answer = observation.outcome, observation.answer
        admitted = outcome in {"pending", "answered", "dismissed"}
        terminal = outcome in {"answered", "dismissed"}
        if (admitted != bool(observation.id) or terminal != (answer is not None)
                or (observation.id and not _choice_text(observation.id, 128))
                or (known_id and observation.id and observation.id != known_id)):
            raise RuntimeError("OA returned an inconsistent image model choice")
        if terminal:
            if (answer.id != observation.id or type(answer.answered_ms) is not int
                    or not 0 < answer.answered_ms < request.pending_until_ms
                    or not _choice_text(answer.answered_by, 4096)
                    or not (re.fullmatch(r"msix:[A-Za-z0-9.-]{3,50}_[a-z0-9]{13}", answer.answered_by)
                            or (os.path.isabs(answer.answered_by) and os.path.normpath(answer.answered_by) == answer.answered_by))
                    or (outcome == "answered" and answer.option not in models)
                    or (outcome == "dismissed" and answer.option != "")):
                raise RuntimeError("OA returned an inconsistent image model answer")
            if outcome == "dismissed":
                raise MissingImageSelection("OA image model choice dismissed; no image was started")
            return answer.option, (choices, ref, answer)
        if outcome != "pending":
            raise MissingImageSelection("OA image model choice %s; no image was started" % outcome)
        known_id = observation.id
        if time.monotonic() >= until or int(time.time() * 1000) >= request.pending_until_ms:
            raise MissingImageSelection("OA image model choice is still pending; answer it in OA Questions and run again")
        sleep(0.1)
        observation = choices.observe(ref, 1000)


def default_selection_policy():
    """Return the image selection policy name this app reads.

    The name is the app's own (DEFAULT_SELECTION_POLICY). An isolated test or
    an explicitly configured instance may name another policy in
    ABSTRACTION_COMFYUI_SELECTION_POLICY. Each image request still uses
    ComfyUI's live caller identity and public IPC authorization; a policy the
    runtime does not hold is refused with its admission reason.
    """
    name = os.environ.get("ABSTRACTION_COMFYUI_SELECTION_POLICY", "") or DEFAULT_SELECTION_POLICY
    if not POLICY_NAME.fullmatch(name):
        raise ValueError("ABSTRACTION_COMFYUI_SELECTION_POLICY is not a selection policy name.")
    return name


def selected_image_policy(*, machine_factory=None):
    """Read this application's effective image choice through its native caller.

    Recipes keep the policy name, preserving its provider pins and fallback
    order when inference admits the request. No management grant is requested.
    """
    from abstraction.facade import Scope
    from abstraction.facade.client import Machine
    from abstraction.inference.api import SelectionOperatorV2Client
    name = default_selection_policy()
    make_machine = machine_factory or (lambda: Machine(
        endpoint=os.environ.get("ABSTRACTION_RUNTIME_ENDPOINT") or None,
        deadline=time.monotonic() + 30, library=oa_route and oa_route.library()))
    operator = SelectionOperatorV2Client(make_machine().resolve_service(
        "abstraction.inference/selection-operator@2", scope=Scope.LOCAL))
    read = operator.read_effective(name)
    if read.outcome != "found" or read.policy is None or read.policy.name != name or not read.policy.candidates:
        raise MissingImageSelection("Choose an image model for this app in OA Models (%s)." % read.outcome)
    return name


def generate(model, prompt, size, selection_policy="", *, machine_factory=None, with_route=False, image=None, mask=None,
             sleep=time.sleep):
    """Return one verified image's bytes and media type through public OA clients.

    No Comfy graph, workflow path, provider URL, or vendor option is translated.
    An unsupported request is refused by image@1 before provider work. With
    image (PNG, JPEG or WebP bytes) and an optional PNG mask, both go through
    the caller's content writer first and the call is an image@1 edit naming
    their digests. A forbidden upload still starts the edit, whose refused input
    read asks OA's first-use question; after an allow the node uploads again and
    starts the edit once more.
    """
    input_type = _check_inputs(image, mask)
    if not isinstance(model, str) or not isinstance(selection_policy, str):
        raise ValueError("OA image generation requires string model and policy names")
    missing = None
    if not model.strip() and not selection_policy:
        try:
            selection_policy = default_selection_policy()
        except MissingImageSelection as error:
            missing = error
    explicit = bool(model.strip())
    policy = bool(re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", selection_policy))
    if (explicit == policy and missing is None) or (selection_policy and not policy) or not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("OA image generation requires exactly one model or saved policy and a prompt")
    if len(prompt.encode("utf-8")) > 32768 or not isinstance(size, str) or not re.fullmatch(r"auto|[1-9][0-9]*x[1-9][0-9]*", size):
        raise ValueError("OA image generation requires a supported prompt and size")
    from abstraction.facade import Scope
    from abstraction.facade.client import Machine
    from abstraction.inference.api import ImageClient, ImageRequest, ImageMode

    make_machine = machine_factory or (lambda: Machine(
        endpoint=os.environ.get("ABSTRACTION_RUNTIME_ENDPOINT") or None,
        deadline=time.monotonic() + 300, library=oa_route and oa_route.library()))
    machine = make_machine()
    choice = None
    if missing is not None:
        model, choice = _choose_model(machine, sleep)
    content = {"mode": ImageMode.GENERATE, "prompt": prompt, "size": size, "count": 1, "guarantees": []}
    inputs = [data for data in (image, mask) if data is not None]
    refused = _store_inputs(machine, inputs)
    if input_type:
        content.update(mode=ImageMode.EDIT, image_digest="sha256:" + hashlib.sha256(image).hexdigest(),
                       image_media_type=input_type)
        if mask is not None:
            content.update(mask_digest="sha256:" + hashlib.sha256(mask).hexdigest(), mask_media_type="image/png")
    contract = IMAGE_POLICY_CONTRACT if policy else IMAGE_CONTRACT
    binding = machine.resolve_service(contract, scope=Scope.LOCAL)
    if policy:
        from abstraction.inference.api import ImageV2Client, ImageRequestV2, ImageRequestContent, Target
        client = ImageV2Client(binding)
        request = ImageRequestV2(target=Target(selection_policy=selection_policy),
            content=ImageRequestContent(**content))
    else:
        client = ImageClient(binding)
        request = ImageRequest(model=model, **content)
    admission = client.start(request)
    if refused is not None and _names(admission.reason, *_ASKING_REASONS):
        # The refusal asked the paired question; an allow lets the upload through.
        for _ in range(int(FIRST_USE_WAIT_SECONDS / FIRST_USE_POLL_SECONDS)):
            sleep(FIRST_USE_POLL_SECONDS)
            refused = _store_inputs(machine, inputs)
            if refused is None:
                admission = client.start(request)
                break
        else:
            raise RuntimeError("OA image admission %s: %s; this app needs %s; allow it in OA's "
                               "question and run the node again" % (admission.outcome, admission.reason, _INPUT_GRANT))
    elif refused is not None and admission.outcome != "accepted":
        raise refused
    questions = _pending_questions(admission.reason) if admission.outcome in ("not_permitted", "forbidden") else []
    if questions and not admission.operation:
        verdict = _await_questions(machine, questions)
        if verdict == "refused":
            raise RuntimeError("OA image admission %s: the person chose not to allow pictures from this app for now; "
                               "OA can ask again in 10 minutes" % admission.outcome)
        if verdict == "blocked":
            raise RuntimeError("OA image admission %s: the person blocked pictures from this app; they can change it "
                               "on the Permissions page of the OpenAbstractions Panel" % admission.outcome)
        admission = client.start(request)
    if (admission.outcome == "no_host" and not admission.operation
            and _names(admission.reason, "selection:unknown", "selection:provider_stale")):
        try:
            model, choice = _choose_model(machine, sleep)
        except MissingImageSelection as error:
            raise RuntimeError("OA image admission no_host: %s; %s" % (admission.reason, error)) from error
        policy, contract = False, IMAGE_CONTRACT
        client = ImageClient(machine.resolve_service(contract, scope=Scope.LOCAL))
        request = ImageRequest(model=model, **content)
        admission = client.start(request)
    if admission.outcome != "accepted" or not admission.operation:
        guidance = "; enter an image model in this node's model field, or choose one in OA setup" if policy else ""
        if _names(admission.reason, "content:read:forbidden"):
            guidance = "; this app needs %s" % _INPUT_GRANT
        original = RuntimeError("OA image admission %s: %s%s" % (admission.outcome, admission.reason, guidance))
        raise original
    operation = admission.operation
    finished = False
    try:
        cursor = 0
        streamed = None
        while True:
            page = client.observe(operation, cursor, 64, 65536, 25000)
            if page.outcome != "page" or page.next < cursor:
                raise RuntimeError("OA image observation %s" % page.outcome)
            for delta in page.deltas:
                if delta.kind == "image_result":
                    streamed = delta.image_result
                if delta.image_end is not None:
                    reply = delta.image_end
                    finished = True
                    if reply.outcome != "completed":
                        guidance = "; enter an image model in this node's model field, or choose one in OA setup" if policy else ""
                        raise RuntimeError("OA image result %s: %s%s" % (reply.outcome, reply.reason, guidance))
                    if len(reply.deliveries) != 1 or streamed is None or streamed.index != 0 \
                            or streamed.delivery.digest != reply.deliveries[0].digest:
                        raise RuntimeError("OA image result differs from terminal delivery")
                    result = _read_image(make_machine(), reply.deliveries[0])
                    if choice is not None:
                        choices, ref, answer = choice
                        acknowledgement = choices.acknowledge(ref, answer)
                        if acknowledgement.outcome != "acknowledged":
                            raise RuntimeError("OA image completed; model choice acknowledgement %s" % acknowledgement.outcome)
                    return (*result, reply.host, reply.model, operation) if with_route else result
            if page.at_end:
                raise RuntimeError("OA image ended without a terminal result")
            cursor = page.next
    finally:
        if not finished:
            try:
                binding = make_machine().resolve_service(contract, scope=Scope.LOCAL)
                if policy:
                    from abstraction.inference.api import ImageV2Client
                    ImageV2Client(binding).cancel(operation)
                else:
                    ImageClient(binding).cancel(operation)
            except Exception:
                pass


def _read_image(machine, delivery):
    return _content.read_delivery(machine, delivery, MEDIA_TYPES, MAX_IMAGE_BYTES, "image"), delivery.media_type


class OAImageGenerate:
    """A deliberate text-to-image or image edit operation, separate from arbitrary Comfy graphs.

    A connected image makes the call an image@1 edit of its first frame; a
    connected mask marks the area to change (1 changes, 0 keeps).
    """

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("image", "OA route")
    FUNCTION = "generate"
    CATEGORY = "OpenAbstractions"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"model": ("STRING", {"default": ""}),
                             "prompt": ("STRING", {"multiline": True}),
                             "selection_policy": ("STRING", {"default": ""}),
                             "size": ("STRING", {"default": "1024x1024"})},
                "optional": {"image": ("IMAGE",), "mask": ("MASK",)}}

    def generate(self, model, prompt, size, selection_policy="", image=None, mask=None):
        source, area = edit_inputs(image.cpu().numpy() if image is not None else None,
                                   mask.cpu().numpy() if mask is not None else None)
        data, media_type, host, selected_model, operation = generate(
            model, prompt, size, selection_policy, with_route=True, image=source, mask=area)
        from PIL import Image
        import numpy as np
        import torch
        with Image.open(io.BytesIO(data)) as image:
            if image.format != MEDIA_TYPES[media_type] or image.width * image.height > 4096 * 4096:
                raise RuntimeError("OA image format or dimensions do not match the delivery")
            pixels = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
        route = json.dumps({"host": host, "model": selected_model, "operation": operation}, sort_keys=True)
        # ComfyUI keeps the ui output in the prompt's history, where an agent's
        # workflow.run reads which OA engine made the picture.
        return {"ui": {"oa_route": [route]}, "result": (torch.from_numpy(pixels).unsqueeze(0), route)}
