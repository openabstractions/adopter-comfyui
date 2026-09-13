# ComfyUI service adapter

Copy this directory into ComfyUI/custom_nodes/abstraction_downloads. Install the
current Python facade, identity IPC, job acceptance and download request packages,
the matching shared native IPC library, and an available runtime with the managed
HTTP execution profile. Current source package installations are exercised by
`python adopters/comfyui/run_service.py --run`; public package availability is a
separate release prerequisite. Copying the directory alone does not supply the
service SDK or the native library.

Runtime selection is the SDK default: `Machine()` verifies the installed runtime
(the MSI product record on Windows, the `abstraction-runtime.service` user unit
on Linux), and retained requests restore through `Jobs.restore_installed`. The
Windows MSI check runs regardless of `ABSTRACTION_RUNTIME_ENDPOINT`: a runtime
with no MSI product registered under the OA upgrade code, such as an isolated
or development runtime, resolves `runtime_unavailable` even when the endpoint
names its real, listening pipe. Such a runtime needs `service.configure` naming
its endpoint explicitly:
`service.configure(lambda **waiting: Machine(endpoint, **waiting), lambda e, owner, **waiting: Jobs.restore(e, owner, **waiting))`.
`service.configure` exists for embeddings with independently configured trust and
for tests. `adopters/comfyui/run_discovery.py --run` proves the default path on
Linux with only the unit record substituted.

## Activation preflight

Before either Manager entrypoint is rebound, `service.preflight()` checks, in
order, with a short total budget (`PREFLIGHT_BUDGET_SECONDS`, 5 seconds): the
facade and identity Python packages import, the native IPC library resolves,
the installed runtime selects through the SDK default `Machine()` path with no
activation, and `abstraction.job/acceptance@1` resolves. The first failing
item and a one-sentence explanation are logged once and recorded in
`install_status()` (`InstallStatus`: `mode`, `code`, `detail`), the way
`logging_status()` records logging adoption. Codes, in check order:

- `sdk_unavailable`: the facade, identity or job acceptance Python package
  does not import.
- `native_unavailable`: the native IPC library did not resolve (`Machine()`
  did not construct).
- `runtime_unavailable`: no installed OA runtime was found.
- `acceptance_unavailable`: `abstraction.job/acceptance@1` resolved to a
  refusal other than a missing runtime, such as `unmet_requirements`.
- `ready`: every item resolved.

When preflight is ready, model installs adopt exactly as described below.
When it fails, the node looks for a retained OA recovery record beside this
ComfyUI's models: a `*.oa-request.json` beside a model, or a
`*.oa-snapshot.json` beside a repository directory, the way
`service.deliver`/`deliver_snapshot` write them (see "Storage reuse" below).
No retained record: Manager's own downloader is left untouched, and the log
line names the failing item and how to activate -- resolve it, then restart
ComfyUI (`install_status().mode` is `"manager"`). A retained record: accepted OA work already belongs to the
service. Model installs stay on the job service and fail loudly in Manager
exactly as they did before this preflight existed; the log line says
accepted OA work is pending and names the failing item
(`install_status().mode` is `"service"`).

## Image model selection

The **OA Image Generate** node exposes **model** and **selection_policy**
fields. Enter a model for this node run, or use a saved policy. When both fields
are blank, the node reads its own saved selection policy name. An absent
selection or a definitive missing/stale-policy refusal with no admitted
operation shares every current offered image model as one ordinary choice
through `application@2`. An assistant the person enrolled may answer any
offered option. The node retains one request reference and deadlines while
observing; an offered recorded answer starts one explicit `image@1` request
with the same prompt and edit inputs. It acknowledges that answer after reading
the verified image. This flow writes no policy or permission. A pending choice
waits at most 90 seconds; dismissal, expiry, refusal or an uncertain admission
starts no replacement image. No offers, more than three offers or unsafe names
report guidance for the existing model field and OA setup controls.

