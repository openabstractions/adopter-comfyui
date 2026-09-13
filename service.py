"""Application delivery and caller recovery metadata over the public job API.

Before a file is admitted as a job, OA content storage is asked for its bytes
(storage_reuse.py). A file storage supplies arrives with no source request and
no job; every other answer is logged with its reason and the job download runs.
"""
import json
import hashlib
import logging
import math
import os
import stat
import sys
import tempfile
import threading
import time
import uuid
from collections import namedtuple
from contextlib import ExitStack, contextmanager
from urllib.parse import urlsplit

from . import oa_route
from . import storage_reuse as reuse
from ._model_delivery import PATH_FILES

LOG = logging.getLogger("abstraction")

# Serializes this ComfyUI process's model deliveries. The service remains the
# authority for duplicate request identities across processes and retries.
_delivery_lock = threading.Lock()
_configuration = None

# The caller-record budget for one encoded download request.
_SPEC_BUDGET = 16384

# The record naming files OA content storage supplied: one beside a model, one
# beside a repository directory holding an entry per repository file. A file a
# store supplies has no job and no request identity, so this is the only record
# of it. See "Storage reuse" in SERVICE.md.
REUSE_SUFFIX = ".oa-reuse.json"
_REUSE_LIMIT = 1024
_REUSE_BUDGET = 1 << 22

# One retained operation's cancellation: one restore and one call.
CONTROL_BUDGET_SECONDS = 10.0


def _hash_file(path, *, _files=PATH_FILES):
    digest = hashlib.sha256()
    with _files.open(path, "rb") as source:
        for block in iter(lambda: source.read(8 << 20), b""):
            digest.update(block)
    return digest.hexdigest()

# Preflight status codes. PREFLIGHT_READY is the only code with ready=True.
PREFLIGHT_READY = "ready"
PREFLIGHT_SDK_UNAVAILABLE = "sdk_unavailable"
PREFLIGHT_NATIVE_UNAVAILABLE = "native_unavailable"
PREFLIGHT_RUNTIME_UNAVAILABLE = "runtime_unavailable"
PREFLIGHT_ACCEPTANCE_UNAVAILABLE = "acceptance_unavailable"

# The preflight's own short total budget, independent of ABSTRACTION_COMFYUI_WAIT_SECONDS.
PREFLIGHT_BUDGET_SECONDS = 5.0

PreflightResult = namedtuple("PreflightResult", "ready code detail")
PreflightResult.__doc__ = """The typed result of one activation preflight check.

ready: True only when code is PREFLIGHT_READY. code: PREFLIGHT_READY or the
first failing item, checked in order: PREFLIGHT_SDK_UNAVAILABLE (the facade,
identity or job acceptance Python package does not import),
PREFLIGHT_NATIVE_UNAVAILABLE (the native IPC library did not resolve),
PREFLIGHT_RUNTIME_UNAVAILABLE (no installed OA runtime was found) or
PREFLIGHT_ACCEPTANCE_UNAVAILABLE (abstraction.job/acceptance@1 resolved to a
refusal other than a missing runtime). detail: a one-sentence explanation.
"""


def preflight(budget=PREFLIGHT_BUDGET_SECONDS):
    """Check, with a short total budget, that service mode can accept downloads
    before any Manager call site is replaced: the facade and identity SDK
    import, the native IPC library resolves, the installed runtime selects (the
    SDK default Machine() path unless configure() supplied independent trust,
    with no activation) and abstraction.job/acceptance@1 resolves.

    Returns the first failing PreflightResult, or one with ready=True.
    """
    try:
        required = _required()
    except ImportError as error:
        return PreflightResult(False, PREFLIGHT_SDK_UNAVAILABLE,
                               "install the OA facade, identity and job acceptance Python packages: %r" % (error,))
    deadline = time.monotonic() + budget
    try:
        _resolve(deadline, required)
    except ImportError as error:
        return PreflightResult(False, PREFLIGHT_SDK_UNAVAILABLE,
                               "install the OA facade Python package: %r" % (error,))
    except Exception as error:
        status = getattr(error, "status", None)
        if status == "runtime_unavailable":
            return PreflightResult(False, PREFLIGHT_RUNTIME_UNAVAILABLE,
                                   "no installed OA runtime was found: %r" % (error,))
        if status is not None:
            return PreflightResult(False, PREFLIGHT_ACCEPTANCE_UNAVAILABLE,
                                   "abstraction.job/acceptance@1 did not resolve: %r" % (error,))
        return PreflightResult(False, PREFLIGHT_NATIVE_UNAVAILABLE,
                               "the native IPC library did not resolve: %r" % (error,))
    return PreflightResult(True, PREFLIGHT_READY,
                           "the facade, native IPC library, installed runtime and job acceptance resolved")


