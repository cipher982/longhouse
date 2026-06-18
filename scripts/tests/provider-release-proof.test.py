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
import time
from pathlib import Path

args = sys.argv[1:]

def value(flag):
    return args[args.index(flag) + 1]

if os.environ.get("FAKE_TIMEOUT") == "1":
    time.sleep(10)

if os.environ.get("FAKE_SKIP_ARTIFACT") == "1":
    raise SystemExit(0)

provider = value("--provider")
if provider == "claude":
    missing = ["--session-id"] if os.environ.get("FAKE_CLAUDE_MISSING_SESSION_ID") == "1" else []
    channels_unconfirmed = os.environ.get("FAKE_CLAUDE_CHANNELS_UNCONFIRMED") == "1"
    verdict = "red" if missing else "yellow" if channels_unconfirmed else "green"
    artifact = {
        "artifact_kind": "provider_live_canary",
        "provider": "claude",
        "provider_version": "Claude Code 2.9.9",
        "verdict": verdict,
        "failure_code": "claude_command_contract_missing" if missing else None,
        "recommendation": "block_upgrade_recommendation"
        if missing
        else "investigate_before_upgrade"
        if channels_unconfirmed
        else "upgrade_allowed",
        "canaries": {
            "binary_identity": {"status": "pass", "version": "Claude Code 2.9.9"},
            "command_shape": {
                "status": "fail" if missing else "pass",
                "failure_code": "claude_command_contract_missing" if missing else None,
                "missing": missing,
            },
            "channels_shape": {
                "status": "warn" if channels_unconfirmed else "pass",
                "reason": "claude_development_channels_contract_unconfirmed"
                if channels_unconfirmed
                else None,
                "missing": ["--resume"] if channels_unconfirmed else [],
            },
            "detached_pty_shape": {"status": "pass", "platform": "darwin"},
        },
        "operation_evidence": {
            "launch_local": {
                "status": "fail" if missing else "pass",
                "level": "none" if missing else "live_no_token",
                "canary": "claude_launch_local_no_token",
                "failure_code": "claude_command_contract_missing" if missing else None,
            }
        },
    }
    Path(value("--artifact")).parent.mkdir(parents=True, exist_ok=True)
    Path(value("--artifact")).write_text(json.dumps(artifact), encoding="utf-8")
    raise SystemExit(1 if verdict == "red" else 0)

verdict = os.environ.get("FAKE_VERDICT", "green")
artifact = {
    "artifact_kind": "provider_live_canary",
    "provider": provider,
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

    _write_exe(
        root / "scripts" / "qa" / "codex-provider-release-canary.py",
        r"""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]

def value(flag, default=None):
    return args[args.index(flag) + 1] if flag in args else default

args_path = os.environ.get("FAKE_CODEX_ARGS_PATH")
if args_path:
    Path(args_path).write_text(json.dumps(args), encoding="utf-8")

source_review_status = value("--source-review-status", "missing")
artifact = {
    "artifact_kind": "codex_provider_release_canary",
    "provider": "codex",
    "codex_version": value("--provider-version", "codex 9.9.9"),
    "codex_bin": value("--codex-bin"),
    "longhouse_commit": "abc123",
    "verdict": "yellow" if source_review_status == "not_run" else "green",
    "failure_code": "insufficient_coverage" if source_review_status == "not_run" else None,
    "recommendation": "investigate_before_upgrade" if source_review_status == "not_run" else "upgrade_allowed",
    "source_review": {"status": source_review_status, "note": value("--source-review-note", "")},
    "canaries": {
        "binary_identity": {
            "status": "pass",
            "version": value("--provider-version", "codex 9.9.9"),
            "path": value("--codex-bin"),
        },
        "raw_fresh_remote": {
            "status": "pass",
            "protocol_fingerprints": {
                "status": "ok",
                "path": "/tmp/noisy/codex.jsonl",
                "responses": {"initialize": {"platformFamily": "str"}},
                "notifications": {"thread/started": {"threadId": "str"}},
                "server_requests": {},
                "response_errors": {},
            },
        }
    },
    "operation_evidence": {
        "launch_local": {
            "status": "not_run" if source_review_status == "not_run" else "pass",
            "level": "none" if source_review_status == "not_run" else "live_no_token",
            "canary": "fake_codex_release_canary",
        }
    },
}
Path(value("--artifact")).parent.mkdir(parents=True, exist_ok=True)
Path(value("--artifact")).write_text(json.dumps(artifact), encoding="utf-8")
""",
    )


def _run_proof(
    root: Path,
    provider: str,
    *,
    env: dict[str, str] | None = None,
    extra_args: list[str] | None = None,
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
            *(extra_args or []),
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


def test_opencode_release_proof_blocks_when_source_artifact_missing() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "opencode",
            env={"FAKE_SKIP_ARTIFACT": "1"},
        )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "provider_release_proof_source_missing"


def test_opencode_release_proof_blocks_when_source_canary_times_out() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "opencode",
            env={"FAKE_TIMEOUT": "1"},
            extra_args=["--timeout-secs", "1"],
        )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "provider_release_proof_timeout"


