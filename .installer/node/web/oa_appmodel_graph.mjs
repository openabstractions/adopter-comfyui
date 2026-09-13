// Private graph projection over ComfyUI's extension surface: the app object
// (/scripts/app.js), the api object (/scripts/api.js), its LiteGraph root
// graph, node widgets and the workflow store the extension manager exposes.
// Nothing here imports a compiled frontend module, so any frontend version
// that keeps that surface works. The adapter in the native host owns the
// revision and the agent's commands; this page keeps one current view
// (adopters/comfyui/GRAPH.md).
export const MAX_NODES = 512;
export const MAX_LINKS = 4096;
export const MAX_STRING = 4096;
export const MAX_CHOICES = 64;
export const MAX_SLOTS = 128;
export const MAX_OPS = 64;
// Beyond this many nodes the page does not walk the graph at all.
export const MAX_GRAPH_NODES = 4096;
// The page channel's frame bound, less room for the message around it.
export const FRAME = 1 << 20;
const BUDGET = FRAME - 4096;
const MODES = { 0: "always", 2: "mute", 4: "bypass" };
const MODE_VALUES = { always: 0, mute: 2, bypass: 4 };
const SEEDS = new Set(["seed", "noise_seed"]);
const exact = (value, keys) => value && typeof value === "object" && !Array.isArray(value) &&
  Object.keys(value).sort().join(",") === [...keys].sort().join(",");
const plain = value => value && typeof value === "object" && !Array.isArray(value);
const token = value => typeof value === "string" && /^[A-Za-z0-9_-]{16,64}$/.test(value);

// The root graph: app.rootGraph on current frontends, app.graph before it.
// isGraphReady, where the frontend has it, says whether either exists yet.
export function rootGraph(app) {
  if (app.isGraphReady === false) return null;
  const graph = app.rootGraph ?? app.graph;
  return graph && typeof graph.getNodeById === "function" ? graph : null;
}

function graphNodes(graph) {
  const nodes = graph._nodes ?? graph.nodes;
  return Array.isArray(nodes) ? nodes : null;
}

function graphLinks(graph) {
  const links = graph._links ?? graph.links;
  if (links instanceof Map) return [...links.values()];
  if (Array.isArray(links)) return links.filter(Boolean);
  return links && typeof links === "object" ? Object.values(links) : [];
}

function cut(value) {
  const text = String(value);
  return text.length > MAX_STRING ? [text.slice(0, MAX_STRING), true] : [text, false];
}

// The node class's own input spec for a widget: ComfyUI's nodeData, as
// /object_info lists it.
function inputSpec(node, name) {
  const data = node.constructor?.nodeData;
  const spec = data?.input?.required?.[name] ?? data?.input?.optional?.[name];
  return Array.isArray(spec) ? spec : null;
}

function comboValues(widget, node) {
  let values = widget.options?.values;
  if (typeof values === "function") {
    try { values = values(widget, node); } catch { values = null; }
  }
  return Array.isArray(values) ? values : null;
}

function integerOptions(widget) {
  const o = widget.options ?? {};
  if (o.precision === 0) return true;
  if (o.precision !== undefined) return false;
  return Number.isInteger(widget.value) && Number.isInteger(o.step2 ?? 1) && (o.round === undefined || o.round === 1);
}

// The widget's kind: int, float, string, bool, combo or seed; null for a
// widget that holds no value an agent sets (a button, a preview).
export function widgetKind(node, widget) {
  const spec = inputSpec(node, widget.name);
  const base = spec ? (Array.isArray(spec[0]) ? "COMBO" : String(spec[0])) : null;
  const seed = SEEDS.has(widget.name) || spec?.[1]?.control_after_generate === true;
  if (base === "INT") return seed ? "seed" : "int";
  if (base === "FLOAT") return "float";
  if (base === "STRING") return "string";
  if (base === "BOOLEAN") return "bool";
  if (base === "COMBO") return "combo";
  switch (widget.type) {
    case "number": return integerOptions(widget) ? (seed ? "seed" : "int") : "float";
    case "combo": return "combo";
    case "toggle": case "boolean": return "bool";
    case "text": case "customtext": case "string": return "string";
    default: return null;
  }
}

