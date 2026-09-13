"""This ComfyUI's OA operations as rows a person can read.

The job service names operations by request identity and retains display labels.
The caller records this adapter retains beside a model carry the rest:
which model an operation fetches, where it lands, which repository a file
belongs to, and which provider supplied bytes without a source request. This module
joins the two and serves the result under the inventory route,
`GET /abstraction/jobs`, which `oa_inventory.py` registers.
Unmatched service snapshots remain operation rows after an app restart, with
their service progress and label and no inferred model, file or video profile.

One row is one operation. A repository snapshot is one parent row holding its
per-file children, because the service has no snapshot unit: the caller record
is what groups those files. A row says whether the bytes were reused from a
provider or fetched by a download job, and `provider` names the provider the
runtime resolved for the read (abstraction-facade CONTRACT.md FAC-B4). Reuse
means one authorized read of content the runtime already held; it is not a
claim about disk deduplication.

`POST /abstraction/jobs/<id>/retry` re-drives one retained delivery on a
background thread. A terminal failed delivery may advance its durable attempt
under the same key, epoch and request. `POST /abstraction/jobs/<id>/cancel`
asks the service to cancel the active attempt through `service.cancel_retained`.
An unmatched operation is resolved again from the current caller's inventory
before the generated job client sends CancelWork; it has no retry action.
Neither route changes the retained key or destination.
"""
import hashlib
import json
import logging
import os
import sys
import threading
import time

from . import service

LOG = logging.getLogger("abstraction")

# The route oa_inventory registers; this module adds the two actions under it.
ROUTE = "/abstraction/jobs"

REQUEST_SUFFIX = ".oa-request.json"
SNAPSHOT_SUFFIX = ".oa-snapshot.json"
REUSE_SUFFIX = service.REUSE_SUFFIX

# The record scan is bounded: a ComfyUI models directory is the caller's, and a
# route may not walk it without an end.
SCAN_BUDGET_SECONDS = 2.0
MAX_RECORDS = 512
MAX_RECORD_BYTES = 1 << 24
MAX_CANCELLED_FILES = 256
MAX_RUNNING_RETRIES = 4
MAX_SINCE = 160
MAX_EXACT_OBSERVATIONS = 64
EXACT_OBSERVE_BUDGET_SECONDS = 3.0

# What one repository row says when its files disagree. The first state any file
# holds is the repository's.
PRECEDENCE = ("failed", "running", "pending", "transferred", "cancelled", "unknown", "complete")

TERMINAL = frozenset(("complete", "failed", "cancelled"))

_roots = None
_lock = threading.Lock()
# Retries this process started: id -> dict(state, detail). One entry per row.
_retries = {}


def configure(roots=None):
    """Supply the model roots to scan for caller records.

    roots is a sequence of directories. No argument restores the roots the
    package discovers from the running ComfyUI.
    """
    global _roots
    _roots = None if roots is None else [os.path.abspath(root) for root in roots]


def model_roots():
    """The directories scanned for retained caller records."""
    if _roots is not None:
        return list(_roots)
    package = sys.modules.get(__package__)
    discover = getattr(package, "_model_search_roots", None)
    return list(discover()) if callable(discover) else []


def _read(path):
    try:
        with open(path, encoding="utf-8") as source:
            value = json.loads(source.read(MAX_RECORD_BYTES))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _exists(path):
    try:
        return os.path.exists(path)
    except OSError:
        return False


def read_records(budget=SCAN_BUDGET_SECONDS, limit=MAX_RECORDS):
    """Retained caller records under the model roots, grouped by destination.

    The key of each group is the path the records name: a model file for a
    `.oa-request.json` and its `.oa-reuse.json`, a repository directory for a
    `.oa-snapshot.json` and its `.oa-reuse.json`. Values are
    dict(request=, snapshot=, reuse=) with the records that exist. An
    unreadable or malformed record is left out. The walk stops at the budget or
    the record limit, whichever comes first.
    """
    deadline = time.monotonic() + budget
    kinds = ((REQUEST_SUFFIX, "request"), (SNAPSHOT_SUFFIX, "snapshot"), (REUSE_SUFFIX, "reuse"))
    groups, seen = {}, set()
    for root in model_roots():
        try:
            for base, directories, files in os.walk(root):
                directories.sort()
                for name in sorted(files):
                    for suffix, kind in kinds:
                        if not name.endswith(suffix):
                            continue
                        path = os.path.join(base, name)
                        key = os.path.normcase(path)
                        if key in seen:
                            break
                        seen.add(key)
                        group = path[:-len(suffix)]
                        if group not in groups and len(groups) >= limit:
                            return groups
                        value = _read(path)
                        if value is not None:
                            groups.setdefault(group, {})[kind] = value
                        break
                if time.monotonic() >= deadline:
                    return groups
        except OSError:
            continue
    return groups


