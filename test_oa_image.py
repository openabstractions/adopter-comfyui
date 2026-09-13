"""Boundary tests for the explicit OA image node; no native engine is started."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

MODULE_PATH = Path(__file__).with_name("oa_image.py")
spec = importlib.util.spec_from_file_location("oa_image_test_target", MODULE_PATH)
oa_image = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oa_image)

DATA = b"fixture image bytes"
DIGEST = "sha256:" + hashlib.sha256(DATA).hexdigest()


class Row:
    def __init__(self, **values):
        self.__dict__.update(values)


class Image:
    def __init__(self, delivery, calls):
        self.delivery, self.calls = delivery, calls

    def start(self, request):
        self.calls.append(("start", request))
        return Row(outcome="accepted", operation="op")

    def observe(self, *_):
        self.calls.append("observe")
        return Row(outcome="page", next=2, at_end=True, deltas=[
            Row(kind="image_result", image_result=Row(index=0, delivery=self.delivery), image_end=None),
            Row(kind="end", image_result=None, image_end=Row(
                outcome="completed", host="fixture", model="paint", deliveries=[self.delivery])),
        ])

    def cancel(self, _):
        self.calls.append("cancel")


class Reader:
    def __init__(self, calls, corrupt=False):
        self.calls, self.corrupt = calls, corrupt

    def open(self, digest):
        self.calls.append(("open", digest))
        return Row(outcome="opened", resource=Row(handle="h", digest=digest, size=len(DATA)))

    def copy(self, _, destination):
        self.calls.append("copy")
        destination.write(DATA[:-1] + b"X" if self.corrupt else DATA)

    def close(self, _):
        self.calls.append("close")


PNG = b"\x89PNG\r\n\x1a\n" + b"input picture"
MASK = b"\x89PNG\r\n\x1a\n" + b"mask picture"
JPEG = b"\xff\xd8\xff\xe0" + b"input picture"


class Refused(RuntimeError):
    """The storage client's OutcomeError shape: a refusal carrying its outcome word."""

    def __init__(self, outcome):
        super().__init__(outcome)
        self.outcome = outcome


class Writer:
    def __init__(self, calls, refuse=None):
        self.calls, self.refuse = calls, refuse

    def write(self, request, digest, content):
        self.calls.append(("write", request, digest, content))
        if self.refuse:
            raise Refused(self.refuse)
        return Row(digest=digest, size=len(content), evidence="hashed")


class Choice:
    def __init__(self, calls, observations=None):
        self.calls = calls
        self.observations = iter(observations or [Row(outcome="answered", id="choice",
            answer=Row(id="choice", option="second", answered_ms=int(time.time() * 1000),
                       answered_by=str(Path(__file__).resolve())))])

    def share_choice(self, request, assistant_options):
        self.calls.append(("share", request, assistant_options))
        return next(self.observations)

    def observe(self, request_ref, wait_ms):
        self.calls.append(("choice_observe", request_ref, wait_ms))
        return next(self.observations)

    def acknowledge(self, request_ref, answer):
        self.calls.append(("ack", request_ref, answer))
        return Row(outcome="acknowledged")


