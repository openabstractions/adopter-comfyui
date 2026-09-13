# ComfyUI downloads through OpenAbstractions

This adapter connects ComfyUI-Manager downloads to the local runtime's durable
job service. Backend HTTP runs in the service. ComfyUI receives bounded result
chunks and exposes a model file after the complete result is copied.

## Install and use

Copy this directory into `ComfyUI/custom_nodes/abstraction_downloads`. Install
coordinated Python identity IPC, facade, job acceptance and download request
packages, the matching native IPC library, and the shared runtime with managed
HTTP execution. Copying this directory alone does not supply those dependencies.
See [service setup, recovery and limits](SERVICE.md) before enabling the node.
Use release-specific package instructions; development source availability does
not establish published package or platform availability.

The default uses independently selected installed-runtime trust. A missing or
refused service stays visible in Manager. Retry the same Manager action after an
interrupted wait: the caller-owned `.oa-request.json` sidecar retains the original
request identity and binding for reconciliation. Keep that sidecar while an
outcome is uncertain. It contains the original URL; avoid URLs carrying secrets.

The request profile supports anonymous HTTP(S). Admission promises cover caller
exit, service restart and reconciliation. Gated credentials, arbitrary backend
placement and permanent result retention require additional provider support.
Waiting cancellation leaves accepted work alone. Current isolated entrypoint
proof does not establish a native macOS or owner ComfyUI installation verdict.

## Explicit legacy provider

Set `ABSTRACTION_COMFYUI_PROVIDER=legacy` before importing the node to select the
previous bundled file-provider behavior and its historical Manager fallback.
That mode has separate lifecycle guarantees. [_vendor](_vendor/README.md) records
the retained provider sources; normal service mode does not use their stores.

[Apache-2.0](LICENSE) · [OpenAbstractions](https://github.com/openabstractions/abstractions)
