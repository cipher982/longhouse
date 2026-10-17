#!/usr/bin/env python3
"""CI receipts: write-once, stamped last, newest first, scoped to their prefix."""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("ci_receipts", ROOT / "scripts" / "ops" / "ci_receipts.py")
assert SPEC and SPEC.loader
ci_receipts = importlib.util.module_from_spec(SPEC)
sys.modules["ci_receipts"] = ci_receipts
SPEC.loader.exec_module(ci_receipts)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_s3 import BUCKET  # noqa: E402
from fake_s3 import FakeS3  # noqa: E402
from fake_s3 import PREFIX  # noqa: E402


def identity(run_id: int, attempt: int = 1) -> dict[str, str]:
    return {"run_id": str(run_id), "run_attempt": str(attempt), "repository": "o/r", "workflow_ref": "o/r/.github/workflows/w.yml@refs/heads/main", "sha": "a" * 40}


class CiReceiptsTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeS3()
        self.fake.__enter__()
        self.addCleanup(self.fake.__exit__)
        self.store = ci_receipts.ReceiptStore(ci_receipts.S3(self.fake.endpoint, BUCKET, "ak", "sk"), PREFIX)

    def test_round_trip_is_stamped_and_signed(self):
        instance = self.store.put("engine-compat", "a" * 40, {"receipt.json": b'{"result":"passed"}'}, identity=identity(7, 2))
        self.assertEqual(instance, "7-2")
        (receipt,) = self.store.instances("engine-compat", "a" * 40)
        self.assertEqual((receipt.run_id, receipt.attempt, receipt.stamp["sha"]), (7, 2, "a" * 40))
        self.assertEqual(receipt.json("receipt.json"), {"result": "passed"})
        self.assertGreater(self.fake.signed_requests, 0)
        self.assertIn(f"{PREFIX}/engine-compat/{'a' * 40}/7-2/_receipt.json", self.fake.objects)

    def test_write_once(self):
        self.store.put("k", "s", {"f": b"1"}, identity=identity(1))
        with self.assertRaisesRegex(ci_receipts.ReceiptError, "write-once"):
            self.store.put("k", "s", {"f": b"2"}, identity=identity(1))
        self.assertEqual(self.store.instances("k", "s")[0].read("f"), b"1")

    def test_a_receipt_without_a_stamp_is_invisible(self):
        self.fake.objects[f"{PREFIX}/k/s/5-1/f"] = b"half written"
        self.assertEqual(self.store.instances("k", "s"), [])

    def test_a_forged_or_mismatched_stamp_is_skipped(self):
        self.store.put("k", "s", {"f": b"1"}, identity=identity(1))
        self.fake.objects[f"{PREFIX}/k/s/9-1/_receipt.json"] = json.dumps({"schema": ci_receipts.STAMP_SCHEMA, "run_id": 8, "run_attempt": 1}).encode()
        self.fake.objects[f"{PREFIX}/k/s/10-1/_receipt.json"] = b"not json"
        self.assertEqual([r.instance for r in self.store.instances("k", "s")], ["1-1"])

    def test_newest_first_and_attempt_selection(self):
        for run, attempt in ((3, 1), (3, 2), (3, 3)):
            self.store.put("runtime-verification", "3", {"v.json": b"{}"}, identity=identity(run, attempt))
        receipts = self.store.instances("runtime-verification", "3")
        self.assertEqual(ci_receipts.select(receipts, instance=None, max_attempt=2).instance, "3-2")
        self.assertEqual(ci_receipts.select(receipts, instance="3-1", max_attempt=None).instance, "3-1")
        with self.assertRaisesRegex(ci_receipts.ReceiptError, "no matching"):
            ci_receipts.select(receipts, instance="4-1", max_attempt=None)

    def test_subjects_do_not_leak_across_each_other(self):
        self.store.put("k", "s1", {"f": b"1"}, identity=identity(1))
        self.assertEqual(self.store.instances("k", "s2"), [])

    def test_unsafe_names_and_missing_identity_are_refused(self):
        with self.assertRaisesRegex(ci_receipts.ReceiptError, "unsafe"):
            self.store.put("k", "../x", {"f": b"1"}, identity=identity(1))
        with self.assertRaisesRegex(ci_receipts.ReceiptError, "unsafe"):
            self.store.put("k", "s", {"../f": b"1"}, identity=identity(1))
        with self.assertRaisesRegex(ci_receipts.ReceiptError, "run identity"):
            self.store.put("k", "s", {"f": b"1"}, identity={"run_id": "", "run_attempt": ""})

    def test_unconfigured_store_names_what_is_missing(self):
        with self.assertRaisesRegex(ci_receipts.ReceiptError, "CI_RECEIPTS_S3_ENDPOINT"):
            ci_receipts.store_from_environment({})


if __name__ == "__main__":
    unittest.main()
