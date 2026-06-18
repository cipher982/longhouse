#!/usr/bin/env python3
"""Tests for the provider release-proof artifact wrapper."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CANARY = REPO_ROOT / "scripts" / "qa" / "provider-release-proof.py"


def _write_exe(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def _write_fake_repo(root: Path) -> None:
    manifest = {
        "schema_version": 1,
        "providers": [
            {
                "provider": "opencode",
                "operation_evidence": {
                    "launch_local": {
                        "level": "live_no_token",
                        "source": "fake provider-live canary",
                    },
                    "send_input": {
                        "level": "live_no_token",
                        "source": "fake provider-live canary",
                    },
                },
            }
        ],
    }
    manifest_path = root / "server" / "zerg" / "config" / "managed_provider_contracts.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    _write_exe(
        root / "scripts" / "qa" / "provider-live-canary.py",
        r"""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]

def value(flag):
    return args[args.index(flag) + 1]

verdict = os.environ.get("FAKE_VERDICT", "green")
artifact = {
    "artifact_kind": "provider_live_canary",
    "provider": value("--provider"),
    "provider_version": "opencode 1.2.3",
    "verdict": verdict,
    "failure_code": None if verdict == "green" else "fake_provider_break",
    "recommendation": "upgrade_allowed" if verdict == "green" else "block_upgrade_recommendation",
    "canaries": {
        "server_contract": {
            "status": "pass" if verdict == "green" else "fail",
            "failure_code": None if verdict == "green" else "fake_provider_break",
        }
    },
    "operation_evidence": {
        "launch_local": {"status": "pass", "level": "live_no_token", "canary": "server_contract"},
        "send_input": {
            "status": "pass" if verdict == "green" else "fail",
            "level": "live_no_token",
            "canary": "server_contract",
            "failure_code": None if verdict == "green" else "fake_provider_break",
        },
    },
}
Path(value("--artifact")).parent.mkdir(parents=True, exist_ok=True)
Path(value("--artifact")).write_text(json.dumps(artifact), encoding="utf-8")
raise SystemExit(0 if verdict != "red" else 1)
""",
    )


def _run_proof(
    root: Path,
    provider: str,
    *,
    env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict]:
    artifact = root / "artifact.json"
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    result = subprocess.run(
        [
            sys.executable,
            str(CANARY),
            "--repo-root",
            str(root / "repo"),
            "--provider",
            provider,
            "--provider-bin",
            str(root / "fake-provider"),
            "--artifact",
            str(artifact),
            "--evidence-root",
            str(root / "evidence"),
            "--json",
        ],
        cwd=REPO_ROOT,
        env=run_env,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, json.loads(artifact.read_text(encoding="utf-8"))


def test_opencode_release_proof_normalizes_source_canary() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(root, "opencode")

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["artifact_kind"] == "provider_release_proof"
        assert payload["provider"] == "opencode"
        assert payload["provider_version"] == "opencode 1.2.3"
        assert payload["verdict"] == "green"
        assert payload["failure_code"] is None
        assert payload["canaries"]["source_canary"]["status"] == "pass"
        assert payload["operation_evidence"]["send_input"]["status"] == "pass"
        assert payload["normalized"]["canaries"]["server_contract"]["status"] == "pass"
        assert Path(payload["artifacts"]["normalized_contract"]).exists()


def test_opencode_release_proof_blocks_on_source_canary_red() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "opencode",
            env={"FAKE_VERDICT": "red"},
        )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "fake_provider_break"
        assert payload["canaries"]["source_canary"]["status"] == "fail"
        assert payload["operation_evidence"]["send_input"]["status"] == "fail"


def test_gemini_release_proof_is_explicit_yellow_gap() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")

        result, payload = _run_proof(root, "gemini")

        assert result.returncode == 0
        assert payload["provider"] == "gemini"
        assert payload["verdict"] == "yellow"
        assert payload["failure_code"] == "provider_release_proof_not_implemented"


def main() -> int:
    tests = [
        test_opencode_release_proof_normalizes_source_canary,
        test_opencode_release_proof_blocks_on_source_canary_red,
        test_gemini_release_proof_is_explicit_yellow_gap,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
