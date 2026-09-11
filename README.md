# abstraction_downloads

**In development.** No tagged release; install by copying the directory at a
commit you have checked.

For someone running ComfyUI: a custom node that puts ComfyUI-Manager's model
downloads behind a durable job record, so a download interrupted by closing
ComfyUI continues from the bytes already on disk instead of starting again.

The record lives in a job store on disk rather than in ComfyUI's memory, so it
survives the process that created it. A partly transferred file is written to a
`.part` name and only given its final name once the transfer has finished, so a
truncated file cannot sit in the models tree looking installed.

Built on [Open Abstractions](https://github.com/openabstractions/abstractions),
the parent project, which holds the scope rules, the method, the measured
results and the conformance suite. This node is one application of its download
and job layers.

## Install

```
git clone https://github.com/openabstractions/adopter-comfyui.git ComfyUI/custom_nodes/abstraction_downloads
```

and restart ComfyUI. Without git, download this repository as a zip and unpack
it under `custom_nodes/` with that directory name.

That is the whole installation. No `pip install`, no environment variable: the
node carries the four modules it depends on and they are pure standard library.

To confirm it took, look for this line in the ComfyUI log at startup:

```
[abstraction] ComfyUI model downloads now go through the job store
```

If ComfyUI-Manager is not installed the node loads, defines no nodes, prints
nothing and changes nothing.

## What changes

| | before | after |
|---|---|---|
| ComfyUI closed mid-download | starts over | resumes from what arrived |
| the machine sleeps | starts over | resumes |
| a truncated file | left at the final name, looks installed | never given the final name |
| a download in progress | known only to this process | a record any process can read |

The first row is
[ComfyUI-Manager issue #2934](https://github.com/Comfy-Org/ComfyUI-Manager/issues/2934).

## What it does not change

Nothing about how you use ComfyUI. There are no new nodes —
`NODE_CLASS_MAPPINGS` is empty. Manager's UI, its model list and its buttons
behave as before, and files land exactly where Manager already puts them. This
node changes who fetches the bytes and what is recorded, never the destination.

Every failure inside this node falls back to Manager's own downloader: no store,
an unreadable store, a facade that cannot be imported. ComfyUI downloaded models
before this node existed and has to keep downloading them when this node is
broken.

A download that *fails* is the opposite case and is reported rather than
retried elsewhere. A refusal from the server, or a transfer shorter than the
length the server declared, leaves the record and the partial file in place for
the next attempt to continue from; handing that to a transport with no resume
would throw away the bytes already proven.

## What it does not do

**The bytes are still fetched by ComfyUI's own process and they stop when
ComfyUI stops.** They resume when it starts again; nothing arrives while it is
closed. This node does not hand the transfer to a supervisor such as
[service-jobd](https://github.com/openabstractions/service-jobd), Windows BITS
or a NAS.

The reason is a property of the request, not of ComfyUI: a ComfyUI destination
is an absolute path inside ComfyUI's own models tree, and a supervisor on
another machine resolves a record against its own store, so the bytes would
land somewhere useless while ComfyUI waited for a file that was never coming.
The refusal lives in the layer below — a destination only this machine can
reach is worked here even when a supervisor is watching — so this node asks for
nothing and gets the right answer. Handing the transfer over needs a
destination expressed relative to a shared store, and that does not exist yet.

## Status

Experimental, and the only thing living with it is us.

Exercised once inside ComfyUI 0.33.0 with ComfyUI-Manager 3.41, installed by
copying the directory and nothing else: a 6.9 GB SDXL checkpoint requested
through Manager's own model list, ComfyUI killed mid-transfer, restarted, and
the same download continued from the byte the record had proven.

A source that declares no length at all — chunked, no `Content-Length` — cannot
be checked for completeness by anyone, and this does not pretend otherwise.

## Requirements

- ComfyUI with [ComfyUI-Manager](https://github.com/Comfy-Org/ComfyUI-Manager)
  installed. Without Manager the node is inert.
- CPython 3.12, which is what it is tested on. Every module parses as far back
  as 3.7 syntax; no older interpreter has been run against it, so treat the
  floor as unproven.
- Nothing else. No third-party package, at install or at run time.

## Development

`_vendor/` holds copies of four modules — `abstraction_cas`,
`abstraction_job`, `abstraction_watch` and `abstraction_download` — see
[`_vendor/README.md`](_vendor/README.md).

`ABSTRACTION_HOME` bypasses those copies, so working on the node never means
working on a copy. It names a directory holding `openabstractions-flat/abstraction-cas/python`, `openabstractions-flat/abstraction-job/python`,
`openabstractions-flat/abstraction-watch/python` and `openabstractions-flat/abstraction-download/python`; clone the four layer repositories into one
directory under those names and set the variable to it.

Twelve tests cover the node, including one that installs it as a bare copy with
no environment set and one that compares every vendored module against its
original byte for byte. They run against a stand-in Manager, because a test that
needs somebody's ComfyUI running is a test that will not be run.

**`test_node.py` does not run from a clone of this repository.** It resolves its
paths against the layout it was written in, where the four layers sit beside each
other, so a `git clone` of this repository alone cannot run the published tests.
That is a defect of ours, not a step you are missing; the same layout
`ABSTRACTION_HOME` names above is what it wants.

The same four modules, and what to call in an application of your own, are on
[`abstraction-download`'s Python page](https://github.com/openabstractions/abstraction-download/blob/main/python/README.md).

## Licence

Apache-2.0. See [LICENSE](LICENSE).
