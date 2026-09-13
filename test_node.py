"""Does the rebinding actually reach the call sites?

The integration is a monkey-patch, and a monkey-patch that misses is worse than
none: everything looks installed, the log says so, and the bytes still go
through the old path. So this builds stand-in ComfyUI-Manager trees with the
module layout and import style of the real ones and checks that a call made the
way manager_server makes it lands in ours.

Two layouts are pinned (PINS). ComfyUI core at the pinned commit installs the
pip package comfyui_manager 4.2.2 through manager_requirements.txt and imports
its server in comfyui_manager.start() before custom nodes load. The 3.41
custom-node Manager lives at custom_nodes/ComfyUI-Manager/glob. The stand-ins
are written here, since Manager is GPL-3.0; UpstreamPinTest compares every
import line and call site they reproduce with the pinned upstream checkouts
when OA_COMFYUI_FORKS names the directory holding ComfyUI, ComfyUI-Manager and
ComfyUI-Manager-v4.

OA_COMFYUI_NODE selects a different node directory to test, for example an
older revision as a negative control.
"""

import hashlib
import http.server
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from types import SimpleNamespace
from unittest import mock

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_NODE = HERE if os.path.isfile(os.path.join(HERE, "__init__.py")) else os.path.join(REPO, "openabstractions-flat", "adopter-comfyui")
NODE = os.environ.get("OA_COMFYUI_NODE") or DEFAULT_NODE
LAYERS = os.path.dirname(DEFAULT_NODE)

PINS = {
    "core": dict(checkout="ComfyUI", commit="a2afcdb89910f7ada92e2a6e04c8b5c9126122f1",
                 requirement="comfyui_manager==4.2.2"),
    "v4": dict(checkout="ComfyUI-Manager-v4", branch="manager-v4",
               commit="bd4ede2237c97b3a71569a0301a0be9db226c7e9", version="4.2.2"),
    "v3": dict(checkout="ComfyUI-Manager", branch="main",
               commit="97f123ef4417305e4ec3d51f125a0abbb4ede352", version="3.41"),
}

DOWNLOADER = '''
calls = []

def basic_download_url(url, dest_folder, filename):
    calls.append(("basic", url, dest_folder, filename))
    return "original-basic"

def download_url(model_url, model_dir, filename):
    calls.append((model_url, model_dir, filename))
    return "original"

def download_url_with_agent(url, save_path):
    calls.append(("agent", url, save_path))
    return "original-agent"

def download_repo_in_bytes(repo_id, local_dir):
    calls.append(("repo", repo_id, local_dir))
'''

# do_install_model picks by URL exactly as Manager does: github, huggingface and
# heibox go one way and everything else -- civitai included -- goes to the agent
# with a whole path instead of a directory and a name.
#
# install_model_message is what the USER is told. Manager reads the agent
# downloader's return value as a boolean and turns a falsy one into an error
# message, so a function that downloads perfectly and returns None reports a
# failure for a model that arrived.
MODEL_INSTALL = '''
def do_install_model(url, model_dir, filename):
    if any(h in url for h in ("github.com", "huggingface.co", "heibox")):
        return download_url(url, model_dir, filename=filename)
    return download_url_with_agent(url, os.path.join(model_dir, filename))

def install_model_message(url, model_dir, filename):
    res = do_install_model(url, model_dir, filename)
    if any(h in url for h in ("github.com", "huggingface.co", "heibox")):
        res = True  # download_url raises rather than returning falsy
    if res:
        return "success"
    return "Model installation error: " + url

# A "<huggingface>" model entry: the url is a repository id, delivered through
# the downloader module attribute.
def install_snapshot(model_url, model_path):
    manager_downloader.download_repo_in_bytes(repo_id=model_url, local_dir=model_path)
    return "success"
'''

# cnr_install: a registry node arrives as a zip under an unpredictable name in
# custom_nodes, is extracted and deleted. The module-attribute call is the
# upstream call form.
CNR_INSTALL = '''
def cnr_install(zip_url, custom_nodes_path):
    archive_name = f"CNR_temp_{str(uuid.uuid4())}.zip"
    manager_downloader.download_url(zip_url, custom_nodes_path, archive_name)
    return archive_name
'''

# 4.2.2: comfyui_manager/glob/manager_server.py binds the functions into its OWN
# namespace, so patching the downloader module alone would leave this call site
# calling the original. legacy/manager_server.py uses the one-line form.
V4_SERVER = '''
import os
from . import manager_core as core
from ..common import manager_downloader

from comfyui_manager.common.manager_downloader import (
    download_url,
    download_url_with_agent,
)
''' + MODEL_INSTALL
V4_LEGACY_SERVER = '''
import os
from . import manager_core as core
from ..common import manager_downloader
from comfyui_manager.common.manager_downloader import download_url, download_url_with_agent
''' + MODEL_INSTALL
V4_CORE = '''
import uuid
from ..common import manager_downloader
''' + CNR_INSTALL

# 3.41: glob/manager_server.py L198, plus copy_install, which calls the same
# bound download_url for .py files into custom_nodes and .js into web/extensions.
V3_SERVER = '''
import os
import manager_core as core
import manager_downloader
from manager_downloader import download_url, download_url_with_agent
core.js_path = os.path.join(core.comfy_path, "web", "extensions")
''' + MODEL_INSTALL + '''
def copy_install(files, js_path_name=None):
    for url in files:
        filename = os.path.basename(url)
        if url.endswith(".py"):
            download_url(url, core.get_default_custom_nodes_path(), filename)
        else:
            path = os.path.join(core.js_path, js_path_name) if js_path_name is not None else core.js_path
            os.makedirs(path, exist_ok=True)
            download_url(url, path, filename)
    return True
'''
V3_CORE = '''
import os
import uuid
import manager_downloader
comfy_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
js_path = None
def get_default_custom_nodes_path():
    return os.path.join(comfy_path, "custom_nodes")
''' + CNR_INSTALL


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def build_manager(root, layouts, enable_manager=None, legacy_ui=False, v3_dir="ComfyUI-Manager"):
    """Build a ComfyUI root with the requested Manager layouts.

    layouts: a subset of {"v4", "v3"}. enable_manager: None writes no comfy
    core module; True or False writes comfy/cli_args.py with those flags.
    Returns (custom_nodes, site). Put site on sys.path before importing.
    """
    custom_nodes = os.path.join(root, "custom_nodes")
    site = os.path.join(root, "site-packages")
    os.makedirs(custom_nodes, exist_ok=True)
    os.makedirs(site, exist_ok=True)
    if enable_manager is not None:
        _write(os.path.join(site, "comfy", "__init__.py"), "")
        _write(os.path.join(site, "comfy", "cli_args.py"),
               "import types\nargs = types.SimpleNamespace(enable_manager=%r, enable_manager_legacy_ui=%r)\n"
               % (bool(enable_manager), bool(legacy_ui)))
    if "v4" in layouts:
        package = os.path.join(site, "comfyui_manager")
        _write(os.path.join(package, "__init__.py"), "# stand-in for comfyui_manager %s\n" % PINS["v4"]["version"])
        for sub in ("common", "glob", "legacy"):
            _write(os.path.join(package, sub, "__init__.py"), "")
        _write(os.path.join(package, "common", "manager_downloader.py"), DOWNLOADER)
        _write(os.path.join(package, "glob", "manager_server.py"), V4_SERVER)
        _write(os.path.join(package, "glob", "manager_core.py"), V4_CORE)
        _write(os.path.join(package, "legacy", "manager_server.py"), V4_LEGACY_SERVER)
        _write(os.path.join(package, "legacy", "manager_core.py"), V4_CORE)
        # What pip leaves behind; importlib.metadata reads the version from it.
        _write(os.path.join(site, "comfyui_manager-%s.dist-info" % PINS["v4"]["version"], "METADATA"),
               "Metadata-Version: 2.1\nName: comfyui-manager\nVersion: %s\n" % PINS["v4"]["version"])
    if "v3" in layouts:
        manager = os.path.join(custom_nodes, v3_dir)
        _write(os.path.join(manager, "pyproject.toml"),
               '[project]\nname = "comfyui-manager"\nversion = "%s"\n' % PINS["v3"]["version"])
        _write(os.path.join(manager, "glob", "manager_downloader.py"), DOWNLOADER)
        _write(os.path.join(manager, "glob", "manager_server.py"), V3_SERVER)
        _write(os.path.join(manager, "glob", "manager_core.py"), V3_CORE)
    return custom_nodes, site


def start_manager(layout, legacy_ui=False):
    """What ComfyUI does before custom nodes load: comfyui_manager.start()
    imports the selected server (4.x). A 3.x custom node imports nothing yet."""
    import importlib
    if layout == "v4":
        return importlib.import_module("comfyui_manager.%s.manager_server" % ("legacy" if legacy_ui else "glob"))
    return None


def import_node_with_passing_preflight(machine_factory=None, restore_factory=None):
    """Import abstraction_downloads fresh with service.configure() already in
    effect for the node's own import-time preflight (C1 SERVICE.md "Service
    path": preflight, then activation).

    Every existing test below configures its OWN fake job/model/storage
    clients through service.configure() AFTER importing the node, to drive a
    download the way ComfyUI-Manager triggers it. That import used to always
    rebind Manager's call sites, because service mode had no import-time
    runtime check at all. Now it does, and this process has no real installed
    OA runtime, so an unconfigured import correctly leaves Manager untouched
    (ActivationTest and PreflightTest cover that directly). Defaulting every
    OTHER test's import to a trivially passing preflight keeps their own,
    later service.configure() call the only thing that matters to them, same
    as before C1 existed.
    """
    import importlib
    import importlib.util
    # service.preflight()'s _resolve() imports abstraction.facade for Scope.LOCAL,
    # even with a configured fake machine (fixed alongside the C1 live run,
    # research/adoption/comfyui/results/live-2026-09-22.md).
    path = os.path.join(LAYERS, "abstraction-facade", "py")
    if os.path.isdir(path) and path not in sys.path:
        sys.path.insert(0, path)
    if machine_factory is None:
        machine_factory = lambda **waiting: SimpleNamespace(resolve_jobs=lambda **kw: SimpleNamespace())
    if restore_factory is None:
        restore_factory = lambda endpoint, owner, **waiting: None
    spec = importlib.util.find_spec("abstraction_downloads")
    if spec is None:
        raise ModuleNotFoundError("abstraction_downloads")
    module = importlib.util.module_from_spec(spec)
    sys.modules["abstraction_downloads"] = module
    service = importlib.import_module("abstraction_downloads.service")
    service.configure(machine_factory, restore_factory)
    spec.loader.exec_module(module)
    return module


def load_node(layout, legacy_ui=False):
    """Start Manager as core does, import the node, return
    (node, downloader, server, core)."""
    import importlib
    start_manager(layout, legacy_ui)
    node = import_node_with_passing_preflight()
    if layout == "v4":
        prefix = "comfyui_manager.%s." % ("legacy" if legacy_ui else "glob")
        downloader = importlib.import_module("comfyui_manager.common.manager_downloader")
        server = importlib.import_module(prefix + "manager_server")
        core = importlib.import_module(prefix + "manager_core")
    else:
        downloader = importlib.import_module("manager_downloader")
        server = importlib.import_module("manager_server")
        core = importlib.import_module("manager_core")
    return node, downloader, server, core


