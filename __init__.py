"""ComfyUI-Manager download adapter using the installed job service.

Normal downloads use generated Python clients over shared native local IPC.
The service owns backend transfer and result storage. This adapter retains a
caller recovery record and atomically delivers complete result bytes into the
application's model directory. Service failures remain explicit.
See SERVICE.md for prerequisites, recovery and the limits of the current
anonymous HTTP execution profile.
"""

import logging
import os
import sys

LOG = logging.getLogger("abstraction")

# ComfyUI requires these two names from every custom node. WEB_DIRECTORY names
# the directory ComfyUI serves under /extensions/<node>/ and loads every .js
# from: it carries the "OA downloads" sidebar tab. The tab reads
# GET /abstraction/jobs and says so when that route is not served.
from .oa_image import OAImageGenerate
from .oa_video import OAVideoGenerate

NODE_CLASS_MAPPINGS = {"OAImageGenerate": OAImageGenerate, "OAVideoGenerate": OAVideoGenerate}
WEB_DIRECTORY = "./web"
__all__ = ["NODE_CLASS_MAPPINGS", "WEB_DIRECTORY"]

_HERE = os.path.dirname(os.path.abspath(__file__))


def _custom_nodes_root():
    """The host's custom_nodes for direct or composed extension placement."""
    parent = os.path.dirname(_HERE)
    if os.path.basename(parent).lower() == 'custom_nodes':
        return parent
    if os.path.basename(_HERE) == 'oa_nodes' and os.path.basename(parent) in ('comfyui-openabstractions', 'abstraction_downloads'):
        custom_nodes = os.path.dirname(parent)
        if os.path.basename(custom_nodes).lower() == 'custom_nodes':
            return custom_nodes
    return None


from collections import namedtuple

# Adoption status codes. ADOPTED is the only code with adopted=True.
ADOPTED = "adopted"
MANAGER_NOT_FOUND = "manager_not_found"
MANAGER_NOT_ENABLED = "manager_not_enabled"
MANAGER_NOT_IMPORTABLE = "manager_not_importable"
MANAGER_SERVER_NOT_LOADED = "manager_server_not_loaded"
# Manager was found, but the preflight failed and no retained OA recovery
# record gives accepted work a reason to stay with the service: Manager's own
# downloader is left untouched instead.
NOT_ACTIVATED = "not_activated"
PENDING = "pending"

# Supported layouts.
LAYOUT_PACKAGE = "comfyui_manager"      # pip comfyui_manager 4.x, loaded by core --enable-manager
LAYOUT_CUSTOM_NODE = "custom_node_glob"  # custom_nodes/ComfyUI-Manager/glob, 3.x

AdoptionStatus = namedtuple(
    "AdoptionStatus", "adopted code layout version provider patched detail")
AdoptionStatus.__doc__ = """Whether Manager model installs use this adapter.

adopted: True only when Manager's model-install call sites are rebound.
code: one of the module status codes (ADOPTED, MANAGER_NOT_FOUND, ...).
layout: LAYOUT_PACKAGE, LAYOUT_CUSTOM_NODE or None.
version: the detected Manager version string, or None.
provider: "service", the job service.
patched: names of the Manager modules whose model-install bindings were replaced.
detail: human-readable explanation.
"""

_status = AdoptionStatus(False, PENDING, None, None, None, (), "not yet installed")


def adoption_status() -> AdoptionStatus:
    """The typed result of the last install attempt."""
    return _status


# install_status() modes. MODE_SERVICE covers both a passed preflight and a
# failed one kept in service mode because accepted OA work is pending.
MODE_SERVICE = "service"
MODE_MANAGER = "manager"

InstallStatus = namedtuple("InstallStatus", "mode code detail")
InstallStatus.__doc__ = """Whether service-mode Manager model installs are
active or left untouched, and why.

mode: MODE_SERVICE (the job service is wired in, whether preflight passed or
a retained OA recovery record kept it wired despite a failing preflight),
MODE_MANAGER (preflight failed with no retained OA recovery record, so
Manager's own downloader is untouched).
code: one of service.py's PREFLIGHT_* codes when mode is MODE_SERVICE or
MODE_MANAGER, or an AdoptionStatus code such as
MANAGER_NOT_FOUND when Manager itself was never found.
detail: human-readable explanation, naming the failing preflight item and
how to activate when mode is MODE_MANAGER, or that accepted OA work is
pending when a retained record kept mode MODE_SERVICE despite a failed
preflight.
"""

