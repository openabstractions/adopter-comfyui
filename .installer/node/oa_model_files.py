"""OpenAbstractions picks a ComfyUI template's model files (MODEL-AND-ENGINE.md, Step 1).

The mapping table oa_model_files.json beside this file names each known
template model file and each file OA keeps in its local store by family, role
and variant. OA core keeps no ComfyUI names; the table is this node pack's data.

register_store() adds the store's set folders the table names
(%USERPROFILE%\\OA-models\\<set>, or OA_COMFYUI_MODELS_ROOT\\<set>) to ComfyUI's
model folders with folder_paths.add_model_folder_path. ComfyUI reads the files
in place. A folder that is a link, or whose real path is not directly under the
store root, is skipped. Files reach the store only through OA download jobs.
guard_listing() first wraps ComfyUI's listing so a set folder shows only the
files a loader ComfyUI ships loads (Listing); without that guard no folder is
registered.

resolve() answers, per loader node of a workflow, the file this ComfyUI loads:
it identifies the node's file (Hugging Face paths-info SHA-256, then the table
by name, checked against a local file's GGUF or safetensors header), ranks the
same family and role's variants that a loader ComfyUI ships and has present
loads (present and fits > present > downloadable), and names the loader swap
when the chosen variant needs another loader. A swap is one of the table's
listed pairs, and Step 1 never targets a custom node's loader. The route POST /openabstractions/models/resolve serves it to the
frontend extension (web/oa_model_files.js), which rewrites the workflow in
memory as it loads; the template file stays untouched.

Every failure leaves the workflow as authored and writes one log line.
Standard library only; ComfyUI's own modules are read from sys.modules when called.
"""

from contextlib import contextmanager
import importlib.util
import ipaddress
import json
import logging
import os
from pathlib import Path
import stat
import struct
import sys
import threading
import urllib.parse
import urllib.request

# The standalone adapter is the shared owner of the held native walk.
_file_owner_spec = importlib.util.spec_from_file_location(
    "_oa_model_file_owner", Path(__file__).with_name("oa_native_host.py"))
_file_owner = importlib.util.module_from_spec(_file_owner_spec)
_file_owner_spec.loader.exec_module(_file_owner)
_name_component = _file_owner._name_component
_native_component = _file_owner._native_component
_held_walk = _file_owner._held_walk

LOG = logging.getLogger("openabstractions")
HERE = os.path.dirname(os.path.abspath(__file__))
TABLE_PATH = os.path.join(HERE, "oa_model_files.json")
STORE_ENV = "OA_COMFYUI_MODELS_ROOT"
ROUTE = "/openabstractions/models/resolve"
TABLE_LIMIT = 1 << 20
BODY_LIMIT = 256 * 1024
MAX_NODES = 512
HEADER_LIMIT = 16 << 20
GGUF_KEYS = 64
HF_TIMEOUT = 3.0


# The mapping table.

class TableError(ValueError):
    pass


def _text(value, name):
    if not isinstance(value, str) or not value or len(value) > 512:
        raise TableError("%s is not a short text" % name)
    return value


