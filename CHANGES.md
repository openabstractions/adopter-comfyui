# Changes

## 0.8.0

### Application identity, Connect and native relay

- ComfyUI registers itself through a native self-only registration front door.
- OA grants ComfyUI's rules to the program it records for ComfyUI's calls.
- The native relay keeps cancellation working for every operation.
- The relay finds the IPC library through the shared recorded discovery.
- Permission guidance requires a configured relay program.
- The portable ComfyUI manifest and the pre-0.8.0 application route are retired.
- ComfyUI's Python logging reaches OA's logging service through the connector, with credentials redacted and noisy loggers held back. ComfyUI logs as usual when OA is away.

### Card leases

- ComfyUI joins OA's resource management as a lease holder: the connector leases card:0 for each model ComfyUI loads, and OA frees an idle ComfyUI model, between prompts, when one of its own engines needs the memory. A running prompt is never cut short. ComfyUI loads as before when OA cannot answer.

### Desktop installer, backend launch and pinned inputs

- The custom ComfyUI Desktop build acquires its backend through an Electron-owned bridge.
- The backend comes from pinned, verified inputs with a sealed Python home.
- The launcher keeps working with long Windows paths.
- macOS builds stage an app-local CPython host with its native dependencies.
- An unsigned local native ComfyUI preview can be built for testing.

### Graph presentation and app model

- The app model shares one native Facade core across languages.
- `?oa_recipe=picture` requires a one-time token.
- The loopback address and client are shared, and arrival tokens are capped.
- ComfyUI video jobs stay visible and controllable after an app restart.
- The ComfyUI helper prints the same waiting sentence as other OA clients.
- Comfy Desktop's own window carries the graph context, admitted like ComfyUI Desktop's.
- Setup finds Comfy Desktop's installed ComfyUIs through its installations.json.
- Agents read, edit and run the open canvas: `graph.read`, `graph.choices`, `graph.edit` and `graph.run` replace `node.set_widget` (GRAPH.md).
- A run's outputs reach the person's Pictures, Videos or Music folder under OpenAbstractions, and `job.status` answers each with its digest, size, media type and path.

### Downloads and storage reuse

- The ComfyUI node no longer falls back to the Manager downloader.
- The legacy provider and the resident prototype are removed.
- Content writes flush the drive cache, with `F_FULLFSYNC` on macOS.
- Model preparation stays generic, and ComfyUI's choices bind to its native caller.
- Workflows load with the model files OA keeps in its local store when this ComfyUI loads them, within the same model family and role; other files stay as the template names them (MODEL-AND-ENGINE.md, Step 1).
