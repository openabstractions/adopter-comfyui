"""Hugging Face hub downloads in this ComfyUI through OA download jobs.

ABSTRACTION_COMFYUI_HUB=service points HF_ENDPOINT at a route this node serves
on ComfyUI's own server, /openabstractions/hf. When Manager's prestartup
imported huggingface_hub earlier, a tested release (2.2.x) gets the same
endpoint in its constants (adopt_imported_hub). Another release keeps its
endpoint, with one log line. A person's own endpoint is kept.

The route serves loopback clients only and refuses any request carrying Origin
or Sec-Fetch-Site: hub clients send neither, and a web page cannot drive it.
Every request goes to huggingface.co unchanged, Authorization included, except
a model file Hugging Face publishes a SHA-256 for (X-Linked-Etag). For that
file the HEAD answer keeps X-Repo-Commit, ETag, X-Linked-Etag and
X-Linked-Size and drops Location, the X-Xet-* headers and the xet Links, so
the hub downloads it from this route instead of the CDN or the Xet CAS. The
GET admits one download job pinned to the commit, digest and size, under
ComfyUI's own download right, and streams the job's delivered result; the job
store's retention applies and no copy is made. While the job runs the route
answers 429 with Retry-After, which the hub honours. When OA cannot be
reached, refuses or the job fails, the request goes to huggingface.co.
Authorization goes to huggingface.co only. It is never logged and never
enters a job.
"""
import http.client
import ipaddress
import logging
import os
import re
import sys
import threading
import time
import uuid
from collections import namedtuple
from urllib.parse import quote, unquote, urlsplit

LOG = logging.getLogger("abstraction")

ENV = "ABSTRACTION_COMFYUI_HUB"
PREFIX = "/openabstractions/hf"
# The endpoint this node set, so other adapter code can tell it from a mirror.
MARK = "ABSTRACTION_COMFYUI_HUB_ENDPOINT"
UPSTREAM = "https://huggingface.co"
UPSTREAM_SECONDS = 30.0
RESOLVE_SECONDS = 3.0
FIRST_WAIT_SECONDS = 8.0
POLL_SECONDS = 0.5
RETRY_MIN, RETRY_MAX = 5, 600
# Retry-After before the job has shown any progress.
RETRY_UNKNOWN = 15
MAX_BODY = 16 << 20
CHUNK = 65536
HOP = frozenset(("connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
                 "transfer-encoding", "upgrade", "host", "content-length", "origin", "cookie"))
KEPT_ON_JOB = frozenset(("x-repo-commit", "etag", "x-linked-etag", "x-linked-size"))
_RESOLVE = re.compile(r"^(?P<repo>[^/]+/[^/]+)/resolve/(?P<revision>[^/]+)/(?P<file>.+)$")
_REPO_TYPES = frozenset(("datasets", "spaces", "models", "api"))
UNFINISHED = frozenset(("pending", "running", "transferred"))
_COMMIT = re.compile(r"[0-9a-fA-F]{40}")

Status = namedtuple("Status", "enabled code endpoint detail")
_lock = threading.Lock()
_status = Status(False, "off", None, "not installed")
_registered = []


def status():
    with _lock:
        return _status


def _set(value):
    global _status
    with _lock:
        _status = value
    return value


# --- the endpoint -----------------------------------------------------------

def endpoint_for(listen, port, tls=False):
    """The loopback base URL of this ComfyUI's route, or (None, reason)."""
    if tls:
        return None, "ComfyUI serves TLS; the hub client would not trust its certificate"
    if type(port) is not int or not 0 < port < 65536:
        return None, "ComfyUI's port is unknown"
    hosts = [h.strip().strip("[]") for h in str(listen or "127.0.0.1").split(",") if h.strip()]
    for host in hosts:
        if host in ("0.0.0.0", "127.0.0.1", "localhost") or host.startswith("127."):
            return "http://127.0.0.1:%d%s" % (port, PREFIX), ""
    for host in hosts:
        if host in ("::", "::1"):
            return "http://[::1]:%d%s" % (port, PREFIX), ""
    return None, "ComfyUI listens on %s only, not on loopback" % ",".join(hosts)


