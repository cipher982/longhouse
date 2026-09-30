#!/usr/bin/env python3
"""Hosted Live QA runs on its own instance and holds it: the invariants that keep it isolated.

The fix for 19 of 100 runs failing on a canary a newer push redeployed
(control-plane repo, docs/specs/build-compute-pipeline.md, section 2) is a property of the workflow, not of any
script: QA must target a dedicated instance that no push touches, deploy the
commit under test there itself, and hold the instance for the whole run by
queueing (never cancelling, never dropping) concurrent runs. A refactor that
points QA back at the deploy canary, or lets a queued run be cancelled, brings
the failures back without breaking any other test, so it is pinned here.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (ROOT / ".github" / "workflows" / "hosted-live-qa.yml").read_text(encoding="utf-8")


def block(text: str, header: str) -> str:
    """The indented block that follows a top-level `header:` line."""
    match = re.search(rf"^{re.escape(header)}:\n((?:[ \t]+.*\n|\n)+)", text, re.MULTILINE)
    assert match, f"no top-level {header}: block"
    return match.group(1)


def step_names() -> list[str]:
    return re.findall(r"^      - name: (.+)$", WORKFLOW, re.MULTILINE)


class HostedLiveQaIsolationTests(unittest.TestCase):
    def test_the_instance_is_a_mutex_whose_waiters_are_queued_not_dropped(self) -> None:
        concurrency = block(WORKFLOW, "concurrency")
        self.assertIn("vars.HOSTED_QA_SUBDOMAIN", concurrency, "the group must be per QA instance")
        self.assertNotIn("github.sha", concurrency, "a group that varies per commit or run excludes nothing")
        self.assertNotIn("run_id", concurrency)
        self.assertRegex(concurrency, r"(?m)^\s+queue: max\s*$", "the default keeps one pending run and cancels the rest")
        self.assertNotRegex(concurrency, r"cancel-in-progress:\s*true")

    def test_qa_targets_the_qa_instance_and_never_the_deploy_canary(self) -> None:
        self.assertRegex(WORKFLOW, r"QA_INSTANCE_SUBDOMAIN: \$\{\{ vars\.HOSTED_QA_SUBDOMAIN \}\}")
        self.assertRegex(WORKFLOW, r"SMOKE_RUNTIME_TOKEN: \$\{\{ secrets\.HOSTED_QA_RUNTIME_TOKEN \}\}")
        self.assertRegex(WORKFLOW, r"LONGHOUSE_MACHINE_TOKEN: \$\{\{ secrets\.HOSTED_QA_RUNTIME_TOKEN \}\}")
        self.assertNotIn("HOSTED_CANARY_RUNTIME_TOKEN", WORKFLOW)
        self.assertNotIn("HOSTED_CANARY_TELEMETRY_TOKEN", WORKFLOW)
        # The one legitimate mention of the deploy canary is the guard that refuses to run against it.
        mentions = re.findall(r".*HOSTED_CANARY_SUBDOMAIN.*", WORKFLOW)
        self.assertEqual(len(mentions), 1, mentions)
        self.assertIn("DEPLOY_CANARY_SUBDOMAIN", mentions[0])
        self.assertIn('== "${DEPLOY_CANARY_SUBDOMAIN,,}"', WORKFLOW)

    def test_the_run_deploys_the_exact_commit_before_it_measures_anything(self) -> None:
        names = step_names()
        deploy = names.index("Deploy the image under test to the QA instance")
        verify = names.index("Verify the QA instance serves the commit under test")
        qa = names.index("Run hosted live QA")
        self.assertLess(deploy, verify)
        self.assertLess(verify, qa)
        self.assertIn(".qa-tools/scripts/ops/runtime-deploy.sh", WORKFLOW)
        # An older commit's run must use today's tooling, and QA order is deploy order, not commit order.
        self.assertRegex(WORKFLOW, r"ref: \$\{\{ github\.sha \}\}\n\s+path: \.qa-tools")
        self.assertIn('RUNTIME_SOURCE_ORDER="$(date +%s)"', WORKFLOW)
        self.assertIn('export RUNTIME_SOURCE_SHA="$expected"', WORKFLOW)

    def test_only_a_run_that_deployed_its_own_image_uploads_promotion_evidence(self) -> None:
        upload = re.search(r"- name: Upload verdict receipt\n\s+if: (.+)\n", WORKFLOW)
        assert upload, "no verdict receipt upload step"
        self.assertIn("steps.deploy_qa.outcome == 'success'", upload.group(1))

    def test_the_verdict_watches_the_run_s_own_deployment_to_the_qa_instance(self) -> None:
        self.assertIn("OWN_DEPLOYMENT: ${{ steps.deploy_qa.outputs.deployment_id }}", WORKFLOW)
        self.assertIn('--subdomain "$QA_INSTANCE_SUBDOMAIN"', WORKFLOW)
        self.assertIn("python3 .qa-tools/scripts/qa/hosted-qa-verdict.py", WORKFLOW)


if __name__ == "__main__":
    unittest.main()
