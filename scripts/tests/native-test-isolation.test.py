"""Boundaries of the native dispatch preflight and the checkout it depends on."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "native_test_isolation", ROOT / "scripts/qa/native-test-isolation.py"
)
native = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = native
spec.loader.exec_module(native)


class AuthenticatedGhBoundaryTests(unittest.TestCase):
    def test_missing_gh_names_the_credential_not_the_command(self):
        with mock.patch.object(
            native.subprocess, "run", side_effect=FileNotFoundError("gh")
        ):
            with self.assertRaises(ValueError) as context:
                native.require_authenticated_gh()
        self.assertIn("gh auth login", str(context.exception))
        self.assertIn("GitHub CLI", str(context.exception))

    def test_unauthenticated_gh_is_reported_as_such(self):
        failure = subprocess.CalledProcessError(1, ["gh", "auth", "status"])
        with mock.patch.object(native.subprocess, "run", side_effect=failure):
            with self.assertRaises(ValueError) as context:
                native.require_authenticated_gh()
        self.assertIn("gh auth login", str(context.exception))

    def test_authenticated_gh_passes_through(self):
        with mock.patch.object(native.subprocess, "run", return_value=None):
            native.require_authenticated_gh()


boundary_spec = importlib.util.spec_from_file_location(
    "test_boundary", ROOT / "scripts/qa/test_boundary.py"
)
boundary = importlib.util.module_from_spec(boundary_spec)
boundary_spec.loader.exec_module(boundary)


class NativeRunnerBoundaryTests(unittest.TestCase):
    def runner(self, **environment: str) -> bool:
        with mock.patch.dict(os.environ, environment, clear=True):
            with mock.patch.object(boundary.sys, "platform", "darwin"):
                return boundary.native_runner()

    def test_a_github_hosted_vm_and_the_wisp_runner_are_native_runners(self):
        self.assertTrue(
            self.runner(GITHUB_ACTIONS="true", RUNNER_ENVIRONMENT="github-hosted")
        )
        self.assertTrue(
            self.runner(
                GITHUB_ACTIONS="true", RUNNER_ENVIRONMENT="self-hosted", RUNNER_NAME="wisp"
            )
        )

    def test_any_other_self_hosted_runner_and_a_plain_login_are_not(self):
        self.assertFalse(
            self.runner(
                GITHUB_ACTIONS="true", RUNNER_ENVIRONMENT="self-hosted", RUNNER_NAME="cinder"
            )
        )
        self.assertFalse(self.runner(RUNNER_ENVIRONMENT="self-hosted", RUNNER_NAME="wisp"))
        self.assertFalse(self.runner())


class DispatchRunsOnTheBenchTests(unittest.TestCase):
    def test_dispatch_from_inside_the_bench_lock_is_refused_not_deadlocked(self):
        """bench.sh holds the lock the dispatched job waits for on the same Mac."""

        with tempfile.TemporaryDirectory() as scratch:
            options = Path(scratch) / "options.json"
            options.write_text("{}")
            args = native.argparse.Namespace(
                target="test-ios",
                options_json=options,
                artifact_dir=Path(scratch) / "out",
                timeout=1,
            )
            with mock.patch.dict(os.environ, {"BENCH_OUT": scratch}):
                with self.assertRaises(ValueError) as context:
                    native.run(args)
        self.assertIn("bench lock", str(context.exception))

    def test_the_workflow_runs_on_wisp_and_leaves_no_actions_artifact(self):
        workflow = (ROOT / ".github/workflows/native-test-isolation.yml").read_text()
        self.assertIn("runs-on: wisp", workflow)
        self.assertNotIn("upload-artifact", workflow)
        self.assertIn(native.EVIDENCE_DIR, workflow)


class DispatchTargetsTheCallerRevisionTests(unittest.TestCase):
    def test_workflow_checks_out_the_dispatched_source_sha(self):
        """The workflow definition comes from main; the tested revision does not.

        Reading a dispatched run's own head_sha as "what was tested" is the error
        this pins: the VM checks out inputs.source_sha.
        """

        workflow = (ROOT / ".github/workflows/native-test-isolation.yml").read_text()
        checkout = workflow.split("- uses: actions/checkout", 1)[1].split("- ", 1)[0]
        self.assertIn("ref: ${{ inputs.source_sha }}", checkout)


class NoHostedRunnerTests(unittest.TestCase):
    def test_no_workflow_asks_for_a_hosted_runner(self):
        """Hosted jobs are out of budget while the repository is private ("The job was not started
        because an Actions budget is preventing further use"): macOS runs on wisp, Linux on crunch
        or cube, so a hosted label would queue or fail and never start."""

        offenders = []
        for workflow in sorted((ROOT / ".github/workflows").glob("*.yml")):
            for number, line in enumerate(workflow.read_text().splitlines(), 1):
                code = line.split("#", 1)[0]
                if not ("runs-on" in code or "os:" in code):
                    continue
                if "macos-" in code or "ubuntu-" in code or "windows-" in code:
                    offenders.append(f"{workflow.name}:{number}: {line.strip()}")
        self.assertEqual(offenders, [])

    def test_the_bench_lock_wrapper_only_locks_on_wisp(self):
        wrapper = ROOT / "scripts/ci/with-bench-lock.sh"
        elsewhere = subprocess.run(
            [str(wrapper), "echo", "ran"],
            env={"PATH": os.environ["PATH"]},
            capture_output=True,
            text=True,
        )
        self.assertEqual((elsewhere.returncode, elsewhere.stdout.strip()), (0, "ran"))
        body = wrapper.read_text()
        self.assertIn("/tmp/longhouse-bench.lock", body)
        self.assertIn('RUNNER_NAME:-}" == "wisp"', body)


if __name__ == "__main__":
    unittest.main()
