import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { graphView } from "./oa_appmodel_graph.mjs";

// The graph view uses ComfyUI's extension surface only (app, api and the
// graph, widgets and workflow store they expose), so it works on whatever
// frontend version the person's ComfyUI ships.
const STORAGE = "oa.comfy.native.graph.v1";
const id = () => crypto.randomUUID().replaceAll("-", "");
const token = value => typeof value === "string" && /^[A-Za-z0-9_-]{16,64}$/.test(value);
let model, socket, dispatching = false, stopped = false, graphRetired = false;
let closeControl, closeButton, closeStatus, closing = false, currentPage;
function closeUI() {
  if (closeControl) return;
  closeControl = document.createElement("div");
  Object.assign(closeControl.style, { position: "fixed", right: "16px", bottom: "16px", zIndex: "1000",
    background: "#20242a", color: "#fff", padding: "8px", maxWidth: "300px" });
  closeButton = document.createElement("button");
  closeButton.textContent = "Close ComfyUI";
  closeStatus = document.createElement("span");
  closeStatus.setAttribute("role", "status");
  closeButton.onclick = event => {
    if (event.isTrusted !== true || closing || !currentPage || socket?.readyState !== WebSocket.OPEN) return;
    if (!confirm("Close ComfyUI? Stopping cancels running work.")) return;
    closing = true; closeButton.disabled = true;
    closeStatus.textContent = "Requesting stop…";
    try { send({ type: "shutdown", generation: currentPage.generation, page: currentPage.page }); }
    catch { closeStatus.textContent = "Stop outcome is unconfirmed."; }
  };
  closeControl.append(closeButton, closeStatus);
  document.body.append(closeControl);
}
// The frontend's queue settings store, read through the Vue application's
// Pinia, or null when this frontend does not expose it. Without it edits
// apply; with auto queue on they refuse.
function queueState() {
  const pinia = document.querySelector?.("#vue-app")?.__vue_app__?.config?.globalProperties?.$pinia;
  const store = typeof pinia?._s?.get === "function" ? pinia._s.get("queueSettingsStore") : undefined;
  return store && typeof store.mode === "string" ? store : null;
}
// The page channel's frame bound (oa_appmodel_bridge.py MAX_FRAME).
const FRAME = 1 << 20;
const send = value => {
  const raw = JSON.stringify(value);
  if (new TextEncoder().encode(raw).length > FRAME) throw Error("graph bound");
  if (socket?.readyState !== WebSocket.OPEN) throw Error("graph unavailable");
  socket.send(raw);
};
function retire() {
  model?.retire();
  if (socket?.readyState === WebSocket.OPEN) send({ type: "retired" });
}
function publish() {
  if (dispatching || stopped || graphRetired) return;
  // A frontend without the load hooks below reloads with no new epoch; its
  // next change starts one.
  const before = model?.active ? { epoch: model.epoch, sequence: model.sequence, ready: model.status === "ready" } : null;
  const state = model?.active ? model.changed() : model?.loaded(id());
  // graphChanged also fires for a check that found nothing new.
  if (state && before?.ready && state.sequence === before.sequence && state.epoch === before.epoch) return;
  if (state) send({ type: "projection", projection: state });
  else if (model?.status === "projection_exhausted") send({ type: "exhausted" });
}
// One adapter request: run it on the graph view and send its result. A run
// awaits ComfyUI's queue. The result carries the view after it, so the
// projections the request itself caused are not sent again.
async function answer(request, body) {
  dispatching = true;
  let result;
  try { result = model && !graphRetired ? await model.execute(body) : { outcome: "unavailable" }; }
  catch { result = { outcome: "unknown" }; }
  finally { dispatching = false; }
  try {
    send({ type: "result", id: request, outcome: result.outcome, projection: result.projection ?? null, answer: result.answer ?? null });
  } catch { model?.retire(); socket?.close(); return; }
  if (result.outcome === "unknown" || result.outcome === "unavailable") {
    // Retain the original authenticated page solely for local Close.
    graphRetired = true; model?.retire();
  } else if (model?.status === "projection_exhausted") send({ type: "exhausted" });
}
// Only the document reached by the native bootstrap can take this handoff.
function browserCredential() {
  const raw = sessionStorage.getItem(STORAGE);
  if (!raw) return;
  sessionStorage.removeItem(STORAGE);
  const credential = JSON.parse(raw);
  if (!token(credential?.generation) || location.hash !== "#oa-appmodel=" + credential.generation) return;
  history.replaceState(history.state, "", location.pathname + location.search);
  return credential;
}
// Desktop admits this session through its own window, with its own Close.
async function start(credential, desktop = false) {
  if (!credential || Object.keys(credential).sort().join(",") !== "credential,generation" ||
      !/^[A-Za-z0-9_-]{32}$/.test(credential.credential) || !token(credential.generation) ||
      !/^http:\/\/127\.0\.0\.1:[1-9][0-9]{0,4}$/.test(location.origin)) return;
  // Desktop's window lives on across sessions; each starts afresh.
  stopped = graphRetired = closing = false;
  // The local close control remains available if the graph view refuses.
  try { model = graphView(app, { queueState, api }); } catch { model = undefined; }
  // The page's own ComfyUI listener, the origin the connector opened it on.
  socket = new WebSocket("ws://" + location.host + "/oa/appmodel/ws",
    ["oa-comfy-graph-v1", credential.credential]);
  let heartbeat;
  socket.onmessage = event => {
    try {
      if (new TextEncoder().encode(event.data).length > FRAME) throw Error();
      const value = JSON.parse(event.data);
      if (value.type === "ready" && Object.keys(value).sort().join(",") === "generation,page,type" &&
          value.generation === credential.generation && token(value.page) && !currentPage) {
        currentPage = {generation: value.generation, page: value.page};
        if (!desktop) closeUI();
        const state = model?.loaded(id());
        if (state) send({ type: "projection", projection: state });
        else if (model?.status === "projection_exhausted") send({ type: "exhausted" });
        heartbeat = setInterval(() => { try { send({ type: "ping" }); } catch { socket.close(); } }, 10000);
      } else if (value.type === "graph_retired" && Object.keys(value).join(",") === "type") {
        graphRetired = true; model?.retire();
      } else if (value.type === "shutdown" && Object.keys(value).sort().join(",") === "outcome,type" &&
          closing && ["requested", "refused"].includes(value.outcome)) {
        closeStatus.textContent = value.outcome === "requested"
          ? "Stop requested. Waiting for ComfyUI to close…" : "Stop request refused; outcome is unconfirmed.";
      } else if (value.type === "request" && Object.keys(value).sort().join(",") === "id,request,type" && token(value.id) && !closing) {
        void answer(value.id, value.request);
      } else throw Error();
    } catch { model?.retire(); socket.close(); }
  };
  socket.onclose = () => {
    stopped = true; model?.retire(); clearInterval(heartbeat);
    if (closeButton) closeButton.disabled = true;
    if (closing) closeStatus.textContent = "Connection closed. Stop outcome is unconfirmed here.";
    currentPage = null;
  };
  api.addEventListener("graphChanged", publish);
  api.addEventListener("graphCleared", retire);
}
// The bridge's oa-appmodel-arm event, sent while a Desktop arm is live. The
// window answers with an empty-body bootstrap; the bridge admits it by its
// process. Any other page is refused, and nothing changes here.
let arming = false;
async function desktopArm() {
  if (arming || (socket && socket.readyState !== WebSocket.CLOSED) ||
      !/^http:\/\/127\.0\.0\.1:[1-9][0-9]{0,4}$/.test(location.origin)) return;
  arming = true;
  try {
    const response = await fetch("/oa/appmodel/bootstrap", { method: "POST", credentials: "omit", cache: "no-store" });
    if (!response.ok) return;
    await start(await response.json(), true);
  } catch { /* refused: this page is no Desktop window */ }
  finally { arming = false; }
}
app.registerExtension({
  name: "openabstractions.native.graph",
  setup() {
    api.addEventListener("oa-appmodel-arm", () => { void desktopArm(); });
    void Promise.resolve().then(() => start(browserCredential())).catch(() => { model?.retire(); socket?.close(); });
  },
  beforeLoadGraph: retire,
  onGraphLoadError: retire,
  afterLoadGraph() {
    if (!stopped && !graphRetired && model) {
      const state = model.loaded(id());
      if (state && socket?.readyState === WebSocket.OPEN) send({ type: "projection", projection: state });
      else if (model.status === "projection_exhausted" && socket?.readyState === WebSocket.OPEN) send({ type: "exhausted" });
    }
  }
});
