import test from "node:test";
import assert from "node:assert/strict";
import { graphView, projectGraph, widgetKind, MAX_NODES, MAX_STRING } from "./oa_appmodel_graph.mjs";

// Fakes of ComfyUI's extension surface (the app and api objects, the
// LiteGraph root graph, node classes with their nodeData, widgets and the
// workflow store) in the shapes frontends ship: "1.55" as frontend 1.55.x,
// "1.58" as frontend 1.58.x, where a widget's value lives in the widget value
// store behind a getter and setter and the root graph lists its nodes and
// links through getters, and "bare", a frontend with only app.graph, no
// workflow store, no legacy menu and no queue store.
const SHAPES = ["1.55", "1.58", "bare"];
const CHECKPOINTS = Array.from({ length: 100 }, (_, i) => `model-${String(i).padStart(3, "0")}.safetensors`);
const SAMPLERS = ["euler", "euler_ancestral", "dpmpp_2m"];

function nodeClass(name, required) {
  return class { static nodeData = { name, input: { required } }; };
}
const CLASSES = {
  CheckpointLoaderSimple: nodeClass("CheckpointLoaderSimple", { ckpt_name: [CHECKPOINTS] }),
  CLIPTextEncode: nodeClass("CLIPTextEncode", { text: ["STRING", { multiline: true }], clip: ["CLIP"] }),
  KSampler: nodeClass("KSampler", { model: ["MODEL"], seed: ["INT", { min: 0, max: 2 ** 53 - 1, control_after_generate: true }],
    steps: ["INT", { min: 1, max: 10000 }], cfg: ["FLOAT", { min: 0, max: 100 }], sampler_name: [SAMPLERS],
    denoise: ["FLOAT", { min: 0, max: 1 }], enabled: ["BOOLEAN"] }),
  SaveImage: nodeClass("SaveImage", { images: ["IMAGE"], filename_prefix: ["STRING"] })
};

function makeWidget(shape, store, spec) {
  const callback = function (value) { if (typeof value === "number" && spec.type === "number" && spec.options?.precision === 0) this.value = Math.round(value); };
  if (shape !== "1.58") return { callback, ...spec };
  const key = spec.name + ":" + store.size;
  store.set(key, spec.value);
  const { value, ...rest } = spec;
  return { ...rest, callback, widgetId: key, get value() { return store.get(key); }, set value(next) { store.set(key, next); } };
}

function makeNode(shape, store, id, type, title, widgets, inputs = [], outputs = []) {
  const node = new CLASSES[type]();
  Object.assign(node, { id, type, comfyClass: type, title, mode: 0, pos: [id * 100, 0], size: [80, 60],
    inputs, outputs, widgets: widgets.map(spec => makeWidget(shape, store, spec)) });
  return node;
}

