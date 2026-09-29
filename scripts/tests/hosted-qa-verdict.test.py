#!/usr/bin/env python3
"""Hosted Live QA stands down when the shared canary is replaced mid-run.

Fixtures are the real shapes from 09-26..09-29: 19 of 100 runs failed and every
one overlapped a newer canary deployment. Timestamps below are the control
plane's own (UTC, no offset) and the runs' recorded windows.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from datetime import datetime
from datetime import timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("hosted_qa_verdict", ROOT / "scripts" / "qa" / "hosted-qa-verdict.py")
assert SPEC and SPEC.loader
verdict = importlib.util.module_from_spec(SPEC)
sys.modules["hosted_qa_verdict"] = verdict
SPEC.loader.exec_module(verdict)

CANARY = "release-canary-a"
ALL_GREEN = {"qa_live": "success", "hosted_shipper_bench": "success", "render_canary": "skipped", "cohort_journey": "success"}


def sha(prefix: str) -> str:
    return (prefix + "0" * 40)[:40]


def utc(value: str) -> datetime:
    return verdict.parse_ts(value)


def health(commit: str, *, dirty: bool = False) -> dict:
    return {"source_sha": None, "build": {"version": "0.1.56", "commit": commit, "dirty": dirty}}


def deployment(id: str, source: str, created: str, completed: str | None, status: str = "success") -> dict:
    return {"id": id, "status": status, "source_sha": sha(source), "created_at": created, "completed_at": completed}


def detail(*subdomains: str) -> dict:
    return {"targets": [{"subdomain": name, "deploy_state": "success"} for name in subdomains]}


class Control:
    """The two control-plane reads, over fixed rows."""

    def __init__(self, rows: list[dict], targets: dict[str, dict] | None = None, *, down: bool = False) -> None:
        self.rows = rows
        self.targets = targets or {}
        self.down = down
        self.reads = 0

    def list_deployments(self) -> list[dict]:
        self.reads += 1
        if self.down:
            raise OSError("control plane unreachable")
        return self.rows

    def get_deployment(self, deployment_id: str) -> dict:
        self.reads += 1
        return self.targets.get(deployment_id, detail(CANARY))


def run(
    *,
    expected: str,
    served: dict | None,
    steps: dict[str, str] | None = None,
    control: Control | None = None,
    started: str = "2026-09-29T17:35:10Z",
    now: str = "2026-09-29T17:41:21Z",
    own: str = "",
):
    control = control or Control([])
    return verdict.decide(
        expected_sha=expected,
        started_at=utc(started),
        now=utc(now),
        subdomain=CANARY,
        own_deployment=own,
        step_outcomes=steps or ALL_GREEN,
        health=served,
        list_deployments=control.list_deployments,
        get_deployment=control.get_deployment,
    )


class PassedTests(unittest.TestCase):
    def test_green_run_on_the_verified_commit_passes_without_reading_deployments(self) -> None:
        control = Control([], down=True)
        result = run(expected=sha("f4eff9d7c"), served=health(sha("f4eff9d7c")), control=control)
        self.assertEqual(result.verdict, "passed")
        self.assertEqual(control.reads, 0)

    def test_a_deployment_that_started_after_the_last_step_does_not_spoil_a_green_run(self) -> None:
        # Run 36357112492: green, and a deployment was submitted 40 s before the
        # run ended. Its cutover had not happened, so nothing measured moved.
        rows = [deployment("d-late", "c547f65e0", "2026-09-27T23:04:33", "2026-09-27T23:04:33.828866")]
        result = run(
            expected=sha("c547f65e0"),
            served=health(sha("c547f65e0")),
            control=Control(rows),
            started="2026-09-27T22:59:10Z",
            now="2026-09-27T23:05:12Z",
        )
        self.assertEqual(result.verdict, "passed")


class SupersededTests(unittest.TestCase):
    def test_canary_moved_after_green_steps_is_superseded_not_failed(self) -> None:
        # Run 36605964031: every QA step passed, then the post-QA source check
        # saw 05bfd7be7 where it had verified f4eff9d7c.
        result = run(
            expected=sha("f4eff9d7c"),
            served=health(sha("05bfd7be7")),
            control=Control([], down=True),
        )
        self.assertEqual(result.verdict, "superseded")
        self.assertTrue(result.ok)
        self.assertEqual(result.canary_now, sha("05bfd7be7"))

    def test_step_failure_during_a_cutover_that_health_cannot_see_yet_is_superseded(self) -> None:
        # Run 36349634779: the cohort journey failed at 20:58:08 inside the
        # 20:57:55-20:58:39 deployment of bcd59743, and the post-QA health read
        # at 20:58:13 still showed the old commit.
        rows = [
            deployment("d-20260927-205755-e703d9", "bcd59743", "2026-09-27T20:57:55", "2026-09-27T20:58:39.992024"),
            deployment("d-20260927-205057-d03384", "2822ddc5e", "2026-09-27T20:50:57", "2026-09-27T20:51:46.231814"),
        ]
        result = run(
            expected=sha("2822ddc5e"),
            served=health(sha("2822ddc5e")),
            steps={**ALL_GREEN, "cohort_journey": "failure"},
            control=Control(rows),
            started="2026-09-27T20:52:53Z",
            now="2026-09-27T20:58:13Z",
            own="d-20260927-205057-d03384",
        )
        self.assertEqual(result.verdict, "superseded")
        self.assertEqual([row["id"] for row in result.superseded_by], ["d-20260927-205755-e703d9"])
        self.assertEqual(result.failed_steps, ["cohort_journey"])

    def test_bench_502_while_the_canary_restarts_is_superseded(self) -> None:
        # Run 36516251542: bench got HTTP 502 at 03:16:13 during the 03:15:08 to
        # 03:16:33 deployment of 280fba7fd; verify then read 280fba7fd.
        rows = [deployment("d-new", "280fba7fd", "2026-09-29T03:15:20", "2026-09-29T03:16:33.5")]
        result = run(
            expected=sha("0daaa93a6"),
            served=health(sha("280fba7fd")),
            steps={**ALL_GREEN, "hosted_shipper_bench": "failure"},
            control=Control(rows),
            started="2026-09-29T03:14:50Z",
            now="2026-09-29T03:17:33Z",
        )
        self.assertEqual(result.verdict, "superseded")
        self.assertEqual(result.failed_steps, ["hosted_shipper_bench"])

    def test_a_deployment_still_in_flight_counts(self) -> None:
        rows = [deployment("d-active", "aaaa", "2026-09-29T17:40:00", None, status="active")]
        result = run(
            expected=sha("f4eff9d7c"), served=health(sha("f4eff9d7c")), steps={**ALL_GREEN, "qa_live": "failure"}, control=Control(rows)
        )
        self.assertEqual(result.verdict, "superseded")

    def test_unreadable_canary_during_a_deployment_is_superseded(self) -> None:
        rows = [deployment("d-active", "aaaa", "2026-09-29T17:41:00", None, status="active")]
        result = run(expected=sha("f4eff9d7c"), served=None, control=Control(rows))
        self.assertEqual(result.verdict, "superseded")

    def test_a_moved_canary_is_superseded_even_if_receipts_cannot_be_read(self) -> None:
        result = run(
            expected=sha("f4eff9d7c"),
            served=health(sha("05bfd7be7")),
            steps={**ALL_GREEN, "cohort_journey": "failure"},
            control=Control([], down=True),
        )
        self.assertEqual(result.verdict, "superseded")


class FailedTests(unittest.TestCase):
    def test_step_failure_with_no_deployment_in_the_run_is_a_real_failure(self) -> None:
        rows = [deployment("d-before", "f4eff9d7c", "2026-09-29T17:33:35", "2026-09-29T17:34:10.5")]
        result = run(
            expected=sha("f4eff9d7c"),
            served=health(sha("f4eff9d7c")),
            steps={**ALL_GREEN, "hosted_shipper_bench": "failure"},
            control=Control(rows),
        )
        self.assertEqual(result.verdict, "failed")
        self.assertFalse(result.ok)
        self.assertEqual(result.failed_steps, ["hosted_shipper_bench"])

    def test_unreadable_receipts_are_never_read_as_a_supersession(self) -> None:
        result = run(
            expected=sha("f4eff9d7c"),
            served=health(sha("f4eff9d7c")),
            steps={**ALL_GREEN, "qa_live": "failure"},
            control=Control([], down=True),
        )
        self.assertEqual(result.verdict, "failed")
        self.assertIn("receipts unavailable", result.reason)

    def test_qa_that_never_ran_is_not_a_pass(self) -> None:
        result = run(expected=sha("f4eff9d7c"), served=health(sha("f4eff9d7c")), steps={"qa_live": "skipped"})
        self.assertEqual(result.verdict, "failed")
        self.assertEqual(result.failed_steps, ["qa_live (did not run)"])

    def test_dirty_canary_fails_even_when_every_step_passed(self) -> None:
        result = run(expected=sha("f4eff9d7c"), served=health(sha("f4eff9d7c"), dirty=True))
        self.assertEqual(result.verdict, "failed")

    def test_unreadable_canary_with_no_deployment_fails(self) -> None:
        result = run(expected=sha("f4eff9d7c"), served=None)
        self.assertEqual(result.verdict, "failed")

    def test_cancelled_step_is_a_failure(self) -> None:
        result = run(expected=sha("f4eff9d7c"), served=health(sha("f4eff9d7c")), steps={**ALL_GREEN, "cohort_journey": "cancelled"})
        self.assertEqual(result.verdict, "failed")


class DeploymentEvidenceTests(unittest.TestCase):
    """Which receipts count as having replaced the canary."""

    def failing_run(self, rows: list[dict], targets: dict[str, dict] | None = None, **kwargs):
        return run(
            expected=sha("f4eff9d7c"),
            served=health(sha("f4eff9d7c")),
            steps={**ALL_GREEN, "qa_live": "failure"},
            control=Control(rows, targets),
            **kwargs,
        )

    def test_a_deployment_with_no_target_for_the_canary_did_not_touch_it(self) -> None:
        # d-20260927-230433 was a pointer-only submission: total 0 targets.
        rows = [deployment("d-pointer", "aaaa", "2026-09-29T17:38:00", "2026-09-29T17:38:01")]
        self.assertEqual(self.failing_run(rows, {"d-pointer": {"targets": []}}).verdict, "failed")
        self.assertEqual(self.failing_run(rows, {"d-pointer": detail("some-other-tenant")}).verdict, "failed")

    def test_the_receipt_this_run_observed_is_not_its_own_superseder(self) -> None:
        rows = [deployment("d-own", "f4eff9d7c", "2026-09-29T17:34:00", "2026-09-29T17:35:12")]
        self.assertEqual(self.failing_run(rows, own="d-own").verdict, "failed")

    def test_a_queued_candidate_that_was_itself_superseded_never_touched_the_canary(self) -> None:
        rows = [deployment("d-queued", "aaaa", "2026-09-29T17:38:00", "2026-09-29T17:38:05", status="superseded")]
        self.assertEqual(self.failing_run(rows).verdict, "failed")

    def test_dry_runs_never_touch_the_canary(self) -> None:
        rows = [deployment("d-dry", "aaaa", "2026-09-29T17:38:00", None, status="dry_run")]
        self.assertEqual(self.failing_run(rows).verdict, "failed")

    def test_stale_rows_do_not_supersede_every_later_run(self) -> None:
        # lifecycle-4-1 and lifecycle-6-1 sit paused with no completion time, and
        # an "active" row from days ago would look in flight forever.
        rows = [
            deployment("lifecycle-4-1", "", "2026-09-24T01:41:06", None, status="paused"),
            deployment("d-zombie", "aaaa", "2026-09-26T03:00:00", None, status="active"),
        ]
        self.assertEqual(self.failing_run(rows).verdict, "failed")

    def test_a_deployment_that_failed_mid_run_still_counts(self) -> None:
        rows = [deployment("d-bad", "aaaa", "2026-09-29T17:38:00", "2026-09-29T17:39:00", status="failure")]
        self.assertEqual(self.failing_run(rows).verdict, "superseded")

    def test_a_deployment_created_after_the_verdict_is_ignored(self) -> None:
        rows = [deployment("d-future", "aaaa", "2026-09-29T17:50:00", None, status="queued")]
        self.assertEqual(self.failing_run(rows).verdict, "failed")


class CommandLineTests(unittest.TestCase):
    def invoke(self, *, served: dict | None, steps: list[str], control: Control) -> tuple[int, dict[str, str]]:
        with tempfile.TemporaryDirectory() as tmp:
            output, summary = Path(tmp, "output"), Path(tmp, "summary")
            argv = [
                "--expected-sha", sha("f4eff9d7c"),
                "--started-at", "2026-09-29T17:35:10Z",
                "--subdomain", CANARY,
                "--own-deployment", "d-own",
                *[part for step in steps for part in ("--step", step)],
            ]  # fmt: skip
            env = {
                "CONTROL_PLANE_URL": "https://control.test",
                "CONTROL_PLANE_ADMIN_TOKEN": "token",
                "GITHUB_OUTPUT": str(output),
                "GITHUB_STEP_SUMMARY": str(summary),
            }
            with (
                mock.patch.dict(os.environ, env),
                mock.patch.object(verdict, "read_health", return_value=served),
                mock.patch.object(verdict, "control_plane_readers", return_value=(control.list_deployments, control.get_deployment)),
                mock.patch.object(verdict, "datetime", wraps=datetime) as clock,
            ):
                clock.now.return_value = utc("2026-09-29T17:41:21Z")
                code = verdict.main(argv)
            outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
            outputs["summary"] = summary.read_text()
            return code, outputs

    def test_superseded_exits_zero_and_records_the_replacing_deployment(self) -> None:
        rows = [deployment("d-new", "05bfd7be7", "2026-09-29T17:39:53", "2026-09-29T17:40:32.4")]
        code, out = self.invoke(
            served=health(sha("f4eff9d7c")),
            steps=["qa_live=success", "hosted_shipper_bench=failure", "render_canary=skipped", "cohort_journey=skipped"],
            control=Control(rows),
        )
        self.assertEqual(code, 0)
        self.assertEqual(out["verdict"], "superseded")
        self.assertEqual(out["failed_steps"], "hosted_shipper_bench")
        self.assertIn("d-new", out["summary"])

    def test_failed_exits_one(self) -> None:
        code, out = self.invoke(
            served=health(sha("f4eff9d7c")),
            steps=["qa_live=failure", "hosted_shipper_bench=skipped", "render_canary=skipped", "cohort_journey=skipped"],
            control=Control([]),
        )
        self.assertEqual(code, 1)
        self.assertEqual(out["verdict"], "failed")

    def test_passed_exits_zero(self) -> None:
        code, out = self.invoke(
            served=health(sha("f4eff9d7c")),
            steps=["qa_live=success", "hosted_shipper_bench=success", "render_canary=skipped", "cohort_journey=success"],
            control=Control([]),
        )
        self.assertEqual(code, 0)
        self.assertEqual(out["verdict"], "passed")


if __name__ == "__main__":
    unittest.main(verbosity=1)
