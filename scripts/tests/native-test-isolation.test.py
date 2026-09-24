"""Boundaries of the native dispatch preflight and the checkout it depends on."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
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


class DispatchTargetsTheCallerRevisionTests(unittest.TestCase):
    def test_workflow_checks_out_the_dispatched_source_sha(self):
        """The workflow definition comes from main; the tested revision does not.

        Reading a dispatched run's own head_sha as "what was tested" is the error
        this pins: the VM checks out inputs.source_sha.
        """

        workflow = (ROOT / ".github/workflows/native-test-isolation.yml").read_text()
        checkout = workflow.split("- uses: actions/checkout", 1)[1].split("- ", 1)[0]
        self.assertIn("ref: ${{ inputs.source_sha }}", checkout)


if __name__ == "__main__":
    unittest.main()