class Serve(http.server.BaseHTTPRequestHandler):
    body = b""
    # Set to cut the first response short, the way a dropped connection or a
    # closed application does. Cleared afterwards so the retry can succeed.
    die_after = 0
    # Set to refuse outright. 401 is what Hugging Face answers for a gated
    # repository -- the first real failure this node met in a live ComfyUI.
    status = 0
    ranges_seen = []

    def _refuse(self) -> bool:
        if not Serve.status:
            return False
        self.send_response(Serve.status)
        self.send_header("Content-Length", "0")
        self.end_headers()
        return True

    def do_HEAD(self):
        if self._refuse():
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.body)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):
        if self._refuse():
            return
        rng = self.headers.get("Range", "")
        Serve.ranges_seen.append(rng)
        start = 0
        if rng.startswith("bytes="):
            start = int(rng[len("bytes="):].split("-")[0])

        body = self.body[start:]
        if Serve.die_after:
            # Announce the full length and then send less: the transfer looks
            # healthy right up to the moment it stops, which is what a dropped
            # connection actually looks like.
            self.send_response(206 if start else 200)
            if start:
                self.send_header(
                    "Content-Range", f"bytes {start}-{len(self.body) - 1}/{len(self.body)}"
                )
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(body[: Serve.die_after])
            self.wfile.flush()
            self.close_connection = True
            return

        self.send_response(206 if start else 200)
        if start:
            self.send_header(
                "Content-Range", f"bytes {start}-{len(self.body) - 1}/{len(self.body)}"
            )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class ManagerCase(unittest.TestCase):
    """A temporary ComfyUI root with the node copied into custom_nodes."""

    layout = None       # "v4", "v3" or None
    enable_manager = None
    legacy_ui = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self._env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._env)))
        # The Hub's paths-info lookup reaches the network; HubDigestTest
        # turns it on against its own loopback Hub.
        os.environ["ABSTRACTION_COMFYUI_HF_DIGEST"] = "off"
        os.environ.pop("HF_ENDPOINT", None)
        self._modules = dict(sys.modules)
        self._path = list(sys.path)
        self._meta_path = list(sys.meta_path)
        self.addCleanup(self._restore_imports)

        # Use one current source tree for the SDK and its generated constants.
        # A prepared interpreter may carry an older installed OA package set.
        for layer in sorted(os.listdir(LAYERS)):
            path = os.path.join(LAYERS, layer, "py")
            if os.path.isdir(os.path.join(path, "abstraction")):
                sys.path.insert(0, path)
        for name in list(sys.modules):
            if name == "abstraction" or name.startswith("abstraction."):
                del sys.modules[name]

        # Manager discovery must see the synthetic host, even when the
        # prepared interpreter also has a real Manager package installed.
        import importlib.abc
        import importlib.machinery
        site = os.path.join(self.root, "site-packages")

        class FixtureHostFinder(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname in ("comfy", "comfyui_manager"):
                    spec = importlib.machinery.PathFinder.find_spec(fullname, [site])
                    if spec is None:
                        raise ModuleNotFoundError("No module named %r" % fullname, name=fullname)
                    return spec

        for name in list(sys.modules):
            if name.split(".", 1)[0] in ("comfy", "comfyui_manager"):
                del sys.modules[name]
        sys.meta_path.insert(0, FixtureHostFinder())

        from aiohttp import web
        self.routes = web.RouteTableDef()
        server = types.ModuleType("server")
        server.PromptServer = SimpleNamespace(instance=SimpleNamespace(routes=self.routes))
        sys.modules["server"] = server

        self.models = os.path.join(self.root, "models", "checkpoints")
        os.makedirs(self.models)

        if self.layout is not None:
            enable = self.enable_manager
            if enable is None:
                enable = self.layout == "v4"
            self.build((self.layout,), enable_manager=enable, legacy_ui=self.legacy_ui)

    def build(self, layouts, **kwargs):
        import importlib
        self.custom_nodes, site = build_manager(self.root, layouts, **kwargs)
        node_copy = os.path.join(self.custom_nodes, "abstraction_downloads")
        if not os.path.isdir(node_copy):
            shutil.copytree(NODE, node_copy, ignore=shutil.ignore_patterns("__pycache__"))
        for path in (site, self.custom_nodes):
            if path not in sys.path:
                sys.path.insert(0, path)
        importlib.invalidate_caches()

    def _restore_imports(self):
        for name in set(sys.modules) - self._modules.keys():
            del sys.modules[name]
        sys.modules.update(self._modules)
        sys.path[:] = self._path
        sys.meta_path[:] = self._meta_path

    def load(self):
        return load_node(self.layout, self.legacy_ui)

    def serve(self, body: bytes, die_after: int = 0, status: int = 0) -> str:
        Serve.body = body
        Serve.die_after = die_after
        Serve.status = status
        Serve.ranges_seen = []
        self.addCleanup(lambda: setattr(Serve, "status", 0))
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        httpd = http.server.HTTPServer(("127.0.0.1", port), Serve)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return f"http://127.0.0.1:{port}/model.safetensors"

    def sidecars(self):
        found = []
        for base, _, files in os.walk(self.custom_nodes):
            found += [os.path.join(base, f) for f in files if f.endswith(".oa-request.json")]
        return found


class ManagerFixtureIsolationTest(unittest.TestCase):
    def test_existing_modules_are_restored_by_identity(self):
        original = types.ModuleType("server")
        ambient_manager = types.ModuleType("comfyui_manager")
        with mock.patch.dict(sys.modules, {"server": original, "comfyui_manager": ambient_manager}):
            modules = dict(sys.modules)
            finders = list(sys.meta_path)
            case = ManagerCase()
            case.setUp()
            try:
                self.assertIsNot(sys.modules["server"], original)
                self.assertNotIn("comfyui_manager", sys.modules)
                with self.assertRaises(ModuleNotFoundError):
                    __import__("comfyui_manager")
                sys.modules["server"] = types.ModuleType("replacement")
            finally:
                case.doCleanups()
            self.assertIs(sys.modules["server"], original)
            self.assertEqual(sys.modules, modules)
            self.assertEqual(sys.meta_path, finders)

    def test_absent_server_is_removed_and_real_recipe_routes_register(self):
        with mock.patch.dict(sys.modules):
            sys.modules.pop("server", None)
            case = ManagerCase()
            case.setUp()
            try:
                case.build(("v4",), enable_manager=True)
                start_manager("v4")
                node = import_node_with_passing_preflight()
                self.assertTrue(node.workflow_routes_status)
                routes = {(route.method, route.path) for route in case.routes}
                self.assertIn(("GET", node.klein_recipe.PLAN_ROUTE), routes)
                self.assertIn(("POST", node.klein_recipe.PREPARE_ROUTE), routes)
            finally:
                case.doCleanups()
            self.assertNotIn("server", sys.modules)


def _service_refuses():
    """Service factories that fail the test if any request reaches the service."""
    def refuse(*args, **kwargs):
        raise AssertionError("a custom-node file install reached the job service")
    return refuse, refuse


class DetectionTest(ManagerCase):
    """Which Manager is adopted, and what is reported when none is."""

    def setUp(self):
        super().setUp()
        # service.preflight()'s _required() imports abstraction.job.acceptance
        # unconditionally, even with a configured fake machine (C1).
        path = os.path.join(LAYERS, "abstraction-job", "py")
        if os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)

    def import_node(self):
        with self.assertLogs("abstraction", "INFO") as logs:
            node = import_node_with_passing_preflight()
        return node, "\n".join(logs.output)

    def test_custom_nodes_root_keeps_direct_stock_and_modern_nested_placements(self):
        self.build(("v4",), enable_manager=True)
        start_manager("v4")
        node, _ = self.import_node()
        for relative in ("abstraction_downloads", "comfyui-openabstractions/oa_nodes", "abstraction_downloads/oa_nodes"):
            with self.subTest(relative=relative), mock.patch.object(node, "_HERE", os.path.join(self.custom_nodes, *relative.split('/'))):
                self.assertEqual(node._custom_nodes_root(), self.custom_nodes)
        with mock.patch.object(node, "_HERE", os.path.join(self.root, "foreign", "oa_nodes")):
            self.assertIsNone(node._custom_nodes_root())

    def test_core_installed_manager_4_is_adopted_through_the_glob_server(self):
        self.build(("v4",), enable_manager=True)
        server = start_manager("v4")
        node, log = self.import_node()
        status = node.adoption_status()
        self.assertEqual(status.code, node.ADOPTED, status.detail)
        self.assertTrue(status.adopted)
        self.assertEqual(status.layout, node.LAYOUT_PACKAGE)
        self.assertEqual(status.version, PINS["v4"]["version"])
        self.assertEqual(status.patched, ("comfyui_manager.glob.manager_server",))
        self.assertIs(server.download_url, node.download_url)
        self.assertIs(server.download_url_with_agent, node.download_url_with_agent)
        self.assertIn("4.2.2", log)

    def test_manager_4_legacy_ui_is_adopted_through_the_legacy_server(self):
        self.build(("v4",), enable_manager=True, legacy_ui=True)
        server = start_manager("v4", legacy_ui=True)
        node, _ = self.import_node()
        self.assertEqual(node.adoption_status().patched, ("comfyui_manager.legacy.manager_server",))
        self.assertIs(server.download_url, node.download_url)
        self.assertNotIn("comfyui_manager.glob.manager_server", sys.modules,
                         "the node imported the UI core did not select")

    def test_manager_4_wins_over_a_custom_node_core_disables(self):
        self.build(("v4", "v3"), enable_manager=True)
        start_manager("v4")
        node, _ = self.import_node()
        self.assertEqual(node.adoption_status().layout, node.LAYOUT_PACKAGE)
        self.assertNotIn("manager_server", sys.modules)

    def test_manager_3_custom_node_is_adopted(self):
        self.build(("v3",), enable_manager=False)
        node, _ = self.import_node()
        status = node.adoption_status()
        self.assertEqual((status.code, status.layout, status.version),
                         (node.ADOPTED, node.LAYOUT_CUSTOM_NODE, PINS["v3"]["version"]))
        self.assertIs(sys.modules["manager_server"].download_url, node.download_url)

    def test_manager_3_registry_directory_name_is_adopted(self):
        self.build(("v3",), enable_manager=None, v3_dir="comfyui-manager")
        node, _ = self.import_node()
        self.assertEqual(node.adoption_status().layout, node.LAYOUT_CUSTOM_NODE)

    def test_no_manager_is_reported(self):
        self.build((), enable_manager=True)
        node, log = self.import_node()
        status = node.adoption_status()
        self.assertFalse(status.adopted)
        self.assertEqual(status.code, node.MANAGER_NOT_FOUND)
        self.assertIn("WARNING:abstraction:[abstraction] NOT ADOPTED (manager_not_found)", log)

    def test_installed_package_without_enable_manager_is_reported(self):
        self.build(("v4",), enable_manager=False)
        node, log = self.import_node()
        self.assertEqual(node.adoption_status().code, node.MANAGER_NOT_ENABLED)
        self.assertIn("NOT ADOPTED (manager_not_enabled)", log)

    def test_manager_4_server_not_started_is_reported(self):
        self.build(("v4",), enable_manager=True)
        node, log = self.import_node()
        self.assertEqual(node.adoption_status().code, node.MANAGER_SERVER_NOT_LOADED)
        self.assertIn("NOT ADOPTED (manager_server_not_loaded)", log)
        self.assertIsNot(sys.modules["comfyui_manager.common.manager_downloader"].download_url,
                         node.download_url)

    def test_snapshots_without_an_adopted_manager_are_reported(self):
        os.environ["ABSTRACTION_COMFYUI_SNAPSHOTS"] = "service"
        self.build((), enable_manager=True)
        node, log = self.import_node()
        status = node.snapshot_status()
        self.assertEqual((status.enabled, status.adopted, status.code),
                         (True, False, node.SNAPSHOTS_MANAGER_NOT_ADOPTED))
        self.assertIn("SNAPSHOTS NOT ADOPTED (manager_not_adopted)", log)

    def test_manager_4_unimportable_is_reported(self):
        self.build(("v4",), enable_manager=True)
        _write(os.path.join(self.root, "site-packages", "comfyui_manager", "common", "manager_downloader.py"),
               "raise ImportError('huggingface_hub missing')\n")
        node, log = self.import_node()
        status = node.adoption_status()
        self.assertEqual((status.code, status.version), (node.MANAGER_NOT_IMPORTABLE, PINS["v4"]["version"]))
        self.assertIn("huggingface_hub missing", log)

    def assert_archive_stays_with_manager(self, layout):
        self.layout = layout
        self.build((layout,), enable_manager=(layout == "v4"))
        node, downloader, _, core = self.load()
        self.assertTrue(node.adoption_status().adopted)
        from abstraction_downloads import service
        service.configure(*_service_refuses())
        self.addCleanup(service.configure)
        archive = core.cnr_install("https://cdn.comfy.org/node/1.0.0/node.zip", self.custom_nodes)
        self.assertEqual(downloader.calls, [
            ("https://cdn.comfy.org/node/1.0.0/node.zip", self.custom_nodes, archive)])
        self.assertEqual(self.sidecars(), [])

    def test_manager_4_registry_archive_stays_with_manager(self):
        self.assert_archive_stays_with_manager("v4")

    def test_manager_3_registry_archive_stays_with_manager(self):
        self.assert_archive_stays_with_manager("v3")

    def test_manager_3_copy_install_files_stay_with_manager(self):
        self.layout = "v3"
        self.build(("v3",), enable_manager=False)
        node, downloader, server, core = self.load()
        from abstraction_downloads import service
        service.configure(*_service_refuses())
        self.addCleanup(service.configure)
        self.assertTrue(server.copy_install(["https://example.org/node.py", "https://example.org/ui.js"], "ext"))
        self.assertEqual([call[2] for call in downloader.calls], ["node.py", "ui.js"])
        self.assertEqual(self.sidecars(), [])
        self.assertFalse(os.path.exists(os.path.join(core.js_path, "ext", "ui.js.oa-request.json")))


class PreflightTest(ManagerCase):
    """service.preflight(): the four checked items and their codes, unit-tested
    directly against service.configure() -- no Manager layout is needed."""

    def setUp(self):
        super().setUp()
        # service.preflight()'s _required() imports abstraction.job.acceptance,
        # and _resolve() imports abstraction.facade for Scope.LOCAL, both
        # unconditionally, even with a configured fake machine.
        for layer in ("abstraction-job", "abstraction-facade"):
            path = os.path.join(LAYERS, layer, "py")
            if os.path.isdir(path) and path not in sys.path:
                sys.path.insert(0, path)
        self.build(())  # no Manager layout: just get the node onto sys.path
        import importlib
        try:
            import abstraction.job.acceptance  # noqa: F401
            from abstraction.facade import Scope  # noqa: F401
        except ImportError:
            self.skipTest("the abstraction-job/abstraction-facade Python packages are not importable beside this node")
        # No Manager fixture exists, so the node reports MANAGER_NOT_FOUND and
        # returns before ever calling preflight(); that is fine here, since this
        # class calls service.preflight() directly rather than importing through _install().
        importlib.import_module("abstraction_downloads")
        self.service = importlib.import_module("abstraction_downloads.service")

    def test_ready_when_the_sdk_native_library_runtime_and_acceptance_all_resolve(self):
        self.service.configure(lambda **waiting: SimpleNamespace(resolve_jobs=lambda **kw: SimpleNamespace()),
                               lambda endpoint, owner, **waiting: None)
        result = self.service.preflight()
        self.assertEqual((result.ready, result.code), (True, self.service.PREFLIGHT_READY))

    def test_resolve_jobs_receives_a_real_scope_enum_member(self):
        """abstraction.facade.client.Machine._bind rejects a scope argument that
        is not a Scope instance. This adapter used to pass the bare string
        "local" here; every fake machine in this suite accepts any scope kwarg,
        so a real Machine was the first thing to reject it, live
        (research/adoption/comfyui/results/live-2026-09-22.md, C1)."""
        from abstraction.facade import Scope
        seen = {}

        def resolve_jobs(**kw):
            seen.update(kw)
            return SimpleNamespace()
        self.service.configure(lambda **waiting: SimpleNamespace(resolve_jobs=resolve_jobs),
                               lambda endpoint, owner, **waiting: None)
        self.service._resolve(0.0, ())
        self.assertEqual(seen.get("scope"), Scope.LOCAL)
        self.assertIsInstance(seen.get("scope"), Scope)

    def test_native_unavailable_when_the_machine_does_not_construct(self):
        def raiser(**waiting):
            raise OSError("no native IPC library configured")
        self.service.configure(raiser, lambda endpoint, owner, **waiting: None)
        result = self.service.preflight()
        self.assertEqual((result.ready, result.code), (False, self.service.PREFLIGHT_NATIVE_UNAVAILABLE))
        self.assertIn("native IPC library", result.detail)

    def test_runtime_unavailable_when_resolution_reports_no_runtime(self):
        class Refused(Exception):
            status = "runtime_unavailable"
        def resolve_jobs(**kw):
            raise Refused("service resolution: runtime_unavailable")
        self.service.configure(lambda **waiting: SimpleNamespace(resolve_jobs=resolve_jobs),
                               lambda endpoint, owner, **waiting: None)
        result = self.service.preflight()
        self.assertEqual((result.ready, result.code), (False, self.service.PREFLIGHT_RUNTIME_UNAVAILABLE))
        self.assertIn("no installed OA runtime", result.detail)

    def test_acceptance_unavailable_when_resolution_refuses_for_another_reason(self):
        class Refused(Exception):
            status = "unmet_requirements"
        def resolve_jobs(**kw):
            raise Refused("service resolution: unmet_requirements")
        self.service.configure(lambda **waiting: SimpleNamespace(resolve_jobs=resolve_jobs),
                               lambda endpoint, owner, **waiting: None)
        result = self.service.preflight()
        self.assertEqual((result.ready, result.code), (False, self.service.PREFLIGHT_ACCEPTANCE_UNAVAILABLE))
        self.assertIn("unmet_requirements", result.detail)


