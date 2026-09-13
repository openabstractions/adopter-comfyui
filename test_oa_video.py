"""Boundary tests for the OA video node; no runtime, provider or ComfyUI is started.

The job document, acceptance and router types are OA's generated Python codecs,
found installed or beside this tree in the OA source repository. The runtime,
content store and ComfyUI are fakes. Nothing here needs ComfyUI's server module
or aiohttp: progress text reaches a stub PromptServer.
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import time
import types
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
for _layer in ("abstraction-inference", "abstraction-job", "abstraction-router"):
    _path = HERE.parent / _layer / "py"
    if _path.is_dir() and str(_path) not in sys.path:
        sys.path.append(str(_path))
try:
    import abstraction.inference.job as jobwire
    import abstraction.job.acceptance as acceptance
    import abstraction.router as router
    HAVE_CODECS = True
except ImportError:
    HAVE_CODECS = False
try:
    import numpy  # noqa: F401
    import PIL  # noqa: F401
    import torch
    HAVE_TENSORS = True
except ImportError:
    HAVE_TENSORS = False


def load(name):
    spec = importlib.util.spec_from_file_location("oa_video_test_" + name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


oa_video = load("oa_video")
oa_content = load("oa_content")

# The byte fixtures abstraction-inference/go/job_media_test.go sniffs.
TEST_MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
TEST_WEBM = b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\xf7\x81\x01\x42\x82\x84webm"
TEST_AVI = b"RIFF\x24\x00\x00\x00AVI LIST\x00\x00\x00\x00hdrlavih"
TEST_PNG = b"\x89PNG\r\n\x1a\n1234"


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


class Row:
    def __init__(self, **values):
        self.__dict__.update(values)


class Jobs:
    """A job service that walks one submission through scripted snapshots."""

    def __init__(self, log, snapshots, result=None, admission="accepted"):
        self.log, self.snapshots, self.result, self.admission = log, list(snapshots), result, admission
        self.identity = None
        # Each cancel takes the next answer: an outcome word or an exception to raise.
        self.cancels = []

    def get_history_window(self):
        return acceptance.HistoryWindow(logical_owner="owner", history_epoch="epoch-1",
                                        minimum_retention_ms=1000, result_retention_ms=1000)

    def _receipt(self, identity):
        return acceptance.Receipt(identity=identity, logical_owner="owner", operation_id="op-7",
                                  accepted_guarantees=[oa_video.RECOVERABLE_UPSTREAM],
                                  history_retention_ms=1000)

    def submit(self, submission):
        self.log.append(("submit", submission))
        self.identity = submission.identity
        if self.admission != "accepted":
            return acceptance.AcceptanceResult(outcome=self.admission, reason=getattr(self, "reason", "host:none"))
        return acceptance.AcceptanceResult(outcome="accepted", receipt=self._receipt(submission.identity))

    def reconcile(self, identity):
        self.log.append("reconcile")
        return acceptance.AcceptanceResult(outcome="accepted", receipt=self._receipt(identity))

    def observe_work(self, identity):
        self.log.append("observe")
        state, done, total, waiting, failure = self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]
        return acceptance.ObservationResult(outcome="observed", snapshot=acceptance.OperationSnapshot(
            receipt=self._receipt(identity), state=state,
            progress=acceptance.WorkProgress(done=done, total=total), waiting=waiting, failure=failure))

    def read_result(self, identity, offset, max_bytes):
        data = self.result[offset:offset + max_bytes]
        return acceptance.ResultRead(outcome="data", chunk=acceptance.ResultChunk(
            receipt=self._receipt(identity), offset=offset, total=len(self.result), data=data,
            eof=offset + len(data) == len(self.result)))

    def cancel_work(self, identity):
        self.log.append(("cancel", identity))
        answer = self.cancels.pop(0) if self.cancels else "requested"
        if isinstance(answer, Exception):
            raise answer
        return acceptance.CancellationResult(outcome=answer)


class Reader:
    def __init__(self, log, data, corrupt=False):
        self.log, self.data, self.corrupt = log, data, corrupt

    def open(self, value):
        self.log.append(("open", value))
        return Row(outcome="opened", resource=Row(handle="h", digest=value, size=len(self.data)))

    def copy(self, _, destination):
        self.log.append("copy")
        destination.write(self.data[:-1] + b"X" if self.corrupt else self.data)

    def close(self, _):
        self.log.append("close")


class Writer:
    def __init__(self, log):
        self.log = log

    def write(self, request, naming_digest, content):
        self.log.append(("write", request, naming_digest, content))
        return Row(digest=naming_digest, size=len(content), evidence="hashed")


class Router:
    def __init__(self, log, offers, outcome="offered"):
        self.log, self.offers, self.outcome = log, offers, outcome

    def offer(self, request):
        self.log.append(("offer", request))
        if isinstance(self.offers, Exception):
            raise self.offers
        return router.OfferSnapshot(outcome=self.outcome, observation=None, offers=self.offers)


def result_document(media_type, data, profile="video"):
    return jobwire.encode(jobwire.Document(result=jobwire.JobResult(
        profile=profile, host="sdcpp-video", model="fastwan-2.2-ti2v-5b",
        deliveries=[jobwire.Delivery(digest=digest(data), media_type=media_type, size=len(data),
                                     location="local")])))


RUNNING = "running"


class Fixture:
    def fixture(self, snapshots=None, media_type="video/webm", data=TEST_WEBM, delivered=None,
                corrupt=False, offers=(), admission="accepted", offer_outcome="offered"):
        log = []
        snapshots = snapshots or [("pending", 0, 0, "queue:2", None), (RUNNING, 1, 3, "", None),
                                  (RUNNING, 3, 3, "", None), ("complete", 1, 1, "", None)]
        jobs = Jobs(log, snapshots, result_document(media_type, data), admission)
        reader = Reader(log, data if delivered is None else delivered, corrupt)
        writer = Writer(log)
        route = Router(log, offers, offer_outcome)
        deadlines = self.deadlines = []

        class Machine:
            def __init__(self, deadline):
                deadlines.append(deadline)

            def resolve_jobs(self, *, guarantees=(), scope):
                log.append(("resolve_jobs", tuple(guarantees), scope))
                return jobs

            def resolve_storage(self, *, scope):
                return reader

            def resolve_storage_writer(self, *, scope):
                return writer

            def resolve_router(self, *, scope):
                return route

        facade = types.ModuleType("abstraction.facade")
        facade.Scope = Row(LOCAL="local")
        self.patcher = patch.dict(sys.modules, {"abstraction.facade": facade})
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        return Machine, log, jobs

    def run_video(self, machine, **kwargs):
        progress = []
        kwargs.setdefault("progress", lambda done, total, waiting: progress.append((done, total, waiting)))
        value = oa_video.generate(kwargs.pop("model", "fastwan-2.2-ti2v-5b"), "a red fox runs", "832x480",
                                  kwargs.pop("duration_ms", 2000), machine_factory=machine,
                                  sleep=kwargs.pop("sleep", lambda _: None), **kwargs)
        return value, progress

    def submitted(self, log):
        submission = next(entry[1] for entry in log if isinstance(entry, tuple) and entry[0] == "submit")
        return submission, jobwire.decode(submission.spec).request


@unittest.skipUnless(HAVE_CODECS, "OA's generated Python codecs are not importable")
class VideoJobTests(Fixture, unittest.TestCase):
    def test_submits_a_video_job_drives_progress_and_reads_the_verified_delivery(self):
        machine, log, _ = self.fixture()
        (data, media_type, route), progress = self.run_video(machine)
        self.assertEqual((data, media_type), (TEST_WEBM, "video/webm"))
        self.assertEqual(route, {"host": "sdcpp-video", "model": "fastwan-2.2-ti2v-5b", "operation": "op-7",
                                 "digest": digest(TEST_WEBM), "media_type": "video/webm"})
        submission, request = self.submitted(log)
        self.assertEqual(submission.kind, "inference")
        self.assertIn(oa_video.RECOVERABLE_UPSTREAM, submission.required_guarantees)
        self.assertEqual(submission.identity.history_epoch, "epoch-1")
        self.assertEqual((request.profile, request.duration_ms), ("video", 2000))
        self.assertEqual((request.image.mode, request.image.count, request.image.size, request.image.model),
                         ("generate", 1, "832x480", "fastwan-2.2-ti2v-5b"))
        self.assertEqual((request.image.image_digest, request.image.image_media_type), ("", ""))
        self.assertEqual(progress, [(0, 0, "queue:2"), (1, 3, ""), (3, 3, ""), (1, 1, "")])
        self.assertEqual(log[-3:], [("open", digest(TEST_WEBM)), "copy", "close"])
        self.assertFalse([entry for entry in log if isinstance(entry, tuple) and entry[0] == "cancel"])
        self.assertIn(("resolve_jobs", (oa_video.RECOVERABLE_UPSTREAM,), "local"), log)

    def test_the_recoverable_guarantee_is_the_one_the_job_contract_names(self):
        self.assertEqual([oa_video.RECOVERABLE_UPSTREAM], jobwire.EXECUTION_GUARANTEES)
        self.assertEqual(oa_video.JOB_KIND, jobwire.KINDS[0])

    def test_a_first_frame_is_stored_and_sent_as_the_edit_image(self):
        machine, log, _ = self.fixture()
        self.run_video(machine, first_frame_png=TEST_PNG)
        write = next(entry for entry in log if isinstance(entry, tuple) and entry[0] == "write")
        self.assertEqual(write[2:], (digest(TEST_PNG), TEST_PNG))
        self.assertLess(log.index(write), next(i for i, e in enumerate(log) if isinstance(e, tuple) and e[0] == "submit"))
        _, request = self.submitted(log)
        self.assertEqual((request.image.mode, request.image.image_digest, request.image.image_media_type),
                         ("edit", digest(TEST_PNG), "image/png"))

    def test_an_interrupted_run_cancels_the_job(self):
        machine, log, jobs = self.fixture(snapshots=[(RUNNING, 1, 3, "", None)])
        calls = []

        def interrupted():
            calls.append(1)
            if len(calls) == 2:
                raise KeyboardInterrupt("interrupted")

        with self.assertRaises(KeyboardInterrupt):
            self.run_video(machine, interrupted=interrupted)
        self.assertEqual(log[-1], ("cancel", jobs.identity))

    def test_app_observation_or_progress_disconnect_keeps_the_video_job(self):
        for failure in ("observation", "progress", "shutdown"):
            with self.subTest(failure=failure):
                machine, log, jobs = self.fixture(snapshots=[(RUNNING, 1, 3, "", None)])
                def disconnected(*args):
                    raise ConnectionError("app disconnected")
                options = {}
                if failure == "observation":
                    jobs.observe_work = disconnected
                elif failure == "progress":
                    options["progress"] = disconnected
                else:
                    def shutdown(*args):
                        raise KeyboardInterrupt("app stopped")
                    options["sleep"] = shutdown
                with self.assertRaises((RuntimeError, ConnectionError, KeyboardInterrupt)):
                    self.run_video(machine, **options)
                self.assertFalse([entry for entry in log if isinstance(entry, tuple) and entry[0] == "cancel"])

    def interrupt_with_cancels(self, answers):
        machine, log, jobs = self.fixture(snapshots=[(RUNNING, 1, 3, "", None)])
        jobs.cancels = list(answers)

        def interrupted():
            if "observe" in log:
                raise KeyboardInterrupt("interrupted")

        expect_warning = len(answers) >= oa_video.CANCEL_ATTEMPTS
        with (self.assertLogs("abstraction", "WARNING") if expect_warning else contextlib.nullcontext()) as logged:
            with self.assertRaises(KeyboardInterrupt):
                self.run_video(machine, interrupted=interrupted)
        return [entry for entry in log if isinstance(entry, tuple) and entry[0] == "cancel"], logged

    def test_a_cancel_that_raises_is_sent_again(self):
        before = time.monotonic()
        cancels, _ = self.interrupt_with_cancels([ConnectionError("pipe closed")])
        self.assertEqual(len(cancels), 2)
        # The last two machines are the two cancel attempts, each with its own 5 s budget.
        for deadline in self.deadlines[-2:]:
            self.assertLessEqual(deadline - before, oa_video.CANCEL_DEADLINE_SECONDS + 1)
        self.assertEqual(oa_video.CANCEL_DEADLINE_SECONDS, 5.0)

    def test_a_job_that_already_ended_is_left_quietly(self):
        with self.assertNoLogs("abstraction", "WARNING"):
            cancels, _ = self.interrupt_with_cancels(["already_terminal"])
        self.assertEqual(len(cancels), 1)

    def test_a_cancel_that_never_lands_is_logged_with_the_operation(self):
        cancels, logged = self.interrupt_with_cancels([ConnectionError("pipe closed"), "unavailable", "unavailable"])
        self.assertEqual(len(cancels), oa_video.CANCEL_ATTEMPTS)
        self.assertIn("op-7", logged.output[0])
        self.assertIn("unavailable", logged.output[0])

    def test_a_failed_job_is_reported_and_not_cancelled(self):
        failure = acceptance.WorkFailure(classification="permanent", message="native:invalid:content:unknown",
                                         cause="refused")
        machine, log, _ = self.fixture(snapshots=[("failed", 0, 0, "", failure)])
        with self.assertRaisesRegex(RuntimeError, "native:invalid:content:unknown"):
            self.run_video(machine)
        self.assertFalse([entry for entry in log if isinstance(entry, tuple) and entry[0] == "cancel"])

    def test_a_refused_admission_names_the_reason(self):
        machine, log, _ = self.fixture(admission="forbidden")
        with self.assertRaisesRegex(RuntimeError, "forbidden: host:none"):
            self.run_video(machine)
        self.assertFalse([entry for entry in log if isinstance(entry, tuple) and entry[0] == "cancel"])

    def test_a_refused_first_frame_names_the_input_image_grant(self):
        # storage STO-W12, inference INF-V4: a first frame is request input under input:image.
        machine, log, _ = self.fixture()

        class Refused(Exception):
            outcome = "forbidden"

        def refuse(*_):
            raise Refused("storage: begin forbidden")
        machine(0).resolve_storage_writer(scope="local").write = refuse
        with self.assertRaisesRegex(RuntimeError, r"first frame \(forbidden\): this app needs "
                                    r"abstraction\.storage/content\.write and abstraction\.storage/content\.read on input:image"):
            self.run_video(machine, first_frame_png=TEST_PNG)
        self.assertFalse([entry for entry in log if isinstance(entry, tuple) and entry[0] == "submit"])

    def test_a_refused_first_frame_read_names_the_input_image_grant(self):
        machine, _, jobs = self.fixture(admission="forbidden")
        jobs.reason = "content:read:forbidden"
        with self.assertRaisesRegex(RuntimeError, r"content:read:forbidden; this app needs .* on input:image"):
            self.run_video(machine, first_frame_png=TEST_PNG)
        self.assertEqual(oa_video.INPUT_RIGHTS["resource"], "input:image")

    def test_a_refused_clip_names_the_output_video_grant(self):
        # inference INF-S7, INF-V4: the clip is written under output:video.
        machine, _, jobs = self.fixture(admission="forbidden")
        jobs.reason = "output:forbidden"
        with self.assertRaisesRegex(RuntimeError, r"output:forbidden; this app needs "
                                    r"abstraction\.storage/content\.write and abstraction\.storage/content\.read on output:video"):
            self.run_video(machine, first_frame_png=TEST_PNG)
        self.assertEqual(oa_video.OUTPUT_RIGHTS["resource"], "output:video")

    def test_a_combined_refusal_names_every_grant_it_lists(self):
        # inference INF-V4: one refusal names each missing grant, separated by spaces.
        machine, _, jobs = self.fixture(admission="forbidden")
        jobs.reason = "rights:not_granted content:read:forbidden output:forbidden"
        with self.assertRaises(RuntimeError) as refused:
            self.run_video(machine, first_frame_png=TEST_PNG)
        message = str(refused.exception)
        self.assertIn("rights:not_granted content:read:forbidden output:forbidden", message)
        self.assertIn("on input:image", message)
        self.assertIn("on output:video", message)

    def test_a_declared_type_the_bytes_do_not_sniff_as_is_refused(self):
        machine, _, _ = self.fixture(media_type="video/webm", data=TEST_MP4)
        with self.assertRaisesRegex(RuntimeError, "sniff"):
            self.run_video(machine)

    def test_mp4_and_avi_deliveries_are_accepted(self):
        for media_type, data in (("video/mp4", TEST_MP4), ("video/avi", TEST_AVI)):
            with self.subTest(media_type=media_type):
                machine, _, _ = self.fixture(media_type=media_type, data=data)
                (value, declared, _), _ = self.run_video(machine)
                self.assertEqual((value, declared), (data, media_type))

    def test_an_image_delivery_is_refused(self):
        machine, _, _ = self.fixture(media_type="image/png", data=TEST_PNG)
        with self.assertRaisesRegex(RuntimeError, "unsupported"):
            self.run_video(machine)

    def test_the_shared_reader_verifies_the_digest(self):
        machine, _, _ = self.fixture(corrupt=True)
        with self.assertRaisesRegex(RuntimeError, "digest mismatch"):
            self.run_video(machine)

    def test_auto_takes_the_first_name_of_the_first_offer(self):
        offers = [router.Offer(family="wan", names=["fastwan-2.2-ti2v-5b", "fastwan"], servers=["sdcpp-video"]),
                  router.Offer(family="ltx", names=["ltx-2.3"], servers=["sdcpp-video"])]
        machine, log, _ = self.fixture(offers=offers)
        self.assertEqual(oa_video.offered_models(machine_factory=machine, cache=False),
                         ["fastwan-2.2-ti2v-5b", "ltx-2.3"])
        request = next(entry[1] for entry in log if isinstance(entry, tuple) and entry[0] == "offer")
        self.assertEqual((request.profile, request.placement, request.reserve), (oa_video.VIDEO_PROFILE, "any", 0))
        self.run_video(machine, model="auto")
        _, sent = self.submitted(log)
        self.assertEqual(sent.image.model, "fastwan-2.2-ti2v-5b")

    def test_auto_reports_router_failure_before_submitting(self):
        for error in (RuntimeError("forbidden"), ConnectionError("pipe closed")):
            with self.subTest(error=error):
                machine, log, _ = self.fixture(offers=error)
                self.assertEqual(oa_video.offered_models(machine_factory=machine, cache=False), [])
                with self.assertRaisesRegex(RuntimeError, r"video model discovery failed") as refused:
                    self.run_video(machine, model="auto", first_frame_png=TEST_PNG)
                self.assertIn(str(error), str(refused.exception))
                self.assertNotIn("install a video model", str(refused.exception))
                self.assertFalse([entry for entry in log if isinstance(entry, tuple) and entry[0] in ("resolve_jobs", "submit", "write", "open", "cancel")])

    def test_typed_router_refusals_return_no_names_and_perform_no_work(self):
        leaked = "refused-secret-model"
        offers = [router.Offer(family="secret", names=[leaked], servers=["secret-server"])]
        for outcome in (router.OfferOutcome.NOT_PERMITTED, router.OfferOutcome.FORBIDDEN,
                        router.OfferOutcome.UNAVAILABLE, router.OfferOutcome.INVALID, "", "future"):
            for payload in ([], offers):
                with self.subTest(outcome=outcome, payload=bool(payload)):
                    machine, log, _ = self.fixture(offers=payload, offer_outcome=outcome)
                    self.assertEqual(oa_video.offered_models(machine_factory=machine, cache=False), [])
                    with self.assertRaisesRegex(RuntimeError, r"video model discovery failed") as refused:
                        self.run_video(machine, model="auto", first_frame_png=TEST_PNG)
                    message = str(refused.exception)
                    self.assertIn("router offer %s" % outcome, message)
                    self.assertNotIn(leaked, message)
                    self.assertNotIn("install a video model", message)
                    self.assertNotIn("grant", message)
                    self.assertNotIn("Panel", message)
                    self.assertFalse([entry for entry in log if isinstance(entry, tuple) and entry[0] in ("resolve_jobs", "submit", "write", "open", "cancel")])

    def test_auto_successful_empty_offer_names_model_installation(self):
        machine, log, _ = self.fixture(offers=[])
        self.assertEqual(oa_video.offered_models(machine_factory=machine, cache=False), [])
        with self.assertRaisesRegex(RuntimeError, r"offers no video model to ComfyUI; install a video model"):
            self.run_video(machine, model="auto", first_frame_png=TEST_PNG)
        self.assertEqual(oa_video._offer_error, "")
        self.assertFalse([entry for entry in log if isinstance(entry, tuple) and entry[0] in ("resolve_jobs", "submit", "write", "open", "cancel")])

    def test_overlapping_empty_discovery_keeps_this_generations_refusal(self):
        refused_machine, log, _ = self.fixture(offer_outcome=router.OfferOutcome.NOT_PERMITTED)
        empty_machine, _, _ = self.fixture(offers=[])
        # Interleave another complete discovery after this call returns its
        # result, before generate decides which advice to give.
        helper_name = "_discover_models" if hasattr(oa_video, "_discover_models") else "offered_models"
        original = getattr(oa_video, helper_name)

        def interleave(**kwargs):
            result = original(**kwargs)
            if kwargs.get("machine_factory") is refused_machine:
                original(machine_factory=empty_machine, cache=False)
                self.assertEqual(oa_video._offer_error, "")
            return result

        with patch.object(oa_video, helper_name, interleave):
            with self.assertRaisesRegex(RuntimeError, r"discovery failed.*not_permitted") as refused:
                self.run_video(refused_machine, model="auto", first_frame_png=TEST_PNG)
        self.assertNotIn("install a video model", str(refused.exception))
        self.assertFalse([entry for entry in log if isinstance(entry, tuple) and entry[0] in ("resolve_jobs", "submit", "write", "open", "cancel")])


class RequestBoundTests(unittest.TestCase):
    def refuse(self, **changes):
        values = dict(model="m", prompt="p", size="832x480", duration_ms=2000)
        values.update(changes)
        with self.assertRaises(ValueError):
            oa_video.generate(values["model"], values["prompt"], values["size"], values["duration_ms"],
                              machine_factory=lambda _: self.fail("must refuse before IPC"))

    def test_bounds_are_checked_before_any_call(self):
        self.refuse(size="1024x1024")
        self.refuse(size="832x480;workflow")
        self.refuse(duration_ms=0)
        self.refuse(duration_ms=oa_video.MAX_SECONDS * 1000 + 1)
        self.refuse(prompt="")
        self.refuse(model="")

    def test_the_media_rule_matches_the_runtime_fixtures(self):
        self.assertEqual([oa_content.video_media(d) for d in (TEST_MP4, TEST_WEBM, TEST_AVI, TEST_PNG)],
                         ["video/mp4", "video/webm", "video/avi", ""])

    def test_one_owner_reads_and_verifies_delivered_content(self):
        for name in ("oa_image.py", "oa_video.py"):
            with self.subTest(module=name):
                source = (HERE / name).read_text(encoding="utf-8")
                self.assertNotIn("digest mismatch", source)
                self.assertNotIn("resolve_storage(", source)
                self.assertIn("_content.read_delivery(", source)


@unittest.skipUnless(HAVE_CODECS and HAVE_TENSORS, "OA codecs, numpy, PIL and torch are required")
class NodeTests(Fixture, unittest.TestCase):
    def comfy(self, log):
        class VideoFromFile:
            def __init__(self, source):
                self.source = source

            def __getattr__(self, name):
                # The node hands the clip on; decoding it is GetVideoComponents' work.
                log.append(("video_call", name))
                raise AssertionError("the node called VIDEO.%s" % name)

        class ProgressBar:
            def __init__(self, total, node_id=None):
                log.append(("bar", total, node_id))

            def update_absolute(self, value, total=None, preview=None):
                log.append(("bar_value", value, total))

        latest = types.ModuleType("comfy_api.latest")
        latest.InputImpl = Row(VideoFromFile=VideoFromFile)
        utils = types.ModuleType("comfy.utils")
        utils.ProgressBar = ProgressBar
        comfy = types.ModuleType("comfy")
        comfy.utils = utils
        server = types.ModuleType("server")
        server.PromptServer = Row(instance=Row(send_progress_text=lambda text, node: log.append(("text", text, node))))
        return {"comfy_api": types.ModuleType("comfy_api"), "comfy_api.latest": latest,
                "comfy": comfy, "comfy.utils": utils, "server": server}

    def test_node_returns_video_and_route_and_drives_the_progress_bar(self):
        machine, log, _ = self.fixture()
        oa_video.configure(machine)
        self.addCleanup(oa_video.configure, None)
        with patch.dict(sys.modules, self.comfy(log)), patch.object(oa_video, "POLL_SECONDS", 0):
            outputs = oa_video.OAVideoGenerate().generate(
                "fastwan-2.2-ti2v-5b", "a red fox runs", "832x480", 2, unique_id="5")
        self.assertEqual(oa_video.OAVideoGenerate.RETURN_TYPES, ("VIDEO", "STRING"))
        video, route = outputs
        self.assertIsInstance(video.source, io.BytesIO)
        self.assertEqual(video.source.getvalue(), TEST_WEBM)
        self.assertEqual(json.loads(route)["operation"], "op-7")
        self.assertEqual([entry for entry in log if entry[0] == "video_call"], [])
        self.assertIn(("text", "Queued: position 2", "5"), log)
        self.assertIn(("bar_value", 3, 3), log)
        self.assertEqual([entry for entry in log if entry[0] == "bar_value"][-1], ("bar_value", 1, 1))

    def test_generate_never_decodes_the_clip(self):
        source = (HERE / "oa_video.py").read_text(encoding="utf-8")
        for name in ("get_components", "get_frame_count", "get_dimensions"):
            self.assertNotIn(name, source)

    def test_node_encodes_one_first_frame_as_png(self):
        machine, log, _ = self.fixture()
        oa_video.configure(machine)
        self.addCleanup(oa_video.configure, None)
        frame = torch.zeros(1, 4, 6, 3)
        frame[0, :, :, 0] = 1.0
        with patch.dict(sys.modules, self.comfy(log)), patch.object(oa_video, "POLL_SECONDS", 0):
            oa_video.OAVideoGenerate().generate("fastwan-2.2-ti2v-5b", "fox", "832x480", 2, first_frame=frame)
        write = next(entry for entry in log if isinstance(entry, tuple) and entry[0] == "write")
        from PIL import Image
        with Image.open(io.BytesIO(write[3])) as image:
            self.assertEqual((image.format, image.size, image.getpixel((0, 0))), ("PNG", (6, 4), (255, 0, 0)))
        with self.assertRaisesRegex(ValueError, "one image"):
            oa_video.OAVideoGenerate().generate("m", "fox", "832x480", 2, first_frame=torch.zeros(2, 4, 6, 3))

    def test_input_types_offer_auto_and_the_sizes(self):
        machine, _, _ = self.fixture(offers=[router.Offer(family="wan", names=["fastwan"], servers=["s"])])
        oa_video.configure(machine)
        self.addCleanup(oa_video.configure, None)
        oa_video._offer_cache = None
        before = time.monotonic()
        required = oa_video.OAVideoGenerate.INPUT_TYPES()["required"]
        self.assertEqual(required["model"][0], ["auto", "fastwan"])
        self.assertEqual(len(self.deadlines), 1)
        self.assertLessEqual(self.deadlines[0] - before, oa_video.OFFER_BUDGET_SECONDS + 0.5)
        self.assertEqual(oa_video.OFFER_BUDGET_SECONDS, 1.5)
        self.assertEqual(required["size"][0], ["832x480", "480x832", "1280x704", "704x1280"])
        self.assertEqual(oa_video.OAVideoGenerate.INPUT_TYPES()["optional"]["first_frame"], ("IMAGE",))


class WorkflowTests(unittest.TestCase):
    def test_the_video_workflow_connects_the_node_to_save_video(self):
        graph = json.loads((HERE / "workflows" / "oa_video_generate_save.json").read_text(encoding="utf-8"))
        nodes = {node["id"]: node for node in graph["nodes"]}
        self.assertEqual({node["type"] for node in nodes.values()}, {"OAVideoGenerate", "SaveVideo"})
        source = next(node for node in nodes.values() if node["type"] == "OAVideoGenerate")
        self.assertEqual(source["widgets_values"][0], "auto")
        self.assertIn(source["widgets_values"][2], oa_video.SIZES)
        self.assertEqual([link[5] for link in graph["links"]], ["VIDEO"])
        self.assertEqual(graph["links"][0][1:3], [source["id"], 0])

    def test_the_package_registers_the_video_node(self):
        text = (HERE / "__init__.py").read_text(encoding="utf-8")
        self.assertIn('"OAVideoGenerate": OAVideoGenerate', text)


if __name__ == "__main__":
    unittest.main()