class ImageTests(unittest.TestCase):
    def run_image(self, delivery, corrupt=False, with_route=False, policy="", empty_target=False, image_input=None, mask=None,
                  write_refusal=None, admission=None, writer=None, start=None, sleep=None, calls=None, offers=None, choice=None,
                  offer_outcome="offered", permission=None):
        calls = [] if calls is None else calls
        image = Image(delivery, calls)
        if admission is not None:
            image.start = lambda request: calls.append(("start", request)) or admission
        if start is not None:
            image.start = lambda request: calls.append(("start", request)) or start(request)
        reader = Reader(calls, corrupt)
        writer = writer or Writer(calls, write_refusal)
        class Machine:
            def resolve_service(self, contract, *, scope):
                calls.append(("contract", contract))
                if contract == "abstraction.asks/permission@1":
                    return permission
                return choice if contract == "abstraction.asks/application@2" else image

            def resolve_storage(self, *, scope):
                return reader

            def resolve_storage_writer(self, *, scope):
                calls.append(("writer", scope))
                return writer

            def resolve_router(self, *, scope):
                calls.append(("router", scope))
                def offer(request):
                    calls.append(("offer", request))
                    names = offers() if callable(offers) else offers
                    return Row(outcome=offer_outcome, offers=[Row(names=[name]) for name in (names or [])])
                return Row(offer=offer)

            def resolve_asks_choices(self, *, scope):
                calls.append(("choices", scope))
                return choice

        facade = types.ModuleType("abstraction.facade")
        facade.Scope = Row(LOCAL="local")
        facade_client = types.ModuleType("abstraction.facade.client")
        facade_client.Machine = Machine
        inference = types.ModuleType("abstraction.inference.api")
        inference.ImageClient = lambda binding: binding
        inference.ImageV2Client = lambda binding: binding
        inference.ImageRequest = Row
        inference.ImageRequestV2 = Row
        inference.ImageRequestContent = Row
        inference.Target = Row
        inference.ImageMode = Row(GENERATE="generate", EDIT="edit")
        asks = types.ModuleType("abstraction.asks.api")
        asks.ChoiceOption = asks.ChoiceRequest = asks.RequestRef = Row
        asks.ChoiceApplicationClient = lambda binding: binding
        asks.PermissionApplicationClient = lambda binding: binding
        router = types.ModuleType("abstraction.router")
        router.OfferRequest = Row
        router.Placement = Row(LOCAL="local", ANY="any")
        with patch.dict(sys.modules, {"abstraction": types.ModuleType("abstraction"),
                                      "abstraction.facade": facade,
                                      "abstraction.facade.client": facade_client,
                                      "abstraction.inference": types.ModuleType("abstraction.inference"),
                                      "abstraction.inference.api": inference,
                                      "abstraction.asks.api": asks, "abstraction.router": router}):
            return oa_image.generate("" if policy or empty_target else "paint", "cat", "512x512", policy, machine_factory=Machine,
                                     with_route=with_route, image=image_input, mask=mask,
                                     sleep=sleep or (lambda _: self.fail("no wait expected"))), calls

    def test_input_image_and_mask_are_uploaded_then_sent_as_edit(self):
        delivery = Row(location="local", media_type="image/png", size=len(DATA), digest=DIGEST)
        value, calls = self.run_image(delivery, image_input=JPEG, mask=MASK)
        self.assertEqual(value, (DATA, "image/png"))
        writes = [call for call in calls if call[0] == "write"]
        self.assertEqual([(w[2], w[3]) for w in writes], [
            ("sha256:" + hashlib.sha256(JPEG).hexdigest(), JPEG), ("sha256:" + hashlib.sha256(MASK).hexdigest(), MASK)])
        self.assertTrue(all(16 <= len(w[1]) <= 128 for w in writes) and writes[0][1] != writes[1][1])
        start = next(i for i, call in enumerate(calls) if call[0] == "start")
        self.assertLess(calls.index(writes[1]), start, "inputs are stored before the call starts")
        request = calls[start][1]
        self.assertEqual((request.mode, request.image_digest, request.image_media_type, request.mask_digest, request.mask_media_type),
                         ("edit", writes[0][2], "image/jpeg", writes[1][2], "image/png"))
        self.assertFalse(getattr(request, "extensions", None), "the node names no vendor extension")

    def test_saved_policy_edit_carries_the_input_in_content(self):
        delivery = Row(location="local", media_type="image/png", size=len(DATA), digest=DIGEST)
        _, calls = self.run_image(delivery, policy="paint_local", image_input=PNG)
        content = next(call for call in calls if call[0] == "start")[1].content
        self.assertEqual((content.mode, content.image_digest, content.image_media_type, getattr(content, "mask_digest", "")),
                         ("edit", "sha256:" + hashlib.sha256(PNG).hexdigest(), "image/png", ""))

    def test_edit_inputs_are_checked_before_any_service_call(self):
        for image_input, mask, message in [(b"plain text", None, "PNG, JPEG or WebP"), (PNG, JPEG, "mask must be a PNG"),
                                           (None, MASK, "mask needs an input image"),
                                           (b"\x89PNG\r\n\x1a\n" + b"x" * oa_image.MAX_INPUT_BYTES, None, "exceeds")]:
            with self.assertRaisesRegex(ValueError, message):
                oa_image.generate("paint", "cat", "512x512", machine_factory=lambda: self.fail("must stop before IPC"),
                                  image=image_input, mask=mask)

    @unittest.skipUnless(importlib.util.find_spec("numpy") and importlib.util.find_spec("PIL"), "numpy and Pillow")
    def test_comfy_image_and_mask_become_png(self):
        import io
        import numpy as np
        from PIL import Image as PILImage
        pixels = np.zeros((1, 2, 3, 3), dtype=np.float32)
        pixels[0, 0, 0] = [1.0, 0.5, 0.0]
        data = oa_image.png_from_pixels(pixels)
        with PILImage.open(io.BytesIO(data)) as picture:
            self.assertEqual((picture.format, picture.mode, picture.size, picture.getpixel((0, 0))), ("PNG", "RGB", (3, 2), (255, 128, 0)))

    @unittest.skipUnless(importlib.util.find_spec("numpy") and importlib.util.find_spec("PIL"), "numpy and Pillow")
    def test_comfy_mask_becomes_the_contract_mask(self):
        """[INF-I1]: a PNG of the input's size whose fully transparent pixels mark the area to change."""
        import io
        import numpy as np
        from PIL import Image as PILImage
        # 1 marks the area to change; 0.5 keeps, as ComfyUI's own 1.0 - mask.round() reads it.
        comfy = np.array([[[0.0, 1.0, 0.5], [0.75, 0.0, 1.0]]], dtype=np.float32)
        data = oa_image.png_from_mask(comfy)
        with PILImage.open(io.BytesIO(data)) as picture:
            self.assertEqual((picture.format, picture.size), ("PNG", (3, 2)))
            rgba = picture.convert("RGBA")
            changed = {(x, y) for y in range(2) for x in range(3) if rgba.getpixel((x, y))[3] == 0}
            kept_alpha = {rgba.getpixel((x, y))[3] for y in range(2) for x in range(3) if (x, y) not in changed}
        self.assertEqual(changed, {(1, 0), (0, 1), (2, 1)})
        self.assertEqual(kept_alpha, {255})

    @unittest.skipUnless(importlib.util.find_spec("numpy") and importlib.util.find_spec("PIL"), "numpy and Pillow")
    def test_node_inputs_refuse_a_mask_of_another_size(self):
        import numpy as np
        image, mask = oa_image.edit_inputs(np.zeros((1, 2, 3, 3), dtype=np.float32), np.ones((1, 2, 3), dtype=np.float32))
        self.assertTrue(image.startswith(b"\x89PNG") and mask.startswith(b"\x89PNG"))
        self.assertEqual(oa_image.edit_inputs(np.zeros((1, 2, 3, 3), dtype=np.float32), None)[1], None)
        with self.assertRaisesRegex(ValueError, "mask is 3x2 pixels and the image 2x3"):
            oa_image.edit_inputs(np.zeros((1, 3, 2, 3), dtype=np.float32), np.ones((1, 2, 3), dtype=np.float32))
        with self.assertRaisesRegex(ValueError, "mask needs an input image"):
            oa_image.edit_inputs(None, np.ones((1, 2, 3), dtype=np.float32))

    def test_refused_input_write_names_the_storage_grant(self):
        delivery = Row(location="local", media_type="image/png", size=len(DATA), digest=DIGEST)
        with self.assertRaises(RuntimeError) as refused:
            self.run_image(delivery, image_input=PNG, write_refusal="forbidden",
                           admission=Row(outcome="no_host", reason="model:paint", operation=""))
        # storage STO-W12, inference INF-V4: an edit input is request input
        # under input:image, which needs content write and read together.
        for right in (oa_image.INPUT_RIGHTS["write"], oa_image.INPUT_RIGHTS["read"], "on input:image"):
            self.assertIn(right, str(refused.exception))
        self.assertIn("forbidden", str(refused.exception))
        self.assertEqual(oa_image.INPUT_RIGHTS["resource"], "input:image")

    def test_only_a_forbidden_upload_names_the_grant(self):
        delivery = Row(location="local", media_type="image/png", size=len(DATA), digest=DIGEST)
        for outcome in ("unavailable", "too_large"):
            with self.subTest(outcome=outcome):
                with self.assertRaises(RuntimeError) as refused:
                    self.run_image(delivery, image_input=PNG, write_refusal=outcome)
                self.assertIn(outcome, str(refused.exception))
                self.assertNotIn("input:image", str(refused.exception))

    def test_first_use_question_allow_then_upload_and_edit(self):
        # INF-V4: the refused upload asks nothing; the edit naming the digest is
        # refused for its read, or for a complete decision that reads
        # not_granted, and either asks the paired question. After the allow the
        # node uploads and starts the edit once more.
        for refusal in ("content:read:forbidden", "rights:not_granted",
                        "rights:not_granted content:read:forbidden output:forbidden"):
            with self.subTest(refusal=refusal):
                self._allow_then_edit(refusal)

    def _allow_then_edit(self, refusal):
        delivery = Row(location="local", media_type="image/png", size=len(DATA), digest=DIGEST)
        calls, state = [], {"allowed": False, "asked": 0}

        class Gate:
            def write(self, request, digest, content):
                calls.append(("write", request, digest, content, state["allowed"]))
                if not state["allowed"]:
                    raise Refused("forbidden")
                return Row(digest=digest, size=len(content), evidence="hashed")

        def start(request):
            if not state["allowed"]:
                state["asked"] += 1
                return Row(outcome="forbidden" if refusal.startswith("content") else "not_permitted", reason=refusal, operation="")
            return Row(outcome="accepted", operation="op")

        def answer(seconds):
            calls.append(("wait", seconds))
            state["allowed"] = True

        value, _ = self.run_image(delivery, image_input=PNG, mask=MASK, writer=Gate(), start=start, sleep=answer, calls=calls)
        self.assertEqual(value, (DATA, "image/png"))
        self.assertEqual(state["asked"], 1)
        steps = [call[0] if isinstance(call, tuple) else call for call in calls if call != ("writer", "local")]
        self.assertEqual(steps[:7], ["write", "write", "contract", "start", "wait", "write", "write"])
        self.assertEqual(steps[7], "start")
        starts = [call[1] for call in calls if isinstance(call, tuple) and call[0] == "start"]
        self.assertEqual(len(starts), 2)
        self.assertEqual(starts[0].image_digest, "sha256:" + hashlib.sha256(PNG).hexdigest())
        self.assertEqual(starts[0].mask_digest, "sha256:" + hashlib.sha256(MASK).hexdigest())

    def test_first_use_question_never_answered_allow_stops_with_the_grant(self):
        delivery = Row(location="local", media_type="image/png", size=len(DATA), digest=DIGEST)
        calls, waits = [], []
        with self.assertRaises(RuntimeError) as refused:
            self.run_image(delivery, image_input=PNG, write_refusal="forbidden", calls=calls,
                           admission=Row(outcome="forbidden", reason="content:read:forbidden", operation=""),
                           sleep=waits.append)
        self.assertIn("on input:image", str(refused.exception))
        self.assertEqual(len(waits), int(oa_image.FIRST_USE_WAIT_SECONDS / oa_image.FIRST_USE_POLL_SECONDS))
        self.assertEqual(len([c for c in calls if isinstance(c, tuple) and c[0] == "start"]), 1)

    def test_refused_input_read_names_the_read_grant(self):
        delivery = Row(location="local", media_type="image/png", size=len(DATA), digest=DIGEST)
        with self.assertRaises(RuntimeError) as refused:
            self.run_image(delivery, image_input=PNG,
                           admission=Row(outcome="forbidden", reason="content:read:forbidden", operation=""))
        for right in (oa_image.INPUT_RIGHTS["write"], oa_image.INPUT_RIGHTS["read"], "on input:image"):
            self.assertIn(right, str(refused.exception))
        with self.assertRaises(RuntimeError) as other:
            self.run_image(delivery, admission=Row(outcome="no_host", reason="model:paint", operation=""))
        self.assertNotIn(oa_image.INPUT_RIGHTS["read"], str(other.exception))

    def test_readme_names_the_input_grants(self):
        readme = Path(__file__).with_name("README.md").read_text(encoding="utf-8")
        edit = readme[readme.index("makes the call an `image@1` edit"):]
        edit = edit[:edit.index("A refusal names")]
        for right in (oa_image.INPUT_RIGHTS["write"], oa_image.INPUT_RIGHTS["read"], "`input:image`"):
            self.assertIn(right, edit)
        self.assertNotIn("digest:", edit)

    def test_reads_verified_bytes(self):
        delivery = Row(location="local", media_type="image/png", size=len(DATA), digest=DIGEST)
        value, calls = self.run_image(delivery)
        self.assertEqual(value, (DATA, "image/png"))
        self.assertEqual(calls[-3:], [("open", DIGEST), "copy", "close"])
        self.assertEqual(calls[1][1].mode, "generate")

    def test_saved_policy_uses_image_v2_and_no_inline_authority(self):
        delivery = Row(location="local", media_type="image/png", size=len(DATA), digest=DIGEST)
        value, calls = self.run_image(delivery, policy="paint_local")
        self.assertEqual(value, (DATA, "image/png"))
        self.assertEqual(calls[0], ("contract", "abstraction.inference/image@2"))
        request = calls[1][1]
        self.assertEqual(request.target.selection_policy, "paint_local")
        self.assertEqual(request.content.guarantees, [])
        self.assertNotIn("credential", vars(request.content))

    def test_exactly_one_target(self):
        with self.assertRaisesRegex(ValueError, "exactly one"):
            oa_image.generate("paint", "cat", "auto", "paint_local")

    def test_exposes_selected_route_for_shared_provider_acceptance(self):
        delivery = Row(location="local", media_type="image/png", size=len(DATA), digest=DIGEST)
        value, _ = self.run_image(delivery, with_route=True)
        self.assertEqual(value, (DATA, "image/png", "fixture", "paint", "op"))

    def test_refuses_remote_delivery(self):
        delivery = Row(location="remote", media_type="image/png", size=len(DATA), digest=DIGEST)
        with self.assertRaisesRegex(RuntimeError, "unsupported"):
            self.run_image(delivery)

    def test_verifies_digest(self):
        delivery = Row(location="local", media_type="image/png", size=len(DATA), digest=DIGEST)
        with self.assertRaisesRegex(RuntimeError, "digest mismatch"):
            self.run_image(delivery, corrupt=True)

    def test_rejects_unrepresentable_size_before_resolution(self):
        with self.assertRaisesRegex(ValueError, "supported"):
            oa_image.generate("paint", "cat", "1024;workflow")

    def test_the_node_reads_its_own_policy_name_and_no_setup_file(self):
        with patch.dict(os.environ, {"ABSTRACTION_COMFYUI_SELECTION_POLICY": ""}):
            self.assertEqual(oa_image.default_selection_policy(), "oa-image-default")
        with patch.dict(os.environ, {"ABSTRACTION_COMFYUI_SELECTION_POLICY": "isolated-test"}):
            self.assertEqual(oa_image.default_selection_policy(), "isolated-test")
        with patch.dict(os.environ, {"ABSTRACTION_COMFYUI_SELECTION_POLICY": "bad/name"}):
            with self.assertRaisesRegex(ValueError, "not a selection policy name"):
                oa_image.default_selection_policy()
        self.assertFalse(hasattr(oa_image, "RECIPE_CONFIG_RELATIVE"))

    def test_blank_workflow_target_uses_saved_policy_or_gives_setup_guidance(self):
        delivery = Row(location="local", media_type="image/png", size=len(DATA), digest=DIGEST)
        with patch.object(oa_image, "default_selection_policy", side_effect=ValueError("Choose an image model in OA setup.")):
            with self.assertRaisesRegex(ValueError, "Choose an image model in OA setup"):
                oa_image.generate("", "cat", "512x512", machine_factory=lambda: self.fail("must stop before IPC"))
        with patch.object(oa_image, "default_selection_policy", return_value="oa-image-default"):
            value, calls = self.run_image(delivery, empty_target=True)
        self.assertEqual(value, (DATA, "image/png"))
        self.assertEqual(calls[0], ("contract", "abstraction.inference/image@2"))

    def test_ready_workflow_targets_preview_and_save_without_model_alias(self):
        path = Path(__file__).with_name("workflows") / "oa_image_generate_preview_save.json"
        graph = json.loads(path.read_text(encoding="utf-8"))
        nodes = {node["id"]: node for node in graph["nodes"]}
        self.assertEqual({node["type"] for node in nodes.values()}, {"OAImageGenerate", "PreviewImage", "SaveImage"})
        self.assertEqual(nodes[1]["widgets_values"][0], "")
        self.assertEqual(nodes[1]["widgets_values"][2], "")
        self.assertEqual(nodes[1]["widgets_values"][3], "512x512")
        self.assertEqual({tuple(link[2:5]) for link in graph["links"]}, {(0, 2, 0), (0, 3, 0)})
        self.assertTrue(all(link[1] == 1 and link[5] == "IMAGE" for link in graph["links"]))

    def test_missing_preference_shares_every_model_then_starts_one_explicit_image(self):
        calls = []
        delivery = Row(location="local", media_type="image/png", size=len(DATA), digest=DIGEST)
        with patch.object(oa_image, "default_selection_policy", side_effect=oa_image.MissingImageSelection("missing")):
            value, _ = self.run_image(delivery, empty_target=True, calls=calls,
                                     offers=["first", "second", "third"], choice=Choice(calls))
        self.assertEqual(value, (DATA, "image/png"))
        shared = next(c for c in calls if c[0] == "share")
        self.assertEqual([(o.id, o.label) for o in shared[1].options],
                         [(name, name) for name in ["first", "second", "third"]])
        self.assertEqual(shared[2], ["first", "second", "third"])
        self.assertGreater(shared[1].pending_until_ms, 0)
        self.assertLessEqual(shared[1].pending_until_ms, shared[1].request_ref.replay_until_ms)
        self.assertEqual([c[1] for c in calls if c[0] == "contract"],
                         ["abstraction.asks/application@2", oa_image.IMAGE_CONTRACT])
        starts = [c[1] for c in calls if c[0] == "start"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(vars(starts[0]), dict(model="second", mode="generate", prompt="cat",
                                             size="512x512", count=1, guarantees=[]))
        self.assertEqual(calls[-1][0], "ack")

    def test_stale_policy_shares_model_and_keeps_content_for_one_explicit_start(self):
        for reason in ("selection:unknown", "selection:provider_stale"):
            with self.subTest(reason=reason):
                calls = []
                def start(request):
                    return (Row(outcome="no_host", reason=reason, operation="") if hasattr(request, "target")
                            else Row(outcome="accepted", operation="op"))
                delivery = Row(location="local", media_type="image/png", size=len(DATA), digest=DIGEST)
                value, _ = self.run_image(delivery, policy="stale", image_input=PNG, mask=MASK, calls=calls,
                                         offers=["first", "second"], start=start, choice=Choice(calls))
                self.assertEqual(value, (DATA, "image/png"))
                starts = [c[1] for c in calls if c[0] == "start"]
                self.assertEqual(len(starts), 2)
                self.assertEqual(starts[0].target.selection_policy, "stale")
                self.assertEqual(starts[1].model, "second")
                self.assertEqual({k: v for k, v in vars(starts[1]).items() if k != "model"}, vars(starts[0].content))
                self.assertEqual(len([c for c in calls if c[0] == "write"]), 2)
                self.assertEqual(len([c for c in calls if c[0] == "share"]), 1)
                offered = next(c[1] for c in calls if c[0] == "offer")
                self.assertEqual(vars(offered), dict(profile="image", placement="any", reserve=0, fresh=True))

    def test_choice_pending_observes_same_reference_and_acknowledges_after_delivery(self):
        calls = []
        pending = Row(outcome="pending", id="choice", answer=None)
        terminal = Row(outcome="answered", id="choice", answer=Row(id="choice", option="second",
            answered_ms=int(time.time() * 1000), answered_by="msix:Example.App_123456789abcd"))
        with patch.object(oa_image, "default_selection_policy", side_effect=oa_image.MissingImageSelection("missing")):
            self.run_image(Row(location="local", media_type="image/png", size=len(DATA), digest=DIGEST),
                           empty_target=True, calls=calls, offers=["second"],
                           choice=Choice(calls, [pending, terminal]), sleep=lambda _: None)
        shared = next(c for c in calls if c[0] == "share")
        observed = next(c for c in calls if c[0] == "choice_observe")
        self.assertIs(observed[1], shared[1].request_ref)
        self.assertEqual(observed[2], 1000)
        self.assertIs(calls[-1][1], shared[1].request_ref)
        self.assertIs(calls[-1][2], terminal.answer)
        self.assertEqual(calls[-2], "close")

    def test_choice_refused_dismissed_or_pending_never_starts_image(self):
        for outcome in ("forbidden", "unavailable", "invalid", "unknown", "gone", "conflict", "exhausted", "expired", "dismissed", "pending"):
            with self.subTest(outcome=outcome):
                calls = []
                observation = Row(outcome=outcome, id="choice" if outcome in {"dismissed", "pending"} else "", answer=None)
                if outcome == "dismissed":
                    observation.answer = Row(id="choice", option="", answered_ms=1, answered_by=str(Path(__file__).resolve()))
                with patch.object(oa_image, "default_selection_policy", side_effect=oa_image.MissingImageSelection("missing")), \
                     patch.object(oa_image.time, "monotonic", side_effect=[0, 91]):
                    with self.assertRaises(oa_image.MissingImageSelection):
                        self.run_image(Row(), empty_target=True, calls=calls, offers=["second"],
                                       choice=Choice(calls, [observation]))
                self.assertFalse(any(c[0] in {"start", "ack"} for c in calls))
                self.assertEqual(len([c for c in calls if c[0] == "share"]), 1)

    def test_invalid_or_unoffered_answer_never_starts_image(self):
        answer = dict(id="choice", option="second", answered_ms=int(time.time() * 1000), answered_by=str(Path(__file__).resolve()))
        for changed in ({"id": "other"}, {"option": "unoffered"}, {"answered_ms": 0}, {"answered_ms": True},
                        {"answered_by": "relative/assistant"}, {"answered_by": ""}, {"answered_by": "msix:bad"}):
            with self.subTest(changed=changed):
                calls = []
                observation = Row(outcome="answered", id="choice", answer=Row(**(answer | changed)))
                with patch.object(oa_image, "default_selection_policy", side_effect=oa_image.MissingImageSelection("missing")):
                    with self.assertRaisesRegex(RuntimeError, "inconsistent image model answer"):
                        self.run_image(Row(), empty_target=True, calls=calls, offers=["second"], choice=Choice(calls, [observation]))
                self.assertFalse(any(c[0] in {"start", "ack"} for c in calls))

    def test_answer_at_or_after_pending_deadline_never_starts_image(self):
        for offset in (0, 1):
            with self.subTest(offset=offset):
                calls = []
                class LateChoice(Choice):
                    def share_choice(self, request, options):
                        observation = super().share_choice(request, options)
                        observation.answer.answered_ms = request.pending_until_ms + offset
                        return observation
                with patch.object(oa_image, "default_selection_policy", side_effect=oa_image.MissingImageSelection("missing")):
                    with self.assertRaisesRegex(RuntimeError, "inconsistent image model answer"):
                        self.run_image(Row(location="local", media_type="image/png", size=len(DATA), digest=DIGEST),
                                       empty_target=True, calls=calls, offers=["second"], choice=LateChoice(calls))
                self.assertFalse(any(c[0] in {"start", "ack"} for c in calls))

    def test_observation_must_keep_admitted_choice_identity_and_shape(self):
        for observation in [Row(outcome="pending", id="", answer=None),
                            Row(outcome="answered", id="choice", answer=None),
                            Row(outcome="forbidden", id="choice", answer=None)]:
            with self.subTest(outcome=observation.outcome):
                calls = []
                with patch.object(oa_image, "default_selection_policy", side_effect=oa_image.MissingImageSelection("missing")):
                    with self.assertRaisesRegex(RuntimeError, "inconsistent image model choice"):
                        self.run_image(Row(), empty_target=True, calls=calls, offers=["second"], choice=Choice(calls, [observation]))
                self.assertFalse(any(c[0] == "start" for c in calls))
        calls = []
        pending = Row(outcome="pending", id="original", answer=None)
        answered = Row(outcome="answered", id="other", answer=Row(id="other", option="second", answered_ms=1,
                       answered_by=str(Path(__file__).resolve())))
        with patch.object(oa_image, "default_selection_policy", side_effect=oa_image.MissingImageSelection("missing")):
            with self.assertRaisesRegex(RuntimeError, "inconsistent image model choice"):
                self.run_image(Row(), empty_target=True, calls=calls, offers=["second"],
                               choice=Choice(calls, [pending, answered]), sleep=lambda _: None)
        self.assertFalse(any(c[0] == "start" for c in calls))

    def test_stale_selection_keeps_original_refusal_when_choice_cannot_supply_model(self):
        for outcome in ("forbidden", "dismissed", "pending"):
            with self.subTest(outcome=outcome):
                calls = []
                observation = Row(outcome=outcome, id="choice" if outcome != "forbidden" else "", answer=None)
                if outcome == "dismissed":
                    observation.answer = Row(id="choice", option="", answered_ms=1, answered_by=str(Path(__file__).resolve()))
                with patch.object(oa_image.time, "monotonic", side_effect=[0, 91]):
                    with self.assertRaisesRegex(RuntimeError, "admission no_host: selection:unknown"):
                        self.run_image(Row(), policy="stale", calls=calls, offers=["second"],
                                       choice=Choice(calls, [observation]),
                                       admission=Row(outcome="no_host", reason="selection:unknown", operation=""))
                self.assertEqual(len([c for c in calls if c[0] == "start"]), 1)
                self.assertFalse(any(c[0] == "ack" for c in calls))

    def test_no_unsafe_truncated_or_refused_model_inventory_is_shared(self):
        for names, outcome in [([], "offered"), (["a", "b", "c", "d"], "offered"), (["bad\nname"], "offered"),
                               (["x" * 65], "offered"), (["second"], "not_permitted"), (["second"], "unavailable")]:
            with self.subTest(names=names, outcome=outcome):
                calls = []
                with patch.object(oa_image, "default_selection_policy", side_effect=oa_image.MissingImageSelection("missing")):
                    with self.assertRaises(oa_image.MissingImageSelection):
                        self.run_image(Row(), empty_target=True, calls=calls, offers=names, offer_outcome=outcome)
                self.assertFalse(any(c[0] in {"start", "share", "ack"} for c in calls))

    def test_uncertain_share_or_selected_image_start_never_creates_another_intent(self):
        calls = []
        class UncertainChoice(Choice):
            def share_choice(self, request, options):
                self.calls.append(("share", request, options))
                raise OSError("unknown_method or uncertain sharing")
        with patch.object(oa_image, "default_selection_policy", side_effect=oa_image.MissingImageSelection("missing")):
            with self.assertRaisesRegex(OSError, "uncertain sharing"):
                self.run_image(Row(), empty_target=True, calls=calls, offers=["second"], choice=UncertainChoice(calls))
        self.assertEqual(len([c for c in calls if c[0] == "share"]), 1)
        self.assertFalse(any(c[0] == "start" for c in calls))
        calls = []
        def uncertain(_):
            raise OSError("uncertain explicit admission")
        with patch.object(oa_image, "default_selection_policy", side_effect=oa_image.MissingImageSelection("missing")):
            with self.assertRaisesRegex(OSError, "uncertain explicit"):
                self.run_image(Row(), empty_target=True, calls=calls, offers=["second"], choice=Choice(calls), start=uncertain)
        self.assertEqual(len([c for c in calls if c[0] == "start"]), 1)
        self.assertEqual(len([c for c in calls if c[0] == "share"]), 1)
        self.assertFalse(any(c[0] == "ack" for c in calls))

    def test_other_typed_refusals_never_offer_substitution(self):
        for outcome, reason, operation in [("not_permitted", "rights:not_granted", ""), ("budget_exceeded", "budget", ""),
                                          ("invalid", "selection:unknown", ""), ("unavailable", "selection:unknown", ""),
                                          ("no_host", "selection:invalid", ""), ("no_host", "selection:unknown", "accepted-id")]:
            with self.subTest(outcome=outcome, reason=reason, operation=operation):
                calls = []
                with self.assertRaises(RuntimeError):
                    self.run_image(Row(), policy="stale", calls=calls,
                                   admission=Row(outcome=outcome, reason=reason, operation=operation))
                self.assertFalse(any(c[0] in {"choices", "router"} for c in calls))

    def test_explicit_selection_has_no_extra_question_and_uncertain_start_is_not_retried(self):
        calls = []
        self.run_image(Row(location="local", media_type="image/png", size=len(DATA), digest=DIGEST), calls=calls)
        self.assertFalse(any(c[0] in {"choices", "router"} for c in calls))
        calls = []
        def fail(_):
            raise OSError("uncertain admission")
        with self.assertRaisesRegex(OSError, "uncertain"):
            self.run_image(Row(), policy="stale", start=fail, calls=calls)
        self.assertEqual(len([c for c in calls if c[0] == "start"]), 1)
        self.assertFalse(any(c[0] in {"choices", "router"} for c in calls))

    def test_missing_preference_does_not_mask_invalid_input_or_unreadable_config(self):
        with patch.object(oa_image, "default_selection_policy", side_effect=oa_image.MissingImageSelection("missing")):
            with self.assertRaisesRegex(ValueError, "exactly one"):
                oa_image.generate("", "", "512x512", machine_factory=lambda: self.fail("invalid input before IPC"))
        for error in (ValueError("malformed"), ValueError("unreadable")):
            with patch.object(oa_image, "default_selection_policy", side_effect=error):
                with self.assertRaisesRegex(ValueError, str(error)):
                    oa_image.generate("", "cat", "512x512", machine_factory=lambda: self.fail("config error before IPC"))

    def test_accepted_operation_failure_never_starts_a_choice(self):
        calls = []
        class FailureImage(Image):
            def observe(self, *_):
                return Row(outcome="page", next=1, at_end=True, deltas=[Row(kind="end", image_end=Row(
                    outcome="unavailable", reason="selection:provider_stale", deliveries=[]))])
        with patch.object(sys.modules[__name__], "Image", FailureImage):
            with self.assertRaisesRegex(RuntimeError, "image result unavailable"):
                self.run_image(Row(), policy="stale", calls=calls)
        self.assertEqual(len([c for c in calls if c[0] == "start"]), 1)
        self.assertFalse(any(c[0] in {"choices", "router"} for c in calls))
        calls = []
        with patch.object(sys.modules[__name__], "Image", FailureImage), \
             patch.object(oa_image, "default_selection_policy", side_effect=oa_image.MissingImageSelection("missing")):
            with self.assertRaisesRegex(RuntimeError, "image result unavailable"):
                self.run_image(Row(), empty_target=True, calls=calls, offers=["second"], choice=Choice(calls))
        self.assertEqual(len([c for c in calls if c[0] == "start"]), 1)
        self.assertEqual(len([c for c in calls if c[0] == "share"]), 1)
        self.assertFalse(any(c[0] == "ack" for c in calls))


class ImageServiceTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("OA_COMFYUI_SELECTION_FRAME_TEST"), "configured Go service fixture")
    def test_real_selection_refusal_then_separate_explicit_run(self):
        """Generated Python/Go frames for two deliberate node runs; no native transport or engine."""
        import base64

        class Transport:
            def exchange_frame(self, frame):
                print(base64.b64encode(frame).decode("ascii"), flush=True)
                return base64.b64decode(sys.stdin.readline())

        transport, calls = Transport(), []
        class Machine:
            def resolve_service(self, contract, *, scope):
                calls.append(("contract", contract))
                return transport

            def resolve_storage(self, *, scope):
                return Reader(calls)

            def resolve_router(self, *, scope):
                # This Image-only Go dispatcher supplies no Router capability.
                # Preserve the refusal and exercise the separate explicit run.
                raise oa_image.MissingImageSelection("fixture model inventory unavailable; enter a model in the model field")

        with self.assertRaisesRegex(RuntimeError, "admission no_host: selection:unknown.*model field"):
            oa_image.generate("", "cat", "512x512", "stale", machine_factory=Machine, with_route=True)
        self.assertEqual([c[1] for c in calls if c[0] == "contract"], [oa_image.IMAGE_POLICY_CONTRACT])
        # The person now enters a model in the existing node field and runs
        # the node again. This separate call owns the explicit image intent.
        result = oa_image.generate("second", "cat", "512x512", machine_factory=Machine, with_route=True)
        self.assertEqual(result, (DATA, "image/png", "fixture", "second", "fixture-image"))
        self.assertEqual([c[1] for c in calls if c[0] == "contract"], [oa_image.IMAGE_POLICY_CONTRACT, oa_image.IMAGE_CONTRACT])


