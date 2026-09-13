# ComfyUI connector

`comfyui-connector.exe` is the program OpenAbstractions knows the person's own
ComfyUI by. The extension MSI places it in the node pack folder,
`<ComfyUI>\custom_nodes\comfyui-openabstractions`, and it serves the ComfyUI
around it. `scripts/preview_build.py` builds it and passes it to
`installer/apps/build_extension.py --program` (`extension.json` "programs").

## Agent Stop

P17 architect ruling, approved by the owner on 2026-11-13 and confirmed in
owner-decision item 7 on 2026-11-15. Only the authenticated agent whose
start receipt names an OA-started ComfyUI instance may stop that instance.
The person can stop it using the Panel. An agent's stop is refused while
the person uses ComfyUI. A busy job drains without interruption. The
approved reasons are a finished task or freeing resources for the agent's
next job. Shared model files and other agents' model leases keep their
existing rules.

The gateway exposes `backend.stop` through the existing application
command for that starter. The connector forwards a cooperative request
only to its freshly launched backend through the verified node channel.
The node checks for open browser sessions when it receives the request
and again before exiting. An accepted request reports `stop_requested`.

Panel Stop selects the launcher occurrence shown on the Apps page. Its
request carries the selected processes' PID and creation time. The
connector and Panel serialize publication, request writing and cleanup on
the same start record lock. A running predecessor node that reads only
the former PID-only request format reaches the existing verified
PID-and-creation fallback after the Panel or takeover grace period. Updated
nodes require a matching launcher record and the exact target occurrence.

## Catalog connection and update

The 2026-11-11 Connect ruling used these exact messages:

- "ComfyUI connected to OpenAbstractions" with Undo, which removes the registration.
- "Let ComfyUI work with OpenAbstractions?" with Allow / Don't allow and the unsigned note behind Show details, for a connection without a matching expectation.

The 2026-11-12 upgrade ruling used "ComfyUI's OpenAbstractions integration was updated with OpenAbstractions <version>". Undo restores the previous pinned connector while its entry is kept.

The original Setup maintenance owner is `monitor/setup_comfy_maintenance.go`. Its expectation is recorded in `comfyui-integration.json` in Setup's config folder for the kept, enabled `comfyui` entry. It waits while the application's programs run.

Operations (`comfyui-connector --help`):

- `register-self` asks the runtime to register the connector as the `comfyui`
  application (applications@3, create-only). The manifest is this image's
  `OA_APPLICATION_MANIFEST` resource, `rsrc_windows_amd64.syso`, compiled from
  `application-manifest.json` with `connector.rc`. An unsigned image is pinned by
  its SHA-256 and stays pending until the person keeps it. Setup's Connect and
  the node's "Connect to OpenAbstractions" run it. An updated connector at the
  same path meets conflict "identity"; Setup's Connect then re-pins the kept
  entry to the connector whose size and SHA-256 the extension pin
  (`adopter_assets.json` `connector_sha256`, `connector_bytes`) names, or
  removes the earlier entry so the person keeps the new one.
