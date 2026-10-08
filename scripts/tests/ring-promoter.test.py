#!/usr/bin/env python3
"""The ring promoter's decisions: which lane dogfood takes, when production follows, never backwards."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("ring_promoter", ROOT / "scripts" / "ops" / "ring_promoter.py")
promoter = importlib.util.module_from_spec(spec)
sys.modules["ring_promoter"] = promoter
spec.loader.exec_module(promoter)

A, B, C, X = "a" * 40, "b" * 40, "c" * 40, "e" * 40  # history A <- B <- C on main; X is off it
HISTORY = [A, B, C]


class World:
    """What the rings serve, which images are published, and what each promote script answers."""

    def __init__(self, dogfood=A, production=A, published=C):
        self.served = {promoter.DOGFOOD_HEALTH: dogfood, promoter.PRODUCTION_HEALTH: production}
        self.published = published
        self.answers: dict[str, tuple[int, str, str]] = {}  # script key -> (rc, stdout, stderr)
        self.gates: dict | None = None  # the receipt promote-production saves
        self.calls: list[list[str]] = []

    def is_ancestor(self, older, newer):
        if older not in HISTORY or newer not in HISTORY:
            return older == newer
        return HISTORY.index(older) <= HISTORY.index(newer)

    def run_script(self, argv, env=None):
        self.calls.append(argv[1:])
        key = " ".join(a for a in argv[1:] if not a.startswith(("a" * 8, "b" * 8, "c" * 8)))
        rc, out, err = self.answers.get(key, (0, "", ""))
        if "promote-production.sh" in key and self.gates is not None:
            Path(env["PROMOTION_RECEIPT_DIR"], "production-x.json").write_text(json.dumps(self.gates))
        if rc == 0 and "promote-dogfood.sh" in key:
            self.served[promoter.DOGFOOD_HEALTH] = argv[-1]
        return rc, out, err, 0.1


class PromoterTests(unittest.TestCase):
    def setUp(self):
        self.world = World()
        patches = {
            "served": lambda url: self.world.served.get(url),
            "newest_published": lambda main: self.world.published,
            "is_ancestor": self.world.is_ancestor,
            "run_script": self.world.run_script,
        }
        for name, fn in patches.items():
            original = getattr(promoter, name)
            setattr(promoter, name, fn)
            self.addCleanup(setattr, promoter, name, original)

    def test_a_range_off_the_blocking_list_takes_the_fast_lane(self):
        d = promoter.decide_dogfood(C, dry_run=False)
        self.assertEqual((d["action"], d["lane"], d["target"]), ("promoted", "fast-lane", C))
        self.assertEqual(self.world.calls, [["scripts/ops/promote-dogfood.sh", "--fast-lane", C]])

    def test_a_blocking_range_takes_the_canary_qualified_path_and_waits_for_review(self):
        self.world.answers["scripts/ops/promote-dogfood.sh --fast-lane"] = (3, "", "touches the review blocking list")
        self.world.answers["scripts/ops/promote-dogfood.sh"] = (1, "", "review-gate: REFUSED promotion: no review attestation")
        d = promoter.decide_dogfood(C, dry_run=False)
        self.assertEqual((d["action"], d["lane"], d["waiting_on"]), ("waiting", "canary-qualified", ["review"]))
        self.world.answers["scripts/ops/promote-dogfood.sh"] = (0, "Promoted", "")
        d = promoter.decide_dogfood(C, dry_run=False)
        self.assertEqual((d["action"], d["lane"]), ("promoted", "canary-qualified"))

    def test_a_fast_lane_deploy_that_fails_is_a_failure_not_a_wait(self):
        self.world.answers["scripts/ops/promote-dogfood.sh --fast-lane"] = (1, "", "deployment failed")
        self.assertEqual(promoter.decide_dogfood(C, dry_run=False)["action"], "failed")

    def test_dogfood_never_moves_backwards_or_sideways(self):
        self.world.served[promoter.DOGFOOD_HEALTH] = C
        self.world.published = B
        self.assertEqual(promoter.decide_dogfood(C, dry_run=False)["action"], "up_to_date")
        self.world.served[promoter.DOGFOOD_HEALTH] = X
        self.assertEqual(promoter.decide_dogfood(C, dry_run=False)["action"], "refused")
        self.assertEqual(self.world.calls, [])

    def test_production_follows_dogfood_when_the_contract_passes(self):
        self.world.served[promoter.DOGFOOD_HEALTH] = C
        self.world.gates = {"promotion": {"deployment_id": "d-1"}, "gates": {}}
        d = promoter.decide_production(dry_run=False)
        self.assertEqual((d["action"], d["target"], d["deployment"]), ("promoted", C, "d-1"))
        self.assertEqual(self.world.calls, [["scripts/ops/promote-production.sh", C]])

    def test_production_records_the_gates_it_waits_on(self):
        self.world.served[promoter.DOGFOOD_HEALTH] = C
        self.world.answers["scripts/ops/promote-production.sh"] = (1, "", "Refusing to promote")
        self.world.gates = {"gates": {"dogfood": {"ok": True}, "hosted_qa": {"ok": False, "refusal": "no verdict"},
                                      "engine_compat": {"ok": False, "refusal": "no receipt"}}}
        d = promoter.decide_production(dry_run=False)
        self.assertEqual((d["action"], d["waiting_on"]), ("waiting", ["engine_compat", "hosted_qa"]))
        self.assertIn("waiting on engine_compat, hosted_qa", promoter.describe(d))

    def test_production_never_moves_backwards(self):
        self.world.served[promoter.PRODUCTION_HEALTH] = C
        self.world.served[promoter.DOGFOOD_HEALTH] = B
        self.assertEqual(promoter.decide_production(dry_run=False)["action"], "refused")
        self.world.served[promoter.DOGFOOD_HEALTH] = C
        self.assertEqual(promoter.decide_production(dry_run=False)["action"], "up_to_date")
        self.assertEqual(self.world.calls, [])

    def test_a_run_is_red_only_when_a_promotion_failed(self):
        record = []
        for name, fn in {"post_status": lambda d, url: record.append(d["ring"]), "git": lambda *a, check=True: C}.items():
            self.addCleanup(setattr, promoter, name, getattr(promoter, name))
            setattr(promoter, name, fn)
        self.world.answers["scripts/ops/promote-production.sh"] = (1, "", "Refusing")
        self.world.gates = {"gates": {"hosted_qa": {"ok": False, "refusal": "no verdict"}}}
        os.environ.pop("GITHUB_STEP_SUMMARY", None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(promoter.main([]), 0)  # dogfood promoted, production waiting
        self.assertEqual(record, ["dogfood", "production"])
        self.world.served[promoter.DOGFOOD_HEALTH] = A
        self.world.answers["scripts/ops/promote-dogfood.sh --fast-lane"] = (1, "", "deployment failed")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(promoter.main([]), 1)


class WakeWiringTests(unittest.TestCase):
    """Every gate run wakes the promoter. A trigger GitHub never fires reads as wired and is not.

    On 2026-10-08 Hosted Live QA passed on 605347929 at 15:39Z and production sat "waiting on hosted_qa"
    until a hand dispatch at 17:30Z: QA is dispatched with GITHUB_TOKEN, so its completion never fires
    `workflow_run` (0 of 23), and the `schedule` catch-all ran 2 times in 22 h.
    """

    WORKFLOWS = ROOT / ".github" / "workflows"

    def test_workflow_run_lists_only_runs_whose_completion_fires_it(self) -> None:
        text = (self.WORKFLOWS / "promote-rings.yml").read_text(encoding="utf-8")
        listed = re.search(r"^    workflows: \[(.*)\]$", text, re.MULTILINE)
        self.assertIsNotNone(listed)
        names = {n.strip().strip('"') for n in listed.group(1).split(",")}
        self.assertEqual(names, {"Publish Runtime Image", "Archive Runtime Image", "Deploy and Verify", "CI"})
        self.assertNotRegex(text, r"(?m)^  schedule:", "the catch-all is Sauron longhouse-ring-wake")

    def test_hosted_live_qa_wakes_the_promoter_and_the_promoter_waits_for_it(self) -> None:
        qa = (self.WORKFLOWS / "hosted-live-qa.yml").read_text(encoding="utf-8")
        self.assertIn("\n  wake-promoter:\n", qa)
        wake = qa[qa.index("\n  wake-promoter:\n"):]
        self.assertIn("needs: hosted-live-qa", wake, "the wake must follow the verdict upload")
        self.assertIn("promote-rings.yml/dispatches", wake)
        self.assertIn("after_run:$run", wake)
        self.assertIn("continue-on-error: true", wake, "a failed wake must not fail QA's own qualification")
        self.assertIn("actions: write", wake)
        self.assertNotRegex(qa[:qa.index("\njobs:")], r"actions: write", "only the wake job may dispatch workflows")
        rings = (self.WORKFLOWS / "promote-rings.yml").read_text(encoding="utf-8")
        self.assertRegex(rings, r"(?m)^      after_run:$")
        self.assertRegex(rings, r"(?m)^    needs: wake$")


if __name__ == "__main__":
    unittest.main()
