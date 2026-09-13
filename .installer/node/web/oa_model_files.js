// Workflows open with the model files OpenAbstractions picked
// (oa_model_files_graph.mjs). The extension wraps app.loadGraphData so the
// rewrite reaches the workflow before ComfyUI's frontend builds its graph and
// subgraphs. The template file and any saved workflow stay as stored until the
// person saves.

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { wrapLoadGraphData } from "./oa_model_files_graph.mjs";

const hasType = name => Boolean(globalThis.LiteGraph?.registered_node_types?.[name]);

app.registerExtension({
  name: "openabstractions.modelFiles",
  setup() {
    app.loadGraphData = wrapLoadGraphData(app.loadGraphData, (route, init) => api.fetchApi(route, init), hasType);
  },
});