def _reuse_entries(record):
    """The delivered-from-storage entries of one reuse record."""
    if not isinstance(record, dict):
        return []
    files = record.get("files")
    if not isinstance(files, list):
        return []
    return [entry for entry in files if isinstance(entry, dict) and isinstance(entry.get("final"), str)]


def _is_repository(group):
    return isinstance(group.get("snapshot"), dict) or any(
        entry.get("repo") for entry in _reuse_entries(group.get("reuse")))


def identifier(prefix, text):
    """The stable row id of one destination: the same before and after a restart."""
    return prefix + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _identity_key(identity):
    if not isinstance(identity, dict):
        return None
    return (identity.get("key"), identity.get("history_epoch"), int(identity.get("attempt") or 0))


def _identity(key, epoch, entry):
    if isinstance(key, str) and key and isinstance(epoch, str) and epoch:
        try:
            return dict(key=key, history_epoch=epoch, attempt=service.attempt(entry))
        except ValueError:
            return None
    return None


def _percent(progress):
    total = progress["total"]
    return round(progress["done"] * 100 / total) if total > 0 else None


def retry_state(row_id):
    """What a retry this process started did, or None."""
    with _lock:
        state = _retries.get(row_id)
        return dict(state) if state is not None else None


def _row(row_id, model, destination, identity, reused, observed, url):
    """One operation, joined from its caller record and its service snapshot."""
    snapshot = observed.get(_identity_key(identity)) if identity is not None else None
    if reused is not None and identity is not None and identity.get("attempt", 0) > 0:
        reused = None  # Historical reuse is not the result of the active retry.
    present = _exists(destination)
    state, reason, failure = "unknown", "", None
    progress = dict(done=0, total=0)
    operation_id, label, waiting, cancelling = "", "", "", False
    if snapshot is not None:
        state = snapshot.get("state") or "unknown"
        reported = snapshot.get("progress") or {}
        progress = dict(done=int(reported.get("done") or 0), total=int(reported.get("total") or 0))
        failure = snapshot.get("failure")
        operation_id = (snapshot.get("receipt") or {}).get("operation_id", "")
        label = snapshot.get("label", "")
        waiting = snapshot.get("waiting", "")
        cancelling = bool(snapshot.get("cancellation_requested"))
    elif identity is not None:
        reason = "the service did not list this operation; its history may have expired"
    if reused is not None and present:
        # The bytes are here and a store supplied them; an earlier job's state
        # does not describe this file any more.
        state, reason = "complete", ""
    if state == "failed":
        reason = (failure or {}).get("message") or (failure or {}).get("cause") or "the download failed"
    elif state == "cancelled":
        reason = reason or "the operation was cancelled"
    elif waiting:
        reason = "waiting for " + waiting
    return dict(id=row_id, kind="file", model=model, destination=destination, url=url,
                state=state, progress=progress, percent=_percent(progress),
                source="reused" if reused is not None else "fetched",
                # provider names who supplied the bytes (abstraction-facade
                # CONTRACT.md FAC-B4). store holds the same word for readers
                # written before the reuse record named a provider.
                provider=(reused or {}).get("store", ""),
                store=(reused or {}).get("store", ""), present=present,
                failure=failure, reason=reason, identity=identity,
                operation_id=operation_id, label=label, waiting=waiting,
                cancellation_requested=cancelling, retry=retry_state(row_id))


