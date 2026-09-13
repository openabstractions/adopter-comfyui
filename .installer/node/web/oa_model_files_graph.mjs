// The model files OpenAbstractions picked for a workflow, applied to the
// workflow's data as it loads (MODEL-AND-ENGINE.md, Step 1). The server route
// (oa_model_files.py) answers per loader node; this module writes the answer
// into a copy of the workflow. The copy carries the file OA holds, the loader
// class for a listed swap whose target this ComfyUI has, and the title line
// "file from OA: <variant>". Any failure leaves the workflow as authored.
// Pure functions over workflow JSON; oa_model_files.js wires them to ComfyUI.

export const ROUTE = "/openabstractions/models/resolve";
export const TIMEOUT_MS = 10000;
const TITLE_MARK = " · file from OA: ";
const MAX_WIDGETS = 8;
const primitive = value => ["string", "number", "boolean"].includes(typeof value);
const fileName = value => typeof value === "string" && value.length > 0 && value.length <= 512 &&
  !value.split(/[\\/]/).includes("..");

function* scopes(graph) {
  if (Array.isArray(graph?.nodes)) yield ["root", graph.nodes];
  for (const sub of Array.isArray(graph?.definitions?.subgraphs) ? graph.definitions.subgraphs : []) {
    if (sub && typeof sub.id === "string" && Array.isArray(sub.nodes)) yield [sub.id, sub.nodes];
  }
}

function* entries(graph) {
  for (const [scope, nodes] of scopes(graph)) {
    for (const node of nodes) {
      if (node && typeof node === "object" && typeof node.type === "string") yield [`${scope}:${node.id}`, node, scope];
    }
  }
}

// A subgraph's links as {id, origin_id, origin_slot}, from either stored shape.
function link(sub, id) {
  for (const l of Array.isArray(sub?.links) ? sub.links : []) {
    const found = Array.isArray(l) ? { id: l[0], origin_id: l[1], origin_slot: l[2] } : l;
    if (found?.id === id) return found;
  }
  return null;
}

// The subgraph input slot that feeds a node's widget input, or -1. ComfyUI
// templates promote a loader's file widget to the subgraph node; the value
// the person sees and ComfyUI runs is the subgraph node's widget value.
function promotedSlot(graph, scope, node, widget) {
  if (scope === "root") return -1;
  const sub = graph.definitions.subgraphs.find(s => s?.id === scope);
  const input = (Array.isArray(node.inputs) ? node.inputs : []).find(i => i?.name === widget);
  const found = input?.link != null ? link(sub, input.link) : null;
  return found && found.origin_id === -10 && Number.isInteger(found.origin_slot) ? found.origin_slot : -1;
}

// Rewrite the promoted value on every node of the subgraph's type whose value
// is still the authored file. False when a node's value cannot be placed.
function rewritePromoted(graph, scope, slot, authored, file) {
  const count = graph.definitions.subgraphs.find(s => s?.id === scope)?.inputs?.length;
  const changes = [];
  for (const [, node] of entries(graph)) {
    if (node.type !== scope || !Array.isArray(node.widgets_values)) continue;
    const values = node.widgets_values;
    let index = values.length === count && values[slot] === authored ? slot : -1;
    if (index < 0) {
      const matches = values.flatMap((v, i) => v === authored ? [i] : []);
      if (matches.length > 1) return false;
      index = matches.length ? matches[0] : -1;
    }
    if (index >= 0) changes.push([values, index]);
  }
  for (const [values, index] of changes) values[index] = file;
  return true;
}

// The nodes the route resolves: every node whose first widget is a text and
// whose properties list models, or whose type names a loader.
export function loaderNodes(graph) {
  const out = [];
  for (const [key, node] of entries(graph)) {
    const values = node.widgets_values;
    const models = Array.isArray(node.properties?.models) ? node.properties.models : [];
    if (!Array.isArray(values) || typeof values[0] !== "string") continue;
    if (!models.length && !/Loader/.test(node.type)) continue;
    out.push({ key, type: node.type, value: values[0], widgets_values: values,
      models: models.filter(m => m && typeof m === "object").map(({ name, url, directory }) => ({ name, url, directory })) });
  }
  return out;
}

function titled(node, line) {
  const base = String(node.title ?? node.type).split(TITLE_MARK)[0];
  return base + TITLE_MARK + line.slice("file from OA: ".length);
}

