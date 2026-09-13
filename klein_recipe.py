"""Pinned FLUX.2 Klein distilled workflow and explicitly approved preparation.

The workflow template is from Comfy-Org/workflow_templates at the revision in
TEMPLATE. Model files are delivered through this adopter's existing service;
the UI only receives a graph after each exact file has passed size and SHA256
verification.
"""
import copy
import asyncio
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import socket
import stat
import threading
from urllib.parse import urlsplit

from aiohttp import web

HERE = Path(__file__).resolve().parent
TEMPLATE_PATH = HERE / "workflows" / "image_flux2_klein_text_to_image.json"
IMAGE_RECIPE_PATH = HERE / "workflows" / "oa_image_generate_preview_save.json"
TEMPLATE_SHA256 = "8bb879856e54765b6c3ed219181e855981d8d10cfd19acff836603d9b56e838f"
TEMPLATE_REVISION = "e7cd011d4ded3411c2f481200544f0be6fdc962e"
TEMPLATE_URL = ("https://raw.githubusercontent.com/Comfy-Org/workflow_templates/" +
                TEMPLATE_REVISION + "/templates/image_flux2_klein_text_to_image.json")
TEMPLATE_NAME = "image_flux2_klein_text_to_image.json"
TEMPLATE_VARIANT = "distilled-fp8"
TEMPLATE_LICENSE = {"id": "MIT", "url": "https://opensource.org/license/mit"}
MODEL_LICENSE = {"id": "Apache-2.0", "url": "https://www.apache.org/licenses/LICENSE-2.0"}
PROMPT = ("A red fox using a hand-cranked espresso machine inside a tiny Nordic greenhouse, "
          "warm morning light, editorial illustration, rich textures, no text")
SEED = 20260929
WIDTH = 512
HEIGHT = 512
STEPS = 4
CONTROL_AFTER_GENERATE = "fixed"

ASSETS = (
    {"role": "diffusion_model", "repository": "black-forest-labs/FLUX.2-klein-4b-fp8",
     "revision": "5b4408e59397a4a37ccb46afe426d8ed86379441",
     "url": "https://huggingface.co/black-forest-labs/FLUX.2-klein-4b-fp8/resolve/"
            "5b4408e59397a4a37ccb46afe426d8ed86379441/flux-2-klein-4b-fp8.safetensors",
     "filename": "flux-2-klein-4b-fp8.safetensors", "folder": "diffusion_models",
     "size_bytes": 4070624520,
     "sha256": "97ed34fe0567e436200f2faee3939b88f2b5d99f8af2a4dc16532c4245c0ccb6",
     "license": MODEL_LICENSE},
    # Comfy-Org's mixed FP8/NVFP4 Qwen3 4B encoder published for FLUX.2: the
    # compact set Setup prepares, 8.26 GB with the FP8 model and the VAE.
    {"role": "text_encoder", "repository": "Comfy-Org/flux2-klein",
     "revision": "5b3abca5c0dc1ecd4eaf3340868d09063fc1c965",
     "url": "https://huggingface.co/Comfy-Org/flux2-klein/resolve/5b3abca5c0dc1ecd4eaf3340868d09063fc1c965/split_files/text_encoders/qwen_3_4b_fp4_flux2.safetensors",
     "filename": "qwen_3_4b_fp4_flux2.safetensors", "folder": "text_encoders",
     "size_bytes": 3848213998,
     "sha256": "3eab03a77adb0ee5304a4e677d5c10ac22f9049c1d7c894adca4f8bb39206ca8",
     "license": MODEL_LICENSE},
    {"role": "vae", "repository": "Comfy-Org/vae-text-encorder-for-flux-klein-4b",
     "revision": "5f526678002e43af5551dadb73ce2e8c91b43afe",
     "url": "https://huggingface.co/Comfy-Org/vae-text-encorder-for-flux-klein-4b/resolve/5f526678002e43af5551dadb73ce2e8c91b43afe/split_files/vae/flux2-vae.safetensors",
     "filename": "flux2-vae.safetensors", "folder": "vae",
     "size_bytes": 336211292,
     "sha256": "868fe7b343cc8f3a19dbcfcafbc3d5f888802be3f89bd81b65b3621a066ce8f3",
     "license": MODEL_LICENSE},
)