class SelectedRecipeTests(unittest.TestCase):
    def test_effective_app_policy_keeps_fallbacks_and_refusals(self):
        facade = types.ModuleType("abstraction.facade")
        facade.Scope = Row(LOCAL="local")
        client = types.ModuleType("abstraction.facade.client")
        client.Machine = object
        api = types.ModuleType("abstraction.inference.api")
        calls = []
        policy = Row(name="oa-image-default", candidates=[Row(model="first"), Row(model="fallback")])
        operator = Row(read_effective=lambda name: calls.append(name) or read)
        api.SelectionOperatorV2Client = lambda binding: operator
        machine = Row(resolve_service=lambda contract, **kwargs: calls.append((contract, kwargs)) or object())
        modules = {"abstraction.facade": facade, "abstraction.facade.client": client,
                   "abstraction.inference.api": api}
        with patch.dict(sys.modules, modules), patch.dict(os.environ, {"ABSTRACTION_COMFYUI_SELECTION_POLICY": ""}):
            read = Row(outcome="found", policy=policy)
            self.assertEqual(oa_image.selected_image_policy(machine_factory=lambda: machine), "oa-image-default")
            self.assertEqual([c.model for c in policy.candidates], ["first", "fallback"])
            self.assertEqual(calls[0], ("abstraction.inference/selection-operator@2", {"scope": "local"}))
            self.assertEqual(calls[1], "oa-image-default")
            for outcome in ("unknown", "forbidden", "unavailable", "invalid"):
                read = Row(outcome=outcome, policy=None)
                with self.assertRaisesRegex(oa_image.MissingImageSelection, outcome):
                    oa_image.selected_image_policy(machine_factory=lambda: machine)

    def test_recipe_commits_the_selected_policy_name(self):
        package = types.ModuleType("oa_recipe_test_package")
        package.__path__ = [str(MODULE_PATH.parent)]
        fake_aiohttp = types.ModuleType("aiohttp")
        fake_aiohttp.web = Row()
        spec = importlib.util.spec_from_file_location("oa_recipe_test_package.klein_recipe", MODULE_PATH.with_name("klein_recipe.py"))
        recipe = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"oa_recipe_test_package": package,
                                      "oa_recipe_test_package.oa_image": oa_image,
                                      "aiohttp": fake_aiohttp}):
            spec.loader.exec_module(recipe)
            with patch.object(oa_image, "selected_image_policy", return_value="chosen-policy"):
                graph = json.loads(recipe._image_graph_bytes())
            image = next(n for n in graph["nodes"] if n["type"] == "OAImageGenerate")
            self.assertEqual(image["widgets_values"][2], "chosen-policy")
            self.assertEqual(graph["extra"]["oa_recipe"]["selection_policy"], "chosen-policy")
            with patch.object(oa_image, "selected_image_policy", side_effect=oa_image.MissingImageSelection("refused")):
                with self.assertRaisesRegex(oa_image.MissingImageSelection, "refused"):
                    recipe._image_graph_bytes()


