#!/usr/bin/env python3
"""The terminal-truth CI lane: pinned provider versions, the verdict script, and the workflow's shape."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PINS = ROOT / "scripts" / "ci" / "terminal-truth-providers.toml"
SUMMARY = ROOT / "scripts" / "ci" / "terminal_truth_summary.py"
WORKFLOW = ROOT / ".github" / "workflows" / "terminal-truth.yml"
BUDGET = ROOT / "schemas" / "terminal-truth-budget.json"
PROVIDERS = {"codex", "claude", "opencode", "pi", "omp", "antigravity"}


def row(cell: str, outcome: str = "pass", median: float | None = 0.2, requests: int = 1, stage: str | None = None) -> dict:
    provider, mode = cell.split("/")
    return {
        "cell": cell, "provider": provider, "mode": mode, "status": "measured", "outcome": outcome, "first_failed_stage": stage,
        "reason": "", "stages": {"preflight": {"ok": True, "evidence": {"model_requests_served": requests}}},
        "lanes": [{"lane": "workspace stream (SSE)", "median_delay_s": median}],
    }


class PinsTests(unittest.TestCase):
    def test_every_provider_the_lane_installs_has_an_exact_pin(self):
        pins = tomllib.loads(PINS.read_text())
        self.assertEqual(set(pins), PROVIDERS)
        for name, pin in pins.items():
            self.assertRegex(pin["version"], r"^\d+\.\d+\.\d+$", name)
            self.assertIn(pin["version"], pin["reports"], name)
            self.assertIn(pin["kind"], {"npm", "omp", "agy"}, name)
            if pin["kind"] == "npm":
                self.assertTrue(pin["package"], name)
            if pin["kind"] == "agy":
                self.assertRegex(pin["sha512"], r"^[0-9a-f]{128}$")
                self.assertIn(pin["version"], pin["url"])

    def test_the_workflow_installs_and_verifies_through_the_pins_and_never_the_unpinned_installers(self):
        text = WORKFLOW.read_text()
        self.assertIn("terminal_truth_providers.py install", text)
        self.assertIn("terminal_truth_providers.py verify", text)
        for forbidden in ("npm install", "https://omp.sh/install", "install.sh"):
            self.assertNotIn(forbidden, text)

    def test_install_refuses_an_unpinned_provider(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/ci/terminal_truth_providers.py"), "verify", "--providers", "cursor"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no pinned version for: cursor", result.stderr)


class WorkflowTests(unittest.TestCase):
    def test_push_paths_are_the_one_list_and_cover_the_live_text_files(self):
        text = WORKFLOW.read_text()
        block = text.split("    paths:\n", 1)[1].split("  schedule:", 1)[0]
        paths = re.findall(r'^\s+- "([^"]+)"$', block, re.M)
        self.assertGreater(len(paths), 30)
        for needed in (
            "engine/src/*_print.rs", "engine/src/*_helm_launcher.rs", "engine/src/*_bridge.rs", "engine/src/codex_exec.rs",
            "engine/src/status_slot.rs", "server/zerg/routers/runtime.py", "server/zerg/services/session_live_previews.py",
            "server/zerg/qa/terminal_truth*.py", "scripts/ci/terminal-truth-*", ".github/workflows/terminal-truth.yml",
            "schemas/terminal-truth-budget.json",
        ):
            self.assertIn(needed, paths)

    def test_the_workflow_publishes_one_stable_status_and_uses_no_secret_but_the_receipt_store(self):
        text = WORKFLOW.read_text()
        self.assertEqual(len(re.findall(r"terminal_truth_status\.py", text)), 2)  # pending, result
        self.assertNotIn("gh api", text)  # the Linux runner has no gh
        secrets = set(re.findall(r"secrets\.([A-Z0-9_]+)", text))
        self.assertTrue(all(name.startswith("CI_RECEIPTS_S3_") for name in secrets), secrets)
        self.assertNotIn("upload-artifact", text)
        self.assertNotIn("--live", text)  # never a real model

    def test_the_budget_file_names_every_declared_cell(self):
        data = json.loads(BUDGET.read_text())
        for cell, value in data["budget_s"].items():
            self.assertIn(cell.split("/")[0], PROVIDERS)
            self.assertGreater(value, 0)


class StatusTests(unittest.TestCase):
    def test_the_payload_has_the_one_context_the_promotion_requirement_reads(self):
        sys.path.insert(0, str(ROOT / "scripts" / "ci"))
        import terminal_truth_status as status

        env = {"GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "7", "GITHUB_SERVER_URL": "https://github.com"}
        body = status.payload("success", "x" * 300, env)
        self.assertEqual(body["context"], "longhouse/terminal-truth")
        self.assertEqual(body["target_url"], "https://github.com/o/r/actions/runs/7")
        self.assertEqual(len(body["description"]), 140)
        with self.assertRaises(SystemExit):
            status.payload("cancelled", "x", env)


class SummaryTests(unittest.TestCase):
    def run_summary(self, passes: dict[str, list[dict]], repeats: int, scenarios: str = "long_reply"):
        with tempfile.TemporaryDirectory() as tmp:
            for name, rows in passes.items():
                (Path(tmp) / name).write_text(json.dumps(rows))
            result = subprocess.run(
                [sys.executable, str(SUMMARY), tmp, "--repeats", str(repeats), "--scenarios", scenarios], capture_output=True, text=True
            )
            return result, json.loads((Path(tmp) / "gate.json").read_text())

    def test_all_passing_passes_succeed(self):
        rows = [row("codex/helm"), row("claude/helm", "known_bad", None, stage="engine_posted")]
        result, gate = self.run_summary({"pass-1-long_reply.json": rows, "pass-2-long_reply.json": rows}, 2)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(gate["state"], "success")

    def test_one_failing_cell_in_one_pass_fails_the_lane_and_names_the_stage(self):
        ok = [row("codex/helm")]
        bad = [row("codex/helm", "fail", None, stage="host_published")]
        result, gate = self.run_summary({"pass-1-long_reply.json": ok, "pass-2-long_reply.json": bad}, 2)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(gate["state"], "failure")
        self.assertEqual(gate["failing_cells"], ["codex/helm [long_reply]"])
        self.assertIn("host_published", result.stdout)

    def test_a_missing_pass_file_fails_closed(self):
        result, gate = self.run_summary({"pass-1-long_reply.json": [row("codex/helm")]}, 2)
        self.assertEqual(result.returncode, 1)
        self.assertIn("pass 2 (long_reply): no result file", gate["problems"])

    def test_a_cell_whose_fake_model_served_nothing_fails_closed(self):
        result, gate = self.run_summary({"pass-1-long_reply.json": [row("codex/helm", requests=0)]}, 1)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(any("served no request" in p for p in gate["problems"]))

    def test_a_pass_with_fewer_cells_than_the_first_fails_the_coverage_floor(self):
        result, gate = self.run_summary(
            {"pass-1-long_reply.json": [row("codex/helm"), row("pi/helm")], "pass-2-long_reply.json": [row("codex/helm")]}, 2
        )
        self.assertEqual(result.returncode, 1)
        self.assertTrue(any("1 rows, the first pass had 2" in p for p in gate["problems"]))


if __name__ == "__main__":
    unittest.main()