PLAN_ROUTE = "/abstraction/workflows/flux2-klein-distilled"
IMAGE_RECIPE_ROUTE = "/abstraction/workflows/oa-image-generate"
RECIPE_ID = "flux2-klein-distilled-v1"
BF16_VARIANT = "distilled-bf16"
BF16_RECIPE_ID = "flux2-klein-distilled-bf16-v1"
BF16_ASSETS = (
    {"role": "diffusion_model", "repository": "Comfy-Org/vae-text-encorder-for-flux-klein-4b",
     "revision": "5f526678002e43af5551dadb73ce2e8c91b43afe",
     "url": "https://huggingface.co/Comfy-Org/vae-text-encorder-for-flux-klein-4b/resolve/5f526678002e43af5551dadb73ce2e8c91b43afe/split_files/diffusion_models/flux-2-klein-4b.safetensors",
     "filename": "flux-2-klein-4b.safetensors", "folder": "diffusion_models",
     "size_bytes": 7751105712,
     "sha256": "ec3d4e733a771f61c052fb4856c48b336c55eaf2c65487c2a1faeb9bbda7a343",
     "license": MODEL_LICENSE},
    {"role": "text_encoder", "repository": "Comfy-Org/vae-text-encorder-for-flux-klein-4b",
     "revision": "5f526678002e43af5551dadb73ce2e8c91b43afe",
     "url": "https://huggingface.co/Comfy-Org/vae-text-encorder-for-flux-klein-4b/resolve/5f526678002e43af5551dadb73ce2e8c91b43afe/split_files/text_encoders/qwen_3_4b.safetensors",
     "filename": "qwen_3_4b.safetensors", "folder": "text_encoders",
     "size_bytes": 8044982048,
     "sha256": "6c671498573ac2f7a5501502ccce8d2b08ea6ca2f661c458e708f36b36edfc5a",
     "license": MODEL_LICENSE},
    ASSETS[2],
)


def _variant(variant):
    if variant == TEMPLATE_VARIANT:
        return RECIPE_ID, ASSETS
    if variant == BF16_VARIANT:
        return BF16_RECIPE_ID, BF16_ASSETS
    raise ValueError("unsupported Klein recipe variant: %r" % variant)


PREPARE_ROUTE = PLAN_ROUTE + "/prepare"
GRAPH_ROUTE = PLAN_ROUTE + "/graph"
IMAGE_GRAPH_ROUTE = IMAGE_RECIPE_ROUTE + "/graph"
GRAPH_DIGEST_HEADER = "X-OA-Graph-SHA256"
RECIPES_ROUTE = "/abstraction/recipes"
MAX_RECIPES = 64
MAX_RECIPES_BYTES = 1024 * 1024
ALLOWED_HOSTS_ENV = "ABSTRACTION_COMFYUI_WORKFLOW_ALLOWED_HOSTS"
MAX_BODY_BYTES = 1024
# The Panel opens ComfyUI at ?oa_recipe=picture&oa_token=<token>. It obtains
# the token with a POST that carries ARRIVAL_ISSUE_HEADER and no Origin, which
# a web page cannot send without a CORS preflight this server never grants.
# The sidebar spends the token once before it loads the picture recipe.
ARRIVAL_ROUTE = IMAGE_RECIPE_ROUTE + "/arrival"
ARRIVAL_ISSUE_HEADER = "X-OA-Arrival"
ARRIVAL_SECONDS = 300
ARRIVAL_LIMIT = 16
_arrivals = {}
_lock = threading.Lock()
_prepare_thread = None
_prepare_detail = ""
_registered = []
_folder_paths = None
_deliver = None
_verified = {}
_verification_lock = threading.Lock()


def configure(folder_paths=None, deliver=None):
    """Inject Comfy's folder resolver and delivery seam for focused tests."""
    global _folder_paths, _deliver
    _folder_paths, _deliver = folder_paths, deliver


def _model_path(asset):
    module = _folder_paths or __import__("folder_paths")
    roots = module.get_folder_paths(asset["folder"])
    if not roots:
        raise RuntimeError("ComfyUI has no %s search folder" % asset["folder"])
    # ComfyUI resolves the first matching name in search order. Verify and
    # prepare that same path so a duplicate later root cannot mask the result.
    for root in roots:
        path = Path(root) / asset["filename"]
        if os.path.lexists(str(path)):
            return path
    return Path(roots[0]) / asset["filename"]


def _is_link_or_reparse(path):
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return False
    attrs = getattr(entry, "st_file_attributes", 0)
    return stat.S_ISLNK(entry.st_mode) or bool(attrs & 0x400)


def _sha256(source):
    digest = hashlib.sha256()
    for block in iter(lambda: source.read(8 << 20), b""):
        digest.update(block)
    return digest.hexdigest()


def _same_snapshot(first, second):
    return (first.st_ino != 0 and second.st_ino != 0
            and os.path.samestat(first, second)
            and stat.S_ISREG(first.st_mode) and stat.S_ISREG(second.st_mode)
            and (first.st_size, first.st_mtime_ns, first.st_ctime_ns) ==
                (second.st_size, second.st_mtime_ns, second.st_ctime_ns))