class Table:
    """The checked mapping table."""

    def __init__(self, data):
        if not isinstance(data, dict) or data.get("version") != 1:
            raise TableError("the table is not version 1")
        self.loaders = {}
        for cls, spec in (data.get("loaders") or {}).items():
            widgets = spec.get("widgets") if isinstance(spec, dict) else None
            if not (isinstance(widgets, list) and widgets and all(isinstance(w, str) for w in widgets)
                    and spec.get("file") in widgets):
                raise TableError("loader %r is malformed" % cls)
            self.loaders[_text(cls, "loader")] = {"category": _text(spec.get("category"), "category"),
                                                  "file": spec["file"], "widgets": list(widgets)}
            if spec.get("custom_node") is not None:
                self.loaders[cls]["custom_node"] = _text(spec["custom_node"], "custom_node")
        self.swaps = {}
        for swap in data.get("swaps") or []:
            source, target, widgets = swap.get("from"), swap.get("to"), swap.get("widgets")
            if source not in self.loaders or target not in self.loaders or not isinstance(widgets, dict):
                raise TableError("swap %r is malformed" % (swap,))
            if any(new not in self.loaders[target]["widgets"] or old not in self.loaders[source]["widgets"]
                   for new, old in widgets.items()):
                raise TableError("swap %s to %s names an unknown widget" % (source, target))
            if not self.core(target):
                raise TableError("swap %s to %s needs a custom node; Step 1 installs none" % (source, target))
            self.swaps[(source, target)] = dict(widgets)
        self.sets = [_text(name, "set") for name in data.get("sets") or []]
        if not all(_name_component(name) for name in self.sets):
            raise TableError("a set name is a path")
        self.files = []
        for item in data.get("files") or []:
            entry = {key: _text(item.get(key), key) for key in ("name", "family", "role", "variant", "label")}
            loaders = item.get("loaders")
            if not isinstance(loaders, list) or any(cls not in self.loaders for cls in loaders):
                raise TableError("%s names an unknown loader" % entry["name"])
            entry["loaders"] = list(loaders)
            for key in ("made_for", "set", "sha256"):
                if item.get(key) is not None:
                    entry[key] = _text(item[key], key)
            if entry.get("set") is not None:
                if entry["set"] not in self.sets:
                    raise TableError("%s names an unlisted set" % entry["name"])
                if not _name_component(entry["name"]):
                    raise TableError("a store file name is a path")
            size = item.get("size")
            if entry.get("set") and (type(size) is not int or size <= 0):
                raise TableError("%s has no pinned size" % entry["name"])
            entry["size"] = size if type(size) is int else None
            entry["header"] = dict(item.get("header") or {})
            self.files.append(entry)
        self.by_name = {}
        self.by_digest = {}
        for entry in self.files:
            self.by_name.setdefault(entry["name"], entry)
            digest = _digest(entry.get("sha256"))
            if digest is not None:
                self.by_digest.setdefault(digest, entry)

    def variants(self, family, role):
        return [entry for entry in self.files if entry["family"] == family and entry["role"] == role]

    def core(self, cls):
        """True when ComfyUI itself ships the loader class; a custom node's loader is never used."""
        return cls in self.loaders and "custom_node" not in self.loaders[cls]

    def categories(self, entry):
        """The model categories ComfyUI's own loaders list entry under."""
        return {self.loaders[cls]["category"] for cls in entry["loaders"] if self.core(cls)}


def load_table(path=TABLE_PATH):
    """The table, or None with one log line when it is missing or unreadable."""
    try:
        with open(path, "rb") as stream:
            raw = stream.read(TABLE_LIMIT + 1)
        if len(raw) > TABLE_LIMIT:
            raise TableError("the table is larger than %d bytes" % TABLE_LIMIT)
        return Table(json.loads(raw))
    except (OSError, ValueError, AttributeError, TypeError) as error:
        LOG.warning("[openabstractions] model files stay as authored; the mapping table is unreadable: %s", error)
        return None


# OA's local store.

def store_root(environ=None):
    """OA_COMFYUI_MODELS_ROOT, else %USERPROFILE%\\OA-models; None when neither is an absolute path."""
    environ = os.environ if environ is None else environ
    root = environ.get(STORE_ENV) or ""
    if not root:
        home = environ.get("USERPROFILE") or environ.get("HOME") or ""
        root = os.path.join(home, "OA-models") if home else ""
    return root if root and os.path.isabs(root) else None


def _model_name(value):
    if not isinstance(value, (str, os.PathLike)):
        return None
    name = Path(value)
    if name.anchor or not name.parts or not all(_native_component(part) for part in name.parts):
        return None
    return name


def _digest(value):
    if not isinstance(value, str) or len(value) != 64:
        return None
    try:
        raw = bytes.fromhex(value)
    except ValueError:
        return None
    return raw.hex() if len(raw) == 32 else None


@contextmanager
def _held_path(path, *, admit_parent=None):
    with _held_walk(path, admit_parent=admit_parent) as (facts, _, _):
        yield facts


def _link_observation(alias, held):
    info = os.lstat(alias)
    if not os.path.samestat(info, held):
        raise ValueError("model link changed")
    return info.st_mode, info.st_ctime_ns, info.st_mtime_ns, os.readlink(alias)


