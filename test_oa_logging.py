"""ABSTRACTION_COMFYUI_LOGGING=service without the logging package fails open.

ComfyUI keeps loading and logging as usual; one line names the missing
package. The packaged integration ships the abstraction-logging package, so
the shipped app does not reach this state. The adoption tests with the
package present live in adopters/comfyui/test_node.py.
"""

import dataclasses
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


def load():
    spec = importlib.util.spec_from_file_location("oa_logging_absent_test", HERE / "oa_logging.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AbsentLoggingPackageTests(unittest.TestCase):
    def test_a_missing_logging_package_names_it_and_attaches_nothing(self):
        module = load()
        absent = {"abstraction.logging": None, "abstraction.logging.handler": None}
        root = __import__("logging").getLogger()
        handlers = list(root.handlers)
        with patch.dict(os.environ, {module.ENV: "service"}), patch.dict(sys.modules, absent), \
                self.assertLogs("abstraction", "WARNING") as logged:
            status = module.install()
        self.assertEqual((status.enabled, status.attached, status.code), (True, False, module.SDK_UNAVAILABLE))
        self.assertIn("abstraction.logging", status.detail)
        self.assertEqual(len(logged.output), 1)
        self.assertEqual(root.handlers, handlers)

    def test_an_unsupported_value_attaches_nothing(self):
        module = load()
        with patch.dict(os.environ, {module.ENV: "file"}), self.assertLogs("abstraction", "WARNING"):
            status = module.install()
        self.assertEqual((status.attached, status.code), (False, module.INVALID_CONFIGURATION))


@dataclasses.dataclass
class _Record:
    msg: str = ""
    attrs: dict = dataclasses.field(default_factory=dict)


class RedactionTests(unittest.TestCase):
    def test_a_record_leaves_redacted(self):
        module = load()
        written = []
        sink = module._Sink(lambda: SimpleNamespace(write=written.append))
        sink.write(_Record("token=abc123 sent", {"exception": "Error: password=hunter2", "logger": "x"}))
        self.assertEqual(written, [_Record("token=[redacted] sent", {"exception": "Error: password=[redacted]",
                                                                     "logger": "x"})])

    def test_a_failed_redaction_sends_the_record_once_without_its_text(self):
        """The delivery thread retries a record whose write raised; a record
        that redaction cannot handle would be retried forever."""
        module = load()
        written = []
        sink = module._Sink(lambda: SimpleNamespace(write=written.append))

        def broken(value):
            raise RecursionError("maximum recursion depth exceeded")
        record = _Record("token=abc123", {"exception": "Error: secret", "exception.truncated": "true", "logger": "x"})
        with patch.object(module.oa_redact, "text", broken), self.assertLogs("abstraction", "WARNING"):
            sink.write(record)
        self.assertEqual(written, [_Record(module.UNREDACTED, {"logger": "x"})])


if __name__ == "__main__":
    unittest.main()
