"""Content storage reuse before a job download.

An OA-routed model install or snapshot file first learns its digest, from the
repository listing or from the resolved model client, and asks the resolved
content storage reader for those bytes. Bytes the reader supplies are delivered
after an exact size and sha256 check, with no source request and no job. Every
other answer for one file is a StorageMiss whose reason the caller logs before
it submits the job download.

A capability the runtime does not offer is reported per process through an
Offer: one line when it stops being offered, no line per file while it is not,
a new resolution at most once per RECHECK_SECONDS, and one line when it is
offered again.
"""
import contextlib
import hashlib
import logging
import os
import threading
import time
from urllib.parse import unquote

from ._model_delivery import PATH_FILES

LOG = logging.getLogger("abstraction")

# Per-file reasons a file is downloaded through the job service instead.
DIGEST_UNKNOWN = "digest_unknown"
STORAGE_NOT_FOUND = "storage_not_found"
STORAGE_REFUSED = "storage_refused"
STORAGE_READ_FAILED = "storage_read_failed"
STORAGE_DIGEST_MISMATCH = "storage_digest_mismatch"

# Per-process reasons, reported by an Offer when availability changes.
MODEL_NOT_OFFERED = "model_not_offered"
STORAGE_NOT_OFFERED = "storage_not_offered"

# The longest a capability that was not offered goes unchecked.
RECHECK_SECONDS = 300.0


class StorageMiss(Exception):
    """Storage did not supply the bytes; reason is one of the constants above.

    quiet is True when the process-level Offer already reported the reason, so
    the caller logs nothing for this file.
    """

    def __init__(self, reason, detail, quiet=False):
        super().__init__("%s: %s" % (reason, detail))
        self.reason, self.detail, self.quiet = reason, detail, quiet


class Offer:
    """Whether the runtime offered one capability at its last resolution.

    The first failure logs one WARNING for the process. Until interval seconds
    pass, resolve skips the capability without a line. A later resolution logs
    again only when the answer changed.
    """

    def __init__(self, contract, reason, interval=RECHECK_SECONDS, clock=time.monotonic):
        self.contract, self.reason, self.interval, self.clock = contract, reason, interval, clock
        self.offered, self.checked = None, None
        self._lock = threading.Lock()

    def due(self):
        with self._lock:
            return self.offered is not False or self.clock() - self.checked >= self.interval

    def resolve(self, bind):
        """Return bind(), or raise a quiet StorageMiss when the capability is not offered."""
        if not self.due():
            raise StorageMiss(self.reason, "%s was not offered at the last check" % self.contract, quiet=True)
        try:
            value = bind()
        except Exception as e:
            self._not_offered("%r" % (e,))
            raise StorageMiss(self.reason, "%s: %r" % (self.contract, e), quiet=True) from e
        self._now_offered()
        return value

    def lost(self, detail):
        """A bound capability failed a call: report it as no longer offered and return the miss to raise."""
        self._not_offered(detail)
        return StorageMiss(self.reason, "%s: %s" % (self.contract, detail), quiet=True)

    def _not_offered(self, detail):
        with self._lock:
            changed = self.offered is not False
            self.offered, self.checked = False, self.clock()
        if changed:
            LOG.warning("[abstraction] OA %s not offered (reason=%s): %s. Storage reuse skips it and checks "
                        "again at most every %d s", self.contract, self.reason, detail, int(self.interval))

    def _now_offered(self):
        with self._lock:
            changed = self.offered is False
            self.offered, self.checked = True, self.clock()
        if changed:
            LOG.info("[abstraction] OA %s is offered again; storage reuse uses it", self.contract)


MODEL = Offer("abstraction.model/resolver@1", MODEL_NOT_OFFERED)
STORAGE = Offer("abstraction.storage/content-reader@1", STORAGE_NOT_OFFERED)


