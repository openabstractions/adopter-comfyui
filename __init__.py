"""ComfyUI model downloads, through the abstraction, without forking ComfyUI.

# Why this is a custom node and not a fork

ComfyUI is GPL-3.0 and so is ComfyUI-Manager, which is the component that
actually fetches model weights. Forking either one and distributing it would put
the shared facade into a GPL-3.0 combined work, which is the entanglement this
project's licence policy exists to prevent. Nothing here modifies a GPL file:
this node rebinds two names at runtime, in memory, in a process the user already
started. That is not distribution of a modified work.

Forking would also buy nothing. ComfyUI CORE HAS NO MODEL DOWNLOADER -- no
huggingface_hub, no hf_hub_download, no fetch path in app/model_manager.py at
all; still true at v0.34.0, whose `app/` contains no downloader module. There is
nothing in core to replace.

Nor is there a version of this that arrives as an upstream patch. Core HAD an
internal download endpoint and deliberately deleted it: PR #5432, "Remove
internal model download endpoint", merged 2024-10-30, replacing it with the
browser download "for easier to avoid us reinventing the wheel for file
downloading". Five PRs have tried to put one back -- #7376, #12170, #12487,
#13961, #14586 -- and all five are still open. The sixth, #14472, was closed by
a maintainer in two days: "we have an internal initiative to manage this ... we
also need to ensure compatibility with our cloud and desktop applications."

So the door upstream is shut, by policy rather than by review, and a custom node
is not the second-best route to adoption here. It is the only one.

# What it replaces, and why that is worth doing

ComfyUI-Manager's default path fetches model weights with
``torchvision.datasets.utils.download_url``. That writes straight to the FINAL
filename: no temp file, no Range request, no resume, no digest. ComfyUI
discovers models by filename, so a download interrupted at 93% leaves a
truncated file that presents as an installed model and fails much later, at load
time, with a deserialisation error naming neither the download nor the
truncation.

That is not a hypothetical. Manager issue #2934 is a user with ~32 GB of models
who "tried probably about 8 times", hung at 93-98% every time, and had to
restart ComfyUI -- "by which point the partial download has been lost". Filed
2026-05-31, no comments, unanswered.

It is also not new. Manager #101 reported the same truncation in 2023 -- "it
gets to 80/90/99% then is marked as succeeded" -- and #234, "Large model
downloads fail", reported it again in December 2023. Both are closed, and
neither was closed by a fix: #101 went quiet after the maintainer asked whether
the browser had the same trouble, and #234's accepted answer was to turn SSL
verification off. Three years and three issues, and the transport still writes
straight to the final name.

Through this node the same download gets a job record that outlives the process,
a `.part` file, a resume from the byte a predecessor PROVED, and an atomic
delivery to the final name -- so a truncated file cannot appear under the final
name, because that name does not exist until the transfer has finished.

Finished, here, means what the SOURCE said: a model list gives a bare URL with
no digest and no size, so completeness is judged against the server's declared
Content-Length. Writing this node is what exposed that the facade checked
neither when the spec carried no size, and delivered short transfers under the
final name exactly as torchvision does. A source that declares no length at all
-- chunked, no Content-Length -- still cannot be checked by anyone, and this
does not pretend otherwise.

# What this deliberately does NOT do yet

It does not hand the transfer to a supervisor, and it no longer decides that
for itself.

A ComfyUI sink is an absolute path in ComfyUI's own models tree, and a job
handed to a NAS tier would name a directory that exists on this PC and not on
the NAS -- so the bytes would land somewhere useless, or nowhere, and ComfyUI
would wait for a file that was never coming. That is a fact about sinks, not
about ComfyUI, so the refusal now lives in the layer: a sink only this machine
can reach is worked here even when a supervisor is watching. This node asks for
nothing and gets the right answer.

The record still outlives the process, so a download interrupted by closing
ComfyUI is resumed on the next start rather than lost. That is the part that
fixes #2934.

# Failing soft

Every failure here falls back to Manager's original function. The downloader
worked before this node existed and has to keep working when this node is
broken, misconfigured, or looking at a store it cannot open. An abstraction that
can stop bytes from moving is worse than no abstraction.

Install: copy or symlink this directory into ComfyUI/custom_nodes/.
"""

import logging
import os
import sys

LOG = logging.getLogger("abstraction")

# ComfyUI requires these two names from every custom node.
NODE_CLASS_MAPPINGS = {}
__all__ = ["NODE_CLASS_MAPPINGS"]

_HERE = os.path.dirname(os.path.abspath(__file__))


