"""Which provider supplied a reused file, and where the jobs view says so.

A file OA content storage supplies has no job and no request identity, so the
reuse record is the only place its origin is written. It used to hold the
endpoint the runtime resolved, which is a location, not a name. A resolved
binding now carries the reference the runtime returned, and its `provider` is
the logical provider that answered (abstraction-facade CONTRACT.md FAC-B4).

These cases load `storage_reuse` and `oa_jobs_view` directly, without the
ComfyUI Manager fixtures `test_node.py` builds, so they say only what they
check: the name the reuse path derives, and the row the view builds from a
retained record.

Run from adopters/comfyui: python test_reuse_reference.py
"""

import importlib
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LAYERS = os.path.join(REPO, "openabstractions-flat")
ADAPTER = os.path.join(LAYERS, "adopter-comfyui")
PACKAGE = "oa_comfyui_adapter_under_test"


def adapter():
    """The adapter's modules under a package name of this test's own.

    The real `__init__.py` registers ComfyUI nodes and patches Manager. These
    cases want the two modules and nothing else, so the package is a shell
    whose search path is the adapter directory.
    """
    if PACKAGE not in sys.modules:
        shell = types.ModuleType(PACKAGE)
        shell.__path__ = [ADAPTER]
        sys.modules[PACKAGE] = shell
    return (importlib.import_module(PACKAGE + ".storage_reuse"),
            importlib.import_module(PACKAGE + ".oa_jobs_view"))


def facade():
    """The facade client package, or None where it is not importable."""
    for layer in ("abstraction-facade", "abstraction-identity"):
        path = os.path.join(LAYERS, layer, "py")
        if os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)
    try:
        import abstraction.facade as wire
        import abstraction.facade.client as client
    except ImportError:
        return None
    return SimpleNamespace(wire=wire, client=client)


PROVIDER = "openabstractions.user-runtime"


class ProviderNameTest(unittest.TestCase):
    """What a bound content reader is named after."""

    def setUp(self):
        self.reuse, _ = adapter()

    def reader(self, transport):
        """A content reader holding one transport, as a resolved one does."""
        return SimpleNamespace(_transport=transport)

    def test_a_resolved_reader_names_the_provider_that_served_it(self):
        oa = facade()
        if oa is None:
            self.skipTest("the facade Python package is not importable beside this adapter")
        reference = oa.wire.ServiceReference(
            provider=PROVIDER, capability="abstraction.storage",
            contract="abstraction.storage/content-reader@1", guarantees=[],
            scope=oa.wire.Scope.LOCAL, transport="oa-framed-local@1",
            endpoint=r"\\.\pipe\openabstractions-user-S-1-5-21-store-v1")
        binding = oa.client.Binding(object(), reference.endpoint, reference=reference)
        self.assertEqual(self.reuse.provider_name(self.reader(binding)), PROVIDER)

    def test_the_name_is_never_the_endpoint_when_a_reference_is_there(self):
        oa = facade()
        if oa is None:
            self.skipTest("the facade Python package is not importable beside this adapter")
        endpoint = "/run/user/1000/openabstractions/store.sock"
        reference = oa.wire.ServiceReference(
            provider=PROVIDER, capability="abstraction.storage",
            contract="abstraction.storage/content-reader@1", guarantees=[],
            scope=oa.wire.Scope.LOCAL, transport="oa-framed-local@1", endpoint=endpoint)
        binding = oa.client.Binding(object(), endpoint, reference=reference)
        named = self.reuse.provider_name(self.reader(binding))
        self.assertEqual(named, PROVIDER)
        self.assertNotIn(os.sep, named)

    def test_a_reader_carrying_no_reference_names_what_it_has(self):
        self.assertEqual(self.reuse.provider_name(self.reader(SimpleNamespace(endpoint=b"oa-local-store"))),
                         "oa-local-store")
        self.assertEqual(self.reuse.provider_name(self.reader(SimpleNamespace(endpoint=""))),
                         self.reuse.STORAGE.contract)
        self.assertEqual(self.reuse.provider_name(object()), self.reuse.STORAGE.contract)


class ReusedRowTest(unittest.TestCase):
    """The row the jobs view builds from a retained reuse record."""

    def setUp(self):
        _, self.view = adapter()
        self.root = tempfile.mkdtemp(prefix="oa-reuse-row-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.models = os.path.join(self.root, "models", "checkpoints")
        os.makedirs(self.models)
        self.view.configure([os.path.join(self.root, "models")])
        self.addCleanup(self.view.configure)

    def write(self, destination, entries):
        with open(destination + ".oa-reuse.json", "w", encoding="utf-8") as record:
            json.dump(dict(version=1, files=entries), record)

    def entry(self, final, **rest):
        return dict(final=final, url="https://huggingface.co/oa/tiny/resolve/main/" + os.path.basename(final),
                    digest="sha256:" + "0" * 64, size=16, store=PROVIDER, at="2026-09-22T00:00:00Z", **rest)

    def rows(self):
        return self.view.rows(self.view.read_records(), [])

    def test_a_reused_file_row_names_the_provider_it_came_from(self):
        final = os.path.join(self.models, "model.safetensors")
        open(final, "wb").close()
        self.write(final, [self.entry(final)])
        row, = self.rows()
        self.assertEqual((row["kind"], row["source"], row["state"]), ("file", "reused", "complete"))
        self.assertEqual(row["provider"], PROVIDER)
        # store keeps the same word for a reader written before the record
        # named a provider.
        self.assertEqual(row["store"], PROVIDER)
        self.assertIsNone(row["identity"], "a file a provider supplied has no operation")

    def test_a_repository_every_file_of_which_was_reused_names_its_provider(self):
        base = os.path.join(self.models, "tiny")
        os.makedirs(os.path.join(base, "unet"))
        paths = ("a.bin", "unet/c.bin")
        for path in paths:
            open(os.path.join(base, *path.split("/")), "wb").close()
        self.write(base, [self.entry(os.path.join(base, *path.split("/")), repo="oa/tiny", path=path)
                          for path in paths])
        row, = self.rows()
        self.assertEqual((row["kind"], row["source"], row["files"], row["reused"]), ("repository", "reused", 2, 2))
        self.assertEqual((row["provider"], row["providers"]), (PROVIDER, [PROVIDER]))
        self.assertEqual((row["store"], row["stores"]), (PROVIDER, [PROVIDER]))
        self.assertEqual([child["provider"] for child in row["children"]], [PROVIDER, PROVIDER])

    def test_two_providers_leave_the_repository_row_without_one_name(self):
        base = os.path.join(self.models, "tiny")
        os.makedirs(base)
        entries = []
        for path, provider in (("a.bin", PROVIDER), ("b.bin", "lemonade.store")):
            open(os.path.join(base, path), "wb").close()
            entries.append(dict(self.entry(os.path.join(base, path), repo="oa/tiny", path=path), store=provider))
        self.write(base, entries)
        row, = self.rows()
        self.assertEqual(row["provider"], "")
        self.assertEqual(row["providers"], ["lemonade.store", PROVIDER])
        self.assertEqual(row["stores"], row["providers"])


if __name__ == "__main__":
    unittest.main()