**Use for ComfyUI** saves the chosen prepared/routed model as a selection
policy. Current **Create pictures / Install** consent grants inference complete
on `host:lemonade-image` and content write/read on `output:image`. Image work
still requires those execution/output permissions; selecting a model conveys
application intent. ComfyUI runs under the runtime's account, and its physical
Python executable is the rights subject. See
[the Python rights-subject guidance](#the-rights-subject-of-a-python-process);
a Python grant covers every script that interpreter runs.

The generated Python/Go image dispatcher fixture verifies a stale-policy
refusal followed by a separate explicit node run, with unchanged image content.
Its supplied Image-only transport has no model inventory. Shared-choice tests
separately cover complete offered options and the retained recorded answer.
Historical results remain evidence for their original source. Composed-runtime rights gates,
native peer proof and a live image engine need separate qualification.

## Logging

`ABSTRACTION_COMFYUI_LOGGING=service` sends ComfyUI's Python logging to the
resolved `abstraction.logging/sink@1`. The "OpenAbstractions for ComfyUI"
extension sets it. The node attaches the logging package's `ServiceHandler` to
the root logger at import, before Manager adoption, beside ComfyUI's own
handlers: the console, the log file, `/internal/logs` and the terminal view keep
every record. Records keep their message, the slog level scale (DEBUG -4, INFO 0,
WARNING 4, ERROR 8, CRITICAL 12), UTC microsecond time, `logger` and `source`
attributes, and a formatted exception. Messages and exceptions over 8 KiB are cut
at a character boundary and marked `truncated`. Records emitted before the node
loads stay in ComfyUI's own handlers only.

The extension's relay makes the calls (`oa_route.py`), so the service records
the ComfyUI connector, the program OpenAbstractions knows this ComfyUI by, as
each record's verified emitter. Before a record leaves, its message and
exception pass through OA's redaction (`oa_redact.py`, the Python reading of
`abstraction-facade/go-core/redact` that answers the same bypass table).

Logging fails open. `install()` never raises and never waits: the handler
resolves the sink on its own delivery thread at the first record, through the
same factory each time. ComfyUI's thread converts a record and queues it. Unset
leaves logging unchanged. `logging_status()` returns a `LoggingStatus` with
`enabled`, `attached`, `code`, `written`, `failed`, `dropped`, `queued` and
`detail`. Codes:

- `off`: the switch is unset.
- `attached`: the handler is on the root logger.
- `invalid_configuration`: any value other than `service`; nothing is attached.
- `sdk_unavailable`: the facade or logging package is missing; nothing is
  attached.
- `service_unavailable`: attached, and the service has not answered yet;
  records stay queued.
- `write_failed`: attached, and the service went away; records stay queued and
  are retried.

The handler keeps up to 1024 records queued, drops the newest when full, retries
with backoff and resolves again through the same factory. When the service
answers after a failure it delivers one `records dropped while the sink was
unreachable` record, then the queued records in order. A logger that writes
more than 20 records a second over a burst of 200 is held back; its next record
after the pause is preceded by one naming how many were held back. ComfyUI's own
handlers carry one line for each: `invalid_configuration` or `sdk_unavailable`,
`OA LOGGING SERVICE UNAVAILABLE (service_unavailable)` at start,
`LOGGING SERVICE UNREACHABLE (write_failed)` per outage,
`reaches the OA logging service again` on recovery, `LOGGING QUEUE FULL` per
overflow, and a held-back logger. `oa_logging.configure(factory)` followed by
`oa_logging.install()` supplies independently configured trust.

## Runtime diagnostics

`ABSTRACTION_COMFYUI_DIAGNOSTICS=service` observes the OA runtime for the
contracts the node uses: `abstraction.job/acceptance@1` with the admission
guarantees, `abstraction.job/inventory@1`, `abstraction.logging/sink@1`,
`abstraction.model/resolver@1` and `abstraction.storage/content-reader@1`, all
with local scope. At import it logs `OA runtime readiness (bootstrap=unknown)`
and one line per contract: `resolved (provider=…, scope=…, transport=…)`, a
resolution status such as `unavailable`, or `unobserved`. `GET
/abstraction/diagnostics` observes again and answers 200 with
`{"observation": <RuntimeObservation JSON>, "error": null | "<repr>"}`.

The observation is the facade contract's `RuntimeObservation`, composed in the
node because the Python facade has no `Observe`. Bootstrap is `unknown` with a
detail saying so. Queries share a two-second budget. The first selection or
transport error stops querying, is reported in `error`, and the remaining
entries have no `result`. A resolved entry is a candidate binding.

