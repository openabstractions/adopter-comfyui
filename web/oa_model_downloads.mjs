// The missing-model dialog's downloads through OpenAbstractions.
//
// The frontend downloads a missing model itself: in Comfy Desktop through
// window.__comfyDesktop2.downloadModel, in a browser through an <a download>
// click that saves into the person's Downloads folder. This module takes the
// dialog's Download and Download all clicks (data-testid
// "missing-model-download" and "missing-model-download-all", frontend
// 1.58.5) and an <a download> click whose URL a workflow lists, and asks the
// node's route to deliver the model into its ComfyUI folder through OA.
// Every click it cannot place, and every model OA declines or fails, goes on
// to the frontend's own handler unchanged.

export const ROUTE = "/openabstractions/models/download";
export const ROW = "missing-model-download";
export const ALL = "missing-model-download-all";
export const SECTION = "error-group-missing-model";
// The frontend's own rule (isModelDownloadable in frontend 1.58.5).
export const ALLOWED = ["https://huggingface.co/", "https://civitai.com/", "https://civitai.red/"];
export const EXTENSIONS = [".safetensors", ".sft", ".ckpt", ".pth", ".pt"];

// The dialog matches the final suffix of the advertised model name.
function extname(name) {
  const dot = name.lastIndexOf(".");
  return dot < 0 ? "" : name.slice(dot);
}

export function downloadable(model) {
  return !!model && typeof model.url === "string" && typeof model.name === "string"
    && typeof model.directory === "string" && model.directory !== ""
    && ALLOWED.some((prefix) => model.url.startsWith(prefix))
    && EXTENSIONS.includes(extname(model.name).toLowerCase());
}

function normalized(url) {
  try { return new URL(url).href; } catch { return url; }
}

// The models a workflow lists: its own "models" and every node's
// properties.models, subgraph definitions included.
export function workflowModels(graphData) {
  const found = [];
  const add = (list) => {
    for (const model of Array.isArray(list) ? list : []) {
      if (downloadable(model)) found.push(model);
    }
  };
  const nodes = (list) => {
    for (const node of Array.isArray(list) ? list : []) add(node?.properties?.models);
  };
  if (graphData && typeof graphData === "object") {
    add(graphData.models);
    nodes(graphData.nodes);
    for (const subgraph of graphData.definitions?.subgraphs ?? []) nodes(subgraph?.nodes);
  }
  return found;
}

export class Catalog {
  constructor() { this.byName = new Map(); this.byUrl = new Map(); }
  remember(models) {
    for (const model of models) {
      const entry = { url: model.url, name: model.name, directory: model.directory };
      if (typeof model.hash === "string" && typeof model.hash_type === "string") {
        entry.hash = model.hash;
        entry.hash_type = model.hash_type;
      }
      this.byName.set(model.name, entry);
      this.byUrl.set(normalized(model.url), entry);
    }
  }
  forUrl(url) { return this.byUrl.get(normalized(url)); }
  // The model a row's Download button names: its aria-label carries the
  // model's name, and the longest listed name inside it wins.
  forLabel(label) {
    let best;
    for (const [name, entry] of this.byName) {
      if (typeof label === "string" && label.includes(name) && (!best || name.length > best.name.length)) best = entry;
    }
    return best;
  }
  // The listed models whose names the missing-model section shows.
  shownIn(text) {
    return [...this.byName.values()].filter((entry) => typeof text === "string" && text.includes(entry.name));
  }
}

// The models one captured click names, or [] when it names none this module
// can place.
export function clickedModels(catalog, button, sectionText) {
  const id = button?.getAttribute?.("data-testid");
  if (id === ROW) {
    const entry = catalog.forLabel(button.getAttribute("aria-label") ?? button.textContent ?? "");
    return entry ? [entry] : [];
  }
  if (id === ALL) return catalog.shownIn(sectionText ?? "");
  return [];
}