def _file_row(base, group, observed):
    request = group.get("request")
    entries = _reuse_entries(group.get("reuse"))
    reused = entries[0] if entries else None
    destination, url, identity = base, "", None
    if isinstance(request, dict):
        final = request.get("final")
        destination = final if isinstance(final, str) and final else base
        url = service.described(request.get("spec"))[0] or ""
        identity = _identity(request.get("key"), request.get("epoch"), request)
        if request.get("cancelled_predecessor"):
            reused = None
    if reused is not None and (identity is None or identity["attempt"] == 0):
        destination = reused.get("final") or destination
        url = url or reused.get("url") or ""
    return _row(identifier("f", base), os.path.basename(destination), destination, identity, reused, observed, url)


def repository_paths(group):
    """Every repository file one group names, from its snapshot and reuse records."""
    record = group.get("snapshot")
    files = record.get("files") if isinstance(record, dict) else None
    paths = set()
    if isinstance(files, dict):
        paths.update(path for path, entry in files.items() if isinstance(path, str) and isinstance(entry, dict))
    paths.update(entry["path"] for entry in _reuse_entries(group.get("reuse"))
                 if isinstance(entry.get("path"), str) and entry["path"])
    return sorted(paths)


def _repository_row(base, group, observed):
    record = group.get("snapshot") if isinstance(group.get("snapshot"), dict) else {}
    entries = {entry["path"]: entry for entry in _reuse_entries(group.get("reuse"))
               if isinstance(entry.get("path"), str) and entry["path"]}
    files = record.get("files") if isinstance(record.get("files"), dict) else {}
    repo = record.get("repo") or next((entry.get("repo") for entry in entries.values() if entry.get("repo")), "")
    revision = record.get("revision") or ""
    children = []
    for path in repository_paths(group):
        entry, reused = files.get(path), entries.get(path)
        destination = os.path.join(base, *path.split("/"))
        url, identity = "", None
        if isinstance(entry, dict):
            url = service.described(entry.get("spec"))[0] or ""
            identity = _identity(entry.get("key"), record.get("epoch"), entry)
        if reused is not None and (identity is None or identity["attempt"] == 0):
            destination = reused.get("final") or destination
            url = url or reused.get("url") or ""
        child = _row(identifier("c", base + "\0" + path), path, destination, identity, reused, observed, url)
        child["repository"] = repo
        children.append(child)
    done = sum(child["progress"]["done"] for child in children)
    total = sum(child["progress"]["total"] for child in children)
    states = {child["state"] for child in children}
    state = next((word for word in PRECEDENCE if word in states), "unknown")
    reused_files = [child for child in children if child["source"] == "reused"]
    providers = sorted({child["provider"] for child in reused_files if child["provider"]})
    stores = providers
    progress = dict(done=done, total=total)
    failed = [child for child in children if child["state"] == "failed"]
    reason = ""
    if failed:
        reason = "%d of %d files failed: %s" % (len(failed), len(children), failed[0]["reason"])
    elif reused_files and len(reused_files) < len(children):
        reason = "%d of %d files reused from a store" % (len(reused_files), len(children))
    return dict(id=identifier("r", base), kind="repository", model="%s@%s" % (repo, revision) if revision else repo,
                destination=base, url="", state=state, progress=progress, percent=_percent(progress),
                source=("reused" if len(reused_files) == len(children) and children else
                        "mixed" if reused_files else "fetched"),
                provider=providers[0] if len(providers) == 1 else "", providers=providers,
                store=stores[0] if len(stores) == 1 else "", stores=stores, present=_exists(base),
                failure=None, reason=reason, identity=None, operation_id="", label="", waiting="",
                cancellation_requested=any(child["cancellation_requested"] for child in children),
                retry=retry_state(identifier("r", base)),
                repository=dict(repo=repo, revision=revision),
                files=len(children), reused=len(reused_files), children=children)