class ActivationTest(ManagerCase):
    """C1: the import-time preflight and the retained-record activation rule.

    the job service throughout; each test
    builds its own Manager v4 fixture and imports the node with
    service.configure() already in effect, so the import's own preflight call
    observes it -- the same seam SnapshotTests uses, just applied before import
    instead of after.
    """

    def setUp(self):
        super().setUp()
        for layer in ("abstraction-job", "abstraction-facade"):
            path = os.path.join(LAYERS, layer, "py")
            if os.path.isdir(path) and path not in sys.path:
                sys.path.insert(0, path)
        try:
            import abstraction.job.acceptance  # noqa: F401
            from abstraction.facade import Scope  # noqa: F401
        except ImportError:
            self.skipTest("the abstraction-job/abstraction-facade Python packages are not importable beside this node")
        self.build(("v4",), enable_manager=True)

    def import_with(self, machine_factory):
        import importlib.util
        node_dir = os.path.join(self.custom_nodes, "abstraction_downloads")
        pkg_spec = importlib.util.spec_from_file_location(
            "abstraction_downloads", os.path.join(node_dir, "__init__.py"),
            submodule_search_locations=[node_dir])
        pkg = importlib.util.module_from_spec(pkg_spec)
        sys.modules["abstraction_downloads"] = pkg
        start_manager("v4")
        service = importlib.import_module("abstraction_downloads.service")
        service.configure(machine_factory, lambda endpoint, owner, **waiting: None)
        with self.assertLogs("abstraction", "INFO") as logs:
            pkg_spec.loader.exec_module(pkg)
        server = importlib.import_module("comfyui_manager.glob.manager_server")
        return pkg, server, service, "\n".join(logs.output)

    def test_a_passing_preflight_installs_service_mode(self):
        ready = lambda **waiting: SimpleNamespace(resolve_jobs=lambda **kw: SimpleNamespace())
        node, server, service, log = self.import_with(ready)

        status = node.install_status()
        self.assertEqual((status.mode, status.code), (node.MODE_SERVICE, service.PREFLIGHT_READY))
        self.assertTrue(node.adoption_status().adopted)
        self.assertIs(server.download_url, node.download_url)

    def test_an_absent_runtime_with_no_retained_records_leaves_manager_untouched(self):
        def absent(**waiting):
            raise OSError("no installed OA runtime")
        node, server, service, log = self.import_with(absent)

        status = node.install_status()
        self.assertEqual((status.mode, status.code), (node.MODE_MANAGER, service.PREFLIGHT_NATIVE_UNAVAILABLE))
        self.assertIn("native IPC library", status.detail)
        self.assertIn("no installed OA runtime", status.detail)
        adoption = node.adoption_status()
        self.assertFalse(adoption.adopted)
        self.assertEqual(adoption.code, node.NOT_ACTIVATED)
        self.assertIsNot(server.download_url, node.download_url,
                         "Manager's own download_url must stay bound when nothing is activated")
        self.assertIn("OA DOWNLOADS NOT ACTIVATED (native_unavailable)", log)

    def test_an_absent_runtime_with_a_retained_record_keeps_service_mode(self):
        # A retained caller record beside a model, the way service.deliver()
        # writes one (SERVICE.md "Storage reuse"): accepted OA work is pending.
        record = os.path.join(self.models, "existing.safetensors.oa-request.json")
        with open(record, "w", encoding="utf-8") as f:
            f.write("{}")

        def absent(**waiting):
            raise OSError("no installed OA runtime")
        node, server, service, log = self.import_with(absent)

        status = node.install_status()
        self.assertEqual((status.mode, status.code), (node.MODE_SERVICE, service.PREFLIGHT_NATIVE_UNAVAILABLE))
        self.assertIn("pending", status.detail)
        self.assertTrue(node.adoption_status().adopted)
        self.assertIs(server.download_url, node.download_url,
                      "accepted OA work must keep Manager model installs on the job service")
        self.assertIn("OA DOWNLOADS PENDING (native_unavailable)", log)


REVISION = "0123456789abcdef0123456789abcdef01234567"


class _FakeJobs:
    """A job binding that accepts download submissions and serves their bytes."""

    endpoint, owner = "fake-endpoint", "fake-owner"

    def __init__(self, bodies, failing=()):
        self.bodies, self.failing = bodies, set(failing)
        self.accepted, self.submits, self.reconciles = {}, [], []
        self.cancelled = []
        self.labels = {}
        self.lose_reply_after = None
        self.refuse = None

    def get_history_window(self):
        return SimpleNamespace(history_epoch="epoch-1")

    def submit(self, submission):
        import json
        self.submits.append(submission.identity.key)
        if self.refuse:
            return SimpleNamespace(outcome=self.refuse)
        self.labels[json.loads(submission.spec)["sources"][0]["locator"]] = submission.label
        self.accepted[submission.identity.key] = json.loads(submission.spec)
        if self.lose_reply_after is not None and len(self.submits) > self.lose_reply_after:
            raise ConnectionError("fixture lost the acceptance reply")
        return SimpleNamespace(outcome="accepted")

    def reconcile(self, identity):
        self.reconciles.append(identity.key)
        return SimpleNamespace(outcome="accepted" if identity.key in self.accepted else "definitely_not_accepted")

    def _url(self, identity):
        return self.accepted[identity.key]["sources"][0]["locator"]

    def observe_work(self, identity):
        url = self._url(identity)
        state = "failed" if url in self.failing else "complete"
        size = len(self.bodies.get(url, b"")) if state == "complete" else 0
        return SimpleNamespace(outcome="observed", snapshot=SimpleNamespace(
            state=state, progress=SimpleNamespace(done=size, total=size)))

    def cancel_work(self, identity):
        self.cancelled.append((identity.key, identity.history_epoch, identity.attempt))
        return SimpleNamespace(outcome="requested")

    def copy_result(self, identity, output):
        output.write(self.bodies[self._url(identity)])


class SnapshotTests:
    """ABSTRACTION_COMFYUI_SNAPSHOTS=service: one download job per repository file."""

    FILES = {
        "model_index.json": b'{"_class_name": "TinyPipeline"}',
        "unet/diffusion.safetensors": bytes(range(256)) * 300,
        "tokenizer/vocab.txt": b"a\nb\n",
    }

    def setUp(self):
        super().setUp()
        for layer in ("abstraction-download", "abstraction-job"):
            path = os.path.join(LAYERS, layer, "py")
            if os.path.isdir(path) and path not in sys.path:
                sys.path.insert(0, path)
        try:
            import abstraction.download.request as rec  # noqa: F401
            import abstraction.job.acceptance as acceptance  # noqa: F401
        except ImportError:
            self.skipTest("the download and job Python packages are not importable beside this node")
        self.local_dir = os.path.join(self.root, "models", "diffusers", "tiny")

    def load_with(self, value):
        if value is None:
            os.environ.pop("ABSTRACTION_COMFYUI_SNAPSHOTS", None)
        else:
            os.environ["ABSTRACTION_COMFYUI_SNAPSHOTS"] = value
        node, downloader, server, _ = self.load()
        return node, downloader, server

    def listing(self, files=None, repo="org/tiny"):
        files = self.FILES if files is None else files
        entries = []
        for path, body in files.items():
            entries.append(dict(
                path=path, url="https://huggingface.co/%s/resolve/%s/%s" % (repo, REVISION, path), size=len(body),
                digest="sha256:" + hashlib.sha256(body).hexdigest() if path.endswith(".safetensors") else ""))
        return dict(revision=REVISION, files=entries), {e["url"]: files[e["path"]] for e in entries}

    def adopt(self, files=None, failing_paths=()):
        node, downloader, server = self.load_with("service")
        listing, bodies = self.listing(files)
        failing = [e["url"] for e in listing["files"] if e["path"] in failing_paths]
        fake = _FakeJobs(bodies, failing)
        from abstraction_downloads import service
        service.configure(lambda **waiting: SimpleNamespace(resolve_jobs=lambda **kwargs: fake),
                          lambda endpoint, owner, **waiting: fake)
        self.addCleanup(service.configure)
        self.listed = []

        def list_once(repo_id):
            self.listed.append(repo_id)
            return listing
        node._list_repository = list_once
        return node, downloader, server, service, fake

    def delivered(self):
        found = []
        for base, _, names in os.walk(self.local_dir):
            found += [os.path.relpath(os.path.join(base, n), self.local_dir).replace(os.sep, "/") for n in names]
        return sorted(found)

    def test_unset_leaves_manager_snapshots_alone(self):
        node, downloader, server = self.load_with(None)
        status = node.snapshot_status()
        self.assertEqual((status.enabled, status.adopted, status.code), (False, False, node.SNAPSHOTS_OFF))
        self.assertIsNot(downloader.download_repo_in_bytes, node.download_repo_in_bytes)
        self.assertEqual(server.install_snapshot("org/tiny", self.local_dir), "success")
        self.assertEqual(downloader.calls, [("repo", "org/tiny", self.local_dir)])

    def test_an_unsupported_value_is_refused_visibly(self):
        with self.assertLogs("abstraction", "INFO") as logs:
            node, downloader, _ = self.load_with("aria2")
        self.assertEqual(node.snapshot_status().code, node.SNAPSHOTS_INVALID_CONFIGURATION)
        self.assertIn("SNAPSHOTS NOT ADOPTED (invalid_configuration)", "\n".join(logs.output))
        self.assertIsNot(downloader.download_repo_in_bytes, node.download_repo_in_bytes)

    def test_every_file_is_one_job_under_one_record(self):
        import json
        node, downloader, server, service, fake = self.adopt()
        self.assertTrue(node.snapshot_status().adopted)
        self.assertIs(downloader.download_repo_in_bytes, node.download_repo_in_bytes)
        self.assertEqual(server.install_snapshot("org/tiny", self.local_dir), "success")
        self.assertEqual(self.delivered(), sorted(self.FILES))
        for path, body in self.FILES.items():
            with open(os.path.join(self.local_dir, *path.split("/")), "rb") as f:
                self.assertEqual(f.read(), body, path)
        self.assertEqual(len(fake.submits), 3)
        self.assertEqual(len(set(fake.submits)), 3, "files shared a request identity")
        record_path = service.snapshot_record_path(self.local_dir)
        self.assertEqual(os.path.dirname(record_path), os.path.dirname(self.local_dir))
        with open(record_path, encoding="utf-8") as f:
            record = json.load(f)
        self.assertEqual((record["repo"], record["revision"], record["epoch"]), ("org/tiny", REVISION, "epoch-1"))
        specs = {path: json.loads(entry["spec"]) for path, entry in record["files"].items()}
        self.assertEqual(specs["unet/diffusion.safetensors"]["artifact"],
                         dict(digest="sha256:" + hashlib.sha256(self.FILES["unet/diffusion.safetensors"]).hexdigest(),
                              size=len(self.FILES["unet/diffusion.safetensors"])))
        self.assertEqual(specs["tokenizer/vocab.txt"]["artifact"], dict(size=4))
        # Each job carries the display label <repo>@<rev> · <path> (JOB-A12).
        self.assertEqual(fake.labels, {spec["sources"][0]["locator"]: "org/tiny@%s · %s" % (REVISION, path)
                                       for path, spec in specs.items()})
        self.assertEqual(downloader.calls, [])
        self.assertEqual(self.sidecars(), [])

    def test_a_forbidden_submission_names_the_grant_command(self):
        _, _, server, service, fake = self.adopt()
        fake.refuse = "forbidden"
        # Guidance names a configured relay helper. This synthetic program
        # exercises presentation; it supplies no native caller identity proof.
        helper = os.path.join(self.root, "helper", "python.exe")
        relay = SimpleNamespace(program=lambda: helper, library=lambda: None)
        patch = mock.patch.dict(sys.modules, {service.oa_route.RELAY: relay})
        patch.start()
        self.addCleanup(patch.stop)
        self.assertEqual(service.rights_program(), helper)
        command = 'openabstractions rights grant --for downloads --program "%s" --registry hf' % helper
        with self.assertLogs("abstraction", "WARNING") as logs, self.assertRaises(RuntimeError) as raised:
            server.install_snapshot("org/tiny", self.local_dir)
        self.assertIn("download acceptance: forbidden", str(raised.exception))
        self.assertIn(command, str(raised.exception))
        self.assertIn("DOWNLOAD NOT PERMITTED (forbidden)", "\n".join(logs.output))
        self.assertIn(command, "\n".join(logs.output))
        self.assertEqual(service.grant_command(), command)
        self.assertEqual(service.refusal_detail("unavailable"), "", "only a rule refusal names a grant")
        self.assertEqual(self.delivered(), [])

    def test_a_forbidden_submission_without_a_relay_names_no_program(self):
        _, _, server, service, fake = self.adopt()
        fake.refuse = "forbidden"
        sys.modules.pop(service.oa_route.RELAY, None)
        self.assertIsNone(service.rights_program())
        with self.assertLogs("abstraction", "WARNING") as logs, self.assertRaises(RuntimeError) as raised:
            server.install_snapshot("org/tiny", self.local_dir)
        for detail in (str(raised.exception), "\n".join(logs.output)):
            self.assertIn("caller program is unavailable", detail)
            self.assertNotIn("--program", detail)
        self.assertIsNone(service.grant_command())
        self.assertEqual(self.delivered(), [])

    def test_a_long_snapshot_label_keeps_the_file_name(self):
        node, _, _, service, _ = self.adopt()
        label = service.snapshot_label("org/" + "r" * 300, REVISION, "unet/model.safetensors")
        self.assertLessEqual(len(label.encode("utf-8")), 256)
        self.assertTrue(label.startswith("…") and label.endswith(" · unet/model.safetensors"), label)
        self.assertEqual(service.snapshot_label("org/tiny", REVISION, "a\nb.txt"), "org/tiny@%s · ab.txt" % REVISION)

    def test_a_lost_reply_is_reconciled_without_listing_again(self):
        node, _, server, _, fake = self.adopt()
        fake.lose_reply_after = 1
        with self.assertRaises(ConnectionError):
            server.install_snapshot("org/tiny", self.local_dir)
        self.assertEqual(self.delivered(), [])

        def listed_again(repo_id):
            raise AssertionError("listed again despite a retained record")
        node._list_repository = listed_again
        fake.lose_reply_after = None
        server.install_snapshot("org/tiny", self.local_dir)
        self.assertEqual(self.delivered(), sorted(self.FILES))
        self.assertEqual(len(fake.reconciles), 3)
        self.assertEqual(len(fake.submits), 3, "an accepted file was submitted twice")
        self.assertEqual(self.listed, ["org/tiny"])

    def test_a_deleted_file_is_redelivered_from_the_same_job(self):
        _, _, server, _, fake = self.adopt()
        server.install_snapshot("org/tiny", self.local_dir)
        os.unlink(os.path.join(self.local_dir, "model_index.json"))
        server.install_snapshot("org/tiny", self.local_dir)
        self.assertEqual(self.delivered(), sorted(self.FILES))
        self.assertEqual(len(fake.submits), 3)

    def test_a_failed_file_raises_and_keeps_the_record(self):
        _, _, server, service, _ = self.adopt(failing_paths=("tokenizer/vocab.txt",))
        with self.assertRaises(RuntimeError) as raised:
            server.install_snapshot("org/tiny", self.local_dir)
        self.assertIn("download state: failed", str(raised.exception))
        self.assertTrue(os.path.exists(service.snapshot_record_path(self.local_dir)))
        self.assertNotIn("tokenizer/vocab.txt", self.delivered())

    def test_a_path_outside_the_directory_is_refused_before_any_submission(self):
        _, _, server, service, fake = self.adopt(files={"../escape.bin": b"x"})
        with self.assertRaises(ValueError):
            server.install_snapshot("org/tiny", self.local_dir)
        self.assertEqual(fake.submits, [])
        self.assertFalse(os.path.exists(service.snapshot_record_path(self.local_dir)))

    def test_the_repository_listing_pins_the_revision(self):
        node, _, _ = self.load_with("service")
        hub = types.ModuleType("huggingface_hub")

        class HfApi:
            def repo_info(self, repo_id, files_metadata=False):
                assert files_metadata and repo_id == "org/tiny"
                return SimpleNamespace(sha="abc123", siblings=[
                    SimpleNamespace(rfilename="unet/a b.safetensors", size=10, lfs=SimpleNamespace(sha256="ABCDEF")),
                    SimpleNamespace(rfilename="README.md", size=5, lfs=None),
                    SimpleNamespace(rfilename="no-size", size=None, lfs=None),
                ])
        hub.HfApi = HfApi
        sys.modules["huggingface_hub"] = hub
        os.environ["HF_ENDPOINT"] = "https://hf-mirror.example/"
        self.assertEqual(node._list_repository("org/tiny"), dict(revision="abc123", files=[
            dict(path="unet/a b.safetensors", url="https://hf-mirror.example/org/tiny/resolve/abc123/unet/a%20b.safetensors",
                 size=10, digest="sha256:abcdef"),
            dict(path="README.md", url="https://hf-mirror.example/org/tiny/resolve/abc123/README.md", size=5, digest=""),
        ]))