function linked(node, widget) {
  return node.inputs?.some(input => input?.widget?.name === widget.name && input.link != null) === true;
}

function slots(list) {
  if (!Array.isArray(list)) return [];
  return list.slice(0, MAX_SLOTS).map(slot => ({ name: cut(slot?.name ?? "")[0], type: cut(slot?.type ?? "*")[0] }));
}

function step(widget, kind) {
  const o = widget.options ?? {};
  const value = o.step2 ?? (Number.isFinite(o.step) ? o.step / 10 : undefined);
  return Number.isFinite(value) && value > 0 ? (kind === "float" ? value : Math.max(1, Math.round(value))) : undefined;
}

// One widget's projection, or null.
function projectWidget(node, widget, inline) {
  if (typeof widget?.name !== "string" || !widget.name) return null;
  const kind = widgetKind(node, widget);
  if (!kind) return null;
  const row = { name: cut(widget.name)[0], type: kind };
  let value = widget.value;
  if (kind === "int" || kind === "seed") {
    if (!Number.isSafeInteger(value)) return null;
  } else if (kind === "float") {
    if (!Number.isFinite(value)) return null;
  } else if (kind === "bool") {
    if (typeof value !== "boolean") return null;
  } else {
    if (value === undefined || value === null) value = "";
    if (typeof value !== "string" && !(kind === "combo" && Number.isFinite(value))) return null;
    if (typeof value === "string") {
      const [text, truncated] = cut(value);
      value = text;
      if (truncated) row.truncated = true;
    }
  }
  row.value = value;
  if (kind === "int" || kind === "seed" || kind === "float") {
    const o = widget.options ?? {};
    if (Number.isFinite(o.min)) row.min = o.min;
    if (Number.isFinite(o.max)) row.max = o.max;
    const s = step(widget, kind);
    if (s !== undefined) row.step = s;
  }
  if (kind === "combo") {
    const values = comboValues(widget, node) ?? [];
    if (inline && values.length <= MAX_CHOICES) row.choices = values.map(choice => typeof choice === "number" ? choice : cut(choice)[0]);
    else row.choices_count = values.length;
  }
  if (linked(node, widget)) row.linked = true;
  return row;
}

function projectNode(node, inline) {
  const [title, truncated] = cut(node.title ?? node.type ?? "");
  const row = { id: String(node.id), type: cut(node.comfyClass ?? node.type ?? "")[0], title,
    mode: MODES[node.mode ?? 0] ?? "always", widgets: [], inputs: slots(node.inputs), outputs: slots(node.outputs) };
  if (truncated) row.truncated = true;
  const names = new Set();
  for (const widget of node.widgets ?? []) {
    const projected = projectWidget(node, widget, inline);
    if (!projected || names.has(projected.name)) continue;
    names.add(projected.name);
    row.widgets.push(projected);
  }
  return row;
}

function bounds(item) {
  const value = item?._bounding ?? item?.bounding;
  return value && value.length >= 4 && [0, 1, 2, 3].every(i => Number.isFinite(value[i])) ? value : null;
}

function projectGroups(graph, nodes) {
  const groups = graph._groups ?? graph.groups;
  if (!Array.isArray(groups)) return [];
  return groups.slice(0, MAX_NODES).map(group => {
    const box = bounds(group), members = [];
    if (box) for (const node of nodes) {
      const pos = node.pos, size = node.size;
      if (!pos || !size || ![pos[0], pos[1], size[0], size[1]].every(Number.isFinite)) continue;
      const x = pos[0] + size[0] / 2, y = pos[1] + size[1] / 2;
      if (x >= box[0] && y >= box[1] && x <= box[0] + box[2] && y <= box[1] + box[3]) members.push(String(node.id));
    }
    return { title: cut(group.title ?? "")[0], members };
  });
}

