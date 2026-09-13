// The missing-model dialog's downloads through OpenAbstractions; the logic
// and its rules live in oa_model_downloads.mjs.
import { app } from "../../scripts/app.js";
import { Catalog, install, workflowModels } from "./oa_model_downloads.mjs";

const catalog = new Catalog();

function rememberGraph() {
  const nodes = app.rootGraph?.nodes ?? app.graph?._nodes ?? [];
  catalog.remember(workflowModels({ nodes: nodes.map((node) => ({ properties: node.properties })) }));
}

app.registerExtension({
  name: "openabstractions.modelDownloads",
  beforeConfigureGraph(graphData) {
    catalog.remember(workflowModels(graphData));
  },
  async setup() {
    let served = false;
    try {
      // The node's route names an unknown id; ComfyUI's own 404 has no state.
      const answer = await fetch("/openabstractions/models/download/probe", { cache: "no-store" });
      served = (await answer.json().catch(() => null))?.state === "unknown";
    } catch {
      served = false;
    }
    if (!served) return;
    install(catalog, {
      document,
      MouseEvent,
      anchorPrototype: HTMLAnchorElement.prototype,
      desktop: () => window.__comfyDesktop2,
      collect: rememberGraph,
      fetch: (...args) => fetch(...args),
      sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
      refresh: () => app.refreshComboInNodes?.(),
      notify: (severity, summary, detail) => app.extensionManager?.toast?.add?.({ severity, summary, detail, life: 5000 }),
    });
  },
});
