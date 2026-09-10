"""Does the rebinding actually reach the call sites?

The integration is a monkey-patch, and a monkey-patch that misses is worse than
none: everything looks installed, the log says so, and the bytes still go
through the old path. So this builds a stand-in ComfyUI-Manager with the import
style the real one uses -- ``from manager_downloader import download_url``, which
copies the function into the consuming module's namespace at import time -- and
checks that a call made the way manager_server makes it lands in ours.

It runs against a stand-in rather than the installed ComfyUI on purpose: the
thing under test is the seam, and a test that needs somebody's ComfyUI running
is a test that will not be run.
"""

import http.server
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
NODE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "abstraction_downloads")

FAKE_DOWNLOADER = '''
calls = []

def download_url(model_url, model_dir, filename):
    calls.append((model_url, model_dir, filename))
    return "original"

def download_url_with_agent(url, save_path):
    calls.append(("agent", url, save_path))
    return "original-agent"
'''

# The import style is the point of the whole test. manager_server.py L198 binds
# the function into its OWN namespace, so patching the downloader module alone
# would leave this call site calling the original.
FAKE_SERVER = '''
import os
import manager_downloader
from manager_downloader import download_url, download_url_with_agent

# do_install_model picks by URL exactly as Manager does: github, huggingface and
# heibox go one way and everything else -- civitai included -- goes to the agent
# with a whole path instead of a directory and a name.
def do_install_model(url, model_dir, filename):
    if any(h in url for h in ("github.com", "huggingface.co", "heibox")):
        return download_url(url, model_dir, filename)
    return download_url_with_agent(url, os.path.join(model_dir, filename))

# What the USER is told, which is not the same thing. Manager reads the agent
# downloader's return value as a boolean and turns a falsy one into an error
# message, so a function that downloads perfectly and returns None reports a
# failure for a model that arrived.
def install_model_message(url, model_dir, filename):
    res = do_install_model(url, model_dir, filename)
    if any(h in url for h in ("github.com", "huggingface.co", "heibox")):
        res = True  # download_url raises rather than returning falsy
    if res:
        return "success"
    return "Model installation error: " + url
'''