def _same_path_snapshot(path_info, handle_info):
    # Windows Python stat(path) exposes creation time as ctime while fstat
    # exposes change time. Compare ctime only between handle snapshots.
    return (path_info.st_ino != 0 and handle_info.st_ino != 0
            and os.path.samestat(path_info, handle_info)
            and stat.S_ISREG(path_info.st_mode)
            and (path_info.st_size, path_info.st_mtime_ns) ==
                (handle_info.st_size, handle_info.st_mtime_ns))


def _state(asset, verify, *, variant=TEMPLATE_VARIANT):
    try:
        path = _model_path(asset)
        entry = path.lstat()
    except FileNotFoundError:
        return "missing", ""
    except OSError as error:
        return "invalid", "cannot inspect %s: %s" % (asset["filename"], error)
    except Exception as error:
        return "invalid", str(error)
    if _is_link_or_reparse(path):
        return "invalid", "%s is a symbolic link or reparse point" % asset["filename"]
    try:
        root = path.parent.resolve()
        resolved = path.resolve(strict=True)
        if os.path.normcase(os.path.commonpath((str(root), str(resolved)))) != os.path.normcase(str(root)):
            return "invalid", "%s resolves outside its Comfy model folder" % asset["filename"]
        entry = path.stat()
    except (OSError, ValueError) as error:
        return "invalid", "cannot resolve %s within its Comfy model folder: %s" % (asset["filename"], error)
    if not stat.S_ISREG(entry.st_mode):
        return "invalid", "%s is not a regular model file" % asset["filename"]
    if entry.st_size != asset["size_bytes"]:
        return "invalid", "%s has %d bytes; expected %d" % (
            asset["filename"], entry.st_size, asset["size_bytes"])
    # One replaceable proof per admitted variant/role, never a persistent store.
    key = (variant, asset["role"])
    expected = (asset["sha256"], asset["size_bytes"], asset["filename"],
                asset["repository"], asset["revision"])
    try:
        with open(path, "rb") as source:
            before = os.fstat(source.fileno())
            if not _same_path_snapshot(entry, before):
                return "invalid", "%s changed while opening" % asset["filename"]
            with _verification_lock:
                proof = _verified.get(key)
            ready = (proof is not None and proof[:2] == (resolved, expected)
                     and _same_snapshot(proof[2], before))
            if verify:
                digest = _sha256(source)
                ready = digest == asset["sha256"]
            after = os.fstat(source.fileno())
            # Resolve the first name again: a new first-root shadow or rename
            # must not borrow evidence established for another open file.
            current = _model_path(asset)
            if (_is_link_or_reparse(current) or current.resolve(strict=True) != resolved
                    or not _same_snapshot(before, after)
                    or not _same_path_snapshot(current.stat(), after)):
                return "invalid", "%s changed during verification" % asset["filename"]
            if verify and not ready:
                with _verification_lock:
                    _verified.pop(key, None)
                return "invalid", "%s does not match the pinned SHA256" % asset["filename"]
            if ready:
                # _state only receives the six supported variant/role pairs.
                # Refuse to retain unbounded evidence if a future caller drifts.
                _, supported = _variant(variant)
                if asset["role"] not in {item["role"] for item in supported}:
                    return "invalid", "unsupported recipe component role"
                with _verification_lock:
                    _verified[key] = (resolved, expected, after)
                return "ready", ""
    except (OSError, ValueError) as error:
        return "invalid", "cannot verify %s: %s" % (asset["filename"], error)
    return "present_unverified", ""


def _template_record(variant=TEMPLATE_VARIANT):
    return {"name": TEMPLATE_NAME, "revision": TEMPLATE_REVISION, "url": TEMPLATE_URL,
            "variant": variant, "sha256": TEMPLATE_SHA256,
            "graph_sha256": _graph_sha256(variant),
            "license": dict(TEMPLATE_LICENSE)}


def _static_manifest(variant=TEMPLATE_VARIANT):
    recipe_id, assets = _variant(variant)
    return {"id": recipe_id, "title": "Prepare FLUX.2 Klein distilled",
            "template": _template_record(variant), "assets": copy.deepcopy(list(assets))}