class ServiceUnavailable(RuntimeError):
    """OA could not be reached before any work was recorded for a file.

    deliver raises it only before the caller record exists, so the caller may
    download the file another way: OA holds no request for it.
    """

    def __init__(self, cause):
        super().__init__("OA is not reachable: %r" % (cause,))
        self.cause = cause


def unreachable(error):
    """True when error says the runtime could not be selected or reached:
    a missing SDK or native library, no runtime, or a transport failure. A
    refusal word from a runtime that answered is not unreachability."""
    if isinstance(error, (ImportError, OSError, TimeoutError)):
        return True
    status = getattr(error, "status", None)
    return status is None or status == "runtime_unavailable"


def rights_program():
    """The configured relay's caller-program suggestion, or None.

    When the "OpenAbstractions for ComfyUI" extension's helper makes this
    ComfyUI's calls (oa_route.py), name that helper. Permission guidance
    requires its configured program. The runtime's authenticated native
    caller and audit remain authoritative.
    """
    if oa_route.RELAY in sys.modules:
        try:
            helper = oa_route.program()
        except Exception:
            return None
        return helper if isinstance(helper, str) and helper else None
    return None


def grant_command(registries=("hf",)):
    """The suggested download grant command, or None if the caller is unknown.

    `rights grant --for downloads` writes the exact rules: job submission, and
    model lookup on each registry (research/rights-defaults/DECISION.md §2).
    Nothing here grants anything; the person does.
    """
    program = rights_program()
    if program is None:
        return None
    return 'openabstractions rights grant --for downloads --program "%s" --registry %s' % (
        program, ",".join(registries))


def refusal_detail(outcome):
    """The sentence a forbidden service answer adds: the grant command."""
    if outcome != "forbidden":
        return ""
    command = grant_command()
    if command is None:
        return "; the OA caller program is unavailable; no download permission command can be suggested"
    return ("; this program holds no rights rule for downloads. A person allows it with: %s "
            "(or in the Abstraction Panel under Rights, Allow downloads for...)" % command)


def configure(machine_factory=None, restore_factory=None):
    """Supply independent host trust for an explicitly configured embedding.

    Factories receive the operation's absolute deadline. Restore must preserve
    the supplied endpoint and logical owner. Calling with no arguments restores
    normal installed-runtime selection. Configure before starting downloads.
    """
    global _configuration
    if machine_factory is None and restore_factory is None:
        _configuration = None
    elif callable(machine_factory) and callable(restore_factory):
        _configuration = (machine_factory, restore_factory)
    else:
        raise ValueError("supply both trusted machine and restoration factories")