def hf_parts(url):
    """(base, repo, revision, file) of a Hugging Face model resolve URL, or None.

    Manager's URLs arrive rewritten to HF_ENDPOINT when a mirror is set, so the
    mirror's base is recognised as well as huggingface.co.
    """
    bases = ["https://huggingface.co"]
    endpoint = os.environ.get("HF_ENDPOINT")
    if endpoint:
        bases.append(endpoint.rstrip("/"))
    for base in bases:
        if not url.startswith(base + "/"):
            continue
        rest = url[len(base) + 1:].split("#", 1)[0].split("?", 1)[0]
        parts = rest.split("/")
        if len(parts) >= 5 and parts[2] == "resolve" and all(parts):
            return base, parts[0] + "/" + parts[1], unquote(parts[3]), unquote("/".join(parts[4:]))
    return None


def hf_reference(url):
    """(repo, revision, file) of a Hugging Face resolve URL, or None."""
    parts = hf_parts(url)
    return None if parts is None else parts[1:]


# The direct Hugging Face lookup: off disables it. It is a plain network read
# by ComfyUI and needs no OA right.
HF_DIGEST_ENV = "ABSTRACTION_COMFYUI_HF_DIGEST"
HF_API_SECONDS = 15.0
_HF_API_BYTES = 1 << 20