// The workflow with the answer's resolutions applied, or the workflow itself
// when nothing applies. hasType(name) says whether this ComfyUI has a node
// class. A type change applies only for a swap pair the answer lists, from
// the node's own type, to a class this ComfyUI has.
export function applyResolutions(graph, answer, hasType) {
  const resolutions = Array.isArray(answer?.resolutions) ? answer.resolutions : [];
  const swaps = new Set((Array.isArray(answer?.swaps) ? answer.swaps : [])
    .filter(s => typeof s?.from === "string" && typeof s?.to === "string").map(s => `${s.from}\u0000${s.to}`));
  const wanted = new Map();
  for (const r of resolutions) {
    if (r?.from_oa === true && typeof r.key === "string" && fileName(r.file) && typeof r.type === "string" &&
        typeof r.title === "string" && r.title.startsWith("file from OA: ") && r.title.length <= 200 &&
        Array.isArray(r.widgets_values) && r.widgets_values.length <= MAX_WIDGETS &&
        r.widgets_values.every(primitive) && r.widgets_values.includes(r.file)) wanted.set(r.key, r);
  }
  if (!wanted.size) return { graph, applied: [] };
  const copy = structuredClone(graph);
  const applied = [];
  for (const [key, node, scope] of entries(copy)) {
    const r = wanted.get(key);
    if (!r) continue;
    const authored = Array.isArray(node.widgets_values) ? node.widgets_values[0] : undefined;
    const slot = typeof r.file_widget === "string" ? promotedSlot(copy, scope, node, r.file_widget) : -1;
    const inputs = Array.isArray(node.inputs) ? node.inputs : [];
    const keep = new Set(Array.isArray(r.inputs) ? r.inputs.filter(n => typeof n === "string") : []);
    if (r.type !== node.type && (!swaps.has(`${node.type}\u0000${r.type}`) || !hasType(r.type) ||
        inputs.some(input => !keep.has(input?.name) && input?.link != null))) continue;
    if (slot >= 0 && authored !== r.file && !rewritePromoted(copy, scope, slot, authored, r.file)) continue;
    node.title = titled(node, r.title);
    if (r.type !== node.type) {
      node.inputs = inputs.filter(input => keep.has(input?.name));
      node.type = r.type;
      if (node.properties && "Node name for S&R" in node.properties) node.properties["Node name for S&R"] = r.type;
    }
    node.widgets_values = [...r.widgets_values];
    if (authored !== r.file && node.properties) node.properties.models = [];
    applied.push({ key, file: r.file, type: node.type });
  }
  return applied.length ? { graph: copy, applied } : { graph, applied };
}

// Ask the route and apply its answer. Returns the workflow to load: the
// rewritten copy, or the workflow as authored on any failure (one warning).
export async function resolveGraph(graph, fetchApi, hasType, log = console) {
  try {
    const nodes = loaderNodes(graph);
    if (!nodes.length) return graph;
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), TIMEOUT_MS);
    let response;
    try {
      response = await fetchApi(ROUTE, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ nodes }), signal: abort.signal });
    } finally {
      clearTimeout(timer);
    }
    if (!response?.ok) throw Error(`the route answered ${response?.status}`);
    const { graph: out, applied } = applyResolutions(graph, await response.json(), hasType);
    if (applied.length) log.info?.(`[openabstractions] ${applied.length} model file(s) from OpenAbstractions`);
    return out;
  } catch (error) {
    log.warn?.(`[openabstractions] model files stay as authored: ${error?.message ?? error}`);
    return graph;
  }
}

// app.loadGraphData wrapped so a workflow that opens loads with OA's files.
// ComfyUI's frontend (1.58) builds the canvas from the data it is given and
// resets the workflow's change tracker to that graph, which leaves the
// workflow unmodified: the rewrite reaches the canvas and every run, and the
// template or saved file changes only when the person saves. A reload with
// clean === false (undo, redo, the reroute migration) passes through as given.
export function wrapLoadGraphData(original, fetchApi, hasType, log = console) {
  if (typeof original !== "function" || original.openabstractionsModelFiles) return original;
  const wrapped = async function (graph, ...rest) {
    const data = graph && typeof graph === "object" && rest[0] !== false
      ? await resolveGraph(graph, fetchApi, hasType, log)
      : graph;
    return original.call(this, data, ...rest);
  };
  wrapped.openabstractionsModelFiles = true;
  return wrapped;
}