def _add_facade_to_path() -> bool:
    """Make the facade importable.

    ABSTRACTION_HOME is read first so the node can be copied into custom_nodes
    while the facade stays wherever it is developed. The layout beside this file
    is the fallback for the case where the whole thing was copied together.
    """
    roots = []
    home = os.environ.get("ABSTRACTION_HOME")
    layers = ("cas", "job", "watch", "download")
    if home:
        roots += [os.path.join(home, layer, "python") for layer in layers]
    # The shipped copy. Every module in it is pure standard library, so
    # carrying them is what makes this directory the whole installation --
    # no pip, no clone, no environment variable. See _vendor/README.
    roots.append(os.path.join(_HERE, "_vendor"))
    roots += [os.path.join(_HERE, layer, "python") for layer in layers]
    found = False
    for r in roots:
        if os.path.isdir(r):
            if r not in sys.path:
                sys.path.insert(0, r)
            found = True
    return found


def _manager_modules():
    """Manager's ``glob`` modules, imported here rather than waited for.

    ComfyUI loads custom node directories in sorted order, so whether this node
    runs before or after ComfyUI-Manager depends on what it is named. Importing
    the modules directly makes the order irrelevant: if Manager has already
    loaded, these come out of sys.modules and are the very objects it is using;
    if it has not, this loads them and Manager's own import later is a no-op on
    the same objects.
    """
    import importlib

    custom_nodes = os.path.dirname(_HERE)
    glob_dir = os.path.join(custom_nodes, "ComfyUI-Manager", "glob")
    if not os.path.isdir(glob_dir):
        return None, None
    if glob_dir not in sys.path:
        sys.path.append(glob_dir)
    try:
        downloader = importlib.import_module("manager_downloader")
        server = importlib.import_module("manager_server")
    except Exception as e:
        LOG.warning("[abstraction] ComfyUI-Manager present but not importable: %r", e)
        return None, None
    return downloader, server


_original_download_url = None
_original_download_url_with_agent = None


def _mirrored(model_url: str) -> str:
    """Manager's own first act inside download_url, and it has to survive.

    HF_ENDPOINT points Hugging Face traffic at a mirror. It is not a nicety:
    where huggingface.co is slow or unreachable it is the only way downloads
    work at all. Replacing download_url without this sends those users back to
    the origin, which fails or crawls -- and looks like our abstraction broke
    their downloads, because it did.
    """
    endpoint = os.environ.get("HF_ENDPOINT")
    if endpoint:
        return model_url.replace("https://huggingface.co", endpoint.rstrip("/"))
    return model_url


class _Unavailable(Exception):
    """The abstraction could not be USED -- and nothing was written down.

    This is the only condition that may fall back to Manager's own downloader,
    and the boundary is not "an error happened", it is "does a job record
    exist yet". Once one does, this node owns the transfer; a second downloader
    running against a live record is `docs/integrating.md` §1, where every bug
    of the Lemonade night came from two executors disagreeing about one file.
    """


def _through_the_abstraction(model_url: str, model_dir: str, filename: str) -> None:
    try:
        import abstraction_download as dl
    except Exception as e:
        raise _Unavailable(e) from e

    model_url = _mirrored(model_url)
    final = os.path.abspath(os.path.join(model_dir, filename))
    if os.path.exists(final):
        # Manager's own contract: an existing file is a finished download. Not
        # this layer's place to second-guess it -- without a digest there is
        # nothing to check it against, and re-fetching a model the user already
        # has is the failure mode this whole project started from.
        return

    # Everything up to and including the submit is setup: it can fail without
    # anything having been recorded, so Manager's downloader may still run.
    try:
        os.makedirs(model_dir, exist_ok=True)
        service = dl.discover()
        job_id = service.submit(
            dl.Spec(
                # No digest: a bare URL from a model list does not carry one.
                # The abstraction records that honestly rather than inventing a
                # hash it cannot check, and every guarantee that does not need
                # one -- resume, atomic delivery, a record that outlives the
                # process -- still holds.
                artifact=dl.Artifact(digest="", size=0),
                sources=[dl.Source(scheme="https", locator=model_url)],
                sink=dl.Sink(final=final),
            ),
            requires=[dl.CAP_RESUME],
        )
    except Exception as e:
        raise _Unavailable(e) from e

    # Past here a record exists and this node owns the work. Whatever goes wrong
    # now is the DOWNLOAD failing, not the abstraction being absent, and it is
    # raised rather than handed to a second downloader.
    #
    # deliver waits for the bytes and then says "I have them", which Manager's
    # caller needs: download_url is synchronous and ComfyUI reads the file the
    # moment it returns. Who moved the bytes, whether an existing job was
    # resumed, and what the partial file was called are all settled inside the
    # layer now. They were four hand-rolled decisions here, and every one of
    # them was a decision the next adopter would have had to make again.
    service.deliver(job_id)


