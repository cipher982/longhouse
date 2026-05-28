#!/usr/bin/env python3
"""Tests for stable local provider live-proof publishing."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PUBLISHER = REPO_ROOT / "scripts/qa/provider-live-proof-publish.py"


def _write_fake_canary(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        r"""#!/usr/bin/env python3
import argparse
import json
from datetime import UTC, datetime

parser = argparse.ArgumentParser()
parser.add_argument("--repo-root")
parser.add_argument("--provider", required=True)
parser.add_argument("--evidence-root", required=True)
parser.add_argument("--artifact", required=True)
parser.add_argument("--json", action="store_true")
parser.add_argument("--live-token-timeout-secs")
args = parser.parse_args()

artifact = {
    "schema_version": 1,
    "artifact_kind": "provider_live_canary",
    "provider": args.provider,
    "provider_version": "2.1.153" if args.provider == "claude" else "1.2.3",
    "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    "verdict": "green",
    "operation_evidence": {
        "send_input": {
            "status": "pass",
            "level": "live_no_token",
            "source": "fake provider-live-canary",
        }
    },
    "evidence_root": args.evidence_root,
    "received": {
        "live_token_timeout_secs": args.live_token_timeout_secs,
    },
}
with open(args.artifact, "w", encoding="utf-8") as handle:
    json.dump(artifact, handle)
if args.json:
    print(json.dumps(artifact))
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _write_fake_canary_without_artifact(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """#!/usr/bin/env python3\nprint('no artifact today')\n""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_publishes_stable_sidecar_from_live_canary() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fake_repo = root / "repo"
        proof_dir = root / "proof"
        _write_fake_canary(fake_repo / "scripts/qa/provider-live-canary.py")

        result = subprocess.run(
            [
                sys.executable,
                str(PUBLISHER),
                "--repo-root",
                str(fake_repo),
                "--provider",
                "claude",
                "--canary-script",
                str(fake_repo / "scripts/qa/provider-live-canary.py"),
                "--evidence-root",
                str(root / "evidence"),
                "--proof-dir",
                str(proof_dir),
                "--json",
            ],
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["results"][0]["status"] == "published"
        stable = proof_dir / "claude.json"
        artifact = json.loads(stable.read_text(encoding="utf-8"))
        assert artifact["artifact_kind"] == "provider_live_canary"
        assert artifact["provider"] == "claude"
        assert artifact["operation_evidence"]["send_input"]["level"] == "live_no_token"


def test_publishes_fallback_when_canary_is_missing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fake_repo = root / "repo"
        fake_repo.mkdir()
        proof_dir = root / "proof"

        result = subprocess.run(
            [
                sys.executable,
                str(PUBLISHER),
                "--repo-root",
                str(fake_repo),
                "--provider",
                "opencode",
                "--canary-script",
                str(fake_repo / "scripts/qa/provider-live-canary.py"),
                "--evidence-root",
                str(root / "evidence"),
                "--proof-dir",
                str(proof_dir),
                "--json",
            ],
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 1
        artifact = json.loads((proof_dir / "opencode.json").read_text(encoding="utf-8"))
        assert artifact["artifact_kind"] == "provider_live_canary"
        assert artifact["verdict"] == "red"
        assert artifact["failure_code"] == "live_canary_script_missing"
        assert artifact["canaries"]["provider_live_canary"]["status"] == "fail"


def test_missing_artifact_from_successful_canary_fails_publisher() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fake_repo = root / "repo"
        proof_dir = root / "proof"
        _write_fake_canary_without_artifact(fake_repo / "scripts/qa/provider-live-canary.py")

        result = subprocess.run(
            [
                sys.executable,
                str(PUBLISHER),
                "--repo-root",
                str(fake_repo),
                "--provider",
                "claude",
                "--canary-script",
                str(fake_repo / "scripts/qa/provider-live-canary.py"),
                "--evidence-root",
                str(root / "evidence"),
                "--proof-dir",
                str(proof_dir),
                "--json",
            ],
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 1
        payload = json.loads(result.stdout)
        assert payload["results"][0]["status"] == "published_fallback"
        artifact = json.loads((proof_dir / "claude.json").read_text(encoding="utf-8"))
        assert artifact["verdict"] == "red"
        assert artifact["failure_code"] == "live_canary_failed_to_emit_artifact"


def test_defaults_proof_dir_to_longhouse_home() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fake_repo = root / "repo"
        longhouse_home = root / ".longhouse-dev"
        _write_fake_canary(fake_repo / "scripts/qa/provider-live-canary.py")
        env = os.environ.copy()
        env.pop("LONGHOUSE_PROVIDER_LIVE_PROOF_DIR", None)
        env["LONGHOUSE_HOME"] = str(longhouse_home)
        env["LONGHOUSE_PROVIDER_RELEASE_STATUS_CONFIG"] = str(root / "missing-provider-status.env")

        result = subprocess.run(
            [
                sys.executable,
                str(PUBLISHER),
                "--repo-root",
                str(fake_repo),
                "--provider",
                "claude",
                "--canary-script",
                str(fake_repo / "scripts/qa/provider-live-canary.py"),
                "--evidence-root",
                str(root / "evidence"),
                "--json",
            ],
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["proof_dir"] == str((longhouse_home / "provider-live-proof").resolve())
        assert (longhouse_home / "provider-live-proof" / "claude.json").exists()


if __name__ == "__main__":
    test_publishes_stable_sidecar_from_live_canary()
    test_publishes_fallback_when_canary_is_missing()
    test_missing_artifact_from_successful_canary_fails_publisher()
    test_defaults_proof_dir_to_longhouse_home()