class SnapshotManagerV4Test(SnapshotTests, ManagerCase):
    layout = "v4"


class SnapshotManagerV4LegacyUITest(SnapshotTests, ManagerCase):
    layout = "v4"
    legacy_ui = True


class SnapshotManagerV3Test(SnapshotTests, ManagerCase):
    layout = "v3"


class _FakeResolver:
    """abstraction.model/resolver@1 that knows some files' digests."""

    def __init__(self, known):
        self.known, self.refs = known, []

    def resolve(self, ref):
        self.refs.append((ref.registry, ref.repo, ref.revision, ref.file))
        hit = self.known.get(ref.file)
        if hit is None:
            return SimpleNamespace(outcome="unsupported_mapping", request=None)
        return SimpleNamespace(outcome="resolved", request=SimpleNamespace(
            artifact=SimpleNamespace(digest=hit[0], size=hit[1])))


class _FakeReader:
    """abstraction.storage/content-reader@1 holding some blobs by digest."""

    def __init__(self, blobs, outcome=None, fail_read=None):
        self.blobs, self.outcome, self.fail_read = blobs, outcome, fail_read
        self.opened, self.closed = [], 0

    def open(self, digest):
        self.opened.append(digest)
        if self.outcome:
            return SimpleNamespace(outcome=self.outcome, resource=None)
        if digest not in self.blobs:
            return SimpleNamespace(outcome="not_found", resource=None)
        return SimpleNamespace(outcome="opened", resource=SimpleNamespace(
            handle="h", digest=digest, size=len(self.blobs[digest]), verification="unverified"))

    def copy(self, resource, destination):
        if self.fail_read:
            raise RuntimeError(self.fail_read)
        data = self.blobs[resource.digest]
        destination.write(data)
        return len(data)

    def close(self, resource):
        self.closed += 1


class StorageReuseTest(ManagerCase):
    """An OA-routed install looks up the digest and asks content storage first."""

    layout = "v4"
    BODY = bytes(range(256)) * 400
    URL = "https://huggingface.co/oa/tiny/resolve/main/model.safetensors"

    def setUp(self):
        super().setUp()
        for layer in ("abstraction-download", "abstraction-facade", "abstraction-job", "abstraction-model",
                      "abstraction-storage"):
            path = os.path.join(LAYERS, layer, "py")
            if os.path.isdir(path) and path not in sys.path:
                sys.path.insert(0, path)
        try:
            import abstraction.model.api as rec  # noqa: F401
            import abstraction.job.acceptance as acceptance  # noqa: F401
            from abstraction.facade import Scope  # noqa: F401
        except ImportError:
            self.skipTest("the model, download, facade and job Python packages are not importable beside this node")
        self.digest = "sha256:" + hashlib.sha256(self.BODY).hexdigest()

    def configure(self, jobs, resolver=None, reader=None, model_error=None, storage_error=None):
        self.scopes = {}

        def resolve_jobs(**kwargs):
            self.scopes["resolve_jobs"] = kwargs.get("scope")
            return jobs

        def resolve_model(**kwargs):
            self.scopes["resolve_model"] = kwargs.get("scope")
            if model_error is not None:
                raise model_error
            return resolver

        def resolve_storage(**kwargs):
            self.scopes["resolve_storage"] = kwargs.get("scope")
            if storage_error is not None:
                raise storage_error
            return reader
        machine = SimpleNamespace(resolve_jobs=resolve_jobs, resolve_model=resolve_model,
                                  resolve_storage=resolve_storage)
        from abstraction_downloads import service
        service.configure(lambda **waiting: machine, lambda endpoint, owner, **waiting: jobs)
        self.addCleanup(service.configure)

    def install(self, url=None, **kwargs):
        _, _, server, _ = self.load()
        url = url or self.URL
        jobs = _FakeJobs({url: self.BODY})
        self.configure(jobs, **kwargs)
        with self.assertLogs("abstraction", "INFO") as logs:
            server.do_install_model(url, self.models, "model.safetensors")
        return jobs, "\n".join(logs.output)

    def final(self):
        return os.path.join(self.models, "model.safetensors")

    def known(self):
        return _FakeResolver({"model.safetensors": (self.digest, len(self.BODY))})

    def assert_clean(self):
        self.assertEqual([n for n in os.listdir(self.models) if n.startswith(".oa-model-")], [])

    def test_stored_bytes_are_delivered_without_a_job(self):
        resolver, reader = self.known(), _FakeReader({self.digest: self.BODY})
        jobs, log = self.install(resolver=resolver, reader=reader)
        with open(self.final(), "rb") as f:
            self.assertEqual(f.read(), self.BODY)
        self.assertEqual(jobs.submits, [])
        self.assertEqual(resolver.refs, [("hf", "oa/tiny", "main", "model.safetensors")])
        self.assertEqual((reader.opened, reader.closed), ([self.digest], 1))
        self.assertIn("model.safetensors delivered from OA storage (%s)" % self.digest, log)
        self.assertFalse(os.path.exists(self.final() + ".oa-request.json"))
        self.assert_clean()

    def test_scope_is_a_real_enum_member_not_a_bare_string(self):
        """abstraction.facade.client.Machine._bind rejects a scope argument that
        is not a Scope instance. This adapter used to pass the bare string
        "local"; every fake machine in this suite accepts any scope kwarg, so a
        real Machine was the first thing to reject it, live
        (research/adoption/comfyui/results/live-2026-09-22.md, C1)."""
        from abstraction.facade import Scope
        resolver, reader = self.known(), _FakeReader({self.digest: self.BODY})
        self.install(resolver=resolver, reader=reader)
        self.assertEqual(self.scopes["resolve_model"], Scope.LOCAL)
        self.assertEqual(self.scopes["resolve_storage"], Scope.LOCAL)
        for name, value in self.scopes.items():
            self.assertIsInstance(value, Scope, "%s received %r, not a Scope member" % (name, value))

    def assert_job_fallback(self, reason, level, **kwargs):
        jobs, log = self.install(**kwargs)
        with open(self.final(), "rb") as f:
            self.assertEqual(f.read(), self.BODY)
        self.assertEqual(len(jobs.submits), 1)
        self.assertIn("%s:abstraction:[abstraction] OA storage did not supply model.safetensors; "
                      "downloading through the job service (reason=%s)" % (level, reason), log)
        self.assert_clean()
        return jobs, log

    def test_absent_bytes_download_with_the_looked_up_digest(self):
        jobs, _ = self.assert_job_fallback("storage_not_found", "INFO", resolver=self.known(), reader=_FakeReader({}))
        self.assertEqual(next(iter(jobs.accepted.values()))["artifact"], dict(digest=self.digest, size=len(self.BODY)))

    def test_refused_storage_is_typed(self):
        self.assert_job_fallback("storage_refused", "WARNING", resolver=self.known(),
                                 reader=_FakeReader({self.digest: self.BODY}, outcome="forbidden"))

    def test_a_failed_read_is_typed(self):
        self.assert_job_fallback("storage_read_failed", "WARNING", resolver=self.known(),
                                 reader=_FakeReader({self.digest: self.BODY}, fail_read="gap"))

    def test_wrong_stored_bytes_are_refused(self):
        self.assert_job_fallback("storage_digest_mismatch", "WARNING", resolver=self.known(),
                                 reader=_FakeReader({self.digest: bytes(len(self.BODY))}))

    def test_a_stored_size_that_differs_is_refused_before_reading(self):
        reader = _FakeReader({self.digest: self.BODY + b"x"})
        reader.copy = lambda resource, destination: (_ for _ in ()).throw(AssertionError("read despite a size mismatch"))
        self.assert_job_fallback("storage_digest_mismatch", "WARNING", resolver=self.known(), reader=reader)

    def capture(self):
        import logging
        capture, logger = _Capture(), logging.getLogger("abstraction")
        previous = logger.level
        logger.addHandler(capture)
        logger.setLevel(logging.INFO)
        self.addCleanup(logger.removeHandler, capture)
        self.addCleanup(logger.setLevel, previous)
        return capture

    def test_absent_storage_is_reported_once_and_checked_again_on_an_interval(self):
        import importlib
        _, _, server, _ = self.load()
        reuse = importlib.import_module("abstraction_downloads.storage_reuse")
        now = [1000.0]
        reuse.STORAGE.clock = lambda: now[0]
        urls = ["https://huggingface.co/oa/tiny/resolve/main/m%d.safetensors" % i for i in range(4)]
        jobs = _FakeJobs({url: self.BODY for url in urls})
        resolver = _FakeResolver({"m%d.safetensors" % i: (self.digest, len(self.BODY)) for i in range(4)})
        reader = _FakeReader({self.digest: self.BODY})
        offered, attempts = [False], []

        def resolve_storage(**kwargs):
            attempts.append(now[0])
            if not offered[0]:
                raise RuntimeError("service resolution: not_ready")
            return reader
        machine = SimpleNamespace(resolve_jobs=lambda **kwargs: jobs, resolve_model=lambda **kwargs: resolver,
                                  resolve_storage=resolve_storage)
        from abstraction_downloads import service
        service.configure(lambda **waiting: machine, lambda endpoint, owner, **waiting: jobs)
        self.addCleanup(service.configure)
        capture = self.capture()

        def install(i):
            server.do_install_model(urls[i], self.models, "m%d.safetensors" % i)

        def not_offered():
            return [m for m in capture.messages
                    if "abstraction.storage/content-reader@1 not offered (reason=storage_not_offered)" in m]
        install(0)
        install(1)
        self.assertEqual(len(not_offered()), 1, capture.messages)
        self.assertEqual(attempts, [1000.0], "storage resolution was repeated inside the interval")
        self.assertFalse([m for m in capture.messages if "OA storage did not supply" in m], capture.messages)
        self.assertEqual(len(jobs.submits), 2)
        now[0] += reuse.RECHECK_SECONDS
        install(2)
        self.assertEqual(attempts, [1000.0, 1000.0 + reuse.RECHECK_SECONDS])
        self.assertEqual(len(not_offered()), 1, "an unchanged answer was logged again")
        offered[0] = True
        now[0] += reuse.RECHECK_SECONDS
        install(3)
        self.assertEqual([m for m in capture.messages if "is offered again" in m],
                         ["[abstraction] OA abstraction.storage/content-reader@1 is offered again; storage reuse uses it"])
        self.assertIn("m3.safetensors delivered from OA storage (%s)" % self.digest, "\n".join(capture.messages))
        self.assertEqual(len(jobs.submits), 3)
        for i in range(4):
            with open(os.path.join(self.models, "m%d.safetensors" % i), "rb") as f:
                self.assertEqual(f.read(), self.BODY)

    def test_an_unknown_digest_is_typed_and_storage_is_not_asked(self):
        reader = _FakeReader({self.digest: self.BODY})
        _, log = self.assert_job_fallback("digest_unknown", "INFO", resolver=_FakeResolver({}), reader=reader)
        self.assertEqual(reader.opened, [])
        self.assertIn("model lookup: unsupported_mapping", log)

    def test_an_absent_model_client_is_reported_once(self):
        _, _, server, _ = self.load()
        urls = ["https://huggingface.co/oa/tiny/resolve/main/m%d.safetensors" % i for i in range(3)]
        jobs = _FakeJobs({url: self.BODY for url in urls})
        reader, attempts = _FakeReader({self.digest: self.BODY}), []

        def resolve_model(**kwargs):
            attempts.append(1)
            raise RuntimeError("service resolution: not_ready")
        machine = SimpleNamespace(resolve_jobs=lambda **kwargs: jobs, resolve_model=resolve_model,
                                  resolve_storage=lambda **kwargs: reader)
        from abstraction_downloads import service
        service.configure(lambda **waiting: machine, lambda endpoint, owner, **waiting: jobs)
        self.addCleanup(service.configure)
        capture = self.capture()
        for i, url in enumerate(urls):
            server.do_install_model(url, self.models, "m%d.safetensors" % i)
        lines = [m for m in capture.messages if "not offered" in m]
        self.assertEqual(len(lines), 1, capture.messages)
        self.assertIn("OA abstraction.model/resolver@1 not offered (reason=model_not_offered)", lines[0])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(reader.opened, [], "storage was asked without a digest")
        self.assertFalse([m for m in capture.messages if "OA storage did not supply" in m], capture.messages)
        self.assertEqual(len(jobs.submits), 3)

    def test_a_failing_bound_reader_is_reported_as_not_offered(self):
        reader = _FakeReader({self.digest: self.BODY})
        reader.open = lambda digest: (_ for _ in ()).throw(ConnectionError("endpoint closed"))
        jobs, log = self.install(resolver=self.known(), reader=reader)
        self.assertEqual(len(jobs.submits), 1)
        self.assertIn("abstraction.storage/content-reader@1 not offered (reason=storage_not_offered): "
                      "open: ConnectionError('endpoint closed')", log)
        self.assertNotIn("OA storage did not supply", log)

    def test_a_retry_uses_the_retained_digest(self):
        resolver = self.known()
        _, _, server, _ = self.load()
        jobs = _FakeJobs({self.URL: self.BODY})
        jobs.lose_reply_after = 0
        self.configure(jobs, resolver=resolver, reader=_FakeReader({}))
        with self.assertRaises(ConnectionError), self.assertLogs("abstraction", "INFO"):
            server.download_url(self.URL, self.models, "model.safetensors")
        reader = _FakeReader({self.digest: self.BODY})
        self.configure(jobs, resolver=_FakeResolver({}), reader=reader)
        with self.assertLogs("abstraction", "INFO") as logs:
            server.download_url(self.URL, self.models, "model.safetensors")
        self.assertIn("delivered from OA storage", "\n".join(logs.output))
        self.assertEqual(reader.opened, [self.digest], "the retained digest was not used")
        with open(self.final(), "rb") as f:
            self.assertEqual(f.read(), self.BODY)

    def test_a_snapshot_file_with_stored_bytes_skips_its_job(self):
        os.environ["ABSTRACTION_COMFYUI_SNAPSHOTS"] = "service"
        node, _, server, _ = self.load()
        stored, fetched = b"stored" * 100, b"fetched" * 100
        urls = {p: "https://huggingface.co/org/tiny/resolve/%s/%s" % (REVISION, p) for p in ("a.bin", "b.bin")}
        digest = lambda b: "sha256:" + hashlib.sha256(b).hexdigest()
        listing = dict(revision=REVISION, files=[
            dict(path="a.bin", url=urls["a.bin"], size=len(stored), digest=digest(stored)),
            dict(path="b.bin", url=urls["b.bin"], size=len(fetched), digest=digest(fetched))])
        node._list_repository = lambda repo_id: listing
        jobs = _FakeJobs({urls["b.bin"]: fetched})
        self.configure(jobs, model_error=AssertionError("listed digests need no model lookup"),
                       reader=_FakeReader({digest(stored): stored}))
        local = os.path.join(self.root, "models", "diffusers", "tiny")
        with self.assertLogs("abstraction", "INFO") as logs:
            server.install_snapshot("org/tiny", local)
        log = "\n".join(logs.output)
        for name, body in (("a.bin", stored), ("b.bin", fetched)):
            with open(os.path.join(local, name), "rb") as f:
                self.assertEqual(f.read(), body, name)
        self.assertEqual(len(jobs.submits), 1)
        self.assertIn("a.bin delivered from OA storage", log)
        self.assertIn("OA storage did not supply b.bin; downloading through the job service (reason=storage_not_found)", log)


