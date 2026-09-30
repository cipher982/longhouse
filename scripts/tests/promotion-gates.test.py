#!/usr/bin/env python3
"""Each gate of a production promotion, refused and passed on fixtures.

The green world (scripts/tests/promotion_world.py) is what every source says when
a promotion is allowed; each test breaks exactly one thing. The properties that
matter: a gate never passes on absence or on a green-looking summary, and every
gate runs so one check lists everything that is missing.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import promotion_world as w  # noqa: E402

gates = w.gates
CFG = gates.Config(repo=w.REPO, dogfood_subdomain=w.DOGFOOD, dogfood_health_url="https://dogfood.test/api/health")


def evaluate(world: dict, sha: str | None = w.SHA) -> dict:
    return gates.evaluate(CFG, w.InProcessSources(world), sha)


def refused(receipt: dict) -> dict[str, str]:
    return {name: gate["refusal"] for name, gate in receipt["gates"].items() if not gate["ok"]}


class GreenTests(unittest.TestCase):
    def test_every_gate_passes_and_the_receipt_names_the_evidence(self) -> None:
        receipt = evaluate(w.green_world())
        self.assertTrue(receipt["promotable"], refused(receipt))
        self.assertEqual(receipt["sha"], w.SHA)
        self.assertEqual(receipt["image_digest"], w.DIGEST)
        self.assertEqual(set(receipt["gates"]), {"dogfood", "hosted_qa", "engine_compat", "soak"})
        self.assertEqual(receipt["gates"]["dogfood"]["evidence"]["deployment_id"], "d-dogfood")
        self.assertEqual(receipt["gates"]["hosted_qa"]["evidence"]["run_id"], w.QA_RUN)
        self.assertEqual(receipt["gates"]["engine_compat"]["evidence"]["previous_release_tag"], "v0.1.59")
        self.assertEqual(receipt["gates"]["soak"]["evidence"]["required_hours"], 0)
        # A promotable receipt is machine-readable JSON as it stands.
        self.assertEqual(json.loads(json.dumps(receipt)), receipt)

    def test_the_sha_defaults_to_what_dogfood_serves(self) -> None:
        receipt = evaluate(w.green_world(), None)
        self.assertTrue(receipt["promotable"], refused(receipt))
        self.assertEqual(receipt["sha"], w.SHA)

    def test_targets_are_active_tenants_except_dogfood(self) -> None:
        world = w.green_world()
        world["instances"] += [
            {"id": 5, "subdomain": "acme", "status": "active"},
            {"id": 6, "subdomain": "booting", "status": "provisioning"},
        ]
        plan = evaluate(world)["plan"]
        self.assertEqual(plan["targets"], [{"id": 5, "subdomain": "acme"}])
        self.assertFalse(plan["pointer_only"])
        self.assertTrue(evaluate(w.green_world())["plan"]["pointer_only"])


class DogfoodGateTests(unittest.TestCase):
    def test_dogfood_serving_another_commit_is_refused_and_says_how_to_fix_it(self) -> None:
        world = w.green_world()
        world["dogfood_health"]["build"]["commit"] = w.OTHER_SHA
        receipt = evaluate(world)
        self.assertFalse(receipt["promotable"])
        self.assertIn(f"make promote-dogfood SHA={w.SHA}", refused(receipt)["dogfood"])

    def test_healthy_is_not_the_same_as_serving_the_commit(self) -> None:
        # The gap the review found: a healthy dogfood on some other build used to pass.
        world = w.green_world()
        world["dogfood_health"]["build"]["commit"] = w.OTHER_SHA
        world["deployments"][0]["source_sha"] = w.OTHER_SHA
        self.assertIn("serves", refused(evaluate(world))["dogfood"])

    def test_unhealthy_unreachable_and_dirty_are_refused(self) -> None:
        for mutate, expected in (
            (lambda world: world["dogfood_health"].update(status="degraded"), "not healthy"),
            (lambda world: world.update(dogfood_health=None), "unreadable"),
            (lambda world: world["dogfood_health"]["build"].update(dirty=True), "dirty"),
            (lambda world: world["dogfood_health"]["build"].pop("dirty"), "dirty"),
        ):
            world = w.green_world()
            mutate(world)
            self.assertIn(expected, refused(evaluate(world))["dogfood"], expected)

    def test_a_commit_that_was_never_promoted_to_dogfood_is_refused(self) -> None:
        world = w.green_world()
        world["deployments"] = []
        self.assertIn("no successful dogfood deployment", refused(evaluate(world))["dogfood"])

    def test_a_failed_or_digestless_dogfood_deployment_is_not_one(self) -> None:
        for change in ({"status": "failure"}, {"status": "paused"}, {"image_digest": "ghcr.io/x/y:latest"}, {"completed_at": None}):
            world = w.green_world()
            world["deployments"][0].update(change)
            self.assertIn("no successful dogfood deployment", refused(evaluate(world))["dogfood"], change)

    def test_dogfood_running_a_different_digest_now_is_refused(self) -> None:
        # The deployment once succeeded, but a later one moved dogfood on.
        world = w.green_world()
        world["deployment_detail"]["d-dogfood"]["target_evidence"][0]["instance_current_image"] = w.OTHER_DIGEST
        message = refused(evaluate(world))["dogfood"]
        self.assertIn(w.OTHER_DIGEST, message)
        self.assertIn(w.DIGEST, message)

    def test_dogfood_still_applying_is_refused(self) -> None:
        world = w.green_world()
        world["deployment_detail"]["d-dogfood"]["target_evidence"][0]["instance_desired_generation"] = 10
        self.assertIn("not finished applying", refused(evaluate(world))["dogfood"])

    def test_missing_target_evidence_is_refused(self) -> None:
        world = w.green_world()
        world["deployment_detail"]["d-dogfood"]["target_evidence"] = []
        self.assertIn("no target evidence", refused(evaluate(world))["dogfood"])
        world = w.green_world()
        world["deployment_detail"]["d-dogfood"]["target_evidence"][0]["disposition"] = "failure"
        self.assertIn("ended 'failure'", refused(evaluate(world))["dogfood"])

    def test_the_newest_of_several_dogfood_deployments_names_the_digest(self) -> None:
        world = w.green_world()
        world["deployments"].append({**world["deployments"][0], "id": "d-older", "image_digest": w.OTHER_DIGEST, "completed_at": "2026-09-28T01:00:00"})
        receipt = evaluate(world)
        self.assertTrue(receipt["promotable"], refused(receipt))
        self.assertEqual(receipt["image_digest"], w.DIGEST)

    def test_the_gates_that_need_a_digest_say_so_when_dogfood_refuses(self) -> None:
        world = w.green_world()
        world["deployments"] = []
        self.assertIn("not evaluated", refused(evaluate(world))["soak"])


class HostedQaGateTests(unittest.TestCase):
    name = f"hosted-live-qa-verdict-{w.SHA}"

    def test_no_receipt_means_no_evidence_and_says_how_to_get_one(self) -> None:
        world = w.green_world()
        world["artifacts"] = [a for a in world["artifacts"] if a["name"] != self.name]
        message = refused(evaluate(world))["hosted_qa"]
        self.assertIn("no Hosted Live QA verdict receipt", message)
        self.assertIn(f"gh workflow run hosted-live-qa.yml --ref main -f source_sha={w.SHA}", message)

    def test_a_superseded_run_is_never_qa_evidence(self) -> None:
        # It exits 0 and the run concludes success, which is why the verdict is read, not the conclusion.
        world = w.green_world()
        world["artifacts"][0]["receipt"] = w.qa_receipt(verdict="superseded")
        message = refused(evaluate(world))["hosted_qa"]
        self.assertIn("no decisive run", message)
        self.assertIn("superseded", message)

    def test_a_newer_superseded_run_does_not_retract_an_older_pass(self) -> None:
        world = w.green_world()
        world["artifacts"].append(w.artifact(103, self.name, w.qa_receipt(verdict="superseded", run=36000000009), run=36000000009, created="2026-09-29T23:30:00Z"))
        receipt = evaluate(world)
        self.assertTrue(receipt["promotable"], refused(receipt))
        self.assertEqual(receipt["gates"]["hosted_qa"]["evidence"]["superseded_runs_ignored"], 1)

    def test_the_latest_decisive_verdict_wins_so_a_later_failure_refuses(self) -> None:
        world = w.green_world()
        world["artifacts"].append(w.artifact(103, self.name, w.qa_receipt(verdict="failed", run=36000000009), run=36000000009, created="2026-09-29T23:30:00Z"))
        self.assertIn("is 'failed'", refused(evaluate(world))["hosted_qa"])

    def test_a_partial_pass_is_refused(self) -> None:
        world = w.green_world()
        world["artifacts"][0]["receipt"] = w.qa_receipt(failed_steps=["hosted_shipper_bench"])
        self.assertIn("partial", refused(evaluate(world))["hosted_qa"])

    def test_the_run_must_be_completed_and_successful(self) -> None:
        for change in ({"status": "in_progress", "conclusion": None}, {"conclusion": "failure"}, {"conclusion": "cancelled"}, {"name": "CI"}):
            world = w.green_world()
            world["runs"][str(w.QA_RUN)].update(change)
            self.assertIn("not a completed successful run", refused(evaluate(world))["hosted_qa"], change)

    def test_a_missing_run_is_refused(self) -> None:
        world = w.green_world()
        world["runs"] = {}
        self.assertIn("cannot read Hosted Live QA run", refused(evaluate(world))["hosted_qa"])

    def test_a_receipt_for_another_commit_or_run_is_not_this_commits(self) -> None:
        world = w.green_world()
        world["artifacts"][0]["receipt"] = w.qa_receipt(sha=w.OTHER_SHA)
        self.assertIn("no decisive run", refused(evaluate(world))["hosted_qa"])
        world = w.green_world()
        world["artifacts"][0]["receipt"] = w.qa_receipt(run=1)
        self.assertIn("does not belong to the run", refused(evaluate(world))["hosted_qa"])

    def test_expired_artifacts_are_not_evidence(self) -> None:
        world = w.green_world()
        world["artifacts"][0]["expired"] = True
        self.assertIn("no Hosted Live QA verdict receipt", refused(evaluate(world))["hosted_qa"])

    def test_qa_of_a_different_digest_is_refused_but_an_unnamed_one_is_not_contradicted(self) -> None:
        world = w.green_world()
        world["artifacts"][0]["receipt"] = w.qa_receipt(digest=w.OTHER_DIGEST)
        self.assertIn("tested", refused(evaluate(world))["hosted_qa"])
        world = w.green_world()
        world["artifacts"][0]["receipt"] = w.qa_receipt(digest=None)
        self.assertTrue(evaluate(world)["promotable"])

    def test_a_malformed_receipt_is_missing_evidence(self) -> None:
        world = w.green_world()
        world["artifacts"][0]["receipt"] = {"schema": gates.QA_SCHEMA, "verdict": "passed"}
        self.assertFalse(evaluate(world)["gates"]["hosted_qa"]["ok"])


class EngineCompatGateTests(unittest.TestCase):
    name = f"engine-compat-{w.SHA}"

    def test_no_receipt_is_refused(self) -> None:
        world = w.green_world()
        world["artifacts"] = [a for a in world["artifacts"] if a["name"] != self.name]
        self.assertIn("no engine-compat receipt", refused(evaluate(world))["engine_compat"])

    def test_a_skip_is_not_a_pass(self) -> None:
        world = w.green_world()
        world["artifacts"][1]["receipt"] = w.compat_receipt(result="skipped")
        message = refused(evaluate(world))["engine_compat"]
        self.assertIn("'skipped'", message)
        self.assertIn("no published release", message)

    def test_a_receipt_for_another_commit_is_not_this_commits(self) -> None:
        world = w.green_world()
        world["artifacts"][1]["receipt"] = w.compat_receipt(sha=w.OTHER_SHA)
        self.assertIn("no engine-compat receipt", refused(evaluate(world))["engine_compat"])
        world = w.green_world()
        world["artifacts"][1]["workflow_run"]["head_sha"] = w.OTHER_SHA
        self.assertIn("no engine-compat receipt", refused(evaluate(world))["engine_compat"])

    def test_the_newest_receipt_decides(self) -> None:
        world = w.green_world()
        world["artifacts"].append(w.artifact(103, self.name, w.compat_receipt(result="skipped"), run=36000000010, created="2026-09-29T23:40:00Z"))
        self.assertIn("'skipped'", refused(evaluate(world))["engine_compat"])

    def test_names_the_previous_release_it_tested(self) -> None:
        evidence = evaluate(w.green_world())["gates"]["engine_compat"]["evidence"]
        self.assertEqual(evidence["previous_release_tag"], "v0.1.59")
        self.assertEqual(evidence["previous_engine"]["engine_sha256"], "c" * 64)


class SoakGateTests(unittest.TestCase):
    def test_no_real_tenant_needs_no_soak(self) -> None:
        evidence = evaluate(w.green_world())["gates"]["soak"]["evidence"]
        self.assertEqual((evidence["required_hours"], evidence["enforced"], evidence["real_tenants"]), (0, False, []))

    def test_a_real_tenant_makes_the_soak_binding_and_it_can_be_unmet(self) -> None:
        world = w.green_world()
        w.add_real_tenant(world, satisfied=False, earliest="2026-09-30T22:00:00")
        message = refused(evaluate(world))["soak"]
        self.assertIn("24h soak is required", message)
        self.assertIn("customer", message)
        self.assertIn("2026-09-30T22:00:00", message)

    def test_a_real_tenant_with_a_soaked_image_passes_and_names_the_deployment(self) -> None:
        world = w.green_world()
        w.add_real_tenant(world, satisfied=True, soaked_by={"deployment_id": "d-dogfood", "ready_at": "2026-09-29T00:00:00"})
        receipt = evaluate(world)
        self.assertTrue(receipt["promotable"], refused(receipt))
        evidence = receipt["gates"]["soak"]["evidence"]
        self.assertEqual(evidence["real_tenants"], ["customer"])
        self.assertEqual(evidence["soaked_by"]["deployment_id"], "d-dogfood")

    def test_unreadable_tenant_status_is_a_refusal_never_pre_launch(self) -> None:
        world = w.green_world()
        world["soak"] = None
        message = refused(evaluate(world))["soak"]
        self.assertIn("unknown is never read as pre-launch", message)

    def test_a_malformed_answer_is_a_refusal(self) -> None:
        for bad in ({}, {"required_hours": "0", "real_tenants": [], "satisfied": True}, {"required_hours": 0, "real_tenants": [], "satisfied": "yes"}, {"required_hours": 0, "satisfied": True}):
            world = w.green_world()
            world["soak"] = bad
            self.assertIn("malformed", refused(evaluate(world))["soak"], bad)

    def test_an_answer_about_another_image_is_not_this_images_soak(self) -> None:
        world = w.green_world()
        world["soak"] = {**world["soak"], "image": w.OTHER_DIGEST}
        # The fixture control plane echoes the asked image, so break the echo itself.
        sources = w.InProcessSources(world)
        honest = sources.control
        sources.control = lambda path, params=None: (  # type: ignore[method-assign]
            world["soak"] if path.endswith("production-soak") else honest(path, params)
        )
        receipt = gates.evaluate(CFG, sources, w.SHA)
        self.assertIn("is about", refused(receipt)["soak"])
        self.assertFalse(receipt["promotable"])

    def test_the_answer_is_the_control_planes_not_a_local_override(self) -> None:
        # Nothing here reads SOAK_HOURS or any environment variable: satisfied is the control plane's word.
        world = w.green_world()
        w.add_real_tenant(world, satisfied=False)
        self.assertFalse(evaluate(world)["promotable"])
        self.assertNotIn("SOAK_HOURS", Path(gates.__file__).read_text())


class WholeReceiptTests(unittest.TestCase):
    def test_every_gate_runs_so_one_check_lists_everything_missing(self) -> None:
        world = w.green_world()
        world["artifacts"] = []
        world["soak"] = None
        world["dogfood_health"]["build"]["commit"] = w.OTHER_SHA
        receipt = evaluate(world)
        self.assertEqual(set(refused(receipt)), {"dogfood", "hosted_qa", "engine_compat", "soak"})
        self.assertEqual(len(gates.refusals(receipt)), 4)
        self.assertFalse(receipt["promotable"])

    def test_an_unreadable_instance_list_blocks_the_plan(self) -> None:
        world = w.green_world()
        world["instances"] = None
        receipt = evaluate(world)
        self.assertFalse(receipt["promotable"])
        self.assertIn("cannot list control-plane instances", receipt["plan"]["refusal"])
        self.assertTrue(all(gate["ok"] for gate in receipt["gates"].values()))

    def test_a_malformed_instance_list_is_a_refusal_not_a_crash(self) -> None:
        world = w.green_world()
        world["instances"] = [{"subdomain": "acme", "status": "active"}]  # no id
        receipt = evaluate(world)
        self.assertFalse(receipt["promotable"])
        self.assertIn("malformed instance list", receipt["plan"]["refusal"])

    def test_an_unresolvable_dogfood_commit_is_a_refusal(self) -> None:
        world = w.green_world()
        world["dogfood_health"] = None
        receipt = evaluate(world, None)
        self.assertFalse(receipt["promotable"])
        self.assertIn("cannot resolve the commit dogfood", receipt["gates"]["dogfood"]["refusal"])

    def test_the_cli_rejects_a_short_or_uppercase_sha(self) -> None:
        for bad in ("abc123", "A" * 40):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                gates.main(["--dogfood-subdomain", w.DOGFOOD, "--sha", bad])


if __name__ == "__main__":
    unittest.main(verbosity=1)