def deliver(url, directory, filename, *, size=0, digest="", _retry=None, _cancelled=None, _observe=None,
            _destination=None):
    """Deliver a model through the durable service path.

    Optional size and sha256:<hex> pin the request before acceptance and are
    enforced again while copying the completed result. Manager's existing
    callers omit them and keep the historical behavior.
    """
    if type(size) is not int or size < 0:
        raise ValueError("pinned model size must be a nonnegative integer")
    if digest and not (isinstance(digest, str) and digest.startswith("sha256:")
                       and len(digest) == 71 and all(c in "0123456789abcdef" for c in digest[7:].lower())):
        raise ValueError("pinned model digest must be sha256:<64 hex>")
    pinned_size, pinned_digest = size, digest.lower()
    required = _required()
    timeout, deadline = _deadline()
    if not _delivery_lock.acquire(timeout=timeout):
        raise TimeoutError("waiting for another model delivery; no new work submitted")
    try:
        with ExitStack() as custody:
            files = PATH_FILES if _destination is None else custody.enter_context(_destination.hold(create=True))
            final = os.path.abspath(os.path.join(directory, filename))
            if files.present(final):
                if _retry is not None:
                    raise ValueError("the selected retry destination is already present")
                if size or digest:
                    entry = files.lstat(final)
                    if (stat.S_ISLNK(entry.st_mode) or getattr(entry, "st_file_attributes", 0) & 0x400
                            or not stat.S_ISREG(entry.st_mode)):
                        raise ValueError("existing pinned model path is not a regular file")
                    if size and entry.st_size != size:
                        raise ValueError("existing model differs from pinned size")
                    if pinned_digest and "sha256:" + _hash_file(final, _files=files) != pinned_digest:
                        raise ValueError("existing model differs from pinned SHA256")
                return
            _spec(url, size, digest)
            record_path = final + ".oa-request.json"
            if _destination is None:
                os.makedirs(os.path.dirname(final), exist_ok=True)
                fresh = not os.path.exists(record_path)
            else:
                fresh = not files.present(record_path)
            if fresh and (_retry is not None or _cancelled is not None):
                raise ValueError("the retained delivery selected for retry is missing")
            reuse_record = (reuse_record_path(final), "", "")
            if fresh:
                supplied, digest, size = _from_storage(deadline, url, final, digest, size, reuse_record, _files=files)
                if supplied:
                    return
                spec = _spec(url, size, digest)
                try:
                    binding = _resolve(deadline, required)
                    window = binding.get_history_window()
                except Exception as error:
                    # No record is written yet, so OA holds no work for this file.
                    if unreachable(error):
                        raise ServiceUnavailable(error) from error
                    raise
                record = dict(version=1, endpoint=binding.endpoint, owner=binding.owner,
                              epoch=window.history_epoch, key=uuid.uuid4().hex,
                              required=list(required), spec=spec.decode("utf-8"), final=final)
                # Caller metadata is durable before the first possible acceptance.
                if files.verify_locator is not None:
                    files.verify_locator()
                _write_new(record_path, record, _files=files)
            else:
                with files.open(record_path, "r", encoding="utf-8") as source:
                    record = json.loads(source.read(65537))
                retained_url, retained_digest, retained_size = _described(record.get("spec"))
                if (record.get("version") != 1 or record.get("final") != final
                        or retained_url != url or record.get("required") != list(required)
                        or (pinned_size and retained_size != pinned_size)
                        or (pinned_digest and retained_digest.lower() != pinned_digest)
                        or any(not isinstance(record.get(k), str) or not record[k]
                               for k in ("endpoint", "owner", "epoch", "key"))):
                    raise ValueError("retained request differs or is invalid; resolve it before changing this model")
                spec = record["spec"].encode("utf-8")
                digest, size = retained_digest, retained_size
                if (_retry is None and _cancelled is None and not record.get("cancelled_predecessor") and attempt(record) == 0
                        and _from_storage(deadline, url, final, digest, size, reuse_record, _files=files)[0]):
                    return
                binding = _restore(record, required, deadline)
                if _cancelled is not None:
                    record = _restart_cancelled_delivery(record_path, record, binding, _cancelled)
            active = (_retry_attempt(record_path, record, _retry, binding) if _retry is not None else attempt(record))
            if files.verify_locator is not None:
                files.verify_locator()
            identity = _admit(binding, record["key"], record["epoch"], spec, required, fresh, attempt=active)
            _await(binding, identity, deadline, _observe)
            _copy(binding, identity, final, expected_size=pinned_size, expected_digest=pinned_digest, _files=files)
            # Keep recovery metadata: deletion/re-delivery reuses the accepted work.
    finally:
        _delivery_lock.release()


def snapshot_record_path(local_dir):
    """The caller record of a repository snapshot: a file beside the directory,
    so the directory holds only the repository's files."""
    return os.path.abspath(local_dir).rstrip("\\/") + ".oa-snapshot.json"