# The huggingface_hub releases whose constants this node may assign (architect
# ruling of 2026-11-11, "Hub downloads through OA").
TESTED_HUB = "2.2."
DEFAULT_ENDPOINT = "https://huggingface.co"
_TEMPLATE = "/{repo_id}/resolve/{revision}/{filename}"
_configured = False


def adopt_imported_hub(endpoint, hub, constants, inherited=""):
    """Point an already imported huggingface_hub at endpoint. Returns True
    when it was assigned.

    Only a tested release (2.2.x) whose ENDPOINT, HUGGINGFACE_CO_URL_TEMPLATE
    and HF_URL_HOSTS have their expected types and whose ENDPOINT is the
    default, or inherited, is assigned. inherited is the route an earlier
    ComfyUI process set (MARK); its port may be another program's now.
    HF_URL_HOSTS gains the route's host:port and keeps every entry. The
    route's upstream becomes the default ENDPOINT read before the
    assignment, and huggingface.co in place of an inherited route. HfApi
    instances made earlier keep their own endpoint.
    """
    global UPSTREAM
    version = getattr(hub, "__version__", None)
    if not isinstance(version, str) or not version.startswith(TESTED_HUB):
        LOG.info("[abstraction] hub downloads go to Hugging Face directly: huggingface_hub %s was imported "
                 "before this node and is not a tested release", version)
        return False
    old = getattr(constants, "ENDPOINT", None)
    template = getattr(constants, "HUGGINGFACE_CO_URL_TEMPLATE", None)
    hosts = getattr(constants, "HF_URL_HOSTS", None)
    if not (isinstance(old, str) and isinstance(template, str) and isinstance(hosts, frozenset)):
        LOG.info("[abstraction] hub downloads go to Hugging Face directly: huggingface_hub %s constants "
                 "differ from the tested release", version)
        return False
    if old != DEFAULT_ENDPOINT and not (inherited and old.rstrip("/") == inherited):
        LOG.info("[abstraction] hub downloads keep huggingface_hub's own endpoint")
        return False
    host = urlsplit(endpoint).netloc
    UPSTREAM = DEFAULT_ENDPOINT if old.rstrip("/") == inherited else old
    constants.ENDPOINT = endpoint
    constants.HUGGINGFACE_CO_URL_TEMPLATE = endpoint + _TEMPLATE
    constants.HF_URL_HOSTS = hosts | {host}
    LOG.info("[abstraction] huggingface_hub %s, imported before this node, now downloads through OA: "
             "ENDPOINT %s -> %s, HF_URL_HOSTS gains %s", version, old, endpoint, host)
    return True


def configure_endpoint(args=None, modules=None, environ=None):
    """Point this ComfyUI's hub downloads at this node's route. Returns the
    endpoint or None.

    HF_ENDPOINT is set when it is unset or the default. A huggingface_hub
    imported earlier, as Manager's prestartup does, is assigned through
    adopt_imported_hub; a person's own endpoint is kept either way.

    An HF_ENDPOINT equal to MARK is a route an earlier ComfyUI process set
    and this one inherited. It is replaced with this process's route. When
    this process serves no route, HF_ENDPOINT and MARK are removed: the
    inherited port may belong to another program by now, and a hub client
    sends its token to whatever answers there.
    """
    environ = os.environ if environ is None else environ
    modules = sys.modules if modules is None else modules
    own = environ.get("HF_ENDPOINT", "").rstrip("/")
    inherited = own if own and own == environ.get(MARK, "").rstrip("/") else ""
    if own and own != DEFAULT_ENDPOINT and not inherited:
        LOG.info("[abstraction] hub downloads keep the person's own HF_ENDPOINT")
        return None
    if args is None:
        try:
            from comfy.cli_args import args
        except Exception as error:
            LOG.warning("[abstraction] hub downloads go to Hugging Face directly: ComfyUI arguments: %r", error)
            return _forget(environ, inherited)
    endpoint, reason = endpoint_for(getattr(args, "listen", None), getattr(args, "port", None),
                                    bool(getattr(args, "tls_keyfile", None)))
    if endpoint is None:
        LOG.info("[abstraction] hub downloads go to Hugging Face directly: %s", reason)
        return _forget(environ, inherited)
    hub = modules.get("huggingface_hub")
    if hub is not None and own != endpoint:
        constants = modules.get("huggingface_hub.constants") or getattr(hub, "constants", None)
        if not adopt_imported_hub(endpoint, hub, constants, inherited):
            return _forget(environ, inherited)
    environ["HF_ENDPOINT"] = endpoint
    environ[MARK] = endpoint
    return endpoint