_install_status = InstallStatus(MODE_MANAGER, PENDING, "not yet installed")


def install_status() -> InstallStatus:
    """The typed result of the last preflight and activation decision."""
    return _install_status


def _core_manager_flags():
    """(enable_manager, enable_manager_legacy_ui) from ComfyUI core, or
    (None, None) when this process is not running ComfyUI core."""
    try:
        from comfy.cli_args import args
    except Exception:
        return None, None
    return bool(getattr(args, "enable_manager", False)), bool(getattr(args, "enable_manager_legacy_ui", False))


def _package_manager_present() -> bool:
    import importlib.util
    try:
        spec = importlib.util.find_spec("comfyui_manager")
    except (ImportError, ValueError):
        return False
    # Core applies the same test before it accepts the package (main.py).
    return spec is not None and bool(spec.origin) and spec.origin.endswith("__init__.py")


def _custom_node_manager_dir():
    """The 3.x custom-node Manager directory beside this node, or None.

    Zip and registry installs name the directory ``comfyui-manager``; git
    clones name it ``ComfyUI-Manager``. Core matches the name the same way.
    """
    custom_nodes = _custom_nodes_root()
    if custom_nodes is None:
        return None
    try:
        names = sorted(os.listdir(custom_nodes))
    except OSError:
        return None
    for name in names:
        path = os.path.join(custom_nodes, name)
        if name.lower() in ("comfyui-manager", "comfyui_manager") and os.path.isfile(
                os.path.join(path, "glob", "manager_downloader.py")):
            return path
    return None


def _read_pyproject_version(path):
    import re
    try:
        with open(os.path.join(path, "pyproject.toml"), encoding="utf-8") as f:
            text = f.read(65536)
    except OSError:
        return None
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else None


def _package_version():
    try:
        from importlib.metadata import version
        return version("comfyui_manager")
    except Exception:
        return None


def _select_layout():
    """Choose the Manager layout ComfyUI is actually running.

    With core --enable-manager, core runs the pip package and disables every
    custom-node directory named comfyui-manager (comfyui_manager.should_be_disabled).
    Without it, the package does not run and only a 3.x custom node can.
    Outside ComfyUI core the package is preferred when installed.
    Returns (layout, manager_dir, code, detail).
    """
    enabled, _ = _core_manager_flags()
    package = _package_manager_present()
    custom = _custom_node_manager_dir()
    if enabled or (enabled is None and package):
        if package:
            return LAYOUT_PACKAGE, None, ADOPTED, ""
        return None, None, MANAGER_NOT_FOUND, "core --enable-manager is set but comfyui_manager is not importable"
    if custom is not None:
        return LAYOUT_CUSTOM_NODE, custom, ADOPTED, ""
    if package:
        return None, None, MANAGER_NOT_ENABLED, (
            "comfyui_manager is installed but ComfyUI was started without --enable-manager")
    return None, None, MANAGER_NOT_FOUND, (
        "neither the comfyui_manager package nor custom_nodes/ComfyUI-Manager/glob was found")


def _manager_modules():
    """Detect the Manager layout and return its model-install modules.

    Returns (downloader, servers, layout, version, code, detail). ``servers``
    are the modules holding the model-install call sites (do_install_model).

    Package layout (4.x): core imports comfyui_manager.glob.manager_server, or
    legacy.manager_server with --enable-manager-legacy-ui, in
    comfyui_manager.start() before it loads custom nodes. Both are taken from
    sys.modules. This node does not import them itself: importing the UI that
    core did not select would register a second set of routes.

    Custom-node layout (3.x): ComfyUI loads custom node directories in sorted
    order. Importing the glob modules directly makes the order irrelevant:
    Manager's own later import is a no-op on the same objects.
    """
    import importlib

    layout, manager_dir, code, detail = _select_layout()
    if layout is None:
        return None, (), None, None, code, detail

    if layout == LAYOUT_PACKAGE:
        version = _package_version()
        try:
            downloader = importlib.import_module("comfyui_manager.common.manager_downloader")
        except Exception as e:
            return None, (), layout, version, MANAGER_NOT_IMPORTABLE, "comfyui_manager.common.manager_downloader: %r" % (e,)
        servers = tuple(sys.modules[name] for name in (
            "comfyui_manager.glob.manager_server", "comfyui_manager.legacy.manager_server")
            if name in sys.modules)
        if not servers:
            return downloader, (), layout, version, MANAGER_SERVER_NOT_LOADED, (
                "comfyui_manager is installed but neither glob nor legacy manager_server was "
                "loaded before custom nodes; check the ComfyUI-Manager start log")
        return downloader, servers, layout, version, ADOPTED, ""

    version = _read_pyproject_version(manager_dir)
    glob_dir = os.path.join(manager_dir, "glob")
    if glob_dir not in sys.path:
        sys.path.append(glob_dir)
    try:
        downloader = importlib.import_module("manager_downloader")
        server = importlib.import_module("manager_server")
    except Exception as e:
        return None, (), layout, version, MANAGER_NOT_IMPORTABLE, "%s: %r" % (glob_dir, e)
    return downloader, (server,), layout, version, ADOPTED, ""


