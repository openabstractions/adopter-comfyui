"""The comfyui application's adapter, started by the ComfyUI connector.

The connector (adopters/comfyui/connector, comfyui-connector.exe beside this
file) is the program OpenAbstractions knows this ComfyUI by. It runs this file
with this ComfyUI's own Python and supplies the service-issued instance, two
inherited control handles, two private graph DATA handles, the application
manifest and the installed runtime's application-model library. MCP parsing,
schemas and retirement belong to the shared SDK, which this node pack carries
in oa_client. The control channel has fixed one-byte requests, one bounded ARM
acknowledgement and one bounded answer naming the served ComfyUI's port and
output folder; it cannot carry an OA request, path, command, environment or
argument tail. Agent workflow commands use ComfyUI's own HTTP API on the
loopback listener the connector proved belongs to this ComfyUI.
"""
import collections
from contextlib import ExitStack, contextmanager
import errno
import hashlib
import http.client
import json
import os
import re
import secrets
import shutil
import stat
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

# One graph channel frame (oa_appmodel_bridge.py MAX_FRAME), and the shared
# core's snapshot bound for this adapter's contexts.
GRAPH_LIMIT = 1 << 20
SNAPSHOT_BYTES = 64 * 1024
_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,64}")


# The connector found ComfyUI Desktop's (or Comfy Desktop's) program
# unsigned: its window gets no graph context, and app_reveal opens the
# browser page instead. The notice's reason names the program.
DESKTOP_UNSIGNED = "ComfyUI Desktop isn't signed, so agents use the browser page"
COMFY_DESKTOP_UNSIGNED = "Comfy Desktop isn't signed, so agents use the browser page"
UNSIGNED_NOTES = {"desktop_unsigned": DESKTOP_UNSIGNED, "comfy_desktop_unsigned": COMFY_DESKTOP_UNSIGNED}


class GraphUnavailable(RuntimeError):
    """Fixed diagnostic; never carries page data or credentials."""


def native_registry(instance, registry_type, records, refusal_type, library=None):
    class NativeRegistry(registry_type):
        # Retain the original typed invocation across the shared core's unlocked
        # handler boundary. No MCP parsing or core decision is duplicated here.
        def __init__(self):
            options = {} if library is None else {"library": library}
            super().__init__(instance, extensions=("proposal", "async"), bounds={"snapshot_bytes": SNAPSHOT_BYTES,"calls_in_flight":1},
                             **options)
            self.native_invocation = threading.local()

        def _dispatch_callback(self, event, raw):
            if event.kind != self._command_event:
                return super()._dispatch_callback(event, raw)
            invocation = records.from_json(records.Invocation, raw)
            previous = getattr(self.native_invocation, "value", None)
            self.native_invocation.value = invocation
            graph = getattr(self, "native_graph", None)
            # Only graph commands hold person projections back; workflow commands
            # use ComfyUI's HTTP API and never touch the page graph.
            if not self._callback_names.get(event.callback, "").startswith("graph."): graph = None
            if graph is not None: graph.enter_call()
            try:
                return super()._dispatch_callback(event, raw)
            finally:
                if graph is not None: graph.leave_call()
                self.native_invocation.value = previous

        def _read_callback(self, context):
            graph = getattr(self,"native_graph",None)
            if graph is None: return super()._read_callback(context)
            with graph.lock:
                if graph.calls:
                    return records.ContextSnapshot(outcome=records.ReadingOutcome.EXHAUSTED,
                        context=context.ref(),revision=context.revision,reason="bound:calls_in_flight")
                return super()._read_callback(context)

        def _reveal_callback(self, context, request):
            try:
                return super()._reveal_callback(context, request)
            except refusal_type as error:
                return records.CommandResult(outcome=records.CommandOutcome(str(error.outcome)),
                                             reason=error.reason,revision=getattr(error,"native_revision",""))
            except GraphUnavailable as error:
                return records.CommandResult(outcome=records.CommandOutcome.UNAVAILABLE,
                                             reason="frontend_unavailable",revision=getattr(error,"native_revision",""))
    return NativeRegistry()


def _graph_integer(value, low=0, high=9007199254740991):
    return type(value) is int and low <= value <= high


def _graph_token(value):
    return isinstance(value, str) and _TOKEN.fullmatch(value) is not None


def _arm_ack_bytes(message):
    if (not isinstance(message,dict) or set(message)!={"type","generation","nonce"}
            or message["type"]!="arm_ack" or not _graph_token(message["generation"])
            or not _graph_token(message["nonce"]) or len(message["nonce"])!=32):
        raise ValueError("native ARM acknowledgement shape")
    raw=json.dumps(message,separators=(",",":")).encode("utf-8")
    if len(raw)>256: raise ValueError("native ARM acknowledgement bound")
    return raw


# The agent's view of the open canvas (adopters/comfyui/GRAPH.md): the page
# projects it (node/web/oa_appmodel_graph.mjs) and this adapter keeps a clean
# copy, bounded again here, for graph.read and the commands' checks.
MAX_NODES = 512
MAX_LINKS = 4096
MAX_STRING = 4096
MAX_CHOICES = 64
MAX_SLOTS = 128
MAX_OPS = 64
# graph.read's answer stays well inside the shared core's 256 KiB result.
READ_BYTES = 200 * 1024
MODES = ("always", "bypass", "mute")
WIDGET_KINDS = ("int", "float", "string", "bool", "combo", "seed")
_NODE_ID = re.compile(r"(?:0|[1-9][0-9]{0,15})\Z")
NO_PAGE = ("No ComfyUI page is attached; open ComfyUI with oa_applications_open, "
           "and its canvas attaches once the page loads")


def _text(value, limit=MAX_STRING):
    """value as text cut at limit characters, and whether it was cut."""
    text = value if isinstance(value, str) else ""
    return (text[:limit], True) if len(text) > limit else (text, False)


def _number(value):
    return type(value) in (int, float) and value == value and value not in (float("inf"), float("-inf"))


def _slots(rows):
    out = []
    for row in rows[:MAX_SLOTS] if isinstance(rows, list) else ():
        if isinstance(row, dict):
            out.append({"name": _text(row.get("name"), 256)[0], "type": _text(row.get("type"), 256)[0] or "*"})
    return out


def _widget(row):
    """One widget of the page's projection, cleaned, or None."""
    if not isinstance(row, dict) or row.get("type") not in WIDGET_KINDS:
        return None
    name, _ = _text(row.get("name"), 256)
    if not name:
        return None
    kind, value = row["type"], row.get("value")
    widget = {"name": name, "type": kind}
    if kind in ("int", "seed"):
        if type(value) is not int:
            return None
    elif kind == "float":
        if not _number(value):
            return None
    elif kind == "bool":
        if type(value) is not bool:
            return None
    else:
        if kind == "combo" and _number(value):
            pass
        elif isinstance(value, str):
            value, cut = _text(value)
            if cut or row.get("truncated") is True:
                widget["truncated"] = True
        else:
            return None
    widget["value"] = value
    if kind in ("int", "seed", "float"):
        for key in ("min", "max", "step"):
            if _number(row.get(key)):
                widget[key] = row[key]
    if kind == "combo":
        choices = row.get("choices")
        if isinstance(choices, list) and len(choices) <= MAX_CHOICES:
            widget["choices"] = [c if _number(c) else _text(c)[0] for c in choices if isinstance(c, str) or _number(c)]
        else:
            count = row.get("choices_count")
            widget["choices_count"] = count if type(count) is int and count >= 0 else len(choices) if isinstance(choices, list) else 0
    if row.get("linked") is True:
        widget["linked"] = True
    return widget


def sanitize_graph(raw):
    """The page's projected graph as the agent reads it: nodes, links and
    groups, within MAX_NODES, MAX_LINKS and MAX_STRING. Malformed rows are
    left out with a log line."""
    if not isinstance(raw, dict):
        raise ValueError("graph object required")
    nodes, ids, dropped = [], set(), 0
    for row in raw.get("nodes") or () if isinstance(raw.get("nodes"), list) else ():
        node_id = row.get("id") if isinstance(row, dict) else None
        if not isinstance(node_id, str) or not _NODE_ID.fullmatch(node_id) or node_id in ids:
            dropped += 1
            continue
        if len(nodes) == MAX_NODES:
            raw = dict(raw, truncated=True)
            break
        title, cut = _text(row.get("title"))
        node = {"id": node_id, "type": _text(row.get("type"), 256)[0], "title": title,
                "mode": row.get("mode") if row.get("mode") in MODES else "always", "widgets": [],
                "inputs": _slots(row.get("inputs")), "outputs": _slots(row.get("outputs"))}
        if cut or row.get("truncated") is True:
            node["truncated"] = True
        names = set()
        for item in row.get("widgets") or () if isinstance(row.get("widgets"), list) else ():
            widget = _widget(item)
            if widget is None or widget["name"] in names:
                dropped += widget is None
                continue
            names.add(widget["name"])
            node["widgets"].append(widget)
        ids.add(node_id)
        nodes.append(node)
    links = []
    for row in raw.get("links") or () if isinstance(raw.get("links"), list) else ():
        if (not isinstance(row, dict) or row.get("from") not in ids or row.get("to") not in ids
                or type(row.get("from_slot")) is not int or type(row.get("to_slot")) is not int):
            dropped += 1
            continue
        if len(links) == MAX_LINKS:
            raw = dict(raw, truncated=True)
            break
        links.append({"from": row["from"], "from_slot": row["from_slot"], "to": row["to"], "to_slot": row["to_slot"]})
    groups = []
    for row in raw.get("groups") or () if isinstance(raw.get("groups"), list) else ():
        if isinstance(row, dict) and isinstance(row.get("members"), list):
            groups.append({"title": _text(row.get("title"), 256)[0],
                           "members": [m for m in row["members"][:MAX_NODES] if m in ids]})
    if dropped:
        _log("graph projection: %d malformed rows left out" % dropped)
    view = {"nodes": nodes, "links": links, "groups": groups}
    if raw.get("truncated") is True:
        view["truncated"] = True
    return view


def _graph_projection(message):
    """(binding, sequence, view) of one projection frame from the page."""
    if set(message) != {"type", "generation", "page", "workflow", "sequence", "graph"} or message["type"] != "projection":
        raise ValueError("graph projection shape")
    binding = tuple(message[name] for name in ("generation", "page", "workflow"))
    if not all(map(_graph_token, binding)) or not _graph_integer(message["sequence"]):
        raise ValueError("graph binding")
    return binding, message["sequence"], sanitize_graph(message["graph"])


def _label(text):
    """An entity label: at most 256 UTF-8 bytes."""
    raw = text.encode("utf-8")[:256]
    return raw.decode("utf-8", "ignore")


