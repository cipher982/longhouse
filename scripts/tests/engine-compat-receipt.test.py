#!/usr/bin/env python3
"""The engine-compat receipt says passed only for a smoke that actually ran."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("engine_compat_receipt", ROOT / "scripts" / "qa" / "engine_compat_receipt.py")
assert SPEC and SPEC.loader
receipt_module = importlib.util.module_from_spec(SPEC)
sys.modules["engine_compat_receipt"] = receipt_module
SPEC.loader.exec_module(receipt_module)

SHA = "a" * 40
PREVIOUS = {
    "previous_release_tag": "v0.1.59",
    "asset": "longhouse-engine-linux-arm64",
    "platform": "linux-arm64",
    "engine_sha256": "c" * 64,
}


def junit(tests: int, failures: int = 0, errors: int = 0, skipped: int = 0) -> str:
    return (
        f'<?xml version="1.0"?><testsuites><testsuite name="pytest" tests="{tests}" failures="{failures}" '
        f'errors="{errors}" skipped="{skipped}"></testsuite></testsuites>'
    )


class EngineCompatReceiptTests(unittest.TestCase):
    def write(self, *, metadata: dict, report: str | None) -> tuple[int, dict | None]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "previous.json").write_text(json.dumps(metadata))
            argv = ["--metadata", str(root / "previous.json"), "--output", str(root / "receipt.json"), "--source-sha", SHA]
            if report is not None:
                (root / "junit.xml").write_text(report)
                argv += ["--junit", str(root / "junit.xml")]
            code = receipt_module.main(argv)
            out = root / "receipt.json"
            return code, json.loads(out.read_text()) if out.exists() else None

    def test_a_clean_run_passes_and_names_the_engine_it_used(self) -> None:
        code, receipt = self.write(metadata=PREVIOUS, report=junit(6))
        self.assertEqual(code, 0)
        self.assertEqual(receipt["schema"], "longhouse.engine-compat.v1")
        self.assertEqual(receipt["result"], "passed")
        self.assertEqual(receipt["source_sha"], SHA)
        self.assertEqual(receipt["previous_release_tag"], "v0.1.59")
        self.assertEqual(receipt["previous_engine"]["engine_sha256"], "c" * 64)
        self.assertEqual(receipt["counts"]["tests"], 6)

    def test_all_tests_skipped_is_not_a_pass(self) -> None:
        # pytest exits 0 when the fixture skips (for example the engine binary was not found).
        code, receipt = self.write(metadata=PREVIOUS, report=junit(6, skipped=6))
        self.assertEqual(code, 1)
        self.assertIsNone(receipt)

    def test_any_skip_failure_or_error_is_not_a_pass(self) -> None:
        for report in (junit(6, skipped=1), junit(6, failures=1), junit(6, errors=1), junit(0)):
            code, receipt = self.write(metadata=PREVIOUS, report=report)
            self.assertEqual(code, 1, report)
            self.assertIsNone(receipt)

    def test_no_report_means_the_smoke_did_not_run(self) -> None:
        code, receipt = self.write(metadata=PREVIOUS, report=None)
        self.assertEqual(code, 1)
        self.assertIsNone(receipt)

    def test_no_previous_release_is_recorded_as_skipped_never_passed(self) -> None:
        code, receipt = self.write(metadata={"skipped": "no published release precedes the candidate"}, report=None)
        self.assertEqual(code, 0)
        self.assertEqual(receipt["result"], "skipped")
        self.assertIn("no published release", receipt["reason"])
        self.assertNotIn("previous_release_tag", receipt)

    def test_metadata_without_the_engine_identity_is_refused(self) -> None:
        code, receipt = self.write(metadata={"previous_release_tag": "v0.1.59"}, report=junit(6))
        self.assertEqual(code, 1)
        self.assertIsNone(receipt)


if __name__ == "__main__":
    unittest.main(verbosity=1)