class Store:
    """The table's ordinary set directories and their admitted identities."""

    def __init__(self, root, table):
        self.root = None
        self.root_identity = None
        self.folders = {}
        self.identities = {}
        if root is None or table is None:
            return
        try:
            with _held_path(root) as root_facts:
                root_info = root_facts[-1]
                if not stat.S_ISDIR(root_info.st_mode):
                    return
                self.root = os.fspath(Path(root))
                self.root_identity = root_info
                for name in table.sets:
                    folder = os.path.join(self.root, name)
                    try:
                        with _held_path(folder) as facts:
                            if stat.S_ISDIR(facts[-1].st_mode) and os.path.samestat(facts[-2], root_info):
                                self.folders[name] = folder
                                self.identities[name] = facts[-1]
                    except (OSError, ValueError):
                        continue
        except (OSError, ValueError):
            return

    def enrolled_set(self, parents):
        if len(parents) < 2 or self.root_identity is None or not os.path.samestat(parents[-2], self.root_identity):
            raise ValueError("enrolled model root changed")
        for name, identity in self.identities.items():
            if os.path.samestat(parents[-1], identity):
                return name
        raise ValueError("enrolled model set changed")

    @contextmanager
    def pinned(self, entry):
        """Hold one ordinary file after proving its enrolled root and set."""
        set_name = entry.get("set")
        folder = self.folders.get(set_name)
        if folder is None or entry.get("size") is None:
            raise ValueError("no pinned store file")
        if not _name_component(entry["name"]):
            raise ValueError("store file must have a single name")

        def admit(parents):
            if self.enrolled_set(parents) != set_name:
                raise ValueError("model file is in another set")

        path = os.path.join(folder, entry["name"])
        with _held_walk(path, admit_parent=admit) as (facts, _, fd):
            info = facts[-1]
            if not stat.S_ISREG(info.st_mode) or info.st_size != entry["size"]:
                raise ValueError("store file changed")
            yield path, info, fd

    def inspect(self, entry):
        """Read a selected OA file's header from the descriptor that admitted it."""
        try:
            with self.pinned(entry) as (path, _, fd):
                return path, _read_held_header(fd) if entry["header"] else {}
        except (OSError, ValueError):
            return None, {}

    def _matches_set(self, facts):
        for name, identity in self.identities.items():
            if any(os.path.samestat(info, identity) for info in facts):
                return True
            # A replacement of the registered spelling belongs to the guard
            # too, even when the replacement is itself a link.
            try:
                with _held_walk(self.folders[name], stop_at_link=True) as (current, _, _):
                    if any(os.path.samestat(info, current[-1]) for info in facts):
                        return True
            except FileNotFoundError:
                continue
        return False

    def _admit_candidate_parent(self, parents):
        if self._matches_set(parents):
            self.enrolled_set(parents)

    @contextmanager
    def _name_proof(self, path, *, candidate=False):
        """Hold the name's observed filesystem identity and its OA classification."""
        admit = self._admit_candidate_parent if candidate else None
        with _held_walk(path, stop_at_link=True, admit_parent=admit) as (facts, alias, _):
            if self._matches_set(facts):
                yield path, facts[-1], True
            elif alias is None:
                yield path, facts[-1], False
            else:
                before = _link_observation(alias, facts[-1])
                target = os.path.realpath(path, strict=True)
                with _held_path(target, admit_parent=admit) as resolved:
                    if (not os.path.samestat(resolved[-1], os.stat(path))
                            or _link_observation(alias, facts[-1]) != before):
                        raise ValueError("model alias changed")
                    owned_root = self.root_identity is not None and any(
                        os.path.samestat(info, self.root_identity) for info in resolved)
                    yield target, resolved[-1], owned_root or self._matches_set(resolved)

    def holds(self, path):
        """True for OA, False for a proven separate tree, None if uncertain."""
        if not self.identities:
            return False
        try:
            with self._name_proof(path) as (_, _, owned):
                return owned
        except (OSError, ValueError):
            return None

    def authored_header(self, path, entry):
        try:
            with self._name_proof(path, candidate=True) as (target, info, owned):
                if owned:
                    with self.pinned(entry) as (_, actual, fd):
                        if os.path.samestat(info, actual):
                            return _read_held_header(fd)
                else:
                    with _held_walk(target, admit_parent=self._admit_candidate_parent) as (facts, _, fd):
                        if (stat.S_ISREG(facts[-1].st_mode) and os.path.samestat(info, facts[-1])
                                and not self._matches_set(facts)):
                            return _read_held_header(fd)
        except (OSError, ValueError):
            pass
        return {}