def _fit(view):
    """view within READ_BYTES: inline choices become counts, then nodes are
    cut from the end and the view is marked truncated."""
    def size(value):
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if size(view) <= READ_BYTES:
        return view
    nodes = []
    for node in view["nodes"]:
        widgets = []
        for widget in node["widgets"]:
            if "choices" in widget:
                count = len(widget["choices"])
                widget = {k: v for k, v in widget.items() if k != "choices"}
                widget["choices_count"] = count
            widgets.append(widget)
        nodes.append(dict(node, widgets=widgets))
    fitted = dict(view, nodes=nodes)
    while size(fitted) > READ_BYTES and fitted["nodes"]:
        kept = fitted["nodes"][:max(1, len(fitted["nodes"]) * 3 // 4)] if len(fitted["nodes"]) > 1 else []
        ids = {node["id"] for node in kept}
        fitted = dict(fitted, nodes=kept, truncated=True,
                      links=[l for l in fitted["links"] if l["from"] in ids and l["to"] in ids],
                      groups=[dict(g, members=[m for m in g["members"] if m in ids]) for g in fitted["groups"]])
    return fitted


def _find(view, node_id):
    return next((node for node in view["nodes"] if node["id"] == node_id), None)


def check_op(view, op, index):
    """One graph.edit operation checked against the agent's view: None, or
    the plain reason it is refused."""
    where = "ops[%d]: " % index
    if not isinstance(op, dict) or op.get("op") not in ("set_widget", "set_mode"):
        return where + "op is set_widget or set_mode"
    node = _find(view, op.get("node")) if isinstance(op.get("node"), str) else None
    if node is None:
        return where + "node %s is not on the canvas; graph.read lists the nodes" % json.dumps(op.get("node"))
    if op["op"] == "set_mode":
        if set(op) != {"op", "node", "mode"} or op["mode"] not in MODES:
            return where + "set_mode takes node and mode: always, bypass or mute"
        return None
    if set(op) != {"op", "node", "widget", "value"} or not isinstance(op["widget"], str):
        return where + "set_widget takes node, widget and value"
    widget = next((w for w in node["widgets"] if w["name"] == op["widget"]), None)
    if widget is None:
        return where + "node %s has no widget %s" % (node["id"], json.dumps(op["widget"]))
    if widget.get("linked"):
        return where + "widget %s takes its value from a link" % widget["name"]
    kind, value = widget["type"], op["value"]
    low, high = widget.get("min"), widget.get("max")
    if kind in ("int", "seed", "float"):
        if (type(value) is not int if kind != "float" else not _number(value)):
            return where + "%s takes a %s" % (widget["name"], "number" if kind == "float" else "whole number")
        if (low is not None and value < low) or (high is not None and value > high):
            return where + "%s takes %s to %s" % (widget["name"], low if low is not None else "-inf",
                                                   high if high is not None else "inf")
    elif kind == "bool" and type(value) is not bool:
        return where + "%s takes true or false" % widget["name"]
    elif kind == "string" and not isinstance(value, str):
        return where + "%s takes text" % widget["name"]
    elif kind == "combo":
        if not isinstance(value, str) and not _number(value):
            return where + "%s takes one of its choices" % widget["name"]
        if "choices" in widget and value not in widget["choices"]:
            return where + "%s has no choice %s; its choices are %s" % (
                widget["name"], json.dumps(value), ", ".join(map(str, widget["choices"][:16])))
    return None


class GraphSession:
    """The open canvas of one attached ComfyUI page, mapped into the shared
    application core as the graph context and the graph.* commands."""
    def __init__(self, registry, incoming, outgoing, read_frame, write_frame, refusal_type, timeout=30, shutdown=None, arm_ack=None,
                 show_page=None, follow=None):
        self.registry, self.incoming, self.outgoing = registry, incoming, outgoing
        self.read_frame, self.write_frame, self.Refuse = read_frame, write_frame, refusal_type
        self.timeout, self.lock = timeout, threading.RLock()
        self.shutdown = shutdown
        self.arm_ack = arm_ack
        # show_page asks the connector to open the browser graph page; note
        # is the unsigned sentence (UNSIGNED_NOTES) once the connector said so.
        self.show_page, self.note = show_page, None
        # follow(call, prompts) answers the job that follows queued prompts
        # (WorkflowCommands.follow_prompts).
        self.follow = follow
        self.context, self.binding, self.sequence, self.view = None, None, 0, None
        self.pending, self.blocked, self.ended = None, None, False
        self.blocked_page = None
        self.mediation = True
        self.calls, self.held = 0, None
        registry.native_graph = self
        self.status = "waiting"
        registry.kind("node", schema={"type": "object", "properties": {
            "type": {"type": "string"}, "mode": {"type": "string"}},
            "required": ["type", "mode"], "additionalProperties": False}, title="ComfyUI node")
        registry.kind("graph", schema={"type": "object", "properties": {
            "sequence": {"type": "integer"}, "nodes": {"type": "integer"}, "links": {"type": "integer"}},
            "required": ["sequence", "nodes", "links"], "additionalProperties": False}, title="ComfyUI canvas")
        revision = {"type": "string", "maxLength": 64,
                    "description": "The revision graph.read answered; a canvas changed since is refused."}
        registry.command("graph.read", self.read, "read", input_schema={
            "type": "object", "properties": {}, "additionalProperties": False},
            title="Read the canvas",
            description="Read the open ComfyUI canvas: nodes with their widgets and slots, links, groups and the "
                        "revision graph.edit and graph.run take.")
        registry.command("graph.choices", self.choices, "read", input_schema={
            "type": "object", "properties": {"node": {"type": "string", "maxLength": 16},
            "widget": {"type": "string", "maxLength": 256}, "query": {"type": "string", "maxLength": 256},
            "cursor": {"type": "string", "maxLength": 16}},
            "required": ["node", "widget"], "additionalProperties": False},
            title="List a widget's choices",
            description="List the choices of a combo widget whose graph.read row has choices_count, narrowed by "
                        "query and paged by cursor.")
        registry.command("graph.edit", self.edit, "edit", input_schema={
            "type": "object", "properties": {"ops": {"type": "array", "minItems": 1, "maxItems": MAX_OPS,
            "items": {"type": "object"}}, "expected_revision": revision},
            "required": ["ops"], "additionalProperties": False},
            title="Edit the canvas",
            description="Edit the open canvas as one ComfyUI undo step with ops "
                        "{op: set_widget, node, widget, value} and {op: set_mode, node, mode: always, bypass or mute}.")
        registry.command("graph.run", self.run, "commit", input_schema={
            "type": "object", "properties": {"expected_revision": revision}, "additionalProperties": False},
            title="Run the canvas",
            description="Queue the open canvas through the page's own Queue, so the person sees the progress, and "
                        "answer a job to follow with job.status.",
            long_running=True)
        registry.on_reveal = self.reveal
        self.thread = threading.Thread(target=self._receive, name="comfy-graph-data", daemon=True)

    def start(self):
        self.thread.start()

    def enter_call(self):
        with self.lock:
            self.calls += 1

    def leave_call(self):
        with self.lock:
            self.calls -= 1
            if not self.calls and self.held is not None:
                held, self.held = self.held, None
                try:
                    self._install(held)
                except (ValueError, GraphUnavailable):
                    self._retire("transport_failed")

    def _person_projection(self, message):
        # A person's change during an agent's call is published after the
        # call: each projection is the whole canvas, so the newest one stands.
        binding, _, _ = _graph_projection(message)
        if not self.mediation or binding == self.blocked or binding[:2] == self.blocked_page:
            return
        if self.calls:
            self.held = message
        else:
            self._install(message)

    def _read_state(self):
        # Immutable replacement avoids taking the application lock while the
        # shared core holds its own lock to project this state.
        return list(self.entities), {}

    entities = ()

    def _retire(self, status="closed", preserve_new=False, retire_page=False):
        if retire_page and self.binding is not None:
            self.blocked_page = self.binding[:2]
        held = self.held if preserve_new and self.held is not None else None
        if held is not None:
            binding = _graph_projection(held)[0]
            if binding == self.binding or binding[:2] == self.blocked_page:
                held = None
        if self.context is not None:
            self.context.close()
        self.context, self.view = None, None
        self.entities = ()
        self.held = held
        self.status, self.blocked = status, self.binding
        if self.pending is not None:
            self.pending["answer"] = None
            self.pending["done"].set()

    def _install(self, message, call=None):
        binding, sequence, view = _graph_projection(message)
        if binding == self.blocked or binding[:2] == self.blocked_page:
            return False
        if self.binding == binding and sequence < self.sequence:
            raise ValueError("graph sequence moved backwards")
        replaced = self.context is None or self.binding != binding
        if replaced and self.context is not None:
            self.context.close()
            self.context = None
        self.binding, self.sequence, self.view = binding, sequence, view
        self.entities = tuple([{"id": "graph", "kind": "graph", "label": "Canvas",
                                "properties": {"sequence": sequence, "nodes": len(view["nodes"]), "links": len(view["links"])}}]
                              + [{"id": "node-" + node["id"], "kind": "node", "label": _label(node["title"] or node["type"]),
                                  "properties": {"type": node["type"], "mode": node["mode"]}} for node in view["nodes"]])
        if self.context is None:
            self.context = self.registry.context("graph", self._read_state, title="ComfyUI canvas")
        elif call is not None and call.context is self.context:
            call.commit()
        else:
            self.context.commit()
        self.status = "connected"
        return True

    @staticmethod
    def _unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ValueError("duplicate graph field")
            result[key] = value
        return result

    def _receive(self):
        try:
            while True:
                body = self.read_frame(self.incoming.read, GRAPH_LIMIT)
                if body is None or len(body) > GRAPH_LIMIT: raise GraphUnavailable("graph channel ended")
                message = json.loads(body, object_pairs_hook=self._unique)
                if not isinstance(message, dict): raise ValueError("graph body")
                with self.lock:
                    if self.ended: return
                    if message.get("type") == "arm_ack":
                        _arm_ack_bytes(message)
                        if self.arm_ack is None: raise ValueError("native ARM acknowledgement owner unavailable")
                    elif message.get("type") == "projection":
                        self._person_projection(message)
                    elif message.get("type") == "closed":
                        if (set(message) != {"type","generation","page","reason"}
                                or not _graph_token(message["generation"]) or not _graph_token(message["page"])
                                or message["reason"] not in ("workflow_loading","page_closed","native_closed","projection_exhausted","graph_expired")):
                            raise ValueError("graph closure")
                        if self.binding is None or self.binding[:2] == (message["generation"],message["page"]):
                            self._retire("exhausted" if message["reason"] == "projection_exhausted" else
                                         "expired" if message["reason"] == "graph_expired" else "closed",
                                         retire_page=message["reason"] in ("graph_expired","native_closed"))
                    elif message.get("type") == "result":
                        self._result(message)
                    elif message.get("type") == "notice":
                        if set(message) != {"type","reason"} or message["reason"] not in UNSIGNED_NOTES:
                            raise ValueError("graph notice")
                        self.note = UNSIGNED_NOTES[message["reason"]]
                    elif message.get("type") == "shutdown":
                        if (set(message) != {"type","generation","page"}
                                or not _graph_token(message["generation"]) or not _graph_token(message["page"])):
                            raise ValueError("graph shutdown shape")
                        # The sealed node owns the current authenticated page
                        # lease, including loading pages with no graph projection.
                        # This channel has exactly that held node as its writer.
                        self._retire("closing")
                        shutdown = self.shutdown
                    else:
                        raise ValueError("foreign graph operation")
                if message.get("type") == "shutdown" and shutdown is not None:
                    if shutdown() is not False: return
                if message.get("type") == "arm_ack":
                    self.arm_ack(message)
        except (OSError, ValueError, TypeError, GraphUnavailable, EOFError, RecursionError):
            with self.lock:
                self._retire("transport_failed")

    def _result(self, message):
        if (not {"type","id","generation","page","workflow","outcome"} <= set(message)
                or set(message) - {"type","id","generation","page","workflow","outcome","projection","answer"}
                or not _graph_token(message["id"])
                or message["outcome"] not in ("applied","stale","invalid","refused","unavailable","unknown")):
            raise ValueError("graph result shape")
        pending = self.pending
        if (pending is None or pending["done"].is_set() or self.context is None
                or self.binding != pending["binding"] or message["id"] != pending["id"]): return
        if tuple(message[name] for name in ("generation","page","workflow")) != pending["binding"]:
            raise ValueError("graph result binding")
        answer = message.get("answer")
        if answer is not None and not isinstance(answer, dict):
            raise ValueError("graph result answer")
        projection = message.get("projection")
        if message["outcome"] == "applied" and projection is not None:
            if not isinstance(projection, dict) or set(projection) != {"epoch","sequence","graph"} or projection["epoch"] != pending["binding"][2]:
                raise ValueError("graph result projection")
            update = dict(type="projection", generation=message["generation"], page=message["page"],
                          workflow=projection["epoch"], sequence=projection["sequence"], graph=projection["graph"])
            if not self._install(update, pending["call"]):
                return
        elif message["outcome"] in ("unavailable", "unknown"):
            # A stale answer only says the person's newer projection is on its
            # way; the canvas stays attached.
            self._retire("transport_failed", preserve_new=True, retire_page=True)
        pending["answer"] = (message["outcome"], answer or {})
        pending["done"].set()

    def revision(self):
        """The canvas's current revision: the graph context's, or "" with none."""
        return self.context.revision if self.context is not None else ""

    def _attached(self):
        """The current view, refusing plainly while no page is attached."""
        if self.ended or self.context is None or self.view is None:
            raise self.Refuse("unsupported", NO_PAGE)
        return self.view

    def _fence(self, call):
        """Refuse a call whose expected_revision names an older canvas."""
        expected = call.arguments.get("expected_revision", "")
        current = self.revision()
        if expected and expected != current:
            raise self.Refuse("conflict", "The canvas changed since revision %s; it is now %s. Read it again with "
                              "graph.read." % (expected[:64], current))

    def _request(self, operation, arguments, call=None, strict=True):
        """Send one operation to the page and answer (outcome, answer)."""
        with self.lock:
            self._attached()
            if self.pending is not None:
                raise self.Refuse("exhausted", "bound:calls_in_flight")
            if strict and self.held is not None:
                raise self.Refuse("conflict", "The person is changing the canvas; read it again with graph.read.")
            request = dict(type="request", id=secrets.token_urlsafe(18), operation=operation,
                           generation=self.binding[0], page=self.binding[1], workflow=self.binding[2],
                           sequence=self.sequence, arguments=arguments)
            pending = dict(id=request["id"], binding=self.binding, request=request, call=call,
                           done=threading.Event(), answer=None)
            self.pending = pending
        try:
            body = json.dumps(request, separators=(",", ":")).encode("utf-8")
            if len(body) > GRAPH_LIMIT: raise ValueError("graph request bound")
            self.write_frame(self.outgoing.write, body, GRAPH_LIMIT)
            self.outgoing.flush()
            if not pending["done"].wait(self.timeout): raise GraphUnavailable("graph response timeout")
            with self.lock:
                answer = pending["answer"]
            if answer is None or answer[0] in ("unavailable", "unknown"):
                raise GraphUnavailable("graph response unavailable")
            return answer
        except (OSError, ValueError, GraphUnavailable):
            with self.lock: self._retire("transport_failed", preserve_new=True, retire_page=True)
            raise GraphUnavailable("graph bridge unavailable") from None
        finally:
            with self.lock:
                if self.pending is pending: self.pending = None

    def _refused(self, outcome, answer, default):
        reason = answer.get("reason") if isinstance(answer.get("reason"), str) else ""
        if outcome == "stale":
            return self.Refuse("conflict", "The canvas changed on the page; read it again with graph.read.")
        if outcome == "invalid":
            return self.Refuse("invalid", (reason or default)[:512])
        return self.Refuse("unsupported", (reason or default)[:512])

    def read(self, call):
        with self.lock:
            view = _fit(self._attached())
            return dict(view, revision=self.revision())

    def choices(self, call):
        arguments = call.arguments
        with self.lock:
            node = _find(self._attached(), arguments["node"])
            widget = next((w for w in node["widgets"] if w["name"] == arguments["widget"]), None) if node else None
            if widget is None or widget["type"] != "combo":
                raise self.Refuse("invalid", "node %s has no combo widget %s" % (json.dumps(arguments["node"]),
                                                                                json.dumps(arguments["widget"])))
        try:
            outcome, answer = self._request("choices", {"node": arguments["node"], "widget": arguments["widget"],
                                                        "query": arguments.get("query", ""),
                                                        "cursor": arguments.get("cursor", "")}, strict=False)
        except GraphUnavailable:
            raise self.Refuse("unsupported", NO_PAGE) from None
        if outcome != "applied":
            raise self._refused(outcome, answer, "the choices could not be listed")
        rows = answer.get("choices") if isinstance(answer.get("choices"), list) else []
        cursor = answer.get("cursor") if isinstance(answer.get("cursor"), str) else ""
        total = answer.get("total") if type(answer.get("total")) is int else len(rows)
        return {"node": arguments["node"], "widget": arguments["widget"],
                "choices": [c if _number(c) else _text(c)[0] for c in rows[:MAX_CHOICES] if isinstance(c, str) or _number(c)],
                "cursor": cursor[:16], "total": total}

    def edit(self, call):
        ops = call.arguments["ops"]
        with self.lock:
            view = self._attached()
            self._fence(call)
            for index, op in enumerate(ops):
                reason = check_op(view, op, index)
                if reason:
                    raise self.Refuse("invalid", reason[:512])
        try:
            outcome, answer = self._request("edit", {"ops": ops}, call)
        except GraphUnavailable:
            raise self.Refuse("unknown", "internal") from None
        if outcome != "applied":
            raise self._refused(outcome, answer, "ComfyUI refused the edit")
        return {"applied": len(ops), "revision": self.revision()}

    def run(self, call):
        with self.lock:
            self._attached()
            self._fence(call)
        if self.follow is None:
            raise self.Refuse("unsupported", "This adapter follows no runs")
        try:
            outcome, answer = self._request("run", {}, call)
        except GraphUnavailable:
            raise self.Refuse("unknown", "internal") from None
        if outcome != "applied":
            raise self._refused(outcome, answer, "ComfyUI queued nothing")
        prompts = [p for p in answer.get("prompts", ()) if _graph_token(p)] if isinstance(answer.get("prompts"), list) else []
        if not prompts:
            raise self.Refuse("unsupported", "ComfyUI queued nothing; the canvas may have no output node")
        return self.follow(call, prompts, "graph")

    def retire_mediation(self):
        with self.lock:
            self.mediation = False
            self._retire("mediation_retired")

    def status_text(self):
        """The graph status app_read shows: the unsigned-Desktop sentence while
        no browser page is connected."""
        with self.lock:
            return self.note if self.note is not None and self.status != "connected" else self.status

    def reveal(self, context, entities, style):
        try:
            if len(entities) != 1: raise self.Refuse("unsupported","single_node")
            with self.lock:
                browser = context is not self.context and self.note is not None and self.show_page is not None
                if context is not self.context and not browser: raise GraphUnavailable("graph context unavailable")
            if browser:
                # An unsigned ComfyUI Desktop: the browser page carries the graph.
                if not self.show_page(): raise GraphUnavailable("browser page unavailable")
                return
            entity = entities[0]
            if not entity.startswith("node-") or self.view is None or _find(self.view, entity[5:]) is None:
                raise self.Refuse("not_found", "entity")
            outcome, answer = self._request("reveal", {"node": entity[5:]}, strict=False)
            if outcome == "refused":
                raise self.Refuse("unsupported", "canvas_in_subgraph")
            if outcome != "applied":
                raise self._refused(outcome, answer, "reveal")
        except (self.Refuse,GraphUnavailable) as error:
            error.native_revision=context.revision
            raise

    def close(self):
        with self.lock:
            self.ended = True
            self._retire("closed")
        self.incoming.close(); self.outgoing.close()
        if self.thread.ident is not None: self.thread.join()


# Agent-driven workflows through ComfyUI's own HTTP API on the listener the
# connector proved (/object_info, /userdata, /templates, /workflow_templates,
# /prompt, /history and /queue). The adapter converts a saved or bundled workflow graph into
# ComfyUI's prompt form, applies the agent's input values and follows the
# queued prompt as an AppModel job. Each output reaches the person's Pictures,
# Videos or Music folder under OpenAbstractions as a hard link, or one copy on
# another volume, and the job answers it as a reference (output_reference).

COMFY_HOST, COMFY_PORT = "127.0.0.1", 8188  # ComfyUI's default; the connector answers the served port
API_BYTES = 8 << 20
NODE_INFO_BYTES = 1 << 20
LIST_LIMIT = 100
OTHER_INPUTS_LIMIT = 150
CHOICES_LIMIT = 64
JOB_SECONDS = 3600
JOBS_KEPT = 64
# The node pack's route that delivers a workflow's model through OA's download
# service (adopter-comfyui oa_model_downloads.py).
MODEL_DOWNLOAD_ROUTE = "/openabstractions/models/download"
MODELS_LIMIT = 32
MODEL_STATES = ("present", "complete", "waiting", "unavailable", "failed", "refused")
MODEL_INPUT_CATEGORIES = {"unet_name": "diffusion_models", "ckpt_name": "checkpoints", "clip_name": "text_encoders",
                          "vae_name": "vae", "lora_name": "loras"}
# ComfyUI's folder_paths.map_legacy treats these two directory names as aliases.
LEGACY_MODEL_CATEGORIES = {"unet": "diffusion_models", "clip": "text_encoders"}
CONTROL_WORDS = ("fixed", "increment", "decrement", "randomize")
UPLOAD_WORDS = ("image", "video", "audio")
VIRTUAL_NODES = frozenset(("Note", "MarkdownNote", "Reroute", "PrimitiveNode"))
SEED_INPUTS = ("seed", "noise_seed")
WIDGET_TYPES = ("INT", "FLOAT", "STRING", "BOOLEAN", "COMBO")
ALIAS_INPUTS = {"seed": "seed", "noise_seed": "seed", "steps": "steps", "cfg": "cfg", "denoise": "denoise",
                "width": "width", "height": "height", "batch_size": "batch_size", "ckpt_name": "ckpt_name",
                "unet_name": "unet_name", "clip_name": "clip_name", "vae_name": "vae_name", "lora_name": "lora_name",
                # The OA Image Generate node's own inputs.
                "prompt": "prompt", "size": "size"}
_TEMPLATE = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_PROMPT_NAMESPACE = uuid.UUID("6f0e3c2a-8d4b-4c47-9a3e-0a1b2c3d4e5f")


class ComfyRefusal(Exception):
    """One application outcome word and its bounded reason."""
    def __init__(self, outcome, reason):
        super().__init__(reason)
        self.outcome, self.reason = outcome, reason[:512]


def _log(line):
    """One line on this adapter's standard error, which the connector keeps; stdout carries MCP."""
    try:
        sys.stderr.write("oa_native_host: " + line.replace("\n", " ") + "\n")
        sys.stderr.flush()
    except (OSError, ValueError, AttributeError):
        pass


def _comfy_reason(value):
    if isinstance(value, dict):
        parts = [str(value.get(name, "")) for name in ("type", "message", "details") if value.get(name)]
        return ": ".join(parts) or "error"
    return str(value)


class ComfyAPI:
    """ComfyUI's own API on its loopback listener. No proxy, no other host.

    port is the port, or a callable answering the served ComfyUI's port at
    each request. connect, when given, makes the connection with no
    argument (tests)."""
    def __init__(self, running=lambda: True, timeout=30, connect=None, port=COMFY_PORT):
        if not callable(port) and (type(port) is not int or not 0 < port < 65536):
            raise ValueError("backend port")
        self.running, self.timeout, self.port, self.connect = running, timeout, port, connect

    def current_port(self):
        port = self.port() if callable(self.port) else self.port
        if type(port) is not int or not 0 < port < 65536:
            raise ComfyRefusal("unsupported", "backend:not_running")
        return port

    def request(self, method, path, body=None, limit=API_BYTES, same_origin=False):
        """same_origin sends ComfyUI's own origin, which the node pack's routes require."""
        if not self.running():
            raise ComfyRefusal("unsupported", "backend:not_running")
        port = self.current_port()
        headers = {"Host": "%s:%d" % (COMFY_HOST, port), "Accept": "application/json"}
        if same_origin:
            headers["Origin"] = "http://%s:%d" % (COMFY_HOST, port)
        raw = None
        if body is not None:
            raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection = (self.connect() if self.connect is not None
                      else http.client.HTTPConnection(COMFY_HOST, port, timeout=self.timeout))
        try:
            connection.request(method, path, body=raw, headers=headers)
            response = connection.getresponse()
            data = response.read(limit + 1)
            if len(data) > limit:
                raise ComfyRefusal("exhausted", "bound:comfyui_answer")
            return response.status, data
        except OSError:
            raise ComfyRefusal("unsupported", "backend:unreachable") from None
        finally:
            connection.close()

    def json(self, method, path, body=None, limit=API_BYTES, missing=None, same_origin=False):
        status, data = self.request(method, path, body, limit, same_origin)
        if status == 404 and missing is not None:
            raise ComfyRefusal("not_found", missing)
        try:
            value = json.loads(data) if data else None
        except ValueError:
            value = None
        if status == 400 and isinstance(value, dict):
            return status, value
        if status != 200 or value is None:
            raise ComfyRefusal("unsupported", "backend:http_%d" % status)
        return status, value


# A command that arrives while ComfyUI starts waits this long for it, the
# MCP gateway's default follow time, then answers that ComfyUI is starting.
BACKEND_WAIT_SECONDS = 20
# An open waits this long for ComfyUI to serve before it answers not ready.
# The whole open, the connector's start included, has the manifest's 30 s.
OPEN_READY_SECONDS = 15
# What an agent can do about each backend phase that does not serve commands.
BACKEND_REASONS = {
    "idle": "backend:not_started: call oa_applications_open for comfyui",
    "verifying": "backend:starting: ComfyUI is still starting; the first start can take a few minutes. "
                 "Call the command again",
    "desktop_idle": "backend:desktop_idle: Comfy Desktop is open, but no ComfyUI install runs in it. "
                    "Ask the person to start ComfyUI in Comfy Desktop, then call the command again",
    "launching": "backend:starting: ComfyUI's server is launching. Call the command again",
    "loading_custom_nodes": "backend:starting: ComfyUI's server is loading its custom nodes. Call the command again",
    "listening": "backend:starting: ComfyUI's server listens; OpenAbstractions is connecting to it. Call the command again",
    "already_running": "backend:starting: ComfyUI is starting in Comfy Desktop. Call the command again",
    "failed": "backend:failed: ComfyUI is not served. Call oa_applications_open for comfyui to try again",
    "exited": "backend:exited: ComfyUI stopped. Call oa_applications_open for comfyui to start it again",
    "exit_unknown": "backend:exit_unknown: ComfyUI's end could not be confirmed",
}


# The phases in which ComfyUI does not serve yet and is on its way.
STARTING_PHASES = ("verifying", "launching", "loading_custom_nodes", "listening", "already_running")


def backend_ready(state, ended, wait=BACKEND_WAIT_SECONDS, clock=time.monotonic):
    """True when ComfyUI serves commands; a starting backend is awaited up to wait seconds.

    Otherwise raises the phase's refusal. browser_failed serves commands: only
    the browser page failed to open.
    """
    began = clock()
    deadline = began + wait
    while True:
        phase = state["phase"]
        if phase in ("running", "browser_failed"):
            return True
        # An open's start reaches the polled phase within a poll interval.
        # Comfy Desktop without a running install waits for the person.
        starting = phase in STARTING_PHASES or phase == "idle" and clock() < began + 1
        if not starting or ended.is_set() or clock() >= deadline:
            reason = BACKEND_REASONS.get(phase, "backend:not_running")
            if phase == "failed" and state.get("cause"):
                reason = "backend:failed: " + state["cause"] + ". Call oa_applications_open for comfyui to start it again"
            raise ComfyRefusal("unsupported", reason)
        ended.wait(0.25)


def _quote(path):
    return urllib.parse.quote(path, safe="")


def _plain_template(name):
    return bool(_TEMPLATE.fullmatch(name)) and not name.startswith(".")


def _workflow_name(name):
    """template:<name>, template:<custom node>/<name>, or user:<path below the person's workflows folder>."""
    source, _, rest = name.partition(":")
    if source == "template" and (_plain_template(rest) or (
            rest.count("/") == 1 and all(_plain_template(part) for part in rest.split("/")))):
        return source, rest
    if source == "user" and 0 < len(rest) <= 256 and rest.endswith(".json") and "\\" not in rest and "%" not in rest:
        parts = rest.split("/")
        if all(part and not re.fullmatch(r"\.{1,2}", part) for part in parts):
            return source, rest
    raise ComfyRefusal("invalid", "workflow")


def list_workflows(api, search="", source="", limit=LIST_LIMIT):
    """Saved workflows first, then the bundled local templates; cloud API templates are left out."""
    words = [word for word in search.casefold().split() if word]
    found = []
    if source in ("", "user"):
        status, data = api.request("GET", "/api/userdata?dir=workflows&recurse=true")
        if status == 200:
            paths = json.loads(data)
            for path in sorted(p for p in paths if isinstance(p, str) and p.endswith(".json")):
                found.append({"name": "user:" + path, "title": path[:-5], "source": "user"})
        elif status != 404:
            raise ComfyRefusal("unsupported", "backend:http_%d" % status)
    if source in ("", "template"):
        found.extend(custom_node_templates(api))
        status, data = api.request("GET", "/templates/index.json")
        if status == 200:
            for category in json.loads(data):
                for item in category.get("templates", ()) if isinstance(category, dict) else ():
                    name = item.get("name") if isinstance(item, dict) else None
                    if (not isinstance(name, str) or not _TEMPLATE.fullmatch(name) or name.startswith("api_")
                            or item.get("openSource") is False):
                        continue
                    models = item.get("models") if isinstance(item.get("models"), list) else []
                    found.append({"name": "template:" + name, "title": str(item.get("title", name))[:200],
                                  "source": "template", "category": str(category.get("title", ""))[:64],
                                  "media": str(item.get("mediaType", ""))[:16],
                                  "description": str(item.get("description", ""))[:300],
                                  "models": [str(m)[:64] for m in models[:4]]})
        elif status != 404:
            raise ComfyRefusal("unsupported", "backend:http_%d" % status)
    if words:
        found = [row for row in found if all(word in " ".join(
            (row["name"], row["title"], row.get("category", ""), row.get("description", ""),
             " ".join(row.get("models", ())))).casefold()
            for word in words)]
    return {"workflows": found[:limit], "total": len(found)}


def custom_node_templates(api):
    """The example workflows custom nodes ship, as ComfyUI's /api/workflow_templates lists them.

    The OpenAbstractions node pack ships its picture recipe there: OA Image
    Generate renders through OA's picture engine. They come before the bundled
    templates so the list limit never cuts them. A listing ComfyUI cannot
    answer leaves them out.
    """
    try:
        status, data = api.request("GET", "/api/workflow_templates")
        listed = json.loads(data) if status == 200 else {}
    except (ComfyRefusal, ValueError):
        return []
    rows = []
    for module, names in sorted(listed.items()) if isinstance(listed, dict) else ():
        if not isinstance(module, str) or not _plain_template(module) or not isinstance(names, list):
            continue
        for name in sorted(n for n in names if isinstance(n, str) and _plain_template(n)):
            rows.append({"name": "template:%s/%s" % (module, name), "title": name.replace("_", " ")[:200],
                         "source": "template", "category": module[:64]})
    return rows


def load_workflow(api, name):
    source, rest = _workflow_name(name)
    if source == "template" and "/" in rest:
        module, file = rest.split("/")
        path = "/api/workflow_templates/%s/%s.json" % (_quote(module), _quote(file))
    elif source == "template":
        path = "/templates/%s.json" % rest
    else:
        path = "/api/userdata/" + _quote("workflows/" + rest)
    status, data = api.request("GET", path)
    if status == 404:
        raise ComfyRefusal("not_found", "workflow")
    if status != 200:
        raise ComfyRefusal("unsupported", "backend:http_%d" % status)
    try:
        value = json.loads(data)
    except ValueError:
        raise ComfyRefusal("invalid", "workflow:json") from None
    if not isinstance(value, dict):
        raise ComfyRefusal("invalid", "workflow:json")
    return value


def workflow_models(workflow):
    """The models a workflow lists, as ComfyUI's frontend reads them: the workflow's own
    "models" and every node's properties.models, subgraph definitions included.

    Each is {url, name, directory} with the hash and hash_type a template gives, once per
    folder and name.
    """
    found, seen = [], set()

    def add(items):
        for model in items if isinstance(items, list) else ():
            if not isinstance(model, dict):
                continue
            row = {key: model.get(key) for key in ("url", "name", "directory")}
            if not all(isinstance(value, str) and value for value in row.values()):
                continue
            if (row["directory"], row["name"]) in seen:
                continue
            seen.add((row["directory"], row["name"]))
            if isinstance(model.get("hash"), str) and isinstance(model.get("hash_type"), str):
                row["hash"], row["hash_type"] = model["hash"], model["hash_type"]
            found.append(row)

    def nodes(items):
        for node in items if isinstance(items, list) else ():
            if isinstance(node, dict) and isinstance(node.get("properties"), dict):
                add(node["properties"].get("models"))

    add(workflow.get("models"))
    nodes(workflow.get("nodes"))
    definitions = workflow.get("definitions")
    for subgraph in (definitions.get("subgraphs") or ()) if isinstance(definitions, dict) else ():
        if isinstance(subgraph, dict):
            nodes(subgraph.get("nodes"))
    return found[:MODELS_LIMIT]


class NodeInfo:
    """object_info per node class, read once per command call."""
    def __init__(self, api):
        self.api, self.cache = api, {}

    def __call__(self, cls):
        if cls not in self.cache:
            status, data = self.api.request("GET", "/api/object_info/" + _quote(cls), limit=NODE_INFO_BYTES)
            value = json.loads(data) if status == 200 and data else {}
            self.cache[cls] = value.get(cls) if isinstance(value, dict) else None
        return self.cache[cls]


def _widget_inputs(info):
    """The widget inputs of one node class in the frontend's widget order."""
    rows = []
    inputs = info.get("input", {}) or {}
    order = info.get("input_order") or {}
    for group in ("required", "optional"):
        specs = inputs.get(group, {}) or {}
        for name in order.get(group) or list(specs):
            spec = specs.get(name)
            if not isinstance(spec, (list, tuple)) or not spec:
                continue
            kind, options = spec[0], (spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {})
            if options.get("forceInput"):
                continue
            if isinstance(kind, list):
                rows.append((name, "COMBO", options, list(kind)))
            elif kind in WIDGET_TYPES:
                rows.append((name, kind, options, list(options.get("options", ())) if kind == "COMBO" else None))
    return rows


class WorkflowGraph:
    """A workflow converted into ComfyUI's prompt form with its settable inputs."""
    def __init__(self, workflow, info):
        self.prompt, self.widgets, self.friendly, self.outputs = {}, {}, {}, []
        self.controls = {}
        if workflow and all(isinstance(v, dict) and "class_type" in v for v in workflow.values()):
            self._from_prompt(workflow, info)
        elif isinstance(workflow.get("nodes"), list):
            self._from_graph(workflow, info)
        else:
            raise ComfyRefusal("invalid", "workflow:format")
        if not self.prompt:
            raise ComfyRefusal("invalid", "workflow:empty")
        self._aliases()

    # Prompt (API) form: literal inputs are the widgets.
    def _from_prompt(self, workflow, info):
        for node_id, node in workflow.items():
            cls = node.get("class_type")
            spec = info(cls) if isinstance(cls, str) else None
            if spec is None:
                raise ComfyRefusal("unsupported", "node:%s" % cls)
            inputs = dict(node.get("inputs") or {})
            title = str((node.get("_meta") or {}).get("title", cls))
            self.prompt[str(node_id)] = {"class_type": cls, "inputs": inputs,
                                         "_meta": {"title": title}}
            for name, kind, options, choices in _widget_inputs(spec):
                if name in inputs and not isinstance(inputs[name], list):
                    self._widget(str(node_id), name, kind, options, choices, inputs[name], cls, title)
            if spec.get("output_node"):
                self.outputs.append({"node": str(node_id), "class": cls})

    # Graph (UI) form, subgraphs flattened the way the frontend queues them.
    def _from_graph(self, workflow, info):
        self.info = info
        self.subgraphs = {g.get("id"): g for g in (workflow.get("definitions") or {}).get("subgraphs", ()) if isinstance(g, dict)}
        self.scopes = {}
        top = self._scope(workflow, "", None, None)
        self._walk(top, 0)
        self.top = top

    def _scope(self, graph, prefix, parent, instance):
        links = {}
        for link in graph.get("links") or ():
            if isinstance(link, list) and len(link) >= 5:
                links[link[0]] = tuple(link[1:5])
            elif isinstance(link, dict):
                links[link.get("id")] = (link.get("origin_id"), link.get("origin_slot"), link.get("target_id"), link.get("target_slot"))
        scope = {"graph": graph, "prefix": prefix, "parent": parent, "instance": instance, "links": links,
                 "nodes": {n.get("id"): n for n in graph.get("nodes") or () if isinstance(n, dict)}}
        self.scopes[prefix] = scope
        return scope

    def _child(self, scope, node):
        prefix = scope["prefix"] + str(node["id"]) + ":"
        if prefix not in self.scopes:
            if prefix.count(":") > 8:
                raise ComfyRefusal("unsupported", "workflow:subgraph_depth")
            self._scope(self.subgraphs[node["type"]], prefix, scope, node)
        return self.scopes[prefix]

    def _walk(self, scope, depth):
        for node in scope["nodes"].values():
            kind, mode = node.get("type"), node.get("mode", 0)
            if mode in (2, 4) or kind in VIRTUAL_NODES:
                continue
            if kind in self.subgraphs:
                self._walk(self._child(scope, node), depth + 1)
            else:
                self._emit(scope, node)

    def _link(self, scope, link):
        if link is None or link not in scope["links"]:
            return None
        origin, slot, _, _ = scope["links"][link]
        return self._output(scope, origin, slot, 0)

    def _output(self, scope, node_id, slot, depth):
        if depth > 64:
            raise ComfyRefusal("unsupported", "workflow:link_depth")
        if node_id == -10:
            # A subgraph input: the instance's own input of that name, when linked outside.
            definition = scope["graph"].get("inputs") or ()
            if scope["instance"] is None or not isinstance(slot, int) or slot >= len(definition):
                return None
            name = definition[slot].get("name")
            for item in scope["instance"].get("inputs") or ():
                if item.get("name") == name and item.get("link") is not None:
                    return self._link(scope["parent"], item["link"])
            return None
        node = scope["nodes"].get(node_id)
        if node is None:
            return None
        kind, mode = node.get("type"), node.get("mode", 0)
        if mode == 2:
            return None
        if kind in self.subgraphs:
            child = self._child(scope, node)
            for origin, oslot, target, tslot in child["links"].values():
                if target == -20 and tslot == slot:
                    return self._output(child, origin, oslot, depth + 1)
            return None
        if kind == "Reroute" or mode == 4:
            wanted = None
            if mode == 4:
                outputs = node.get("outputs") or ()
                wanted = outputs[slot].get("type") if isinstance(slot, int) and slot < len(outputs) else None
            for item in node.get("inputs") or ():
                if item.get("link") is not None and (wanted is None or item.get("type") == wanted):
                    return self._link(scope, item["link"])
            return None
        if kind == "PrimitiveNode":
            values = node.get("widgets_values") or [None]
            return ("value", values[0])
        if kind in VIRTUAL_NODES:
            return None
        return ("ref", [scope["prefix"] + str(node_id), slot])

    def _emit(self, scope, node):
        cls = node.get("type")
        spec = self.info(cls) if isinstance(cls, str) else None
        if spec is None:
            raise ComfyRefusal("unsupported", "node:%s" % cls)
        api_id = scope["prefix"] + str(node["id"])
        values, inputs = node.get("widgets_values"), {}
        index, widgets = 0, []
        for name, kind, options, choices in _widget_inputs(spec):
            if isinstance(values, dict):
                if name in values:
                    widgets.append((name, kind, options, choices, values[name]))
                continue
            if not isinstance(values, list) or index >= len(values):
                break
            value = values[index]
            index += 1
            widgets.append((name, kind, options, choices, value))
            following = values[index] if index < len(values) else None
            if kind == "INT" and following in CONTROL_WORDS and (options.get("control_after_generate") or name in SEED_INPUTS):
                self.controls[(api_id, name)] = following
                index += 1
            elif any(options.get(key) for key in ("image_upload", "video_upload", "audio_upload")) and following in UPLOAD_WORDS:
                index += 1
        for name, kind, options, choices, value in widgets:
            inputs[name] = value
        linked = set()
        for item in node.get("inputs") or ():
            if item.get("link") is None:
                continue
            target = self._link(scope, item["link"])
            if target is None:
                continue
            linked.add(item.get("name"))
            inputs[item.get("name")] = target[1]
        title = str(node.get("title") or spec.get("display_name") or cls)[:128]
        self.prompt[api_id] = {"class_type": cls, "inputs": inputs, "_meta": {"title": title}}
        for name, kind, options, choices, value in widgets:
            if name not in linked:
                self._widget(api_id, name, kind, options, choices, value, cls, title)
        if spec.get("output_node"):
            self.outputs.append({"node": api_id, "class": cls})

    def _widget(self, api_id, name, kind, options, choices, value, cls, title=None):
        self.widgets[api_id + "." + name] = {"targets": [(api_id, name)], "type": kind, "options": options,
                                             "choices": choices, "value": value, "class": cls,
                                             "title": title or cls}

    def _aliases(self):
        # Subgraph inputs the workflow's author exposed come first, by their label.
        for scope in getattr(self, "scopes", {}).values():
            instance = scope["instance"]
            if instance is None or scope["parent"] is not getattr(self, "top", None):
                continue
            outer = {item.get("name"): item for item in instance.get("inputs") or ()}
            for slot, definition in enumerate(scope["graph"].get("inputs") or ()):
                item = outer.get(definition.get("name"))
                if item is not None and item.get("link") is not None:
                    continue
                targets = []
                for origin, oslot, target, tslot in scope["links"].values():
                    if origin == -10 and oslot == slot:
                        node = scope["nodes"].get(target)
                        rows = node.get("inputs") or () if node else ()
                        if isinstance(tslot, int) and tslot < len(rows):
                            key = scope["prefix"] + str(target) + "." + rows[tslot].get("name", "")
                            if key in self.widgets:
                                targets.append(key)
                label = str(definition.get("label") or definition.get("name") or "")[:64]
                if targets and label:
                    name = label if label not in self.friendly else "%s@%s" % (label, instance.get("id"))
                    self.friendly[name] = targets
        # Then the usual inputs, when exactly one widget carries them.
        taken = {key for keys in self.friendly.values() for key in keys}
        for name in sorted(set(ALIAS_INPUTS.values())):
            keys = [key for key in self.widgets if ALIAS_INPUTS.get(key.rsplit(".", 1)[1]) == name and key not in taken]
            if len(keys) == 1 and name not in self.friendly:
                self.friendly[name] = keys
        if "prompt" not in self.friendly:
            for wanted, alias in (("positive", "prompt"), ("negative", "negative_prompt")):
                keys = set()
                for node in self.prompt.values():
                    source = node["inputs"].get(wanted)
                    if isinstance(source, list) and len(source) == 2:
                        key = str(source[0]) + ".text"
                        if key in self.widgets and self.widgets[key]["type"] == "STRING" and key not in taken:
                            keys.add(key)
                if len(keys) == 1:
                    self.friendly[alias] = sorted(keys)
        if "prompt" not in self.friendly:
            # The Flux2 Klein template labels the text primitive "Prompt".
            # One source can feed several encoders, while multiple named
            # sources stay addressable by node.
            sources = set()
            for node in self.prompt.values():
                source = node["inputs"].get("text")
                if node["class_type"] != "CLIPTextEncode" or not isinstance(source, list) or len(source) != 2:
                    continue
                key = str(source[0]) + ".value"
                widget = self.widgets.get(key)
                if (widget and key not in taken and widget["type"] == "STRING" and
                        re.fullmatch("prompt", widget["title"].strip(), re.IGNORECASE | re.ASCII) and
                        widget["class"] in ("PrimitiveString", "PrimitiveStringMultiline")):
                    sources.add(key)
            if len(sources) == 1:
                self.friendly["prompt"] = sorted(sources)

    def describe(self, name):
        def row(label, key):
            widget = self.widgets[key]
            entry = {"name": label, "type": widget["type"], "value": widget["value"]}
            for bound in ("min", "max"):
                if isinstance(widget["options"].get(bound), (int, float)):
                    entry[bound] = widget["options"][bound]
            if widget["choices"]:
                if len(widget["choices"]) <= CHOICES_LIMIT:
                    entry["choices"] = widget["choices"]
                entry["available"] = widget["value"] in widget["choices"]
            if self.controls.get(widget["targets"][0]) == "randomize":
                entry["randomized_when_unset"] = True
            return entry
        friendly = [row(label, keys[0]) for label, keys in self.friendly.items()]
        named = {key for keys in self.friendly.values() for key in keys}
        other = [dict(row(key, key), node=self.widgets[key]["title"]) for key in self.widgets if key not in named]
        return {"workflow": name, "inputs": friendly, "other_inputs": other[:OTHER_INPUTS_LIMIT],
                "other_inputs_total": len(other), "outputs": self.outputs}

    def apply(self, overrides, random_seed):
        if not isinstance(overrides, dict):
            raise ComfyRefusal("invalid", "inputs")
        prompt = json.loads(json.dumps(self.prompt))
        set_keys, used = set(), {}
        for label, value in overrides.items():
            keys = self.friendly.get(label) or ([label] if label in self.widgets else None)
            if keys is None:
                raise ComfyRefusal("invalid", "input:%s" % label[:64])
            for key in keys:
                widget = self.widgets[key]
                if not _accepts(widget, value):
                    raise ComfyRefusal("invalid", "input:%s" % label[:64])
                for node_id, input_name in widget["targets"]:
                    prompt[node_id]["inputs"][input_name] = value
                set_keys.add(key)
            used[label] = value
        for (node_id, input_name), control in self.controls.items():
            key = node_id + "." + input_name
            if control == "randomize" and key not in set_keys and key in self.widgets:
                value = random_seed(self.widgets[key]["options"])
                prompt[node_id]["inputs"][input_name] = value
                label = next((name for name, keys in self.friendly.items() if key in keys), key)
                used[label] = value
        return prompt, used


def _accepts(widget, value):
    kind, options, choices = widget["type"], widget["options"], widget["choices"]
    if kind == "INT":
        ok = type(value) is int
    elif kind == "FLOAT":
        ok = type(value) in (int, float)
    elif kind == "BOOLEAN":
        ok = type(value) is bool
    elif kind == "STRING":
        ok = isinstance(value, str)
    else:
        ok = isinstance(value, str) and value in (choices or ())
    if ok and kind in ("INT", "FLOAT"):
        low, high = options.get("min"), options.get("max")
        ok = (not isinstance(low, (int, float)) or value >= low) and (not isinstance(high, (int, float)) or value <= high)
    return ok


def _random_seed(options):
    high = options.get("max")
    high = min(high, 2 ** 53) if isinstance(high, int) and high > 0 else 2 ** 32
    low = options.get("min") if isinstance(options.get("min"), int) and options.get("min") >= 0 else 0
    return low + secrets.randbelow(max(1, high - low))


def _name_component(name):
    # Syntax only. Admission below asks the filesystem about open objects.
    return bool(name.strip(".")) and not name.endswith((".", " ")) and not any(c in name for c in "/\\:\0")


def _native_component(name):
    if os.name == "nt":
        return _name_component(name)
    # A POSIX component has no drive or stream syntax. Only the one- and
    # two-period components navigate to a directory.
    return (len(name) > 2 or bool(name.strip("."))) and not any(c in name for c in "/\0")


def _open_stat(path, leaf, parent, stop_at_link):
    if os.name == "nt":
        import _winapi
        import msvcrt
        # Read data/attributes, share read/write, open the reparse point itself.
        # Denying delete sharing keeps each checked ancestor in its namespace.
        handle = _winapi.CreateFile(os.fspath(path), 0x81, 3, 0, 3, 0x02200000, 0)
        try:
            return msvcrt.open_osfhandle(handle, os.O_RDONLY)
        except OSError:
            _winapi.CloseHandle(handle)
            raise
    # O_PATH opens a Linux link itself and keeps its inode available for
    # the read-side proof. The ordinary walk still refuses every link.
    flags = os.O_RDONLY | os.O_NONBLOCK
    if stop_at_link:
        flags = getattr(os, "O_PATH", 0) or flags
    return os.open(leaf, flags | os.O_NOFOLLOW, dir_fd=parent)


@contextmanager
def _held_walk(path, *, stop_at_link=False, admit_parent=None):
    name = Path(path)
    parts = name.parts
    if not name.is_absolute() or not all(_native_component(part) for part in parts[1:]):
        raise ValueError("path must use ordinary absolute names")
    at, parent, facts = Path(parts[0]), None, []
    with ExitStack() as handles:
        for index, part in enumerate(parts):
            if index:
                at = at / part
            if index + 1 == len(parts) and admit_parent is not None:
                admit_parent(facts)
            try:
                current = _open_stat(at, part, parent, stop_at_link)
            except OSError as error:
                if not stop_at_link or os.name == "nt" or error.errno != errno.ELOOP:
                    raise
                # POSIX without O_PATH (including macOS): observe the link
                # relative to its held parent and recheck it after resolution.
                info = os.stat(part, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISLNK(info.st_mode):
                    raise ValueError("link changed") from error
            else:
                handles.callback(os.close, current)
                parent = current
                info = os.fstat(current)
            facts.append(info)
            if (stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
                if not stop_at_link:
                    raise ValueError("path crosses a link")
                if os.name == "nt":
                    # The raw Windows link handle denies replacement. Admit
                    # only the filesystem's symlink and junction tags.
                    linked = os.lstat(at)
                    if (not os.path.samestat(info, linked) or linked.st_reparse_tag not in
                            (stat.IO_REPARSE_TAG_MOUNT_POINT, stat.IO_REPARSE_TAG_SYMLINK)):
                        raise ValueError("path crosses an unverified reparse point")
                yield facts, at, None
                return
            if index + 1 < len(parts) and not stat.S_ISDIR(info.st_mode):
                raise ValueError("path crosses a non-directory")
        yield facts, None, parent


@contextmanager
def _output_file(path, root=None):
    def admit(ancestors):
        if root is not None and not any(os.path.samestat(root, item) for item in ancestors):
            raise ValueError("output is outside the held folder")

    with _held_walk(path, admit_parent=admit) as (facts, _, fd):
        if not stat.S_ISREG(facts[-1].st_mode):
            raise ValueError("output is not a regular file")
        with os.fdopen(os.dup(fd), "rb") as stream:
            yield stream, facts


def _held_outputs(entry, output_root):
    if not output_root:
        return
    root = os.path.realpath(output_root)
    seen = set()
    try:
        with _held_walk(root) as (root_facts, _, _):
            if not stat.S_ISDIR(root_facts[-1].st_mode):
                return
            for node_id, output in sorted((entry.get("outputs") or {}).items()):
                for items in (output.values() if isinstance(output, dict) else ()):
                    for item in items if isinstance(items, list) else ():
                        if not isinstance(item, dict) or item.get("type") != "output" or not item.get("filename"):
                            continue
                        name = Path(str(item.get("subfolder") or "")) / str(item["filename"])
                        if name.anchor or not all(_native_component(part) for part in name.parts):
                            continue
                        path = os.path.realpath(os.path.join(root, name))
                        try:
                            with _output_file(path, root_facts[-1]) as (stream, facts):
                                if path in seen:
                                    continue
                                seen.add(path)
                                yield str(node_id), path, stream, facts
                        except (OSError, ValueError):
                            continue
                        if len(seen) == OUTPUTS_LIMIT:
                            return
    except (OSError, ValueError):
        return


def output_files(entry, output_root):
    """The saved output names in node order."""
    return [(node, path) for node, path, _, _ in _held_outputs(entry, output_root)]


# Each output's kind and media type by its extension; its kind names the
# person's folder that receives it.
MEDIA = {".png": ("image", "image/png"), ".jpg": ("image", "image/jpeg"), ".jpeg": ("image", "image/jpeg"),
         ".webp": ("image", "image/webp"), ".gif": ("image", "image/gif"), ".bmp": ("image", "image/bmp"),
         ".tif": ("image", "image/tiff"), ".tiff": ("image", "image/tiff"),
         ".mp4": ("video", "video/mp4"), ".webm": ("video", "video/webm"), ".mov": ("video", "video/quicktime"),
         ".mkv": ("video", "video/x-matroska"), ".avi": ("video", "video/x-msvideo"),
         ".flac": ("audio", "audio/flac"), ".wav": ("audio", "audio/wav"), ".mp3": ("audio", "audio/mpeg"),
         ".ogg": ("audio", "audio/ogg"), ".opus": ("audio", "audio/opus"), ".m4a": ("audio", "audio/mp4")}
MEDIA_SUFFIXES = tuple((re.compile(re.escape(ext), re.IGNORECASE | re.ASCII), media) for ext, media in MEDIA.items())
OUTPUTS_LIMIT = 64
# The Windows known folders of each kind, and their usual names elsewhere.
KNOWN_FOLDERS = {"image": ("{33E28130-4E1E-4676-835A-98395C3BC3BB}", "Pictures"),
                 "video": ("{18989B1D-99B5-455B-841C-AB7C74E4DDFC}", "Videos"),
                 "audio": ("{4BD8D571-6D19-48D3-BE97-422220080E43}", "Music")}


def known_folder(kind):
    """The person's Pictures, Videos or Music folder for kind, or ""."""
    if kind not in KNOWN_FOLDERS:
        return ""
    folder_id, name = KNOWN_FOLDERS[kind]
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD), ("d3", wintypes.WORD),
                            ("d4", ctypes.c_ubyte * 8)]
            value = uuid.UUID(folder_id)
            guid = GUID(value.time_low, value.time_mid, value.time_hi_version,
                        (ctypes.c_ubyte * 8).from_buffer_copy(value.bytes[8:]))
            shell32, ole32 = ctypes.WinDLL("shell32"), ctypes.WinDLL("ole32")
            shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
                                                     ctypes.POINTER(ctypes.c_void_p)]
            shell32.SHGetKnownFolderPath.restype = ctypes.c_long
            ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
            path = ctypes.c_void_p()
            if shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path)) == 0:
                try:
                    return ctypes.wstring_at(path.value)
                finally:
                    ole32.CoTaskMemFree(path)
        except (OSError, AttributeError, ValueError) as error:
            _log("known folder %s: %s" % (name, error))
    home = os.path.expanduser("~")
    return os.path.join(home, name) if os.path.isabs(home) else ""


