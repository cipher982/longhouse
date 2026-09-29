#!/usr/bin/env python3
"""promote-production.sh moves nothing unless every gate passes, and says where it stopped.

The real script and the real gates run against a fixture control plane and health
endpoint served over HTTP, with `gh` and `ssh` stubbed on PATH (see
scripts/tests/promotion_world.py). The gates themselves are covered one by one in
promotion-gates.test.py; this file covers the orchestration: the refusal moves
nothing, --check prints the receipt, the sequence and its recovery, and that no
tag or GitHub release is involved.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import promotion_world as w  # noqa: E402

ROOT = w.ROOT

LIBRARY_STUB = """\
lh_hosted_prepare_control_plane_auth() {
  CONTROL_PLANE_URL="$FIXTURE_CONTROL_PLANE_URL"
  CONTROL_PLANE_ADMIN_TOKEN="fixture-token"
  export CONTROL_PLANE_URL CONTROL_PLANE_ADMIN_TOKEN
}
lh_hosted_reprovision_production() {
  printf '%s\\t%s\\t%s\\t%s\\n' "$1" "$2" "$LH_DEPLOYMENT_IDEMPOTENCY_KEY" "$LH_DEPLOYMENT_REASON" >> "$FIXTURE_ROOT/promotions"
  if [[ "${FIXTURE_SUBMIT_REFUSED:-0}" != "1" ]]; then
    LH_DEPLOYMENT_ID="d-production-fixture"
    export LH_DEPLOYMENT_ID
  fi
  [[ "${FIXTURE_REPROVISION_FAIL:-0}" != "1" && "${FIXTURE_SUBMIT_REFUSED:-0}" != "1" ]]
}
"""


# The review gate has its own tests (review-gate.test.py); here it only has to be asked, with the
# target and the served-health URL, before anything changes.
REVIEW_GATE_STUB = """\
lh_review_gate_promotion() {
  printf '%s %s\\n' "$1" "$2" >> "$FIXTURE_ROOT/gate_calls"
  [[ "${FIXTURE_GATE_REFUSES:-0}" != "1" ]] || { echo "review-gate: REFUSED" >&2; return 1; }
  # The real gate reports success on stdout; the receipt on stdout must stay JSON.
  echo "review-gate: promotion OK."
}
"""


class PromoteProductionTests(unittest.TestCase):
    def run_promotion(self, world: dict | None = None, *args: str, env: dict | None = None):
        world = world if world is not None else w.green_world()
        with tempfile.TemporaryDirectory(prefix="longhouse-promote-production-test-") as directory:
            root = Path(directory)
            ops, library = root / "scripts" / "ops", root / "scripts" / "lib"
            ops.mkdir(parents=True)
            library.mkdir(parents=True)
            for name in ("promote-production.sh", "promotion_gates.py"):
                shutil.copyfile(ROOT / "scripts" / "ops" / name, ops / name)
            (ops / "promote-production.sh").chmod(0o755)
            (library / "hosted-instance.sh").write_text(LIBRARY_STUB)
            (library / "review-gate.sh").write_text(REVIEW_GATE_STUB)
            with w.Wire(world, root) as wire:
                result = subprocess.run(
                    ["bash", str(ops / "promote-production.sh"), *args],
                    env={**wire.env(), **(env or {})},
                    text=True,
                    capture_output=True,
                    timeout=60,
                )
            promotions_path, ssh_path = root / "promotions", root / "ssh_invocations"
            promotions = [line.split("\t") for line in promotions_path.read_text().splitlines()] if promotions_path.exists() else []
            ssh_calls = ssh_path.read_text().splitlines() if ssh_path.exists() else []
            receipts = sorted((root / "receipts").glob("*.json")) if (root / "receipts").exists() else []
            saved = [json.loads(path.read_text()) for path in receipts]
            gate_calls = root / "gate_calls"
            self.gate_calls = gate_calls.read_text().splitlines() if gate_calls.exists() else []
            return result, promotions, ssh_calls, saved

    def test_the_review_gate_is_asked_for_the_target_against_what_production_serves(self) -> None:
        result, promotions, _ssh, _saved = self.run_promotion(w.green_world(), w.SHA)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(promotions), 1)
        self.assertEqual(len(self.gate_calls), 1)
        target, served_url = self.gate_calls[0].split()
        self.assertEqual(target, w.SHA)
        self.assertTrue(served_url.endswith("/demo/api/health"), served_url)

    def test_an_unreviewed_range_moves_nothing_and_still_keeps_the_receipt(self) -> None:
        for args in ((w.SHA,), ("--check", w.SHA)):
            result, promotions, ssh_calls, saved = self.run_promotion(w.green_world(), *args, env={"FIXTURE_GATE_REFUSES": "1"})
            self.assertNotEqual(result.returncode, 0, args)
            self.assertEqual((promotions, ssh_calls), ([], []))
            self.assertIn("review-gate: REFUSED", result.stderr)
            self.assertIn("Nothing was changed", result.stderr)
            self.assertEqual(len(saved), 1)

    def test_happy_path_promotes_the_dogfood_digest_and_prints_the_receipt(self) -> None:
        world = w.green_world()
        world["instances"].append({"id": 11, "subdomain": "acme", "status": "active"})
        result, promotions, ssh_calls, saved = self.run_promotion(world, w.SHA)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(promotions), 1)
        image, targets, key, reason = promotions[0]
        self.assertEqual(image, w.DIGEST)
        self.assertEqual(json.loads(targets), [11])
        self.assertEqual(key, f"promote-production-{w.SHA}")
        self.assertIn(w.SHA, reason)
        self.assertEqual(len(ssh_calls), 1)
        self.assertIn(w.DIGEST, ssh_calls[0])

        receipt = json.loads(result.stdout)
        self.assertTrue(receipt["promotable"])
        self.assertEqual(receipt["sha"], w.SHA)
        self.assertEqual(receipt["image_digest"], w.DIGEST)
        self.assertTrue(all(gate["ok"] for gate in receipt["gates"].values()))
        self.assertEqual(receipt["publish_run"], {"id": 555, "number": 44, "attempt": 1})
        self.assertEqual(receipt["promotion"]["deployment_id"], "d-production-fixture")
        self.assertEqual(receipt["promotion"]["targets"], 1)
        self.assertTrue(receipt["promotion"]["demo_verified"])
        self.assertEqual(saved, [receipt])
        self.assertIn("demo_verified=true", result.stderr)

    def test_no_tag_and_no_github_release_is_needed(self) -> None:
        # The gh stub aborts on anything but `run list` and `api`; a `release view` or
        # `git ls-remote` for a tag would fail the promotion.
        result, promotions, _ssh, _saved = self.run_promotion()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(promotions), 1)
        script = (ROOT / "scripts" / "ops" / "promote-production.sh").read_text()
        for needle in ("ls-remote", "release view", "VERSION", "SOAK_HOURS"):
            self.assertNotIn(needle, script.replace("PROMOTION_ATTEMPT", ""), needle)

    def test_pointer_only_when_no_tenant_is_active(self) -> None:
        result, promotions, ssh_calls, _saved = self.run_promotion()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(promotions[0][1]), [])
        self.assertEqual(len(ssh_calls), 1)
        self.assertEqual(json.loads(result.stdout)["promotion"]["targets"], 0)
        self.assertIn("pointer-only", result.stderr)

    def test_the_sha_defaults_to_what_dogfood_serves(self) -> None:
        result, promotions, _ssh, _saved = self.run_promotion()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["sha"], w.SHA)
        self.assertEqual(promotions[0][0], w.DIGEST)

    def test_check_prints_the_receipt_and_moves_nothing(self) -> None:
        result, promotions, ssh_calls, _saved = self.run_promotion(None, "--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(promotions, [])
        self.assertEqual(ssh_calls, [])
        receipt = json.loads(result.stdout)
        self.assertTrue(receipt["promotable"])
        self.assertNotIn("promotion", receipt)
        self.assertIn("nothing was changed", result.stderr)

    def test_check_of_a_refused_promotion_still_prints_every_gate_and_exits_nonzero(self) -> None:
        world = w.green_world()
        world["artifacts"] = []
        result, promotions, ssh_calls, _saved = self.run_promotion(world, "--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((promotions, ssh_calls), ([], []))
        receipt = json.loads(result.stdout)
        self.assertFalse(receipt["promotable"])
        self.assertTrue(receipt["gates"]["dogfood"]["ok"])
        self.assertFalse(receipt["gates"]["hosted_qa"]["ok"])
        self.assertFalse(receipt["gates"]["engine_compat"]["ok"])

    def assertRefusedUntouched(self, world: dict, gate: str, *args: str, env: dict | None = None) -> str:
        result, promotions, ssh_calls, saved = self.run_promotion(world, *args, env=env)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(promotions, [], "production was submitted despite a refusal")
        self.assertEqual(ssh_calls, [], "the demo was touched despite a refusal")
        self.assertIn(f"REFUSED gate {gate}:", result.stderr)
        self.assertIn("Nothing was changed", result.stderr)
        self.assertFalse(json.loads(result.stdout)["promotable"])
        self.assertEqual(len(saved), 1, "a refusal keeps its receipt too")
        return result.stderr

    def test_dogfood_not_serving_the_commit_is_refused(self) -> None:
        world = w.green_world()
        world["dogfood_health"]["build"]["commit"] = w.OTHER_SHA
        self.assertRefusedUntouched(world, "dogfood", w.SHA)

    def test_superseded_qa_is_refused(self) -> None:
        world = w.green_world()
        world["artifacts"][0]["receipt"] = w.qa_receipt(verdict="superseded")
        self.assertRefusedUntouched(world, "hosted_qa")

    def test_a_missing_engine_compat_receipt_is_refused(self) -> None:
        world = w.green_world()
        world["artifacts"] = world["artifacts"][:1]
        self.assertRefusedUntouched(world, "engine_compat")

    def test_an_unmet_soak_is_refused(self) -> None:
        world = w.green_world()
        w.add_real_tenant(world, satisfied=False, earliest="2026-09-30T22:00:00")
        stderr = self.assertRefusedUntouched(world, "soak")
        self.assertIn("24h soak is required", stderr)

    def test_unreadable_tenant_status_is_refused(self) -> None:
        # A control plane that does not serve the soak answer (an old build) reads as unknown.
        world = w.green_world()
        world["soak"] = None
        stderr = self.assertRefusedUntouched(world, "soak")
        self.assertIn("unknown is never read as pre-launch", stderr)

    def test_a_stale_soak_hours_variable_cannot_switch_the_soak_off(self) -> None:
        world = w.green_world()
        w.add_real_tenant(world, satisfied=False)
        self.assertRefusedUntouched(world, "soak", env={"SOAK_HOURS": "0"})

    def test_every_refused_gate_is_reported_in_one_run(self) -> None:
        world = w.green_world()
        world["artifacts"] = []
        world["soak"] = None
        result, promotions, ssh_calls, _saved = self.run_promotion(world)
        self.assertNotEqual(result.returncode, 0)
        for gate in ("hosted_qa", "engine_compat", "soak"):
            self.assertIn(f"REFUSED gate {gate}:", result.stderr)
        self.assertEqual((promotions, ssh_calls), ([], []))

    def test_a_short_sha_and_unknown_options_are_usage_errors(self) -> None:
        for args in (("abc123",), ("--force",), (w.SHA, w.SHA)):
            result, promotions, ssh_calls, _saved = self.run_promotion(None, *args)
            self.assertEqual(result.returncode, 2, args)
            self.assertEqual((promotions, ssh_calls), ([], []))

    def test_a_missing_publish_run_is_refused_before_anything_moves_and_by_check(self) -> None:
        for args in ((), ("--check",)):
            world = w.green_world()
            world["publish_runs"] = []
            result, promotions, ssh_calls, saved = self.run_promotion(world, *args)
            self.assertNotEqual(result.returncode, 0, args)
            self.assertIn("no successful Publish Runtime Image run", result.stderr)
            self.assertEqual(len(saved), 1, "this refusal keeps its receipt too")
            self.assertEqual((promotions, ssh_calls), ([], []))

    def test_a_halted_wave_leaves_the_demo_alone_and_says_how_to_recover(self) -> None:
        world = w.green_world()
        world["instances"].append({"id": 11, "subdomain": "acme", "status": "active"})
        result, promotions, ssh_calls, _saved = self.run_promotion(world, env={"FIXTURE_REPROVISION_FAIL": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(promotions), 1)
        self.assertEqual(ssh_calls, [], "the demo must not be touched after a failed wave")
        self.assertIn("stopped before the public demo was touched", result.stderr)
        self.assertIn("/rollback", result.stderr)
        self.assertIn("PROMOTION_ATTEMPT=2 make promote-production", result.stderr)

    def test_a_promotion_the_control_plane_refuses_says_nothing_was_recorded(self) -> None:
        # For example its own soak check answering 409: no deployment exists to roll back.
        result, promotions, ssh_calls, _saved = self.run_promotion(env={"FIXTURE_SUBMIT_REFUSED": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(ssh_calls, [])
        self.assertIn("did not record the promotion", result.stderr)
        self.assertNotIn("/rollback", result.stderr)

    def test_a_new_attempt_uses_a_new_idempotency_key(self) -> None:
        result, promotions, _ssh, _saved = self.run_promotion(env={"PROMOTION_ATTEMPT": "2"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(promotions[0][2], f"promote-production-{w.SHA}-attempt-2")

    def test_a_demo_that_cannot_be_pinned_says_only_the_demo_is_behind(self) -> None:
        result, promotions, ssh_calls, _saved = self.run_promotion(env={"FIXTURE_SSH_FAIL": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(promotions), 1)
        self.assertEqual(len(ssh_calls), 1)
        self.assertIn("only the\npublic demo is not verified", result.stderr)
        self.assertIn(f"make promote-production SHA={w.SHA}", result.stderr)

    def test_a_demo_that_never_reports_the_commit_says_only_the_demo_is_behind(self) -> None:
        world = w.green_world()
        world["demo_health"]["build"]["commit"] = w.OTHER_SHA
        result, promotions, ssh_calls, _saved = self.run_promotion(world)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((len(promotions), len(ssh_calls)), (1, 1))
        self.assertIn("Timed out waiting for public demo", result.stderr)
        self.assertIn("Nothing to roll back", result.stderr)

    def test_a_non_dogfood_subdomain_is_refused(self) -> None:
        result, promotions, ssh_calls, _saved = self.run_promotion(env={"SUBDOMAIN": "demo"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((promotions, ssh_calls), ([], []))


if __name__ == "__main__":
    unittest.main(verbosity=1)