def test_codex_release_proof_maps_provider_binary_and_keeps_source_review_honest() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        args_path = root / "codex-args.json"
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "codex",
            env={"FAKE_CODEX_ARGS_PATH": str(args_path)},
            extra_args=["--provider-version", "codex 2.0.0"],
        )

        assert result.returncode == 0
        codex_args = json.loads(args_path.read_text(encoding="utf-8"))
        assert codex_args[codex_args.index("--codex-bin") + 1] == str(root / "fake-provider")
        assert codex_args[codex_args.index("--source-review-status") + 1] == "not_run"
        assert payload["provider"] == "codex"
        assert payload["provider_version"] == "codex 2.0.0"
        assert payload["verdict"] == "yellow"
        assert payload["failure_code"] == "insufficient_coverage"
        assert payload["normalized"]["provider_version"] == "codex 2.0.0"
        assert payload["normalized"]["source_review"]["status"] == "not_run"
        assert payload["normalized"]["codex"] == {
            "binary_present": True,
            "longhouse_commit_present": True,
        }
        assert payload["normalized"]["canaries"]["binary_identity"]["version"] == "codex 2.0.0"
        fingerprints = payload["normalized"]["canaries"]["raw_fresh_remote"]["protocol_fingerprints"]
        assert "path" not in fingerprints
        assert fingerprints["responses"]["initialize"]["platformFamily"] == "str"


def test_claude_release_proof_normalizes_no_token_contract_shape() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "claude",
            env={"FAKE_CLAUDE_CHANNELS_UNCONFIRMED": "1"},
        )

        assert result.returncode == 0
        assert payload["provider"] == "claude"
        assert payload["provider_version"] == "Claude Code 2.9.9"
        assert payload["verdict"] == "yellow"
        assert payload["normalized"]["claude"] == {
            "launch_flags_missing": [],
            "development_channels_status": "warn",
            "development_channels_missing": ["--resume"],
            "detached_pty_status": "pass",
            "detached_pty_platform": "darwin",
        }
        assert payload["normalized"]["canaries"]["channels_shape"]["reason"] == (
            "claude_development_channels_contract_unconfirmed"
        )


def test_claude_release_proof_red_when_session_flag_missing() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "claude",
            env={"FAKE_CLAUDE_MISSING_SESSION_ID": "1"},
        )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "claude_command_contract_missing"
        assert payload["normalized"]["claude"]["launch_flags_missing"] == ["--session-id"]
        assert payload["operation_evidence"]["launch_local"]["failure_code"] == (
            "claude_command_contract_missing"
        )


def test_gemini_release_proof_is_explicit_yellow_gap() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")

        result, payload = _run_proof(root, "gemini")

        assert result.returncode == 0
        assert payload["provider"] == "gemini"
        assert payload["verdict"] == "yellow"
        assert payload["failure_code"] == "provider_release_proof_not_implemented"
        assert {"source_artifact", "stdout", "stderr"} <= set(payload["artifacts"])


def main() -> int:
    tests = [
        test_opencode_release_proof_normalizes_source_canary,
        test_opencode_release_proof_blocks_on_source_canary_red,
        test_opencode_release_proof_blocks_when_source_artifact_missing,
        test_opencode_release_proof_blocks_when_source_canary_times_out,
        test_codex_release_proof_maps_provider_binary_and_keeps_source_review_honest,
        test_claude_release_proof_normalizes_no_token_contract_shape,
        test_claude_release_proof_red_when_session_flag_missing,
        test_gemini_release_proof_is_explicit_yellow_gap,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