def _digest(stream, writer=None):
    stream.seek(0)
    hasher, size = hashlib.sha256(), 0
    for block in iter(lambda: stream.read(1 << 20), b""):
        hasher.update(block)
        size += len(block)
        if writer is not None:
            writer.write(block)
    return "sha256:" + hasher.hexdigest(), size


def _place(source, reader, source_facts, digest, folder, folder_fact, folder_fd):
    """Reuse an output already in the held folder or place its exact bytes there."""
    if any(os.path.samestat(folder_fact, ancestor) for ancestor in source_facts[:-1]):
        _check_placement(source, folder_fact, digest, expected=source_facts[-1])
        return source
    stem, ext = os.path.splitext(os.path.basename(source))
    for n in range(1, 101):
        leaf = stem + ext if n == 1 else "%s-%d%s" % (stem, n, ext)
        target = os.path.join(folder, leaf)
        try:
            if _check_placement(target, folder_fact, digest):
                return target
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            continue
        else:
            continue
        if os.name == "nt" or sys.platform.startswith("linux"):
            try:
                if os.name == "nt":
                    os.link(source, target)
                else:
                    os.link("/proc/self/fd/%d" % reader.fileno(), leaf, dst_dir_fd=folder_fd, follow_symlinks=True)
            except FileExistsError:
                continue
            except OSError:
                pass
            else:
                _check_placement(target, folder_fact, digest, expected=source_facts[-1])
                return target
        # Where the platform cannot link the held file, stage and verify its
        # bytes before claiming a name in the person's folder.
        try:
            with tempfile.TemporaryFile() as staged:
                if _digest(reader, staged)[0] != digest:
                    raise OSError("ComfyUI's output changed while being copied")
                staged.seek(0)
                if os.name == "nt":
                    writer = open(target, "xb")
                else:
                    fd = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666, dir_fd=folder_fd)
                    writer = os.fdopen(fd, "wb")
                with writer:
                    shutil.copyfileobj(staged, writer, 1 << 20)
                    written = os.fstat(writer.fileno())
        except FileExistsError:
            continue
        _check_placement(target, folder_fact, digest, expected=written)
        return target
    raise OSError("100 names are taken in %s" % folder)


