// "Connect to OpenAbstractions": one sidebar tab with one button. The click
// POSTs /openabstractions/connect (oa_connect.py), which starts the installed
// OpenAbstractions Panel on its registration confirmation; the tab shows the
// one sentence the route answers. Plain JavaScript, no build step.

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const ROUTE = "/openabstractions/connect";

function element(tag, text) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  return node;
}

export async function connect(fetchApi = (route, init) => api.fetchApi(route, init)) {
  try {
    const response = await fetchApi(ROUTE, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const body = await response.json();
    if (typeof body?.message === "string") return body.message;
    return "ComfyUI answered without a message.";
  } catch {
    return "This ComfyUI does not serve the Connect action; restart ComfyUI and try again.";
  }
}

export function mount(el, run = connect) {
  el.replaceChildren();
  const panel = element("div");
  panel.style.padding = "8px";
  panel.append(element("p", "Register this ComfyUI with OpenAbstractions."));
  const button = element("button", "Connect to OpenAbstractions");
  button.className = "p-button p-component";
  const status = element("p");
  status.setAttribute("role", "status");
  button.addEventListener("click", async () => {
    button.disabled = true;
    status.textContent = await run();
    button.disabled = false;
  });
  panel.append(button, status);
  el.append(panel);
}

app.registerExtension({
  name: "openabstractions.connect",
  async setup() {
    if (!app.extensionManager || !app.extensionManager.registerSidebarTab) return;
    app.extensionManager.registerSidebarTab({
      id: "oaConnect",
      icon: "pi pi-link",
      title: "OpenAbstractions",
      tooltip: "Connect to OpenAbstractions",
      type: "custom",
      render: (el) => mount(el),
    });
  },
});