def store_categories(store, table):
    """{category: [folder]}: each set folder under the categories its loaders use."""
    categories, seen = {}, {}
    for entry in table.files:
        set_name = entry.get("set")
        folder = store.folders.get(set_name)
        if folder is None:
            continue
        identity = store.identities[set_name]
        for category in sorted(table.categories(entry)):
            admitted = seen.setdefault(category, [])
            if not any(os.path.samestat(identity, other) for other in admitted):
                admitted.append(identity)
                categories.setdefault(category, []).append(folder)
    return categories


def register_store(add=None, environ=None, table=None):
    """Add the store's set folders to ComfyUI's model folders. Returns {category: [folder]}."""
    table = load_table() if table is None else table
    if table is None:
        return {}
    return _register_store(Store(store_root(environ), table), table, add)


def _register_store(store, table, add):
    categories = store_categories(store, table)
    if not categories:
        return {}
    if add is None:
        add = getattr(sys.modules.get("folder_paths"), "add_model_folder_path", None)
        if add is None:
            return {}
    for category, folders in categories.items():
        for folder in folders:
            try:
                with _held_path(folder) as facts:
                    store.enrolled_set(facts)
                    add(category, folder)
            except Exception as error:  # the extension never stops ComfyUI from starting
                LOG.warning("[openabstractions] the %s folder %s is not added: %s", category, folder, error)
    return categories


# What ComfyUI lists from the store.

class Listing:
    """The store files ComfyUI may list and open. A file in one of the table's
    set folders is visible in a category only when the table names it in that
    folder at its pinned size and a loader ComfyUI ships, present in this
    ComfyUI, loads it as that category. sd.cpp-only files, custom-node files
    and unknown files stay hidden. classes() is the node classes present now."""

    def __init__(self, store, table, classes):
        self.store = store
        self.table = table
        self.classes = classes

    def inside(self, path):
        return self.store.holds(path)

    def openable(self, category, path):
        # Check the parent before touching a candidate, including the stock
        # loader's fallback folders. Only the external loader reopens a name.
        parent = self.inside(os.path.dirname(path))
        if parent is None:
            return False
        if parent:
            return self.visible(category, path)
        try:
            with self.store._name_proof(path, candidate=True) as (_, info, owned):
                return not owned and stat.S_ISREG(info.st_mode)
        except (OSError, ValueError):
            return False

    def visible(self, category, path):
        """True when the opened file is the pinned object for a present loader."""
        try:
            with _held_path(path, admit_parent=self.store.enrolled_set) as facts:
                entry = self.table.by_name.get(os.path.basename(os.path.realpath(path)))
                if entry is None:
                    return False
                present = self.classes()
                if not any(self.table.core(cls) and cls in present and self.table.loaders[cls]["category"] == category
                           for cls in entry["loaders"]):
                    return False
                with self.store.pinned(entry) as (_, held, _):
                    return os.path.samestat(facts[-1], held) and os.path.samestat(
                        facts[-2], self.store.identities[entry["set"]])
        except (OSError, ValueError):
            return False


GUARDED = "openabstractions_guarded"