// The whole bounded projection of the root graph, or null past MAX_GRAPH_NODES.
export function projectGraph(graph, inline = true) {
  const all = graphNodes(graph);
  if (!all || all.length > MAX_GRAPH_NODES) return null;
  const sorted = all.filter(node => /^(0|[1-9][0-9]{0,15})$/.test(String(node?.id)))
    .sort((a, b) => Number(a.id) - Number(b.id));
  const kept = sorted.slice(0, MAX_NODES), ids = new Set(kept.map(node => String(node.id)));
  const projected = { nodes: kept.map(node => projectNode(node, inline)), links: [], groups: projectGroups(graph, kept) };
  for (const link of graphLinks(graph)) {
    const from = String(link?.origin_id), to = String(link?.target_id);
    if (!ids.has(from) || !ids.has(to) || !Number.isSafeInteger(link.origin_slot) || !Number.isSafeInteger(link.target_slot)) continue;
    if (projected.links.length === MAX_LINKS) { projected.truncated = true; break; }
    projected.links.push({ from, from_slot: link.origin_slot, to, to_slot: link.target_slot });
  }
  projected.links.sort((a, b) => Number(a.to) - Number(b.to) || a.to_slot - b.to_slot);
  if (sorted.length > kept.length) projected.truncated = true;
  return projected;
}

// A person's own edit of a widget: the value, the widget's callback and the
// node's change hook, in the order ComfyUI's frontend applies them.
function writeWidget(node, widget, value) {
  const previous = widget.value;
  widget.value = value;
  widget.callback?.(value);
  node.onWidgetChanged?.(widget.name, value, previous, widget);
}

// One operation checked against the live node and widget: the change to
// make, or the reason it is refused.
function check(graph, op, index) {
  const refuse = reason => ({ refused: `ops[${index}]: ${reason}` });
  if (!plain(op) || typeof op.op !== "string") return refuse("an operation is an object with op");
  const node = typeof op.node === "string" && /^(0|[1-9][0-9]{0,15})$/.test(op.node) ? graph.getNodeById(Number(op.node)) : null;
  if (!node || node.graph !== graph) return refuse(`node ${JSON.stringify(op.node)} is not on the canvas`);
  if (op.op === "set_mode") {
    if (!exact(op, ["op", "node", "mode"]) || !(op.mode in MODE_VALUES)) return refuse("set_mode takes node and mode: always, bypass or mute");
    return { apply: () => { const mode = MODE_VALUES[op.mode]; if (typeof node.changeMode === "function") node.changeMode(mode); else node.mode = mode; },
      check: () => (node.mode ?? 0) === MODE_VALUES[op.mode] };
  }
  if (op.op !== "set_widget") return refuse(`op ${JSON.stringify(op.op)} is not set_widget or set_mode`);
  if (!exact(op, ["op", "node", "widget", "value"]) || typeof op.widget !== "string") return refuse("set_widget takes node, widget and value");
  const found = (node.widgets ?? []).filter(w => w?.name === op.widget);
  const widget = found[0], kind = widget ? widgetKind(node, widget) : null;
  if (found.length !== 1 || !kind) return refuse(`node ${op.node} has no widget ${JSON.stringify(op.widget)}`);
  if (linked(node, widget)) return refuse(`widget ${op.widget} takes its value from a link`);
  const value = op.value, o = widget.options ?? {};
  const range = () => (!Number.isFinite(o.min) || value >= o.min) && (!Number.isFinite(o.max) || value <= o.max);
  const limits = `${Number.isFinite(o.min) ? o.min : "-∞"} to ${Number.isFinite(o.max) ? o.max : "∞"}`;
  if (kind === "int" || kind === "seed") {
    if (!Number.isSafeInteger(value)) return refuse(`${op.widget} takes a whole number`);
    if (!range()) return refuse(`${op.widget} takes ${limits}`);
  } else if (kind === "float") {
    if (typeof value !== "number" || !Number.isFinite(value)) return refuse(`${op.widget} takes a number`);
    if (!range()) return refuse(`${op.widget} takes ${limits}`);
  } else if (kind === "bool") {
    if (typeof value !== "boolean") return refuse(`${op.widget} takes true or false`);
  } else if (kind === "string") {
    if (typeof value !== "string") return refuse(`${op.widget} takes text`);
  } else {
    const values = comboValues(widget, node);
    if (!values || !values.includes(value)) return refuse(`${op.widget} has no choice ${JSON.stringify(value)}; graph.choices lists them`);
  }
  return { apply: () => writeWidget(node, widget, value), check: () => widget.value === value };
}