def _forget(environ, inherited):
    """Remove an inherited route from environ. Returns None."""
    if inherited:
        environ.pop("HF_ENDPOINT", None)
        environ.pop(MARK, None)
        LOG.info("[abstraction] HF_ENDPOINT %s came from an earlier ComfyUI and is removed", inherited)
    return None


# --- requests ---------------------------------------------------------------

def admitted(remote, headers):
    """True for a loopback client that sent neither Origin nor Sec-Fetch-Site."""
    if "Origin" in headers or "Sec-Fetch-Site" in headers:
        return False
    try:
        return ipaddress.ip_address((remote or "").split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def model_file(tail):
    """(repo, revision, file) of a model repository resolve path, or None."""
    match = _RESOLVE.match(tail)
    if match is None or match.group("repo").split("/", 1)[0] in _REPO_TYPES:
        return None
    return match.group("repo"), unquote(match.group("revision")), unquote(match.group("file"))


def _header_name(name):
    return name.lower()


def forwarded(headers):
    """The client's request headers to send to huggingface.co."""
    return [(name, value) for name, value in headers.items() if _header_name(name) not in HOP]


def relayed(headers):
    """huggingface.co's response headers for the client: hop-by-hop headers
    dropped, a relative Location kept under this route."""
    out = []
    for name, value in headers:
        lower = _header_name(name)
        if lower in HOP and lower != "content-length":
            continue
        if lower == "location" and value.startswith("/") and not value.startswith("//"):
            value = PREFIX + value
        out.append((name, value))
    return out


def _without_xet_links(value):
    kept = [part for part in value.split(",") if not re.search(r'rel\s*=\s*"?xet-', part)]
    return ",".join(kept).strip()


def job_headers(headers, size):
    """The HEAD answer for a file this route serves through a job."""
    out = [(name, value) for name, value in headers if _header_name(name) in KEPT_ON_JOB]
    for name, value in headers:
        if _header_name(name) == "link":
            rest = _without_xet_links(value)
            if rest:
                out.append(("Link", rest))
    out += [("Content-Length", str(size)), ("Accept-Ranges", "bytes")]
    return out


def header(headers, name):
    wanted = _header_name(name)
    for key, value in headers:
        if _header_name(key) == wanted:
            return value
    return None


Meta = namedtuple("Meta", "digest size commit")


def job_meta(status_code, headers):
    """Meta of a resolve answer this route may serve through a job, or None:
    a SHA-256 X-Linked-Etag, a 40-hex X-Repo-Commit and an X-Linked-Size."""
    if not 200 <= status_code < 400:
        return None
    from .storage_reuse import sha256_hex
    etag = sha256_hex(header(headers, "X-Linked-Etag"))
    commit = header(headers, "X-Repo-Commit") or ""
    try:
        size = int(header(headers, "X-Linked-Size") or "")
    except ValueError:
        return None
    if etag is None or not _COMMIT.fullmatch(commit) or size < 0:
        return None
    return Meta("sha256:" + etag, size, bytes.fromhex(commit).hex())


def byte_range(value, total):
    """The first offset of a "bytes=N-" or "bytes=N-M" Range, or None for the whole file."""
    if not value:
        return None
    match = re.match(r"^bytes=(\d+)-(\d*)$", value.strip())
    if match is None:
        return None
    start = int(match.group(1))
    return start if start < total or total == 0 else None


def retry_after(samples, total):
    """Seconds until the job likely completes, from its progress samples."""
    if len(samples) >= 2 and total:
        (t0, d0), (t1, d1) = samples[0], samples[-1]
        if t1 > t0 and d1 > d0:
            remaining = (total - d1) / ((d1 - d0) / (t1 - t0))
            return int(min(RETRY_MAX, max(RETRY_MIN, remaining)))
    return RETRY_UNKNOWN


# --- upstream ---------------------------------------------------------------

class Upstream:
    """One request to huggingface.co (UPSTREAM). Redirects are relayed, never followed."""

    def __init__(self, method, target, headers, body=None, timeout=UPSTREAM_SECONDS):
        parts = urlsplit(UPSTREAM)
        kind = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        self.connection = kind(parts.netloc, timeout=timeout)
        path = (parts.path.rstrip("/") + "/" + target.lstrip("/"))
        self.connection.request(method, path, body=body, headers=dict(headers))
        self.response = self.connection.getresponse()
        self.status = self.response.status
        self.headers = self.response.getheaders()

    def read(self, size=CHUNK):
        return self.response.read(size)

    def close(self):
        self.connection.close()


# --- jobs -------------------------------------------------------------------

class _Served:
    def __init__(self, binding, identity, meta):
        self.binding, self.identity, self.meta = binding, identity, meta
        self.samples = []


class Jobs:
    """The download jobs this route admitted, by digest, for this process."""

    def __init__(self, service=None):
        self._service = service
        self._served = {}
        self._lock = threading.Lock()

    def service(self):
        if self._service is None:
            from . import service
            self._service = service
        return self._service

    def reachable(self):
        service = self.service()
        try:
            service._resolve(time.monotonic() + RESOLVE_SECONDS, service._required())
            return True
        except Exception as error:
            if service.unreachable(error):
                return False
            raise

    def admit(self, repo, file, meta):
        """The job serving meta's file, admitted once per digest."""
        with self._lock:
            found = self._served.get(meta.digest)
            if found is not None:
                return found
            service = self.service()
            required = service._required()
            binding = service._resolve(time.monotonic() + RESOLVE_SECONDS, required)
            window = binding.get_history_window()
            url = "%s/%s/resolve/%s/%s" % ("https://huggingface.co", repo, meta.commit, quote(file))
            spec = service._spec(url, meta.size, meta.digest)
            identity = service._admit(binding, uuid.uuid4().hex, window.history_epoch, spec, required, True,
                                      label="%s/%s" % (repo, file))
            served = self._served[meta.digest] = _Served(binding, identity, meta)
            LOG.info("[abstraction] hub file %s/%s goes through an OA download job", repo, file)
            return served

    def forget(self, digest):
        with self._lock:
            self._served.pop(digest, None)

    def state(self, served):
        """(state, done, total) of served's job."""
        observed = served.binding.observe_work(served.identity)
        if observed.outcome != "observed":
            return "failed", 0, 0
        snapshot = observed.snapshot
        progress = getattr(snapshot, "progress", None)
        done, total = getattr(progress, "done", 0) or 0, getattr(progress, "total", 0) or served.meta.size
        served.samples = (served.samples + [(time.monotonic(), done)])[-20:]
        state = getattr(snapshot.state, "value", snapshot.state)
        return state, done, total

    def read(self, served, offset):
        result = served.binding.read_result(served.identity, offset, CHUNK)
        if result.outcome != "data":
            raise OSError("job result: " + str(getattr(result.outcome, "value", result.outcome)))
        return result.chunk


# --- the route --------------------------------------------------------------

def install(routes=None, jobs=None, args=None):
    value = os.environ.get(ENV, "")
    if value == "":
        return _set(Status(False, "off", None, "%s is unset" % ENV))
    if value != "service":
        LOG.warning("[abstraction] HUB ROUTE NOT SERVED: %s=%r; supported values are unset and service", ENV, value)
        return _set(Status(True, "invalid_configuration", None, "%s=%r" % (ENV, value)))
    try:
        if routes is None:
            server = sys.modules.get("server")
            routes = server.PromptServer.instance.routes
    except Exception as error:
        LOG.warning("[abstraction] HUB ROUTE NOT SERVED: %r", error)
        return _set(Status(True, "server_unavailable", None, repr(error)))
    jobs = jobs or Jobs()
    if not any(table is routes for table in _registered):
        handler = Handler(jobs)

        async def hub(request):
            return await handler(request)
        routes.route("*", PREFIX + "/{tail:.*}")(hub)
        _registered.append(routes)
    global _configured
    # Once per process, at node-pack import.
    endpoint = None if _configured else configure_endpoint(args)
    _configured = True
    return _set(Status(True, "serving", endpoint, "%s serves hub downloads through OA" % PREFIX))


class Handler:
    def __init__(self, jobs):
        self.jobs = jobs

    async def __call__(self, request):
        from aiohttp import web
        if not admitted(request.remote, request.headers):
            return web.Response(status=403, text="the OpenAbstractions hub route serves local hub clients only")
        tail = request.match_info["tail"]
        # The query as the client encoded it: a decoded one would turn %26, %3D
        # and %23 into separators and a space into a request line http.client refuses.
        query = request.rel_url.raw_query_string
        target = "/" + quote(tail, safe="/@:+=,;~!$&'()*") + (("?" + query) if query else "")
        found = model_file(tail) if request.method in ("GET", "HEAD") else None
        if found is not None:
            answered = await self._through_job(request, target, found)
            if answered is not None:
                return answered
        return await self._passthrough(request, target)

    async def _run(self, function, *args):
        import asyncio
        return await asyncio.get_running_loop().run_in_executor(None, function, *args)

    async def _passthrough(self, request, target, body=None):
        from aiohttp import web
        if body is None and request.can_read_body:
            body = await request.content.read(MAX_BODY + 1)
            if len(body) > MAX_BODY:
                raise web.HTTPRequestEntityTooLarge(max_size=MAX_BODY, actual_size=len(body))
        try:
            upstream = await self._run(Upstream, request.method, target, forwarded(request.headers), body or None)
        except (OSError, http.client.HTTPException) as error:
            return web.Response(status=502, text="huggingface.co did not answer: %s" % type(error).__name__)
        try:
            response = web.StreamResponse(status=upstream.status)
            for name, value in relayed(upstream.headers):
                response.headers.add(name, value)
            await response.prepare(request)
            if request.method != "HEAD":
                while True:
                    data = await self._run(upstream.read, CHUNK)
                    if not data:
                        break
                    await response.write(data)
            await response.write_eof()
            return response
        finally:
            upstream.close()

    async def _through_job(self, request, target, found):
        """The answer for a resolve request served through a job, or None to pass it through."""
        from aiohttp import web
        repo, _revision, file = found
        try:
            head = await self._run(Upstream, "HEAD", target, forwarded(request.headers), None)
        except (OSError, http.client.HTTPException):
            return None
        head.close()
        meta = job_meta(head.status, head.headers)
        if meta is None:
            return None
        if request.method == "HEAD":
            try:
                reachable = await self._run(self.jobs.reachable)
            except Exception as error:
                LOG.info("[abstraction] hub file %s/%s goes to Hugging Face: %r", repo, file, error)
                return None
            if not reachable:
                return None
            response = web.Response(status=200)
            for name, value in job_headers(head.headers, meta.size):
                response.headers[name] = value
            return response
        try:
            served = await self._run(self.jobs.admit, repo, file, meta)
            end = time.monotonic() + FIRST_WAIT_SECONDS
            while True:
                state, done, total = await self._run(self.jobs.state, served)
                if state not in UNFINISHED:
                    break
                if time.monotonic() >= end:
                    seconds = retry_after(served.samples, total)
                    return web.Response(status=429, headers={"Retry-After": str(seconds)},
                                        text="OpenAbstractions is downloading this file")
                await self._sleep(POLL_SECONDS)
            if state != "complete":
                raise RuntimeError("download job " + str(state))
            return await self._stream(request, served)
        except Exception as error:
            self.jobs.forget(meta.digest)
            LOG.warning("[abstraction] hub file %s/%s goes to Hugging Face: %s", repo, file,
                        type(error).__name__ + ": " + str(error)[:200])
            return None

    async def _sleep(self, seconds):
        import asyncio
        await asyncio.sleep(seconds)

    async def _stream(self, request, served):
        from aiohttp import web
        total = served.meta.size
        start = byte_range(request.headers.get("Range"), total)
        offset = start or 0
        first = await self._run(self.jobs.read, served, offset)
        if first.total != total:
            raise OSError("job result has %d bytes, the hub file %d" % (first.total, total))
        response = web.StreamResponse(status=206 if start else 200)
        response.headers["Content-Length"] = str(total - offset)
        response.headers["Accept-Ranges"] = "bytes"
        response.headers["ETag"] = '"%s"' % served.meta.digest[7:]
        if start:
            response.headers["Content-Range"] = "bytes %d-%d/%d" % (offset, total - 1, total)
        await response.prepare(request)
        chunk = first
        while True:
            if chunk.data:
                await response.write(chunk.data)
                offset += len(chunk.data)
            if chunk.eof:
                break
            chunk = await self._run(self.jobs.read, served, offset)
        await response.write_eof()
        return response