// Asks the route for each model. fallback(model) hands one model to the
// frontend's own downloader. Returns once every model arrived or fell back.
export async function deliver(models, { fetch, fallback, notify, refresh, sleep, pollMs = 2000 }) {
  let handedBack = 0;
  const giveBack = async (model, why) => {
    handedBack += 1;
    notify?.("info", "Downloading without OpenAbstractions", `${model.name}: ${why}`);
    await fallback(model);
  };
  await Promise.all(models.map(async (model) => {
    let answer;
    try {
      const response = await fetch(ROUTE, {
        method: "POST", cache: "no-store",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(model),
      });
      answer = await response.json();
    } catch (error) {
      return giveBack(model, "the OpenAbstractions route did not answer");
    }
    if (answer?.outcome === "present") return undefined;
    if (answer?.outcome !== "accepted") return giveBack(model, answer?.error ?? "not taken");
    notify?.("info", "Downloading through OpenAbstractions", model.name);
    for (;;) {
      await sleep(pollMs);
      let state;
      try {
        state = await (await fetch(`${ROUTE}/${encodeURIComponent(answer.id)}`, { cache: "no-store" })).json();
      } catch {
        continue;
      }
      if (state?.state === "running") continue;
      if (state?.state === "complete") {
        notify?.("success", "Downloaded", model.name);
        refresh?.();
        return undefined;
      }
      if (state?.state === "waiting") {
        notify?.("info", "Still downloading in OpenAbstractions", model.name);
        return undefined;
      }
      return giveBack(model, state?.error || state?.state || "failed");
    }
  }));
  return handedBack;
}

// Wires the capture: a capture-phase click listener on the dialog's buttons
// and a guard on HTMLAnchorElement.prototype.click. host supplies document,
// the anchor prototype, fetch, notify, refresh and sleep, and collect, which
// adds the open graph's models to the catalog before a click is matched.
export function install(catalog, host) {
  const replaying = new WeakSet();
  const anchorsPassing = new WeakSet();
  // URLs a replayed row click hands to the frontend: in a browser its handler
  // clicks an <a download> for the URL, which the guard lets through once.
  const urlsPassing = new Set();
  const original = host.anchorPrototype.click;

  const replay = (button, model) => {
    urlsPassing.add(normalized(model.url));
    const event = new host.MouseEvent("click", { bubbles: true, cancelable: true });
    replaying.add(event);
    button.dispatchEvent(event);
  };
  const vendor = (model) => {
    const desktop = host.desktop?.();
    if (desktop?.downloadModel && !(desktop.isRemote?.() ?? false)) {
      return Promise.resolve(desktop.downloadModel(model.url, model.name, model.directory)).catch(() => {});
    }
    const anchor = host.document.createElement("a");
    anchor.href = model.url;
    anchor.download = model.name;
    anchor.target = "_blank";
    anchor.rel = "noopener noreferrer";
    anchorsPassing.add(anchor);
    original.call(anchor);
    return undefined;
  };
  const options = (fallback) => ({ fetch: host.fetch, notify: host.notify, refresh: host.refresh, sleep: host.sleep, fallback });

  host.document.addEventListener("click", (event) => {
    if (replaying.has(event)) return;
    const button = event.target?.closest?.(`[data-testid="${ROW}"],[data-testid="${ALL}"]`);
    if (!button) return;
    host.collect?.();
    const section = host.document.querySelector(`[data-testid="${SECTION}"]`);
    const models = clickedModels(catalog, button, section?.textContent);
    if (models.length === 0) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    const single = button.getAttribute("data-testid") === ROW;
    // A row's own click goes back to the frontend's handler; a model of
    // Download all goes to the frontend's downloader alone.
    deliver(models, options(single ? (model) => replay(button, model) : vendor));
  }, true);

  host.anchorPrototype.click = function click() {
    if (urlsPassing.delete(normalized(this.href))) return original.call(this);
    if (!anchorsPassing.has(this) && this.hasAttribute?.("download")) {
      host.collect?.();
      const model = catalog.forUrl(this.href);
      if (model && downloadable(model)) {
        const anchor = this;
        deliver([model], options(() => { anchorsPassing.add(anchor); original.call(anchor); }));
        return undefined;
      }
    }
    return original.call(this);
  };
  return { replaying, anchorsPassing };
}