def _observe_unlisted(groups, operations):
    """Use each retained identity to observe caller-owned work omitted by inventory.

    Some runtimes expose operation observation to the submitting caller without
    advertising the broader inventory contract. Observation is bounded and tied
    to the exact durable request records this adapter created.
    """
    observed = {}
    for snapshot in operations:
        if not isinstance(snapshot, dict):
            continue
        identity = (snapshot.get("receipt") or {}).get("identity")
        key = _identity_key(identity)
        if key is not None:
            observed[key] = snapshot

    records = []
    for base in sorted(groups):
        group = groups[base]
        request = group.get("request")
        if isinstance(request, dict):
            identity = _identity(request.get("key"), request.get("epoch"), request)
            if identity is not None and _identity_key(identity) not in observed:
                records.append((identity, request))
        snapshot = group.get("snapshot")
        if isinstance(snapshot, dict) and isinstance(snapshot.get("files"), dict):
            for entry in snapshot["files"].values():
                if isinstance(entry, dict):
                    identity = _identity(entry.get("key"), snapshot.get("epoch"), entry)
                    if identity is not None and identity["attempt"] and _identity_key(identity) not in observed:
                        records.append((identity, snapshot))
    if not records:
        return observed

    deadline = time.monotonic() + EXACT_OBSERVE_BUDGET_SECONDS
    try:
        required = service._required()
        bindings = {}
        from abstraction.job.acceptance import RequestIdentity
        from abstraction.job.acceptance import to_json
    except Exception:
        return observed

    for identity, request in records[:MAX_EXACT_OBSERVATIONS]:
        if time.monotonic() >= deadline:
            break
        endpoint, owner = request.get("endpoint"), request.get("owner")
        if not isinstance(endpoint, str) or not endpoint or not isinstance(owner, str) or not owner:
            continue
        binding_key = (endpoint, owner)
        try:
            binding = bindings.get(binding_key)
            if binding is None:
                binding = service._restore(request, required, deadline)
                bindings[binding_key] = binding
            result = binding.observe_work(RequestIdentity(
                key=identity["key"], history_epoch=identity["history_epoch"], attempt=identity["attempt"]))
            if result.outcome != "observed" or result.snapshot is None:
                continue
            observed[_identity_key(identity)] = json.loads(to_json(result.snapshot))
        except Exception:
            # Inventory remains useful when one exact observation is unavailable.
            continue
    return observed


def rows(groups, operations):
    """One row per operation: a model file, or a repository with its files."""
    observed = _observe_unlisted(groups, operations)
    out = []
    represented, active = set(), {}
    for base in sorted(groups):
        group = groups[base]
        repository = _is_repository(group)
        row = _repository_row(base, group, observed) if repository else _file_row(base, group, observed)
        out.append(row)
        record = group.get("snapshot" if repository else "request")
        owner = record.get("owner") if isinstance(record, dict) else None
        for item in [row] + row.get("children", []):
            key = _identity_key(item.get("identity"))
            represented.add(key)
            if key is not None and key[2] and isinstance(owner, str) and owner:
                active[(owner, *key[:2])] = key[2]
    for snapshot in operations:
        if len(out) >= MAX_RECORDS:
            break
        receipt = snapshot.get("receipt") or {}
        identity = receipt.get("identity")
        key = _identity_key(identity)
        if key is not None and key not in represented and key[2] >= active.get((receipt.get("logical_owner"), *key[:2]), 0):
            out.append(_operation_row(snapshot))
            represented.add(key)
    return out


def _operation_row(snapshot):
    """Project a service snapshot without inventing a file or a video profile."""
    receipt = snapshot["receipt"]
    identity = receipt["identity"]
    row_id = identifier("o", json.dumps([receipt["logical_owner"], identity], sort_keys=True, separators=(",", ":")))
    row = _row(row_id, "", "", identity,
               None, {_identity_key(identity): snapshot}, "")
    row.update(kind="operation", source="", logical_owner=receipt["logical_owner"])
    if row["state"] == "failed" and not (row["failure"] or {}).get("message") and not (row["failure"] or {}).get("cause"):
        row["reason"] = "the operation failed"
    return row


def _cancel_operation(row_id, selected=None):
    # Reopen the current caller's inventory; a stale UI cursor or a supplied
    # row id provides no authority and never supplies a request identity.
    from . import oa_inventory
    try:
        body = oa_inventory.list_operations()
        outcome = body.get("outcome")
        if outcome != "page":
            return dict(outcome=outcome or "unavailable")
        for snapshot in body.get("operations", [])[:MAX_RECORDS]:
            row = _operation_row(snapshot)
            if row["id"] == row_id:
                identity = row["identity"]
                current = _identity(identity.get("key"), identity.get("history_epoch"), identity)
                if selected is not None and selected != [current]:
                    return dict(outcome="stale")
                return dict(outcome=oa_inventory.cancel_work(row["identity"], row["logical_owner"]))
        return dict(outcome="unknown" if body.get("complete") else "unavailable")
    except Exception as error:
        LOG.warning("[abstraction] current operation cancellation unavailable: %s", type(error).__name__)
        return dict(outcome="unavailable")