def guard_listing(listing, folder_paths, model_manager):
    """Wrap ComfyUI's model listing so the store shows only visible files:
    folder_paths.get_filename_list_ (which get_filename_list and its cache
    filename_list_cache fill from), folder_paths.get_full_path (which every
    loader opens through) and the model library's
    ModelFileManager.get_model_file_list. Returns False, wrapping nothing,
    when this ComfyUI does not have that shape."""
    names = ("get_filename_list_", "get_full_path", "recursive_search", "map_legacy")
    manager = getattr(model_manager, "ModelFileManager", None)
    if not (folder_paths is not None and all(callable(getattr(folder_paths, n, None)) for n in names)
            and isinstance(getattr(folder_paths, "folder_names_and_paths", None), dict)
            and isinstance(getattr(folder_paths, "filename_list_cache", None), dict)
            and callable(getattr(manager, "get_model_file_list", None))):
        return False
    if getattr(folder_paths.get_full_path, GUARDED, False):
        return True
    list_names, stock_full_path = folder_paths.get_filename_list_, folder_paths.get_full_path
    model_list = manager.get_model_file_list

    def folders(category):
        return list((folder_paths.folder_names_and_paths.get(category) or ([], set()))[0])

    def get_filename_list_(folder_name):
        out, dirs, stamp = list_names(folder_name)
        # ComfyUI owns enumeration and its cache metadata. Authorize only the
        # names it already reported. OA never starts a recursive walk.
        if not listing.store.identities:
            return out, dirs, stamp
        return [name for name in out if get_full_path(folder_name, name) is not None], dirs, stamp

    def get_full_path(folder_name, filename):
        if not listing.store.identities:
            return stock_full_path(folder_name, filename)
        category = folder_paths.map_legacy(folder_name)
        relative = _model_name(filename)
        if relative is None:
            return None
        for folder in folders(category):
            candidate = os.path.join(folder, *relative.parts)
            if listing.openable(category, candidate):
                return candidate
        return None

    def get_model_file_list(self, folder_name):
        out = model_list(self, folder_name)
        if not listing.store.identities:
            return out
        category = folder_paths.map_legacy(folder_name)
        roots = folders(category)
        kept = []
        for item in out:
            index = item.get("pathIndex") if isinstance(item, dict) else None
            if isinstance(index, int) and 0 <= index < len(roots):
                relative = _model_name(item.get("name"))
                if relative is None or not listing.openable(category, os.path.join(roots[index], *relative.parts)):
                    continue
            kept.append(item)
        return kept

    for wrapper in (get_filename_list_, get_full_path, get_model_file_list):
        setattr(wrapper, GUARDED, listing)
    folder_paths.get_filename_list_ = get_filename_list_
    folder_paths.get_full_path = get_full_path
    manager.get_model_file_list = get_model_file_list
    return True


# Identity sources.

def hf_digest(url, fetch):
    """The SHA-256 Hugging Face paths-info reports for a resolve URL, or None.
    fetch(api_url, form_bytes) returns the response body."""
    if fetch is None or not isinstance(url, str):
        return None
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.hostname != "huggingface.co":
            return None
        segments = [urllib.parse.unquote(s) for s in parts.path.split("/") if s]
        if len(segments) < 5 or segments[2] != "resolve":
            return None
        repo, revision, path = segments[0] + "/" + segments[1], segments[3], "/".join(segments[4:])
        api = "https://huggingface.co/api/models/%s/paths-info/%s" % (
            urllib.parse.quote(repo), urllib.parse.quote(revision, safe=""))
        answer = json.loads(fetch(api, urllib.parse.urlencode({"paths": path}).encode("ascii")))
        for item in answer if isinstance(answer, list) else []:
            oid = (item.get("lfs") or {}).get("oid") if isinstance(item, dict) else None
            if item.get("path") == path:
                digest = _digest(oid)
                if digest is not None:
                    return digest
    except Exception as error:  # paths-info is one identity source among three
        LOG.info("[openabstractions] Hugging Face paths-info did not answer for %s: %s", url, error)
    return None


_hf_cache = {}
_hf_lock = threading.Lock()


def hf_fetch(api, form, timeout=HF_TIMEOUT):
    """POST form to Hugging Face's paths-info; answers are cached for this process."""
    key = (api, form)
    with _hf_lock:
        if key in _hf_cache:
            return _hf_cache[key]
    request = urllib.request.Request(api, data=form, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(1 << 20)
    with _hf_lock:
        _hf_cache[key] = body
    return body


_GGUF_SCALARS = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}


def _read_held_header(fd):
    """A local model file's identity facts: a GGUF file's general.* scalar
    metadata, or a safetensors file's __metadata__. {} when there is none."""
    try:
        with os.fdopen(os.dup(fd), "rb") as stream:
            magic = stream.read(8)
            if magic[:4] == b"GGUF":
                return _gguf(stream)
            if len(magic) == 8:
                (length,) = struct.unpack("<Q", magic)
                if 2 <= length <= HEADER_LIMIT:
                    meta = json.loads(stream.read(length)).get("__metadata__")
                    return {key: value for key, value in meta.items() if isinstance(value, str) and len(value) < 4096} \
                        if isinstance(meta, dict) else {}
    except (OSError, ValueError, struct.error, AttributeError):
        pass
    return {}