def sha256_hex(value):
    """value as a lowercase 64-hex SHA-256, or None."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.startswith(("w/", "W/")):
        value = value[2:]
    value = value.strip('"')
    if len(value) != 64:
        return None
    try:
        digest = bytes.fromhex(value)
    except ValueError:
        return None
    return digest.hex() if len(digest) == 32 else None


def hf_api_digest(url, deadline=None):
    """(digest, size) of a Hugging Face resolve URL from the Hub's paths-info
    API, or None.

    The Hub publishes the SHA-256 of a Git LFS file, Xet-backed files
    included, as lfs.oid. A file stored in Git itself has no SHA-256 there and
    gets None. A renamed repository answers 307; that one redirect on the same
    host is followed.
    """
    import json
    from urllib.error import HTTPError
    from urllib.parse import quote, urlencode, urljoin, urlsplit
    from urllib.request import Request, urlopen

    if os.environ.get(HF_DIGEST_ENV, "") == "off":
        return None
    parts = hf_parts(url)
    if parts is None:
        return None
    base, repo, revision, file = parts
    api = "%s/api/models/%s/paths-info/%s" % (base, repo, quote(revision, safe=""))
    body = urlencode({"paths": file}).encode("ascii")
    timeout = HF_API_SECONDS
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0:
            return None
    raw = None
    for _ in range(2):
        request = Request(api, data=body, method="POST", headers={
            "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read(_HF_API_BYTES + 1)
            break
        except HTTPError as error:
            location = error.headers.get("Location") if error.code in (301, 302, 307, 308) else None
            if not location:
                LOG.info("[abstraction] Hugging Face paths-info for %s answered %d", file, error.code)
                return None
            moved = urljoin(api, location)
            if urlsplit(moved).netloc != urlsplit(api).netloc:
                return None
            api = moved
        except (OSError, ValueError) as error:
            LOG.info("[abstraction] Hugging Face paths-info for %s failed: %r", file, error)
            return None
    if raw is None or len(raw) > _HF_API_BYTES:
        return None
    try:
        entries = json.loads(raw)
    except ValueError:
        return None
    for entry in entries if isinstance(entries, list) else ():
        if not isinstance(entry, dict) or entry.get("path") != file:
            continue
        lfs = entry.get("lfs") if isinstance(entry.get("lfs"), dict) else {}
        digest = sha256_hex(lfs.get("oid"))
        size = lfs.get("size", entry.get("size"))
        if digest is None or type(size) is not int or size < 0:
            return None
        return "sha256:" + digest, size
    return None


def lookup_digest(machine, url, deadline=None):
    """(digest, size) for url, or raise StorageMiss.

    abstraction.model/resolver@1 answers first. When it gives no digest, the
    Hub's own paths-info API does (hf_api_digest). machine is a callable
    returning the facade Machine.
    """
    try:
        return _resolved_digest(machine, url)
    except StorageMiss as miss:
        if miss.reason not in (DIGEST_UNKNOWN, MODEL_NOT_OFFERED) or hf_reference(url) is None:
            raise
        found = hf_api_digest(url, deadline)
        if found is None:
            raise
        LOG.info("[abstraction] %s: SHA-256 from the Hugging Face API", url.rsplit("/", 1)[-1])
        return found


def _resolved_digest(machine, url):
    reference = hf_reference(url)
    if reference is None:
        raise StorageMiss(DIGEST_UNKNOWN, "not a Hugging Face resolve URL; no model lookup")
    repo, revision, file = reference

    def bind():
        import abstraction.model.api as model
        from abstraction.facade import Scope
        return model, machine().resolve_model(scope=Scope.LOCAL)
    model, resolver = MODEL.resolve(bind)
    try:
        result = resolver.resolve(model.Ref(registry="hf", repo=repo, revision=revision, quant="", file=file))
    except Exception as e:
        raise MODEL.lost("resolve: %r" % (e,)) from e
    request = getattr(result, "request", None)
    if result.outcome != "resolved" or request is None or not request.artifact.digest:
        from . import service
        raise StorageMiss(DIGEST_UNKNOWN, "model lookup: %s%s" % (result.outcome, service.refusal_detail(result.outcome)))
    return request.artifact.digest, request.artifact.size or 0


class _Hashing:
    def __init__(self, output):
        self.output, self.hash = output, hashlib.sha256()

    def write(self, data):
        written = self.output.write(data)
        self.hash.update(data[:written])
        return written


def provider_name(reader):
    """The provider a bound content reader read from.

    A resolved binding carries the reference the runtime returned, and its
    provider is the logical provider that answered (abstraction-facade
    CONTRACT.md FAC-B4). It names who supplied a file and says nothing about
    where that provider keeps its bytes. A reader built any other way exposes
    no reference: then the endpoint it was given, and last the contract, is all
    this can honestly name."""
    try:
        from abstraction.facade.client import reference
    except Exception:
        reference = None
    resolved = reference(reader) if reference is not None else None
    if resolved is not None and resolved.provider:
        return resolved.provider
    endpoint = getattr(getattr(reader, "_transport", None), "endpoint", None)
    if isinstance(endpoint, (bytes, bytearray)):
        endpoint = bytes(endpoint).decode("utf-8", "replace")
    if isinstance(endpoint, str) and endpoint:
        return endpoint
    return STORAGE.contract


def deliver_from_storage(machine, digest, size, final, *, _files=PATH_FILES):
    """Copy the stored bytes named digest into final, or raise StorageMiss.

    machine is a callable returning the facade Machine. The bytes go to a
    temporary file beside final and are renamed into place only when their
    sha256 and length equal the request's. No partial file is left at final.
    Returns the name of the provider that supplied them.
    """
    def bind():
        from abstraction.facade import Scope
        return machine().resolve_storage(scope=Scope.LOCAL)
    reader = STORAGE.resolve(bind)
    try:
        opened = reader.open(digest)
    except Exception as e:
        raise STORAGE.lost("open: %r" % (e,)) from e
    if opened.outcome == "not_found":
        raise StorageMiss(STORAGE_NOT_FOUND, "open: not_found")
    if opened.outcome != "opened":
        raise StorageMiss(STORAGE_REFUSED, "open: " + opened.outcome)
    resource = opened.resource
    try:
        if size and resource.size != size:
            raise StorageMiss(STORAGE_DIGEST_MISMATCH,
                              "storage holds %d bytes, the request says %d" % (resource.size, size))
        with _files.staged(final, ".oa-model-", keep_existing=True) as output:
            hashing = _Hashing(output)
            try:
                copied = reader.copy(resource, hashing)
            except Exception as e:
                raise StorageMiss(STORAGE_READ_FAILED, "read: %r" % (getattr(e, "cause", None) or e,)) from e
            output.flush()
            os.fsync(output.fileno())
            got = "sha256:" + hashing.hash.hexdigest()
            if got != digest or copied != resource.size:
                raise StorageMiss(STORAGE_DIGEST_MISMATCH, "storage delivered %s, %d bytes" % (got, copied))
    finally:
        with contextlib.suppress(Exception):
            reader.close(resource)
    return provider_name(reader)