Unset registers no route, logs nothing and makes no runtime call. Any other value
logs `DIAGNOSTICS NOT SERVED (invalid_configuration)`. `sdk_unavailable` means
the facade or job package is missing and `server_unavailable` means no ComfyUI
`PromptServer` was running. `diagnostics_status()` returns `enabled`, `code`
(`off`, `serving` or a refusal code), `route` and `detail`.
`oa_diagnostics.configure(resolver_factory)` followed by `install()` supplies
independently configured trust.

## Job inventory

`ABSTRACTION_COMFYUI_INVENTORY=service` serves `GET /abstraction/jobs`. Each
request resolves `abstraction.job/inventory@1` through `resolve_job_inventory`
and pages `ListWork` from an empty cursor, 64 snapshots a page, at most 16 pages
within ten seconds. It answers 200 with `outcome` (`page` or the refusal that
ended the listing: `gap`, `forbidden`, `invalid`, `unavailable`), `complete`,
`pages` and `operations`, each an `OperationSnapshot` in the contract's JSON.
Resolution or transport failure answers 503 with `outcome: "unresolved"` and
`error`.

The service scopes inventory to the calling account, so operations accepted
before a ComfyUI restart are listed. Snapshots carry no application label: the
`key` in a model's `.oa-request.json` names its operation. Unset registers no
route and makes no runtime call. Refusal codes and `inventory_status()` match
diagnostics, with the warning `INVENTORY NOT SERVED (<code>)`.

## Jobs view

The same switch serves the Comfy-facing join, `oa_jobs_view.py`: the body above
plus `rows`, one row per operation with the caller record's half of it. The
node passes the module to `oa_inventory.configure_view` at import; loaded
without it, the route answers the operation listing alone.

Each row carries `id`, `kind` (`file` or `repository`), `model`, `destination`,
`url`, `state`, `progress`, `percent`, `source` (`fetched`, `reused`, or
`mixed` for a repository), `store`, `present`, `failure`, `reason`, `identity`,
`operation_id`, `label`, `waiting`, `cancellation_requested` and `retry`. A
repository snapshot is one row with `repository`, `files`, `reused` and a
`children` list of its files; its state is the first of `failed`, `running`,
`pending`, `transferred`, `cancelled`, `unknown`, `complete` that any file
holds, and its progress is the sum of theirs. The service has no snapshot unit;
the caller record is what groups those files.

Rows come from the retained caller records under this ComfyUI's model roots
(`folder_paths.models_dir` when imported, and the sibling `models` directory of
the standard install layout), joined to the listed snapshots by request
identity. The walk is bounded to `SCAN_BUDGET_SECONDS` (2 seconds) and
`MAX_RECORDS` (512). A record the service no longer lists keeps its row with
state `unknown` and a reason saying the history may have expired, so reopening
ComfyUI shows the same rows before any new download.
`oa_jobs_view.configure(roots)` supplies the roots for an embedding or a test.

`?since=<cursor>` compares against the `cursor` each answer carries: an equal
cursor answers `changed: false` with no rows and no snapshots. The cursor is
the adapter's digest of the rows, not a service change feed. The route makes
one bounded listing and one bounded scan per request and waits for nothing.

`POST /abstraction/jobs/<id>/retry` re-drives one retained delivery on a
background thread. A native attempt in terminal state `failed` can advance
to the next attempt under the same key, history epoch, request and
destination. The adapter saves that attempt before native reconciliation.
Other states keep their active attempt, and a cancelled attempt stays
cancelled. A repository child retries only its own file. The answer is
`started`, `running`, `busy`, `unsupported`, `stale` or `unknown`, and the
row's `retry` then reports `running`, `finished` or `failed` with the detail.
The workflow's Prepare action can instead name one exact cancelled recipe
delivery and start a fresh key only at that pinned destination. Repeating that
selection keeps its existing successor. A cancelled successor requires a new
selection. A manifest approval without a cancelled selection keeps the current
key.

`POST /abstraction/jobs/<id>/cancel` calls `service.cancel_retained`, which
restores the binding from the caller record and asks
`abstraction.job/acceptance@1` to cancel that exact identity. The answer is the
contract's outcome: `requested`, `already_terminal`, `unknown`, `forbidden`,
`unsupported` or `unavailable`. Requested is intent, not stopped effects. A
repository cancels every file its record names with an active native operation
and answers `requested` when any file was. A file a store supplied has no
operation and answers `unsupported`. Whole-repository actions require at
most 256 retained file identities. Larger repositories keep their per-file
actions.