def _check_placement(path, folder, digest, *, expected=None):
    with _output_file(path, folder) as (stream, facts):
        before = facts[-1]
        if expected is not None and not os.path.samestat(before, expected):
            raise OSError("the exported name changed")
        observed, size = _digest(stream)
        after = os.fstat(stream.fileno())
        snapshot = (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        unchanged = (before.st_size, before.st_mtime_ns, before.st_ctime_ns) == snapshot
        if not unchanged or size != after.st_size or observed != digest:
            if expected is not None:
                raise OSError("the exported file's bytes changed")
            return False
        if os.name != "nt":
            # A POSIX hold keeps the inode alive, even if a name above it moves.
            with _output_file(path, folder) as (_, current):
                named = current[-1]
                if not os.path.samestat(before, named):
                    raise OSError("the exported name changed")
                if (named.st_size, named.st_mtime_ns, named.st_ctime_ns) != snapshot:
                    raise OSError("the exported file's bytes changed")
        return True


def output_reference(node, path, folders=known_folder):
    real = os.path.realpath(path)
    with _output_file(real) as (stream, facts):
        return _output_reference(node, real, stream, facts, folders)


def _output_reference(node, path, stream, source_facts, folders):
    """One output as an OA reference: kind, name, digest, size, media type and
    its path under the person's Pictures, Videos or Music folder. An output
    that cannot be placed keeps its reference and says why in note."""
    suffix = os.path.splitext(path)[1]
    kind, media = next((media for pattern, media in MEDIA_SUFFIXES if pattern.fullmatch(suffix)),
                       ("file", "application/octet-stream"))
    digest, size = _digest(stream)
    row = {"kind": kind, "name": os.path.basename(path), "node": node, "digest": digest, "size": size,
           "media_type": media}
    base = folders(kind) if kind in KNOWN_FOLDERS else ""
    if not base:
        row["note"] = "Not placed: OpenAbstractions keeps no folder for this kind of file."
        return row
    base = os.path.realpath(base)
    folder = os.path.join(base, "OpenAbstractions")
    try:
        os.makedirs(base, exist_ok=True)
        with _held_walk(base) as (base_facts, _, base_fd):
            if not stat.S_ISDIR(base_facts[-1].st_mode):
                raise ValueError("the known location is not a directory")
            try:
                if os.name == "nt":
                    os.mkdir(folder)
                else:
                    os.mkdir("OpenAbstractions", dir_fd=base_fd)
            except FileExistsError:
                pass
            if os.name == "nt":
                placed = os.lstat(folder)
            else:
                placed = os.stat("OpenAbstractions", dir_fd=base_fd, follow_symlinks=False)
            with _held_walk(folder) as (facts, _, folder_fd):
                if (not stat.S_ISDIR(facts[-1].st_mode) or not os.path.samestat(facts[-2], base_facts[-1])
                        or not os.path.samestat(facts[-1], placed)):
                    raise ValueError("the export location changed")
                row["path"] = _place(path, stream, source_facts, digest, folder, facts[-1], folder_fd)
    except (OSError, ValueError) as error:
        _log("output %s not placed in %s: %s" % (row["name"], folder, error))
        row["note"] = "Not placed in %s: %s." % (folder, str(error)[:200])
    return row


def oa_routes(entry):
    """The OA picture engine route each OA Image Generate node of one finished prompt reported.

    The node puts its route ({"host", "model", "operation"} as JSON text) in its
    ui output under oa_route; ComfyUI keeps it in the prompt's history.
    """
    routes = []
    for node_id, output in sorted((entry.get("outputs") or {}).items()):
        for item in (output or {}).get("oa_route", ()) if isinstance(output, dict) else ():
            try:
                value = json.loads(item) if isinstance(item, str) else None
            except ValueError:
                value = None
            if isinstance(value, dict):
                routes.append(dict({key: str(value.get(key, ""))[:256] for key in ("host", "model", "operation")},
                                   node=str(node_id)))
    return routes


class WorkflowCommands:
    """workflow.list, workflow.inputs, workflow.run, workflow.download_models and
    job.status on the shared AppModel core."""
    def __init__(self, registry, api, refusal_type, output_root="", ended=None, poll_seconds=1.0, clock=None,
                 folders=None):
        self.registry, self.api, self.Refuse = registry, api, refusal_type
        self.output_root, self.ended = output_root, ended or threading.Event()
        self.poll_seconds, self.clock = poll_seconds, clock or time.monotonic
        # folders(kind) is the person's folder for an output kind (known_folder).
        self.folders = folders if folders is not None else known_folder
        self.jobs = []
        # job.status's records: the newest JOBS_KEPT jobs of this adapter.
        self.status, self.status_lock = collections.OrderedDict(), threading.Lock()
        workflow = {"type": "string", "minLength": 1, "maxLength": 300}
        registry.kind("workflow", schema={"type": "object"}, title="ComfyUI workflow")
        registry.kind("job", schema={"type": "object"}, title="ComfyUI job")
        registry.command("workflow.list", self.list, "read", input_schema={
            "type": "object", "properties": {"search": {"type": "string", "maxLength": 200},
            "source": {"type": "string", "enum": ["", "user", "template"]}}, "additionalProperties": False},
            title="List ComfyUI workflows",
            description="List the person's saved workflows (user:<file>) and the templates (template:<name>), "
                        "narrowed by search; template:<pack>/oa_image_generate_preview_save draws with "
                        "OpenAbstractions' picture engine.")
        registry.command("workflow.inputs", self.inputs, "read", input_schema={
            "type": "object", "properties": {"workflow": workflow},
            "required": ["workflow"], "additionalProperties": False},
            title="Read a workflow's inputs",
            description="Read the inputs workflow.run takes for one workflow: prompt, size, seed, steps, model "
                        "choices with available, and every other widget as <node>.<input>.")
        registry.command("workflow.run", self.run, "commit", input_schema={
            "type": "object", "properties": {"workflow": workflow,
            "inputs": {"type": "object"}}, "required": ["workflow"], "additionalProperties": False},
            title="Run a workflow",
            description="Run a saved workflow or template without the canvas, with values from workflow.inputs, "
                        "and answer a job to follow with job.status.",
            long_running=True)
        registry.command("workflow.download_models", self.download_models, "commit", input_schema={
            "type": "object", "properties": {"workflow": workflow},
            "required": ["workflow"], "additionalProperties": False},
            title="Download a workflow's models",
            description="Download the model files a workflow names that ComfyUI's model folders lack, through "
                        "OpenAbstractions' download service, and answer a job to follow with job.status.",
            long_running=True)
        registry.command("job.status", self.job_status, "read", input_schema={
            "type": "object", "properties": {"job": {"type": "string", "minLength": 1, "maxLength": 64}},
            "required": ["job"], "additionalProperties": False},
            title="Read a job",
            description="Read a ComfyUI job's state, per-model download progress when available, or outputs "
                        "with kind, name, digest, size, media type and a path under Pictures, Videos or Music.")
        self.context = registry.context("workflows", lambda: ([], {}), title="ComfyUI workflows")

    def _refuse(self, error):
        return self.Refuse(error.outcome, error.reason)

    def list(self, call):
        try:
            arguments = call.arguments
            return list_workflows(self.api, arguments.get("search", ""), arguments.get("source", ""))
        except ComfyRefusal as error:
            raise self._refuse(error) from None
        except (ValueError, TypeError, AttributeError):
            raise self.Refuse("unsupported", "backend:answer") from None

    def _graph(self, name):
        return WorkflowGraph(load_workflow(self.api, name), NodeInfo(self.api))

    def inputs(self, call):
        try:
            return self._graph(call.arguments["workflow"]).describe(call.arguments["workflow"])
        except ComfyRefusal as error:
            raise self._refuse(error) from None
        except (ValueError, TypeError, AttributeError, KeyError, IndexError):
            raise self.Refuse("invalid", "workflow:format") from None

    def run(self, call):
        name = call.arguments["workflow"]
        try:
            workflow = load_workflow(self.api, name)
            graph = WorkflowGraph(workflow, NodeInfo(self.api))
            prompt, used = graph.apply(call.arguments.get("inputs", {}), _random_seed)
            key = getattr(call.invocation, "idempotency_key", "") or secrets.token_hex(16)
            prompt_id = str(uuid.uuid5(_PROMPT_NAMESPACE, self.registry.instance + "\0" + key))
            status, answer = self.api.json("POST", "/api/prompt", {
                "prompt": prompt, "client_id": "openabstractions", "prompt_id": prompt_id})
            if status == 400:
                node_errors = answer.get("node_errors") or {}
                model_error = None
                declared = {(LEGACY_MODEL_CATEGORIES.get(model["directory"], model["directory"]), model["name"])
                            for model in workflow_models(workflow)}
                for node_id, errors in node_errors.items():
                    for item in (errors or {}).get("errors", ()):
                        if not isinstance(item, dict) or item.get("type") != "value_not_in_list":
                            continue
                        info = item.get("extra_info")
                        if not isinstance(info, dict):
                            continue
                        input_name = info.get("input_name")
                        filename = info.get("received_value")
                        category = MODEL_INPUT_CATEGORIES.get(input_name) if isinstance(input_name, str) else None
                        if category and isinstance(filename, str):
                            detail = "ComfyUI model %r is unavailable for %s on node %s." % (filename[:192], input_name, node_id[:64])
                            if (category, filename) in declared:
                                raise ComfyRefusal("invalid", detail + " Run workflow.download_models for this workflow, "
                                    "check job.status, then retry. Use workflow.inputs to inspect available choices.")
                            if model_error is None:
                                model_error = detail
                if model_error is not None:
                    raise ComfyRefusal("invalid", model_error + " Use workflow.inputs to inspect available choices.")
                detail = _comfy_reason(answer.get("error"))
                for node_id, errors in sorted(node_errors.items())[:3]:
                    for item in (errors or {}).get("errors", ())[:2]:
                        detail += "; %s: %s" % (node_id, _comfy_reason(item))
                raise ComfyRefusal("invalid", "comfyui:" + detail)
        except ComfyRefusal as error:
            raise self._refuse(error) from None
        except (ValueError, TypeError, AttributeError, KeyError, IndexError):
            raise self.Refuse("invalid", "workflow:format") from None
        return self.follow_prompts(call, [prompt_id], name, used)

    def _start(self, call, target, args, record):
        """Start the call's job, record it for job.status and run target on a
        worker: target(job, *args)."""
        job = call.context.start_job()
        with self.status_lock:
            self.status[job.id] = dict(record, job=job.id, state="queued")
            while len(self.status) > JOBS_KEPT:
                self.status.popitem(last=False)
        worker = threading.Thread(target=target, args=(job, *args), name="comfy-workflow-job", daemon=True)
        self.jobs = [thread for thread in self.jobs if thread.is_alive()]
        self.jobs.append(worker)
        worker.start()
        return job

    def _note(self, job, **fields):
        with self.status_lock:
            record = self.status.get(job.id)
            if record is not None:
                record.update(fields)

    def follow_prompts(self, call, prompts, name, used=None):
        """The job that follows ComfyUI prompts to their outputs: workflow.run's
        one prompt, or the prompts graph.run queued on the page."""
        return self._start(call, self._follow, (list(prompts), name, used), {"workflow": name})

    def job_status(self, call):
        job = call.arguments["job"]
        with self.status_lock:
            record = self.status.get(job)
            record = json.loads(json.dumps(record)) if record is not None else None
        if record is None:
            raise self.Refuse("not_found", "No ComfyUI job %s since ComfyUI's integration started" % job[:64])
        return record

    def _present(self, directory, cache):
        """The file names ComfyUI lists in one model folder, or None when it lists none."""
        if directory not in cache:
            try:
                status, data = self.api.request("GET", "/api/models/" + _quote(directory))
                listed = json.loads(data) if status == 200 else None
            except (ComfyRefusal, ValueError):
                listed = None
            cache[directory] = {n for n in listed if isinstance(n, str)} if isinstance(listed, list) else None
        return cache[directory]

    def download_models(self, call):
        name = call.arguments["workflow"]
        try:
            models = workflow_models(load_workflow(self.api, name))
        except ComfyRefusal as error:
            raise self._refuse(error) from None
        except (ValueError, TypeError, AttributeError):
            raise self.Refuse("invalid", "workflow:format") from None
        listed, missing, rows = {}, [], []
        for model in models:
            present = self._present(model["directory"], listed)
            if present is not None and model["name"] in present:
                rows.append({"name": model["name"], "directory": model["directory"], "state": "present"})
            else:
                missing.append(model)
        return self._start(call, self._deliver, (name, rows, missing), {"workflow": name, "models": rows.copy()})

    def _deliver_one(self, model, deadline, observe):
        """One model through the node pack's route: (state, detail).

        The route delivers one model at a time, so models go one after another. A model
        OA still downloads when the route stops waiting is asked for again, which
        follows the same OA job. The detail is the node pack's own text; it can name
        local paths, so it stays in this adapter's log.
        """
        while True:
            status, answer = self.api.json("POST", MODEL_DOWNLOAD_ROUTE, model, same_origin=True,
                                           missing="comfyui:model_downloads_unavailable")
            outcome = answer.get("outcome") if isinstance(answer, dict) else None
            if outcome == "present":
                return "present", ""
            if status != 200 or outcome != "accepted" or not isinstance(answer.get("id"), str):
                return "refused", str(answer.get("error", "not taken") if isinstance(answer, dict) else "not taken")
            path = MODEL_DOWNLOAD_ROUTE + "/" + _quote(answer["id"])
            while True:
                if self.ended.wait(self.poll_seconds) or self.clock() >= deadline:
                    return "waiting", "the job ended before the download did"
                _, found = self.api.json("GET", path, missing="comfyui:model_download_lost", same_origin=True)
                state = found.get("state") if isinstance(found, dict) else None
                if state != "running":
                    break
                observe(found)
            if state == "waiting" and not self.ended.is_set() and self.clock() < deadline:
                continue
            state = state if state in MODEL_STATES else "failed"
            return state, str(found.get("error", "") if isinstance(found, dict) else "")

    def _deliver(self, job, name, rows, missing):
        deadline = self.clock() + JOB_SECONDS
        try:
            for model in missing:
                row = {"name": model["name"], "directory": model["directory"], "state": "running"}

                def observe(found):
                    progress = row.copy()
                    if found.get("phase") in ("pending", "running", "transferred", "complete", "failed", "cancelled"):
                        progress["phase"] = found["phase"]
                    done, total = found.get("bytes_done"), found.get("bytes_total")
                    if type(done) is int and type(total) is int and done >= 0 and total >= 0:
                        progress.update(bytes_done=done, bytes_total=total)
                        if total:
                            progress["percent"] = min(100, 100 * done // total)
                    self._note(job, state="running", models=[*rows, progress])

                observe({})
                state, detail = self._deliver_one(model, deadline, observe)
                row["state"] = state
                if state not in ("present", "complete"):
                    # The caller gets one fixed word per state; the node pack's text stays here.
                    row["error"] = "comfyui:model_download_" + state
                    if detail:
                        _log("model download %s %s: %s" % (model["name"], state, detail[:512]))
                rows.append(row)
                self._note(job, models=rows.copy())
            failed = [row["name"] for row in rows if row["state"] in ("unavailable", "failed", "refused")]
            self._note(job, models=rows)
            if failed:
                self._fail(job, ("comfyui:model_download_failed: " + ", ".join(failed))[:512])
                return
            self._note(job, state="done")
            job.finish({"workflow": name, "models": rows})
        except ComfyRefusal as error:
            self._fail(job, error.reason)
        except Exception:
            self._fail(job, "internal")

    def _fail(self, job, reason):
        self._note(job, state="failed", error=reason)
        try:
            job.fail(reason)
        except Exception:
            pass

    def _follow(self, job, prompts, name, used):
        deadline, missing, outputs, routes = self.clock() + JOB_SECONDS, 0, [], []
        waiting = list(prompts)
        try:
            while waiting and not self.ended.is_set() and self.clock() < deadline:
                prompt_id = waiting[0]
                _, history = self.api.json("GET", "/api/history/" + _quote(prompt_id))
                entry = history.get(prompt_id) if isinstance(history, dict) else None
                status = (entry or {}).get("status") or {}
                if entry is not None and (status.get("completed") or status.get("status_str") in ("success", "error")):
                    if status.get("status_str") == "error":
                        message = "execution_error"
                        for kind, detail in status.get("messages") or ():
                            if kind == "execution_error" and isinstance(detail, dict):
                                message = "%s: %s" % (detail.get("node_type", ""), detail.get("exception_message", ""))
                        self._fail(job, ("comfyui:" + message.strip())[:512])
                        return
                    output_root = self.output_root() if callable(self.output_root) else self.output_root
                    for node, path, stream, facts in _held_outputs(entry, output_root):
                        try:
                            outputs.append(_output_reference(node, path, stream, facts, self.folders))
                        except OSError as error:
                            _log("output %s unreadable: %s" % (os.path.basename(path), error))
                    routes += oa_routes(entry)
                    waiting.pop(0)
                    missing = 0
                    self._note(job, outputs=outputs)
                    continue
                _, queue = self.api.json("GET", "/api/queue")
                running = any(isinstance(row, list) and len(row) > 1 and row[1] == prompt_id
                              for row in queue.get("queue_running") or ())
                queued = running or any(isinstance(row, list) and len(row) > 1 and row[1] == prompt_id
                                        for row in queue.get("queue_pending") or ())
                if running:
                    self._note(job, state="running")
                missing = 0 if queued or entry is not None else missing + 1
                if missing >= 5:
                    self._fail(job, "comfyui:prompt_lost")
                    return
                self.ended.wait(self.poll_seconds)
            if waiting:
                self._fail(job, "backend:stopped" if self.ended.is_set() else "bound:job_seconds")
                return
            effects = {"workflow": name, "prompts": list(prompts), "outputs": outputs}
            if used is not None:
                effects["inputs"] = used
            if routes:
                effects["oa_routes"] = routes
            self._note(job, state="done", outputs=outputs)
            job.finish(effects)
        except ComfyRefusal as error:
            self._fail(job, error.reason)
        except Exception:
            self._fail(job, "internal")

    def close(self):
        if not self.context.closed:
            self.context.close()


# Private X reply codes from the native Windows launcher and Desktop connector.
# Older retained connectors can return raw backend output in this frame.
START_FAILURE_REASONS = {
    "backend:output_unavailable": "OpenAbstractions could not prepare ComfyUI's backend output",
    "backend:prepare_failed": "OpenAbstractions could not prepare the installed ComfyUI backend",
    "backend:verify_or_launch_failed": "OpenAbstractions could not verify or launch the installed ComfyUI backend",
    "backend:startup_incomplete": "ComfyUI did not finish starting",
    "backend:listener_timeout": "ComfyUI's backend did not begin listening before the startup deadline",
    "backend:early_exit": "ComfyUI's backend exited before it began listening",
    "backend:stopped_after_listening": "ComfyUI's backend stopped after it began listening",
    "backend:manifest_mismatch": "The installed ComfyUI backend files differ from their verified manifest",
    "backend:folder_permissions": "The installed ComfyUI backend folder permissions failed verification",
    "backend:unreadable_file": "OpenAbstractions could not read a file in the installed ComfyUI backend",
    "desktop:listener_timeout": "ComfyUI's server did not begin listening before the startup deadline",
    "desktop:early_exit": "ComfyUI's server exited before it began listening",
    "desktop:server_exit": "ComfyUI's server exited",
}


class Control:
    def __init__(self, incoming, outgoing):
        self.incoming, self.outgoing = incoming, outgoing
        self.lock = threading.Lock()
        self.failed = False

    def _retire(self):
        self.failed = True
        for stream in (self.outgoing, self.incoming):
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def _available(self):
        if self.failed:
            raise OSError("native control channel retired")

    def request(self, operation):
        if operation not in (b"O", b"P", b"C", b"R", b"G", b"S"):
            raise ValueError("foreign native operation")
        with self.lock:
            self._available()
            try:
                self.outgoing.write(operation)
                self.outgoing.flush()
                answer = self.incoming.read(1)
                if not answer:
                    raise OSError("native control channel ended")
                return answer
            except (OSError, ValueError):
                self._retire()
                raise OSError("native control channel failed") from None

    def close(self):
        self._retire()

    def cause(self, read_frame):
        """Read a known startup cause code and return its public wording."""
        with self.lock:
            self._available()
            try:
                self.outgoing.write(b"X")
                self.outgoing.flush()
                raw = read_frame(self.incoming.read, 4096)
                if raw is None:
                    raise OSError("native control channel ended")
                value = json.loads(raw)
                cause = value.get("cause") if isinstance(value, dict) else None
                return START_FAILURE_REASONS.get(cause, "") if isinstance(cause, str) else ""
            except (OSError, ValueError):
                self._retire()
                raise OSError("native control channel failed") from None

    def attachment(self, read_frame):
        """The served ComfyUI: {"port", "output"}, or {} when none."""
        with self.lock:
            self._available()
            try:
                self.outgoing.write(b"Q")
                self.outgoing.flush()
                raw = read_frame(self.incoming.read, 4096)
                if raw is None:
                    raise OSError("native control channel ended")
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError("native attachment shape")
                return value
            except (OSError, ValueError):
                self._retire()
                raise OSError("native control channel failed") from None

    def acknowledge_arm(self,message,write_frame):
        raw=_arm_ack_bytes(message)
        with self.lock:
            self._available()
            try:
                self.outgoing.write(b"B")
                write_frame(self.outgoing.write,raw,256)
                self.outgoing.flush()
                answer = self.incoming.read(1)
                if answer not in (b"A", b"U"):
                    raise OSError("invalid native ARM acknowledgement")
                return answer == b"A"
            except (OSError, ValueError):
                self._retire()
                raise OSError("native control channel failed") from None


# What an open answers for each phase in which ComfyUI does not serve yet:
# refused reads as not ready, unavailable as a failed open.
# A starting phase reads "starting:<phase>: <sentence>"; the MCP gateway
# follows such an open with a handle (mcp-gateway/gateway/appmodel_open.go).
OPEN_NOT_READY = {
    "idle": "starting:launching: ComfyUI is starting",
    "verifying": "starting:launching: ComfyUI is starting; the first start can take a few minutes",
    "launching": "starting:launching: ComfyUI's server is launching",
    "loading_custom_nodes": "starting:loading_custom_nodes: ComfyUI's server is loading its custom nodes",
    "listening": "starting:listening: ComfyUI's server listens; OpenAbstractions is connecting to it",
    "already_running": "starting:already_running: ComfyUI is already starting in Comfy Desktop",
    "desktop_idle": "Comfy Desktop is open, but no ComfyUI install runs in it, and OpenAbstractions does not start "
                    "ComfyUI beside it. Ask the person to start ComfyUI in Comfy Desktop, or to close Comfy Desktop "
                    "and open ComfyUI again",
}
OPEN_FAILED = {
    "failed": "ComfyUI did not start",
    "exited": "ComfyUI stopped while it started",
    "exit_unknown": "ComfyUI's start could not be confirmed",
}


def open_handler(control, result_type, outcomes, phase=None, ended=None, wait=None, clock=time.monotonic, cause=None):
    """The open verb: delivered to the connector, then applied once ComfyUI
    serves commands. phase reads the polled backend phase; without it an
    accepted delivery is applied at once."""

    def activate(raw):
        # Shared Server validates the declared schema before this handler.
        # Retain exact semantics for direct inert testing too.
        if json.loads(raw) != {}:
            return result_type(outcome=outcomes.REFUSED, reason="invalid_open")
        try:
            answer = control.request(b"O")
        except (OSError, ValueError):
            return result_type(outcome=outcomes.UNAVAILABLE, reason="OpenAbstractions lost its connection to ComfyUI")
        if answer != b"A" and (phase is None or phase() not in OPEN_FAILED):
            return result_type(outcome=outcomes.UNAVAILABLE, reason="ComfyUI could not be started")
        if phase is None:
            return result_type(outcome=outcomes.APPLIED, reason="")
        deadline = clock() + (OPEN_READY_SECONDS if wait is None else wait)
        while True:
            current = phase()
            if current in ("running", "browser_failed"):
                return result_type(outcome=outcomes.APPLIED, reason="")
            if current in OPEN_FAILED:
                why = cause() if cause is not None and current == "failed" else ""
                return result_type(outcome=outcomes.UNAVAILABLE, reason=why or OPEN_FAILED[current])
            if current == "desktop_idle" or clock() >= deadline or (ended is not None and ended.is_set()):
                return result_type(outcome=outcomes.REFUSED, reason=OPEN_NOT_READY.get(current, OPEN_NOT_READY["idle"]))
            if ended is not None:
                ended.wait(0.25)
            else:
                time.sleep(0.25)
    return activate


def serve(instance, control_read, control_write, stdin, stdout, manifest, graph_read, graph_write, library=None):
    from abstraction.facade import applications
    from abstraction.facade import registration
    from abstraction.facade import appmodel
    from abstraction.ipc.stream import read_frame_from, write_frame_to
    from appmodel import Registry, Server, Refuse
    verb = registration.ActivationVerb(name="open", class_=appmodel.CommandClass.REVEAL,
        input_schema=b'{"type":"object","properties":{},"additionalProperties":false}')
    # The native parent authenticated Announce before starting this adapter.
    registry = native_registry(instance, Registry, appmodel, Refuse, library)
    control = Control(control_read, control_write)
    ended = threading.Event()
    mediation_ended = threading.Event()
    def agent_stop(call):
        try:
            outcome = control.request(b"S")
        except (OSError, ValueError):
            outcome = b"T"
        if outcome == b"P":
            raise Refuse("conflict", "The person is using ComfyUI.")
        if outcome == b"U":
            raise Refuse("unsupported", "Only ComfyUI started by this OpenAbstractions session can stop.")
        if outcome != b"A":
            raise Refuse("unknown", "internal")
        return {"status": "stop_requested"}
    def shutdown():
        try:
            known = control.request(b"C") == b"A"
        except (OSError,ValueError):
            known = False
        if known: ended.set()
        return known
    graph = GraphSession(registry, graph_read, graph_write, read_frame_from, write_frame_to, Refuse,
                         shutdown=shutdown,arm_ack=lambda message:control.acknowledge_arm(message,write_frame_to),
                         show_page=lambda: control.request(b"G") == b"A")
    state = {"phase": "idle", "integration_ready": False, "graph_status":"waiting", "cause": ""}
    projected = ("phase", "integration_ready", "graph_status", "cause")
    registry.kind("backend", schema={"type":"object","properties":{
        "phase":{"type":"string"},"integration_ready":{"type":"boolean"},"graph_status":{"type":"string"},
        "cause":{"type":"string"}},
        "required":["phase","integration_ready","graph_status","cause"],"additionalProperties":False})
    registry.command("backend.stop", agent_stop, "commit", input_schema={
        "type": "object", "properties": {"reason": {"type": "string",
        "enum": ["task_done", "next_job"]}}, "required": ["reason"], "additionalProperties": False},
        description="Stop the ComfyUI instance this agent started after its queue drains. "
                    "Use task_done or next_job when freeing resources for your next job.")
    context = registry.context("startup", lambda: ([{"id":"backend", "kind":"backend",
        "label":"ComfyUI backend startup", "properties":{key: state[key] for key in projected}}], {}),
        title="Backend startup")
    # The connector proved the served ComfyUI's listener belongs to this
    # ComfyUI; the adapter calls it only while the connector serves it.
    def served():
        value = state.get("served")
        if not value:
            try:
                value = control.attachment(read_frame_from)
            except OSError:
                value = {}
            if value.get("port"):
                state["served"] = value
        return value
    workflows = WorkflowCommands(registry, ComfyAPI(running=lambda: backend_ready(state, ended),
                                                    port=lambda: served().get("port")), Refuse,
                                 output_root=lambda: str(served().get("output") or ""), ended=ended)
    graph.follow = workflows.follow_prompts
    def start_cause():
        try:
            return control.cause(read_frame_from)
        except OSError:
            return ""
    def poll():
        phases = {b"I":"idle", b"V":"verifying", b"D":"desktop_idle", b"L":"launching", b"K":"loading_custom_nodes",
                  b"W":"listening", b"Y":"already_running", b"R":"running", b"B":"browser_failed", b"U":"exit_unknown",
                  b"F":"failed", b"E":"exited"}
        while not ended.is_set():
            try:
                answer = control.request(b"P")
                if answer not in phases:
                    ended.set()
                    return
                if answer == b"F" and state["phase"] != "failed":
                    state["cause"] = start_cause()
                elif answer == b"D":
                    state["cause"] = OPEN_NOT_READY["desktop_idle"]
                elif answer != b"F":
                    state["cause"] = ""
                state["phase"] = phases[answer]
                if answer not in (b"R", b"B"):
                    state.pop("served", None)
                state["graph_status"] = graph.status_text()
                if not mediation_ended.is_set(): context.commit()
                if answer in (b"E",b"F") and mediation_ended.is_set():
                    ended.set()
                    return
            except (OSError, ValueError):
                ended.set()
                return
            ended.wait(0.25)
    watcher = threading.Thread(target=poll, daemon=True)
    graph.start()
    watcher.start()
    server = Server(registry, name=manifest["name"], version="0.8.0", activation={
        "application": manifest["name"], "verbs": [verb],
        "handlers": {"open": open_handler(control,
            applications.HostActivationResult, applications.HostActivationOutcome,
            phase=lambda: state["phase"], ended=ended, cause=lambda: state["cause"])}})
    retire_lock = threading.Lock()
    def retire():
        with retire_lock:
            if mediation_ended.is_set(): return
            mediation_ended.set()
            graph.retire_mediation()
            workflows.close()
            context.close()
            try:
                if control.request(b"R") not in (b"V",b"R",b"B",b"U"): ended.set()
            except (OSError,ValueError): ended.set()
    def write(body):
        write_frame_to(stdout.write, body)
        stdout.flush()
    try:
        server.serve_owned_frames(lambda: read_frame_from(stdin.read), write, retire)
    finally:
        retire()
        # The application-owned shutdown path survives MCP retirement. The
        # native parent retains this adapter until its backend ends or C joins it.
        ended.wait()
        control.close()
        graph.close()
        watcher.join()


def _inherited_stream(handle, access, mode):
    if os.name == "nt":
        import msvcrt
        descriptor = msvcrt.open_osfhandle(handle, access | os.O_BINARY | os.O_NOINHERIT)
    else:
        # The native parent gives this adapter dedicated pipe descriptors, not
        # a file path, socket or the process's standard streams. Transfer only
        # this inherited descriptor's ownership to the existing stream owner.
        if handle < 3 or not stat.S_ISFIFO(os.fstat(handle).st_mode):
            raise ValueError("dedicated inherited pipe descriptor required")
        import fcntl
        if fcntl.fcntl(handle, fcntl.F_GETFL) & os.O_ACCMODE != access:
            raise ValueError("inherited pipe direction differs")
        descriptor = handle
    try:
        if os.name != "nt":
            os.set_inheritable(descriptor, False)
        return os.fdopen(descriptor, mode, buffering=0)
    except BaseException:
        os.close(descriptor)
        raise


def main(argv):
    if len(argv) != 7 or not argv[0] or len(argv[0]) > 128:
        raise ValueError("fixed native host inputs required")
    handles = [int(value) for value in argv[1:5]]
    if (any(value <= 0 or value > 18446744073709551615 or str(value) != raw
            for value,raw in zip(handles,argv[1:5])) or len(set(handles)) != 4):
        raise ValueError("distinct canonical native handles required")
    # The connector runs this file with -I -S: no site startup or ambient
    # PYTHONPATH. OA's own Python SDK is the one this node pack carries.
    sys.path.insert(0, str(Path(__file__).resolve().parent / "oa_client"))
    read_handle, write_handle, graph_read_handle, graph_write_handle = handles
    manifest = json.loads(argv[5])
    if manifest.get("name") != "comfyui":
        raise ValueError("foreign application")
    library = argv[6]
    if not os.path.isabs(library) or not os.path.isfile(library):
        raise ValueError("the runtime's application-model library is unavailable")
    import contextlib
    with contextlib.ExitStack() as stack:
        streams = [stack.enter_context(_inherited_stream(handle, access, mode))
            for handle,access,mode in ((read_handle,os.O_RDONLY,"rb"),(write_handle,os.O_WRONLY,"wb"),
                (graph_read_handle,os.O_RDONLY,"rb"),(graph_write_handle,os.O_WRONLY,"wb"))]
        serve(argv[0], streams[0], streams[1], sys.stdin.buffer, sys.stdout.buffer, manifest, streams[2], streams[3],
              library)


if __name__ == "__main__":
    main(sys.argv[1:])