def view_snapshot(key, state, done=0, total=0, failure=None, label=""):
    """One OperationSnapshot as the inventory route encodes it."""
    value = dict(receipt=dict(identity=dict(key=key, history_epoch="epoch-1", attempt=0),
                              logical_owner="fake-owner", operation_id="op-" + key,
                              accepted_guarantees=[], history_retention_ms=1000),
                 state=state, progress=dict(done=done, total=total), cancellation_requested=False)
    if failure is not None:
        value["failure"] = failure
    if label:
        value["label"] = label
    return value


class JobsViewTest(ManagerCase):
    """C2/C3/C4: the inventory joined with this ComfyUI's caller records.

    The service names operations by request identity and carries no application
    label. These tests drive real deliveries through the Manager fixture, then
    read the rows the route answers: the model and its destination, progress,
    whether a store supplied the bytes, and a repository as one row with its
    files under it.
    """

    layout = "v4"
    BODY = bytes(range(256)) * 40
    URL = "https://huggingface.co/oa/tiny/resolve/main/model.safetensors"

    def setUp(self):
        super().setUp()
        for layer in ("abstraction-download", "abstraction-facade", "abstraction-job", "abstraction-model",
                      "abstraction-storage"):
            path = os.path.join(LAYERS, layer, "py")
            if os.path.isdir(path) and path not in sys.path:
                sys.path.insert(0, path)
        try:
            import abstraction.model.api as rec  # noqa: F401
            import abstraction.job.acceptance as acceptance  # noqa: F401
            from abstraction.facade import Scope  # noqa: F401
        except ImportError:
            self.skipTest("the model, download, facade and job Python packages are not importable beside this node")
        self.digest = "sha256:" + hashlib.sha256(self.BODY).hexdigest()
        self.local_dir = os.path.join(self.root, "models", "diffusers", "tiny")

    def view(self):
        """The node loaded, with the jobs view scanning this ComfyUI's models."""
        node, _, server, _ = self.load()
        from abstraction_downloads import oa_jobs_view, service
        oa_jobs_view.configure([os.path.join(self.root, "models")])
        self.addCleanup(oa_jobs_view.configure)
        self.addCleanup(service.configure)
        return node, server, service, oa_jobs_view

    def configure(self, jobs, resolver=None, reader=None):
        from abstraction_downloads import service
        machine = SimpleNamespace(resolve_jobs=lambda **kwargs: jobs,
                                  resolve_model=lambda **kwargs: resolver,
                                  resolve_storage=lambda **kwargs: reader)
        service.configure(lambda **waiting: machine, lambda endpoint, owner, **waiting: jobs)

    def store(self, blobs):
        """A content reader bound to a named store, as a resolved one is."""
        reader = _FakeReader(blobs)
        reader._transport = SimpleNamespace(endpoint=b"oa-local-store")
        return reader

    def fetched_model(self):
        """One model installed through a download job. Returns (view, jobs, key)."""
        import json
        _, server, _, jobs_view = self.view()
        jobs = _FakeJobs({self.URL: self.BODY})
        self.configure(jobs, resolver=_FakeResolver({}), reader=_FakeReader({}))
        with self.assertLogs("abstraction", "INFO"):
            server.do_install_model(self.URL, self.models, "model.safetensors")
        with open(os.path.join(self.models, "model.safetensors.oa-request.json"), encoding="utf-8") as f:
            key = json.load(f)["key"]
        return jobs_view, jobs, key

    def rows(self, jobs_view, operations=()):
        return jobs_view.rows(jobs_view.read_records(), list(operations))

    def wait_for(self, predicate, seconds=20.0):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_a_job_row_names_the_model_its_destination_and_its_progress(self):
        jobs_view, _, key = self.fetched_model()
        rows = self.rows(jobs_view, [view_snapshot(key, "running", done=3, total=10, label="model.safetensors")])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["kind"], row["model"], row["state"], row["percent"]),
                         ("file", "model.safetensors", "running", 30))
        self.assertEqual(row["destination"], os.path.join(self.models, "model.safetensors"))
        self.assertEqual((row["source"], row["store"]), ("fetched", ""))
        self.assertEqual(row["identity"], dict(key=key, history_epoch="epoch-1", attempt=0))
        self.assertEqual((row["operation_id"], row["url"], row["present"]), ("op-" + key, self.URL, True))

    def test_a_failed_job_row_carries_the_reason(self):
        jobs_view, _, key = self.fetched_model()
        failure = dict(classification="permanent", message="the source answered 404", cause="not_found")
        row = self.rows(jobs_view, [view_snapshot(key, "failed", failure=failure)])[0]
        self.assertEqual((row["state"], row["reason"]), ("failed", "the source answered 404"))
        self.assertEqual(row["failure"], failure)

    def test_an_operation_the_service_no_longer_lists_says_so(self):
        jobs_view, _, _ = self.fetched_model()
        row = self.rows(jobs_view)[0]
        self.assertEqual(row["state"], "unknown")
        self.assertIn("the service did not list this operation", row["reason"])

    def test_a_reused_file_names_the_store_it_came_from(self):
        _, server, service, jobs_view = self.view()
        jobs = _FakeJobs({})
        reader = self.store({self.digest: self.BODY})
        self.configure(jobs, resolver=_FakeResolver({"model.safetensors": (self.digest, len(self.BODY))}), reader=reader)
        with self.assertLogs("abstraction", "INFO") as logs:
            server.do_install_model(self.URL, self.models, "model.safetensors")
        self.assertEqual(jobs.submits, [], "a stored file was downloaded anyway")
        self.assertIn("held by oa-local-store", "\n".join(logs.output))
        record = service.read_reuse(service.reuse_record_path(os.path.join(self.models, "model.safetensors")))
        self.assertEqual([(e["store"], e["digest"], e["repo"]) for e in record["files"]],
                         [("oa-local-store", self.digest, "")])
        row = self.rows(jobs_view)[0]
        self.assertEqual((row["source"], row["store"], row["state"], row["model"]),
                         ("reused", "oa-local-store", "complete", "model.safetensors"))
        self.assertIsNone(row["identity"], "a file a store supplied has no operation")

    def snapshot_install(self, stored=("a.bin",)):
        """A three-file repository, some files from a store. Returns (view, jobs, record)."""
        import json
        node, server, service, jobs_view = self.view()
        os.environ["ABSTRACTION_COMFYUI_SNAPSHOTS"] = "service"
        node, _, server, _ = self.load()
        jobs_view.configure([os.path.join(self.root, "models")])
        bodies = {name: (name.encode() * 50) for name in ("a.bin", "b.bin", "unet/c.bin")}
        digest = lambda value: "sha256:" + hashlib.sha256(value).hexdigest()
        urls = {name: "https://huggingface.co/org/tiny/resolve/%s/%s" % (REVISION, name) for name in bodies}
        node._list_repository = lambda repo_id: dict(revision=REVISION, files=[
            dict(path=name, url=urls[name], size=len(body), digest=digest(body)) for name, body in bodies.items()])
        jobs = _FakeJobs({urls[name]: body for name, body in bodies.items() if name not in stored})
        self.configure(jobs, reader=self.store({digest(bodies[name]): bodies[name] for name in stored}))
        with self.assertLogs("abstraction", "INFO"):
            server.install_snapshot("org/tiny", self.local_dir)
        path = service.snapshot_record_path(self.local_dir)
        # A repository every file of which a store supplied never reaches the
        # service, so it retains no snapshot record; its reuse record is all
        # there is.
        if not os.path.exists(path):
            return jobs_view, jobs, dict(files={})
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
        return jobs_view, jobs, record

    def test_a_repository_is_one_row_with_its_files_under_it(self):
        jobs_view, _, record = self.snapshot_install()
        keys = {path: entry["key"] for path, entry in record["files"].items()}
        rows = self.rows(jobs_view, [view_snapshot(keys["b.bin"], "complete", done=8, total=8),
                                     view_snapshot(keys["unet/c.bin"], "running", done=2, total=10)])
        self.assertEqual(len(rows), 1, "the repository's files did not group")
        row = rows[0]
        self.assertEqual((row["kind"], row["model"], row["destination"]),
                         ("repository", "org/tiny@" + REVISION, self.local_dir))
        self.assertEqual(row["repository"], dict(repo="org/tiny", revision=REVISION))
        self.assertEqual([child["model"] for child in row["children"]], ["a.bin", "b.bin", "unet/c.bin"])
        self.assertEqual([child["state"] for child in row["children"]], ["complete", "complete", "running"])
        self.assertEqual([child["source"] for child in row["children"]], ["reused", "fetched", "fetched"])
        self.assertEqual(row["children"][0]["store"], "oa-local-store")
        self.assertEqual(row["children"][2]["destination"], os.path.join(self.local_dir, "unet", "c.bin"))
        # The repository's own state and progress are its files' own.
        self.assertEqual((row["state"], row["progress"]), ("running", dict(done=10, total=18)))
        self.assertEqual((row["source"], row["reused"], row["files"]), ("mixed", 1, 3))
        self.assertEqual(row["reason"], "1 of 3 files reused from a store")

    def test_a_repository_every_file_of_which_was_reused_is_still_one_row(self):
        jobs_view, jobs, _ = self.snapshot_install(stored=("a.bin", "b.bin", "unet/c.bin"))
        self.assertEqual(jobs.submits, [], "a fully stored repository submitted work")
        rows = self.rows(jobs_view)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["kind"], rows[0]["source"], rows[0]["state"]), ("repository", "reused", "complete"))
        self.assertEqual(rows[0]["store"], "oa-local-store")
        self.assertEqual(len(rows[0]["children"]), 3)

    def reopen(self):
        """What reopening ComfyUI leaves: nothing imported, the records on disk."""
        for name in [n for n in sys.modules if n == "abstraction_downloads"
                     or n.startswith("abstraction_downloads.")]:
            del sys.modules[name]
        return self.view()

    def test_reopening_comfyui_lists_the_same_rows_before_any_new_download(self):
        jobs_view, _, key = self.fetched_model()
        before = self.rows(jobs_view, [view_snapshot(key, "running", done=3, total=10)])
        _, _, _, reopened = self.reopen()
        # No download, no configured service: the caller records alone.
        after = self.rows(reopened, [view_snapshot(key, "running", done=3, total=10)])
        self.assertEqual([row["id"] for row in after], [row["id"] for row in before])
        self.assertEqual(after[0]["destination"], before[0]["destination"])
        self.assertEqual((after[0]["state"], after[0]["percent"], after[0]["identity"]),
                         ("running", 30, dict(key=key, history_epoch="epoch-1", attempt=0)))
        empty = self.rows(reopened)
        self.assertEqual(len(empty), 1, "a restart lost the retained operation")
        self.assertIn("the service did not list this operation", empty[0]["reason"])

    def test_cancel_reaches_the_service_with_the_retained_identity(self):
        jobs_view, jobs, key = self.fetched_model()
        row = self.rows(jobs_view, [view_snapshot(key, "running")])[0]
        self.assertEqual(jobs_view.cancel(row["id"]), dict(outcome="requested"))
        self.assertEqual(jobs.cancelled, [(key, "epoch-1", 0)])

    def test_cancel_of_a_repository_asks_for_every_file(self):
        jobs_view, jobs, record = self.snapshot_install()
        row = self.rows(jobs_view)[0]
        result = jobs_view.cancel(row["id"])
        self.assertEqual(result["outcome"], "requested")
        self.assertEqual(sorted(result["files"]), ["b.bin", "unet/c.bin"])
        self.assertEqual(sorted(k for k, _, _ in jobs.cancelled),
                         sorted(record["files"][p]["key"] for p in ("b.bin", "unet/c.bin")))

    def test_cancel_of_a_reused_file_has_no_operation_to_cancel(self):
        _, server, _, jobs_view = self.view()
        self.configure(_FakeJobs({}), resolver=_FakeResolver({"model.safetensors": (self.digest, len(self.BODY))}),
                       reader=self.store({self.digest: self.BODY}))
        with self.assertLogs("abstraction", "INFO"):
            server.do_install_model(self.URL, self.models, "model.safetensors")
        row = self.rows(jobs_view)[0]
        self.assertEqual(jobs_view.cancel(row["id"])["outcome"], "unsupported")

    def test_retry_redelivers_the_model_through_the_same_retained_request(self):
        jobs_view, jobs, key = self.fetched_model()
        final = os.path.join(self.models, "model.safetensors")
        os.unlink(final)
        row = self.rows(jobs_view, [view_snapshot(key, "failed")])[0]
        self.assertEqual(jobs_view.retry(row["id"]), dict(outcome="started", scope="file"))
        self.assertTrue(self.wait_for(lambda: (jobs_view.retry_state(row["id"]) or {}).get("state") == "finished"),
                        jobs_view.retry_state(row["id"]))
        with open(final, "rb") as f:
            self.assertEqual(f.read(), self.BODY)
        self.assertEqual(jobs.reconciles, [key], "the retry did not reconcile the retained identity")
        self.assertEqual(jobs.submits, [key], "the retry submitted a second request")
        self.assertEqual(self.rows(jobs_view, [view_snapshot(key, "failed")])[0]["retry"],
                         dict(state="finished", detail=""))

    def test_retry_of_a_repository_redelivers_its_pending_files(self):
        jobs_view, jobs, record = self.snapshot_install()
        os.unlink(os.path.join(self.local_dir, "b.bin"))
        row = self.rows(jobs_view)[0]
        self.assertEqual(jobs_view.retry(row["id"]), dict(outcome="started", scope="repository"))
        self.assertTrue(self.wait_for(lambda: (jobs_view.retry_state(row["id"]) or {}).get("state") == "finished"),
                        jobs_view.retry_state(row["id"]))
        self.assertTrue(os.path.exists(os.path.join(self.local_dir, "b.bin")))
        self.assertEqual(jobs.reconciles, [record["files"]["b.bin"]["key"]])

    def test_an_id_no_record_names_is_unknown(self):
        _, _, _, jobs_view = self.view()
        self.assertEqual(jobs_view.retry("f0123456789abcdef")["outcome"], "unknown")
        self.assertEqual(jobs_view.cancel("f0123456789abcdef")["outcome"], "unknown")

    def test_an_unchanged_cursor_answers_no_rows(self):
        jobs_view, _, key = self.fetched_model()
        listing = dict(outcome="page", complete=True, pages=1,
                       operations=[view_snapshot(key, "running", done=3, total=10)])
        first = jobs_view.enrich(dict(listing))
        self.assertTrue(first["changed"])
        self.assertEqual(len(first["rows"]), 1)
        again = jobs_view.enrich(dict(listing), first["cursor"])
        self.assertEqual((again["changed"], again["rows"], again["cursor"]), (False, [], first["cursor"]))
        moved = jobs_view.enrich(dict(listing, operations=[view_snapshot(key, "running", done=9, total=10)]),
                                 first["cursor"])
        self.assertTrue(moved["changed"])
        self.assertEqual(moved["rows"][0]["percent"], 90)

    def test_the_route_serves_the_rows_and_the_two_actions(self):
        try:
            from aiohttp import web
            from aiohttp.test_utils import TestClient, TestServer
        except ImportError:
            self.skipTest("aiohttp is not installed in this interpreter")
        import asyncio
        jobs_view, _, key = self.fetched_model()
        from abstraction_downloads import oa_inventory
        os.environ[oa_inventory.ENV] = "service"
        page = SimpleNamespace(outcome="page", snapshots=[], next="", complete=True)
        inventory = SimpleNamespace(list_work=lambda cursor, limit: page)
        oa_inventory.configure(lambda deadline: SimpleNamespace(resolve_job_inventory=lambda **kw: inventory))
        self.addCleanup(oa_inventory.configure)
        routes = web.RouteTableDef()
        self.assertEqual(oa_inventory.install(routes).code, "serving")

        async def go():
            app = web.Application()
            app.add_routes(routes)
            async with TestClient(TestServer(app)) as client:
                listing = await (await client.get("/abstraction/jobs")).json()
                row = listing["rows"][0]
                unchanged = await (await client.get("/abstraction/jobs?since=" + listing["cursor"])).json()
                selection = {"identities": [row["identity"]]}
                cancelled = await (await client.post("/abstraction/jobs/%s/cancel" % row["id"], json=selection)).json()
                missing = await client.post("/abstraction/jobs/f0123456789abcdef/cancel", json=selection)
                return listing, row, unchanged, cancelled, missing.status
        listing, row, unchanged, cancelled, missing = asyncio.run(go())
        self.assertEqual(row["model"], "model.safetensors")
        self.assertEqual(listing["outcome"], "page")
        self.assertEqual((unchanged["changed"], unchanged["rows"]), (False, []))
        self.assertEqual(cancelled["outcome"], "requested")
        self.assertEqual(missing, 404)

    def test_the_route_answers_one_explanation_instead_of_a_bare_404(self):
        """C1: WEB_DIRECTORY loads the sidebar tab whether or not this route is
        serving. GET /abstraction/jobs is registered either way, so a tab that
        cannot list operations yet reads one explanation from the route itself
        instead of ComfyUI's bare, unrouted 404."""
        try:
            from aiohttp import web
            from aiohttp.test_utils import TestClient, TestServer
        except ImportError:
            self.skipTest("aiohttp is not installed in this interpreter")
        import asyncio
        self.view()
        from abstraction_downloads import oa_inventory

        async def fetch(routes):
            app = web.Application()
            app.add_routes(routes)
            async with TestClient(TestServer(app)) as client:
                response = await client.get("/abstraction/jobs")
                return response.status, await response.json()

        os.environ.pop(oa_inventory.ENV, None)
        routes = web.RouteTableDef()
        off = oa_inventory.install(routes)
        self.assertEqual(off.code, oa_inventory.OFF)
        status, body = asyncio.run(fetch(routes))
        self.assertEqual(status, 404)
        self.assertEqual(body, dict(missing=True, detail=off.detail))
        self.assertIn(oa_inventory.ENV, body["detail"])

        os.environ[oa_inventory.ENV] = "sometimes"
        routes = web.RouteTableDef()
        misconfigured = oa_inventory.install(routes)
        self.assertEqual(misconfigured.code, oa_inventory.INVALID_CONFIGURATION)
        status, body = asyncio.run(fetch(routes))
        self.assertEqual(status, 404)
        self.assertEqual(body, dict(missing=True, detail=misconfigured.detail))
        self.assertNotEqual(body["detail"], off.detail)


