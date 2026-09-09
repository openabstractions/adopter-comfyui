# Vendored facade

Copies of four modules, one per layer the node needs:
`cas/python/abstraction_cas.py`, `job/python/abstraction_job.py`,
`watch/python/abstraction_watch.py` and
`download/python/abstraction_download.py`, from
[abstraction-job](https://github.com/openabstractions/abstraction-job) and
[abstraction-download](https://github.com/openabstractions/abstraction-download).
The set is what `abstraction_download` imports, directly and through
`abstraction_job`; a module the facade grows a dependency on has to be added
here, or the node imports nothing and every download falls back to Manager's own
transport with one warning line to say so.

**Do not edit these.** Edit the originals and copy them back —
`scripts/vendor-comfyui.sh` in the
[abstractions](https://github.com/openabstractions/abstractions) repository does
it from a checkout that has the layers beside it.

## Why they are copied at all

A ComfyUI custom node is installed by putting a directory in `custom_nodes/`.
Anything that also requires cloning another repository, running `pip install`, or
setting an environment variable is not a custom node any more — it is a project,
and almost nobody will try it.

Every module here is pure standard library, so carrying them costs one directory
and no dependencies. That is what makes this whole thing a copy-and-restart
install.

## Why a copy is safe here

Because it cannot drift silently. `test_node.py` compares these files against
the originals byte for byte and fails if they differ, so a change to the facade
that is not vendored breaks the test rather than shipping a stale abstraction to
whoever installed the node. The same test installs the node as a bare copy with
no repository beside it, which is what catches a module the facade started
importing and nobody copied.

A development checkout overrides them anyway: `ABSTRACTION_HOME` is consulted
before this directory, so working on the facade never means working on a copy.
