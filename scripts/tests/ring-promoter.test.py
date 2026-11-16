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
GONE = promoter.gone  # the real probe; setUp replaces promoter.gone with the fake world's

A, B, C, X = "a" * 40, "b" * 40, "c" * 40, "e" * 40  # history A <- B <- C on main; X is off it
HISTORY = [A, B, C]


class World:
    """What the rings serve, which images are published, and what each promote script answers."""

    def __init__(self, dogfood=A, production=A, published=C):
        self.served = {promoter.DOGFOOD_HEALTH: dogfood, promoter.PRODUCTION_HEALTH: production}
        self.published = published  # the newest published image; older ones are in `older`
        self.older: list[str] = []
        self.needs: dict[str, str | None] = {}  # candidate -> the Terminal Truth commit its range needs
        self.gone: set[str] = set()  # ring health URLs that answer 410 Gone
        self.pointer: str | None = None  # the control plane's newest production promotion
        self.answers: dict[str, tuple[int, str, str]] = {}  # script key -> (rc, stdout, stderr)
        self.gates: dict | None = None  # the receipt promote-production saves
        self.calls: list[list[str]] = []
        self.by_target: dict[tuple[str, str], tuple[int, str, str]] = {}  # (script key, target) -> answer

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
        if "promote-dogfood.sh" in key:
            rc, out, err = self.by_target.get((key, argv[-1]), (rc, out, err))
        if rc == 0 and "promote-dogfood.sh" in key:
            self.served[promoter.DOGFOOD_HEALTH] = argv[-1]
        return rc, out, err, 0.1


