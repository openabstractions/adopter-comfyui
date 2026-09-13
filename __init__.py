"""ComfyUI-Manager download adapter using the installed job service.

Normal downloads use generated Python clients over shared native local IPC.
The service owns backend transfer and result storage. This adapter retains a
caller recovery record and atomically delivers complete result bytes into the
application's model directory. Service failures remain explicit.

ABSTRACTION_COMFYUI_PROVIDER=legacy deliberately selects the previous bundled
provider and its historical fallback behavior. See SERVICE.md for prerequisites,
recovery and the limits of the current anonymous HTTP execution profile.
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
        roots += [os.path.join(home, "openabstractions-flat", "abstraction-" + layer, "python") for layer in layers]
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
    provider = os.environ.get("ABSTRACTION_COMFYUI_PROVIDER", "service")
    if provider == "legacy":
        return _through_legacy_provider(model_url, model_dir, filename)
    if provider != "service":
        raise RuntimeError("ABSTRACTION_COMFYUI_PROVIDER must be service or legacy")
    from .service import deliver
    return deliver(_mirrored(model_url), model_dir, filename)


def _through_legacy_provider(model_url: str, model_dir: str, filename: str) -> None:
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
    """Manager download entrypoint. Service failures propagate; explicit legacy
    mode retains its historical pre-admission fallback."""
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
    """Manager agent entrypoint: True for completed delivery, False for failure.
    Explicit legacy mode retains its historical fallback behavior."""
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

    if os.environ.get("ABSTRACTION_COMFYUI_PROVIDER", "service") == "legacy" and not _add_facade_to_path():
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

    LOG.info("[abstraction] ComfyUI model downloads use %s",
             os.environ.get("ABSTRACTION_COMFYUI_PROVIDER", "service"))


_install()
