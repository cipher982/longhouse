#!/usr/bin/env python3
"""False-pass boundaries for the local terminal-fidelity composer, without providers."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("terminal_fidelity_gate", ROOT / "scripts/qa/terminal-fidelity-gate.py")
assert spec is not None and spec.loader is not None
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

RUN = "00000000-0000-4000-8000-000000000001"
SESSION = "00000000-0000-4000-8000-000000000002"
OTHER_SESSION = "00000000-0000-4000-8000-000000000003"


class FidelityBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        # Deliberately not a provider-convention filename: only ownership evidence
        # may select the original bytes for the native client's immutability check.
        self.source = self.root / "actual-owned-history.data"
        self.source.write_bytes(b"original provider history\n")
        self.report = {
            "provider": "codex",
            "device_id": "machine",
            "verdict": "green",
            "session_id": SESSION,
            "run_id": RUN,
            "marker": "LH_SERVED_ALDER_FERN",
        }
        self.claim = {
            "provider": "codex",
            "session_id": SESSION,
            "run_id": RUN,
            "provider_identity_confirmed": True,
            "provider_thread_id": "native-thread",
            "source_path": str(self.source),
            "state": "terminal",
            "result": {"terminal_state": "run_completed"},
        }
        self.claim_path = self.root / f"{RUN}.json"

    def case(self):
        self.claim_path.write_text(json.dumps(self.claim))
        return gate.fidelity_case(self.report, "codex", "machine", self.root)

    def test_manifest_uses_exact_managed_source_without_touching_original_bytes(self):
        case, evidence = self.case()
        self.assertEqual(
            case, {"name": "machine/codex", "session_id": SESSION, "markers": ["LH_SERVED_ALDER_FERN"], "source_path": str(self.source)}
        )
        self.assertEqual(evidence["source_sha256"], gate.digest(self.source))
        self.assertEqual(self.source.read_bytes(), b"original provider history\n")

    def test_other_sessions_source_cannot_qualify_successful_console_case(self):
        self.claim["session_id"] = OTHER_SESSION
        with self.assertRaises(ValueError):
            self.case()
        self.claim["session_id"] = SESSION
        self.claim["provider_identity_confirmed"] = False
        with self.assertRaises(ValueError):
            self.case()

    def test_upstream_failure_stays_failure_even_with_a_readable_source(self):
        self.claim["result"]["terminal_state"] = "run_failed"
        with self.assertRaises(ValueError):
            self.case()
        self.claim["result"]["terminal_state"] = "run_completed"
        self.report["verdict"] = "error"
        with self.assertRaises(ValueError):
            self.case()

    def test_required_stage_or_failed_requested_provider_cannot_be_omitted(self):
        summary = {
            "providers": [{"provider": "codex", "status": "pass"}],
            "stages": {name: {"status": "pass"} for name in gate.REQUIRED_STAGES},
        }
        self.assertTrue(gate.passing(summary))
        for name in gate.REQUIRED_STAGES:
            with self.subTest(stage=name):
                stage = summary["stages"].pop(name)
                self.assertFalse(gate.passing(summary))
                summary["stages"][name] = stage
        summary["providers"].append({"provider": "cursor", "status": "fail"})
        self.assertFalse(gate.passing(summary))

    def test_timed_out_child_retains_output_without_credentials_and_never_passes(self):
        token = "private-runtime-token-for-test"
        result = gate.command_stage(
            "timeout",
            [sys.executable, "-c", "import os,time; print(os.environ['PROOF_TOKEN'], flush=True); time.sleep(30)"],
            self.root,
            {**os.environ, "PROOF_TOKEN": token},
            token,
            1.0,
        )
        self.assertEqual(result["status"], "fail")
        self.assertNotEqual(result["exit_code"], 0)
        self.assertEqual((self.root / "timeout.log").read_text(), "[REDACTED]\n")
        self.assertEqual(json.loads((self.root / "timeout.json").read_text())["status"], "fail")
        self.assertNotIn(token, (self.root / "timeout.json").read_text())


if __name__ == "__main__":
    unittest.main()
