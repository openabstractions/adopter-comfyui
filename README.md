# OpenAbstractions downloads for ComfyUI

*For ComfyUI users and the operators who set it up. Maintainers read
[SERVICE.md](SERVICE.md).*

This ComfyUI custom node lets ComfyUI fetch models, make pictures and make
video clips through OpenAbstractions (OA), the shared service on this machine.

**What's new here:** ComfyUI's model downloads become OA jobs that keep running
when ComfyUI closes, and the pinned FLUX.2 [klein] recipe's files are verified
by digest before the workflow loads.

Use the OA downloads sidebar to inspect Manager downloads, verify the pinned
FLUX.2 Klein recipe assets, and load the prepared workflow into ComfyUI's
canvas. The custom node sends asset requests to the OA job service; it does not
require you to copy files into an OA shared-store directory.

## Requirements

- ComfyUI 0.35.0 or later with a supported ComfyUI-Manager installation.
- The OpenAbstractions runtime, the Python packages and native library listed in
  [SERVICE.md](SERVICE.md),
  and network access from that runtime to the selected model source.
- Set `ABSTRACTION_COMFYUI_INVENTORY=service` before starting ComfyUI and restart
  it to show the OA downloads sidebar.
- Download permission for the runtime's authenticated caller program. The
  extension relay supplies its helper program for permission guidance. When
  that program is unavailable, guidance supplies no permission command. The
  Panel's Rights page can grant download permission. A missing or refused runtime
  stays visible in Manager;
  Manager's own downloader remains the fallback when no OA work was accepted.

The package metadata points to this README. Source availability is not proof
that a public package release is available; use the installation instructions
for the exact release or source revision you have.

## Create and save a picture through OpenAbstractions

