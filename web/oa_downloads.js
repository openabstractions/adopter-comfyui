// "OpenAbstractions": this ComfyUI's picture workflows and download operations.
//
// The tab reads GET /abstraction/jobs, the route the node serves when
// ABSTRACTION_COMFYUI_INVENTORY=service, and lists one row per operation:
// the model and where it lands, its state and progress, whether delivered bytes
// were reused from a store or downloaded, and the reason a failed one failed. A
// repository is one row holding its files. Retry and Cancel POST to that row's
// action under the same route.
//
// "Reused from <store>" means the runtime already held those bytes and an
// authorized read delivered them with no source request. It says nothing about
// how the store keeps them on disk.
//
// Refreshing is bounded and stops while the tab is not on screen. Plain
// JavaScript, no build step.

import { app } from "../../scripts/app.js";

const ROUTE = "/abstraction/jobs";
const WORKFLOW = "/abstraction/workflows/flux2-klein-distilled";
const IMAGE_RECIPE = "/abstraction/workflows/oa-image-generate";
const RECIPE_ID = "flux2-klein-distilled-v1";
const INTERVAL_MS = 4000;
const SWITCH = "ABSTRACTION_COMFYUI_INVENTORY=service";

const STATE_WORDS = {
  pending: "Waiting",
  running: "In progress",
  transferred: "Transferred",
  complete: "Complete",
  failed: "Failed",
  cancelled: "Cancelled",
  unknown: "Status unknown",
};

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function style() {
  if (document.getElementById("oa-downloads-style")) return;
  const sheet = element("style");
  sheet.id = "oa-downloads-style";
  sheet.textContent = `
.oa-downloads { padding: 8px; font-size: 12px; line-height: 1.45; overflow-y: auto; height: 100%; }
.oa-downloads h3 { margin: 4px 0 8px; font-size: 13px; font-weight: 600; }
.oa-note { opacity: 0.75; margin: 6px 0; }
.oa-row { border: 1px solid rgba(128,128,128,0.35); border-radius: 6px; padding: 8px; margin-bottom: 8px; }
.oa-row.oa-child { margin: 6px 0 0 12px; padding: 6px 8px; }
.oa-title { font-weight: 600; word-break: break-all; }
.oa-line { opacity: 0.8; word-break: break-all; }
.oa-path { opacity: 0.8; margin-top: 4px; }
.oa-path summary { cursor: pointer; }
.oa-path code { display: block; overflow-wrap: anywhere; margin-top: 4px; }
.oa-bar { height: 4px; border-radius: 2px; background: rgba(128,128,128,0.3); margin: 6px 0; overflow: hidden; }
.oa-bar > div { height: 100%; background: currentColor; }
.oa-state { text-transform: none; }
.oa-failed { color: #e06c6c; }
.oa-complete { color: #5fbf7f; }
.oa-actions { margin-top: 6px; display: flex; gap: 6px; flex-wrap: wrap; }
.oa-actions button { font: inherit; padding: 2px 8px; cursor: pointer; }
.oa-actions button[disabled] { opacity: 0.5; cursor: default; }
.oa-recipe { border: 1px solid rgba(128,128,128,0.5); border-radius: 6px; padding: 8px; margin-bottom: 12px; }
.oa-recipe h4 { font-size: 13px; margin: 0 0 6px; }
.oa-recipe .oa-asset { margin: 6px 0; }
.oa-recipe .oa-asset-name { font-weight: 600; overflow-wrap: anywhere; }
.oa-recipe .oa-provenance { margin-top: 6px; overflow-wrap: anywhere; }
.oa-recipe details { margin-top: 6px; }
.oa-recipe details .oa-line { margin: 4px 0; overflow-wrap: anywhere; }
.oa-recipe a { color: inherit; text-decoration: underline; }
`;
  document.head.appendChild(sheet);
}