def deliver_snapshot(repo, local_dir, list_files, *, _retry=None):
    """Deliver every file of one repository revision into local_dir.

    Each file not already present is first requested from OA content storage.
    The rest are independent download jobs, each with its own request identity.
    One caller record, snapshot_record_path(local_dir), holds the binding, the
    revision, and each file's identity key and exact request. It is durable
    before the first submission. Recovery reads the files from it and never
    lists the repository again. An explicit retry may advance a failed attempt.

    list_files is called only when no record exists. It returns
    dict(revision=..., files=[dict(path=..., url=..., size=..., digest=...)]),
    with repository paths separated by "/". A file already present is kept, as
    Manager keeps an existing model file. Returns the repository paths this call
    delivered. Any refusal or failed job raises; delivered files stay.
    """
    required = _required()
    timeout, deadline = _deadline()
    if not _delivery_lock.acquire(timeout=timeout):
        raise TimeoutError("waiting for another model delivery; no new work submitted")
    try:
        local_dir = os.path.abspath(local_dir)
        record_path = snapshot_record_path(local_dir)
        reuse_path = reuse_record_path(local_dir)
        fresh = not os.path.exists(record_path)
        if fresh and _retry is not None:
            raise ValueError("the retained snapshot selected for retry is missing")
        delivered, pending = [], []
        if fresh:
            listing = list_files()
            entries = {}
            for entry in listing["files"]:
                path = entry["path"]
                _snapshot_target(local_dir, path)
                if path in entries:
                    raise ValueError("the snapshot of %s lists %s twice" % (repo, path))
                _spec(entry["url"])
                entries[path] = entry
            if not entries:
                raise ValueError("the snapshot of %s lists no files" % repo)
            files = {}
            for path, entry in entries.items():
                final = _snapshot_target(local_dir, path)
                digest, size = entry.get("digest") or "", entry.get("size") or 0
                if not os.path.exists(final):
                    os.makedirs(os.path.dirname(final), exist_ok=True)
                    supplied, digest, size = _from_storage(deadline, entry["url"], final, digest, size,
                                                           (reuse_path, repo, path))
                    (delivered if supplied else pending).append(path)
                spec = _spec(entry["url"], size, digest)
                files[path] = dict(key=uuid.uuid4().hex, spec=spec.decode("utf-8"))
            if not pending:
                return delivered
            binding = _resolve(deadline, required)
            window = binding.get_history_window()
            record = dict(version=1, repo=repo, revision=listing["revision"], endpoint=binding.endpoint,
                          owner=binding.owner, epoch=window.history_epoch, required=list(required), files=files)
            os.makedirs(os.path.dirname(record_path), exist_ok=True)
            _write_new(record_path, record)
        else:
            with open(record_path, encoding="utf-8") as source:
                record = json.loads(source.read(1 << 24))
            files = record.get("files")
            if (record.get("version") != 1 or record.get("repo") != repo
                    or record.get("required") != list(required)
                    or any(not isinstance(record.get(k), str) or not record[k]
                           for k in ("endpoint", "owner", "epoch", "revision"))
                    or not isinstance(files, dict) or not files
                    or any(not isinstance(f, dict) or not isinstance(f.get("key"), str) or not f["key"]
                           or _described(f.get("spec"))[0] is None for f in files.values())):
                raise ValueError("retained snapshot record %s differs or is invalid; resolve it before "
                                 "changing this repository" % record_path)
            if _retry is not None and _retry[1] is not None and _retry[1] not in files:
                raise ValueError("the retained snapshot file selected for retry is missing")
            for path in files:
                _snapshot_target(local_dir, path)
            for path, entry in files.items():
                final = _snapshot_target(local_dir, path)
                if _retry is not None and _retry[1] is not None and path != _retry[1]:
                    continue
                if _retry is not None and _retry[1] == path and os.path.lexists(final):
                    raise ValueError("the selected retry destination is already present")
                if os.path.exists(final):
                    continue
                os.makedirs(os.path.dirname(final), exist_ok=True)
                url, digest, size = _described(entry["spec"])
                supplied = (_retry is None and attempt(entry) == 0 and
                            _from_storage(deadline, url, final, digest, size, (reuse_path, repo, path))[0])
                (delivered if supplied else pending).append(path)
            if not pending:
                return delivered
            binding = _restore(record, required, deadline)
        admitted = []
        for path in pending:
            entry = files[path]
            active = attempt(entry)
            if _retry is not None:
                previous, selected = _retry
                if selected is None or selected == path:
                    active = _retry_attempt(record_path, record, previous, binding, member=path)
            identity = _admit(binding, entry["key"], record["epoch"], entry["spec"].encode("utf-8"), required, fresh,
                              snapshot_label(record["repo"], record["revision"], path), attempt=active)
            admitted.append((path, _snapshot_target(local_dir, path), identity))
        for path, final, identity in admitted:
            _await(binding, identity, deadline)
            _copy(binding, identity, final)
            delivered.append(path)
        return delivered
    finally:
        _delivery_lock.release()