_original_download_url = None
_original_download_url_with_agent = None


_HUB_ENDPOINT_MARK = "ABSTRACTION_COMFYUI_HUB_ENDPOINT"


def _mirrored(model_url: str) -> str:
    """Manager's own first act inside download_url, and it has to survive.

    HF_ENDPOINT points Hugging Face traffic at a mirror. It is not a nicety:
    where huggingface.co is slow or unreachable it is the only way downloads
    work at all. Replacing download_url without this sends those users back to
    the origin, which fails or crawls -- and looks like our abstraction broke
    their downloads, because it did.
    """
    endpoint = os.environ.get("HF_ENDPOINT")
    # OA's own hub route (oa_hub.py) is no mirror: a job sourced there would
    # come back into this ComfyUI.
    if endpoint and endpoint.rstrip("/") != os.environ.get(_HUB_ENDPOINT_MARK, ""):
        return model_url.replace("https://huggingface.co", endpoint.rstrip("/"))
    return model_url


def _through_the_abstraction(model_url: str, model_dir: str, filename: str) -> None:
    from .service import deliver
    return deliver(_mirrored(model_url), model_dir, filename)


_non_model_roots = ()


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(os.path.abspath(path)),
                                  os.path.normcase(os.path.abspath(root))]) == os.path.normcase(os.path.abspath(root))
    except ValueError:
        return False


def _is_model_destination(directory: str) -> bool:
    """Custom-node code and frontend files are Manager installs, not models.

    3.x copy_install calls the same bound download_url for .py files into
    custom_nodes and .js files into web/extensions. Those files are ComfyUI
    code: the caller recovery record the service path retains beside a model
    has no meaning there, and would itself be left inside custom_nodes.
    """
    return not any(_inside(directory, root) for root in _non_model_roots)


def _unreachable(error) -> bool:
    """True when OA could not be reached before it recorded any work for the file."""
    from .service import ServiceUnavailable
    return isinstance(error, ServiceUnavailable)


def _falls_back(filename, error):
    LOG.warning("[abstraction] OA is not reachable; ComfyUI-Manager downloads %s itself: %s", filename, error)


def download_url(model_url: str, model_dir: str, filename: str):
    """Manager download entrypoint. Service failures propagate, except an
    unreachable OA with no recorded work: Manager's own downloader runs then."""
    if not _is_model_destination(model_dir):
        LOG.info("[abstraction] %s is a custom-node file install; ComfyUI-Manager downloads it", filename)
        return _original_download_url(model_url, model_dir, filename)
    try:
        return _through_the_abstraction(model_url, model_dir, filename)
    except Exception as e:
        if _unreachable(e):
            _falls_back(filename, e)
            return _original_download_url(model_url, model_dir, filename)
        LOG.error("[abstraction] download failed for %s: %r", model_url, e)
        raise


def download_url_with_agent(url: str, save_path: str):
    """Manager agent entrypoint: True for completed delivery, False for failure."""
    model_dir, filename = os.path.split(save_path)
    if not _is_model_destination(model_dir):
        LOG.info("[abstraction] %s is a custom-node file install; ComfyUI-Manager downloads it", filename)
        return _original_download_url_with_agent(url, save_path)
    try:
        _through_the_abstraction(url, model_dir, filename)
        return True
    except Exception as e:
        if _unreachable(e) and _original_download_url_with_agent is not None:
            _falls_back(filename, e)
            return _original_download_url_with_agent(url, save_path)
        # Manager's own agent downloader returns False rather than raising, and
        # do_install_model turns that into "Model installation error". Raising
        # here would be caught one level up and reported as an Exception instead
        # -- a different message for the same event, which is again the provider
        # changing what the operation means.
        LOG.error("[abstraction] download failed for %s: %r", url, e)
        return False