class Serve(http.server.BaseHTTPRequestHandler):
    body = b""
    # Set to cut the first response short, the way a dropped connection or a
    # closed application does. Cleared afterwards so the retry can succeed.
    die_after = 0
    # Set to refuse outright. 401 is what Hugging Face answers for a gated
    # repository -- the first real failure this node met in a live ComfyUI.
    status = 0
    ranges_seen = []

    def _refuse(self) -> bool:
        if not Serve.status:
            return False
        self.send_response(Serve.status)
        self.send_header("Content-Length", "0")
        self.end_headers()
        return True

    def do_HEAD(self):
        if self._refuse():
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.body)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):
        if self._refuse():
            return
        rng = self.headers.get("Range", "")
        Serve.ranges_seen.append(rng)
        start = 0
        if rng.startswith("bytes="):
            start = int(rng[len("bytes="):].split("-")[0])

        body = self.body[start:]
        if Serve.die_after:
            # Announce the full length and then send less: the transfer looks
            # healthy right up to the moment it stops, which is what a dropped
            # connection actually looks like.
            self.send_response(206 if start else 200)
            if start:
                self.send_header(
                    "Content-Range", f"bytes {start}-{len(self.body) - 1}/{len(self.body)}"
                )
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(body[: Serve.die_after])
            self.wfile.flush()
            self.close_connection = True
            return

        self.send_response(206 if start else 200)
        if start:
            self.send_header(
                "Content-Range", f"bytes {start}-{len(self.body) - 1}/{len(self.body)}"
            )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class NodeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name

        glob_dir = os.path.join(root, "custom_nodes", "ComfyUI-Manager", "glob")
        os.makedirs(glob_dir)
        with open(os.path.join(glob_dir, "manager_downloader.py"), "w") as f:
            f.write(FAKE_DOWNLOADER)
        with open(os.path.join(glob_dir, "manager_server.py"), "w") as f:
            f.write(FAKE_SERVER)

        shutil.copytree(NODE, os.path.join(root, "custom_nodes", "abstraction_downloads"))

        self.store_dir = os.path.join(root, "store")
        self.models = os.path.join(root, "models", "checkpoints")
        os.makedirs(self.models)

        # A store of our own, so a test can never reach the real one.
        self._env = dict(os.environ)
        os.environ["ABSTRACTION_STORE"] = self.store_dir
        os.environ["ABSTRACTION_HOME"] = REPO
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._env)))

        self._modules = set(sys.modules)
        self._path = list(sys.path)
        self.addCleanup(self._restore_imports)

        sys.path.insert(0, os.path.join(root, "custom_nodes"))

    def _restore_imports(self):
        for name in set(sys.modules) - self._modules:
            del sys.modules[name]
        sys.path[:] = self._path

    def serve(self, body: bytes, die_after: int = 0, status: int = 0) -> str:
        Serve.body = body
        Serve.die_after = die_after
        Serve.status = status
        Serve.ranges_seen = []
        self.addCleanup(lambda: setattr(Serve, "status", 0))
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        httpd = http.server.HTTPServer(("127.0.0.1", port), Serve)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        return f"http://127.0.0.1:{port}/model.safetensors"

    def test_the_rebinding_reaches_the_call_site(self):
        import abstraction_downloads  # noqa: F401  (installs on import)
        import manager_downloader
        import manager_server

        self.assertIs(
            manager_downloader.download_url, abstraction_downloads.download_url,
            "the downloader module still points at the original",
        )
        self.assertIs(
            manager_server.download_url, abstraction_downloads.download_url,
            "manager_server bound the original at import time and kept it -- "
            "patching only the downloader module would have missed every call site",
        )

    def test_a_download_made_the_way_manager_makes_it_goes_through_the_store(self):
        import abstraction_downloads  # noqa: F401
        import manager_downloader
        import manager_server

        body = b"weights" * 5000
        url = self.serve(body)

        # Called exactly as manager_server.do_install_model calls it.
        manager_server.do_install_model(url, self.models, "model.safetensors")

        final = os.path.join(self.models, "model.safetensors")
        self.assertTrue(os.path.exists(final), "the file did not arrive")
        with open(final, "rb") as f:
            self.assertEqual(f.read(), body)

        # Nothing reached Manager's own downloader.
        self.assertEqual(manager_downloader.calls, [])

        # And there is a record, which is the thing torchvision cannot leave
        # behind: the download is now work that outlived the call.
        sys.path.insert(0, os.path.join(REPO, "job", "python"))
        sys.path.insert(0, os.path.join(REPO, "download", "python"))
        import abstraction_download as dl
        from abstraction_job import COMPLETE, FileStore

        records = [r for r in FileStore(self.store_dir).list() if r.kind == dl.KIND]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].state, COMPLETE)
        self.assertEqual(dl.spec_of(records[0]).sink.final, dl.portable(final))

        # No .part left behind: delivery is a rename, so the final name never
        # exists while the bytes are incomplete. That is what stops a truncated
        # file from presenting to ComfyUI as an installed model.
        self.assertFalse(os.path.exists(final + ".part"))

    def test_an_unusable_abstraction_falls_back_instead_of_breaking_downloads(self):
        """The downloader worked before this node existed and has to keep
        working when the node is broken. An abstraction that can stop bytes from
        moving is worse than no abstraction."""
        import abstraction_downloads
        import manager_downloader
        import manager_server

        def explode(*a, **k):
            raise abstraction_downloads._Unavailable("the store is on fire")

        abstraction_downloads._through_the_abstraction = explode

        # Both branches of do_install_model, because both are patched and each
        # falls back to a different original.
        out = manager_server.do_install_model(
            "https://huggingface.co/x", self.models, "x.bin")
        self.assertEqual(out, "original")

        out = manager_server.do_install_model(
            "https://civitai.com/x", self.models, "x.bin")
        self.assertEqual(out, "original-agent")

        self.assertEqual(len(manager_downloader.calls), 2)

    def test_a_refusal_from_the_server_is_not_the_abstraction_being_absent(self):
        """The distinction the fallback rests on, and the one it got wrong.

        A live ComfyUI asked for a gated Hugging Face model and got 401. That is
        the DOWNLOAD failing, but urllib's HTTPError is not one of the facade's
        own exception types, so it was reported as "unavailable" and handed to
        Manager -- with our record already in the store, RUNNING, its lease held
        by a process that had moved on. Two executors for one transfer, which is
        `docs/integrating.md` §1, arrived at by an error handler.
        """
        import abstraction_downloads  # noqa: F401
        import manager_downloader
        import manager_server

        sys.path.insert(0, os.path.join(REPO, "job", "python"))
        sys.path.insert(0, os.path.join(REPO, "download", "python"))
        import abstraction_download as dl
        from abstraction_job import FAILED, FileStore

        url = self.serve(b"", status=401).replace("/model.safetensors", "/gated.safetensors")

        # download_url, which Manager expects to throw.
        with self.assertRaises(Exception):
            manager_server.download_url(url, self.models, "gated.safetensors")

        self.assertEqual(
            manager_downloader.calls, [],
            "a server refusal was treated as the abstraction being unavailable, "
            "so Manager's downloader was set on a job we had already recorded",
        )

        # The record is still there, and still says why. That is the point of
        # not falling back. And the job is OVER: a gated repository answers 401
        # until the person adds a token, which is a new request, so a record
        # that stayed adoptable would be fetched again on every sweep for as
        # long as the store exists. Re-asking resumes the same partial under a
        # new job.
        records = [r for r in FileStore(self.store_dir).list() if r.kind == dl.KIND]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].state, FAILED)
        self.assertIn("401", records[0].error)

        # download_url_with_agent, which Manager expects to report rather than
        # throw -- and which must not fall back either.
        self.assertIs(
            manager_server.download_url_with_agent(
                url, os.path.join(self.models, "gated2.safetensors")),
            False,
            "the agent branch must report Manager's own failure value",
        )
        self.assertEqual(manager_downloader.calls, [])

    def test_the_call_returns_when_nobody_is_working_the_job(self):
        """This suite ran for ninety minutes on somebody's machine and had to be
        killed.

        Every waiter in the layer ended on something the record SAYS: a terminal
        state, or an error somebody wrote down. A worker that dies before it
        records anything -- a supervisor that stopped after the nudge, a claim
        that was never won -- writes nothing at all, and download_url waits for a
        transfer that is not happening.

        A ComfyUI-Manager install that never returns is worse than one that
        fails: the user gets no error, no model, and a web request that hangs
        until they restart the server.
        """
        import abstraction_downloads  # noqa: F401
        import manager_downloader
        import manager_server

        sys.path.insert(0, os.path.join(REPO, "job", "python"))
        sys.path.insert(0, os.path.join(REPO, "download", "python"))
        import abstraction_download as dl

        real_discover = dl.discover

        def worker_that_records_nothing():
            svc = real_discover()
            svc.runner.lease_ttl = 0.2
            svc.runner.run = lambda job_id: None
            return svc

        dl.discover = worker_that_records_nothing
        self.addCleanup(lambda: setattr(dl, "discover", real_discover))

        began = time.monotonic()
        self.assertIs(
            manager_server.do_install_model(
                "http://127.0.0.1:9/stuck.safetensors", self.models, "stuck.safetensors"
            ),
            False,
            "a job nobody is working was reported as an install that worked",
        )
        self.assertLess(time.monotonic() - began, 30, "it waited on a dead worker")
        self.assertEqual(manager_downloader.calls, [],
                         "a job already in the store was handed to a second downloader")

    def test_a_download_that_worked_is_reported_as_success(self):
        """What the user is told, which the node got wrong for every model.

        Manager reads download_url_with_agent's return value as a boolean and
        writes "Model installation error" into the UI when it is falsy. A
        replacement shaped like download_url returns None, so a model that
        arrived, was proven and was recorded COMPLETE was reported as a failed
        install -- and `model_download_by_agent` sends EVERY model down this
        branch, so on a machine with that set it was every install.
        """
        import abstraction_downloads  # noqa: F401
        import manager_server

        body = b"reported" * 2500
        url = self.serve(body).replace("/model.safetensors", "/told.safetensors")

        self.assertEqual(
            manager_server.install_model_message(url, self.models, "told.safetensors"),
            "success",
            "the file arrived and Manager still told the user the install failed",
        )
        self.assertTrue(os.path.exists(os.path.join(self.models, "told.safetensors")))

    def test_the_agent_branch_goes_through_the_store_too(self):
        """do_install_model routes anything that is not github, huggingface or
        heibox to download_url_with_agent. Patching only download_url leaves
        Civitai -- and every other model host -- downloading the old way:
        unrecorded, unresumable, never handed to a supervisor, and silent about
        it."""
        import abstraction_downloads  # noqa: F401
        import manager_downloader
        import manager_server

        self.assertIs(
            manager_server.download_url_with_agent,
            abstraction_downloads.download_url_with_agent,
            "manager_server still holds Manager's own agent downloader",
        )

        body = b"lora" * 4000
        url = self.serve(body).replace("/model.safetensors", "/lora.safetensors")

        manager_server.do_install_model(url, self.models, "lora.safetensors")

        final = os.path.join(self.models, "lora.safetensors")
        self.assertTrue(os.path.exists(final), "the file did not arrive")
        with open(final, "rb") as f:
            self.assertEqual(f.read(), body)
        self.assertEqual(manager_downloader.calls, [],
                         "it fell through to Manager's own downloader")

        sys.path.insert(0, os.path.join(REPO, "job", "python"))
        sys.path.insert(0, os.path.join(REPO, "download", "python"))
        import abstraction_download as dl
        from abstraction_job import COMPLETE, FileStore

        records = [r for r in FileStore(self.store_dir).list() if r.kind == dl.KIND]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].state, COMPLETE)
        self.assertEqual(dl.spec_of(records[0]).sink.final, dl.portable(final))

    def test_a_file_already_present_is_not_fetched_again(self):
        import abstraction_downloads  # noqa: F401
        import manager_server

        final = os.path.join(self.models, "already.safetensors")
        with open(final, "wb") as f:
            f.write(b"mine")

        # An unreachable URL: if this tries to fetch, the test fails loudly.
        manager_server.do_install_model(
            "http://127.0.0.1:9/never", self.models, "already.safetensors"
        )
        with open(final, "rb") as f:
            self.assertEqual(f.read(), b"mine")

    def test_an_interrupted_download_resumes_instead_of_starting_over(self):
        """The claim this whole node exists to make.

        Manager's default transport writes straight to the final filename with
        no Range and no temp file, so an interrupted download is both LOST and
        indistinguishable from a finished one. Here the same interruption has to
        leave a resumable partial, and the retry has to ask for the bytes it does
        not have rather than all of them again.
        """
        import abstraction_downloads  # noqa: F401
        import manager_server

        sys.path.insert(0, os.path.join(REPO, "job", "python"))
        sys.path.insert(0, os.path.join(REPO, "download", "python"))
        import abstraction_download as dl
        from abstraction_job import COMPLETE, FileStore

        body = bytes(range(256)) * 400  # 102400 bytes
        half = len(body) // 2
        url = self.serve(body, die_after=half)

        final = os.path.join(self.models, "big.safetensors")

        # First attempt: dies halfway. A 127.0.0.1 URL is none of github,
        # huggingface or heibox, so this is the agent branch, which reports
        # failure the way Manager's own agent downloader does rather than
        # raising.
        self.assertIs(
            manager_server.do_install_model(url, self.models, "big.safetensors"),
            False,
        )

        store = FileStore(self.store_dir)
        records = [r for r in store.list() if r.kind == dl.KIND]
        self.assertEqual(len(records), 1)

        # The final name must NOT exist. This is the whole difference from
        # torchvision: a truncated file at the final name is what ComfyUI would
        # have listed as an installed model.
        self.assertFalse(os.path.exists(final), "a truncated file is sitting at the final name")

        partial = final + ".part"
        self.assertTrue(os.path.exists(partial), "nothing was kept to resume from")
        kept = os.path.getsize(partial)
        self.assertGreater(kept, 0)

        # And the record says how much was PROVEN, which is what a successor is
        # allowed to trust -- not merely how much was written.
        self.assertGreater(dl.checkpoint_of(records[0]).verified_prefix, 0)

        # Second attempt, with the source healthy again: same call, same node.
        Serve.die_after = 0
        Serve.ranges_seen = []
        manager_server.do_install_model(url, self.models, "big.safetensors")

        self.assertTrue(any(r.startswith("bytes=") for r in Serve.ranges_seen),
                        f"it started over instead of resuming: {Serve.ranges_seen}")

        with open(final, "rb") as f:
            self.assertEqual(f.read(), body, "the resumed file is not the artifact")
        self.assertFalse(os.path.exists(partial))

        records = [r for r in store.list() if r.kind == dl.KIND]
        self.assertEqual(len(records), 1, "the retry created a second job for one artifact")
        self.assertEqual(records[0].state, COMPLETE)

    def test_it_works_as_a_bare_copy_with_no_environment(self):
        """What a stranger's install actually looks like.

        They copy one directory into custom_nodes/ and restart. No clone of this
        repository, no pip install, no ABSTRACTION_HOME. If that does not work
        then the node is not a custom node, it is a project, and almost nobody
        will try it."""
        os.environ.pop("ABSTRACTION_HOME", None)

        import abstraction_downloads
        import manager_downloader
        import manager_server

        # The facade came from the copy that shipped inside the node.
        import abstraction_job
        self.assertIn(
            os.path.join("custom_nodes", "abstraction_downloads", "_vendor"),
            os.path.abspath(abstraction_job.__file__),
            "the facade was imported from outside the installed node -- this "
            "test proves nothing about a bare copy",
        )

        body = b"bare" * 3000
        url = self.serve(body).replace("/model.safetensors", "/bare.safetensors")
        manager_server.do_install_model(url, self.models, "bare.safetensors")

        final = os.path.join(self.models, "bare.safetensors")
        self.assertTrue(os.path.exists(final), "the file did not arrive")
        with open(final, "rb") as f:
            self.assertEqual(f.read(), body)
        self.assertEqual(manager_downloader.calls, [],
                         "it fell back to Manager's downloader, so the facade "
                         "was not importable from the vendored copy")

    def test_the_hugging_face_mirror_is_honoured(self):
        """Manager's download_url rewrites the URL through HF_ENDPOINT before it
        fetches anything. Replacing that function without carrying the rewrite
        sends every mirror user back to an origin that is slow or blocked for
        them -- and it looks like this node broke their downloads, because it
        did."""
        import abstraction_downloads
        import manager_server

        body = b"mirrored" * 2000
        url = self.serve(body).replace("/model.safetensors", "/mirror.safetensors")

        # The mirror is the only host actually serving these bytes; the origin
        # in the URL below does not exist. If the rewrite does not happen the
        # download cannot succeed.
        os.environ["HF_ENDPOINT"] = url[: url.index("/", len("http://"))]
        origin_url = "https://huggingface.co" + url[url.index("/", len("http://")):]

        manager_server.do_install_model(origin_url, self.models, "mirror.safetensors")

        final = os.path.join(self.models, "mirror.safetensors")
        self.assertTrue(
            os.path.exists(final),
            "nothing arrived -- HF_ENDPOINT was ignored and the origin was used",
        )
        with open(final, "rb") as f:
            self.assertEqual(f.read(), body)

    def test_the_vendored_copies_have_not_drifted(self):
        """A copy that can go stale silently would ship an old abstraction to
        everyone who installed the node, while every test here kept passing
        against the originals."""
        vendor = os.path.join(NODE, "_vendor")
        for rel, name in (
            (os.path.join("cas", "python"), "abstraction_cas.py"),
            (os.path.join("job", "python"), "abstraction_job.py"),
            (os.path.join("watch", "python"), "abstraction_watch.py"),
            (os.path.join("config", "python"), "abstraction_config.py"),
            (os.path.join("download", "python"), "abstraction_download.py"),
        ):
            with open(os.path.join(REPO, rel, name), "rb") as f:
                original = f.read()
            with open(os.path.join(vendor, name), "rb") as f:
                shipped = f.read()
            self.assertEqual(
                original, shipped,
                "%s differs from the original -- run scripts/vendor-comfyui.sh" % name,
            )


if __name__ == "__main__":
    unittest.main()
