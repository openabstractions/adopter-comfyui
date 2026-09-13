"""Private native-owned graph DATA on Comfy's existing PromptServer.

No OA requests or authority pass through this page capability. The graph DATA
reaches the ComfyUI connector over the graph channel it names for this
process (below); until it does, the routes refuse. No generic Comfy /ws reuse.
"""
import asyncio
from contextlib import ExitStack
import json
import logging
import os
import queue
import re
import secrets
import stat
import threading
import time

# One page or channel frame. A graph projection is bounded by the page
# (oa_appmodel_graph.mjs: 512 nodes, 4,096 links, 4 KB strings) to fit it.
MAX_FRAME = 1 << 20
OPERATIONS = ("reveal", "edit", "choices", "run")
OUTCOMES = ("applied", "stale", "invalid", "refused", "unavailable", "unknown")
TOKEN = re.compile(r"[A-Za-z0-9_-]{32}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{16,64}\Z")
BASE = "/oa/appmodel"
PROTOCOL = "oa-comfy-graph-v1"
STORAGE = "oa.comfy.native.graph.v1"

# This document runs before the ordinary frontend. No external resource or
# telemetry code observes the fragment; all failures are deliberately generic.
LANDING = """<!doctype html><meta charset="utf-8"><title>ComfyUI</title>
<script>
(()=>{let fragment=location.hash.slice(1);history.replaceState(null,'',location.pathname);
let p=new URLSearchParams(fragment),generation=p.get('generation'),nonce=p.get('nonce');
if(!/^[A-Za-z0-9_-]{16,64}$/.test(generation)||!/^[A-Za-z0-9_-]{32}$/.test(nonce)||
fragment!=='generation='+generation+'&nonce='+nonce)return;
fetch('/oa/appmodel/bootstrap',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({generation,nonce}),credentials:'omit',cache:'no-store'}).then(r=>{nonce='';fragment='';
if(!r.ok)throw Error();return r.json()}).then(v=>{
sessionStorage.setItem('oa.comfy.native.graph.v1',JSON.stringify(v));location.replace('/#oa-appmodel='+generation)
}).catch(()=>{document.body.textContent='Open ComfyUI again from its application.'})})();
</script><body>Opening ComfyUI…</body>"""


# The server event that wakes the extension in ComfyUI Desktop's window while
# a Desktop arm is live; it carries no payload.
DESKTOP_WAKE = "oa-appmodel-arm"
DESKTOP_WAKE_EVERY = 2.0
DESKTOP_PATH_LIMIT = 32767
LOG = logging.getLogger("openabstractions")

# The page's origin: this ComfyUI's loopback listener, on the port the
# connector proved it holds.
ORIGIN = re.compile(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})\Z")


def _origin(value):
    match = ORIGIN.fullmatch(value) if isinstance(value, str) else None
    return match is not None and int(match.group(1)) <= 65535


def _closed(value, fields):
    return type(value) is dict and set(value) == set(fields)


def _desktop_path(value):
    return (isinstance(value, str) and 0 < len(value) <= DESKTOP_PATH_LIMIT and "\0" not in value
            and os.path.isabs(value))


def same_program(a, b):
    """Whether two Windows Desktop image paths open the same ordinary file."""
    if not _desktop_path(a) or not _desktop_path(b):
        return False
    from .oa_interpreter import refused_path
    from .oa_native_host import _held_walk
    try:
        paths = (a, b)
        if any(":" in os.path.splitdrive(path)[1] or refused_path(path) for path in paths):
            return False
        with ExitStack() as observations:
            images = []
            for path in paths:
                facts, _, _ = observations.enter_context(_held_walk(path))
                image = facts[-1]
                if not stat.S_ISREG(image.st_mode) or not image.st_ino:
                    return False
                images.append(image)
            return os.path.samestat(*images)
    except (OSError, ValueError):
        return False


def _id(value):
    return isinstance(value, str) and IDENTIFIER.fullmatch(value) is not None


def _integer(value, minimum=0, maximum=9007199254740991):
    return type(value) is int and minimum <= value <= maximum


def decode(raw):
    if len(raw) > MAX_FRAME:
        raise ValueError("graph frame too large")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate graph field")
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except RecursionError:
        raise ValueError("graph JSON nesting refused") from None
    if type(value) is not dict:
        raise ValueError("graph object required")
    return value


def projection(value):
    """A page projection: its epoch, sequence and graph object. The adapter
    reads the graph; here only its binding and bound are checked."""
    if (not _closed(value, ("epoch", "sequence", "graph")) or not _id(value["epoch"]) or not _integer(value["sequence"])
            or type(value["graph"]) is not dict):
        raise ValueError("invalid projection")
    return value