class Permission:
    """abstraction.asks/permission@1 as the node observes a first-use question."""

    def __init__(self, calls, observations):
        self.calls, self.observations = calls, iter(observations)

    def observe_question(self, id, wait_ms):
        self.calls.append(("observe_question", id, wait_ms))
        return Row(observation=next(self.observations), pending_until_ms=0)


class FirstUseImageTests(unittest.TestCase):
    """ComfyUI's first picture asks OA's image permission; the node waits for the answer."""

    run_image = ImageTests.run_image
    DELIVERY = Row(location="local", media_type="image/png", size=len(DATA), digest=DIGEST)
    REFUSAL = Row(outcome="not_permitted", reason="rights:not_granted rights:question_pending:q1", operation="")

    def first_use(self, observations):
        calls, starts = [], []
        def start(request):
            starts.append(request)
            return self.REFUSAL if len(starts) == 1 else Row(outcome="accepted", operation="op")
        result = self.run_image(self.DELIVERY, policy="oa-image-default", start=start, calls=calls,
                                permission=Permission(calls, observations))
        return result, calls, starts

    def test_allow_answer_starts_the_picture_once_more(self):
        pending = Row(outcome="pending", answer=Row(id="q1", pending=True, option=""), policy_revision="")
        allowed = Row(outcome="answered", answer=Row(id="q1", pending=False, option="allow"), policy_revision="")
        (value, _), calls, starts = self.first_use([pending, allowed])
        self.assertEqual(value, (DATA, "image/png"))
        self.assertEqual(len(starts), 2)
        self.assertEqual([c[:2] for c in calls if c[0] == "observe_question"], [("observe_question", "q1")] * 2)
        self.assertIn(("contract", "abstraction.asks/permission@1"), calls)

    def test_refuse_or_never_stops_with_a_sentence_and_no_second_start(self):
        for option, words in (("refuse", "chose not to allow"), ("never", "Permissions page")):
            with self.subTest(option=option):
                answer = Row(outcome="answered", answer=Row(id="q1", pending=False, option=option), policy_revision="")
                with self.assertRaisesRegex(RuntimeError, words):
                    self.first_use([answer])

    def test_a_refusal_without_a_question_is_not_awaited(self):
        calls = []
        with self.assertRaisesRegex(RuntimeError, "rights:not_granted"):
            self.run_image(self.DELIVERY, policy="oa-image-default", calls=calls,
                           admission=Row(outcome="not_permitted", reason="rights:not_granted", operation=""))
        self.assertNotIn(("contract", "abstraction.asks/permission@1"), calls)


class NodeRouteTests(unittest.TestCase):
    def test_the_node_reports_its_oa_route_in_comfy_history(self):
        import io
        from PIL import Image as Picture
        png = io.BytesIO()
        Picture.new("RGB", (4, 2), (255, 0, 0)).save(png, format="PNG")
        with patch.object(oa_image, "generate", return_value=(png.getvalue(), "image/png", "pytorch", "z-image", "op-1")) as made:
            out = oa_image.OAImageGenerate().generate("", "a fox", "512x512", "")
        self.assertEqual(made.call_args.args, ("", "a fox", "512x512", ""))
        route = json.loads(out["ui"]["oa_route"][0])
        self.assertEqual(route, {"host": "pytorch", "model": "z-image", "operation": "op-1"})
        image, text = out["result"]
        self.assertEqual(tuple(image.shape), (1, 2, 4, 3))
        self.assertEqual(json.loads(text), route)


if __name__ == "__main__":
    unittest.main()