Retained row IDs remain stable across retries and restarts. Both actions
require JSON with an `identities` list from the selected row or repository
children. Each entry carries `key`, `history_epoch` and `attempt`. A selection
that no longer matches the active record returns `stale` before acting.
An unknown row answers 404, and malformed action input answers 400.

## OA downloads sidebar tab

`WEB_DIRECTORY` exports `web/`, which ComfyUI serves at
`/extensions/abstraction_downloads/` and loads every `.js` from.
`web/oa_downloads.js` registers one extension, `openabstractions.downloads`,
and one sidebar tab, "OA downloads". It is plain JavaScript with no build step.

The tab lists the rows above: the model and where it lands, the state and a
progress bar, `reused from <store>` or `fetched`, the reason a failed operation
failed, and Retry and Cancel for each row. A repository is one entry with its
files indented under it. Reuse means the runtime already held those bytes and
one authorized read delivered them with no source request; the tab claims
nothing about how a store keeps them on disk.

It refreshes every four seconds, skipping the fetch while the document is
hidden or the tab is off screen, and sends `?since=` so an unchanged answer
carries no rows. With `ABSTRACTION_COMFYUI_INVENTORY` unset the route is
absent, and the tab says so and names the switch instead of failing silently.

## Download observation

`ABSTRACTION_COMFYUI_DOWNLOAD_OBSERVER=service` serves
`POST /abstraction/downloads/observe`. The body names one exact accepted job
request identity and may carry the previous cursor, its logical owner and a
`wait_ms` budget from 0 through 30000. A running snapshot is `changed` when its
state or progress differs. Complete, failed and cancelled snapshots are
`settled`. An unchanged snapshot at the wait deadline is `unchanged`.

Every snapshot comes from `abstraction.job/operations@1 ObserveWork` in this
process's authenticated caller scope. The adapter reads no job-store path and
watches no model file. A transport interruption causes fresh facade resolution
within the same wait budget. The accepted operation continues independently.

Continuation cursors are bound to the request key, history epoch, attempt and
logical owner. A cursor copied into a replacement UI workflow returns `stale`
with `reason: workflow_replaced` before a runtime call. A changed owner returns
`stale` with `reason: logical_owner_changed`. The route is an optional job
observation view; it does not claim the standalone watch capability.

## The card