def _gguf(stream):
    def take(fmt):
        size = struct.calcsize(fmt)
        data = stream.read(size)
        if len(data) != size:
            raise ValueError("short GGUF header")
        return struct.unpack(fmt, data)[0]

    def text():
        length = take("<Q")
        if length > 1 << 16:
            raise ValueError("long GGUF string")
        return stream.read(length).decode("utf-8", "replace")

    stream.seek(8)
    take("<Q")  # tensor count
    count = take("<Q")
    facts = {}
    for _ in range(min(count, GGUF_KEYS)):
        key, kind = text(), take("<I")
        if kind == 8:
            value = text()
        elif kind in _GGUF_SCALARS:
            value = take(_GGUF_SCALARS[kind])
        else:  # an array: the general.* facts come before the tokenizer's arrays
            break
        if key.startswith("general."):
            facts[key] = value
    return facts


def identify(name, url, table, fetch=None, header=None):
    """The table entry a template model file is, or None. Hugging Face's
    SHA-256 names the file first, then the table by name; a local file's
    header that contradicts the table's facts leaves the file unidentified."""
    entry = None
    digest = hf_digest(url, fetch) if name not in table.by_name else None
    if digest:
        entry = table.by_digest.get(digest)
    if entry is None:
        entry = table.by_name.get(name)
    if entry is not None and entry["header"] and header is not None:
        facts = header(entry)
        if facts and any(facts.get(key) != value for key, value in entry["header"].items()):
            LOG.warning("[openabstractions] %s does not carry the header OA knows for %s; it stays as authored",
                        name, entry["label"])
            return None
    return entry


# Choosing a variant.

def _loadable(entry, current, present):
    """The ComfyUI loader class that loads entry in place of current, or None."""
    for cls in entry["loaders"]:
        if cls in present and present.table.core(cls) and (cls == current or (current, cls) in present.swaps):
            return cls
    return None


class Present:
    """The loader classes present in this ComfyUI, and the table's swap pairs."""

    def __init__(self, classes, table):
        self.classes = set(classes)
        self.table = table
        self.swaps = table.swaps

    def __contains__(self, cls):
        return cls in self.classes


def why_not(entry, authored, table):
    """A plain sentence saying why ComfyUI does not load OA's entry."""
    keep = "the template's %s file stays" % authored["variant"] if authored else "the template's file stays"
    if entry.get("made_for"):
        return "OA's %s is made for %s, which ComfyUI cannot load; %s." % (entry["label"], entry["made_for"], keep)
    if any(not table.core(cls) for cls in entry["loaders"]):
        return "OA's %s is a %s file, which ComfyUI loads only through a custom node; %s." % (
            entry["label"], entry["name"].rsplit(".", 1)[-1].upper(), keep)
    return "ComfyUI has no loader for OA's %s; %s." % (entry["label"], keep)


