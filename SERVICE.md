# ComfyUI service adapter

Copy this directory into ComfyUI/custom_nodes/abstraction_downloads. Install the
current Python facade, identity IPC, job acceptance and download request packages,
the matching shared native IPC library, and an available runtime with the managed
HTTP execution profile. Current source package installations are exercised by
`python adopters/comfyui/run_service.py --run`; public package availability is a
separate release prerequisite. Copying the directory alone supplies the explicit
legacy provider, not the current service SDK/native library.

The default patches both Manager download entrypoints. The application resolves
local job acceptance, retains endpoint/logical owner/history/key/full request
before submitting, observes the work and streams bounded result chunks into an
application-owned temporary model file. An atomic rename exposes only complete
results. Backend HTTP runs in the service. No provider store path is passed to the
application. Application model output and `.oa-request.json` caller records are
owned by ComfyUI's account.

The sidecar contains the original URL; avoid URLs carrying secrets. The current
request profile supports anonymous HTTP(S). Hugging Face mirror rewriting is
preserved. Gated credentials and upstream model-specific headers need a separate
authorized service contract. The adapter requests caller-exit, service-restart
and reconciliation guarantees. These describe admission recovery; they do not
claim downstream NAS/BITS recovery, arbitrary execution placement or permanent
result retention. Source Content-Length is checked by the provider; no digest is
invented for Manager's bare URLs.

Retry the same Manager action after a lost reply or interrupted wait. It reuses
the sidecar and reconciles the original identity. Unknown acceptance remains
explicit. A changed URL for an existing sidecar is refused; retain/reconcile old
work before deliberately clearing its caller record. Deleting a downloaded model
while retaining its sidecar allows re-delivery from the same completed result,
subject to provider retention. Existing final files keep Manager's existing-file
behavior. Partial/malformed caller records fail explicitly rather than inventing
a new key. Wait cancellation or timeout leaves accepted work alone; no automatic
CancelWork is issued. `ABSTRACTION_COMFYUI_WAIT_SECONDS` defaults to 900 seconds
and may be set between 0 and 86400. The budget includes local serialization,
resolution, polling and copying; filesystem writes remain ordinary application I/O.

Service absence, typed refusal and failed work stay visible in Manager. The normal
entrypoint raises; the agent entrypoint returns False as Manager expects. There is
no automatic second downloader after acceptance uncertainty. To deliberately
retain the previous bundled file-provider behavior and historical Manager fallback,
set `ABSTRACTION_COMFYUI_PROVIDER=legacy` before ComfyUI imports the node. This is
compatibility mode and has separate old guarantees. Vendored provider sources are
unchanged by this migration.

An embedding with independently configured trust may call service.configure with
machine/restoration factories before downloads begin. Both factories must retain
the supplied waiting deadline; restoration retains endpoint and logical owner.
The default uses shared installed-runtime selection, including on recovery.
Native macOS Program proof and real owner ComfyUI installation are outside the
isolated Windows proof. No claim that backend NAS routing is implemented by this
adapter: provider configuration selects supported execution.