class Bridge:
    """send writes a frame to the connector. wake(live), called after a Desktop
    arm's acknowledgement, sends DESKTOP_WAKE while live() holds. processes
    answers the Desktop peer check (WindowsProcesses on Windows); without it a
    Desktop arm is refused."""

    def __init__(self, send, clock=time.monotonic, wake=None, processes=None, launchers=None):
        self.send, self.clock = send, clock
        self.wake, self.processes = wake, processes
        self.launchers = venv_launchers() if launchers is None else tuple(launchers)
        # peer is "desktop" while the current arm is a Desktop arm. Its nonce
        # never leaves the channel; only the peer check spends it.
        self.peer = self.desktop_path = None
        self.generation = self.origin = self.nonce = None
        self.nonce_until = 0
        self.last_arm_nonce = None
        self.credential = None
        self.credential_until = 0
        self.page = self.socket = None
        self.lease_until = self.session_until = 0
        self.current = None
        self.pending = None
        self.graph_retired = False
        self.shutdown_requested = False
        self.alive = True
        self._lock = threading.RLock()

    def arm(self, value):
        desktop = _closed(value, ("type", "generation", "nonce", "origin", "peer", "desktop_path"))
        if ((not desktop and not _closed(value, ("type", "generation", "nonce", "origin"))) or value["type"] != "arm"
                or not _id(value["generation"]) or not isinstance(value["nonce"], str)
                or not TOKEN.fullmatch(value["nonce"]) or not _origin(value["origin"])
                or (desktop and (value["peer"] != "desktop" or not _desktop_path(value["desktop_path"])))):
            raise ValueError("invalid arm")
        # Desktop's window proves itself by its process. A platform without
        # its socket-owner lookup (all but Windows today) refuses a Desktop arm.
        if desktop and (os.name != "nt" or self.processes is None):
            LOG.warning("[openabstractions] Desktop window admission is unavailable on this platform")
            return False
        with self._lock:
            if (not self.alive or (self.generation and self.generation != value["generation"])
                    or self.last_arm_nonce == value["nonce"]):
                return False
            self.generation, self.origin = value["generation"], value["origin"]
            self.nonce, self.nonce_until = value["nonce"], self.clock() + 30
            self.last_arm_nonce = value["nonce"]
            self.peer = "desktop" if desktop else None
            self.desktop_path = value["desktop_path"] if desktop else None
            # State is installed before this bounded DATA enqueue. The parent
            # opens a browser only after matching this exact active nonce ACK.
            try:
                self.send({"type": "arm_ack", "generation": self.generation, "nonce": self.nonce})
            except ConnectionError:
                # A full output queue supplies no ACK and no retry. ARM alone
                # preserves the old page; the parent times out without opening.
                return False
            if desktop and self.wake is not None:
                nonce = self.nonce
                self.wake(lambda: self.alive and self.peer == "desktop" and self.nonce == nonce
                          and self.clock() < self.nonce_until)
            return True

    def authorize(self, remote, host, origin=None, require_origin=False):
        return (self.alive and not self.shutdown_requested and self.origin is not None and remote in ("127.0.0.1", "::1")
                and host == self.origin.removeprefix("http://")
                and (not require_origin or origin is not None)
                and (origin is None or origin == self.origin))

    def exchange(self, value):
        with self._lock:
            # A Desktop arm's nonce never left the channel; no page holds it.
            if (not self.alive or self.shutdown_requested or self.peer is not None
                    or not _closed(value, ("generation", "nonce")) or value["generation"] != self.generation
                    or not isinstance(value["nonce"], str)
                    or not self.nonce or self.clock() >= self.nonce_until
                    or not secrets.compare_digest(value["nonce"], self.nonce)):
                raise PermissionError("bootstrap refused")
            return self._issue()

    def exchange_desktop(self, remote, host, origin, peer, listener):
        """The empty-body bootstrap from ComfyUI Desktop's own window. peer and
        listener are this request's socket endpoints, each (ip, port). Each
        peer-check step must pass; a failed step logs its name and refuses."""
        with self._lock:
            step = self._desktop_step(remote, host, origin, peer, listener)
            if step is not None:
                LOG.warning("[openabstractions] Desktop window refused at step %s", step)
                raise PermissionError("bootstrap refused")
            return self._issue()

    def _desktop_step(self, remote, host, origin, peer, listener):
        """The first failed peer-check step's name, or None when all pass."""
        # IPv4 only: the TCP owner lookup reads IPv4 connections.
        if (not self.alive or self.shutdown_requested or remote != "127.0.0.1"
                or not self.authorize(remote, host, origin, require_origin=True)):
            return "authorize"
        if self.peer != "desktop" or not self.nonce or self.clock() >= self.nonce_until:
            return "arm"
        processes = self.processes
        try:
            owner = processes.owner(peer, listener)
            if owner is None:
                return "owner"
            if not same_program(processes.image(owner), self.desktop_path):
                return "image"
            if not desktop_tree(processes, os.getpid(), owner, self.desktop_path, self.launchers):
                return "tree"
        except (OSError, ValueError, TypeError, AttributeError):
            return "owner"
        return None

    def _issue(self):
        """Spend the current arm and issue its bearer session."""
        self.nonce = None
        self.peer = self.desktop_path = None
        # A successful fresh native OPEN explicitly replaces the page.
        if self.pending and not self.pending[1].done():
            self.pending[1].set_exception(ConnectionError("native reopen"))
        self.retire_page()
        self.credential = secrets.token_urlsafe(24)
        self.credential_until = self.clock() + 30
        self.session_until = self.clock() + 600
        self.graph_retired = False
        return {"credential": self.credential, "generation": self.generation}

    def admit(self, credential):
        with self._lock:
            # Each native OPEN admits one document. A later navigation needs
            # another arm even if it retained a copy of the credential.
            if (not self.alive or self.shutdown_requested or self.socket is not None or not self.credential
                    or self.clock() >= self.credential_until
                    or not secrets.compare_digest(credential, self.credential)):
                raise PermissionError("page refused")
            self.credential = None
            self.page = secrets.token_urlsafe(18)
            self.lease_until = self.clock() + 30
            self.current = None
            return self.page

    def retire_graph(self):
        if self.page and not self.graph_retired:
            self.graph_retired = True
            self.current = None
            if self.pending and not self.pending[1].done():
                self.pending[1].set_exception(ConnectionError("graph retired"))
            self.pending = None
            self.send({"type": "closed", "generation": self.generation,
                       "page": self.page, "reason": "graph_expired"})

    def retire_page(self, reason="page_closed"):
        try:
            if self.page:
                self.send({"type": "closed", "generation": self.generation, "page": self.page, "reason": reason})
        finally:
            self.page = self.current = self.pending = self.socket = None

    async def expire_graph(self, page, socket):
        deadline = self.session_until
        await asyncio.sleep(max(0, deadline - self.clock()))
        if self.page == page and self.socket is socket and self.clock() >= deadline:
            try:
                self.retire_graph()
                await socket.send_json({"type": "graph_retired"})
            except (ConnectionError, OSError, RuntimeError, ValueError):
                await socket.close()

    async def request(self, value):
        fields = {"type", "id", "operation", "generation", "page", "workflow", "sequence", "arguments"}
        if (not _closed(value, fields) or value["type"] != "request" or not _id(value["id"])
                or value["operation"] not in OPERATIONS or not _id(value["workflow"])
                or not _integer(value["sequence"]) or type(value["arguments"]) is not dict):
            raise ValueError("invalid request")
        if self.clock() >= self.session_until:
            self.retire_graph()
        if (not self.alive or self.graph_retired or self.shutdown_requested or not self.socket or self.pending or self.clock() >= self.lease_until
                or value["generation"] != self.generation or value["page"] != self.page
                or not self.current or value["workflow"] != self.current["epoch"]):
            self.result(value, "stale")
            return
        future = asyncio.get_running_loop().create_future()
        self.pending = (value, future)
        body = {"operation": value["operation"], "epoch": value["workflow"], "sequence": value["sequence"],
                "arguments": value["arguments"]}
        try:
            await self.socket.send_json({"type": "request", "id": value["id"], "request": body})
            await asyncio.wait_for(future, 30)
        except (asyncio.TimeoutError, ConnectionError, RuntimeError):
            # Delivery can precede disconnect or cancellation. No redispatch.
            if self.page == value["page"]:
                self.graph_retired = True
                self.current = None
            self.result(value, "unknown")
        finally:
            if self.pending and self.pending[0] is value:
                self.pending = None

    def result(self, request, outcome, state=None, answer=None):
        result = {"type": "result", "id": request["id"], "generation": request["generation"],
                  "page": request["page"], "workflow": request["workflow"], "outcome": outcome}
        if state is not None:
            result["projection"] = state
        if answer is not None:
            result["answer"] = answer
        self.send(result)

    def receive(self, value):
        if _closed(value, ("type", "generation", "page")) and value["type"] == "shutdown":
            if (not self.alive or self.shutdown_requested or not self.socket
                    or self.clock() >= self.lease_until
                    or value["generation"] != self.generation or value["page"] != self.page):
                return {"type": "shutdown", "outcome": "refused"}
            # One explicit local UI event, never an operation in request().
            # Queue acceptance proves only a stop request, not a joined backend.
            self.shutdown_requested = True
            self.send({"type": "shutdown", "generation": self.generation, "page": self.page})
            return {"type": "shutdown", "outcome": "requested"}
        if self.shutdown_requested:
            raise ValueError("graph stopping")
        # Heartbeats retain this original page's local Close transport. The
        # absolute graph deadline remains unchanged.
        self.lease_until = self.clock() + 30
        if self.clock() >= self.session_until:
            self.retire_graph()
        if _closed(value, ("type",)) and value["type"] == "ping":
            return {"type": "graph_retired"} if self.graph_retired else None
        if self.graph_retired:
            if value.get("type") in ("projection", "result", "retired", "exhausted"):
                return {"type": "graph_retired"}
            raise ValueError("graph retired")
        if _closed(value, ("type",)) and value["type"] in ("retired", "exhausted"):
            self.current = None
            self.send({"type": "closed", "generation": self.generation,
                       "page": self.page, "reason": "projection_exhausted" if value["type"] == "exhausted" else "workflow_loading"})
            return
        if _closed(value, ("type", "projection")) and value["type"] == "projection":
            state = projection(value["projection"])
            if self.current and state["epoch"] == self.current["epoch"] and state["sequence"] < self.current["sequence"]:
                raise ValueError("decreasing sequence")
            self.current = state
            self.send({"type": "projection", "generation": self.generation, "page": self.page,
                       "workflow": state["epoch"], "sequence": state["sequence"], "graph": state["graph"]})
            return
        if _closed(value, ("type", "id", "outcome", "projection", "answer")) and value["type"] == "result":
            if not self.pending or value["id"] != self.pending[0]["id"]:
                raise ValueError("unbound result")
            if value["outcome"] not in OUTCOMES:
                raise ValueError("unknown outcome")
            if value["answer"] is not None and type(value["answer"]) is not dict:
                raise ValueError("invalid answer")
            state = projection(value["projection"]) if value["projection"] is not None else None
            request, future = self.pending
            if state is not None:
                if state["epoch"] != request["workflow"]:
                    raise ValueError("replaced result")
                if self.current and state["epoch"] == self.current["epoch"] and state["sequence"] < self.current["sequence"]:
                    raise ValueError("decreasing sequence")
                self.current = state
            self.result(request, value["outcome"], state, value["answer"])
            if not future.done():
                future.set_result(None)
            if value["outcome"] in ("unknown", "unavailable"):
                self.graph_retired = True
                self.current = self.pending = None
            return
        raise ValueError("unknown graph message")