def _not_adopted(code, provider, detail, layout=None, version=None):
    global _status, _install_status
    _status = AdoptionStatus(False, code, layout, version, provider, (), detail)
    _install_status = InstallStatus(MODE_MANAGER, code, detail)
    LOG.warning(
        "[abstraction] NOT ADOPTED (%s): ComfyUI-Manager model downloads do not use the "
        "abstraction; Manager's own downloader is in effect. %s", code, detail)
    _install_snapshots(None, detail)


# Repository snapshots: Manager's "<huggingface>" model entries download a whole
# repository with download_repo_in_bytes. ABSTRACTION_COMFYUI_SNAPSHOTS=service
# delivers each file of the listed revision through its own download job.
SNAPSHOT_ENV = "ABSTRACTION_COMFYUI_SNAPSHOTS"
SNAPSHOTS_OFF = "off"
SNAPSHOTS_ADOPTED = "adopted"
SNAPSHOTS_INVALID_CONFIGURATION = "invalid_configuration"
SNAPSHOTS_MANAGER_NOT_ADOPTED = "manager_not_adopted"
SNAPSHOTS_CALL_NOT_FOUND = "snapshot_call_not_found"

SnapshotStatus = namedtuple("SnapshotStatus", "enabled adopted code detail")
SnapshotStatus.__doc__ = """Whether Manager repository snapshots use download jobs.

enabled: ABSTRACTION_COMFYUI_SNAPSHOTS is set.
adopted: True only when download_repo_in_bytes is rebound.
code: one of the SNAPSHOTS_* codes.
detail: human-readable explanation.
"""

_snapshot_status = SnapshotStatus(False, False, PENDING, "not yet installed")
_original_download_repo_in_bytes = None


def snapshot_status() -> SnapshotStatus:
    """The typed result of the last snapshot install attempt."""
    return _snapshot_status


def _list_repository(repo_id):
    """The files of the repository's current revision, the way Manager lists them.

    URLs name the revision's commit, so every file comes from one revision and a
    retained request stays valid when the branch moves. A file stored with Git
    LFS carries its sha256, which the service verifies before delivery; other
    files carry their size only. Files without a size are skipped, as Manager
    skips them.
    """
    from urllib.parse import quote
    from huggingface_hub import HfApi

    info = HfApi().repo_info(repo_id=repo_id, files_metadata=True)
    revision = info.sha or "main"
    base = _mirrored("https://huggingface.co")
    files = []
    for sibling in info.siblings or ():
        if sibling.size is None:
            continue
        lfs = getattr(sibling, "lfs", None)
        sha256 = getattr(lfs, "sha256", None) if lfs is not None else None
        files.append(dict(
            path=sibling.rfilename,
            url="%s/%s/resolve/%s/%s" % (base, repo_id, revision, quote(sibling.rfilename, safe="/")),
            size=sibling.size,
            digest=("sha256:" + sha256.lower()) if sha256 else ""))
    return dict(revision=revision, files=files)


def download_repo_in_bytes(repo_id, local_dir):
    """Manager's repository snapshot entrypoint, delivered through download jobs.

    Service absence, refusal and failed jobs raise; Manager reports the raised
    install as failed. There is no fallback to Manager's own downloader.
    """
    from .service import deliver_snapshot
    try:
        delivered = deliver_snapshot(repo_id, local_dir, lambda: _list_repository(repo_id))
    except Exception as e:
        LOG.error("[abstraction] repository snapshot %s failed: %r", repo_id, e)
        raise
    LOG.info("[abstraction] repository snapshot %s: %d files delivered into %s", repo_id, len(delivered), local_dir)