The bundled `workflows/oa_image_generate_preview_save.json` recipe connects
**OA Image Generate** to ComfyUI's **Preview Image** and **Save Image** nodes.
It starts with no model alias or saved policy selected. With both fields
empty the node uses its own image selection policy, `oa-image-default`
(`DEFAULT_SELECTION_POLICY` in `oa_image.py`). The person chooses the model
behind that policy in OpenAbstractions, which writes a caller-scoped
selection of that name for this ComfyUI executable through the public
SelectionOperatorV2 operations. Each image request still uses ComfyUI's live
caller identity and public IPC authorization. Enter a model in the node's
**model** field, or use the saved selection. If OA refuses the saved
selection as missing or stale before accepting an operation, the node offers
the current image models as one ordinary choice shared with an assistant the
person enrolled. An offered answer selects an explicit model for this run;
the node starts one `image@1` request with the retained prompt and edit inputs.
This choice writes no saved policy or permission. It waits at most 90 seconds
for an answer; dismissal, expiry or refusal starts no image. With no models,
more than three models or names outside the ordinary-choice bounds, enter a
model or update the saved selection and run again. Each node run retains its prompt
and edit inputs and uses that run's selected model or policy. The selection
controls live in ComfyUI; OA permission questions continue to govern image
execution and content access, as described in
[SERVICE.md](SERVICE.md#image-model-selection).
The first picture from this ComfyUI asks the person OA's picture permission
question. The node waits up to two minutes for the answer and then starts the
picture once more; a refusal stops the run with a sentence naming where to
change it.

The node pack also ships its workflows as ComfyUI custom-node examples, so
they appear in ComfyUI's Templates browser and in the agent command
`workflow.list` as `template:<node pack>/oa_image_generate_preview_save`. An
agent sets `prompt` and `size` with `workflow.run`; the job's `oa_routes`
name the OA host and model that made each picture.

A later ComfyUI run is a new request. An isolated test or explicitly
configured instance may name another policy in
`ABSTRACTION_COMFYUI_SELECTION_POLICY`.
In the **OA downloads** tab, click **Load picture recipe**. The click replaces
the current canvas; ComfyUI's own confirmation asks first when the canvas has
unsaved changes. OpenAbstractions Setup opens ComfyUI at
`http://127.0.0.1:8188/?oa_recipe=picture` after its Install; the sidebar then
loads the same recipe once, through the same path. The open carries a
one-time `oa_token` the Panel obtains from this backend; a link without a
spendable token loads nothing. Edit the prompt and press ComfyUI **Run**; the preview
appears and the image is written through ComfyUI's normal save node.

## Make a video clip through OpenAbstractions

**OA Video Generate** submits a durable OA video job (job kind `inference`,
profile `video`) and returns ComfyUI's `VIDEO` and the OA route. ComfyUI's
**Get Video Components** gives the frames when a graph needs them. The
node's model list holds `auto` and the models OA offers for video; `auto`
takes the first of them. Sizes are 832x480, 480x832, 1280x704 and
704x1280; the duration is 1 to 10 seconds. An image connected to
`first_frame` is stored in OA's content store and starts the clip; it is
request input, which needs content write and read on `input:image` for
ComfyUI's Python, and OA removes it once the job has ended and ten minutes
have passed since the upload. The clip is the job's output under
`output:video`, which needs the same two rights on `output:video`; one
Allow in OA's question lets OA write it and the node read it back. A job
missing several grants is refused once, its reason naming each (for
example `rights:not_granted content:read:forbidden output:forbidden`), and
OA asks every question together; the node's error names each grant. The
progress bar follows the job's steps, the node shows its place in the
engine's queue, and stopping the ComfyUI run cancels the job. The provider
chooses steps, guidance and decoder for the model. The clip arrives as MP4,
WebM or AVI of at most 64 MiB, checked against its digest.
`workflows/oa_video_generate_save.json` connects the node to ComfyUI's
**Save Video**. The model list needs the OA router package
(`abstraction.router`) beside the other OA Python packages.

## Follow a prepared workflow

1. Start ComfyUI and open the **OA downloads** sidebar tab.
2. Select the **FLUX.2 Klein 4B distilled** recipe. Review its pinned diffusion
   model, text encoder, and VAE, then use **Check workflow readiness** to
   verify files already present in ComfyUI's model folders.
3. If files are missing, choose **Download missing files and prepare
   workflow**. The adopter requests the missing pinned files through OA and
   verifies their declared size and digest before marking them ready. Reusable
   bytes are resolved through the runtime's authorized storage capability; the
   UI reports the supplying store. Otherwise the job service fetches them from
   the pinned source and the UI shows the source host. Opening the recipe does
   not start a download.
4. Choose **Replace current canvas with prepared workflow**. The click loads
   the editable graph after the sidebar checks the graph's digest. When the
   canvas has unsaved changes, or this ComfyUI cannot say whether it has,
   ComfyUI's own confirmation asks first, and declining keeps the canvas. OA asks no question here: the replacement is
   the person's own action in ComfyUI, and its effect stays in ComfyUI.
5. Edit the prompt if you like, then use ComfyUI's **Run** control to render.
   Loading the graph starts no render.

The recipe uses a pinned distilled-fp8 workflow variant with the compact files
Setup prepares: the FP8 model, Comfy-Org's FP8/NVFP4 Qwen3 4B text encoder
(`qwen_3_4b_fp4_flux2.safetensors`) and the FLUX.2 VAE, 8.26 GB together. It
differs from the base checkpoint selected by ComfyUI's ordinary **Flux.2
[Klein] 4B: Text to Image** template, whose own file names are the 16 GB
full-precision files. Check the selected recipe and files before running it.

The recorded UI render starts with all three verified assets already present.
It confirms workflow loading and a successful CPU render. That run predates
the click-only replacement and answered an OA question to load the graph.
First-time download through the UI remains subject to the configured OA
runtime, source rights, and network; the successful UI render did not exercise
that download path.

The separate **OA Image Generate** node calls the installed
`abstraction.inference/image@1` service for an explicit model or `image@2` for
a saved selection policy. It requires exactly one target with a prompt and
size. It reads the local result through the authorized storage content reader,
verifies its SHA-256 digest and returns a Comfy `IMAGE` tensor plus the selected
OA route. An optional connected `IMAGE` makes the call an `image@1` edit: the
node encodes the first frame as PNG, writes it through the caller's
authorized content writer and names its digest; an optional `MASK` (1 marks
the area to change) must match the image's size and goes the same way as the
image contract's mask, an RGBA PNG whose fully transparent pixels mark the area
to change (inference CONTRACT.md INF-I1). The image and the mask are
request input: an edit needs `abstraction.storage/content.write` and
`abstraction.storage/content.read` on `input:image` for this ComfyUI
executable, one grant the first-use question asks for together (storage
STO-W12, inference INF-V4). The first edit without it still starts: OA refuses
the input read, or the call with `rights:not_granted`, and asks that question.
After an allow the node uploads the inputs again and starts the edit once more;
it waits up to two minutes for the answer. A "never" answer reads to the node
like an unanswered question, so the node waits the full two minutes before it
reports the refusal. A refusal names that grant only when OA answered
forbidden. Selecting this node is an
explicit alternative to running an existing graph; no graph is translated or
modified. Arbitrary graph nodes, seed, sampler, steps and other engine-specific
controls have no representation in this node and require their ordinary Comfy
workflow. The node refuses unsupported service modes, remote-only results,
content over 8 MiB and incompatible image formats. It needs the public OA
Python facade, inference and storage clients in Comfy's Python environment.
Boundary tests cover refused and uncertain admissions. A configured Go service
fixture exercises generated Python/Go Image dispatch with a stale selection,
unavailable model inventory and a separate explicit run. The Python choice
tests cover shared options, a recorded answer, pending/refused/dismissed results
and one explicit image start with unchanged content.
That fixture supplies its transport; native caller proof and a live image
engine with rendered node output have separate qualification requirements.

## Operations and maintenance

[SERVICE.md](SERVICE.md) documents package installation, runtime preflight,
permissions, retained job
records, storage reuse, Manager integration, and diagnostics. It is the
maintainer's detailed guide. The bundled resident-model example is a separate
legacy prototype; current ComfyUI model transfers use the job service and
current OA identity/rights contracts.
