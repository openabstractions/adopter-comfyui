"""Every OpenAbstractions call this package makes leaves through oa_route.

With the "OpenAbstractions for ComfyUI" extension's relay registered, each
Machine, Jobs.restore_installed and Library gets the relay's library; without
it, the SDK's own. Runs without ComfyUI or a runtime.
"""

import ast
import importlib
import itertools
import sys
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
FLAT = HERE.parent
SDK = [FLAT / "abstraction-identity" / "py", FLAT / "abstraction-facade" / "py", FLAT / "abstraction-job" / "py",
       FLAT / "abstraction-download" / "py"]
_COUNT = itertools.count()


class FakeRelay(types.ModuleType):
    def __init__(self):
        super().__init__("openabstractions_relay")
        self.relay_library = types.SimpleNamespace(cancellation=mock.Mock(
            side_effect=lambda: types.SimpleNamespace(signal=mock.Mock(), close=mock.Mock())))

    def library(self):
        return self.relay_library

    def program(self):
        return "C:\\ComfyUI\\custom_nodes\\comfyui-openabstractions\\helper\\python.exe"


class Recorder:
    made = []

    def __init__(self, *args, **options):
        Recorder.made.append(options)

    def resolve_applications(self, **_):
        return self


class Route(unittest.TestCase):
    def setUp(self):
        for path in SDK:
            sys.path.insert(0, str(path))
        self.package = "adopter_route_test_%d" % next(_COUNT)
        module = types.ModuleType(self.package)
        module.__path__ = [str(HERE)]
        sys.modules[self.package] = module
        self.relay = FakeRelay()
        sys.modules["openabstractions_relay"] = self.relay
        import abstraction.facade.client as client
        self.client, self.machine = client, client.Machine
        client.Machine = Recorder
        Recorder.made = []

    def tearDown(self):
        self.client.Machine = self.machine
        sys.modules.pop("openabstractions_relay", None)
        for path in SDK:
            sys.path.remove(str(path))

    def load(self, name):
        return importlib.import_module(self.package + "." + name)

    def test_the_route_follows_the_registered_relay(self):
        route = self.load("oa_route")
        self.assertIs(route.library(), self.relay.relay_library)
        self.assertTrue(route.program().endswith("helper\\python.exe"))
        sys.modules.pop("openabstractions_relay")
        self.assertIsNone(route.library())
        self.assertIsNone(route.program())

    def test_startup_pair_binds_service_logging_and_restore_before_install(self):
        route = self.load("oa_route")
        service, logging = self.load("service"), self.load("oa_logging")
        import abstraction.facade.jobs as jobs
        expected = object()
        with mock.patch.object(self.client, "select_development", return_value=("endpoint", expected)) as select, \
             mock.patch.object(jobs.Jobs, "restore", return_value="restored") as restore:
            self.assertTrue(route.configure_startup({
                "ABSTRACTION_COMFYUI_DEVELOPMENT_PROGRAM": "runtime.exe",
                "ABSTRACTION_COMFYUI_DEVELOPMENT_ENDPOINT": "endpoint"}))
            select.assert_called_once_with("runtime.exe", "endpoint")
            service.machine(123)
            logging._machine_factory(deadline=124)
            self.assertEqual(service._restore({"endpoint": "jobs-endpoint", "owner": "owner"}, ("keyed",), 125), "restored")
            for options in Recorder.made:
                self.assertIs(options["server"], expected)
                self.assertIs(options["library"], self.relay.relay_library)
            self.assertEqual([row["deadline"] for row in Recorder.made], [123, 124])
            self.assertIs(restore.call_args.kwargs["library"], self.relay.relay_library)
            restore.assert_called_once_with("jobs-endpoint", "owner", server=expected,
                                            library=self.relay.relay_library,
                                            required_guarantees=("keyed",), deadline=125)

    def test_startup_enabled_consumers_share_selection_and_real_relay_cancel(self):
        route = self.load("oa_route")
        card, inventory = self.load("oa_card"), self.load("oa_inventory")
        diagnostics, observer = self.load("oa_diagnostics"), self.load("oa_download_observer")
        import abstraction.ipc as ipc
        import abstraction.facade as wire
        expected = object()
        lease = types.SimpleNamespace(outcome="acquired", lease=types.SimpleNamespace(id="lease"), asked=[])
        resource = types.SimpleNamespace(acquire=mock.Mock(return_value=lease))
        page = types.SimpleNamespace(outcome="page", snapshots=[], complete=True)
        work = types.SimpleNamespace(list_work=mock.Mock(return_value=page))
        jobs = types.SimpleNamespace(observe_work=mock.Mock(return_value=types.SimpleNamespace(outcome="absent")))
        resolver = types.SimpleNamespace(resolve=mock.Mock(return_value=types.SimpleNamespace(status="unavailable")))
        with mock.patch.object(self.client, "select_development", return_value=("resolver-endpoint", expected)), \
             mock.patch.object(ipc, "FrameTransport") as transport, \
             mock.patch.object(wire, "ResolverClient", return_value=resolver), \
             mock.patch.object(Recorder, "resolve_resource_leases", create=True, return_value=resource), \
             mock.patch.object(Recorder, "resolve_job_inventory", create=True, return_value=work), \
             mock.patch.object(Recorder, "resolve_jobs", create=True, return_value=jobs):
            route.configure_startup({"ABSTRACTION_COMFYUI_DEVELOPMENT_PROGRAM": "runtime",
                                     "ABSTRACTION_COMFYUI_DEVELOPMENT_ENDPOINT": "resolver-endpoint"})
            token = card._new_cancellation()
            self.relay.relay_library.cancellation.assert_called_once_with()
            self.assertNotIsInstance(token, card._LocalCancellation)
            card._register_wait(token)
            try:
                self.assertEqual(card._acquire(16, "tiny-model", token), "lease")
                card._cancel_pending_waits()
                token.signal.assert_called_once_with()
            finally:
                card._unregister_wait(token)
                token.close()
            self.assertTrue(inventory.list_operations()["complete"])
            self.assertEqual(observer.observe_download({"identity":{"key":"k", "history_epoch":"e", "attempt":1},
                                                       "owner":"owner", "wait_ms":0}), {"outcome":"absent"})
            report, error = diagnostics.observe()
            self.assertIsNone(error)
            self.assertEqual(resolver.resolve.call_count, len(report.capabilities))
            self.assertEqual(transport.call_args.args, (self.relay.relay_library, "resolver-endpoint"))
            self.assertIs(transport.call_args.kwargs["server"], expected)
            self.assertGreater(transport.call_args.kwargs["deadline"], 0)
            self.assertEqual(len(Recorder.made), 3)
            self.assertIs(Recorder.made[0]["cancellation"], token)
            for options in Recorder.made:
                self.assertIs(options["server"], expected)
                self.assertIs(options["library"], self.relay.relay_library)
                self.assertGreater(options["deadline"], 0)

    def test_startup_absent_pair_preserves_installed_and_partial_pair_refuses(self):
        route = self.load("oa_route")
        self.assertFalse(route.configure_startup({}))
        self.assertIsNone(self.load("service")._configuration)
        for name in ("oa_logging", "oa_card", "oa_inventory", "oa_download_observer"):
            self.assertIsNone(self.load(name)._machine_factory)
        self.assertIsNone(self.load("oa_card")._cancellation_factory)
        self.assertIsNone(self.load("oa_diagnostics")._resolver_factory)
        for env in ({"ABSTRACTION_COMFYUI_DEVELOPMENT_ENDPOINT": "endpoint"},
                    {"ABSTRACTION_COMFYUI_DEVELOPMENT_PROGRAM": "runtime"},
                    {"ABSTRACTION_COMFYUI_DEVELOPMENT_PROGRAM": "", "ABSTRACTION_COMFYUI_DEVELOPMENT_ENDPOINT": "endpoint"}):
            with self.subTest(env=env), self.assertRaises(ValueError):
                route.configure_startup(env)
        with self.assertRaises(ValueError):
            route.configure_startup({"ABSTRACTION_COMFYUI_DEVELOPMENT_PROGRAM": "relative-runtime.exe",
                                     "ABSTRACTION_COMFYUI_DEVELOPMENT_ENDPOINT": "endpoint"})
        sys.modules.pop("openabstractions_relay")
        with self.assertRaises(ValueError):
            route.configure_startup({"ABSTRACTION_COMFYUI_DEVELOPMENT_PROGRAM": "runtime",
                                     "ABSTRACTION_COMFYUI_DEVELOPMENT_ENDPOINT": "endpoint"})

    def test_startup_configuration_precedes_first_installers(self):
        tree = ast.parse((HERE / "__init__.py").read_text(encoding="utf-8"))
        calls = [(n.lineno, ast.unparse(n.func)) for n in ast.walk(tree)
                 if isinstance(n, ast.Call)]
        configured = next(line for line, name in calls if name == "oa_route.configure_startup")
        self.assertLess(configured, next(line for line, name in calls if name == "oa_logging.install"))
        self.assertLess(configured, max(line for line, name in calls if name == "_install"))

    def test_each_machine_gets_the_relay_library(self):
        self.load("service")._machine(0.0)
        self.load("oa_inventory")._default_machine(0.0)
        self.load("oa_download_observer")._default_machine(0.0)
        self.load("oa_video")._default_machine(0.0)
        self.load("oa_card")._default_machine(0.0)
        self.load("oa_application")._facade_applications()
        self.assertEqual(len(Recorder.made), 6)
        for options in Recorder.made:
            self.assertIs(options["library"], self.relay.relay_library)

    def test_a_restored_job_binding_gets_the_relay_library(self):
        service = self.load("service")
        import abstraction.facade.jobs as jobs
        restored = []
        original = jobs.Jobs.restore_installed
        jobs.Jobs.restore_installed = classmethod(lambda cls, *args, **options: restored.append(options))
        try:
            service._restore({"endpoint": "e", "owner": "o"}, (), 0.0)
        finally:
            jobs.Jobs.restore_installed = original
        self.assertIs(restored[0]["library"], self.relay.relay_library)

    def test_the_rights_subject_is_the_helper(self):
        self.assertTrue(self.load("service").rights_program().endswith("helper\\python.exe"))

    def test_card_and_download_guidance_name_the_consuming_relay_helper(self):
        service, card = self.load("service"), self.load("oa_card")
        helper = self.relay.program()
        with mock.patch.object(sys, "executable", "standalone-python.exe"), \
             mock.patch.object(sys, "_base_executable", "base-python.exe", create=True):
            for guidance in (card._grant_sentence(), service.grant_command(), service.refusal_detail("forbidden")):
                self.assertIn(helper, guidance)
                self.assertNotIn("standalone-python.exe", guidance)
                self.assertNotIn("base-python.exe", guidance)

    def test_configured_unavailable_relay_never_suggests_a_standalone_grant(self):
        service, card = self.load("service"), self.load("oa_card")
        for missing in (mock.Mock(return_value=None), mock.Mock(return_value=""),
                        mock.Mock(return_value=17), mock.Mock(side_effect=RuntimeError("private relay failure"))):
            with self.subTest(missing=missing), mock.patch.object(self.relay, "program", missing):
                self.assertIsNone(service.rights_program())
                self.assertIsNone(service.grant_command())
                for guidance in (card._grant_sentence(), service.refusal_detail("forbidden")):
                    self.assertIn("caller program is unavailable", guidance)
                    self.assertNotIn("--program", guidance)
                    self.assertNotIn("private relay failure", guidance)
        self.assertEqual(service.refusal_detail("unavailable"), "")
        with mock.patch.dict(sys.modules, {"openabstractions_relay": None}):
            self.assertIsNone(service.rights_program())
            self.assertNotIn("--program", card._grant_sentence())

    def test_direct_guidance_without_a_relay_has_no_proven_program(self):
        service, card = self.load("service"), self.load("oa_card")
        sys.modules.pop("openabstractions_relay")
        with tempfile.TemporaryDirectory(prefix="ComfyRights1616-") as folder:
            base, executable = Path(folder) / "base-python.exe", Path(folder) / "app-python.exe"
            base.write_bytes(b"fixture base image")
            executable.write_bytes(b"fixture app image")
            for base_hint in (str(base), str(executable), str(base) + ".missing", None):
                with self.subTest(base=base_hint), \
                     mock.patch.object(sys, "_base_executable", base_hint, create=True), \
                     mock.patch.object(sys, "executable", str(executable)):
                    self.assertIsNone(service.rights_program())
                    self.assertIsNone(service.grant_command())
                    for guidance in (card._grant_sentence(), service.refusal_detail("forbidden")):
                        self.assertIn("caller program is unavailable", guidance)
                        self.assertNotIn("--program", guidance)
                        self.assertNotIn(str(base), guidance)
                        self.assertNotIn(str(executable), guidance)
        self.assertEqual(service.refusal_detail("unavailable"), "")

    def test_standalone_card_import_and_off_path_do_not_resolve_a_peer(self):
        name = "standalone_card_guidance_%d" % next(_COUNT)
        spec = importlib.util.spec_from_file_location(name, HERE / "oa_card.py")
        card = importlib.util.module_from_spec(spec)
        with mock.patch.object(self.relay, "program", side_effect=AssertionError("off path resolved relay")):
            spec.loader.exec_module(card)
            self.assertIsNone(card.acquire_for("unopened-model"))
        self.assertIsNone(card.oa_route)
        self.assertIn("caller program is unavailable", card._grant_sentence())
        sys.modules.pop("openabstractions_relay")
        with mock.patch.object(sys, "_base_executable", None, create=True), \
             mock.patch.object(sys, "executable", "standalone-python.exe"):
            self.assertIn("caller program is unavailable", card._grant_sentence())
            self.assertNotIn("--program", card._grant_sentence())

    def test_no_call_site_bypasses_the_route(self):
        """Every Machine(...) and Library() outside oa_route names a library."""
        for path in sorted(HERE.glob("*.py")):
            if path.name.startswith("test_") or path.name == "oa_route.py":
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                with self.subTest(path=path.name, line=node.lineno):
                    if name == "Machine":
                        self.assertIn("library", {k.arg for k in node.keywords})
                    if name == "Library":
                        source = ast.get_source_segment(path.read_text(encoding="utf-8"), node)
                        self.assertTrue(node.args or node.keywords or self._guarded(path, node.lineno), source)

    @staticmethod
    def _guarded(path, line):
        """A bare Library() reached only when no relay is registered."""
        text = path.read_text(encoding="utf-8").splitlines()
        return any("oa_route" in previous for previous in text[max(0, line - 4):line])


if __name__ == "__main__":
    unittest.main()
