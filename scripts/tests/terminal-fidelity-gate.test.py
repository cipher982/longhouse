#!/usr/bin/env python3
"""False-pass boundaries for the local terminal-fidelity composer, without providers."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("terminal_fidelity_gate", ROOT / "scripts/qa/terminal-fidelity-gate.py")
assert spec is not None and spec.loader is not None
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

RUN = "00000000-0000-4000-8000-000000000001"
SESSION = "00000000-0000-4000-8000-000000000002"
OTHER_SESSION = "00000000-0000-4000-8000-000000000003"


def published_commit(tag: str) -> str | None:
    """The remote's own commit for a tag, or None when the remote is unreachable."""

    try:
        listing = gate.capture(
            ["git", "ls-remote", gate.CANONICAL_REMOTE, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"],
            cwd=gate.ROOT,
            timeout=60,
        )
    except Exception:
        return None
    rows = [line.split("\t")[0] for line in listing.splitlines() if "\t" in line]
    return rows[-1] if rows else None


class FidelityBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        # Deliberately not a provider-convention filename: only ownership evidence
        # may select the original bytes for the native client's immutability check.
        self.source = self.root / "actual-owned-history.data"
        self.source.write_bytes(b"original provider history\n")
        self.report = {
            "provider": "codex",
            "device_id": "machine",
            "verdict": "green",
            "session_id": SESSION,
            "run_id": RUN,
            "marker": "LH_SERVED_ALDER_FERN",
        }
        self.claim = {
            "provider": "codex",
            "session_id": SESSION,
            "run_id": RUN,
            "provider_identity_confirmed": True,
            "provider_thread_id": "native-thread",
            "source_path": str(self.source),
            "state": "terminal",
            "result": {"terminal_state": "run_completed"},
        }
        self.claim_path = self.root / f"{RUN}.json"

    def case(self):
        self.claim_path.write_text(json.dumps(self.claim))
        return gate.fidelity_case(self.report, "codex", "machine", self.root)

    def test_manifest_uses_exact_managed_source_without_touching_original_bytes(self):
        case, evidence = self.case()
        self.assertEqual(
            case, {"name": "machine/codex", "session_id": SESSION, "markers": ["LH_SERVED_ALDER_FERN"], "source_path": str(self.source)}
        )
        self.assertEqual(evidence["source_sha256"], gate.digest(self.source))
        self.assertEqual(self.source.read_bytes(), b"original provider history\n")

    def test_other_sessions_source_cannot_qualify_successful_console_case(self):
        self.claim["session_id"] = OTHER_SESSION
        with self.assertRaises(ValueError):
            self.case()
        self.claim["session_id"] = SESSION
        self.claim["provider_identity_confirmed"] = False
        with self.assertRaises(ValueError):
            self.case()

    def test_upstream_failure_stays_failure_even_with_a_readable_source(self):
        self.claim["result"]["terminal_state"] = "run_failed"
        with self.assertRaises(ValueError):
            self.case()
        self.claim["result"]["terminal_state"] = "run_completed"
        self.report["verdict"] = "error"
        with self.assertRaises(ValueError):
            self.case()

    def test_required_stage_or_failed_requested_provider_cannot_be_omitted(self):
        summary = {
            "providers": [{"provider": "codex", "status": "pass"}],
            "stages": {name: {"status": "pass"} for name in gate.REQUIRED_STAGES},
        }
        self.assertTrue(gate.passing(summary))
        for name in gate.REQUIRED_STAGES:
            with self.subTest(stage=name):
                stage = summary["stages"].pop(name)
                self.assertFalse(gate.passing(summary))
                summary["stages"][name] = stage
        summary["providers"].append({"provider": "cursor", "status": "fail"})
        self.assertFalse(gate.passing(summary))

    def test_a_dev_build_is_not_a_released_build(self):
        # The positive case must be a really published tag, because the remote
        # is the authority the check uses; a local tag can only ever fail.
        tag = "v0.1.46"
        commit = published_commit(tag)
        if commit is None:
            self.skipTest("remote tags unreachable; the released check is network-backed by design")
        release = {
            "version": tag.removeprefix("v"),
            "commit": commit,
            "commit_short": commit[:8],
            "channel": "release",
            "dirty": False,
        }
        dev = {**release, "channel": "dev"}
        dirty_release = {**release, "dirty": True}

        def provenance(server, cli, engine):
            # Stub only the build-identity call. The tag lookup must stay real:
            # it is the half of this check that a working tree cannot forge.
            original = gate.capture

            def capture(command, **kwargs):
                if command[0] == "longhouse":
                    return json.dumps({"facade": cli, "engine": engine, "engine_path": "/x"})
                return original(command, **kwargs)

            gate.capture = capture
            try:
                return gate.release_provenance(server)
            finally:
                gate.capture = original

        self.assertTrue(provenance(release, release, release)["released"])
        self.assertFalse(provenance(dev, release, release)["released"])
        self.assertFalse(provenance(release, dev, release)["released"])
        self.assertFalse(provenance(release, release, dev)["released"])
        # A dirty tree disqualifies a build whatever its channel claims.
        self.assertFalse(provenance(release, release, dirty_release)["released"])
        self.assertFalse(provenance(release, release, None)["released"])

    def test_the_published_ref_is_read_from_the_canonical_remote_not_origin(self):
        """`origin` is local configuration and can be pointed anywhere."""

        seen = []
        original = gate.capture
        build = {"version": "0.0.1", "commit": "0" * 40, "commit_short": "0" * 8, "channel": "release", "dirty": False}

        def capture(command, **kwargs):
            if command[0] == "longhouse":
                return json.dumps({"facade": build, "engine": build, "engine_path": "/x"})
            seen.append(command)
            return ""

        gate.capture = capture
        try:
            gate.release_provenance(build)
        finally:
            gate.capture = original
        self.assertTrue(seen)
        for command in seen:
            self.assertEqual(command[:3], ["git", "ls-remote", gate.CANONICAL_REMOTE])

    def test_a_locally_created_tag_cannot_make_a_build_released(self):
        """A clean tree can run `git tag v<version> HEAD`; the remote cannot be told to."""

        head = gate.capture(["git", "rev-parse", "HEAD"], cwd=gate.ROOT)
        # 0.0.1 is not, and will not be, a published Longhouse release.
        forged = {
            "version": "0.0.1",
            "commit": head,
            "commit_short": head[:8],
            "channel": "release",
            "dirty": False,
        }
        original = gate.capture

        def capture(command, **kwargs):
            if command[0] == "longhouse":
                return json.dumps({"facade": forged, "engine": forged, "engine_path": "/x"})
            if command[:2] == ["git", "ls-remote"]:
                # Stand in for the local tag the reviewer created: rev-parse
                # would resolve it, and ls-remote against origin does not.
                return ""
            return original(command, **kwargs)

        gate.capture = capture
        try:
            result = gate.release_provenance(forged)
        finally:
            gate.capture = original
        self.assertFalse(result["released"])

    def test_a_release_channel_claim_without_a_matching_tag_is_not_released(self):
        """`channel` is self-attested; the published ref is not."""

        forged = {"version": "9.9.9", "commit": "0" * 40, "commit_short": "00000000", "channel": "release", "dirty": False}
        original = gate.capture

        def capture(command, **kwargs):
            if command[0] == "longhouse":
                return json.dumps({"facade": forged, "engine": forged, "engine_path": "/x"})
            return original(command, **kwargs)

        gate.capture = capture
        try:
            result = gate.release_provenance(forged)
        finally:
            gate.capture = original
        self.assertFalse(result["released"])
        self.assertFalse(any(item["released"] for item in result["components"].values()))

    def test_receipt_names_the_first_failed_boundary_and_its_next_command(self):
        summary = {
            "stages": {
                "preflight": {
                    "status": "pass",
                    "release_provenance": {"released": False, "components": {}},
                    "machine": {"provider_readiness": {"codex": {"state": "ready"}}},
                },
                "console": {"status": "pass"},
                "manifest": {"status": "pass"},
                "web": {"status": "fail"},
                "ios": {"status": "not_run"},
            },
            # The production shape: the Console child reports a verdict word.
            "providers": [{"provider": "codex", "status": "pass", "verdict": "green"}],
        }
        mark = gate.receipt(summary, Path("/evidence"))
        self.assertEqual(mark["failed_boundary"], "web")
        self.assertEqual(mark["evidence"], "/evidence/web.json")
        self.assertIn("test-terminal-fidelity-web", mark["next_command"])
        self.assertIs(mark["released_build"], False)
        self.assertEqual(mark["provider_verdicts"], {"codex": "green"})
        self.assertEqual(mark["provider_readiness"], {"codex": {"state": "ready"}})

    def test_receipt_reports_a_failed_provider_when_every_stage_looks_green(self):
        summary = {
            "stages": {name: {"status": "pass"} for name in gate.REQUIRED_STAGES},
            "providers": [{"provider": "codex", "status": "fail", "verdict": "red"}],
        }
        mark = gate.receipt(summary, Path("/evidence"))
        self.assertEqual(mark["failed_boundary"], "console")

    def test_receipt_names_a_stage_that_never_ran(self):
        """A missing status is a failed boundary, not a silent pass."""

        summary = {"stages": {"preflight": {"status": "pass"}}, "providers": []}
        mark = gate.receipt(summary, Path("/evidence"))
        self.assertEqual(mark["failed_boundary"], "console")
        self.assertEqual(mark["evidence"], "/evidence/console.json")

    def test_receipt_survives_a_successful_run(self):
        summary = {
            "stages": {name: {"status": "pass"} for name in gate.REQUIRED_STAGES},
            "providers": [{"provider": "codex", "status": "pass", "verdict": "green"}],
        }
        mark = gate.receipt(summary, Path("/evidence"))
        self.assertIsNone(mark["failed_boundary"])
        self.assertEqual(mark["evidence"], "/evidence/summary.json")
        self.assertEqual(mark["next_command"], "")

    def test_timed_out_child_retains_output_without_credentials_and_never_passes(self):
        token = "private-runtime-token-for-test"
        result = gate.command_stage(
            "timeout",
            [sys.executable, "-c", "import os,time; print(os.environ['PROOF_TOKEN'], flush=True); time.sleep(30)"],
            self.root,
            {**os.environ, "PROOF_TOKEN": token},
            token,
            1.0,
        )
        self.assertEqual(result["status"], "fail")
        self.assertNotEqual(result["exit_code"], 0)
        self.assertEqual((self.root / "timeout.log").read_text(), "[REDACTED]\n")
        self.assertEqual(json.loads((self.root / "timeout.json").read_text())["status"], "fail")
        self.assertNotIn(token, (self.root / "timeout.json").read_text())


if __name__ == "__main__":
    unittest.main()