class _FakeSink:
    def __init__(self):
        self.records = []
        self.fail = False
        self.gate = None

    def write(self, record):
        # A transport that logs while writing must not recurse into the handler.
        logging_module().getLogger("abstraction.ipc.fixture").warning("transport logged during a write")
        if self.gate is not None:
            self.gate.wait(20)
        if self.fail:
            raise ConnectionError("sink endpoint closed")
        self.records.append(record)


class _FakeMachine:
    def __init__(self, sink=None, error=None):
        self.sink, self.error, self.scope = sink, error, None

    def resolve_log(self, *, guarantees=(), scope="any"):
        if self.error is not None:
            raise self.error
        self.scope = scope
        return self.sink


def logging_module():
    import logging
    return logging


class _Capture(logging_module().Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class LoggingAdoptionTest(unittest.TestCase):
    """ABSTRACTION_COMFYUI_LOGGING=service: ComfyUI's records reach the OA logging
    service, and ComfyUI logs as usual whatever OpenAbstractions does."""

    def setUp(self):
        import importlib.util
        self._path = list(sys.path)
        self._modules = set(sys.modules)
        self.addCleanup(self._restore)
        for layer in ("abstraction-logging", "abstraction-facade", "abstraction-identity"):
            path = os.path.join(LAYERS, layer, "py")
            if os.path.isdir(path):
                sys.path.insert(0, path)
        try:
            import abstraction.logging as rec
            import abstraction.logging.handler  # noqa: F401
            import abstraction.facade  # noqa: F401
        except ImportError:
            self.skipTest("the abstraction-logging and facade Python packages are not importable beside this node")
        self.codec = rec
        spec = importlib.util.spec_from_file_location("oa_logging_under_test", os.path.join(NODE, "oa_logging.py"))
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self._env = os.environ.get(self.module.ENV)
        self.root = logging_module().getLogger()
        self._handlers = list(self.root.handlers)
        self.capture = _Capture()
        logging_module().getLogger("abstraction").addHandler(self.capture)
        self.addCleanup(lambda: logging_module().getLogger("abstraction").removeHandler(self.capture))
        # ComfyUI's own handler, beside the service's.
        self.console = _Capture()
        self.log = logging_module().getLogger("oa-test.records")
        self.log.setLevel(logging_module().INFO)
        self.log.addHandler(self.console)
        self.addCleanup(lambda: (self.log.removeHandler(self.console), self.log.setLevel(logging_module().NOTSET)))

    def _restore(self):
        module = getattr(self, "module", None)
        if module is not None and module._handler is not None:
            self.root.removeHandler(module._handler)
            module._handler.close(1.0)
        if module is not None:
            if self._env is None:
                os.environ.pop(module.ENV, None)
            else:
                os.environ[module.ENV] = self._env
        for name in set(sys.modules) - self._modules:
            del sys.modules[name]
        sys.path[:] = self._path

    def install(self, value, machine=None, limiter=None):
        if value is None:
            os.environ.pop(self.module.ENV, None)
        else:
            os.environ[self.module.ENV] = value
        if machine is not None:
            self.module.configure(machine if callable(machine) else (lambda: machine))
        return self.module.install(limiter=limiter)

    def handler(self):
        from abstraction.logging.handler import ServiceHandler
        attached = [h for h in self.root.handlers if isinstance(h, ServiceHandler)]
        self.assertEqual(len(attached), 1)
        return attached[0]

    def eventually(self, what, condition, timeout=10.0):
        limit = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > limit:
                self.fail("timed out waiting for " + what)
            time.sleep(0.005)

    def test_unset_is_off_and_attaches_nothing(self):
        status = self.install(None, _FakeMachine(_FakeSink()))
        self.assertEqual((status.enabled, status.attached, status.code), (False, False, self.module.OFF))
        self.assertEqual(self.root.handlers, self._handlers)

    def test_an_unsupported_value_writes_one_line_and_attaches_nothing(self):
        status = self.install("file", _FakeMachine(_FakeSink()))
        self.assertEqual((status.enabled, status.attached, status.code),
                         (True, False, self.module.INVALID_CONFIGURATION))
        self.assertIn("supported values are unset and \"service\"", status.detail)
        self.assertEqual(len([m for m in self.capture.messages if "ComfyUI logs as usual" in m]), 1)
        self.assertEqual(self.root.handlers, self._handlers)

    def test_install_and_logging_never_wait_on_the_service(self):
        """Resolution happens on the delivery thread: a runtime that takes its time
        holds neither ComfyUI's start nor a log statement."""
        release = threading.Event()
        self.addCleanup(release.set)

        def slow():
            release.wait(20)
            return _FakeMachine(_FakeSink())
        started = time.monotonic()
        status = self.install("service", slow)
        self.log.warning("logged while the service resolves")
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual((status.attached, status.code), (True, self.module.ATTACHED))
        self.assertEqual(self.console.messages, ["logged while the service resolves"])

    def test_an_absent_service_fails_open_and_delivers_once_it_answers(self):
        """ComfyUI logs as usual while OpenAbstractions is away; the records queue and
        reach the service once it answers, behind one gap record [LOG-S12]."""
        from abstraction.logging.handler import GAP_MESSAGE
        sink = _FakeSink()
        machine = _FakeMachine(sink, error=RuntimeError("service resolution: runtime_unavailable"))
        status = self.install("service", machine)
        self.assertEqual((status.attached, status.code), (True, self.module.ATTACHED))
        self.log.warning("while OpenAbstractions is away")
        self.eventually("the unavailable status", lambda: self.module.status().code == self.module.SERVICE_UNAVAILABLE)
        status = self.module.status()
        self.assertIn("runtime_unavailable", status.detail)
        self.assertEqual((status.attached, status.written, status.queued), (True, 0, 1))
        self.assertEqual(self.console.messages, ["while OpenAbstractions is away"])
        self.assertEqual(len([m for m in self.capture.messages if "OA LOGGING SERVICE UNAVAILABLE" in m]), 1,
                         self.capture.messages)
        machine.error = None
        self.eventually("the delivery", lambda: self.module.status().written == 1)
        self.assertEqual([r.msg for r in sink.records], [GAP_MESSAGE, "while OpenAbstractions is away"])
        self.assertEqual(self.module.status().code, self.module.ATTACHED)

    def test_the_logging_package_handler_is_attached_for_comfyui(self):
        sink = _FakeSink()
        machine = _FakeMachine(sink)
        status = self.install("service", machine)
        self.assertEqual((status.attached, status.code), (True, self.module.ATTACHED))
        handler = self.handler()
        self.log.warning("disk nearly full")
        handler.flush(5.0)
        self.assertEqual([r.msg for r in sink.records], ["disk nearly full"])
        from abstraction.facade import Scope
        self.assertIsInstance(machine.scope, Scope)
        self.assertEqual(machine.scope, "local")
        writer = sink.records[0].identity[0]
        self.assertEqual((writer.by, writer.verified, writer.hop, writer.program), ("self", False, 0, "ComfyUI"))
        self.assertEqual(sink.records[0].attrs["logger"], "oa-test.records")
        self.codec.encode(sink.records[0])
        self.assertEqual(self.module.status().written, 1)
        self.install(None)
        from abstraction.logging.handler import ServiceHandler
        self.assertFalse([h for h in self.root.handlers if isinstance(h, ServiceHandler)])

    def test_credentials_leave_redacted_and_comfyui_keeps_its_own_text(self):
        sink = _FakeSink()
        self.install("service", _FakeMachine(sink))
        try:
            raise ValueError("Authorization: Bearer abc.DEF-1")
        except ValueError:
            self.log.exception("GET https://user:pa55word@example.com/v1 failed with token=abc123")
        self.handler().flush(5.0)
        record = sink.records[0]
        self.assertEqual(record.msg, "GET https://[redacted]@example.com/v1 failed with token=[redacted]")
        self.assertIn("Traceback (most recent call last):\n", record.attrs["exception"])
        self.assertIn("ValueError: Authorization: [redacted]", record.attrs["exception"])
        self.assertNotIn("abc.DEF-1", record.attrs["exception"])
        self.assertEqual(self.console.messages, ["GET https://user:pa55word@example.com/v1 failed with token=abc123"])

    def test_a_full_queue_drops_the_newest_and_says_so_once(self):
        """[LOG-S12] The queue holds 1024 records counting the one being delivered;
        ComfyUI keeps every record."""
        sink = _FakeSink()
        sink.gate = threading.Event()
        self.addCleanup(sink.gate.set)
        self.install("service", _FakeMachine(sink), limiter=self.module._Limiter(rate=1e9, burst=10 ** 9))
        handler = self.handler()
        self.log.warning("record 0")
        self.eventually("the first delivery", lambda: handler.counts().queued == 1 and handler._writer._current)
        for n in range(1, 1100):
            self.log.warning("record %d", n)
        status = self.module.status()
        self.assertEqual((status.queued, status.dropped), (1024, 1100 - 1024))
        self.assertEqual(len([m for m in self.capture.messages if "LOGGING QUEUE FULL" in m]), 1)
        self.assertEqual(len(self.console.messages), 1100)
        sink.gate.set()
        self.eventually("the queue", lambda: self.module.status().written == 1024)
        self.assertEqual([r.msg for r in sink.records], ["record %d" % n for n in range(1024)])

    def test_a_noisy_logger_is_held_back_and_counted(self):
        now = [0.0]
        sink = _FakeSink()
        self.install("service", _FakeMachine(sink), limiter=self.module._Limiter(rate=1.0, burst=3, clock=lambda: now[0]))
        for n in range(5):
            self.log.warning("noisy %d", n)
        now[0] = 2.0
        self.log.warning("after the pause")
        self.handler().flush(5.0)
        self.assertEqual([r.msg for r in sink.records],
                         ["noisy 0", "noisy 1", "noisy 2",
                          "[abstraction] logger 'oa-test.records' writes over 1 records a second; "
                          "OpenAbstractions gets its records again after a pause",
                          "2 records from this logger were held back by the rate limit", "after the pause"])
        self.assertEqual(len([m for m in self.capture.messages if "'oa-test.records' writes over" in m]), 1)
        self.assertEqual(len(self.console.messages), 6)

    def test_a_service_outage_stays_attached_warns_once_and_recovers_once(self):
        """[LOG-S12, LOG-S13] A runtime that goes away after attachment does not stop ComfyUI:
        the handler stays on the root logger, warns once, rebinds through the same factory,
        and delivers the records logged during the outage behind one gap record."""
        from abstraction.logging.handler import GAP_MESSAGE
        sink = _FakeSink()
        machine = _FakeMachine(sink)
        resolutions = []
        original = machine.resolve_log

        def counted(**options):
            resolutions.append(options)
            return original(**options)
        machine.resolve_log = counted
        self.install("service", machine)
        handler = self.handler()
        # The recovery report is INFO; the adapter's own lines come from the delivery
        # thread and reach ComfyUI's handlers only.
        adapter_log = logging_module().getLogger("abstraction")
        adapter_log.setLevel(logging_module().INFO)
        self.addCleanup(lambda: adapter_log.setLevel(logging_module().NOTSET))
        log = logging_module().getLogger("oa-test.outage")
        log.setLevel(logging_module().INFO)
        self.addCleanup(lambda: log.setLevel(logging_module().NOTSET))
        log.warning("before the outage")
        handler.flush(5.0)
        sink.fail = True
        log.warning("first during the outage")
        log.warning("second during the outage")
        self.eventually("the failure warning", lambda: any("LOGGING SERVICE UNREACHABLE (write_failed)" in m for m in self.capture.messages))
        self.eventually("a retry", lambda: len(resolutions) >= 3)
        status = self.module.status()
        self.assertEqual((status.attached, status.code, status.failed, status.written, status.queued),
                         (True, self.module.WRITE_FAILED, 0, 1, 2))
        self.assertIn(handler, self.root.handlers)
        sink.fail = False
        self.eventually("the recovery", lambda: self.module.status().written == 3)
        status = self.module.status()
        self.assertEqual((status.code, status.written, status.failed, status.dropped, status.queued),
                         (self.module.ATTACHED, 3, 0, 0, 0))
        self.assertEqual([r.msg for r in sink.records],
                         ["before the outage", GAP_MESSAGE, "first during the outage", "second during the outage"])
        warnings = [m for m in self.capture.messages if "LOGGING SERVICE UNREACHABLE (write_failed)" in m]
        recoveries = [m for m in self.capture.messages if "reaches the OA logging service again" in m]
        self.assertEqual((len(warnings), len(recoveries)), (1, 1), self.capture.messages)
        self.assertTrue(all(options.get("scope") == "local" for options in resolutions), resolutions)


def _checkout_commit(checkout):
    git = os.path.join(checkout, ".git")
    with open(os.path.join(git, "HEAD"), encoding="utf-8") as f:
        head = f.read().strip()
    if not head.startswith("ref: "):
        return head, None
    ref = head[len("ref: "):]
    loose = os.path.join(git, *ref.split("/"))
    if os.path.isfile(loose):
        with open(loose, encoding="utf-8") as f:
            return f.read().strip(), ref
    with open(os.path.join(git, "packed-refs"), encoding="utf-8") as f:
        for line in f:
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref:
                return parts[0], ref
    raise AssertionError("unresolved ref %s in %s" % (ref, checkout))


@unittest.skipUnless(os.environ.get("OA_COMFYUI_FORKS"),
                     "set OA_COMFYUI_FORKS to the directory holding the pinned upstream checkouts")
class UpstreamPinTest(unittest.TestCase):
    """The stand-ins reproduce these exact upstream facts at the pinned commits."""

    def read(self, pin, *path):
        checkout = os.path.join(os.environ["OA_COMFYUI_FORKS"], PINS[pin]["checkout"])
        with open(os.path.join(checkout, *path), encoding="utf-8") as f:
            return re.sub(r"\s+", " ", f.read())

    def assert_contains(self, pin, path, *needles):
        text = self.read(pin, *path.split("/"))
        for needle in needles:
            self.assertIn(re.sub(r"\s+", " ", needle), text, "%s %s" % (PINS[pin]["checkout"], path))

    def test_checkouts_are_at_the_pinned_commits(self):
        for pin in ("core", "v4", "v3"):
            commit, _ = _checkout_commit(os.path.join(os.environ["OA_COMFYUI_FORKS"], PINS[pin]["checkout"]))
            self.assertEqual(commit, PINS[pin]["commit"], pin)

    def test_core_installs_and_starts_manager_4(self):
        self.assert_contains("core", "manager_requirements.txt", PINS["core"]["requirement"])
        main = self.read("core", "main.py")
        self.assertLess(main.index("comfyui_manager.start()"), main.index("nodes.init_extra_nodes("))
        self.assertIn("comfyui_manager.__file__.endswith('__init__.py')", main)
        self.assert_contains("v4", "comfyui_manager/__init__.py",
                             "from .legacy import manager_server", "from .glob import manager_server",
                             "if args.enable_manager_legacy_ui:", "if 'comfyui-manager' in dir_name:")

    def test_manager_4_layout(self):
        self.assert_contains("v4", "pyproject.toml", 'version = "%s"' % PINS["v4"]["version"])
        self.assert_contains("v4", "comfyui_manager/common/manager_downloader.py",
                             "def download_url(model_url: str, model_dir: str, filename: str):",
                             "def download_url_with_agent(url, save_path):",
                             "def download_repo_in_bytes(repo_id, local_dir):")
        self.assert_contains("v4", "comfyui_manager/glob/manager_server.py",
                             "from comfyui_manager.common.manager_downloader import ( download_url, download_url_with_agent, )",
                             'download_url(model_url, model_dir, filename=json_data["filename"])',
                             "res = download_url_with_agent(model_url, model_path)",
                             "from ..common import manager_downloader",
                             'if json_data["filename"] == "<huggingface>":',
                             "manager_downloader.download_repo_in_bytes( repo_id=model_url, local_dir=model_path )")
        self.assert_contains("v4", "comfyui_manager/legacy/manager_server.py",
                             "from comfyui_manager.common.manager_downloader import download_url, download_url_with_agent",
                             "download_url(model_url, model_dir, filename=json_data['filename'])",
                             "from ..common import manager_downloader",
                             "manager_downloader.download_repo_in_bytes(repo_id=model_url, local_dir=model_path)")
        for ui in ("glob", "legacy"):
            self.assert_contains("v4", "comfyui_manager/%s/manager_core.py" % ui,
                                 "from ..common import manager_downloader",
                                 'archive_name = f"CNR_temp_{str(uuid.uuid4())}.zip"',
                                 "manager_downloader.download_url(node_info.download_url, get_default_custom_nodes_path(), archive_name)")

    def test_manager_3_layout(self):
        self.assert_contains("v3", "pyproject.toml", 'version = "%s"' % PINS["v3"]["version"])
        self.assert_contains("v3", "glob/manager_server.py",
                             "from manager_downloader import download_url, download_url_with_agent",
                             'core.js_path = os.path.join(core.comfy_path, "web", "extensions")',
                             "download_url(model_url, model_dir, filename=json_data['filename'])",
                             "download_url(url, core.get_default_custom_nodes_path(), filename)",
                             "download_url(url, path, filename)",
                             "manager_downloader.download_repo_in_bytes(repo_id=model_url, local_dir=model_path)")
        self.assert_contains("v3", "glob/manager_downloader.py", "def download_repo_in_bytes(repo_id, local_dir):")
        self.assert_contains("v3", "glob/manager_core.py", "import manager_downloader",
                             "manager_downloader.download_url(node_info.download_url, get_default_custom_nodes_path(), archive_name)")


class _Ask:
    """One YieldRecord as the service answers it."""

    def __init__(self, holder, answer, amount=0, took_ms=0):
        self.holder, self.answer, self.amount, self.took_ms = holder, answer, amount, took_ms


class _ComfyInterrupt(Exception):
    """Test stand-in for ComfyUI's InterruptProcessingException."""


def _lease_change_result(applied):
    from abstraction.resource import LeaseChange, LeaseChangeResult, ResourceCallOutcome
    return LeaseChangeResult(outcome=ResourceCallOutcome.OK, change=LeaseChange(applied=applied))


class _FakeLeases:
    """abstraction.resource/leases@1 with a decided answer and a call log."""

    def __init__(self, outcome="acquired", asked=(), lease="lease-0001"):
        self.outcome, self.asked, self.lease = outcome, list(asked), lease
        self.next_lease = 0
        self.acquires, self.renewals, self.releases = [], [], []
        self.cancellations = []
        self.renew_outcomes = {}

    def acquire(self, resource, amount, wait_ms):
        self.acquires.append((resource, amount, wait_ms))
        self.next_lease += 1
        lease_id = self.lease if self.next_lease == 1 else "lease-%04d" % self.next_lease
        lease = SimpleNamespace(id=lease_id, resource=resource, amount=amount) if self.outcome == "acquired" else None
        return SimpleNamespace(outcome=self.outcome, lease=lease, asked=list(self.asked))

    def renew(self, lease):
        self.renewals.append(lease)
        return _lease_change_result(self.renew_outcomes.get(lease, True))

    def release(self, lease):
        self.releases.append(lease)
        return _lease_change_result(True)


class CardLeaseTest(unittest.TestCase):
    """ABSTRACTION_COMFYUI_CARD=service: the ask before a load, the release on an
    unload, and a refusal reaching the render as a typed error."""

    def setUp(self):
        import importlib.util
        self._path = list(sys.path)
        self._modules = set(sys.modules)
        self._env = dict(os.environ)
        self.addCleanup(self._restore)
        for layer in ("abstraction-resource", "abstraction-facade"):
            path = os.path.join(LAYERS, layer, "py")
            if os.path.isdir(path):
                sys.path.insert(0, path)
        try:
            import abstraction.resource  # noqa: F401
            import abstraction.facade  # noqa: F401
        except ImportError:
            self.skipTest("the abstraction-resource Python package is not importable beside this node")
        spec = importlib.util.spec_from_file_location("oa_card_under_test", os.path.join(NODE, "oa_card.py"))
        self.card = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.card)

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.loaded, self.unloaded, self.order = [], [], []

        def load_torch_file(ckpt, *a, **k):
            self.order.append("load")
            self.loaded.append(ckpt)
            return "weights"

        def unload_all_models(*a, **k):
            self.order.append("unload")
            self.unloaded.append(True)

        def interrupt_current_processing(value=True):
            self.order.append("interrupt")
            return value

        self.utils = types.ModuleType("comfy.utils")
        self.utils.load_torch_file = load_torch_file
        self.management = types.ModuleType("comfy.model_management")
        self.management.unload_all_models = unload_all_models
        self.management.interrupt_current_processing = interrupt_current_processing
        self.original_load, self.original_unload = load_torch_file, unload_all_models

    def _restore(self):
        card = getattr(self, "card", None)
        if card is not None:
            card.uninstall()
            card.configure()
        os.environ.clear()
        os.environ.update(self._env)
        for name in set(sys.modules) - self._modules:
            del sys.modules[name]
        sys.path[:] = self._path

    def _raise_comfy_interrupt(self):
        raise _ComfyInterrupt("ComfyUI interrupted")

    def weights(self, name="model.safetensors", size=None):
        """A file of the given size. Truncation writes no bytes."""
        path = os.path.join(self.tmp.name, name)
        with open(path, "wb") as handle:
            handle.truncate(self.card.FLOOR_BYTES if size is None else size)
        return path

    def install(self, value="service", leases=None, **env):
        if value is None:
            os.environ.pop(self.card.ENV, None)
        else:
            os.environ[self.card.ENV] = value
        for name, setting in env.items():
            os.environ[name] = setting
        self.leases = leases if leases is not None else _FakeLeases()
        def machine_factory(deadline, cancellation):
            self.leases.cancellations.append(cancellation)
            return SimpleNamespace(resolve_resource_leases=lambda **kw: self.leases)
        self.card.configure(machine_factory)
        return self.card.install(self.utils, self.management)

    def test_waiting_word_prints_the_sentence_every_client_prints(self):
        # The rows are abstraction-job's, which its Go test and the OpenCode
        # adopter's waiting.test.mjs read too (JOB-A15, RES-L8). A published
        # checkout of this node has no abstraction-job beside it; the private
        # workspace runs the comparison.
        import json
        table = os.path.join(REPO, "openabstractions-flat", "abstraction-job", "testdata", "waiting-sentences.json")
        if not os.path.isfile(table):
            self.skipTest("abstraction-job is not beside this node")
        with open(table, encoding="utf-8") as f:
            rows = json.load(f)["sentences"]
        self.assertTrue(rows, "the sentence table has rows")
        for row in rows:
            self.assertEqual(self.card.waiting_sentence(row["word"]), row["sentence"], row["word"])

    def test_unset_leaves_comfyui_unchanged(self):
        status = self.install(None)
        self.assertEqual((status.enabled, status.code), (False, self.card.OFF))
        self.assertIs(self.utils.load_torch_file, self.original_load)
        self.assertIs(self.management.unload_all_models, self.original_unload)

    def test_an_unsupported_value_holds_nothing_and_wraps_nothing(self):
        status = self.install("on")
        self.assertEqual((status.enabled, status.code), (True, self.card.INVALID_CONFIGURATION))
        self.assertIn('supported values are unset and "service"', status.detail)
        self.assertIs(self.utils.load_torch_file, self.original_load)

    def test_a_lease_is_taken_before_the_weights_are_read(self):
        leases = _FakeLeases()
        granted = leases.acquire
        leases.acquire = lambda r, a, w: (self.order.append("acquire"), granted(r, a, w))[1]
        self.assertEqual(self.install(leases=leases).code, self.card.HOLDING)
        self.assertIsNot(self.utils.load_torch_file, self.original_load)
        self.assertEqual(self.utils.load_torch_file(self.weights()), "weights")
        self.assertEqual(self.order, ["acquire", "load"])

    def test_the_ask_is_what_the_weights_weigh(self):
        self.assertEqual(self.install().code, self.card.HOLDING)
        path = self.weights(size=self.card.FLOOR_BYTES)
        self.assertEqual(self.utils.load_torch_file(path), "weights")
        self.assertEqual(self.leases.acquires, [(self.card.RESOURCE, self.card.FLOOR_BYTES, 60000)])
        self.assertEqual(self.loaded, [path])
        holding = self.card.held()
        self.assertEqual((holding.lease, holding.amount), ("lease-0001", self.card.FLOOR_BYTES))
        self.assertEqual(holding.leases, ("lease-0001",))
        self.assertEqual(list(holding.files), [os.path.realpath(path)])

    def test_the_reserve_is_the_floor_of_the_hold(self):
        reserve = 4 * self.card.FLOOR_BYTES
        self.install(**{self.card.RESERVE_ENV: str(reserve)})
        self.utils.load_torch_file(self.weights(size=self.card.FLOOR_BYTES))
        self.assertEqual(self.leases.acquires, [(self.card.RESOURCE, reserve, 60000)])
        self.assertEqual(self.card.held().amount, reserve)

    def test_a_second_file_grows_by_delta_while_retaining_the_first_grant(self):
        self.install()
        first, second = self.weights("a.safetensors"), self.weights("b.safetensors")
        self.utils.load_torch_file(first)
        self.utils.load_torch_file(second)
        self.assertEqual(self.leases.acquires, [(self.card.RESOURCE, self.card.FLOOR_BYTES, 60000),
                                                (self.card.RESOURCE, self.card.FLOOR_BYTES, 60000)])
        self.assertEqual(self.leases.releases, [])
        self.assertEqual(len(self.card.held().files), 2)
        self.assertEqual(self.card.held().amount, 2 * self.card.FLOOR_BYTES)
        self.assertEqual(self.card.held().leases, ("lease-0001", "lease-0002"))

    def test_refused_growth_preserves_the_old_files_and_grant(self):
        self.install()
        first, second = self.weights("a.safetensors"), self.weights("b.safetensors")
        self.utils.load_torch_file(first)
        self.leases.outcome = "holders_refused"
        with self.assertRaises(self.card.CardRefused):
            self.utils.load_torch_file(second)
        self.assertEqual(self.card.held().leases, ("lease-0001",))
        self.assertEqual(list(self.card.held().files), [os.path.realpath(first)])
        self.assertEqual(self.leases.releases, [])
        self.assertEqual(self.loaded, [first])

    def test_uncanceled_renewal_failure_releases_known_delta_and_preserves_cause(self):
        self.install()
        first, second = self.weights("a.safetensors"), self.weights("b.safetensors")
        self.utils.load_torch_file(first)
        service_error = RuntimeError("lease renewal transport disconnected")
        original_renew = self.leases.renew

        def fail_base_renew(lease):
            if lease == "lease-0001":
                raise service_error
            return original_renew(lease)

        self.leases.renew = fail_base_renew
        with self.assertRaises(self.card.CardUnavailable) as caught:
            self.utils.load_torch_file(second)
        self.assertIs(caught.exception.__cause__, service_error)
        self.assertEqual(self.leases.releases, ["lease-0002"])
        self.assertEqual(self.card.held().leases, ("lease-0001",))
        self.assertEqual(list(self.card.held().files), [os.path.realpath(first)])
        self.assertEqual(self.loaded, [first])

    def test_growth_rechecks_lost_base_and_acquires_only_remaining_amount(self):
        self.install()
        first, second = self.weights("a.safetensors"), self.weights("b.safetensors")
        self.utils.load_torch_file(first)
        self.leases.renew_outcomes["lease-0001"] = False
        self.utils.load_torch_file(second)
        self.assertEqual(self.leases.acquires, [(self.card.RESOURCE, self.card.FLOOR_BYTES, 60000),
                                                (self.card.RESOURCE, self.card.FLOOR_BYTES, 60000),
                                                (self.card.RESOURCE, self.card.FLOOR_BYTES, 60000)])
        self.assertEqual(self.card.held().amount, 2 * self.card.FLOOR_BYTES)
        self.assertEqual(self.card.held().leases, ("lease-0002", "lease-0003"))
        self.assertEqual(self.loaded, [first, second])

    def test_concurrent_model_growth_serializes_deltas_and_keeps_every_file_covered(self):
        self.install()
        first = self.weights("base.safetensors")
        second = self.weights("second.safetensors")
        third = self.weights("third.safetensors")
        self.utils.load_torch_file(first)
        start = threading.Barrier(3)
        errors = []

        def load(path):
            try:
                start.wait(3)
                self.utils.load_torch_file(path)
            except Exception as error:
                errors.append(error)

        workers = [threading.Thread(target=load, args=(path,)) for path in (second, third)]
        for worker in workers:
            worker.start()
        start.wait(3)
        for worker in workers:
            worker.join(5)
        self.assertFalse(any(worker.is_alive() for worker in workers), "concurrent growth did not finish")
        self.assertEqual(errors, [])
        self.assertEqual([amount for _, amount, _ in self.leases.acquires],
                         [self.card.FLOOR_BYTES] * 3)
        holding = self.card.held()
        self.assertEqual(holding.amount, 3 * self.card.FLOOR_BYTES)
        self.assertEqual(set(holding.files), {os.path.realpath(first), os.path.realpath(second),
                                              os.path.realpath(third)})

    def test_wait_and_grant_progress_uses_the_executing_comfy_node(self):
        self.install()
        messages = []
        comfy_execution = types.ModuleType("comfy_execution")
        comfy_execution.__path__ = []
        execution_utils = types.ModuleType("comfy_execution.utils")
        execution_utils.get_executing_context = lambda: SimpleNamespace(node_id="node-7")
        comfy_execution.utils = execution_utils
        prompt_server = SimpleNamespace(send_progress_text=lambda text, node_id: messages.append((text, node_id)))
        server = types.ModuleType("server")
        server.PromptServer = SimpleNamespace(instance=prompt_server)
        sys.modules["comfy_execution"] = comfy_execution
        sys.modules["comfy_execution.utils"] = execution_utils
        sys.modules["server"] = server
        self.utils.load_torch_file(self.weights())
        self.assertEqual([node_id for _, node_id in messages], ["node-7", "node-7"])
        self.assertIn("Waiting for card:0", messages[0][0])
        self.assertIn("capacity granted", messages[1][0])

    def test_interrupt_cancels_only_pending_growth_and_keeps_old_protection(self):
        self.install()
        self.management.throw_exception_if_processing_interrupted = self._raise_comfy_interrupt
        first, second = self.weights("a.safetensors"), self.weights("b.safetensors")
        self.utils.load_torch_file(first)
        waiting, returned = threading.Event(), threading.Event()
        original_acquire = self.leases.acquire

        def wait_for_interrupt(resource, amount, wait_ms):
            waiting.set()
            token = self.leases.cancellations[-1]
            if not token.cancelled.wait(3):
                self.fail("Comfy interrupt did not signal the pending lease call")
            returned.set()
            raise RuntimeError("caller cancelled the framed wait")

        self.leases.acquire = wait_for_interrupt
        errors = []
        worker = threading.Thread(target=lambda: self._capture_load_error(second, errors))
        worker.start()
        self.assertTrue(waiting.wait(3), "growth did not reach the lease service")
        self.management.interrupt_current_processing(True)
        self.assertTrue(returned.wait(3), "the lease call did not return after interruption")
        worker.join(3)
        self.assertFalse(worker.is_alive(), "cancelled loader stayed blocked")
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], _ComfyInterrupt)
        self.assertEqual(self.card.held().leases, ("lease-0001",))
        self.assertEqual(list(self.card.held().files), [os.path.realpath(first)])
        self.assertEqual(self.loaded, [first])
        self.assertEqual(self.leases.releases, [])
        self.leases.acquire = original_acquire

    def test_late_known_grant_is_released_when_comfy_interrupts_before_load(self):
        self.install()
        self.management.throw_exception_if_processing_interrupted = self._raise_comfy_interrupt
        first, second = self.weights("a.safetensors"), self.weights("b.safetensors")
        self.utils.load_torch_file(first)
        waiting, finish = threading.Event(), threading.Event()
        original_acquire = self.leases.acquire

        def late_grant(resource, amount, wait_ms):
            waiting.set()
            if not finish.wait(3):
                self.fail("test did not release the controlled late grant")
            return original_acquire(resource, amount, wait_ms)

        self.leases.acquire = late_grant
        errors = []
        worker = threading.Thread(target=lambda: self._capture_load_error(second, errors))
        worker.start()
        self.assertTrue(waiting.wait(3), "growth did not reach the lease service")
        self.management.interrupt_current_processing(True)
        finish.set()
        worker.join(3)
        self.assertFalse(worker.is_alive(), "late grant loader stayed blocked")
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], _ComfyInterrupt)
        self.assertEqual(self.leases.releases, ["lease-0002"])
        self.assertEqual(self.card.held().leases, ("lease-0001",))
        self.assertEqual(list(self.card.held().files), [os.path.realpath(first)])
        self.assertEqual(self.loaded, [first])

    def _capture_load_error(self, path, errors):
        try:
            self.utils.load_torch_file(path)
        except Exception as error:
            errors.append(error)

    def test_a_file_that_fits_inside_the_lease_already_held_asks_nobody(self):
        self.install(**{self.card.RESERVE_ENV: str(8 * self.card.FLOOR_BYTES)})
        self.utils.load_torch_file(self.weights("a.safetensors"))
        self.utils.load_torch_file(self.weights("b.safetensors"))
        self.assertEqual(len(self.leases.acquires), 1)
        self.assertEqual(self.leases.releases, [])
        self.assertEqual(len(self.card.held().files), 2)

    def test_a_file_under_the_floor_is_loaded_without_asking(self):
        self.install()
        path = self.weights("small.safetensors", size=1024)
        self.assertEqual(self.utils.load_torch_file(path), "weights")
        self.assertEqual(self.leases.acquires, [])
        self.assertEqual(self.loaded, [path])
        self.assertIsNone(self.card.held().lease)

    def test_one_file_loaded_twice_is_one_lease(self):
        self.install()
        path = self.weights()
        self.utils.load_torch_file(path)
        self.utils.load_torch_file(path)
        self.assertEqual(len(self.leases.acquires), 1)
        self.assertEqual(self.loaded, [path, path])

    def test_an_unload_releases_the_lease_and_the_next_load_asks_again(self):
        self.install()
        path = self.weights()
        self.utils.load_torch_file(path)
        self.management.unload_all_models()
        self.assertEqual(self.unloaded, [True])
        self.assertEqual(self.leases.releases, ["lease-0001"])
        self.assertEqual(self.card.held(), (None, 0, {}, ()))
        self.utils.load_torch_file(path)
        self.assertEqual(len(self.leases.acquires), 2)

    def test_holders_refused_stops_the_render_before_the_weights_are_read(self):
        self.install(leases=_FakeLeases("holders_refused",
                                        [_Ask("host:lmstudio", "refused: no rule permits yielding host:lmstudio")]))
        path = self.weights()
        with self.assertRaises(self.card.CardRefused) as caught:
            self.utils.load_torch_file(path)
        self.assertEqual(caught.exception.outcome, "holders_refused")
        self.assertEqual(caught.exception.amount, self.card.FLOOR_BYTES)
        self.assertIn("host:lmstudio", str(caught.exception))
        self.assertIn("no rule permits yielding", str(caught.exception))
        self.assertIn("model.safetensors was not loaded", str(caught.exception))
        self.assertEqual(self.loaded, [])
        self.assertIsNone(self.card.held().lease)

    def test_not_permitted_names_the_rule_a_person_writes(self):
        self.install(leases=_FakeLeases("not_permitted"))
        with self.assertRaises(self.card.CardRefused) as caught:
            self.utils.load_torch_file(self.weights())
        self.assertEqual(caught.exception.outcome, "not_permitted")
        self.assertIn("no holder was asked", str(caught.exception))
        self.assertIn("abstraction.resource/hold", str(caught.exception))
        self.assertIn("card:0", str(caught.exception))
        self.assertEqual(self.loaded, [])

    def test_a_service_that_does_not_answer_stops_the_render(self):
        broken = _FakeLeases()
        broken.acquire = lambda *a: (_ for _ in ()).throw(RuntimeError("service resolution: runtime_unavailable"))
        self.install(leases=broken)
        with self.assertRaises(self.card.CardUnavailable) as caught:
            self.utils.load_torch_file(self.weights())
        self.assertIn("runtime_unavailable", str(caught.exception))
        self.assertEqual(self.loaded, [])

    def test_a_live_lease_is_renewed_while_the_weights_are_held(self):
        self.card.RENEW_SECONDS = 0.05
        self.install()
        self.utils.load_torch_file(self.weights())
        deadline = time.time() + 5
        while not self.leases.renewals and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.leases.renewals[:1], ["lease-0001"])
        self.assertEqual(self.card.held().leases, ("lease-0001",))
        self.card.release_all()
        self.assertEqual(self.leases.releases, ["lease-0001"])

    def test_renewer_drops_a_lease_when_nested_result_says_not_applied(self):
        self.card.RENEW_SECONDS = 0.05
        leases = _FakeLeases()
        leases.renew_outcomes["lease-0001"] = False
        self.install(leases=leases)
        self.utils.load_torch_file(self.weights())
        deadline = time.time() + 5
        while "lease-0001" in self.card.held().leases and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(leases.renewals[:1], ["lease-0001"])
        self.assertNotIn("lease-0001", self.card.held().leases)