- `owned-host` is the application host the runtime starts. It announces the
  presence, starts the adapter `oa_native_host.py` with this ComfyUI's own
  Python (the pack's `helper\python.exe`, `python_embeded\python.exe` or
  `.venv\Scripts\python.exe`) and the runtime's
  `abstraction_facade_appmodel.dll`, and answers the adapter's control
  requests. The adapter serves `workflow.list`, `workflow.inputs`,
  `workflow.run`, `workflow.download_models` and `job.status` over ComfyUI's
  HTTP API, and `graph.read`, `graph.choices`, `graph.edit` and `graph.run`
  over the open page (`../GRAPH.md`). `workflow.download_models` reads the models a workflow
  lists (a template's loaders name them), skips those ComfyUI's model folders
  hold, and asks the node pack's `/openabstractions/models/download` route for
  the rest, one at a time. The route submits each as an OA download job with
  the file's SHA-256 and size from Hugging Face's paths-info, so the download
  archive serves a file it holds.
- `native-relay` makes the node pack's OpenAbstractions calls with the
  runtime's `abstraction_ipc.dll`, so they come from the registered program.
  It also holds this ComfyUI's card leases (`card.go`): one lease of card:0
  per model ComfyUI loads, through the provider kit's holder, the way OA's own
  engines hold theirs. The node pack (`node/oa_lease.py`) asks before each
  load, reports prompt starts and ends, and frees a model OA asks for between
  prompts or at a load point that does not use it. Each yield is one log line
  in ComfyUI's log. ComfyUI's weights live in its Python process, which the
  connector did not start, so they stay unbound to the lease and the card
  counts them twice while ComfyUI holds them (resource gap G1).

`open` serves the ComfyUI:

- A running ComfyUI is attached. When its server listens, the node pack writes
  `%LOCALAPPDATA%\OpenAbstractions\ComfyUI\listening.json` with `{pid, port,
  base, output}` (`node/oa_listening.py`). The connector serves it when that
  process holds the TCP listener on that port and `base` is the connector's
  ComfyUI folder. It never stops an attached ComfyUI.
- A portable ComfyUI is started as `python_embeded\python.exe -s
  ComfyUI\main.py`, owned by the connector, and stops when the connector ends.
- ComfyUI Desktop (`%LOCALAPPDATA%\Programs\@comfyorgcomfyui-electron\ComfyUI.exe`,
  when `%APPDATA%\ComfyUI\config.json` names this folder) and Comfy Desktop
  (`%LOCALAPPDATA%\Programs\Comfy Desktop\Comfy Desktop.exe`, when this folder
  is under `%LOCALAPPDATA%\Comfy-Desktop\ComfyUI-Installs` or is
  `<installPath>\ComfyUI` of an installed local entry in
  `%APPDATA%\Comfy Desktop\installations.json`, read defensively) are started
  as the person's own programs and attached once they report.
- A Comfy Desktop install's server is started by the connector itself,
  headless, with the command Comfy Desktop used (below, "OA starts a Comfy
  Desktop install"). Comfy Desktop 1.1.9 opens on its dashboard and starts
  an install's ComfyUI only when the person launches it there, or when its
  own setting `autoLaunchOnStartup` names one at Desktop's start. It reads
  no install from its command line, registers no link scheme, and a second
  start only focuses its window. When OA cannot tell the command, Comfy
  Desktop is started as before; with no process of this install 15 s after
  that start, the startup phase reads D: `open` answers not ready and
  commands answer at once that the person starts ComfyUI in Comfy Desktop.
- `open` answers applied once ComfyUI serves commands, within 15 s;
  otherwise it answers refused with the reason (ComfyUI is starting, or
  Comfy Desktop has no install running). A command waits up to 20 s for a
  starting ComfyUI, then answers that it is starting.
- Any other ComfyUI, such as a git clone, is the person's to start.

The graph context, the `graph.*` commands and `app_reveal` come from the ComfyUI
page the connector opens (its graph page,
`http://127.0.0.1:<port>/oa/appmodel/bootstrap#...`), through the node pack's
bridge (`node/oa_appmodel_bridge.py`). The bridge reaches the connector over the
graph channel (`channel_windows.go`), for a ComfyUI the connector started or
attached to alike. While the connector serves a ComfyUI, it listens on
`\\.\pipe\openabstractions-comfyui-graph-<32 random>` and names it in
`%LOCALAPPDATA%\OpenAbstractions\ComfyUI\channel-<pid>.json` as `{pid, pipe}`.
The node pack in that ComfyUI process connects. The pipe admits this account
only, with a medium integrity label (`listen.Listen`), and the connector keeps a
connection only when its client process is the served ComfyUI's verified
process, held open while it serves. It refuses any other client. Once the node
connected, `open` shows the graph page; without it, an attached ComfyUI is
shown as before (its plain page). For ComfyUI Desktop and Comfy Desktop,
`open` brings their own window to the front and never opens a browser page.

The page's graph view (`node/web/oa_appmodel.js`, `oa_appmodel_graph.mjs`)
uses ComfyUI's extension surface only: `app` from `/scripts/app.js` and
`app.registerExtension`, the root graph (`app.rootGraph`, or `app.graph` on
older frontends) with its nodes and widgets, the canvas, the workflow store
`app.extensionManager.workflow` and the `graphChanged`/`graphCleared` events.
It imports no compiled frontend module, so it works on any frontend version
that keeps that surface. `graph.run` uses `app.queuePrompt` and
`api.queuePrompt` from `/scripts/api.js`. Pieces a frontend may lack degrade
alone: `graph.edit` refuses while the queue settings store, read through the
Vue application's Pinia, or the legacy menu has auto queue on, and edits
when the store cannot be read; without the workflow store, no workflow counts
as busy or active; without the load hooks, a reloaded workflow gets its new
epoch at its next change. Reveal refuses (`canvas_in_subgraph`) while the
canvas shows a subgraph.

The window of a Desktop program this connector launched can carry the graph
context (Desktop window admission, below). For a separately opened Desktop,
reveal opens a fresh browser graph page and leaves the existing window detached.
An unsigned Desktop program uses the browser page. Native Desktop window
admission is available on Windows.
A ComfyUI started before its node pack was installed or updated needs one
restart before its graph channel connects. A portable start does not apply
the flags or variables of the folder's `run_*.bat`. Comfy Desktop's file layout
is undocumented. A ComfyUI started with `--listen` on a non-loopback address
alone is not served.

## Desktop window admission (architect ruling 2026-11-11)

The connector remains the root of trust. Every graph session starts with an arm sent over the authenticated channel pipe (client PID = the verified ComfyUI Python PID). A browser page proves itself with the arm's nonce, delivered in the URL fragment. ComfyUI Desktop's own window proves itself by its process. For a Desktop arm the nonce stays in the pipe.

Desktop arm. The connector matches the Desktop main to the process ID and creation time it recorded when it launched Desktop. It runs the existing program-continuity check and sends `arm` with `peer: "desktop"` and `desktop_path`, the registered Desktop program path. The bridge never reads OA's registry itself. The arm keeps its 30-second window and single use.

Wake. The bridge sends ComfyUI's own server event `oa-appmodel-arm` with no payload. The extension in the Desktop window answers by POSTing `/oa/appmodel/bootstrap` with an empty body. Browser tabs that receive the event are refused silently.

Peer check, run in the bridge on that POST. Each step must pass:
1. Existing authorize(): loopback remote, Host and Origin required and equal to the armed origin.
2. Connection owner: find the TCP row whose two endpoints exactly match this request's socket (peer ip:port and listener ip:port), using GetExtendedTcpTable with owner PID. The owner PID is that row's PID.
3. Image: the owner's full image path (QueryFullProcessImageNameW) equals `desktop_path`, compared case-insensitively after full-path normalization.
4. Tree: the owner is the Desktop main process or its direct child. The Desktop main is the verified Python process's parent. Its image path is also `desktop_path`, and its creation time precedes both the Python process and the owner (PID-reuse guard). Chain depth is exactly one. No grandparents, no siblings from other parents.
5. One exchange per arm. Both Desktop and browser exchanges return a credential for one WebSocket admission, with the same 600 s session and graph protocol. A browser navigation needs a fresh OpenAbstractions bootstrap.

Any failed step refuses with the existing generic failure. The graph context stays absent, and the bridge writes one log line naming the failed step. The log line holds no ports, nonces or tokens beyond the step name.

Threat model. The boundary is web content: pages from other origins, other browsers and other apps' windows. A same-user process that can rewrite the Desktop tree can already inject into the verified Python process, which the pipe trusts today. Electron's window serves only the armed origin, and step 1 excludes every other origin loaded inside the Electron tree.

Platforms. Windows ships now. On macOS and Linux a Desktop arm is refused until each platform has its socket-owner lookup. There, app_read reports plainly that Desktop has no graph context, and app_reveal opens the browser page as today.

Tests: the bridge accepts a Desktop exchange when the owner matches path, parent and creation order. It refuses each of these: wrong path, a grandchild, an owner newer than its parent, an unarmed or used arm, a wrong Origin, a nonce-armed (browser) arm reached without a nonce. The connector sends `peer: "desktop"` only for a registered Desktop program that passed continuity.

### Implementation

Architect answers on the implementation (2026-11-11):

1. desktop_path: confirmed. The connector resolves it from install.go for this ComfyUI folder and verifies it at arm time. The bridge compares against exactly the arm's value.

2. Continuity: confirmed. An unsigned Desktop gets no arm, in line with our boundary for unsigned programs in user-writable folders. Addition: in that case:
   - app_read says plainly "ComfyUI Desktop isn't signed, so agents use the browser page";
   - app_reveal opens the browser page;
   - one log line is written.

3. Launcher hop: amended.
   - Both sides skip the hop by full path. The path must equal this ComfyUI folder's venv launcher: `<venv>\Scripts\python.exe` or `pythonw.exe` under the folder install.go resolved, compared case-insensitively.
   - The hop must be a direct child of the Desktop main, created after it and before the interpreter.
   - A name match alone is not enough in the connector.
   - Exactly one hop.
   - The owner rule stays: Desktop main or its direct child.

Smaller choices:
- A failed POST does not spend the arm: confirmed.
- Wake every 2 s inside the 30 s window: confirmed. Stop it on a successful exchange.
- IPv4 only: confirmed. A Desktop arm also refuses a remote of ::1.

Where the code does it:

- The connector (`desktop_windows.go`) arms on every showing of ComfyUI
  Desktop (`kindDesktop`) and of Comfy Desktop (`kindComfyDesktop`): at its
  start and on each later `open`. `desktop_path` is the Desktop program
  `install.go` found for this ComfyUI folder; the connector checks it is a
  regular file before each arm. Comfy Desktop's unsigned notice is
  `{"type":"notice","reason":"comfy_desktop_unsigned"}`, which the adapter
  reads as "Comfy Desktop isn't signed, so agents use the browser page".
- Comfy Desktop 1.1.9 (2026-11-11, read on a live install) has the same tree
  and checks: its main process `Comfy Desktop.exe` (signed by Drip Artificial
  Inc) starts `<installPath>\ComfyUI\.venv\Scripts\python.exe -s
  ComfyUI\main.py` with `<installPath>` as working folder; that uv venv
  launcher runs `<installPath>\standalone-env\python.exe`, the ComfyUI
  process. The ComfyUI folder is `<installPath>\ComfyUI`. Its window loads
  `http://127.0.0.1:<port>`, and its NetworkService utility process, which
  makes Chromium's connections, is a direct child of the main process.
- Continuity: the Desktop main process runs `desktop_path`, was created before
  the ComfyUI Python process, and `desktop_path` carries a code signature
  Windows trusts (`identity.VerifyFile`). For an unsigned program the
  connector writes one log line and sends the adapter one
  `{"type":"notice","reason":"desktop_unsigned"}` frame. The adapter's startup
  context then reads that sentence as `graph_status` until a browser page
  connects, and `app_reveal` outside the graph context sends the control
  request `G`, which opens the browser graph page.
- The venv launcher is `<ComfyUI folder>\.venv\Scripts\python.exe` or
  `pythonw.exe`; ComfyUI Desktop starts it from its base folder, Comfy
  Desktop from `<installPath>`. The bridge
  takes the ComfyUI folder as the one holding its `custom_nodes`.
- The bridge (`node/oa_appmodel_bridge.py`, `WindowsProcesses`, stdlib
  `ctypes`) sends the wake every 2 s while the arm is live and unspent.
- The page (`node/web/oa_appmodel.js`) answers the wake only while it has no
  live session and shows no Close control in Desktop's window.
- Step 4's PID-reuse guard refuses a Desktop main created after its owner
  child or after the Python process.

## OA starts a Comfy Desktop install (architect ruling 2026-11-12)

The ruling, as given:

> # OA starts a Comfy Desktop install (architect ruling 2026-11-12, tier 2)
>
> Tier 2: this is behaviour of the existing open verb, under the existing open right, and it adds no new authority. Decision: option (a). Commit this text in the branch that builds it.
>
> Basis: VISION 10-23 L1938 says "OA starts the person's installed programs under their own sign-in", running "the program's own documented mode". `python main.py` with the install's own interpreter is ComfyUI's documented server mode, and the install is the official one. OA ships nothing new into it.
>
> 1. What open does:
> - For a registered Comfy Desktop install with OA's integration, when no server is running, open starts the install's ComfyUI server headless.
> - It uses exactly the command and arguments Desktop used for that install. Their source is Desktop's own launch record (comfy-procs\<id>.json), which OA only reads.
> - If that record is absent, OA rebuilds the command from installations.json, settings.json and the instance model-paths yaml.
> - If any required fact is missing, open falls back to (c): the person starts ComfyUI. OA never guesses arguments.
> - The browser UI is served at that port, and app_reveal opens it.
> - OA writes nothing into Desktop's files (port-locks, comfy-procs, settings). It reads them only.
> - (b) is out, because Desktop offers no mechanism for it.
>
> 2. Identity and rights:
> - The python.exe that OA starts is an interpreter and is never registered as an app (10-07).
> - The app is the registered Comfy Desktop install. OA's connector inside it is the integration.
> - Starting still needs the open right. "Allow once" covers one start, as today.
>
> 3. Ownership and stopping:
> - The runtime's launcher starts the server in a job object without kill-on-close, so a runtime restart doesn't kill a running render.
> - The runtime tracks the server by PID plus creation time, attributed as "started by OpenAbstractions for <requesting app>".
> - It stops only when the person presses Stop on ComfyUI's Apps page, or when Desktop takes over (point 4).
> - The stop is graceful first: OA waits a bounded time for the current prompt to end, then stops the server by PID.
> - Agents can't stop it.
> - Its memory is managed through the connector's lease (engine control), never by killing it.
>
> 4. Coexistence with the Desktop UI:
> - Before starting: if Desktop already runs that install (a live comfy-procs entry, or a port lock with a live PID), open answers already_running and starts nothing.
> - After OA started a server: if the person starts the same install from Desktop, Desktop's run takes over. OA sees the new comfy-procs entry and stops its own server once it is idle. The connector then re-attaches to Desktop's server.
> - Check before shipping what Desktop does when the port is busy.
> - Also check whether two servers on one base directory conflict (its user directory and assets database). If they do, OA refuses to start while Desktop's launcher process is running, and says so plainly.
>
> 5. What the agent sees:
> - open returns `starting` with a handle and phases: launching, loading custom nodes, listening, connector ready.
> - It waits up to its wait_ms (the caller's budget), then continues by handle. This is the same pattern as the image and video tools.
> - When ready, it returns `opened` with the instance.
> - A failure gives one plain cause, e.g. "ComfyUI's server exited: <last log line>".
> - The Apps page shows "ComfyUI is running (started by OpenAbstractions for OpenCode)" with a Stop button.
>
> The manifest's start_guidance changes accordingly. The plain message from (c) stays only for the fallback.

### What Comfy Desktop 1.1.9 does (read from its app.asar, 2026-11-12)

- Launch record: `comfy-procs\<session>.json` is `{v: 1, sessionKey,
  installationId, installPath, childPid, desktopPid, port, spawnedAt,
  desktopStartTime, childStartTime, tree?, lingering?, pendingScan?}`.
  `childStartTime` is the child's `CreationDate.ToFileTimeUtc()` as a decimal
  string. It holds no command or arguments. A port lock,
  `port-locks\port-<n>.json`, is `{pid, installationName, timestamp}`.
- Command: `getLaunchCommand` gives `<installPath>\ComfyUI\.venv\Scripts\python.exe
  -s ComfyUI\main.py <launchArgs>` with `<installPath>` as working folder;
  `applyStorageLaunchArgs` adds `--extra-model-paths-config
  <state>\instance-model-paths\<id>.yaml` when model folders apply, then
  `--input-directory` and `--output-directory` from settings.json unless the
  install opts out (`useSharedInput`/`useSharedOutput` false). At launch
  Desktop adds computed flags: `--feature-flag` pairs, `--enable-assets`
  and campaign arguments such as `--enable-agent`. Its environment adds
  `PYTHONIOENCODING=utf-8`, `PYTHONFAULTHANDLER=1`,
  `__COMFYUI_DESKTOP_VERSION__` (standalone and comfybuilder sources), the
  install's `envVars` and a per-session `__COMFY_CLI_SESSION__` path.
- Port busy: with `portConflict` `auto` Desktop moves to the next free port.
  When the port's holder is a ComfyUI of the same install and the launch
  passes `--enable-assets`, Desktop starts nothing. It says "Starting a
  second copy on another port would fail, because both would use the same
  database", and offers "Stop process and retry", which ends the holder with
  `taskkill /T /F`.
- Two servers on one base folder: they share the user folder and the assets
  database (`user\comfyui.db`), and Desktop treats the database lock as a
  failure (`isDbLockFailure`). OA therefore starts nothing while Comfy
  Desktop's program runs (ruling point 4, last bullet).

### Where the code does it

- `desktop_launch.go` chooses the command. While the connector serves a
  ComfyUI Desktop started, it reads the command line of the process the
  live comfy-procs record names (the creation time must match) and keeps its
  arguments in `%LOCALAPPDATA%\OpenAbstractions\ComfyUI\desktop-launch-<id>.json`,
  beside a digest of the install facts it ran under. The next start uses
  that command while the facts are unchanged. Otherwise the command is
  rebuilt from installations.json, settings.json and the yaml, without
  Desktop's computed flags. A missing fact (no entry, no `launchArgs`, an
  adopted or non-standalone install, a missing yaml or folder) falls back to
  starting Comfy Desktop.
- `desktop_start_windows.go` uses the shared `supervision.StartRetained`.
  Windows assigns the server to its own job during creation and refuses the
  start if the parent job denies breakaway. The server can outlive the
  connector. Output goes to
  `%LOCALAPPDATA%\OpenAbstractions\ComfyUI\server.log`.
  The environment is the connector's with Desktop's variables; the session
  path is left out.
- `desktop_owned_windows.go` waits for it: phases L launching, K loading
  custom nodes, W listening (read from the output), then R once the
  connector serves it. Before starting it answers Y while Desktop runs the
  install, and D while Comfy Desktop's program runs. A server that exits
  before it listens fails with "ComfyUI's server exited: <last output
  line>". The served server's page is the browser graph page; `app_reveal`
  opens it.
- `started.go` records only the processes the connector started, the
  launcher and the server, by PID and creation time in
  `%LOCALAPPDATA%\OpenAbstractions\started\comfyui.json`. The runtime's
  own log names the program whose open started the connector, with the
  connector's PID and creation time (`applications.owned.start`,
  `serve/application_owned_start_record.go`); the connector learns nothing
  of it. The Apps page joins the two, the launcher's parent being that
  connector, and shows the line and Stop (`monitor/app_started.go`). Stop and
  a Desktop takeover write `comfyui.stop`; the node pack ends ComfyUI once
  its queue is empty (`node/oa_listening.py`), and after five minutes the
  Panel or the connector stops each recorded process by PID.
- A later connector attaches to a server OA started when the record names
  the listening process. A connector that ends leaves the server running.
- The adapter answers a starting open with `starting:<phase>: <sentence>`
  and keeps `phase` and `cause` in its startup context. The MCP gateway's
  `oa_applications_open` follows the start up to `wait_ms` (default 20 s)
  through `oa_applications_describe` and `app_read`, then answers
  `status: starting` with the phase and a handle
  (`mcp-gateway/gateway/appmodel_open.go`).

## Hub downloads through OA

Architect ruling of 2026-11-11, as given:

> 1. In-process HF_ENDPOINT route: accepted, scoped to the ComfyUI process.
>    The download jobs are ComfyUI's own, under its existing download right.
>    Conditions:
>    - Requests: the route serves loopback remotes only, even when ComfyUI
>      listens on the LAN. It refuses any request carrying Origin or
>      Sec-Fetch-Site, because hub clients send neither and a web page can't
>      then drive it.
>    - Token: Authorization goes only to huggingface.co, unchanged, and is
>      never logged or put into a job.
>    - Response headers: HEAD and redirect metadata (X-Repo-Commit, ETag,
>      X-Linked-Etag, X-Linked-Size) pass through unchanged, so the hub's cache
>      layout stays correct.
>    - Xet: for a file the route serves through a job, remove the X-Xet-*
>      headers and the xet Link from the resolve response. Otherwise
>      huggingface_hub 2.x with hf_xet fetches from the Xet CAS directly and
>      skips OA. Test it on one Xet-backed repo.
>    - No staging copy: stream from the job's delivered file and let the job
>      store's retention apply. A 20 GB model must not exist three times.
>    - Fallback: if OA is unreachable or the job fails, pass the request
>      through to huggingface.co. If the env was set too late because
>      huggingface_hub had already imported, do nothing beyond one log line.
>    - Subprocesses inheriting HF_ENDPOINT: accepted, since they hit the same
>      loopback route while ComfyUI lives.
>
> 2. A runtime-wide hub mirror endpoint: deferred. It becomes a
>    public-interface decision when a second app needs hub traffic it can't
>    route through download jobs. Lemonade doesn't, today.
>
> (a) Missing-model buttons: accepted as built.
> - The testid coupling is fine with the vendor fallback. Add a fixture test
>   pinned to frontend 1.58.5's dialog markup, so a renamed testid shows up as
>   a failing test.
> - When OA declines or fails, the captured click must still reach Desktop's
>   own handler. Re-dispatch the event; don't drop it.
> - A workflow entry without a hash gets its sha256 from the same paths-info
>   lookup as (b). With no hash found, it's a hash-less job with no archive.
> - The anchor-click guard acts only on allowlisted workflow URLs and passes
>   every other click through untouched.
>
> (b) Manager install_model: accepted as built. paths-info is a plain network
> read by ComfyUI and needs no OA right. Fail-open to Manager is correct.
>
> New node-pack route: add a same-origin check (Origin equal to ComfyUI's own
> origin), so another site's page can't post downloads. Everything else is the
> same authority as Manager's route.
>
> Security review: these ride the R31 batch's single pass. Checklist: the
> loopback and Origin refusal on /openabstractions/hf, the token path, the Xet
> stripping, and the same-origin check on the POST route.

### Where the code does it

The node pack (`openabstractions-flat/adopter-comfyui`, installed as
`oa_nodes`) carries all three paths. The installed profile turns them on by
setting `ABSTRACTION_COMFYUI_MODEL_DOWNLOADS` and `ABSTRACTION_COMFYUI_HUB` to
`service` in `node/__init__.py`.

- ComfyUI-Manager installs: `__init__.py` rebinds Manager 4.2.2's
  `download_url`, `download_url_with_agent` and `download_repo_in_bytes` to
  `service.deliver`. A model that gets no digest from
  `abstraction.model/resolver@1` gets its SHA-256 from the Hub's paths-info
  API (`storage_reuse.hf_api_digest`, Git LFS `lfs.oid`, Xet-backed files
  included). `ABSTRACTION_COMFYUI_HF_DIGEST=off` turns that read off. When
  the runtime cannot be reached before the caller record is written,
  `service.deliver` raises `ServiceUnavailable`, and Manager's own downloader
  runs with one log line.
- The missing-model dialog: `web/oa_model_downloads.js` takes a capture-phase
  click on `[data-testid=missing-model-download]` and
  `[data-testid=missing-model-download-all]`. It posts the open workflow's
  `{url, name, directory, hash, hash_type}` for the model to
  `POST /openabstractions/models/download` (`oa_model_downloads.py`). The
  route serves only ComfyUI's own Origin, under the frontend's own rule:
  - https on huggingface.co, civitai.com or civitai.red;
  - a model file extension;
  - a ComfyUI model folder other than `custom_nodes` and `configs`;
  - a name that stays inside that folder.

  A model OA cannot take goes back to the frontend. A row's click is
  re-dispatched to its own handler. A model of Download all goes to
  `window.__comfyDesktop2.downloadModel` in Comfy Desktop, or to the
  browser's `<a download>`. The guard on `HTMLAnchorElement.prototype.click`
  acts only on a URL the open workflow lists.
- Hugging Face hub: `oa_hub.py` serves `/openabstractions/hf`. It sets
  `HF_ENDPOINT` to `http://127.0.0.1:<port>/openabstractions/hf` when all of
  these hold:
  - huggingface_hub is not yet imported;
  - ComfyUI listens on loopback without TLS;
  - the person set no `HF_ENDPOINT` of their own.

  For a model file with a SHA-256 X-Linked-Etag, HEAD answers 200 with
  X-Repo-Commit, ETag, X-Linked-Etag, X-Linked-Size and Content-Length. It
  sends no Location, X-Xet-* header or xet Link. GET admits one job per
  digest, pinned to the commit. While the job runs, GET answers 429 with
  Retry-After, and huggingface_hub waits and asks again. GET then streams the
  job's result with `read_result`, Range included. A relative Location from
  huggingface.co stays under the route. Manager's URLs are never rewritten to
  the route.

Limits:

- With `--enable-manager`, which Comfy Desktop passes, Manager's prestartup
  imports huggingface_hub before any custom node loads. A tested release
  (2.2.x) gets the route through its constants (ruling below). Any other
  release keeps huggingface.co, with one log line.
- Comfy Desktop's starter models at setup download before ComfyUI runs and
  stay with Comfy Desktop.
- In Comfy Desktop, the template browser's "Download models and open" calls
  `__comfyDesktop2.downloadModel` from no dialog button the capture takes.
  Those models stay with Comfy Desktop. In a browser, the anchor guard takes
  them.
- A gated file's job carries no token and fails. The request then goes to
  huggingface.co with the person's token.
- A hub GET waits for its job through at most six 429 answers of up to 600 s
  each. The job keeps running in OA, and the next call reads it.

### huggingface_hub imported before the node pack

Architect ruling of 2026-11-11, as given:

> In-process assignment to huggingface_hub.constants is accepted. Without it
> the route is inert for every person who runs Manager. Conditions:
> 1. Version gate: assign only when huggingface_hub.__version__ is in the
>    tested set (2.2.x) and all three attributes exist with their expected
>    types. Any other version: assign nothing, write one log line, and let
>    downloads go to HF directly.
> 2. Respect the person's own mirror: rewrite only when ENDPOINT equals the
>    default https://huggingface.co. The same applies to HF_ENDPOINT in
>    os.environ: set it only when it is unset or equals the default.
> 3. Exact values: ENDPOINT and the URL template point at
>    http://127.0.0.1:<ComfyUI's own port>/openabstractions/hf. HF_URL_HOSTS
>    gains exactly that host:port and keeps every existing entry. The route's
>    upstream stays the original default value, captured before the
>    assignment.
> 4. One log line at assignment, with the old and new values and no token.
> 5. The assignment runs once, at node-pack import, in the ComfyUI process
>    only. Leave HfApi instances that already exist untouched.
> 6. Test pinned to 2.2.0 with Manager's prestartup order: Manager imports
>    first, then the node pack assigns, then hf_hub_download goes through the
>    route. One more test: with a non-default HF_ENDPOINT set, nothing is
>    assigned.
>
> This rides the R31 security pass. Add one checklist item: the token reaches
> only the captured upstream, including after Location stripping.

Where the code does it: `oa_hub.adopt_imported_hub`, called from
`configure_endpoint` once per process by `install` at node-pack import. It
checks `__version__` starts with `2.2.`, that `ENDPOINT` and
`HUGGINGFACE_CO_URL_TEMPLATE` are strings and that `HF_URL_HOSTS` is a
frozenset, and that `ENDPOINT` is `https://huggingface.co`. It then sets the
two strings to the route, adds `127.0.0.1:<port>` to `HF_URL_HOSTS` and sets
`UPSTREAM` to the value it replaced. huggingface_hub 2.2.0 compares
`HF_URL_HOSTS` with bare host names when it parses web URLs, so the
`host:port` entry matches nothing there. Downloads and redirects follow
`ENDPOINT` and the template, which huggingface_hub reads on each call.

Security checklist addition: the token reaches only the captured upstream,
including after Location stripping. The route forwards `Authorization` only
in its own requests to `UPSTREAM`. Job sources carry none, and the HEAD the
route answers for a job carries no Location for a client to follow.

## Tests

The tests start a stand-in ComfyUI (the test binary) that reports `{pid,
port}`, serves the HTTP routes `workflow.list` reads, and connects the graph
channel as the node pack and its page do (one KSampler).
`TestTheAdapterDrivesAStandInComfyThroughTheConnector` drives the real adapter
over the owned host's MCP frames through open, `workflow.list`, the graph
context, `graph.read`, `graph.edit` (with a stale revision refused),
`app_reveal`, `graph.run` and `job.status` with the run's output reference
of an attached ComfyUI. `TestTheNodePackConnectsTheGraphChannel` runs the node pack's own
channel owner against the connector. Both need Python 3.12, the first also an
installed OpenAbstractions runtime; they skip without them.
`TestAForeignProcessCannotJoinTheGraphChannel` tries the channel from another
process of the same account.
`TestComfyLoggingReachesTheServiceAsTheConnector` logs from a ComfyUI logger
through the node pack's relay and this binary's `native-relay` into the
installed runtime's logging service, and finds the record with the connector
as its verified emitter and its credential redacted.
`TestTheNodePackRedactsAsOADoes` runs the node pack's redaction over OA's
bypass table (`redacttest.Cases`).