def _install_snapshots(downloader, not_adopted_detail=""):
    global _snapshot_status, _original_download_repo_in_bytes
    value = os.environ.get(SNAPSHOT_ENV, "")
    if value == "":
        _snapshot_status = SnapshotStatus(False, False, SNAPSHOTS_OFF,
                                          "%s is unset; Manager downloads repository snapshots itself" % SNAPSHOT_ENV)
        return
    if value != "service":
        code, detail = SNAPSHOTS_INVALID_CONFIGURATION, "%s=%r; supported values are unset and service" % (SNAPSHOT_ENV, value)
    elif downloader is None:
        code, detail = SNAPSHOTS_MANAGER_NOT_ADOPTED, not_adopted_detail or "ComfyUI-Manager was not adopted"
    elif not callable(getattr(downloader, "download_repo_in_bytes", None)):
        code, detail = SNAPSHOTS_CALL_NOT_FOUND, "%s has no download_repo_in_bytes" % downloader.__name__
    elif _provider_seam(downloader):
        _snapshot_status = SnapshotStatus(True, True, SNAPSHOTS_ADOPTED,
                                          "%s calls the registered download provider" % downloader.__name__)
        LOG.info("[abstraction] ComfyUI-Manager repository snapshots use one download job per file")
        return
    else:
        _original_download_repo_in_bytes = downloader.download_repo_in_bytes
        # Every Manager layout calls manager_downloader.download_repo_in_bytes
        # through the module, so the module attribute is the call site.
        downloader.download_repo_in_bytes = download_repo_in_bytes
        _snapshot_status = SnapshotStatus(True, True, SNAPSHOTS_ADOPTED,
                                          "%s.download_repo_in_bytes uses download jobs" % downloader.__name__)
        LOG.info("[abstraction] ComfyUI-Manager repository snapshots use one download job per file")
        return
    _snapshot_status = SnapshotStatus(True, False, code, detail)
    LOG.warning("[abstraction] SNAPSHOTS NOT ADOPTED (%s): Manager downloads repository snapshots itself. %s",
                code, detail)


def _provider_seam(downloader) -> bool:
    """True when Manager's downloader accepts a registered download provider."""
    return callable(getattr(downloader, "set_download_provider", None))


def _comfy_root():
    """ComfyUI's root directory: two levels up from this node
    (ComfyUI/custom_nodes/abstraction_downloads), the layout SERVICE.md's
    install step produces. None outside that layout."""
    custom_nodes = _custom_nodes_root()
    if custom_nodes is None:
        return None
    return os.path.dirname(custom_nodes)


def _model_search_roots():
    """Directories to look for retained OA caller records in: ComfyUI's own
    folder_paths.models_dir when this process already imported it, and the
    sibling "models" directory of the standard custom_nodes/abstraction_downloads
    install layout. Neither is required to exist."""
    roots = []
    folder_paths = sys.modules.get("folder_paths")
    models_dir = getattr(folder_paths, "models_dir", None)
    if isinstance(models_dir, str) and os.path.isdir(models_dir):
        roots.append(models_dir)
    root = _comfy_root()
    if root is not None:
        models = os.path.join(root, "models")
        if os.path.isdir(models) and models not in roots:
            roots.append(models)
    return roots


def _has_recovery_records():
    """True when this ComfyUI's models hold a retained OA caller record: a
    *.oa-request.json beside a model or a *.oa-snapshot.json beside a
    repository directory (SERVICE.md "Storage reuse", "Repository snapshots").
    Bounded to the roots ComfyUI itself exposes; an absent or unreadable root
    counts as no records, never as a scan error."""
    for root in _model_search_roots():
        try:
            for _base, _dirs, files in os.walk(root):
                if any(name.endswith((".oa-request.json", ".oa-snapshot.json")) for name in files):
                    return True
        except OSError:
            continue
    return False