def reuse_record_path(destination):
    """The reuse record of one model file, or of one repository directory."""
    return os.path.abspath(destination).rstrip("\\/") + REUSE_SUFFIX


def read_reuse(path, *, _files=PATH_FILES):
    """The entries of one reuse record: dict(version=1, files=[...]).

    A missing, unreadable or malformed record reads as no entries, because a
    delivered model is the fact and this record only describes how it arrived.
    """
    try:
        with _files.open(path, "r", encoding="utf-8") as source:
            value = json.loads(source.read(_REUSE_BUDGET))
    except (OSError, ValueError):
        return dict(version=1, files=[])
    files = value.get("files") if isinstance(value, dict) else None
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(files, list):
        return dict(version=1, files=[])
    return dict(version=1, files=[e for e in files if isinstance(e, dict) and isinstance(e.get("final"), str)])


def _record_reuse(path, entry, *, _files=PATH_FILES):
    """Add one delivered-from-storage entry to the reuse record at path.

    The model is already in place; a record that cannot be written costs the
    jobs view one row's provenance and never the file.
    """
    try:
        entries = [e for e in read_reuse(path, _files=_files)["files"] if e.get("final") != entry["final"]]
        entries = entries[-(_REUSE_LIMIT - 1):] + [entry]
        _replace_json_record(path, dict(version=1, files=entries), _files=_files)
    except (OSError, ValueError, TypeError) as error:
        LOG.warning("[abstraction] the OA reuse record %s was not written (%r); %s arrived from OA storage all the same",
                    path, error, os.path.basename(entry["final"]))


def _from_storage(deadline, url, final, digest="", size=0, record=None, *, _files=PATH_FILES):
    """Deliver final from OA content storage and return (True, digest, size), or
    return (False, digest, size) with any digest the model lookup found for the
    job request. A per-file reason is logged here; a capability that is not
    offered was already reported once for the process.

    record is (path, repo, repository_path) naming where to record a delivery
    storage supplied, or None to record nothing."""
    bound = []

    def machine():
        # One Machine for both lookups; a selection failure counts against the
        # capability that asked for it.
        if not bound:
            bound.append(_machine(deadline))
        return bound[0]
    try:
        if not digest:
            digest, size = reuse.lookup_digest(machine, url, deadline)
        store = reuse.deliver_from_storage(machine, digest, size, final, _files=_files)
    except reuse.StorageMiss as miss:
        if not miss.quiet:
            level = logging.INFO if miss.reason in (reuse.STORAGE_NOT_FOUND, reuse.DIGEST_UNKNOWN) else logging.WARNING
            LOG.log(level, "[abstraction] OA storage did not supply %s; downloading through the job service "
                    "(reason=%s): %s", os.path.basename(final), miss.reason, miss.detail)
        return False, digest, size
    if record is not None:
        path, repo, repository_path = record
        _record_reuse(path, dict(final=final, url=url, digest=digest, size=size, store=store,
                                 at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                 repo=repo, path=repository_path), _files=_files)
    LOG.info("[abstraction] %s delivered from OA storage (%s) held by %s; no source request and no job",
             os.path.basename(final), digest, store)
    return True, digest, size