function fixture(shape = "1.55", { autoQueue = false, queueStore = true } = {}) {
  const store = new Map();
  let commits = 0, changes = 0, centered = 0;
  const nodes = [
    makeNode(shape, store, 4, "CheckpointLoaderSimple", "Load Checkpoint",
      [{ name: "ckpt_name", type: "combo", value: CHECKPOINTS[3], options: { values: () => CHECKPOINTS } }],
      [], [{ name: "MODEL", type: "MODEL", links: [1] }, { name: "CLIP", type: "CLIP", links: [2] }]),
    makeNode(shape, store, 6, "CLIPTextEncode", "Positive",
      [{ name: "text", type: "customtext", value: "a fox in snow" }], [{ name: "clip", type: "CLIP", link: 2 }],
      [{ name: "CONDITIONING", type: "CONDITIONING" }]),
    makeNode(shape, store, 3, "KSampler", "KSampler", [
      { name: "seed", type: "number", value: 42, options: { min: 0, max: 2 ** 53 - 1, step: 10, step2: 1, precision: 0 } },
      { name: "control_after_generate", type: "combo", value: "fixed", options: { values: ["fixed", "increment", "decrement", "randomize"] } },
      { name: "steps", type: "number", value: 20, options: { min: 1, max: 10000, step: 10, step2: 1, precision: 0 } },
      { name: "cfg", type: "number", value: 7.5, options: { min: 0, max: 100, step: 1, step2: 0.1, precision: 1 } },
      { name: "sampler_name", type: "combo", value: "euler", options: { values: SAMPLERS } },
      { name: "denoise", type: "number", value: 1, options: { min: 0, max: 1, step: 0.1, step2: 0.01, precision: 2 } },
      { name: "enabled", type: "toggle", value: true },
      { name: "preview", type: "button", value: undefined }
    ], [{ name: "model", type: "MODEL", link: 1 }, { name: "denoise", type: "FLOAT", widget: { name: "denoise" }, link: 9 }],
    [{ name: "LATENT", type: "LATENT" }]),
    makeNode(shape, store, 9, "SaveImage", "Save", [{ name: "filename_prefix", type: "text", value: "ComfyUI" }],
      [{ name: "images", type: "IMAGE", link: null }])
  ];
  const linkRows = [
    { id: 1, origin_id: 4, origin_slot: 0, target_id: 3, target_slot: 0, type: "MODEL" },
    { id: 2, origin_id: 4, origin_slot: 1, target_id: 6, target_slot: 0, type: "CLIP" },
    { id: 9, origin_id: 6, origin_slot: 0, target_id: 3, target_slot: 1, type: "FLOAT" },
    { id: 12, origin_id: 77, origin_slot: 0, target_id: 3, target_slot: 2, type: "X" }
  ];
  const groups = [{ title: "Sampling", _bounding: [250, -20, 300, 120] }];
  const graph = shape === "1.58"
    ? { _nodes: nodes, get nodes() { return this._nodes; }, _links: new Map(linkRows.map(l => [l.id, l])), _groups: groups,
        getNodeById(id) { return this._nodes.find(n => n.id === id); } }
    : { _nodes: nodes, links: Object.fromEntries(linkRows.map(l => [l.id, l])), _groups: groups,
        getNodeById(id) { return this._nodes.find(n => n.id === id); },
        ...(shape === "bare" ? { beforeChange() { changes++; }, afterChange() { changes++; } } : {}) };
  for (const node of nodes) node.graph = graph;
  const active = { changeTracker: { beforeChange() {}, afterChange() { commits++; } } };
  const canvas = { graph, centerOnNode() { centered++; } };
  const queued = [];
  const api = { async queuePrompt(number, data) { queued.push(data); return { prompt_id: "prompt-" + String(queued.length).padStart(12, "0"), number }; } };
  let app;
  const queuePrompt = async function () {
    for (const node of nodes) for (const w of node.widgets) if (w.name === "seed") w.value = w.value + 1;
    await api.queuePrompt(0, { output: {} });
    return true;
  };
  if (shape === "bare") app = { graph, canvas, queuePrompt };
  else {
    app = { isGraphReady: true, extensionManager: { workflow: { activeWorkflow: active, isBusy: false } },
      ui: { autoQueueEnabled: autoQueue }, canvas, queuePrompt };
    if (shape === "1.55") app.rootGraph = app.graph = graph;
    else Object.defineProperties(app, { rootGraph: { get: () => graph }, graph: { get: () => graph } });
  }
  let queue = shape === "bare" || !queueStore ? null : { mode: "disabled" };
  const model = graphView(app, { queueState: () => queue, api });
  const state = model.loaded("workflow0000000001");
  const request = (operation, args, sequence = state.sequence) => ({ operation, epoch: state.epoch, sequence, arguments: args });
  return { app, api, graph, nodes, model, state, request, queued, queue: value => { queue = value; },
    commits: () => commits, changes: () => changes, centered: () => centered, node: id => nodes.find(n => n.id === id) };
}