async function readJobs(since, signal) {
  const query = since ? `${ROUTE}?since=${encodeURIComponent(since)}` : ROUTE;
  const response = await fetch(query, { cache: "no-store", signal });
  if (response.status === 404) {
    // The node always registers this route (see oa_inventory.py), so a 404
    // carries the one explanation for whichever status kept it from serving:
    // switched off, misconfigured, or the OA SDK unavailable. The fallback
    // text below is for the one case that route cannot itself explain: no OA
    // node answered this path at all.
    const body = await response.json().catch(() => null);
    return { missing: true, detail: body && typeof body.detail === "string" ? body.detail : "" };
  }
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    return { error: (body && (body.error || body.outcome)) || `the route answered ${response.status}` };
  }
  return body || { error: "the route answered no JSON" };
}

async function act(row, action) {
  const response = await fetch(`${ROUTE}/${encodeURIComponent(row.id)}/${action}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ identities: selectedIdentities(row) }),
  });
  const body = await response.json().catch(() => null);
  if (body && body.outcome) return body;
  return { outcome: `the route answered ${response.status}` };
}

function validPlan(plan) {
  return plan && plan.id === RECIPE_ID && /^[a-f0-9]{64}$/i.test(plan.manifest_sha256 || "") &&
    typeof plan.title === "string" && plan.template && typeof plan.template.name === "string" &&
    typeof plan.template.revision === "string" && httpsSource(plan.template.url) &&
    plan.template.variant === "distilled-fp8" &&
    Array.isArray(plan.assets) && plan.assets.length === 3 && plan.assets.every((asset) =>
      typeof asset.role === "string" && typeof asset.filename === "string" &&
      typeof asset.folder === "string" && typeof asset.repository === "string" &&
      typeof asset.revision === "string" && httpsSource(asset.url) &&
      Number.isSafeInteger(asset.size_bytes) && asset.size_bytes > 0 &&
      /^[a-f0-9]{64}$/i.test(asset.sha256 || "") && asset.license &&
      typeof asset.license.id === "string" && httpsSource(asset.license.url) &&
      ["missing", "present_unverified", "ready", "invalid"].includes(asset.state)) && typeof plan.ready === "boolean" &&
    (!plan.ready || plan.assets.every((asset) => asset.state === "ready"));
}

function httpsSource(value) {
  try {
    return typeof value === "string" && new URL(value).protocol === "https:";
  } catch {
    return false;
  }
}

function sourceLink(parent, label, url) {
  let safe;
  try {
    safe = new URL(url);
  } catch {
    // A malformed source never becomes a clickable link.
  }
  if (!safe || safe.protocol !== "https:") {
    parent.appendChild(document.createTextNode(`${label}: ${url}`));
    return;
  }
  const link = element("a", null, label);
  link.href = safe.href;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  parent.appendChild(link);
}

function sizeText(bytes) {
  return `${(bytes / (1024 ** 3)).toFixed(2)} GiB`;
}

async function sha256(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function roleName(role) {
  return {
    diffusion_model: "Diffusion model", diffusion_models: "Diffusion model",
    text_encoder: "Text encoder", text_encoders: "Text encoder", vae: "VAE",
  }[role] || role.replaceAll("_", " ");
}

function sourceHost(url) {
  return new URL(url).hostname;
}

function drawPlan(plan, onPrepare, onLoad, onCheck, activity, reading, pending, uncertain, message) {
  const box = element("div", "oa-recipe");
  box.appendChild(element("h4", null, plan.title));
  const total = plan.assets.reduce((sum, asset) => sum + asset.size_bytes, 0);
  const present = plan.assets.filter((asset) => ["present_unverified", "ready"].includes(asset.state));
  const missing = plan.assets.filter((asset) => asset.state === "missing");
  const invalid = plan.assets.some((asset) => asset.state === "invalid");
  const presentBytes = present.reduce((sum, asset) => sum + asset.size_bytes, 0);
  const missingBytes = missing.reduce((sum, asset) => sum + asset.size_bytes, 0);
  box.appendChild(element("div", "oa-line",
    `${plan.assets.length} files · ${sizeText(total)} total · ${present.length} present (${sizeText(presentBytes)}) · ${missing.length} missing (${sizeText(missingBytes)})`));
  for (const asset of plan.assets) {
    const item = element("div", "oa-asset");
    const state = {
      missing: "Missing", present_unverified: "Present, awaiting verification",
      ready: "Verified ready", invalid: "Invalid file",
    }[asset.state];
    item.appendChild(element("div", "oa-asset-name", `${roleName(asset.role)} · ${asset.filename}`));
    item.appendChild(element("div", "oa-line", `${sizeText(asset.size_bytes)} · ${state}`));
    box.appendChild(item);
  }
  const sources = element("div", "oa-provenance");
  const templateSource = element("div", "oa-line");
  sourceLink(templateSource, `Official template: ${sourceHost(plan.template.url)}`, plan.template.url);
  if (plan.template.license?.id && httpsSource(plan.template.license.url)) {
    templateSource.appendChild(document.createTextNode(" · "));
    sourceLink(templateSource, `License: ${plan.template.license.id}`, plan.template.license.url);
  }
  sources.appendChild(templateSource);
  for (const asset of plan.assets) {
    const line = element("div", "oa-line", `${roleName(asset.role)} · `);
    sourceLink(line, `Source: ${sourceHost(asset.url)}`, asset.url);
    line.appendChild(document.createTextNode(" · "));
    sourceLink(line, `License: ${asset.license.id}`, asset.license.url);
    sources.appendChild(line);
  }
  box.appendChild(sources);
  const details = element("details", "oa-path");
  details.appendChild(element("summary", null, "Sources and verification"));
  details.appendChild(element("div", "oa-line",
    `Template: ${plan.template.name} · ${plan.template.variant} · revision ${plan.template.revision}`));
  if (plan.template.sha256) details.appendChild(element("div", "oa-line", `Template SHA-256: ${plan.template.sha256}`));
  if (plan.template.graph_sha256) details.appendChild(element("div", "oa-line", `Graph SHA-256: ${plan.template.graph_sha256}`));
  for (const asset of plan.assets) {
    details.appendChild(element("div", "oa-line",
      `${roleName(asset.role)} · ${asset.repository}@${asset.revision} · folder ${asset.folder} · SHA-256 ${asset.sha256}`));
  }
  if (invalid) box.appendChild(element("div", "oa-line oa-failed",
    "An existing file does not match the pinned asset. Move or remove it manually, then check readiness; preparation will not overwrite it."));
  if (pending) box.appendChild(element("div", "oa-line",
    "Preparation was accepted. Follow the download rows below. If it stops, inspect them and reopen this tab before approving another request."));
  if (plan.detail) box.appendChild(element("div", "oa-line", plan.detail));
  if (message) box.appendChild(element("div", "oa-line", message));
  const actions = element("div", "oa-actions");
  const prepare = element("button", null, activity === "prepare" ? "Sending preparation request…" :
    "Download missing files and prepare workflow");
  prepare.disabled = plan.ready || invalid || !!activity || reading || pending || uncertain;
  prepare.onclick = (event) => onPrepare(plan.manifest_sha256, event?.isTrusted === true);
  actions.appendChild(prepare);
  const load = element("button", null, activity === "load" ? "Loading workflow…" :
    "Replace current canvas with prepared workflow");
  load.disabled = !plan.ready || !!activity || reading;
  load.onclick = onLoad;
  actions.appendChild(load);
  const check = element("button", null, reading ? "Checking workflow readiness…" : "Check workflow readiness");
  check.disabled = !!activity || reading;
  check.onclick = onCheck;
  actions.appendChild(check);
  box.appendChild(actions);
  box.appendChild(details);
  box.appendChild(element("div", "oa-note", "Loading replaces the current canvas. ComfyUI asks first when the canvas has unsaved changes. Press ComfyUI’s Run button when you are ready to render."));
  return box;
}

function drawImageRecipe(detail, loading, busy, onLoad) {
  const box = element("div", "oa-recipe");
  box.appendChild(element("h4", null, "Create a picture with OpenAbstractions"));
  box.appendChild(element("div", "oa-line", "Loads an editable image recipe with preview and save nodes. You choose the prompt, then press ComfyUI Run."));
  if (detail) box.appendChild(element("div", "oa-line", detail));
  const actions = element("div", "oa-actions");
  const load = element("button", null, loading ? "Loading recipe…" : "Load picture recipe");
  load.disabled = busy || loading;
  load.onclick = onLoad;
  actions.appendChild(load);
  box.appendChild(actions);
  box.appendChild(element("div", "oa-note", "Choose an image model in OA setup first. Setup is checked when you load the recipe."));
  return box;
}

async function readPlan(signal, verify) {
  const response = await fetch(verify ? `${WORKFLOW}?verify=1` : WORKFLOW, { cache: "no-store", signal });
  const body = await response.json().catch(() => null);
  if (!response.ok) throw new Error(body?.detail || `workflow route answered ${response.status}`);
  if (!validPlan(body)) throw new Error("workflow route returned an invalid asset plan");
  return body;
}

async function readImageRecipe(signal) {
  const response = await fetch(IMAGE_RECIPE, { cache: "no-store", signal });
  const body = await response.json().catch(() => null);
  if (!response.ok || !body || body.id !== "oa-image-generate-preview-save-v1" || typeof body.ready !== "boolean") {
    throw new Error(body?.detail || `recipe setup route answered ${response.status}`);
  }
  return body;
}

function ready(row) {
  return row.state === "complete" && row.present &&
    (row.kind !== "repository" || (row.children || []).length > 0 &&
      row.children.every((child) => child.state === "complete" && child.present));
}

function provenance(row) {
  if (row.source === "reused" && ready(row)) return row.store ? `Reused from ${row.store}` : "Reused from a store";
  if (row.source === "mixed") {
    const where = row.store ? ` from ${row.store}` : "";
    const reused = `${row.reused} of ${row.files} files reused${where}`;
    return ready(row) ? `${reused}; remaining files downloaded` : reused;
  }
  return ready(row) ? "Downloaded from source" : "";
}

function folderLabel(row) {
  const parts = String(row.destination || "").split(/[\\/]+/).filter(Boolean);
  const folder = row.kind === "repository" ? parts.at(-1) : parts.at(-2);
  return folder ? `Folder: ${folder}` : "";
}

function selectedIdentities(row) {
  const entries = row.kind === "repository" ? row.children : [row];
  const identities = entries.map((entry) => entry.identity).filter(Boolean);
  if (identities.length > 256) return [];
  return identities.map(({key, history_epoch, attempt}) =>
    ({key, history_epoch, attempt: attempt ?? 0}));
}

function retryable(row) {
  return row.kind !== "operation" && row.state === "failed" && row.retry?.state !== "running" &&
    selectedIdentities(row).length > 0;
}

function cancellable(row) {
  return ["pending", "running", "transferred"].includes(row.state) &&
    !row.cancellation_requested && selectedIdentities(row).length > 0;
}

function actionMessage(action, result) {
  const words = action === "retry" ? {
    started: "Retry started", running: "Retry already running", busy: "Retry limit reached",
    unsupported: "Retry unavailable", unknown: "Operation no longer listed",
  } : {
    requested: "Cancellation requested; the operation may still finish",
    unsupported: "Cancellation unavailable", unknown: "Operation no longer listed",
  };
  const label = result.outcome === "stale" ? "The selected attempt changed. Check its current state." :
    words[result.outcome] || `${action === "retry" ? "Retry" : "Cancel"} request: ${result.outcome}`;
  return result.detail ? `${label}: ${result.detail}` : label;
}

function drawRow(row, onAction, child, blocked = () => false) {
  const box = element("div", child ? "oa-row oa-child" : "oa-row");
  box.appendChild(element("div", "oa-title", row.kind === "operation" ? row.label || "OA operation" : row.model || row.destination));

  const state = element("div", "oa-line");
  const preparing = row.kind !== "operation" && row.state === "complete" && !ready(row);
  const word = element("span", `oa-state ${row.state === "failed" ? "oa-failed" : ready(row) ? "oa-complete" : ""}`,
    preparing ? "Preparing model files" : ready(row) ? "Ready" : STATE_WORDS[row.state] || "Status unknown");
  state.appendChild(word);
  const active = ["pending", "running", "transferred"].includes(row.state);
  const reported = row.percent !== null && row.percent !== undefined;
  const percent = active && reported ? ` · ${row.percent}%` :
    preparing && reported && row.source !== "reused" ? ` · Download ${row.percent}%` : "";
  const source = provenance(row);
  const unavailable = row.kind === "repository" ? "final files not yet available" : "final file not yet available";
  state.appendChild(document.createTextNode(`${percent}${preparing ? ` · ${unavailable}` : ""}${source ? ` · ${source}` : ""}`));
  box.appendChild(state);

  if (active && row.percent !== null && row.percent !== undefined) {
    const bar = element("div", "oa-bar");
    const fill = element("div");
    fill.style.width = `${Math.max(0, Math.min(100, row.percent))}%`;
    bar.appendChild(fill);
    box.appendChild(bar);
  }

  if (row.destination) {
    const folder = folderLabel(row);
    if (folder) box.appendChild(element("div", "oa-line", folder));
    const path = element("details", "oa-path");
    path.appendChild(element("summary", null, "Full destination path"));
    path.appendChild(element("code", null, row.destination));
    box.appendChild(path);
  }
  if (row.reason) box.appendChild(element("div", `oa-line${row.state === "failed" ? " oa-failed" : ""}`, row.reason));
  if (row.retry && row.retry.state) {
    box.appendChild(element("div", "oa-line",
      row.retry.state === "running" ? "a retry is running" :
      row.retry.state === "failed" ? `the last retry stopped: ${row.retry.detail}` : "the last retry finished"));
  }
  if (row.cancellation_requested) box.appendChild(element("div", "oa-line", "cancellation requested"));

  const actions = element("div", "oa-actions");
  const retry = element("button", null, "Retry");
  const cancel = element("button", null, "Cancel");
  retry.disabled = !retryable(row) || blocked(row.id);
  cancel.disabled = !cancellable(row) || blocked(row.id);
  retry.onclick = () => onAction(row, "retry", retry);
  cancel.onclick = () => onAction(row, "cancel", cancel);
  actions.appendChild(retry);
  actions.appendChild(cancel);
  box.appendChild(actions);

  (row.children || []).forEach((file) => box.appendChild(drawRow(file, onAction, true, blocked)));
  return box;
}

// unsavedChanges reports whether the open workflow may hold unsaved work.
// Only ComfyUI's own isModified === false proves a saved canvas; a frontend
// without that workflow store counts as unsaved, so the person confirms.
function unsavedChanges() {
  return app.extensionManager?.workflow?.activeWorkflow?.isModified !== false;
}

// confirmReplace asks through ComfyUI's own confirmation dialog, or the
// browser's when this ComfyUI has none, before an edited canvas is replaced.
// With neither, the canvas stays as it is.
async function confirmReplace() {
  const message = "The current canvas has unsaved changes. Replace it with the prepared workflow?";
  const dialog = app.extensionManager?.dialog;
  if (typeof dialog?.confirm === "function") {
    return await dialog.confirm({ title: "Replace canvas", message, type: "default" }) === true;
  }
  if (typeof globalThis.confirm === "function") return globalThis.confirm(message) === true;
  return false;
}

function mount(el) {
  style();
  const view = element("div", "oa-downloads");
  view.appendChild(element("h3", null, "OpenAbstractions"));
  const imageRecipe = element("div");
  const recipe = element("div");
  recipe.appendChild(element("div", "oa-note", "Reading the FLUX.2 Klein preparation plan…"));
  const note = element("div", "oa-note", "reading this ComfyUI's OA operations…");
  const list = element("div");
  view.appendChild(imageRecipe);
  view.appendChild(recipe);
  view.appendChild(note);
  view.appendChild(list);
  el.appendChild(view);

  let cursor = "";
  let timer = null;
  let stopped = false;
  let fetching = false;
  let readController = null;
  let message = "";
  let lastRowCount = 0;
  let preparationRows = [];
  let plan = null;
  let planMessage = "";
  let imageRecipeMessage = "";
  let planDue = true;
  let verifyPlan = false;
  let planReading = false;
  let recipeAction = "";
  let pendingPrepare = false;
  let uncertainPrepare = false;
  let loadAttempt = null;
  const activeActions = new Set();
  const uncertainActions = new Set();
  const blocked = (id) => activeActions.has(id) || uncertainActions.has(id);

  function drawRecipe() {
    imageRecipe.replaceChildren(drawImageRecipe(imageRecipeMessage,
      loadAttempt?.recipe === "image", !!recipeAction, () => onLoad("image")));
    recipe.replaceChildren();
    if (!plan) {
      recipe.appendChild(element("div", "oa-note", planMessage || "Reading the FLUX.2 Klein preparation plan…"));
      const retry = element("button", null, planReading ? "Reading preparation plan…" : "Read preparation plan");
      retry.disabled = planReading;
      retry.onclick = onCheck;
      recipe.appendChild(retry);
      return;
    }
    recipe.appendChild(drawPlan(plan, onPrepare, onLoad, onCheck,
      recipeAction, planReading, pendingPrepare, uncertainPrepare, planMessage));
  }

  function onCheck() {
    if (recipeAction || planReading) return;
    planDue = true;
    verifyPlan = true;
    planReading = true;
    drawRecipe();
    refresh();
  }

  async function onPrepare(approvedHash, selectedInApp) {
    if (!plan || plan.ready || plan.assets.some((asset) => asset.state === "invalid") ||
        recipeAction || planReading || pendingPrepare || uncertainPrepare) return;
    if (plan.manifest_sha256 !== approvedHash) {
      planMessage = "The asset plan changed. Review it before approving the download.";
      drawRecipe();
      return;
    }
    recipeAction = "prepare";
    drawRecipe();
    try {
      const body = { manifest_sha256: approvedHash, approve: true };
      if (selectedInApp) {
        for (const asset of plan.assets) {
          if (asset.state === "ready") continue;
          const rows = preparationRows.filter((row) =>
            row.kind === "file" && row.state === "cancelled" && row.model === asset.filename && row.url === asset.url);
          const identities = rows.length === 1 ? selectedIdentities(rows[0]) : [];
          if (identities.length === 1) {
            body.cancelled = [{ filename: asset.filename, ...identities[0] }];
            break;
          }
        }
      }
      const response = await fetch(`${WORKFLOW}/prepare`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const result = await response.json().catch(() => null);
      if (stopped) return;
      if (!result || !["started", "already_ready", "busy", "refused"].includes(result.outcome)) {
        throw new Error("invalid preparation reply");
      }
      planMessage = `${{
        started: "Preparation started. Progress appears in OpenAbstractions below.",
        already_ready: "All model files are already ready.",
        busy: "Preparation is already running. Progress appears below.",
        refused: "Preparation refused. Review the plan before trying again.",
      }[result.outcome]}${result.detail ? ` ${result.detail}` : ""}`;
      if (["started", "busy"].includes(result.outcome)) pendingPrepare = true;
      if (result.outcome === "already_ready" || result.outcome === "refused") planDue = true;
    } catch {
      if (stopped) return;
      uncertainPrepare = true;
      planMessage = "The preparation request did not return a result. Check the operations below, then reopen this tab before another download request.";
    } finally {
      recipeAction = "";
      if (!stopped) drawRecipe();
    }
    await refresh();
  }

  const activeLoad = (attempt) => loadAttempt === attempt && !stopped;

  function finishLoad(attempt, text) {
    if (!activeLoad(attempt)) return;
    attempt.controller.abort();
    loadAttempt = null;
    recipeAction = "";
    if (attempt.recipe === "image") imageRecipeMessage = text;
    else planMessage = text;
    drawRecipe();
  }

  // onLoad replaces the canvas on the person's own click. A direct action in
  // ComfyUI whose effect stays in ComfyUI needs no OA question (VISION
  // 2026-10-02). The graph route serves only a ready recipe, with its digest.
  async function onLoad(recipe = "klein") {
    if ((recipe !== "image" && !plan?.ready) || recipeAction || (recipe !== "image" && planReading)) return;
    if (typeof app.loadGraphData !== "function") {
      if (recipe === "image") imageRecipeMessage = "This ComfyUI cannot load a workflow into the canvas.";
      else planMessage = "This ComfyUI cannot load the prepared workflow into the canvas.";
      drawRecipe();
      return;
    }
    const attempt = { controller: new AbortController(), recipe };
    loadAttempt = attempt;
    recipeAction = "load";
    if (recipe === "image") imageRecipeMessage = "";
    else planMessage = "";
    drawRecipe();
    let graph;
    try {
      const graphRoute = recipe === "image" ? `${IMAGE_RECIPE}/graph` : `${WORKFLOW}/graph`;
      const response = await fetch(graphRoute, { cache: "no-store", signal: attempt.controller.signal });
      const graphText = await response.text();
      if (!activeLoad(attempt)) return;
      if (!response.ok) {
        let detail;
        try { detail = JSON.parse(graphText)?.detail; } catch { /* HTTP status still explains the refusal. */ }
        if (recipe !== "image") {
          plan = { ...plan, ready: false };
          planDue = true;
        }
        return finishLoad(attempt, detail || `Workflow graph route answered ${response.status}`);
      }
      const expected = response.headers.get("X-OA-Graph-SHA256");
      const graphSha = await sha256(graphText);
      if (!activeLoad(attempt)) return;
      if (!/^[a-f0-9]{64}$/.test(expected || "") || graphSha !== expected) {
        return finishLoad(attempt, "Workflow graph verification failed. The canvas was not changed.");
      }
      graph = JSON.parse(graphText);
      if (!graph || !Array.isArray(graph.nodes)) throw new Error("the graph has no nodes");
    } catch (error) {
      return finishLoad(attempt, `The workflow could not be read: ${error}. The canvas was not changed.`);
    }
    if (unsavedChanges() && !await confirmReplace()) {
      return finishLoad(attempt, "The canvas was kept. Save it, then load the workflow again.");
    }
    if (!activeLoad(attempt)) return;
    try {
      await app.loadGraphData(graph);
      finishLoad(attempt, "Workflow loaded into the editable canvas. Press ComfyUI’s Run button to render.");
    } catch (error) {
      finishLoad(attempt, `ComfyUI could not load the workflow: ${error}`);
    }
  }

  async function onAction(row, action, button) {
    if (blocked(row.id)) return;
    activeActions.add(row.id);
    button.disabled = true;
    try {
      const result = await act(row, action);
      if (stopped) return;
      message = actionMessage(action, result);
    } catch {
      if (stopped) return;
      uncertainActions.add(row.id);
      message = `${action === "retry" ? "Retry" : "Cancel"} request did not return a result. Check the operation, then reopen this tab before another action.`;
    } finally {
      activeActions.delete(row.id);
    }
    cursor = "";
    await refresh();
  }

  function visible() {
    return !document.hidden && view.isConnected && view.getClientRects().length > 0;
  }

  function draw(rows) {
    list.replaceChildren();
    lastRowCount = rows.length;
    if (!rows.length) {
      list.appendChild(element("div", "oa-note",
        "No OA download operations are retained for this ComfyUI yet. Install a model through ComfyUI-Manager and it appears here."));
      return;
    }
    rows.forEach((row) => list.appendChild(drawRow(row, onAction, false, blocked)));
  }

  async function refresh() {
    if (fetching || stopped) return;
    fetching = true;
    const controller = new AbortController();
    readController = controller;
    try {
      const body = await readJobs(cursor, controller.signal);
      if (stopped || controller.signal.aborted) return;
      if (body.missing) {
        preparationRows = [];
        note.textContent = body.detail || `OA downloads are not switched on in this ComfyUI. Set ${SWITCH} and restart ComfyUI.`;
        list.replaceChildren();
        return;
      }
      if (body.error) {
        preparationRows = [];
        note.textContent = `The OA jobs route answered: ${body.error}`;
        return;
      }
      if (body.outcome !== "page" || body.complete !== true) preparationRows = [];
      if (body.changed !== false) {
        cursor = body.cursor || "";
        preparationRows = body.outcome === "page" && body.complete === true ? body.rows || [] : [];
        draw(body.rows || []);
      }
      const refusal = body.outcome && body.outcome !== "page" ? ` · the service ended the listing with "${body.outcome}"` : "";
      note.textContent = `${lastRowCount} ${lastRowCount === 1 ? "operation" : "operations"}${refusal}${message ? ` · ${message}` : ""}`;
    } catch (error) {
      preparationRows = [];
      if (!stopped && !controller.signal.aborted) note.textContent = `The OA jobs route could not be read: ${error}`;
    } finally {
      if (!stopped && !controller.signal.aborted && planDue) {
        planDue = false;
        planReading = true;
        drawRecipe();
        try {
          plan = await readPlan(controller.signal, verifyPlan);
          verifyPlan = false;
          if (!stopped) {
            if (planMessage.startsWith("The workflow plan could not be read:")) planMessage = "";
            drawRecipe();
          }
        } catch (error) {
          if (!stopped && !controller.signal.aborted) {
            plan = null;
            planMessage = `The workflow plan could not be read: ${error}`;
            drawRecipe();
          }
        } finally {
          planReading = false;
          if (!stopped) drawRecipe();
        }
      }
      if (readController === controller) readController = null;
      fetching = false;
    }
  }

  function tick() {
    if (stopped) return;
    if (visible()) refresh();
    timer = setTimeout(tick, INTERVAL_MS);
  }

  refresh();
  timer = setTimeout(tick, INTERVAL_MS);
  return () => {
    stopped = true;
    if (timer !== null) clearTimeout(timer);
    if (readController !== null) readController.abort();
    if (loadAttempt) {
      loadAttempt.controller.abort();
      loadAttempt = null;
    }
  };
}

// A Setup Install in the OpenAbstractions Panel opens ComfyUI at
// ?oa_recipe=picture. The picture recipe then loads once through the same
// path as "Load picture recipe": the ready graph and its digest, ComfyUI's own
// confirmation for unsaved work, and no OA question (VISION 2026-10-02). The
// parameter is removed first, so a reload loads nothing again. The Panel adds
// oa_token, a one-time token it obtained from this ComfyUI's backend; the
// sidebar spends it before loading, so a navigation from any other page loads
// nothing.
async function loadRecipeOnArrival() {
  let wanted = false;
  let token = "";
  try {
    const query = new URLSearchParams(location.search);
    wanted = query.get("oa_recipe") === "picture";
    token = query.get("oa_token") || "";
  } catch { return false; }
  if (!wanted || typeof app.loadGraphData !== "function") return false;
  try { history.replaceState(null, "", location.pathname); } catch { /* the load still runs once */ }
  if (!/^[a-f0-9]{64}$/.test(token)) return false;
  try {
    const spent = await fetch(`${IMAGE_RECIPE}/arrival/${token}`, { method: "POST", cache: "no-store" });
    if (spent.status !== 204) return false;
    const response = await fetch(`${IMAGE_RECIPE}/graph`, { cache: "no-store" });
    const graphText = await response.text();
    if (!response.ok) return false;
    const expected = response.headers.get("X-OA-Graph-SHA256");
    if (!/^[a-f0-9]{64}$/.test(expected || "") || await sha256(graphText) !== expected) return false;
    const graph = JSON.parse(graphText);
    if (!graph || !Array.isArray(graph.nodes)) return false;
    if (unsavedChanges() && !await confirmReplace()) return false;
    await app.loadGraphData(graph);
    return true;
  } catch {
    return false; // "Load picture recipe" in the sidebar stays available.
  }
}

app.registerExtension({
  name: "openabstractions.downloads",
  async setup() {
    if (!app.extensionManager || !app.extensionManager.registerSidebarTab) return;
    app.extensionManager.registerSidebarTab({
      id: "oaDownloads",
      icon: "pi pi-cloud-download",
      title: "OpenAbstractions",
      tooltip: "Use OpenAbstractions picture workflows and manage downloads in this ComfyUI",
      type: "custom",
      render: (el) => mount(el),
    });
    await loadRecipeOnArrival();
  },
});