def download_url(model_url: str, model_dir: str, filename: str):
    """Manager's dispatcher, with the abstraction in front of it.

    Two kinds of failure, and only one of them is a reason to fall back.

    The abstraction being UNAVAILABLE -- no facade on the path, a store that
    cannot be opened -- must never stop bytes from moving, because the
    downloader worked before this node existed. Fall back.

    The DOWNLOAD failing is the opposite. A short transfer, a digest mismatch,
    a 401 from a gated repository -- handing any of those to a transport with no
    resume, no temp file and no integrity check would re-fetch from zero and
    leave the truncated file at the final name, the exact bug this node exists
    to remove, reintroduced by its own error handling. Worse, a record already
    exists by then, so the fallback puts a second executor on a live job. Let it
    raise: Manager already expects download_url to throw, and the partial stays
    on disk for the next attempt to resume from.

    Which of the two it was is decided by _through_the_abstraction, at the one
    place that knows -- whether a job was submitted -- rather than by a list of
    exception types out here. That list was the bug: it named the facade's own
    errors, so an ordinary HTTPError from the network was not on it, and a real
    401 on a gated Hugging Face repository was reported as the abstraction being
    unavailable and fell through to Manager while our record sat RUNNING.
    """
    try:
        return _through_the_abstraction(model_url, model_dir, filename)
    except _Unavailable as e:
        LOG.warning(
            "[abstraction] unavailable, falling back to ComfyUI-Manager's downloader "
            "for %s: %r", model_url, e,
        )
        return _original_download_url(model_url, model_dir, filename)
    except Exception as e:
        LOG.error("[abstraction] download failed for %s: %r", model_url, e)
        raise


def download_url_with_agent(url: str, save_path: str):
    """Manager's OTHER model download, which takes a whole path rather than a
    directory and a name.

    do_install_model picks between the two by URL: GitHub, Hugging Face and
    heibox go through download_url, and EVERYTHING ELSE comes here -- Civitai
    among them. Leaving this unpatched does not break anything, which is why it
    survived: those downloads simply work the old way, unrecorded, unresumable
    and never handed to a supervisor, with nothing to tell anyone that half
    their model sources are not covered.

    And it is not the minority branch it looks like. `model_download_by_agent`
    in Manager's config sends EVERY model here regardless of host, and the
    machine this was first run on had it on.

    It returns True, because Manager's original does and its caller reads it:

        res = download_url_with_agent(model_url, model_path)
        ...
        if res: return 'success'
        return f"Model installation error: {model_url}"

    Returning None here -- which is what a function written to look like
    download_url returns -- put "Model installation error" in the UI for a
    download that had in fact arrived, been proven, and been recorded COMPLETE.
    A facade must not change what the operation MEANS to its caller
    (`docs/integrating.md` §3), and a success reported as a failure is the
    plainest possible case of that.
    """
    model_dir, filename = os.path.split(save_path)
    try:
        _through_the_abstraction(url, model_dir, filename)
        return True
    except _Unavailable as e:
        LOG.warning(
            "[abstraction] unavailable, falling back to ComfyUI-Manager's downloader "
            "for %s: %r", url, e,
        )
        return _original_download_url_with_agent(url, save_path)
    except Exception as e:
        # Manager's own agent downloader returns False rather than raising, and
        # do_install_model turns that into "Model installation error". Raising
        # here would be caught one level up and reported as an Exception instead
        # -- a different message for the same event, which is again the provider
        # changing what the operation means.
        LOG.error("[abstraction] download failed for %s: %r", url, e)
        return False


def _install() -> None:
    global _original_download_url, _original_download_url_with_agent

    if not _add_facade_to_path():
        LOG.warning(
            "[abstraction] facade not found; set ABSTRACTION_HOME to the repository "
            "root. ComfyUI-Manager's own downloader is untouched."
        )
        return

    downloader, server = _manager_modules()
    if downloader is None:
        return

    _original_download_url = downloader.download_url
    _original_download_url_with_agent = getattr(downloader, "download_url_with_agent", None)

    # Both modules, and this is the whole trick. manager_server does
    #     from manager_downloader import download_url, download_url_with_agent
    # which copies the functions into ITS namespace at import time, so rebinding
    # the downloader module alone would leave every actual call site still
    # calling the original. Rebinding only the server module would in turn miss
    # anyone who imports the downloader later.
    #
    # Both FUNCTIONS, because do_install_model chooses between them by URL and
    # patching one covers only the hosts that match it.
    downloader.download_url = download_url
    if server is not None:
        server.download_url = download_url
    if _original_download_url_with_agent is not None:
        downloader.download_url_with_agent = download_url_with_agent
        if server is not None:
            server.download_url_with_agent = download_url_with_agent

    LOG.info("[abstraction] ComfyUI model downloads now go through the job store")


_install()