def choose(node, table, store, present, free_bytes=None, comfy_path=None, fetch=None):
    """One loader node's resolution. node is {key, type, value, models}."""
    cls = node.get("type")
    spec = table.loaders.get(cls)
    name = node.get("value")
    if spec is None or not isinstance(name, str):
        return None
    url = next((m.get("url") for m in node.get("models") or [] if isinstance(m, dict) and m.get("name") == name), None)
    local = None
    if comfy_path is not None:
        try:
            local = comfy_path(spec["category"], name)
        except Exception:
            local = None
    header = (lambda entry: store.authored_header(local, entry)) if local else None
    authored = identify(name, url, table, fetch, header)
    result = {"key": node.get("key"), "file": name, "type": cls, "from_oa": False}
    if authored is None:
        result["reason"] = "OA does not know %s; it stays as authored." % name
        return result
    result.update(family=authored["family"], role=authored["role"], variant=authored["variant"])
    ranked, reasons = [], []
    for order, entry in enumerate(table.variants(authored["family"], authored["role"])):
        stored, facts = store.inspect(entry)
        if entry is not authored and stored is None:
            continue  # neither the template's file nor one OA holds
        loader = _loadable(entry, cls, present)
        if loader is None:
            if stored is not None:
                reasons.append(why_not(entry, authored, table))
            continue
        if stored is not None and entry["header"]:
            if facts and any(facts.get(key) != value for key, value in entry["header"].items()):
                reasons.append("OA's %s does not carry the header OA pinned; it is not used." % entry["label"])
                continue
        here = stored or (local if entry is authored and local and store.holds(local) is False else None)
        fits = free_bytes is None or entry.get("size") is None or entry["size"] <= free_bytes
        rank = 0 if here and fits else 1 if here else 2
        ranked.append((rank, entry is not authored, order, entry, loader, stored))
    if not ranked:
        result["reason"] = " ".join(reasons) or "ComfyUI has no loader for %s; it stays as authored." % name
        return result
    rank, _, _, chosen, loader, stored = min(ranked, key=lambda item: item[:3])
    if stored is None:
        if not reasons:
            reasons.append("OA holds no other ComfyUI file for %s; the template's file stays." % authored["label"]
                           if rank == 2 else "ComfyUI already has the template's file.")
        result["reason"] = " ".join(reasons)
        return result
    widgets = _widgets(node.get("widgets_values"), table, cls, loader, chosen["name"])
    if widgets is None:
        result["reason"] = "The %s node's settings do not match ComfyUI's %s; it stays as authored." % (cls, cls)
        return result
    result.update(file=chosen["name"], type=loader, variant=chosen["variant"], from_oa=True,
                  widgets_values=widgets, title="file from OA: %s" % chosen["label"],
                  inputs=list(table.loaders[loader]["widgets"]), file_widget=table.loaders[loader]["file"],
                  reason="ComfyUI loads OA's %s%s." % (chosen["label"], "" if rank == 0 else
                                                       " (larger than the free memory ComfyUI reports)"))
    result["reason"] = " ".join([result["reason"]] + reasons)
    return result


def _widgets(values, table, source, target, file):
    """The target loader's widgets_values with file in its file widget, or None."""
    names = table.loaders[source]["widgets"]
    if not isinstance(values, list) or len(values) < 1 or len(values) > len(names) or \
            not all(isinstance(v, (str, int, float, bool)) for v in values):
        return None
    old = dict(zip(names, values))
    if source == target:
        mapping = {name: name for name in names}
    else:
        mapping = table.swaps.get((source, target))
        if mapping is None:
            return None
    out = []
    for name in table.loaders[target]["widgets"]:
        if name == table.loaders[target]["file"]:
            out.append(file)
        elif mapping.get(name) in old:
            out.append(old[mapping[name]])
        else:
            return None
    return out


def loader_nodes(workflow):
    """The {key, type, value, widgets_values, models} of a workflow's loader
    nodes, root and subgraph alike, as web/oa_model_files_graph.mjs sends them."""
    out = []
    def walk(nodes, scope):
        for node in nodes if isinstance(nodes, list) else []:
            if not isinstance(node, dict):
                continue
            values = node.get("widgets_values")
            models = (node.get("properties") or {}).get("models") or []
            loader = models or "Loader" in str(node.get("type"))
            if not (isinstance(values, list) and values and isinstance(values[0], str) and loader):
                continue
            out.append({"key": "%s:%s" % (scope, node.get("id")), "type": node.get("type"),
                        "value": values[0],
                        "widgets_values": values, "models": models})
    walk(workflow.get("nodes"), "root")
    for graph in (workflow.get("definitions") or {}).get("subgraphs") or []:
        if isinstance(graph, dict):
            walk(graph.get("nodes"), graph.get("id"))
    return out


def resolve(nodes, table, store, classes, free_bytes=None, comfy_path=None, fetch=None):
    """[resolution] for the loader nodes of one workflow."""
    present = Present(classes, table)
    out = []
    for node in nodes[:MAX_NODES]:
        if not isinstance(node, dict) or not isinstance(node.get("key"), str):
            continue
        try:
            found = choose(node, table, store, present, free_bytes, comfy_path, fetch)
        except Exception as error:  # one node's failure leaves that node as authored
            LOG.warning("[openabstractions] %s stays as authored: %r", node.get("value"), error)
            continue
        if found is not None:
            out.append(found)
    return out


# ComfyUI's side: its modules, which ComfyUI has imported before it loads this pack.