def venv_launchers(base=None):
    """This ComfyUI folder's venv launchers: base\\.venv\\Scripts\\python.exe
    and pythonw.exe. base defaults to the folder holding custom_nodes, the
    one this node pack sits in."""
    if base is None:
        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    scripts = os.path.join(base, ".venv", "Scripts")
    return (os.path.join(scripts, "python.exe"), os.path.join(scripts, "pythonw.exe"))


def desktop_tree(processes, python, owner, program, launchers):
    """The owner is ComfyUI Desktop's main process or its direct child.

    The main process is this verified Python process's parent. When that
    parent is one of launchers by full path (ComfyUI Desktop starts this
    folder's venv launcher, which runs the interpreter as its child), the
    main process is the launcher's parent: exactly one launcher, a direct
    child of the main process created after it and before this process. The
    main process's image is program, and it was created before this process
    and before the owner."""
    main = processes.parent(python)
    if main is None:
        return False
    child = processes.created(python)
    if child is None:
        return False
    if any(same_program(processes.image(main), launcher) for launcher in launchers):
        hop, hop_born = main, processes.created(main)
        main = processes.parent(hop)
        if main is None or hop_born is None or not hop_born < child:
            return False
        created = processes.created(main)
        if created is None or not created < hop_born:
            return False
    if not same_program(processes.image(main), program):
        return False
    created = processes.created(main)
    if created is None or not created < child:
        return False
    if owner == main:
        return True
    born = processes.created(owner)
    return processes.parent(owner) == main and born is not None and created < born