def _manifest_hash(variant=TEMPLATE_VARIANT):
    encoded = json.dumps(_static_manifest(variant), sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _source_path(asset):
    return asset["url"].split("/resolve/" + asset["revision"] + "/", 1)[1]


def manifest(verify=False, *, variant=TEMPLATE_VARIANT):
    """Return a lightweight plan; verify=True explicitly hashes present files."""
    static = _static_manifest(variant)
    rows, details = [], []
    for asset in static["assets"]:
        row = dict(asset)
        row["source_path"] = _source_path(asset)
        row["state"], detail = (_state(asset, verify) if variant == TEMPLATE_VARIANT else
                                _state(asset, verify, variant=variant))
        rows.append(row)
        if detail:
            details.append(detail)
    return {"id": static["id"], "variant": variant, "title": static["title"],
            "manifest_sha256": _manifest_hash(variant), "template": static["template"],
            "assets": rows, "total_bytes": sum(item["size_bytes"] for item in static["assets"]),
            "ready": all(row["state"] == "ready" for row in rows),
            "detail": "; ".join(details) or _worker_detail()}


def _worker_detail():
    with _lock:
        return _prepare_detail


def _build_ui_graph(template=None, *, variant=TEMPLATE_VARIANT):
    _, assets = _variant(variant)
    raw = (TEMPLATE_PATH.read_bytes() if template is None else
           template if isinstance(template, bytes) else Path(template).read_bytes())
    if hashlib.sha256(raw).hexdigest() != TEMPLATE_SHA256:
        raise RuntimeError("bundled official workflow template failed its pinned SHA256")
    graph = json.loads(raw.decode("utf-8"))
    distilled = next((item for item in graph["definitions"]["subgraphs"]
                      if item.get("name") == "Text to Image (Flux.2 Klein 4B Distilled)"), None)
    if distilled is None:
        raise RuntimeError("pinned official template has no distilled Klein subgraph")
    for node in distilled["nodes"]:
        node["mode"] = 0
    unet = next((node for node in distilled["nodes"] if node.get("type") == "UNETLoader"), None)
    if unet is None:
        raise RuntimeError("pinned distilled subgraph has no UNETLoader")
    unet["widgets_values"][0] = assets[0]["filename"]
    latent = next((node for node in distilled["nodes"]
                   if node.get("type") == "EmptyFlux2LatentImage"), None)
    scheduler = next((node for node in distilled["nodes"]
                      if node.get("type") == "Flux2Scheduler"), None)
    noise = next((node for node in distilled["nodes"] if node.get("type") == "RandomNoise"), None)
    if latent is None or scheduler is None or noise is None:
        raise RuntimeError("pinned distilled subgraph is missing size, scheduler or noise nodes")
    latent["widgets_values"][:2] = [WIDTH, HEIGHT]
    scheduler["widgets_values"] = [STEPS, WIDTH, HEIGHT]
    noise["widgets_values"] = [SEED, CONTROL_AFTER_GENERATE]
    graph["nodes"] = [node for node in graph["nodes"] if node.get("id") in {76, 77, 78}]
    graph["nodes"] = [copy.deepcopy(node) for node in graph["nodes"]]
    prompt_node = next(node for node in graph["nodes"] if node["id"] == 76)
    prompt_node["widgets_values"] = [PROMPT]
    subgraph_node = next(node for node in graph["nodes"] if node["id"] == 77)
    subgraph_node["mode"] = 0
    # Keep each host proxy widget aligned with the nested input/child widget
    # value. This makes the visible canvas settings authoritative on load.
    subgraph_node["widgets_values"] = [
        PROMPT, WIDTH, HEIGHT, assets[0]["filename"], assets[1]["filename"],
        assets[2]["filename"], SEED, CONTROL_AFTER_GENERATE]
    save_node = next(node for node in graph["nodes"] if node["id"] == 78)
    save_node["widgets_values"] = ["OA-Flux2-Klein"]
    save_node["mode"] = 0
    graph["links"] = [link for link in graph["links"] if link[0] in {156, 157}]
    graph["definitions"]["subgraphs"] = [distilled]
    graph["last_node_id"] = max(node["id"] for node in graph["nodes"])
    graph["last_link_id"] = max(link[0] for link in graph["links"])
    return graph


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _served_graph_bytes():
    """The exact canonical bytes the graph route serves and its digest binds."""
    return _canonical_json(ui_workflow())


def _image_graph_bytes():
    from .oa_image import selected_image_policy
    policy = selected_image_policy()
    graph = json.loads(IMAGE_RECIPE_PATH.read_text(encoding="utf-8"))
    for node in graph["nodes"]:
        if node.get("type") == "OAImageGenerate":
            node["widgets_values"][2] = policy
    graph["extra"]["oa_recipe"]["selection_policy"] = policy
    return _canonical_json(graph)


def _image_recipe_ready():
    from .oa_image import default_selection_policy
    try:
        default_selection_policy()
        return True, ""
    except ValueError as error:
        return False, str(error)


def _graph_sha256(variant=TEMPLATE_VARIANT):
    return hashlib.sha256(_canonical_json(_build_ui_graph(variant=variant))).hexdigest()


def ui_workflow(template=None, *, variant=TEMPLATE_VARIANT):
    """Return the official Comfy UI graph with only the pinned distilled path."""
    recipe_id, _ = _variant(variant)
    graph = _build_ui_graph(template, variant=variant)
    graph.setdefault("extra", {})["oa_workflow"] = {
        "id": recipe_id, "manifest_sha256": _manifest_hash(variant),
        "variant": variant}
    return graph


def api_workflow(template=None, width=WIDTH, height=HEIGHT, prompt=PROMPT, seed=SEED,
                 *, variant=TEMPLATE_VARIANT):
    """Convert the pinned official distilled subgraph to Comfy's API prompt."""
    _, assets = _variant(variant)
    raw = (TEMPLATE_PATH.read_bytes() if template is None else
           template if isinstance(template, bytes) else Path(template).read_bytes())
    if hashlib.sha256(raw).hexdigest() != TEMPLATE_SHA256:
        raise RuntimeError("official workflow template failed its pinned SHA256")
    source = json.loads(raw.decode("utf-8"))
    subgraph = next((item for item in source["definitions"]["subgraphs"]
                     if item.get("name") == "Text to Image (Flux.2 Klein 4B Distilled)"), None)
    if subgraph is None:
        raise RuntimeError("pinned official template has no distilled Klein subgraph")
    links = {link["id"]: link for link in subgraph["links"]}
    external = {link_id: interface["name"] for interface in subgraph["inputs"]
                for link_id in interface["linkIds"]}
    values = {"value": width, "value_1": height,
              "unet_name": assets[0]["filename"], "clip_name": assets[1]["filename"],
              "vae_name": assets[2]["filename"], "text": prompt}
    api = {}
    for node in subgraph["nodes"]:
        inputs = {}
        for spec in node.get("inputs", []):
            link_id = spec.get("link")
            if link_id is None:
                continue
            link = links[link_id]
            if link["origin_id"] < 0:
                name = external.get(link_id)
                if name not in values:
                    raise RuntimeError("unexpected official workflow input %r" % name)
                inputs[spec["name"]] = values[name]
            else:
                inputs[spec["name"]] = [str(link["origin_id"]), link["origin_slot"]]
        widgets, kind = node.get("widgets_values", []), node["type"]
        if kind == "KSamplerSelect":
            inputs["sampler_name"] = widgets[0]
        elif kind == "RandomNoise":
            inputs["noise_seed"] = seed
        elif kind == "EmptyFlux2LatentImage":
            inputs["batch_size"] = widgets[2]
        elif kind == "UNETLoader":
            inputs["weight_dtype"] = widgets[1]
        elif kind == "CLIPLoader":
            inputs["type"], inputs["device"] = widgets[1], widgets[2]
        elif kind == "CFGGuider":
            inputs["cfg"] = widgets[0]
        elif kind == "Flux2Scheduler":
            inputs["steps"] = STEPS
        api[str(node["id"])] = {"class_type": kind, "inputs": inputs}
    output_link = links[subgraph["outputs"][0]["linkIds"][0]]
    outer_node = next(node for node in source["nodes"] if node["type"] == subgraph["id"])
    outer_link = next(link for link in source["links"] if link[1] == outer_node["id"])
    save_node = next(node for node in source["nodes"] if node["id"] == outer_link[3])
    save_id = str(save_node["id"])
    api[save_id] = {"class_type": "SaveImage", "inputs": {
        "images": [str(output_link["origin_id"]), output_link["origin_slot"]],
        "filename_prefix": "OA-Flux2-Klein"}}
    return api, save_id


def provider_data(*, variant=TEMPLATE_VARIANT):
    """Export finite native adapter data from the pinned graph and asset owner.

    This describes an admitted recipe. Host binding and verified readiness are
    separate receiving checks; exporting never reads model files or delivers.
    """
    recipe_id, assets = _variant(variant)
    graph, output_node = api_workflow(variant=variant)

    def slot(kind, name):
        nodes = [key for key, node in graph.items() if node["class_type"] == kind]
        if len(nodes) != 1 or name not in graph[nodes[0]]["inputs"]:
            raise RuntimeError("pinned graph has no unique %s/%s slot" % (kind, name))
        return {"node": nodes[0], "input": name}

    components = []
    for asset in assets:
        components.append({key: asset[key] for key in
                           ("role", "folder", "filename", "size_bytes", "sha256",
                            "repository", "revision")})
        components[-1]["source_path"] = _source_path(asset)
    result = {"schema": "oa.comfy.recipe@1", "id": recipe_id, "variant": variant,
              "model_alias": recipe_id, "template": {
                  "name": TEMPLATE_NAME, "revision": TEMPLATE_REVISION,
                  "url": TEMPLATE_URL, "sha256": TEMPLATE_SHA256},
              "manifest_sha256": _manifest_hash(variant), "components": components,
              "graph": graph, "graph_sha256": hashlib.sha256(_canonical_json(graph)).hexdigest(),
              "slots": {"prompt": slot("CLIPTextEncode", "text"),
                        "seed": slot("RandomNoise", "noise_seed")},
              "output_node": output_node,
              "request": {"kind": "generate", "count": 1, "sizes": ["512x512"]}}
    # The two PrimitiveInt nodes share a class; their API edges identify width
    # and height without maintaining a second graph/node mapping.
    latent = next(node for node in graph.values() if node["class_type"] == "EmptyFlux2LatentImage")
    for name in ("width", "height"):
        result["slots"][name] = {"node": latent["inputs"][name][0], "input": "value"}
    if len(_canonical_json(result)) > 64 * 1024:
        raise RuntimeError("pinned provider recipe exceeds 64 KiB")
    return result


def recipe_inventory():
    """Describe supported recipes without model inspection or acquisition."""
    rows, ids, aliases = [], set(), set()
    for variant in (TEMPLATE_VARIANT, BF16_VARIANT):
        recipe = provider_data(variant=variant)
        if recipe["id"] in ids or recipe["model_alias"] in aliases:
            raise ValueError("duplicate provider recipe ID or alias")
        ids.add(recipe["id"])
        aliases.add(recipe["model_alias"])
        rows.append({"recipe": recipe, "manifest_path": PLAN_ROUTE + "?variant=" + variant})
    result = {"schema": "oa.comfy.recipes@1", "recipes": rows}
    if len(rows) > MAX_RECIPES or len(_canonical_json(result)) > MAX_RECIPES_BYTES:
        raise ValueError("provider recipe inventory exceeds its bound")
    return result


def _deliver_asset(asset, cancelled=None):
    path = _model_path(asset)
    os.makedirs(path.parent, exist_ok=True)
    deliver = _deliver
    if deliver is None:
        from .service import deliver as service_deliver
        service_deliver(asset["url"], str(path.parent), asset["filename"],
                        size=asset["size_bytes"], digest="sha256:" + asset["sha256"], _cancelled=cancelled)
    else:
        deliver(asset["url"], str(path.parent), asset["filename"],
                size=asset["size_bytes"], digest="sha256:" + asset["sha256"])
    state, detail = _state(asset, verify=True)
    if state != "ready":
        raise RuntimeError(detail or ("delivered file %s failed pinned verification" % asset["filename"]))


def _prepare_worker(missing, cancelled):
    global _prepare_detail
    try:
        if cancelled:
            from . import service
            for asset in missing:
                selected = cancelled.get(asset["filename"])
                if selected is not None:
                    service.check_cancelled_preparation(asset, _model_path(asset), selected)
        for asset in missing:
            _deliver_asset(asset, cancelled.get(asset["filename"]))
        with _lock:
            _prepare_detail = ("The selected model file is verified." if cancelled
                               else "All pinned model files are verified and ready to load.")
    except Exception as error:
        with _lock:
            _prepare_detail = "Preparation stopped: %s" % error


def _inspect_assets(variant=TEMPLATE_VARIANT):
    _, assets = _variant(variant)
    return [(asset, _state(asset, verify=True) if variant == TEMPLATE_VARIANT else
             _state(asset, verify=True, variant=variant)) for asset in assets]


def _selected_cancelled_assets(value, assets):
    if value is None:
        return {}
    from . import service
    if not isinstance(value, list) or len(value) != 1:
        raise ValueError("Select one exact cancelled recipe delivery before starting again.")
    selected = {}
    for row in value:
        if not isinstance(row, dict) or set(row) != {"filename", "key", "history_epoch", "attempt"}:
            raise ValueError("A selected cancelled delivery has an invalid identity.")
        name = row["filename"]
        if (not isinstance(name, str) or name not in assets or name in selected
                or any(not isinstance(row[key], str) or not row[key] for key in ("key", "history_epoch"))):
            raise ValueError("A selected cancelled delivery differs from the missing recipe files.")
        service.attempt(row)
        asset = assets[name]
        identity = {key: row[key] for key in ("key", "history_epoch", "attempt")}
        service.check_cancelled_preparation(asset, _model_path(asset), identity)
        selected[name] = identity
    return selected


def _start_prepare(manifest_sha256, approve, inspected=None, cancelled=None):
    global _prepare_thread, _prepare_detail
    if approve is not True or manifest_sha256 != _manifest_hash():
        return {"outcome": "refused", "detail": "The explicit approval or pinned manifest hash is invalid."}
    rows = inspected if inspected is not None else _inspect_assets()
    invalid = [detail for _, (state, detail) in rows if state == "invalid"]
    if invalid:
        return {"outcome": "refused", "detail": "; ".join(invalid)}
    missing = [asset for asset, (state, _) in rows if state != "ready"]
    if not missing:
        return {"outcome": "already_ready", "detail": "All pinned model files are verified and ready to load."}
    try:
        selected = _selected_cancelled_assets(cancelled, {asset["filename"]: asset for asset in missing})
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        return {"outcome": "refused", "detail": str(error)}
    if selected:
        missing = [asset for asset in missing if asset["filename"] in selected]
    with _lock:
        if _prepare_thread is not None and _prepare_thread.is_alive():
            return {"outcome": "busy", "detail": "This workflow is already being prepared."}
        _prepare_detail = "Preparing %d missing model file(s) through OA." % len(missing)
        _prepare_thread = threading.Thread(target=_prepare_worker, args=(missing, selected),
                                           name="oa-klein-prepare", daemon=True)
        _prepare_thread.start()
    return {"outcome": "started", "detail": "OA preparation started for %d missing model file(s)." % len(missing)}


def _service_preflight():
    from . import service
    return service.preflight()


def _host_name(value):
    try:
        return urlsplit("//" + value).hostname
    except (TypeError, ValueError):
        return None


def _allowed_hosts(request):
    allowed = {"localhost", "127.0.0.1", "::1"}
    server = __import__("sys").modules.get("server")
    instance = getattr(getattr(server, "PromptServer", None), "instance", None)
    address = getattr(instance, "address", None)
    if isinstance(address, str) and address not in ("0.0.0.0", "::", ""):
        try:
            allowed.add(ipaddress.ip_address(address.split("%", 1)[0]).compressed)
        except ValueError:
            allowed.add(address.rstrip(".").casefold())
    try:
        for row in socket.getaddrinfo(socket.gethostname(), None, type=socket.SOCK_STREAM):
            try:
                allowed.add(ipaddress.ip_address(row[4][0].split("%", 1)[0]).compressed)
            except ValueError:
                continue
    except OSError:
        pass
    for item in os.environ.get(ALLOWED_HOSTS_ENV, "").split(","):
        name = _host_name(item.strip())
        if name:
            try:
                allowed.add(ipaddress.ip_address(name.split("%", 1)[0]).compressed)
            except ValueError:
                allowed.add(name.rstrip(".").casefold())
    name = _host_name(request.host)
    if not name:
        return False
    try:
        normalized = ipaddress.ip_address(name.split("%", 1)[0]).compressed
    except ValueError:
        normalized = name.rstrip(".").casefold()
    return normalized in allowed


def _same_origin(request):
    fetch_site = request.headers.get("Sec-Fetch-Site", "").casefold()
    if fetch_site == "cross-site" or not _allowed_hosts(request):
        return False
    origin = request.headers.get("Origin", "")
    if not origin or origin == "null":
        return False
    try:
        parsed = urlsplit(origin)
        return (parsed.scheme in ("http", "https") and bool(parsed.netloc)
                and not parsed.path and not parsed.query and not parsed.fragment
                and parsed.username is None and parsed.password is None
                and parsed.netloc.casefold() == request.host.casefold()
                and parsed.scheme == request.scheme)
    except (ValueError, UnicodeError):
        return False


def issue_arrival(now=None):
    """Return a one-time token for the Panel's ?oa_recipe=picture open."""
    import secrets
    import time
    now = time.monotonic() if now is None else now
    token = secrets.token_hex(32)
    with _lock:
        for old in [key for key, expiry in _arrivals.items() if expiry <= now]:
            del _arrivals[old]
        while len(_arrivals) >= ARRIVAL_LIMIT:
            del _arrivals[min(_arrivals, key=_arrivals.get)]
        _arrivals[token] = now + ARRIVAL_SECONDS
    return token


def spend_arrival(token, now=None):
    """Spend a token once; False for an unknown, spent or expired token."""
    import time
    now = time.monotonic() if now is None else now
    with _lock:
        expiry = _arrivals.pop(token, None)
    return expiry is not None and expiry > now


def _may_issue_arrival(request):
    return (request.headers.get(ARRIVAL_ISSUE_HEADER) == "issue"
            and "Origin" not in request.headers
            and request.headers.get("Sec-Fetch-Site", "") == ""
            and _allowed_hosts(request))


def install(routes=None):
    """Register the manifest, explicit preparation and ready-only graph routes."""
    if routes is None:
        server = __import__("server")
        instance = getattr(getattr(server, "PromptServer", None), "instance", None)
        if instance is None:
            return False
        routes = instance.routes
    if any(table is routes for table in _registered):
        return True

    @routes.get(PLAN_ROUTE)
    async def get_plan(request):
        verify = request.query.get("verify", "")
        if verify not in ("", "1"):
            return web.json_response({"detail": "verify must be omitted or 1"}, status=400)
        variant = request.query.get("variant", TEMPLATE_VARIANT)
        try:
            _variant(variant)
        except ValueError as error:
            return web.json_response({"detail": str(error)}, status=400)
        import asyncio
        body = await asyncio.get_running_loop().run_in_executor(
            None, lambda: manifest(verify == "1", variant=variant))
        return web.json_response(body)

    @routes.get(RECIPES_ROUTE)
    async def get_recipes(_request):
        return web.json_response(recipe_inventory())

    @routes.get(IMAGE_RECIPE_ROUTE)
    async def get_image_recipe(_request):
        ready, detail = await asyncio.get_running_loop().run_in_executor(None, _image_recipe_ready)
        return web.json_response({"id": "oa-image-generate-preview-save-v1", "ready": ready,
                                  "detail": detail})

    @routes.post(ARRIVAL_ROUTE)
    async def post_arrival(request):
        if not _may_issue_arrival(request):
            return web.json_response({"detail": "Only the OpenAbstractions Panel opens the picture recipe."}, status=403)
        return web.json_response({"token": issue_arrival()})

    @routes.post(ARRIVAL_ROUTE + "/{token}")
    async def spend(request):
        if not _same_origin(request) or not spend_arrival(request.match_info.get("token", "")):
            return web.json_response({"detail": "This link does not open the picture recipe."}, status=404)
        return web.Response(status=204)

    @routes.post(PREPARE_ROUTE)
    async def prepare(request):
        if not _same_origin(request):
            return web.json_response({"outcome": "refused", "detail": "Preparation accepts same-origin requests only."}, status=403)
        if request.content_type != "application/json":
            return web.json_response({"outcome": "refused", "detail": "Preparation requires application/json."}, status=415)
        raw = await request.read()
        if len(raw) > MAX_BODY_BYTES:
            return web.json_response({"outcome": "refused", "detail": "Request body is too large."}, status=413)
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            return web.json_response({"outcome": "refused", "detail": "Expected a JSON approval."}, status=400)
        if not isinstance(body, dict) or set(body) - {"cancelled"} != {"manifest_sha256", "approve"}:
            return web.json_response({"outcome": "refused", "detail": "Expected a manifest approval and optional cancelled selection."}, status=400)
        if body.get("approve") is not True:
            return web.json_response({"outcome": "refused", "detail": "Explicit approval is required."}, status=400)
        if body.get("manifest_sha256") != _manifest_hash():
            return web.json_response({"outcome": "refused", "detail": "The pinned manifest hash is invalid."}, status=409)
        import asyncio
        inspected = await asyncio.get_running_loop().run_in_executor(None, _inspect_assets)
        invalid = [detail for _, (state, detail) in inspected if state == "invalid"]
        if invalid:
            return web.json_response({"outcome": "refused", "detail": "; ".join(invalid)}, status=409)
        if all(state == "ready" for _, (state, _) in inspected):
            return web.json_response({"outcome": "already_ready",
                                      "detail": "All pinned model files are verified and ready to load."})
        preflight = await asyncio.get_running_loop().run_in_executor(None, _service_preflight)
        if not preflight.ready:
            return web.json_response({"outcome": "refused", "detail": preflight.detail})
        result = await asyncio.get_running_loop().run_in_executor(
            None, _start_prepare, body.get("manifest_sha256"), body.get("approve"), inspected, body.get("cancelled"))
        return web.json_response(result, status=409 if result["outcome"] == "refused" else 200)

    @routes.get(GRAPH_ROUTE)
    async def get_graph(_request):
        import asyncio
        body = await asyncio.get_running_loop().run_in_executor(None, manifest, True)
        if not body["ready"]:
            return web.json_response({"detail": body["detail"] or "Prepare and verify every model file first."}, status=409)
        try:
            graph_bytes = await asyncio.get_running_loop().run_in_executor(None, _served_graph_bytes)
        except Exception as error:
            return web.json_response({"detail": str(error)}, status=500)
        return web.Response(body=graph_bytes, content_type="application/json",
                            headers={GRAPH_DIGEST_HEADER: hashlib.sha256(graph_bytes).hexdigest()})

    @routes.get(IMAGE_GRAPH_ROUTE)
    async def get_image_graph(_request):
        ready, detail = await asyncio.get_running_loop().run_in_executor(None, _image_recipe_ready)
        if not ready:
            return web.json_response({"detail": detail or "Choose an image model in OA setup."}, status=409)
        try:
            graph_bytes = await asyncio.get_running_loop().run_in_executor(None, _image_graph_bytes)
        except (ValueError, RuntimeError) as error:
            return web.json_response({"detail": str(error)}, status=409)
        return web.Response(body=graph_bytes, content_type="application/json",
                            headers={GRAPH_DIGEST_HEADER: hashlib.sha256(graph_bytes).hexdigest()})

    _registered.append(routes)
    return True