def _free_memory():
    try:
        return int(sys.modules["comfy.model_management"].get_free_memory())
    except Exception:  # unknown: every file fits
        return None


def _comfy_path(category, name):
    return sys.modules["folder_paths"].get_full_path(category, name)


def _class_mappings():
    return getattr(sys.modules.get("nodes"), "NODE_CLASS_MAPPINGS", None) or {}


def _classes():
    return set(_class_mappings())


def answer(body, table=None, environ=None, classes=None, free_bytes=None, comfy_path=None, fetch=None, *, store=None):
    """The route's JSON answer for a request body. Any failure answers no
    resolutions, which the frontend reads as "as authored"."""
    try:
        table = load_table() if table is None else table
        if table is None:
            return {"resolutions": [], "swaps": []}
        nodes = body.get("nodes") if isinstance(body, dict) else None
        if not isinstance(nodes, list):
            return {"resolutions": [], "swaps": []}
        if store is None:
            store = Store(store_root(environ), table)
        found = resolve(nodes, table, store, _classes() if classes is None else classes,
                        _free_memory() if free_bytes is None else free_bytes,
                        comfy_path, fetch if store.folders else None)
        return {"resolutions": found,
                "swaps": [{"from": a, "to": b} for (a, b) in table.swaps]}
    except Exception as error:
        LOG.warning("[openabstractions] model files stay as authored: %r", error)
        return {"resolutions": [], "swaps": []}


def same_origin(headers, scheme, host, local=None):
    """True when the request's Origin is this server's own origin; on a
    loopback address the Host must name loopback too (oa_model_downloads.same_origin)."""
    origin = headers.get("Origin")
    if not (isinstance(origin, str) and isinstance(host, str) and origin == "%s://%s" % (scheme, host)):
        return False
    return not _loopback(local) or _loopback_name(host)


def _loopback(address):
    try:
        return ipaddress.ip_address(str(address).split("%")[0]).is_loopback
    except ValueError:
        return False


def _loopback_name(host):
    try:
        name = urllib.parse.urlsplit("//" + host).hostname
    except ValueError:
        return False
    return name == "localhost" or _loopback(name)


_registered = []


def install(routes=None, add=None, environ=None):
    """Guard ComfyUI's model listing, register the store's folders and serve
    ROUTE. Returns True when the route is served. When the listing cannot be
    guarded, nothing is registered or served: hidden beats wrong."""
    folder_paths = sys.modules.get("folder_paths")
    listing = getattr(getattr(folder_paths, "get_full_path", None), GUARDED, None)
    if listing is None:
        table = load_table()
        if table is None:
            return False
        listing = Listing(Store(store_root(environ), table), table, _class_mappings)
    store, table = listing.store, listing.table
    if not guard_listing(listing, folder_paths, sys.modules.get("app.model_manager")):
        LOG.warning("[openabstractions] this ComfyUI lists model files in a way OA does not know; "
                    "OA's model folders stay out of it")
        return False
    for category in _register_store(store, table, add):
        folder_paths.filename_list_cache.pop(category, None)
    try:
        if routes is None:
            server = sys.modules.get("server")
            instance = getattr(getattr(server, "PromptServer", None), "instance", None)
            if instance is None:
                LOG.info("[openabstractions] no running ComfyUI server; %s is not served", ROUTE)
                return False
            routes = instance.routes
        if any(table_ is routes for table_ in _registered):
            return True
        from aiohttp import web

        @routes.post(ROUTE)
        async def resolve_route(request):
            if not same_origin(request.headers, request.scheme, request.host, request.remote):
                return web.json_response({"resolutions": [], "swaps": []}, status=403)
            try:
                raw = await request.content.read(BODY_LIMIT + 1)
                body = json.loads(raw) if len(raw) <= BODY_LIMIT else None
            except ValueError:
                body = None
            import asyncio
            result = await asyncio.get_running_loop().run_in_executor(
                None, lambda: answer(body, table=table, store=store, comfy_path=_comfy_path, fetch=hf_fetch))
            return web.json_response(result)

        _registered.append(routes)
        return True
    except Exception as error:  # the extension never stops ComfyUI from starting
        LOG.warning("[openabstractions] %s is not served: %r", ROUTE, error)
        return False