class WindowsProcesses:
    """The Desktop peer check's Windows lookups through the stdlib's ctypes:
    GetExtendedTcpTable for a connection's owner, QueryFullProcessImageNameW
    for an image, a Toolhelp snapshot for a parent and GetProcessTimes for a
    creation time. Each answers None when the process is gone."""

    AF_INET, TCP_TABLE_OWNER_PID_ALL = 2, 5
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    TH32CS_SNAPPROCESS = 0x2
    ERROR_INSUFFICIENT_BUFFER = 122

    def __init__(self):
        import ctypes
        from ctypes import wintypes as w
        self.ctypes, self.wintypes = ctypes, w
        self.iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
        self.kernel32 = k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.iphlpapi.GetExtendedTcpTable.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.DWORD), w.BOOL,
                                                      w.ULONG, ctypes.c_int, w.ULONG]
        self.iphlpapi.GetExtendedTcpTable.restype = w.DWORD
        k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        k.OpenProcess.restype = w.HANDLE
        k.CloseHandle.argtypes = [w.HANDLE]
        k.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
        k.QueryFullProcessImageNameW.restype = w.BOOL
        k.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
        k.GetProcessTimes.restype = w.BOOL
        k.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
        k.CreateToolhelp32Snapshot.restype = w.HANDLE

        class Row(ctypes.Structure):
            _fields_ = [("state", w.DWORD), ("local_addr", w.DWORD), ("local_port", w.DWORD),
                        ("remote_addr", w.DWORD), ("remote_port", w.DWORD), ("pid", w.DWORD)]

        class Entry(ctypes.Structure):
            _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("pid", w.DWORD), ("heap", ctypes.c_size_t),
                        ("module", w.DWORD), ("threads", w.DWORD), ("parent", w.DWORD), ("priority", ctypes.c_long),
                        ("flags", w.DWORD), ("exe", w.WCHAR * 260)]
        self.Row, self.Entry = Row, Entry
        k.Process32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
        k.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
        k.Process32FirstW.restype = k.Process32NextW.restype = w.BOOL

    @staticmethod
    def _endpoint(address, port):
        import socket
        import struct
        return socket.inet_ntoa(struct.pack("<I", address)), socket.ntohs(port & 0xFFFF)

    def owner(self, peer, listener):
        """The process owning the TCP connection whose local end is peer and
        whose remote end is listener, both exactly; None unless one row matches."""
        ctypes, w = self.ctypes, self.wintypes
        if not (isinstance(peer, tuple) and isinstance(listener, tuple) and len(peer) == 2 == len(listener)):
            return None
        size = w.DWORD(0)
        for _ in range(4):
            buffer = ctypes.create_string_buffer(max(size.value, ctypes.sizeof(w.DWORD)))
            error = self.iphlpapi.GetExtendedTcpTable(buffer, ctypes.byref(size), False, self.AF_INET,
                                                      self.TCP_TABLE_OWNER_PID_ALL, 0)
            if error == 0:
                break
            if error != self.ERROR_INSUFFICIENT_BUFFER:
                raise OSError(error, "GetExtendedTcpTable failed")
        else:
            raise OSError("GetExtendedTcpTable kept growing")
        count = w.DWORD.from_buffer(buffer).value
        rows = (self.Row * count).from_buffer(buffer, ctypes.sizeof(w.DWORD))
        found = [row.pid for row in rows
                 if self._endpoint(row.local_addr, row.local_port) == (peer[0], peer[1])
                 and self._endpoint(row.remote_addr, row.remote_port) == (listener[0], listener[1])]
        return found[0] if len(found) == 1 else None

    def _open(self, pid):
        return self.kernel32.OpenProcess(self.PROCESS_QUERY_LIMITED_INFORMATION, False, pid) or None

    def image(self, pid):
        handle = self._open(pid)
        if handle is None:
            return None
        try:
            size = self.wintypes.DWORD(32768)
            buffer = self.ctypes.create_unicode_buffer(size.value)
            if not self.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, self.ctypes.byref(size)):
                return None
            return buffer.value
        finally:
            self.kernel32.CloseHandle(handle)

    def created(self, pid):
        handle = self._open(pid)
        if handle is None:
            return None
        try:
            times = [self.wintypes.FILETIME() for _ in range(4)]
            if not self.kernel32.GetProcessTimes(handle, *(self.ctypes.byref(t) for t in times)):
                return None
            return times[0].dwHighDateTime << 32 | times[0].dwLowDateTime
        finally:
            self.kernel32.CloseHandle(handle)

    def parent(self, pid):
        snapshot = self.kernel32.CreateToolhelp32Snapshot(self.TH32CS_SNAPPROCESS, 0)
        if not snapshot or snapshot == self.ctypes.c_void_p(-1).value:
            return None
        try:
            entry = self.Entry()
            entry.size = self.ctypes.sizeof(self.Entry)
            more = self.kernel32.Process32FirstW(snapshot, self.ctypes.byref(entry))
            while more:
                if entry.pid == pid:
                    return entry.parent or None
                more = self.kernel32.Process32NextW(snapshot, self.ctypes.byref(entry))
            return None
        finally:
            self.kernel32.CloseHandle(snapshot)