class RightsProgramTest(unittest.TestCase):
    """Permission guidance names the relay helper, never an interpreter.

    A shared python.exe is never a rights program (owner, 2026-10-07). With no
    relay loaded, rights_program() suggests nothing whatever the interpreter
    facts, and the card and download guidance offer no command. Card loads
    standalone as in CardLeaseTest; service loads in a throwaway package.
    """

    def setUp(self):
        import importlib.util
        self._modules = set(sys.modules)
        self.addCleanup(lambda: [sys.modules.pop(name, None) for name in set(sys.modules) - self._modules])

        # service.py's only relative import is "from . import storage_reuse":
        # a throwaway package whose search path is the node directory resolves
        # it without executing __init__.py, which requires ComfyUI's own
        # comfy.* modules to be loadable.
        pkg_name = "oa_rights_program_pkg_under_test"
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [NODE]
        sys.modules[pkg_name] = pkg
        spec = importlib.util.spec_from_file_location(pkg_name + ".service", os.path.join(NODE, "service.py"))
        self.service = importlib.util.module_from_spec(spec)
        self.service.__package__ = pkg_name
        sys.modules[pkg_name + ".service"] = self.service
        spec.loader.exec_module(self.service)

        card_spec = importlib.util.spec_from_file_location("oa_rights_program_card_under_test", os.path.join(NODE, "oa_card.py"))
        self.card = importlib.util.module_from_spec(card_spec)
        card_spec.loader.exec_module(self.card)

        # A path this test knows exists (this process's own interpreter) and
        # one it knows does not (a fresh temporary directory holds nothing).
        self.real_path = os.path.realpath(sys.executable)
        self.missing_path = os.path.join(tempfile.mkdtemp(prefix="oa-missing-base-"), "no-such-python.exe")

    def test_no_relay_suggests_no_interpreter(self):
        cases = [
            SimpleNamespace(executable=r"C:\venv\Scripts\python.exe", _base_executable=self.real_path, modules={}),
            SimpleNamespace(executable=self.real_path, _base_executable=self.missing_path, modules={}),
            SimpleNamespace(executable=self.real_path, modules={}),
        ]
        for fake_sys in cases:
            with self.subTest(executable=fake_sys.executable):
                with mock.patch.object(self.service, "sys", fake_sys):
                    self.assertIsNone(self.service.rights_program())
                    self.assertIsNone(self.service.grant_command())
                with mock.patch.object(self.card, "sys", fake_sys):
                    sentence = self.card._grant_sentence()
                self.assertNotIn("--program", sentence)
                self.assertIn("unavailable", sentence)


if __name__ == "__main__":
    unittest.main()
