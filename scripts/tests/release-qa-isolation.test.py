#!/usr/bin/env python3
"""The Release workflow's QA job runs on its own instance and holds it: the invariants that keep it isolated.

Hosted Live QA once ran on the canary every push redeployed, and 19 of 100 runs failed because a newer commit
replaced it mid-run. The fix is a property of the workflow, not of any script: QA targets a dedicated instance
no other job touches, deploys the commit under test there itself, and holds it for the whole run because one
release runs at a time and is never cancelled mid-run. A refactor that points QA back at the deploy canary, or
lets a running release be cancelled, brings the failures back without breaking any other test, so it is pinned
here, together with the dogfood gate order.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")


def block(text: str, header: str) -> str:
    """The indented block that follows a top-level `header:` line."""
    match = re.search(rf"^{re.escape(header)}:\n((?:[ \t]+.*\n|\n)+)", text, re.MULTILINE)
    assert match, f"no top-level {header}: block"
    return match.group(1)


def job(name: str) -> str:
    match = re.search(rf"^  {re.escape(name)}:\n((?:    .*\n|\n)+)", WORKFLOW, re.MULTILINE)
    assert match, f"no job {name}"
    return match.group(1)


def step_names(text: str) -> list[str]:
    return re.findall(r"^      - name: (.+)$", text, re.MULTILINE)


QA = job("qa")


class ReleaseWorkflowTests(unittest.TestCase):
    def test_a_running_release_is_never_cancelled(self) -> None:
        concurrency = block(WORKFLOW, "concurrency")
        self.assertRegex(concurrency, r"cancel-in-progress:\s*false")
        self.assertNotIn("github.sha", concurrency, "a group that varies per commit or run excludes nothing")
        self.assertNotIn("run_id", concurrency)

    def test_qa_targets_the_qa_instance_and_never_the_deploy_canary(self) -> None:
        self.assertRegex(QA, r"QA_INSTANCE_SUBDOMAIN: \$\{\{ vars\.HOSTED_QA_SUBDOMAIN \}\}")
        self.assertRegex(QA, r"SMOKE_RUNTIME_TOKEN: \$\{\{ secrets\.HOSTED_QA_RUNTIME_TOKEN \}\}")
        self.assertRegex(QA, r"LONGHOUSE_MACHINE_TOKEN: \$\{\{ secrets\.HOSTED_QA_RUNTIME_TOKEN \}\}")
        self.assertNotIn("HOSTED_CANARY_RUNTIME_TOKEN", QA)
        # The one legitimate mention of the deploy canary is the guard that refuses to run against it.
        mentions = re.findall(r".*HOSTED_CANARY_SUBDOMAIN.*", QA)
        self.assertEqual(len(mentions), 1, mentions)
        self.assertIn("DEPLOY_CANARY_SUBDOMAIN", mentions[0])
        self.assertIn('== "${DEPLOY_CANARY_SUBDOMAIN,,}"', QA)

    def test_the_canary_job_never_targets_dogfood_or_the_qa_instance(self) -> None:
        canary = job("canary")
        self.assertRegex(canary, r"INSTANCE_SUBDOMAIN: \$\{\{ vars\.HOSTED_CANARY_SUBDOMAIN \}\}")
        self.assertNotIn("LONGHOUSE_DEFAULT_SUBDOMAIN", canary)
        self.assertNotIn("HOSTED_QA_RUNTIME_TOKEN", canary)

    def test_qa_deploys_the_exact_commit_before_it_measures_anything(self) -> None:
        names = step_names(QA)
        deploy = names.index("Deploy the image under test to the QA instance")
        verify = names.index("Verify the QA instance serves the commit under test")
        qa = names.index("Run hosted live QA")
        self.assertLess(deploy, verify)
        self.assertLess(verify, qa)
        self.assertIn(".qa-tools/scripts/ops/runtime-deploy.sh", QA)
        # An older commit's run must use today's tooling.
        self.assertRegex(QA, r"ref: \$\{\{ github\.sha \}\}\n\s+path: \.qa-tools")
        self.assertIn("RUNTIME_SOURCE_SHA: ${{ needs.plan.outputs.sha }}", QA)

    def test_the_verdict_watches_the_run_s_own_deployment_to_the_qa_instance(self) -> None:
        self.assertIn("OWN_DEPLOYMENT: ${{ steps.deploy_qa.outputs.deployment_id }}", QA)
        self.assertIn('--subdomain "$QA_INSTANCE_SUBDOMAIN"', QA)
        self.assertIn("python3 .qa-tools/scripts/qa/hosted-qa-verdict.py", QA)

    def test_dogfood_follows_the_canary_and_only_a_passed_qa_verdict(self) -> None:
        dogfood_header = re.search(r"^  dogfood:\n((?:    .*\n)+)", WORKFLOW, re.MULTILINE).group(1)
        self.assertIn("needs: [plan, build, canary, qa]", dogfood_header)
        self.assertIn("needs.canary.result == 'success'", dogfood_header)
        self.assertIn("needs.qa.outputs.verdict == 'passed'", dogfood_header)
        self.assertIn("needs.qa.result == 'skipped'", dogfood_header)
        # A live-preview change waits for its Terminal Truth verdict before anything is deployed.
        names = step_names(job("dogfood"))
        self.assertLess(names.index("Terminal Truth verdict"), names.index("Deploy the exact image"))
        self.assertLess(names.index("Health before"), names.index("Deploy the exact image"))

    def test_dogfood_is_only_ever_david010(self) -> None:
        dogfood = job("dogfood")
        self.assertIn("david010) ;;", dogfood)
        self.assertIn("INSTANCE_SUBDOMAIN: ${{ vars.LONGHOUSE_DEFAULT_SUBDOMAIN }}", dogfood)


if __name__ == "__main__":
    unittest.main()