def install_routes(instance, bridge, web=None, WSMsgType=None):
    """Register on the existing listener; injectable HTTP objects are inert tests.

    bridge is a Bridge, or a callable answering the current one or None."""
    if web is None:
        from aiohttp import web, WSMsgType
    current = bridge if callable(bridge) else (lambda: bridge)
    def check(request, origin=True):
        bridge = current()
        if bridge is None or not bridge.authorize(request.remote, request.headers.get("Host"),
                                request.headers.get("Origin") if origin else None, require_origin=origin):
            raise web.HTTPForbidden(text="graph refused")
        return bridge
    @instance.routes.get(BASE + "/bootstrap")
    async def landing(request):
        check(request, False)
        return web.Response(text=LANDING, content_type="text/html", headers={
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"})
    @instance.routes.post(BASE + "/bootstrap")
    async def exchange(request):
        bridge = check(request)
        if request.content_length == 0:
            # ComfyUI Desktop's window, woken by DESKTOP_WAKE: its process is
            # its proof. Every other page gets the same generic refusal.
            transport = getattr(request, "transport", None)
            peer = transport.get_extra_info("peername") if transport is not None else None
            listener = transport.get_extra_info("sockname") if transport is not None else None
            try:
                previous = bridge.socket
                result = bridge.exchange_desktop(request.remote, request.headers.get("Host"),
                                                 request.headers.get("Origin"),
                                                 tuple(peer[:2]) if peer else None,
                                                 tuple(listener[:2]) if listener else None)
            except PermissionError:
                raise web.HTTPForbidden(text="graph refused") from None
            if previous is not None:
                await previous.close()
            return web.json_response(result, headers={"Cache-Control": "no-store"})
        if (request.content_type != "application/json" or request.content_length is None
                or not 0 < request.content_length <= 256):
            raise web.HTTPForbidden(text="graph refused")
        raw = await request.read()
        if len(raw) > 256:
            raise web.HTTPForbidden(text="graph refused")
        try:
            previous = bridge.socket
            result = bridge.exchange(decode(raw))
        except (ValueError, PermissionError):
            raise web.HTTPForbidden(text="graph refused") from None
        if previous is not None:
            await previous.close()
        return web.json_response(result, headers={"Cache-Control": "no-store"})
    @instance.routes.get(BASE + "/ws")
    async def socket(request):
        bridge = check(request)
        protocols = [v.strip() for v in request.headers.get("Sec-WebSocket-Protocol", "").split(",")]
        if len(protocols) != 2 or protocols[0] != PROTOCOL or not TOKEN.fullmatch(protocols[1]):
            raise web.HTTPForbidden(text="graph refused")
        try:
            page = bridge.admit(protocols[1])
        except PermissionError:
            raise web.HTTPForbidden(text="graph refused") from None
        ws = web.WebSocketResponse(protocols=(PROTOCOL,), max_msg_size=MAX_FRAME, receive_timeout=30)
        bridge.socket = ws
        expiry = None
        try:
            await ws.prepare(request)
            await ws.send_json({"type": "ready", "page": page, "generation": bridge.generation})
            expiry = asyncio.create_task(bridge.expire_graph(page, ws))
            async for message in ws:
                if bridge.socket is not ws or bridge.page != page:
                    break
                if bridge.clock() >= bridge.lease_until:
                    break
                if message.type != WSMsgType.TEXT:
                    break
                notice = bridge.receive(decode(message.data.encode("utf-8")))
                if notice is not None:
                    await ws.send_json(notice)
        except (ValueError, ConnectionError, asyncio.TimeoutError):
            pass
        finally:
            if expiry is not None:
                expiry.cancel()
                try:
                    await expiry
                except asyncio.CancelledError:
                    pass
            if bridge.socket is ws:
                if bridge.pending and not bridge.pending[1].done():
                    bridge.pending[1].set_exception(ConnectionError("page closed"))
                try:
                    bridge.retire_page()
                except (OSError, ValueError):
                    pass
            await ws.close()
        return ws


# The graph channel. While the ComfyUI connector (comfyui-connector.exe beside
# this file) serves this ComfyUI, it listens on a named pipe and names it in
# %LOCALAPPDATA%\OpenAbstractions\ComfyUI\channel-<this process>.json as
# {"pid": <this process>, "pipe": "\\.\pipe\openabstractions-comfyui-graph-<32>"}.
# The pipe admits this account only, and the connector keeps the connection
# only when its client is this process. The connector's page ARMs and the
# adapter's graph requests arrive on it; projections, results and closures
# leave on it. Each connection has a Bridge of its own.

CHANNEL = re.compile(r"\\\\\.\\pipe\\openabstractions-comfyui-graph-[A-Za-z0-9_-]{32}\Z")
CHANNEL_FILE_LIMIT = 4096
CHANNEL_POLL = 0.5
# After a channel ends, the same pipe is tried again only after this pause.
CHANNEL_RETRY = 5.0


def channel_folder(environ=None):
    """%LOCALAPPDATA%\\OpenAbstractions\\ComfyUI, or None without LOCALAPPDATA."""
    local = (os.environ if environ is None else environ).get("LOCALAPPDATA")
    if not local or not os.path.isabs(local):
        return None
    return os.path.join(local, "OpenAbstractions", "ComfyUI")


def announced(folder, pid=None):
    """The graph channel's pipe the connector named for process pid, or None."""
    pid = os.getpid() if pid is None else pid
    try:
        with open(os.path.join(folder, "channel-%d.json" % pid), "rb") as stream:
            raw = stream.read(CHANNEL_FILE_LIMIT + 1)
        value = decode(raw) if len(raw) <= CHANNEL_FILE_LIMIT else None
    except (OSError, ValueError):
        return None
    if (not _closed(value, ("pid", "pipe")) or value["pid"] != pid or not isinstance(value["pipe"], str)
            or not CHANNEL.fullmatch(value["pipe"])):
        return None
    return value["pipe"]


class PipeChannel:
    """The client end of the graph channel, opened for overlapped I/O so one
    thread reads while another writes. The connector learns this process's
    identity from the pipe and is given no right to act as it."""

    GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
    OPEN_EXISTING = 3
    FILE_FLAG_OVERLAPPED = 0x40000000
    SECURITY_SQOS_PRESENT, SECURITY_IDENTIFICATION = 0x00100000, 0x00010000

    def __init__(self, name):
        import _winapi
        self._winapi = _winapi
        self._lock = threading.Lock()
        self._active = set()
        self._closed = False
        self._handle = _winapi.CreateFile(name, self.GENERIC_READ | self.GENERIC_WRITE, 0, _winapi.NULL,
            self.OPEN_EXISTING, self.FILE_FLAG_OVERLAPPED | self.SECURITY_SQOS_PRESENT | self.SECURITY_IDENTIFICATION,
            _winapi.NULL)

    def _run(self, start):
        """Start one overlapped operation and wait for it: (count, error, operation)."""
        with self._lock:
            if self._closed:
                raise ConnectionError("graph channel closed")
            operation, _ = start(self._handle)
            self._active.add(operation)
        try:
            count, error = operation.GetOverlappedResult(True)
        finally:
            with self._lock:
                self._active.discard(operation)
                self._release()
        return count, error, operation

    def _release(self):
        # The handle closes once it is closed and no operation still uses it.
        if self._closed and not self._active and self._handle is not None:
            self._winapi.CloseHandle(self._handle)
            self._handle = None

    def read(self, size):
        """Up to size bytes; b"" once the connector ended the channel."""
        winapi = self._winapi
        try:
            _, error, operation = self._run(lambda handle: winapi.ReadFile(handle, size, overlapped=True))
        except BrokenPipeError:
            return b""
        if error == winapi.ERROR_BROKEN_PIPE:
            return b""
        if error not in (0, winapi.ERROR_MORE_DATA):
            raise ConnectionError("graph channel read failed")
        return bytes(operation.getbuffer())

    def write(self, data):
        written, error, _ = self._run(lambda handle: self._winapi.WriteFile(handle, data, overlapped=True))
        if error:
            raise ConnectionError("graph channel write failed")
        return written

    def close(self):
        """End the channel: operations still pending end with an error."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for operation in self._active:
                try:
                    operation.cancel()
                except OSError:
                    pass
            self._release()


def serve_channel(channel, holder, loop, read_frame, write_frame, wake=None, processes=None, agent_stop=None):
    """Serve one connected graph channel with a fresh Bridge until it ends."""
    outgoing = queue.Queue(maxsize=2)
    requests = queue.Queue(maxsize=2)
    def send(value):
        raw = json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(raw) > MAX_FRAME:
            raise ValueError("graph output too large")
        # A stopped connector reader cannot block PromptServer's event loop.
        # One writer plus two queued frames bound retained output material.
        try:
            outgoing.put_nowait(raw)
        except queue.Full:
            raise ConnectionError("native output unavailable") from None
    bridge = Bridge(send, wake=wake, processes=processes)
    ended = threading.Event()
    def retire_native():
        bridge.alive = False
        if bridge.socket:
            asyncio.create_task(bridge.socket.close())
        if bridge.pending and not bridge.pending[1].done():
            bridge.pending[1].set_exception(ConnectionError("native closed"))
        try:
            bridge.retire_page("native_closed")
        except (OSError, ValueError, ConnectionError):
            pass
        if holder.get("bridge") is bridge:
            holder["bridge"] = None
    def end():
        if not ended.is_set():
            ended.set()
            bridge.alive = False
            channel.close()
            loop.call_soon_threadsafe(retire_native)
            try:
                requests.put_nowait(None)
            except queue.Full:
                pass
    def writer():
        try:
            while not ended.is_set():
                try:
                    raw = outgoing.get(timeout=1)
                except queue.Empty:
                    continue
                write_frame(channel.write, raw, MAX_FRAME)
        except Exception:
            pass
        end()
    def dispatcher():
        # Requests run one at a time, as the adapter sends them; an ARM never
        # waits behind one.
        try:
            while not ended.is_set():
                value = requests.get()
                if value is None:
                    break
                asyncio.run_coroutine_threadsafe(dispatch(value), loop).result()
        except Exception:
            pass
        end()
    async def dispatch(value):
        if (agent_stop is not None and _closed(value, ("type", "id", "key"))
                and value["type"] == "agent_stop" and _id(value["id"])):
            outcome = agent_stop.request(value["key"], lambda: bridge.alive, lambda: bool(bridge.socket))
            send({"type": "agent_stop_result", "id": value["id"], "outcome": outcome})
            return
        await bridge.request(value)
    holder["bridge"] = bridge
    threads = [threading.Thread(target=writer, daemon=True), threading.Thread(target=dispatcher, daemon=True)]
    for thread in threads:
        thread.start()
    try:
        while not ended.is_set():
            raw = read_frame(channel.read, MAX_FRAME)
            if raw is None:
                break
            value = decode(raw)
            if value.get("type") == "arm":
                bridge.arm(value)
            else:
                requests.put_nowait(value)
    except Exception:
        pass
    end()
    for thread in threads:
        thread.join()
    return bridge


def watch_channel(folder, holder, loop, read_frame, write_frame, connect=PipeChannel, clock=time.monotonic,
                  sleep=time.sleep, stop=None, wake=None, processes=None, agent_stop=None):
    """Connect each graph channel the connector names for this process."""
    last, ended_at = None, None
    while stop is None or not stop.is_set():
        name = announced(folder)
        if name is not None and (name != last or clock() - ended_at >= CHANNEL_RETRY):
            try:
                channel = connect(name)
            except OSError:
                channel = None
            if channel is not None:
                try:
                    serve_channel(channel, holder, loop, read_frame, write_frame, wake, processes, agent_stop)
                except Exception:  # a failed channel leaves the next one possible
                    channel.close()
                last, ended_at = name, clock()
        sleep(CHANNEL_POLL)


_installed = None


def install(environ=None):
    """Serve the graph page routes and connect the graph channel whenever the
    connector names one. Without LOCALAPPDATA, ComfyUI keeps its behavior."""
    global _installed
    if _installed is not None:
        return True
    folder = channel_folder(environ)
    if os.name != "nt" or folder is None:
        return False
    from aiohttp import web, WSMsgType
    from abstraction.ipc.stream import read_frame_from, write_frame_to
    from server import PromptServer
    from .oa_listening import AgentStop
    instance = PromptServer.instance
    if instance is None:
        raise ValueError("PromptServer unavailable")
    holder = {"bridge": None}
    stop_key = (os.environ.pop("OA_COMFY_OWNED_STOP", "") if environ is None
                else environ.get("OA_COMFY_OWNED_STOP", ""))
    agent_stop = AgentStop(instance, stop_key)
    install_routes(instance, lambda: holder["bridge"], web, WSMsgType)

    def wake(live):
        # ComfyUI Desktop's window may still be loading when the arm arrives;
        # the event repeats while the arm is live.
        def run():
            while live():
                instance.send_sync(DESKTOP_WAKE, None)
                time.sleep(DESKTOP_WAKE_EVERY)
        threading.Thread(target=run, name="openabstractions-desktop-wake", daemon=True).start()
    try:
        processes = WindowsProcesses()
    except (OSError, AttributeError) as error:
        LOG.warning("[openabstractions] Desktop window admission is unavailable: %s", error)
        processes = None
    threading.Thread(target=watch_channel, name="openabstractions-graph-channel", daemon=True,
                     args=(folder, holder, instance.loop, read_frame_from, write_frame_to),
                     kwargs={"wake": wake, "processes": processes, "agent_stop": agent_stop}).start()
    _installed = holder
    return True