def described(spec_text):
    """(locator, digest, size) of an encoded download request, or (None, "", 0)."""
    return _described(spec_text)


def cancel_retained(record, key, epoch, attempt=0, budget=CONTROL_BUDGET_SECONDS):
    """Ask the service to cancel one operation a caller record retains.

    The binding is restored from the record's endpoint, logical owner and
    required guarantees, exactly as a retry restores it. Returns the
    contract's cancellation outcome: requested, already_terminal, unknown,
    forbidden, unsupported or unavailable. Requested is intent, not stopped
    effects; the operation's own terminal state settles the race.
    """
    import abstraction.job.acceptance as job
    required = tuple(record.get("required") or _required())
    deadline = time.monotonic() + budget
    binding = _restore(record, required, deadline)
    identity = job.RequestIdentity(key=key, history_epoch=epoch, attempt=attempt)
    return binding.cancel_work(identity).outcome


def _described(spec_text):
    """(locator, digest, size) of an encoded download request, or (None, "", 0)."""
    try:
        value = json.loads(spec_text)
        artifact = value.get("artifact", {})
        return value["sources"][0]["locator"], artifact.get("digest", ""), artifact.get("size", 0)
    except (TypeError, ValueError, KeyError, IndexError, AttributeError):
        return None, "", 0


def _snapshot_target(local_dir, path):
    """The final path of one repository file, refused unless it lies inside local_dir."""
    parts = path.split("/") if isinstance(path, str) else []
    if (not parts or "\\" in path or any(part in ("", ".", "..") for part in parts)
            or ":" in path or os.path.isabs(path)):
        raise ValueError("snapshot file path %r is not a relative repository path" % (path,))
    return os.path.join(local_dir, *parts)


def _required():
    import abstraction.job.acceptance as job
    return tuple(job.ADMISSION_GUARANTEES)


def _deadline():
    timeout = float(os.environ.get("ABSTRACTION_COMFYUI_WAIT_SECONDS", "900"))
    if not math.isfinite(timeout) or not 0 < timeout <= 86400:
        raise ValueError("ABSTRACTION_COMFYUI_WAIT_SECONDS must be between 0 and 86400")
    return timeout, time.monotonic() + timeout


def _spec(url, size=0, digest=""):
    import abstraction.download.request as download
    scheme = urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError("the installed download profile requires an HTTP(S) model URL")
    known = {name: value for name, value in (("digest", digest), ("size", size)) if value}
    request = download.Request(artifact=download.Artifact(**known),
                               sources=[download.Source(scheme=scheme, locator=url)])
    spec = download.encode(request)
    if len(spec) > _SPEC_BUDGET:
        raise ValueError("model request exceeds the adapter recovery-record budget")
    return spec


def _machine(deadline):
    if _configuration is None:
        from abstraction.facade.client import Machine
        return Machine(deadline=deadline, library=oa_route.library())
    return _configuration[0](deadline=deadline)


def machine(deadline):
    """Return the service's configured, locally trusted runtime binding.

    Other adopter capabilities that need the same runtime use this factory
    rather than constructing a default Machine and losing embedding trust.
    The caller supplies its already-bounded absolute monotonic deadline.
    """
    return _machine(deadline)


def _resolve(deadline, required):
    from abstraction.facade import Scope
    return _machine(deadline).resolve_jobs(guarantees=required, scope=Scope.LOCAL)


def _restore(record, required, deadline):
    if _configuration is None:
        from abstraction.facade.jobs import Jobs
        restore = Jobs.restore_installed
    else:
        restore = _configuration[1]
    if _configuration is None:
        return restore(record["endpoint"], record["owner"], required_guarantees=required, deadline=deadline,
                       library=oa_route.library())
    return restore(record["endpoint"], record["owner"], required_guarantees=required, deadline=deadline)


def _write_new(path, record, *, _files=PATH_FILES):
    # Exclusive creation refuses races and incomplete predecessor writes.
    with _files.open(path, "x", encoding="utf-8") as output:
        json.dump(record, output, ensure_ascii=False)
        output.flush()
        os.fsync(output.fileno())