def _install() -> None:
    global _original_download_url, _original_download_url_with_agent, _non_model_roots, _status, _install_status

    provider = "service"

    downloader, servers, layout, version, code, detail = _manager_modules()
    if code != ADOPTED:
        _not_adopted(code, provider, detail, layout, version)
        return

    from . import service
    ready, preflight_code, preflight_detail = service.preflight()
    if ready:
        _install_status = InstallStatus(MODE_SERVICE, preflight_code, preflight_detail)
    elif _has_recovery_records():
        _install_status = InstallStatus(MODE_SERVICE, preflight_code,
            "a retained OA recovery record exists; accepted OA work is pending: %s" % preflight_detail)
        LOG.warning(
            "[abstraction] OA DOWNLOADS PENDING (%s): a retained OA recovery record beside a model keeps "
            "Manager model installs on the job service, and they fail until this is resolved: %s",
            preflight_code, preflight_detail)
    else:
        _install_status = InstallStatus(MODE_MANAGER, preflight_code,
            "%s; resolve this, then restart ComfyUI" % preflight_detail)
        LOG.warning(
            "[abstraction] OA DOWNLOADS NOT ACTIVATED (%s): no OA work is pending, so Manager's own "
            "downloader is unchanged. %s", preflight_code, preflight_detail)
        _status = AdoptionStatus(False, NOT_ACTIVATED, layout, version, provider, (), preflight_detail)
        _install_snapshots(None, preflight_detail)
        return

    if _provider_seam(downloader):
        # Manager offers a provider seam: register, rebind nothing.
        from .manager_provider import ManagerDownloadProvider
        _non_model_roots = tuple([_custom_nodes_root() or os.path.dirname(_HERE)] + [
            js for js in (getattr(getattr(s, "core", None), "js_path", None) for s in servers)
            if isinstance(js, str) and js])
        downloader.set_download_provider(ManagerDownloadProvider(sys.modules[__name__]))
        _status = AdoptionStatus(True, ADOPTED, layout, version, provider, (),
                                 "model installs through %s.set_download_provider" % downloader.__name__)
        LOG.info("[abstraction] ComfyUI-Manager %s (%s) model downloads use %s through its download provider seam",
                 version or "unknown version", layout, provider)
        _install_snapshots(downloader)
        return

    _original_download_url = downloader.download_url
    _original_download_url_with_agent = getattr(downloader, "download_url_with_agent", None)

    # The server modules hold the model-install call sites. Each does
    #     from ...manager_downloader import download_url, download_url_with_agent
    # which copies the functions into ITS namespace at import time, so the
    # rebinding has to happen there.
    #
    # The downloader MODULE keeps Manager's functions. manager_core.cnr_install
    # and prestartup's lazy switch call manager_downloader.download_url for
    # custom-node zip archives with a random CNR_temp_<uuid>.zip name that is
    # extracted and deleted at once. Those are code installs outside the model
    # download contract; see SERVICE.md.
    #
    # Both FUNCTIONS, because do_install_model chooses between them by URL and
    # patching one covers only the hosts that match it.
    roots = [_custom_nodes_root() or os.path.dirname(_HERE)]
    for server in servers:
        server.download_url = download_url
        if _original_download_url_with_agent is not None:
            server.download_url_with_agent = download_url_with_agent
        js_path = getattr(getattr(server, "core", None), "js_path", None)
        if isinstance(js_path, str) and js_path:
            roots.append(js_path)
    _non_model_roots = tuple(roots)

    names = tuple(server.__name__ for server in servers)
    _status = AdoptionStatus(True, ADOPTED, layout, version, provider, names,
                             "model installs through %s" % ", ".join(names))
    LOG.info("[abstraction] ComfyUI-Manager %s (%s) model downloads use %s",
             version or "unknown version", layout, provider)
    _install_snapshots(downloader)


# ABSTRACTION_COMFYUI_LOGGING first, so adoption diagnostics below also reach
# the logging service when it is on. See oa_logging.py.
from . import oa_logging  # noqa: E402

logging_status = oa_logging.status
from . import oa_route  # noqa: E402

oa_route.configure_startup()
oa_logging.install()
_install()

# Non-download capabilities, each behind its own switch and off when unset.
# See oa_diagnostics.py, oa_inventory.py, oa_jobs_view.py, oa_download_observer.py
# and oa_card.py.
from . import (klein_recipe, oa_card, oa_diagnostics, oa_download_observer, oa_hub, oa_inventory,
               oa_jobs_view, oa_model_downloads)  # noqa: E402

diagnostics_status = oa_diagnostics.status
inventory_status = oa_inventory.status
download_observer_status = oa_download_observer.status
card_status = oa_card.status
workflow_routes_status = klein_recipe.install()
jobs_view_rows = oa_jobs_view.rows
oa_diagnostics.install()
# The inventory route answers the Comfy-facing rows the sidebar tab reads.
oa_inventory.configure_view(oa_jobs_view)
oa_inventory.install()
oa_download_observer.install()
# The missing-model dialog's downloads (web/oa_model_downloads.js) and the
# Hugging Face hub's downloads go through OA download jobs.
model_downloads_status = oa_model_downloads.status
hub_status = oa_hub.status
oa_model_downloads.install()
oa_hub.install()
# The card hook wraps ComfyUI's own weights loader, which this process already
# imported: a custom node loads after comfy.utils and comfy.model_management.
oa_card.install()
# The OA app setup writes this exact marker after public selection setup
# succeeds. Presence is optional and best-effort; the worker starts only after
# the node and its Comfy routes are loaded.
from . import oa_application  # noqa: E402

oa_application.start_if_configured()