const widget = (state, id, name) => state.graph.nodes.find(n => n.id === id).widgets.find(w => w.name === name);

for (const shape of SHAPES) {
  test(`${shape}: the projection carries nodes, typed widgets, slots, links and groups`, () => {
    const f = fixture(shape);
    assert.equal(f.model.status, "ready");
    assert.deepEqual(f.state.graph.nodes.map(n => [n.id, n.type, n.title, n.mode]),
      [["3", "KSampler", "KSampler", "always"], ["4", "CheckpointLoaderSimple", "Load Checkpoint", "always"],
       ["6", "CLIPTextEncode", "Positive", "always"], ["9", "SaveImage", "Save", "always"]]);
    const sampler = f.state.graph.nodes[0];
    assert.deepEqual(sampler.widgets.map(w => [w.name, w.type]), [["seed", "seed"], ["control_after_generate", "combo"],
      ["steps", "int"], ["cfg", "float"], ["sampler_name", "combo"], ["denoise", "float"], ["enabled", "bool"]]);
    assert.deepEqual(widget(f.state, "3", "steps"), { name: "steps", type: "int", value: 20, min: 1, max: 10000, step: 1 });
    assert.deepEqual(widget(f.state, "3", "sampler_name").choices, SAMPLERS);
    assert.equal(widget(f.state, "3", "denoise").linked, true);
    assert.deepEqual(widget(f.state, "4", "ckpt_name"), { name: "ckpt_name", type: "combo", value: CHECKPOINTS[3], choices_count: 100 });
    assert.deepEqual(widget(f.state, "6", "text"), { name: "text", type: "string", value: "a fox in snow" });
    assert.deepEqual(sampler.inputs, [{ name: "model", type: "MODEL" }, { name: "denoise", type: "FLOAT" }]);
    assert.deepEqual(f.state.graph.links, [{ from: "4", from_slot: 0, to: "3", to_slot: 0 },
      { from: "6", from_slot: 0, to: "3", to_slot: 1 }, { from: "4", from_slot: 1, to: "6", to_slot: 0 }]);
    assert.deepEqual(f.state.graph.groups, [{ title: "Sampling", members: ["3", "4"] }]);
  });

  test(`${shape}: one edit sets widgets and a mode as one undo step and answers the new view`, () => {
    const f = fixture(shape);
    const result = f.model.execute(f.request("edit", { ops: [
      { op: "set_widget", node: "3", widget: "steps", value: 30 },
      { op: "set_widget", node: "3", widget: "sampler_name", value: "dpmpp_2m" },
      { op: "set_widget", node: "4", widget: "ckpt_name", value: CHECKPOINTS[50] },
      { op: "set_mode", node: "9", mode: "bypass" }] }));
    assert.equal(result.outcome, "applied");
    assert.deepEqual(result.answer, { applied: 4 });
    assert.equal(result.projection.sequence, 1);
    assert.equal(widget(result.projection, "3", "steps").value, 30);
    assert.equal(widget(result.projection, "4", "ckpt_name").value, CHECKPOINTS[50]);
    assert.equal(result.projection.graph.nodes.find(n => n.id === "9").mode, "bypass");
    assert.equal(f.node(9).mode, 4);
    if (shape === "bare") assert.equal(f.changes(), 2); else assert.equal(f.commits(), 1);
  });

  test(`${shape}: an edit checks every operation against the widget before it changes anything`, () => {
    const f = fixture(shape);
    for (const [op, reason] of [
      [{ op: "set_widget", node: "3", widget: "steps", value: 0 }, /steps takes 1 to 10000/],
      [{ op: "set_widget", node: "3", widget: "steps", value: 2.5 }, /whole number/],
      [{ op: "set_widget", node: "3", widget: "sampler_name", value: "heun" }, /no choice "heun"/],
      [{ op: "set_widget", node: "3", widget: "denoise", value: 0.5 }, /from a link/],
      [{ op: "set_widget", node: "3", widget: "enabled", value: "yes" }, /true or false/],
      [{ op: "set_widget", node: "8", widget: "steps", value: 3 }, /node "8" is not on the canvas/],
      [{ op: "set_mode", node: "3", mode: "never" }, /always, bypass or mute/],
      [{ op: "add_node", node: "3" }, /not set_widget or set_mode/]]) {
      const result = f.model.execute(f.request("edit", { ops: [{ op: "set_widget", node: "3", widget: "steps", value: 25 }, op] }));
      assert.equal(result.outcome, "invalid");
      assert.match(result.answer.reason, reason);
      assert.match(result.answer.reason, /^ops\[1\]/);
    }
    assert.equal(f.node(3).widgets.find(w => w.name === "steps").value, 20);
    assert.equal(f.model.capture().sequence, 0);
  });

  test(`${shape}: an edit on an older sequence is stale and a person's edit moves the sequence`, () => {
    const f = fixture(shape);
    f.node(3).widgets.find(w => w.name === "steps").value = 22;
    const op = { ops: [{ op: "set_widget", node: "3", widget: "steps", value: 30 }] };
    assert.equal(f.model.execute(f.request("edit", op)).outcome, "stale");
    assert.equal(f.model.execute(f.request("edit", op, 1)).outcome, "applied");
  });

  test(`${shape}: choices pages a long combo by query and cursor`, () => {
    const f = fixture(shape);
    const first = f.model.execute(f.request("choices", { node: "4", widget: "ckpt_name", query: "", cursor: "" }));
    assert.deepEqual([first.outcome, first.answer.choices.length, first.answer.cursor, first.answer.total], ["applied", 64, "64", 100]);
    const second = f.model.execute(f.request("choices", { node: "4", widget: "ckpt_name", query: "", cursor: "64" }));
    assert.deepEqual([second.answer.choices.length, second.answer.cursor], [36, ""]);
    const found = f.model.execute(f.request("choices", { node: "4", widget: "ckpt_name", query: "MODEL-09", cursor: "" }));
    assert.deepEqual(found.answer, { choices: CHECKPOINTS.slice(90), cursor: "", total: 10 });
    assert.equal(f.model.execute(f.request("choices", { node: "3", widget: "steps", query: "", cursor: "" })).outcome, "invalid");
  });

  test(`${shape}: run queues the canvas through the page's own Queue and answers its prompt`, async () => {
    const f = fixture(shape);
    const own = Object.prototype.hasOwnProperty.call(f.api, "queuePrompt"), original = f.api.queuePrompt;
    const result = await f.model.execute(f.request("run", {}));
    assert.equal(result.outcome, "applied");
    assert.deepEqual(result.answer, { prompts: ["prompt-000000000001"] });
    assert.equal(widget(result.projection, "3", "seed").value, 43, "the seed the Queue advanced is in the view");
    assert.equal(f.api.queuePrompt, original);
    assert.equal(Object.prototype.hasOwnProperty.call(f.api, "queuePrompt"), own);
    assert.equal((await f.model.execute(f.request("run", {}))).outcome, "stale", "the agent ran what it read");
  });

  test(`${shape}: reveal centers the canvas on the node`, () => {
    const f = fixture(shape);
    assert.equal(f.model.execute(f.request("reveal", { node: "4" }, 99)).outcome, "applied");
    assert.equal(f.centered(), 1);
    assert.equal(f.model.execute(f.request("reveal", { node: "5" })).outcome, "invalid");
  });
}