// owners: queueState() answers the frontend's queue settings store or null
// when this frontend does not expose it; api is ComfyUI's api object, whose
// queuePrompt the page's own Queue calls.
export function graphView(app, owners) {
  let epoch = null, sequence = 0, view = null;
  let status = "unavailable";
  // The workflow store is optional: without it no workflow is active or busy.
  const workflow = () => app.extensionManager?.workflow;
  const activeWorkflow = () => workflow()?.activeWorkflow ?? null;
  const available = () => epoch && workflow()?.isBusy !== true && rootGraph(app);
  function encode(graph) {
    let projected = projectGraph(graph, true), raw = projected && JSON.stringify(projected);
    if (raw && new TextEncoder().encode(raw).length > BUDGET) {
      projected = projectGraph(graph, false); raw = JSON.stringify(projected);
    }
    return projected && new TextEncoder().encode(raw).length <= BUDGET ? [projected, raw] : [null, null];
  }
  function capture(advance = true) {
    const graph = available();
    if (!graph) { status = "unavailable"; view = null; return null; }
    const active = activeWorkflow();
    const [projected, fingerprint] = encode(graph);
    if (!projected) { status = "projection_exhausted"; view = null; return null; }
    if (advance && view && (view.graph !== graph || view.active !== active || view.fingerprint !== fingerprint)) sequence++;
    if (!Number.isSafeInteger(sequence)) { epoch = null; view = null; return null; }
    view = { graph, active, fingerprint };
    status = "ready";
    return { epoch, sequence, graph: projected };
  }
  // The request's view is this page's current one: same epoch and, for
  // operations that depend on what the agent read, the same sequence. An
  // unobserved edit anywhere in the graph moves the sequence first.
  function current(request, strict) {
    const graph = available();
    if (!view || !graph || request.epoch !== epoch || view.graph !== graph || view.active !== activeWorkflow()) return null;
    const seq = sequence;
    capture();
    if (!view || (strict && (request.sequence !== seq || sequence !== seq))) return null;
    return graph;
  }
  const showsRoot = graph => app.canvas?.graph === graph;
  function edit(graph, ops) {
    if (!Array.isArray(ops) || ops.length < 1 || ops.length > MAX_OPS) return { outcome: "invalid", answer: { reason: `ops holds 1 to ${MAX_OPS} operations` } };
    const changes = [];
    for (const [index, op] of ops.entries()) {
      const change = check(graph, op, index);
      if (change.refused) return { outcome: "invalid", answer: { reason: change.refused } };
      changes.push(change);
    }
    // ComfyUI's queue settings: with auto queue on, an edit would start a run.
    // A frontend without the store keeps editing.
    const queue = owners.queueState?.();
    if ((queue && queue.mode !== "disabled") || app.ui?.autoQueueEnabled === true)
      return { outcome: "refused", answer: { reason: "ComfyUI's auto queue is on, so an edit would start a run; the person turns it off in the Queue button's menu" } };
    // One undo step and one graphChanged notice, where the frontend tracks
    // changes; LiteGraph's own change bracket otherwise.
    const tracker = activeWorkflow()?.changeTracker;
    const bracket = typeof tracker?.beforeChange === "function" && typeof tracker?.afterChange === "function" ? tracker
      : typeof graph.beforeChange === "function" && typeof graph.afterChange === "function" ? graph : null;
    try {
      bracket?.beforeChange();
      try { for (const change of changes) change.apply(); }
      finally { bracket?.afterChange(); }
      if (!changes.every(change => change.check())) throw Error("readback differs");
    } catch {
      // A callback may already have changed the application. Never retry.
      view = null;
      return { outcome: "unknown" };
    }
    try { app.canvas?.setDirty?.(true, true); } catch { /* redraw only */ }
    return { outcome: "applied", projection: capture(), answer: { applied: changes.length } };
  }
  function choices(graph, args) {
    if (!exact(args, ["node", "widget", "query", "cursor"]) || typeof args.node !== "string" || typeof args.widget !== "string" ||
        typeof args.query !== "string" || typeof args.cursor !== "string" || !/^(0|[1-9][0-9]{0,8})?$/.test(args.cursor))
      return { outcome: "invalid", answer: { reason: "choices takes node, widget, query and cursor" } };
    const node = /^(0|[1-9][0-9]{0,15})$/.test(args.node) ? graph.getNodeById(Number(args.node)) : null;
    const widget = node?.graph === graph ? (node.widgets ?? []).find(w => w?.name === args.widget) : null;
    if (!widget || widgetKind(node, widget) !== "combo") return { outcome: "invalid", answer: { reason: `node ${args.node} has no combo widget ${JSON.stringify(args.widget)}` } };
    const query = args.query.toLowerCase();
    const all = (comboValues(widget, node) ?? []).filter(choice => String(choice).toLowerCase().includes(query));
    const start = Number(args.cursor || "0"), page = all.slice(start, start + MAX_CHOICES).map(choice => typeof choice === "number" ? choice : cut(choice)[0]);
    const next = start + MAX_CHOICES < all.length ? String(start + MAX_CHOICES) : "";
    return { outcome: "applied", answer: { choices: page, cursor: next, total: all.length } };
  }
  // The canvas's own Queue: app.queuePrompt with the page's client, so the
  // person sees the progress on the canvas. The prompt ids come from the
  // api.queuePrompt calls it makes while it runs.
  async function run() {
    const api = owners.api;
    if (typeof app.queuePrompt !== "function" || typeof api?.queuePrompt !== "function")
      return { outcome: "refused", answer: { reason: "This ComfyUI frontend has no Queue OpenAbstractions can use" } };
    const prompts = [], errors = [];
    const own = Object.prototype.hasOwnProperty.call(api, "queuePrompt"), original = api.queuePrompt;
    api.queuePrompt = async function (...args) {
      try {
        const answer = await original.apply(this, args);
        if (token(answer?.prompt_id)) prompts.push(answer.prompt_id);
        return answer;
      } catch (error) {
        const detail = error?.response?.error?.message ?? error?.message ?? "the prompt was refused";
        errors.push(cut(detail)[0].slice(0, 400));
        throw error;
      }
    };
    try { await app.queuePrompt(0, 1); }
    catch (error) { if (!errors.length) errors.push(cut(error?.message ?? "the queue failed")[0].slice(0, 400)); }
    finally { if (own) api.queuePrompt = original; else delete api.queuePrompt; }
    if (!prompts.length)
      return { outcome: "refused", answer: { reason: errors[0] ?? "ComfyUI queued nothing; the canvas may have no output node" } };
    return { outcome: "applied", projection: capture(), answer: { prompts } };
  }
  return {
    get status() { return status; },
    get active() { return epoch !== null; },
    get epoch() { return epoch; },
    get sequence() { return sequence; },
    retire() { epoch = null; view = null; sequence = 0; status = "unavailable"; },
    loaded(id) {
      if (!token(id)) throw Error("invalid epoch");
      epoch = id; view = null; sequence = 0;
      return capture();
    },
    changed() { return capture(); },
    capture,
    // request: {operation, epoch, sequence, arguments}. The answer is
    // {outcome, projection?, answer?}, or a promise of it for run.
    execute(request) {
      if (!exact(request, ["operation", "epoch", "sequence", "arguments"]) || !token(request.epoch) ||
          !Number.isSafeInteger(request.sequence) || request.sequence < 0 || !plain(request.arguments) ||
          !["reveal", "edit", "choices", "run"].includes(request.operation))
        return { outcome: "invalid" };
      const strict = request.operation === "edit" || request.operation === "run";
      const graph = current(request, strict);
      if (!graph) return { outcome: "stale" };
      const args = request.arguments;
      if (request.operation === "reveal") {
        const node = typeof args.node === "string" && /^(0|[1-9][0-9]{0,15})$/.test(args.node) ? graph.getNodeById(Number(args.node)) : null;
        if (!exact(args, ["node"]) || !node || node.graph !== graph) return { outcome: "invalid" };
        if (typeof app.canvas?.centerOnNode !== "function") return { outcome: "unavailable" };
        if (!showsRoot(graph)) return { outcome: "refused" };
        app.canvas.centerOnNode(node);
        return { outcome: "applied" };
      }
      if (request.operation === "choices") return choices(graph, args);
      if (request.operation === "edit") {
        if (!exact(args, ["ops"])) return { outcome: "invalid", answer: { reason: "edit takes ops" } };
        return edit(graph, args.ops);
      }
      if (!exact(args, [])) return { outcome: "invalid" };
      return run();
    }
  };
}