def attempt(entry):
    """The active durable attempt. Original caller records imply zero."""
    value = entry.get("attempt", 0)
    if type(value) is not int or not 0 <= value < 2**63:
        raise ValueError("invalid retained download attempt")
    return value


@contextmanager
def _attempt_record_lock(path):
    # Keep one stable lock name beside the atomically replaced caller record.
    with open(path + ".lock", "a+b") as lock:
        if os.name == "nt":
            import msvcrt
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield  # Closing the descriptor releases the OS lock, including on exit.


def _replace_json_record(path, record, *, _files=PATH_FILES):
    if _files is not PATH_FILES:
        with _files.staged(path, ".oa-record-", mode="w", encoding="utf-8") as output:
            _write_json_record(output, record)
        return

    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=os.path.dirname(os.path.abspath(path)), prefix=".oa-record-", delete=False,
                                         mode="w", encoding="utf-8") as output:
            temporary = output.name
            _write_json_record(output, record)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)


def _write_json_record(output, record):
    json.dump(record, output, ensure_ascii=False)
    output.flush()
    os.fsync(output.fileno())

def check_cancelled_preparation(asset, final, selected):
    """Check the selected cancelled request before starting a recipe worker."""
    path = os.path.abspath(final) + ".oa-request.json"
    with open(path, encoding="utf-8") as source:
        record = json.loads(source.read(65537))
    required = _required()
    url, digest, size = _described(record.get("spec"))
    if (record.get("version") != 1 or record.get("final") != os.path.abspath(final)
            or record.get("required") != list(required) or url != asset["url"]
            or size != asset["size_bytes"] or digest != "sha256:" + asset["sha256"]):
        raise ValueError("the selected cancelled delivery differs from this recipe asset")
    binding = _restore(record, required, time.monotonic() + CONTROL_BUDGET_SECONDS)
    _restart_cancelled_delivery(path, record, binding, selected, publish=False)


def _restart_cancelled_delivery(path, retained, binding, selected, *, publish=True):
    """A new approved preparation can replace only its cancelled delivery."""
    import abstraction.job.acceptance as job
    with _attempt_record_lock(path):
        with open(path, encoding="utf-8") as source:
            current = json.loads(source.read(65537))
        fields = ("version", "endpoint", "owner", "required", "final", "spec")
        if any(current.get(field) != retained.get(field) for field in fields):
            raise ValueError("the retained preparation request changed")
        active = dict(key=current["key"], history_epoch=current["epoch"], attempt=attempt(current))
        if active != selected and current.get("cancelled_predecessor") != selected:
            raise ValueError("the selected cancelled preparation changed")
        observed = binding.observe_work(job.RequestIdentity(**active))
        cancelled = observed.outcome == "observed" and observed.snapshot.state == "cancelled"
        if active != selected:
            if cancelled:
                raise ValueError("the selected cancellation already has a cancelled successor")
            return current
        if not cancelled:
            raise ValueError("the selected preparation is not terminal cancelled")
        if not publish:
            return current
        epoch = binding.get_history_window().history_epoch
        if not isinstance(epoch, str) or not epoch:
            raise ValueError("the preparation history epoch is unavailable")
        current.update(key=uuid.uuid4().hex, epoch=epoch, attempt=0, cancelled_predecessor=active)
        _replace_json_record(path, current)
        return current


def _retry_attempt(path, retained, selected, binding, *, member=None):
    """Publish the next explicit failed attempt before any reconcile/submit."""
    import abstraction.job.acceptance as job
    with _attempt_record_lock(path):
        with open(path, encoding="utf-8") as source:
            current = json.loads(source.read((1 << 24) if member is not None else 65537))
        fields = ("version", "endpoint", "owner", "epoch", "required", "final", "repo", "revision")
        if any(current.get(key) != retained.get(key) or current.get(key) != selected.get(key) for key in fields):
            raise ValueError("the retained download binding changed before retry")
        entry = current if member is None else current["files"][member]
        prior = selected if member is None else selected["files"][member]
        loaded = retained if member is None else retained["files"][member]
        if any(entry[key] != loaded[key] or entry[key] != prior[key] for key in ("key", "spec")):
            raise ValueError("the retained download request changed before retry")
        active, expected = attempt(entry), attempt(prior)
        if active != expected:
            raise ValueError("the retained download attempt changed before retry")
        identity = job.RequestIdentity(key=entry["key"], history_epoch=current["epoch"], attempt=active)
        observed = binding.observe_work(identity)
        if observed.outcome != "observed" or observed.snapshot.state != "failed":
            return active
        if active == 2**63 - 1:
            raise ValueError("retained download attempt is exhausted")
        entry["attempt"] = active + 1
        _replace_json_record(path, current)
        return active + 1


