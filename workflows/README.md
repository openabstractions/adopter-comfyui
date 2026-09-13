*For maintainers of the bundled ComfyUI workflows.*

Third-party workflow resource

`image_flux2_klein_text_to_image.json` is the official ComfyUI workflow
`Comfy-Org/workflow_templates/templates/image_flux2_klein_text_to_image.json`
at commit `e7cd011d4ded3411c2f481200544f0be6fdc962e`.

Source: https://github.com/Comfy-Org/workflow_templates/blob/e7cd011d4ded3411c2f481200544f0be6fdc962e/templates/image_flux2_klein_text_to_image.json
License: MIT, https://opensource.org/license/mit
SHA256: `8bb879856e54765b6c3ed219181e855981d8d10cfd19acff836603d9b56e838f`

The adopter derives a distilled-only graph from this file and substitutes the
pinned fp8 diffusion filename. The template itself is preserved unchanged.

`klein_recipe.py` also defines the explicit `distilled-bf16` variant with its own
recipe and manifest identity. Its component filenames, repository, revision,
sizes and hashes are the descriptor-derived `BF16_ASSETS` in
[the owning recipe](../klein_recipe.py). The retained official graph's loader
dtype remains `default`; the FP8 default identity and asset pins remain
available separately.

`provider_data(variant="distilled-bf16")` exports bounded `oa.comfy.recipe@1`
data from that same template converter and asset manifest. It carries the graph,
component provenance, finite prompt/size/seed slots and selected SaveImage node.
The native consumer binds it to a selected host and checks that host's verified
readiness through the existing plan route with `?verify=1&variant=distilled-bf16`.
The read-only query checks ComfyUI's first matching file for each component;
exporting the data reads no models and performs no delivery. The initial native
request is generation of one 512x512 image with a fixed provider seed. Export and
source tests do not establish an actual BF16 load or render.

The generic read-only `/abstraction/recipes` route returns bounded
`oa.comfy.recipes@1` data with the supported FP8 and BF16 variants. Each entry
contains the generated recipe and its same-host relative `manifest_path`.
These paths omit `verify=1`; listing never hashes or delivers model files.
Cheap readiness starts unverified. Explicit `verify=1` or existing preparation
hashes each file through one open handle, checks its identity and metadata
before and after, and retains successful evidence in this process only.
Later cheap readiness re-resolves ComfyUI's first matching filename and checks
OS file identity, size, nanosecond modification/change times and expected digest
against that evidence. Replaced, changed, missing or shadowed files remain
unverified until explicitly verified. There are at most six retained proofs,
one per supported variant/component role. Explicit verification rehashes even
when a matching proof exists. No recipe metadata URL is fetched by discovery.

OpenAbstractions video recipe

`oa_video_generate_save.json` is an OpenAbstractions-authored ComfyUI workflow
distributed under the adopter's Apache-2.0 license. It connects the
`OAVideoGenerate` video output to ComfyUI's `SaveVideo` node. Its model is
`auto`, the first model OA offers for video when the run starts.

OpenAbstractions image recipe

`oa_image_generate_preview_save.json` is an OpenAbstractions-authored ComfyUI
workflow distributed under the adopter's Apache-2.0 license. It connects the
`OAImageGenerate` image output to both ComfyUI's `PreviewImage` and `SaveImage`
nodes. It starts with an empty model and saved-policy field. The person
chooses an image model in OpenAbstractions, which saves it as this ComfyUI
program's `oa-image-default` selection policy, before the first Run. With both
fields empty the node uses that policy by name; it never chooses a model
alias.