test("a refused queue answers ComfyUI's own reason", async () => {
  const f = fixture("1.55");
  f.api.queuePrompt = async () => { const error = Error("400"); error.response = { error: { message: "Prompt outputs failed validation" } }; throw error; };
  f.app.queuePrompt = async function () { try { await f.api.queuePrompt(0, {}); } catch { return false; } };
  const result = await f.model.execute(f.request("run", {}));
  assert.deepEqual(result, { outcome: "refused", answer: { reason: "Prompt outputs failed validation" } });
});

test("with auto queue on an edit refuses; a frontend without the queue store edits", () => {
  const on = fixture("1.55");
  on.queue({ mode: "change" });
  const op = { ops: [{ op: "set_widget", node: "3", widget: "steps", value: 30 }] };
  const refused = on.model.execute(on.request("edit", op));
  assert.equal(refused.outcome, "refused");
  assert.match(refused.answer.reason, /auto queue/);
  const legacy = fixture("1.55", { autoQueue: true });
  assert.equal(legacy.model.execute(legacy.request("edit", op)).outcome, "refused");
  const none = fixture("1.55", { queueStore: false });
  assert.equal(none.model.execute(none.request("edit", op)).outcome, "applied");
});

test("the projection is bounded: 512 nodes, 4 KB strings and inline choices that fit the frame", () => {
  const nodes = [];
  const graph = { _nodes: nodes, links: {}, getNodeById: id => nodes.find(n => n.id === id) };
  for (let id = 1; id <= 600; id++) {
    const node = new CLASSES.CLIPTextEncode();
    Object.assign(node, { id, type: "CLIPTextEncode", title: "t", graph,
      widgets: [{ name: "text", type: "customtext", value: "x".repeat(id === 1 ? 10000 : 10) }] });
    nodes.push(node);
  }
  const projected = projectGraph(graph);
  assert.equal(projected.nodes.length, MAX_NODES);
  assert.equal(projected.truncated, true);
  assert.equal(projected.nodes[0].widgets[0].value.length, MAX_STRING);
  assert.equal(projected.nodes[0].widgets[0].truncated, true);
  // A canvas whose inline choices would pass the frame keeps counts instead.
  const big = { _nodes: [], links: {}, getNodeById: () => null };
  for (let id = 1; id <= 400; id++) {
    const node = new CLASSES.KSampler();
    Object.assign(node, { id, type: "KSampler", graph: big, widgets: [{ name: "sampler_name", type: "combo", value: "a",
      options: { values: Array.from({ length: 64 }, (_, i) => "s".repeat(60) + i) } }] });
    big._nodes.push(node);
  }
  const app = { graph: big, canvas: { graph: big } };
  const view = graphView(app, { queueState: () => null });
  const state = view.loaded("workflow0000000002");
  assert.equal(state.graph.nodes.length, 400);
  assert.equal(state.graph.nodes[0].widgets[0].choices_count, 64);
});