`ABSTRACTION_COMFYUI_CARD=service` makes this ComfyUI ask for the card before
it loads weights. It holds a set of [leases](https://openabstractions.github.io/reference.html#concept-resource.lease) on `card:0` through
[`abstraction.resource/leases@1`](https://openabstractions.github.io/reference.html#concept-resource.leases), whose combined amount covers the weights files
it has loaded, and the render waits for the answer. Unset, ComfyUI is unchanged:
no wrapper, no lease and no runtime call.

The seam is `comfy.utils.load_torch_file`, the one function every model loader
node reads a weights file through — `CheckpointLoaderSimple`, `UNETLoader`,
`CLIPLoader`, `VAELoader` and the LoRA loaders all reach it. It is given the
path, so the amount asked for is the files' own sizes rather than an estimate.

One ComfyUI's live leases add together for accounting. A workflow that loads a
diffusion model, a text encoder and a VAE grows its hold by each file's positive
size delta, and a file that fits inside the combined amount already held asks
nobody. A hold under 256 MiB is not asked for at all: that is the
Windows instrument's own row threshold (abstraction-resource `CONTRACT.md`
RES-T2), and a hold the table would never report is not worth arbitrating.

The service decides `abstraction.resource/hold` on `card:0` for this ComfyUI's
program before it reads the table (RES-L1). A request no larger than what is
free is granted with nobody asked. A larger one has the service ask the
holders it has a mechanism for, in the order RES-L2 sets, and answer
`acquired` or a typed refusal.

Any answer other than `acquired` raises `oa_card.CardRefused` out of the
loader, so the render stops before a byte is allocated and ComfyUI's queue
carries the reason: the outcome, the bytes, the file, and every holder that was
asked with what it answered. `not_permitted` adds the exact command a person
runs to allow the hold. A lease service that cannot be reached raises
`oa_card.CardUnavailable`; a switch that asked for arbitration never falls
back to loading unarbitrated.

While a lease request waits, its executing ComfyUI node shows that it is waiting
for card capacity. ComfyUI's interrupt button cancels the pending caller wait;
a lease returned after that signal is released before the model loader reads
weights. A holder may already have completed an unload request, and cancelling
the wait does not restore that holder's residency.

The rule this ComfyUI needs is one line for its actual native caller.
`service.rights_program()` supplies relay-aware guidance; see "The rights
subject of a Python process" below. A command is suggested only when the
configured relay supplies its program:

```
openabstractions rights grant --action abstraction.resource/hold --resource card:0 --program "<program>"
```

A live lease is renewed every 10 seconds while ComfyUI holds the weights; the
service releases one not renewed within 30 seconds (RES-L4). Every lease is
released when ComfyUI unloads, through `comfy.model_management.unload_all_models` —
what `POST /free {"unload_models": true}` and ComfyUI's own between-prompt
eviction call, and what the resource service itself uses to yield this ComfyUI
on another asker's behalf. The interpreter's exit releases the rest.

`ABSTRACTION_COMFYUI_CARD_RESERVE` is how much of the card this ComfyUI holds
while it renders, whatever its weights weigh: the working memory a render
needs on top of them. The lease is the larger of the reserve and the weights.
On a machine whose instrument reports no capacity, the lease service's pool is
the machine's own memory (RES-L2), so the weights alone rarely exceed what is
free and nobody is asked; the reserve is how a person says what this ComfyUI
actually needs of the card.

`oa_card.configure(machine_factory)` supplies independently configured trust,
the same seam `service.configure()` gives the download path. Without it the
hook selects the installed runtime.

The switch with no `abstraction-resource` Python package logs one warning
(`CARD NOT HELD (sdk_unavailable)`) and leaves ComfyUI unchanged. That is a
configuration fault visible at startup; a service fault is explicit at the
render that needed the card.

## Switches

| Variable | Unset | Values |
|---|---|---|
| `ABSTRACTION_COMFYUI_WAIT_SECONDS` | 900 | 0 < n ≤ 86400 |
| `ABSTRACTION_COMFYUI_LOGGING` | off | `service` |
| `ABSTRACTION_COMFYUI_SNAPSHOTS` | off: Manager downloads repositories | `service` |
| `ABSTRACTION_COMFYUI_DIAGNOSTICS` | off | `service` |
| `ABSTRACTION_COMFYUI_INVENTORY` | off: no jobs route and no sidebar rows | `service` |
| `ABSTRACTION_COMFYUI_DOWNLOAD_OBSERVER` | off | `service` |
| `ABSTRACTION_COMFYUI_CARD` | off: weights load without asking for the card | `service` |
| `ABSTRACTION_COMFYUI_CARD_RESERVE` | 0 | n ≥ 0 bytes |
| `ABSTRACTION_COMFYUI_CARD_WAIT_SECONDS` | 60 | 0 ≤ n ≤ 120 |

Installing the node adopts model installs with every switch unset, once the
activation preflight above is ready or a retained OA recovery record makes
accepted work pending. Otherwise Manager's own downloader is left untouched
until it is activated. Once adopted, a runtime that later becomes unreachable
still fails those installs visibly in Manager; see "Activation preflight".

## Supported Manager layouts

The node detects which Manager ComfyUI is running when it is imported:

- `comfyui_manager` 4.x, the pip package ComfyUI core installs from
  `manager_requirements.txt` and runs with `--enable-manager`. Core imports
  `comfyui_manager.glob.manager_server` (or `legacy.manager_server` with
  `--enable-manager-legacy-ui`) before custom nodes load; the node rebinds the
  one that is loaded. Core disables any `comfyui-manager` custom node then.
- ComfyUI-Manager 3.x at `custom_nodes/ComfyUI-Manager/glob` (or
  `comfyui-manager`) when core runs without `--enable-manager`.

A Manager whose `manager_downloader` has `set_download_provider` (the prototype
seam in `research/adoption/comfyui/l2-provider-seam.patch`) is not rebound. The
node registers `manager_provider.ManagerDownloadProvider`, which declines
custom-node code installs and, unless snapshots are on, repository snapshots;
Manager downloads what it declines. `patched` is then empty.

`adoption_status()` returns an `AdoptionStatus` with `adopted`, `code`, `layout`,
`version`, `provider`, `patched` and `detail`. When no supported Manager is
running the node logs a warning beginning `NOT ADOPTED (<code>)` and Manager's own
downloader stays in effect. Codes: `manager_not_found`, `manager_not_enabled`
(package installed, core started without `--enable-manager`),
`manager_not_importable` and `manager_server_not_loaded` (Manager's start failed
before custom nodes loaded).

## Scope: model installs

Only Manager's model-install call sites (`do_install_model`) are rebound. The
downloader module keeps Manager's functions. Registry node archives
(`manager_core.cnr_install`, `CNR_temp_<uuid>.zip` in `custom_nodes`, extracted
and deleted immediately) and 3.x `copy_install` files into `custom_nodes` or
`web/extensions` stay with Manager. They are application code installs: the
caller record retained beside a delivered model exists for re-delivery and
reconciliation of that model, and a random one-shot archive name can never be
asked for again. The download request contract has no record location apart
from the destination.

## Storage reuse

Before a model install or a snapshot file is admitted as a job, the service path
asks OA for bytes it already holds:

1. The digest comes from the snapshot listing (Git LFS sha256) or from
   `abstraction.model/resolver@1`. A Hugging Face resolve URL, including one
   rewritten to `HF_ENDPOINT`, becomes `Ref(registry="hf", repo, revision, file)`.
2. `abstraction.storage/content-reader@1` opens that digest. Opened bytes are
   copied into a temporary file beside the model and renamed into place only
   when their size and sha256 equal the request's. No source request is made
   and no job is submitted.
3. Otherwise the job download runs, after one log line
   `OA storage did not supply <file>; downloading through the job service
   (reason=<reason>)`. A digest the lookup found is added to the job request.

| Reason | Cause | Level |
|---|---|---|
| `digest_unknown` | not a Hugging Face resolve URL, or a lookup outcome other than `resolved` | INFO |
| `storage_not_found` | `not_found` | INFO |
| `storage_refused` | any other Open outcome, such as `forbidden` | WARNING |
| `storage_read_failed` | a read outcome other than data, or a transport error while reading | WARNING |
| `storage_digest_mismatch` | the stored size or bytes differ from the request | WARNING |

A capability the runtime does not offer is reported once per process instead of
once per file. When `abstraction.model/resolver@1` or
`abstraction.storage/content-reader@1` does not resolve, or a bound one fails a
call, one WARNING reads `OA <contract> not offered (reason=model_not_offered)`
or `(reason=storage_not_offered)`. Files then skip that capability without a
line of their own and go to the job download. Resolution is tried again at most
once every 300 seconds. `OA <contract> is offered again` is logged when a later
resolution succeeds, and nothing is logged while the answer stays the same.

A file a store supplied has no job and no request identity, so the delivery is
written down separately: `<model>.oa-reuse.json` beside a model,
`<local_dir>.oa-reuse.json` beside a repository directory, holding one entry
per file with `final`, `url`, `digest`, `size`, `store`, `at` and, for a
repository file, `repo` and `path`. `store` is the endpoint the runtime
resolved for the content reader, or the contract name when the binding exposes
none: it names which store supplied the bytes and says nothing about how that
store holds them. The jobs view reads this record; the model is the fact, so a
record that cannot be written costs one row its provenance and never the file
(one warning names it). `service.read_reuse` and `service.reuse_record_path`
read it.

A repeated Manager delivery can ask storage again before reconciling the
original attempt. An explicit Jobs retry uses the retained native operation
and its result. Install the `abstraction-model` and
`abstraction-storage` Python packages with the facade. The job request keeps
Manager's own (mirrored) URL as its source.

## Repository snapshots

A Manager model entry whose filename is `<huggingface>` downloads a whole
Hugging Face repository through `manager_downloader.download_repo_in_bytes`.
Unset, Manager keeps doing that itself with `requests`, and
`snapshot_status()` reports `off`. `ABSTRACTION_COMFYUI_SNAPSHOTS=service`
rebinds that module attribute in every supported layout:

- The repository is listed as Manager lists it (`HfApi.repo_info` with file
  metadata), and every URL names the listed commit, so all files come from one
  revision. Files without a size are skipped, as Manager skips them.
- Each file becomes one download job: an anonymous HTTP(S) request with the
  listed size, and `sha256:<hex>` for a file stored with Git LFS. The service
  verifies that digest before delivery. Other files carry their size only.
- One caller record, `<local_dir>.oa-snapshot.json` beside the directory, holds
  the binding, the revision, and each file's identity key and exact request. It
  is written before the first submission. Recovery reads the active attempts
  from that record and reconciles them without listing the repository again.
- Each file is delivered by an atomic rename into `local_dir`. A file already
  present is kept. The whole snapshot shares one
  `ABSTRACTION_COMFYUI_WAIT_SECONDS` budget.
- Service absence, refusal, a failed job or an expired wait raises, and Manager
  reports the install as failed. Delivered files and the record stay. Nothing
  falls back to Manager's downloader.

`snapshot_status()` returns a `SnapshotStatus` with `enabled`, `adopted`,
`code` and `detail`. Codes other than `off` and `adopted` log a warning
beginning `SNAPSHOTS NOT ADOPTED (<code>)`: `invalid_configuration`,
`manager_not_adopted` and `snapshot_call_not_found`. The jobs are independent:
the service has no snapshot unit, so completion, cancellation and inventory are
per file, and the caller record is what groups them. Gated repositories need
credentials, which the portable request excludes.

## Service path

The default patches both Manager model download entrypoints. The application resolves
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
the sidecar and reconciles its active identity. Unknown acceptance remains
explicit. A changed URL for an existing sidecar is refused; retain/reconcile old
work before deliberately clearing its caller record. Deleting a downloaded model
while retaining its sidecar allows re-delivery from the same completed result,
subject to provider retention. Existing final files keep Manager's existing-file
behavior. Partial/malformed caller records fail explicitly rather than inventing
a new key. Wait cancellation or timeout leaves accepted work alone; no automatic
CancelWork is issued. `ABSTRACTION_COMFYUI_WAIT_SECONDS` defaults to 900 seconds
and may be set between 0 and 86400. The budget includes local serialization,
resolution, polling and copying; filesystem writes remain ordinary application I/O.

The runtime decides each submission and each model lookup by a rights rule for
the program it bound (see "The rights subject of a Python process" below).
With no rule, acceptance is `forbidden`, and the node logs
`DOWNLOAD NOT PERMITTED (forbidden)`. When the configured relay supplies its
program, the error includes the command from `service.grant_command()`:
`openabstractions rights grant --for downloads --program "<program>" --registry hf`.
Otherwise it says the OA caller program is unavailable and supplies no command.
The Abstraction Panel's Rights section offers the same as "Allow downloads for…".
The node never grants its own program.

### The rights subject of a Python process

The runtime's rights server judges the physical image path of the native
process that connects (`identity.CanonicalProgramPath`). With the extension's
relay configured, that process is its helper Python. `service.rights_program()`
reuses `oa_route.program()` for card and download permission guidance. If the
configured relay cannot supply its program, guidance says the caller is
unavailable and supplies no grant command.

Without a configured relay program, `service.rights_program()` returns `None`.
Card and download guidance says the OA caller program is unavailable and
supplies no permission command. Python's executable path hints cannot establish
an app-owned native image. The actual request's native caller proof and runtime
audit establish which process made it. The extension helper follows the accepted
app-owned image identity; direct calls and account-wide defaults keep their
existing runtime behavior.

Service absence, typed refusal and failed work stay visible in Manager. The normal
entrypoint raises; the agent entrypoint returns False as Manager expects. There is
no automatic second downloader after acceptance uncertainty.

An embedding with independently configured trust may call service.configure with
machine/restoration factories before downloads begin. Both factories must retain
the supplied waiting deadline; restoration retains endpoint and logical owner.
The default uses shared installed-runtime selection, including on recovery.
Native macOS Program proof and real owner ComfyUI installation are outside the
isolated Windows proof. No claim that backend NAS routing is implemented by this
adapter: provider configuration selects supported execution.