class PromoterTests(unittest.TestCase):
    def setUp(self):
        self.world = World()
        patches = {
            "served": lambda url: self.world.served.get(url),
            "production_pointer": lambda: self.world.pointer,
            "gone": lambda url: url in self.world.gone,
            "published_candidates": lambda main: [self.world.published, *self.world.older],
            "required_terminal_truth_commit": lambda base, head: self.world.needs.get(head),
            "is_ancestor": self.world.is_ancestor,
            "run_script": self.world.run_script,
        }
        for name, fn in patches.items():
            original = getattr(promoter, name)
            setattr(promoter, name, fn)
            self.addCleanup(setattr, promoter, name, original)

    def test_a_range_without_sensitive_paths_takes_the_fast_lane(self):
        d = promoter.decide_dogfood(C, dry_run=False)
        self.assertEqual((d["action"], d["lane"], d["target"]), ("promoted", "fast-lane", C))
        self.assertEqual(self.world.calls, [["scripts/ops/promote-dogfood.sh", "--fast-lane", C]])

    def test_a_sensitive_range_takes_the_canary_qualified_path_and_waits_for_canary(self):
        self.world.answers["scripts/ops/promote-dogfood.sh --fast-lane"] = (3, "", "touches sensitive paths")
        self.world.answers["scripts/ops/promote-dogfood.sh"] = (
            1, "", "no successful Deploy and Verify run has a usable canary verification receipt"
        )
        d = promoter.decide_dogfood(C, dry_run=False)
        self.assertEqual((d["action"], d["lane"], d["waiting_on"]), ("waiting", "canary-qualified", ["canary"]))
        self.world.answers["scripts/ops/promote-dogfood.sh"] = (0, "Promoted", "")
        d = promoter.decide_dogfood(C, dry_run=False)
        self.assertEqual((d["action"], d["lane"]), ("promoted", "canary-qualified"))

    def test_a_live_preview_change_without_a_terminal_truth_status_waits_for_it(self):
        self.world.answers["scripts/ops/promote-dogfood.sh --fast-lane"] = (3, "", "touches sensitive paths")
        self.world.answers["scripts/ops/promote-dogfood.sh"] = (4, "", "has no terminal-truth status yet")
        d = promoter.decide_dogfood(C, dry_run=False)
        self.assertEqual((d["action"], d["lane"], d["waiting_on"]), ("waiting", "canary-qualified", ["terminal-truth"]))
        self.assertEqual(promoter.describe(d), "waiting (canary-qualified) on terminal-truth")
        self.assertEqual(promoter.STATE[d["action"]], "pending")

    def test_a_failed_terminal_truth_status_is_a_refusal_that_is_not_a_failed_promotion(self):
        self.world.answers["scripts/ops/promote-dogfood.sh --fast-lane"] = (3, "", "touches sensitive paths")
        self.world.answers["scripts/ops/promote-dogfood.sh"] = (5, "", "the terminal-truth status on aaaa is failure")
        d = promoter.decide_dogfood(C, dry_run=False)
        self.assertEqual((d["action"], d["lane"]), ("refused", "canary-qualified"))
        self.assertIn("is failure", d["reason"])
        self.assertEqual(promoter.STATE[d["action"]], "failure")

    def test_an_older_green_commit_is_promoted_while_the_newest_waits_for_terminal_truth(self):
        # 2026-11-15: 7861a9eb6 (B) was green, 81099b157 (C) had no verdict; dogfood sat on A for hours
        self.world.older = [B]
        self.world.needs = {C: "c1", B: "b1"}
        self.world.by_target[("scripts/ops/promote-dogfood.sh --fast-lane", C)] = (4, "", "no terminal-truth status yet")
        d = promoter.decide_dogfood(C, dry_run=False)
        self.assertEqual((d["action"], d["target"], d["lane"]), ("promoted", B, "fast-lane"))
        self.assertIn("not ready", d["reason"])
        self.assertEqual(self.world.served[promoter.DOGFOOD_HEALTH], B)

    def test_a_red_terminal_truth_commit_is_never_promoted_and_an_older_green_one_is(self):
        self.world.older = [B]
        self.world.needs = {C: "c1", B: "b1"}
        self.world.by_target[("scripts/ops/promote-dogfood.sh --fast-lane", C)] = (5, "", "status is failure")
        d = promoter.decide_dogfood(C, dry_run=False)
        self.assertEqual((d["action"], d["target"]), ("promoted", B))
        self.assertNotIn(["scripts/ops/promote-dogfood.sh", C], self.world.calls)

    def test_candidates_that_share_the_waiting_terminal_truth_commit_are_not_tried_again(self):
        self.world.older = [B]
        self.world.needs = {C: "c1", B: "c1"}
        self.world.answers["scripts/ops/promote-dogfood.sh --fast-lane"] = (4, "", "pending")
        d = promoter.decide_dogfood(C, dry_run=False)
        self.assertEqual((d["action"], d["target"], d["waiting_on"]), ("waiting", C, ["terminal-truth"]))
        self.assertEqual(len(self.world.calls), 1)

    def test_when_every_candidate_waits_the_newest_ones_outcome_is_reported(self):
        self.world.older = [B]
        self.world.needs = {C: "c1", B: "b1"}
        self.world.answers["scripts/ops/promote-dogfood.sh --fast-lane"] = (4, "", "pending")
        d = promoter.decide_dogfood(C, dry_run=False)
        self.assertEqual((d["action"], d["target"]), ("waiting", C))
        self.assertEqual(self.world.served[promoter.DOGFOOD_HEALTH], A)

    def test_a_failed_deploy_of_the_newest_does_not_fall_back_to_an_older_commit(self):
        self.world.older = [B]
        self.world.answers["scripts/ops/promote-dogfood.sh --fast-lane"] = (1, "", "deployment failed")
        d = promoter.decide_dogfood(C, dry_run=False)
        self.assertEqual((d["action"], d["target"]), ("failed", C))
        self.assertEqual(len(self.world.calls), 1)

    def test_a_fast_lane_terminal_truth_wait_is_a_wait_not_a_failure(self):
        self.world.answers["scripts/ops/promote-dogfood.sh --fast-lane"] = (4, "", "pending")
        self.assertEqual(promoter.decide_dogfood(C, dry_run=False)["action"], "waiting")

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

    def test_gone_reads_404_and_410_as_no_surface_and_other_failures_as_unreadable(self):
        import urllib.error

        def answering(code):
            def urlopen(request, timeout=None):
                raise urllib.error.HTTPError(request.full_url, code, "x", None, None)
            return urlopen

        original = promoter.urllib.request.urlopen
        self.addCleanup(setattr, promoter.urllib.request, "urlopen", original)
        for code, expected in ((404, True), (410, True), (500, False), (502, False), (403, False)):
            promoter.urllib.request.urlopen = answering(code)
            self.assertIs(GONE(promoter.PRODUCTION_HEALTH), expected, code)

        def unreachable(request, timeout=None):
            raise OSError("no route")

        promoter.urllib.request.urlopen = unreachable
        self.assertIs(GONE(promoter.PRODUCTION_HEALTH), False)

    def test_a_production_that_answers_410_is_paused_not_unreadable(self):
        self.world.served[promoter.DOGFOOD_HEALTH] = C
        self.world.served[promoter.PRODUCTION_HEALTH] = None
        self.world.gone.add(promoter.PRODUCTION_HEALTH)
        d = promoter.decide_production(dry_run=False)
        self.assertEqual(d["action"], "paused")
        self.assertEqual(promoter.STATE["paused"], "pending")
        self.assertIn("404 or 410", promoter.describe(d))
        self.assertEqual(self.world.calls, [])
        self.world.gone.clear()
        self.assertEqual(promoter.decide_production(dry_run=False)["action"], "waiting")

    def test_the_control_plane_pointer_is_production_even_when_the_demo_url_is_gone(self):
        # The retired demo answers 404; production is the tenants and the new-tenant image the control plane records.
        self.world.served[promoter.PRODUCTION_HEALTH] = None
        self.world.gone.add(promoter.PRODUCTION_HEALTH)
        self.world.pointer = A
        self.world.served[promoter.DOGFOOD_HEALTH] = C
        self.world.gates = {"gates": {}}
        d = promoter.decide_production(dry_run=False)
        self.assertEqual(d["action"], "promoted")
        self.assertEqual(d["served"], A)
        self.world.pointer = C
        self.assertEqual(promoter.decide_production(dry_run=False)["action"], "up_to_date")

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
        self.assertEqual(names, {"Publish Runtime Image", "Archive Runtime Image", "Deploy and Verify", "CI", "Terminal Truth"})
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