test("a widget's kind follows the node class's input spec, then the widget type", () => {
  const sampler = new CLASSES.KSampler();
  assert.equal(widgetKind(sampler, { name: "seed", type: "number", value: 1 }), "seed");
  assert.equal(widgetKind(sampler, { name: "cfg", type: "number", value: 7 }), "float");
  const plain = {};
  assert.equal(widgetKind(plain, { name: "n", type: "number", value: 3, options: { precision: 0 } }), "int");
  assert.equal(widgetKind(plain, { name: "n", type: "number", value: 0.5, options: { precision: 2 } }), "float");
  assert.equal(widgetKind(plain, { name: "b", type: "button" }), null);
});

test("loading and a busy workflow store make the view unavailable; retire ends the epoch", () => {
  const f = fixture("1.55");
  f.app.extensionManager.workflow.isBusy = true;
  assert.equal(f.model.capture(), null);
  assert.equal(f.model.status, "unavailable");
  f.app.extensionManager.workflow.isBusy = false;
  f.model.retire();
  assert.equal(f.model.execute(f.request("edit", { ops: [{ op: "set_widget", node: "3", widget: "steps", value: 30 }] })).outcome, "stale");
  assert.equal(f.model.execute({ operation: "edit", epoch: "short", sequence: 0, arguments: {} }).outcome, "invalid");
});