_LABEL_BYTES = 256


def snapshot_label(repo, revision, path):
    """The display label of one snapshot file's job: `<repo>@<revision> · <path>`.

    Job lists show it so a person sees which repository file each operation
    fetches (abstraction.job JOB-A12). The label is outside the request's
    identity, and this adapter still names the file it writes. Characters the
    label may not hold are dropped. A label over 256 UTF-8 bytes keeps its end,
    where the file name is, behind a leading ellipsis.
    """
    label = "%s@%s · %s" % (repo, revision, path)
    label = "".join(c for c in label if c >= " " and c not in "\x7f  ").strip()
    data = label.encode("utf-8")
    if len(data) > _LABEL_BYTES:
        ellipsis = "…"
        label = ellipsis + data[len(data) - (_LABEL_BYTES - len(ellipsis.encode("utf-8"))):].decode("utf-8", "ignore")
    return label


def _admit(binding, key, epoch, spec, required, fresh, label="", *, attempt=0):
    import abstraction.job.acceptance as job
    identity = job.RequestIdentity(key=key, history_epoch=epoch, attempt=attempt)
    submission = job.Submission(identity=identity, kind="download", spec=spec,
                                required_guarantees=list(required), label=label)
    result = binding.submit(submission) if fresh else binding.reconcile(identity)
    if not fresh and result.outcome == "definitely_not_accepted":
        # Same identity and exact request only. The service may retain a
        # sealed refusal, which remains visible below.
        result = binding.submit(submission)
    if result.outcome != "accepted":
        detail = refusal_detail(result.outcome)
        if detail:
            LOG.warning("[abstraction] DOWNLOAD NOT PERMITTED (forbidden)%s", detail)
        raise RuntimeError("download acceptance: " + result.outcome + "; retained request kept" + detail)
    return identity


def _await(binding, identity, deadline, observe=None):
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError("model wait expired; accepted work and recovery record retained")
        observed = binding.observe_work(identity)
        if observed.outcome != "observed":
            raise RuntimeError("download observation: " + observed.outcome)
        snapshot = observed.snapshot
        if observe is not None:
            observe(snapshot)
        if snapshot.state == "complete":
            return
        if snapshot.state in ("failed", "cancelled"):
            raise RuntimeError("download state: " + snapshot.state + "; retained request kept")
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))


def _copy(binding, identity, final, *, expected_size=0, expected_digest="", _files=PATH_FILES):
    readers = os.environ.get("OA_COMFYUI_REAL_COPY_READERS", "1")
    if readers not in ("1", "4"):
        raise ValueError("OA_COMFYUI_REAL_COPY_READERS must be 1 or 4")
    with _files.staged(final, ".oa-model-", keep_existing=True) as output:
        hashing = _CopyHash(output)
        if readers == "4":
            binding.copy_result(identity, hashing, max_readers=4)
        else:
            binding.copy_result(identity, hashing)
        output.flush()
        if expected_size and hashing.size != expected_size:
            raise ValueError("downloaded model differs from pinned size")
        if expected_digest and "sha256:" + hashing.digest.hexdigest() != expected_digest.lower():
            raise ValueError("downloaded model differs from pinned SHA256")
        os.fsync(output.fileno())


class _CopyHash:
    """Hash bytes as the shared copy helper writes them, before final publish."""

    def __init__(self, output):
        self.output = output
        self.digest = hashlib.sha256()
        self.size = 0

    def write(self, data):
        written = self.output.write(data)
        if written:
            self.digest.update(data[:written])
            self.size += written
        return written