def cursor_of(view):
    """The change cursor of one row list: equal cursors mean equal rows."""
    encoded = json.dumps(view, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "v1." + hashlib.sha256(encoded).hexdigest()


def enrich(body, since=""):
    """Add this ComfyUI's rows to one inventory listing.

    since is the cursor of a previous answer. An equal cursor answers
    changed=False with no rows and no snapshots, so a refreshing tab carries
    nothing it already has.
    """
    view = rows(read_records(), body.get("operations") or [])
    cursor = cursor_of(view)
    if isinstance(since, str) and since and len(since) <= MAX_SINCE and since == cursor:
        return dict(outcome=body.get("outcome"), complete=body.get("complete"),
                    pages=body.get("pages"), cursor=cursor, changed=False, rows=[])
    answer = dict(body)
    answer.update(cursor=cursor, changed=True, rows=view)
    return answer


def locate(row_id, groups=None):
    """(kind, base, group, path) of the row one id names, or (None, None, None, None).

    kind is "file", "repository" or "repository_file".
    """
    groups = read_records() if groups is None else groups
    for base, group in groups.items():
        if _is_repository(group):
            if row_id == identifier("r", base):
                return "repository", base, group, None
            for path in repository_paths(group):
                if row_id == identifier("c", base + "\0" + path):
                    return "repository_file", base, group, path
        elif row_id == identifier("f", base):
            return "file", base, group, None
    return None, None, None, None


def _cancel_file(record, entry):
    key = entry.get("key")
    epoch = record.get("epoch")
    if not (isinstance(key, str) and key and isinstance(epoch, str) and epoch):
        return "invalid"
    try:
        active = service.attempt(entry)
    except ValueError:
        return "invalid"
    return service.cancel_retained(record, key, epoch, attempt=active)


def _delivered_from_a_store(group, base, path):
    """Whether a store supplied this repository file and it is still in place.

    A snapshot record holds a key for every file of the repository, including
    the ones no job ever fetched. Those have nothing to cancel.
    """
    for entry in _reuse_entries(group.get("reuse")):
        if entry.get("path") == path:
            return _exists(entry.get("final") or os.path.join(base, *path.split("/")))
    return False


def _matches_selection(kind, group, path, selected):
    if selected is None:
        return True
    record = group.get("request" if kind == "file" else "snapshot")
    if not isinstance(record, dict):
        return False
    entries = [record]
    if kind != "file":
        files = record.get("files")
        if not isinstance(files, dict):
            return False
        entries = [files.get(path)] if path is not None else [files[name] for name in sorted(files)]
    current = [_identity(entry.get("key"), record.get("epoch"), entry) for entry in entries if isinstance(entry, dict)]
    return bool(current) and None not in current and selected == current


def cancel(row_id, selected=None):
    """Ask the service to cancel the operations one row names.

    Cancellation is intent, not stopped effects: the contract lets completion
    win the race, and the next listing shows the operation's own terminal state.
    """
    if row_id.startswith("o"):
        return _cancel_operation(row_id, selected)
    kind, base, group, path = locate(row_id)
    if kind is None:
        return dict(outcome="unknown", detail="no retained caller record names this row")
    if not _matches_selection(kind, group, path, selected):
        return dict(outcome="stale")
    if kind == "file":
        record = group.get("request")
        if not isinstance(record, dict):
            return dict(outcome="unsupported", detail="this file was delivered from a store; it has no operation")
        return dict(outcome=_cancel_file(record, record))
    record = group.get("snapshot")
    if not isinstance(record, dict) or not isinstance(record.get("files"), dict):
        return dict(outcome="unsupported", detail="this repository's files were delivered from a store")
    files = record["files"]
    if kind == "repository_file":
        entry = files.get(path)
        if not isinstance(entry, dict) or ("attempt" not in entry and _delivered_from_a_store(group, base, path)):
            return dict(outcome="unsupported", detail="this repository file was delivered from a store")
        return dict(outcome=_cancel_file(record, entry))
    outcomes = {}
    for name in sorted(files)[:MAX_CANCELLED_FILES]:
        entry = files[name]
        if isinstance(entry, dict) and "attempt" not in entry and _delivered_from_a_store(group, base, name):
            continue
        outcomes[name] = _cancel_file(record, entry) if isinstance(entry, dict) else "invalid"
    requested = [word for word in outcomes.values() if word == "requested"]
    return dict(outcome="requested" if requested else (sorted(set(outcomes.values()))[:1] or ["unknown"])[0],
                files=outcomes)


def _no_listing():
    raise AssertionError("a retained snapshot record needs no repository listing")


def _deliver_file(record):
    final = record.get("final")
    url = service.described(record.get("spec"))[0]
    if not isinstance(final, str) or not final or not url:
        raise ValueError("the retained caller record names no file or no request")
    service.deliver(url, os.path.dirname(final), os.path.basename(final), _retry=record)


def _run(row_id, work):
    try:
        work()
    except Exception as error:
        with _lock:
            _retries[row_id] = dict(state="failed", detail=repr(error))
        LOG.warning("[abstraction] the OA retry of %s did not finish: %r", row_id, error)
        return
    with _lock:
        _retries[row_id] = dict(state="finished", detail="")


def _start(row_id, work, scope):
    with _lock:
        running = [state for state in _retries.values() if state["state"] == "running"]
        if (_retries.get(row_id) or {}).get("state") == "running":
            return dict(outcome="running", scope=scope)
        if len(running) >= MAX_RUNNING_RETRIES:
            return dict(outcome="busy", detail="%d retries are already running" % len(running))
        _retries[row_id] = dict(state="running", detail="")
    threading.Thread(target=_run, args=(row_id, work), name="oa-retry", daemon=True).start()
    return dict(outcome="started", scope=scope)


def retry(row_id, selected=None):
    """Re-drive the delivery one row names, on a background thread.

    An explicit retry may advance only a selected failed native attempt.
    Other retained work is reconciled without advancing its attempt.
    """
    kind, base, group, path = locate(row_id)
    if kind is None:
        return dict(outcome="unknown", detail="no retained caller record names this row")
    if not _matches_selection(kind, group, path, selected):
        return dict(outcome="stale")
    if kind == "file":
        record = group.get("request")
        if not isinstance(record, dict):
            return dict(outcome="unsupported", detail="this file was delivered from a store; there is nothing to retry")
        return _start(row_id, lambda: _deliver_file(record), "file")
    record = group.get("snapshot")
    if not isinstance(record, dict) or not record.get("repo"):
        return dict(outcome="unsupported", detail="this repository's files were delivered from a store")
    repo, local_dir = record["repo"], base
    return _start(row_id, lambda: service.deliver_snapshot(repo, local_dir, _no_listing, _retry=(record, path)), "repository")


def register(routes):
    """Add the retry and cancel routes beside the inventory's GET."""
    from aiohttp import web

    async def _act(request, action):
        import asyncio
        row_id = request.match_info.get("id", "")
        if not row_id or len(row_id) > 64 or not row_id.isalnum():
            return web.json_response(dict(outcome="invalid", detail="the row id is not one this view issues"),
                                     status=400)
        try:
            raw = await request.read()
            body = json.loads(raw) if len(raw) <= 64 * 1024 else None
        except (ValueError, UnicodeError):
            body = None
        selected = body.get("identities") if isinstance(body, dict) and set(body) == {"identities"} else None
        if (not isinstance(selected, list) or not 1 <= len(selected) <= MAX_CANCELLED_FILES
                or any(not isinstance(item, dict) or set(item) != {"key", "history_epoch", "attempt"}
                       or not _identity(item["key"], item["history_epoch"], item) for item in selected)):
            return web.json_response(dict(outcome="invalid", detail="select an exact active attempt"), status=400)
        result = await asyncio.get_running_loop().run_in_executor(None, action, row_id, selected)
        return web.json_response(result, status=404 if result.get("outcome") == "unknown" else 200)

    @routes.post(ROUTE + "/{id}/retry")
    async def retry_route(request):
        return await _act(request, retry)

    @routes.post(ROUTE + "/{id}/cancel")
    async def cancel_route(request):
        return await _act(request, cancel)
