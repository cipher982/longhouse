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


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


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
    channels_missing = os.environ.get("FAKE_CLAUDE_CHANNELS_MISSING") == "1"
    pty_missing = os.environ.get("FAKE_CLAUDE_PTY_MISSING") == "1"
    verdict = "red" if (missing or channels_missing or pty_missing) else "yellow" if channels_unconfirmed else "green"
    failure_code = (
        "claude_command_contract_missing"
        if missing
        else "claude_development_channels_contract_missing"
        if channels_missing
        else "claude_detached_pty_unavailable"
        if pty_missing
        else None
    )
    artifact = {
        "artifact_kind": "provider_live_canary",
        "provider": "claude",
        "provider_version": "Claude Code 2.9.9",
        "verdict": verdict,
        "failure_code": failure_code,
        "recommendation": "block_upgrade_recommendation"
        if failure_code
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
                "status": "fail" if channels_missing else "warn" if channels_unconfirmed else "pass",
                "failure_code": "claude_development_channels_contract_missing"
                if channels_missing
                else None,
                "reason": "claude_development_channels_contract_unconfirmed"
                if channels_unconfirmed
                else None,
                "missing": ["--dangerously-load-development-channels"]
                if channels_missing
                else ["--resume"]
                if channels_unconfirmed
                else [],
            },
            "detached_pty_shape": {
                "status": "fail" if pty_missing else "pass",
                "failure_code": "claude_detached_pty_unavailable" if pty_missing else None,
                "platform": "darwin",
            },
        },
        "operation_evidence": {
            "launch_local": {
                "status": "fail" if failure_code else "pass",
                "level": "none" if failure_code else "live_no_token",
                "canary": "claude_launch_local_no_token",
                "failure_code": failure_code,
            }
        },
    }
    Path(value("--artifact")).parent.mkdir(parents=True, exist_ok=True)
    Path(value("--artifact")).write_text(json.dumps(artifact), encoding="utf-8")
    raise SystemExit(1 if verdict == "red" else 0)
if provider == "antigravity":
    artifact = {
        "artifact_kind": "provider_live_canary",
        "provider": "antigravity",
        "provider_version": "agy 1.0.3",
        "verdict": "green",
        "failure_code": None,
        "recommendation": "upgrade_allowed",
        "canaries": {
            "binary_identity": {"status": "pass", "version": "agy 1.0.3"},
            "command_shape": {"status": "pass"},
            "plugin_contract": {"status": "pass"},
            "global_hooks_contract": {"status": "pass"},
        },
        "operation_evidence": {
            "launch_local": {
                "status": "pass",
                "level": "live_no_token",
                "canary": "antigravity_launch_local_no_token",
            }
        },
    }
    Path(value("--artifact")).parent.mkdir(parents=True, exist_ok=True)
    Path(value("--artifact")).write_text(json.dumps(artifact), encoding="utf-8")
    raise SystemExit(0)

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
    "session_projection": {
        "artifact_kind": "provider_live_session_projection",
        "provider": provider,
        "status": "captured" if verdict == "green" else "partial",
        "provider_session_id": "ses_fake_release_proof",
        "operation_statuses": {
            "send_input": {
                "status": "pass" if verdict == "green" else "fail",
                "level": "live_no_token" if verdict == "green" else "none",
                "canary": "server_contract",
            }
        },
    },
}
Path(value("--artifact")).parent.mkdir(parents=True, exist_ok=True)
Path(value("--artifact")).write_text(json.dumps(artifact), encoding="utf-8")
raise SystemExit(1 if os.environ.get("FAKE_EXIT_ONE") == "1" else 0 if verdict != "red" else 1)
""",
    )

    _write_exe(
        root / "scripts" / "qa" / "provider-control-e2e-canary.py",
        r"""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]

def value(flag):
    return args[args.index(flag) + 1]

args_path = os.environ.get("FAKE_CONTROL_ARGS_PATH")
if args_path:
    Path(args_path).write_text(json.dumps(args), encoding="utf-8")

status = "fail" if os.environ.get("FAKE_ANTIGRAVITY_CONTROL_FAIL") == "1" else "pass"
failure_code = "fake_antigravity_send_failed" if status == "fail" else None
artifact = {
    "schema_version": 1,
    "provider": value("--provider"),
    "verdict": "red" if status == "fail" else "green",
    "failure_code": failure_code,
    "canaries": {
        "antigravity": {
            "status": status,
            "failure_code": failure_code,
            "operation_evidence": {
                "send_input": {
                    "status": status,
                    "level": "none" if status == "fail" else "live_token",
                    "source": "fake real agy send canary",
                    "canary": "antigravity_real_agy_send",
                    "failure_code": failure_code,
                }
            },
        }
    },
}
Path(value("--artifact")).parent.mkdir(parents=True, exist_ok=True)
Path(value("--artifact")).write_text(json.dumps(artifact), encoding="utf-8")
raise SystemExit(1 if status == "fail" else 0)
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
env_path = os.environ.get("FAKE_CODEX_ENV_PATH")
if env_path:
    Path(env_path).write_text(
        json.dumps(
            {
                "CODEX_AGENTS_TOKEN": os.environ.get("CODEX_AGENTS_TOKEN"),
                "CODEX_API_URL": os.environ.get("CODEX_API_URL"),
            }
        ),
        encoding="utf-8",
    )
if os.environ.get("FAKE_CODEX_SKIP_ARTIFACT") == "1":
    print(os.environ.get("CODEX_AGENTS_TOKEN", ""))
    raise SystemExit(0)

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
        assert payload["scenario_id"] == "opencode-release-proof-v1"
        assert payload["scenario_profile"] == "default"
        assert payload["verdict"] == "green"
        assert payload["failure_code"] is None
        assert payload["canaries"]["source_canary"]["status"] == "pass"
        assert payload["operation_evidence"]["send_input"]["status"] == "pass"
        assert payload["normalized"]["canaries"]["server_contract"]["status"] == "pass"
        assert Path(payload["artifacts"]["normalized_contract"]).exists()
        provider_contract = _read_json(Path(payload["artifacts"]["provider_contract"]))
        operation_evidence = _read_json(Path(payload["artifacts"]["operation_evidence"]))
        session_projection = _read_json(Path(payload["artifacts"]["session_projection"]))
        assert provider_contract["contract_operations"]["send_input"]["level"] == "live_no_token"
        assert operation_evidence["operation_evidence"]["send_input"]["status"] == "pass"
        assert session_projection["status"] == "captured"
        assert session_projection["projection"]["provider_session_id"] == "ses_fake_release_proof"


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


def test_opencode_release_proof_blocks_green_artifact_from_failed_source_canary() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "opencode",
            env={"FAKE_EXIT_ONE": "1"},
        )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "source_canary_returncode_mismatch"
        assert payload["source_canary_returncode"] == 1


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
        env_path = root / "codex-env.json"
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "codex",
            env={"FAKE_CODEX_ARGS_PATH": str(args_path), "FAKE_CODEX_ENV_PATH": str(env_path)},
            extra_args=[
                "--provider-version",
                "codex 2.0.0",
                "--codex-run-raw-fresh-remote",
                "--codex-run-managed-tui-attach",
                "--codex-run-detached-ui",
                "--codex-run-managed-live-send",
                "--codex-api-url",
                "http://longhouse.test",
                "--codex-agents-token",
                "secret-token",
            ],
        )

        assert result.returncode == 0
        codex_args = json.loads(args_path.read_text(encoding="utf-8"))
        codex_env = json.loads(env_path.read_text(encoding="utf-8"))
        assert codex_args[codex_args.index("--codex-bin") + 1] == str(root / "fake-provider")
        assert codex_args[codex_args.index("--source-review-status") + 1] == "not_run"
        assert "--run-raw-fresh-remote" in codex_args
        assert "--run-managed-tui-attach" in codex_args
        assert "--run-detached-ui" in codex_args
        assert "--run-managed-live-send" in codex_args
        assert codex_args[codex_args.index("--api-url") + 1] == "http://longhouse.test"
        assert "--agents-token" not in codex_args
        assert codex_env["CODEX_AGENTS_TOKEN"] == "secret-token"
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
        assert Path(payload["artifacts"]["provider_contract"]).exists()
        assert Path(payload["artifacts"]["operation_evidence"]).exists()
        assert Path(payload["artifacts"]["session_projection"]).exists()


def test_codex_release_proof_redacts_token_from_command_evidence() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "codex",
            env={"FAKE_CODEX_SKIP_ARTIFACT": "1"},
            extra_args=["--codex-agents-token", "secret-token"],
        )

        assert result.returncode == 1
        source = _read_json(Path(payload["artifacts"]["source_artifact"]))
        command = source["canaries"]["release_proof"]["command"]
        assert "secret-token" not in json.dumps(command)
        assert "<redacted>" in json.dumps(command)


def test_codex_managed_live_send_uses_distinct_scenario() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        args_path = root / "codex-args.json"
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "codex",
            env={"FAKE_CODEX_ARGS_PATH": str(args_path)},
            extra_args=[
                "--source-review-status",
                "pass",
                "--codex-run-managed-live-send",
            ],
        )

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["scenario_id"] == "codex-managed-live-send-release-proof-v1"
        assert payload["scenario_profile"] == "managed-live-send"
        source_args = json.loads(args_path.read_text(encoding="utf-8"))
        assert "--run-managed-live-send" in source_args


def test_codex_managed_live_send_preflight_reports_missing_credentials() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "codex",
            env={"CODEX_API_URL": "", "CODEX_AGENTS_TOKEN": ""},
            extra_args=[
                "--preflight-only",
                "--codex-run-managed-live-send",
            ],
        )

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["artifact_kind"] == "provider_release_proof_preflight"
        assert payload["scenario_id"] == "codex-managed-live-send-release-proof-v1"
        assert payload["scenario_profile"] == "managed-live-send"
        assert payload["verdict"] == "yellow"
        assert payload["failure_code"] == "provider_release_proof_prerequisites_missing"
        checks = {check["name"]: check for check in payload["checks"]}
        assert checks["provider_binary"]["status"] == "pass"
        assert checks["codex_api_url"]["failure_code"] == "codex_runtime_host_api_url_missing"
        assert checks["codex_agents_token"]["failure_code"] == "codex_runtime_host_agents_token_missing"


def test_codex_managed_live_send_preflight_redacts_credentials() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "codex",
            extra_args=[
                "--preflight-only",
                "--codex-run-managed-live-send",
                "--codex-api-url",
                "http://longhouse.test",
                "--codex-agents-token",
                "secret-token",
            ],
        )

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["verdict"] == "green"
        assert "secret-token" not in json.dumps(payload)
        assert {check["status"] for check in payload["checks"]} == {"pass"}


def test_preflight_reports_missing_provider_binary_as_red() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")

        result, payload = _run_proof(
            root,
            "opencode",
            extra_args=["--preflight-only"],
        )

        assert result.returncode == 1
        assert payload["artifact_kind"] == "provider_release_proof_preflight"
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "provider_binary_not_found"


def test_explicit_scenario_id_overrides_profile_default() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "codex",
            extra_args=[
                "--source-review-status",
                "pass",
                "--codex-run-managed-live-send",
                "--scenario-id",
                "codex-custom-live-proof-v1",
            ],
        )

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["scenario_id"] == "codex-custom-live-proof-v1"
        assert payload["scenario_profile"] == "managed-live-send"


def test_antigravity_release_proof_can_attach_real_agy_send_evidence() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        args_path = root / "control-args.json"
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "antigravity",
            env={"FAKE_CONTROL_ARGS_PATH": str(args_path)},
            extra_args=[
                "--antigravity-run-real-agy-send",
                "--antigravity-print-timeout-secs",
                "5",
            ],
        )

        assert result.returncode == 0, result.stderr + result.stdout
        control_args = json.loads(args_path.read_text(encoding="utf-8"))
        assert "--antigravity-real-agy-send" in control_args
        assert payload["provider"] == "antigravity"
        assert payload["scenario_id"] == "antigravity-real-agy-send-release-proof-v1"
        assert payload["scenario_profile"] == "real-agy-send"
        assert payload["verdict"] == "green"
        assert payload["operation_evidence"]["send_input"]["status"] == "pass"
        assert payload["operation_evidence"]["send_input"]["level"] == "live_token"
        assert payload["normalized"]["operation_evidence"]["send_input"]["level"] == "live_token"
        assert payload["normalized"]["canaries"]["antigravity_real_agy_send"]["status"] == "pass"
        assert Path(payload["artifacts"]["antigravity_control_artifact"]).exists()


def test_antigravity_release_proof_blocks_failed_real_agy_send_evidence() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "antigravity",
            env={"FAKE_ANTIGRAVITY_CONTROL_FAIL": "1"},
            extra_args=["--antigravity-run-real-agy-send"],
        )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "fake_antigravity_send_failed"
        assert payload["operation_evidence"]["send_input"]["status"] == "fail"
        assert payload["normalized"]["canaries"]["antigravity_real_agy_send"]["failure_code"] == (
            "fake_antigravity_send_failed"
        )


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
            "launch_flags_failure_code": None,
            "development_channels_status": "warn",
            "development_channels_missing": ["--resume"],
            "development_channels_failure_code": None,
            "development_channels_reason": "claude_development_channels_contract_unconfirmed",
            "detached_pty_status": "pass",
            "detached_pty_failure_code": None,
            "detached_pty_reason": None,
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
        assert payload["normalized"]["claude"]["launch_flags_failure_code"] == (
            "claude_command_contract_missing"
        )
        assert payload["operation_evidence"]["launch_local"]["failure_code"] == (
            "claude_command_contract_missing"
        )


def test_claude_release_proof_preserves_development_channel_failure_code() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "claude",
            env={"FAKE_CLAUDE_CHANNELS_MISSING": "1"},
        )

        assert result.returncode == 1
        assert payload["failure_code"] == "claude_development_channels_contract_missing"
        assert payload["normalized"]["claude"]["development_channels_status"] == "fail"
        assert payload["normalized"]["claude"]["development_channels_failure_code"] == (
            "claude_development_channels_contract_missing"
        )
        assert payload["normalized"]["claude"]["development_channels_missing"] == [
            "--dangerously-load-development-channels"
        ]


def test_claude_release_proof_preserves_detached_pty_failure_context() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "claude",
            env={"FAKE_CLAUDE_PTY_MISSING": "1"},
        )

        assert result.returncode == 1
        assert payload["failure_code"] == "claude_detached_pty_unavailable"
        assert payload["normalized"]["claude"]["detached_pty_status"] == "fail"
        assert payload["normalized"]["claude"]["detached_pty_failure_code"] == (
            "claude_detached_pty_unavailable"
        )
        assert payload["normalized"]["claude"]["detached_pty_platform"] == "darwin"


def main() -> int:
    tests = [
        test_opencode_release_proof_normalizes_source_canary,
        test_opencode_release_proof_blocks_on_source_canary_red,
        test_opencode_release_proof_blocks_green_artifact_from_failed_source_canary,
        test_opencode_release_proof_blocks_when_source_artifact_missing,
        test_opencode_release_proof_blocks_when_source_canary_times_out,
        test_codex_release_proof_maps_provider_binary_and_keeps_source_review_honest,
        test_codex_release_proof_redacts_token_from_command_evidence,
        test_codex_managed_live_send_uses_distinct_scenario,
        test_codex_managed_live_send_preflight_reports_missing_credentials,
        test_codex_managed_live_send_preflight_redacts_credentials,
        test_preflight_reports_missing_provider_binary_as_red,
        test_explicit_scenario_id_overrides_profile_default,
        test_antigravity_release_proof_can_attach_real_agy_send_evidence,
        test_antigravity_release_proof_blocks_failed_real_agy_send_evidence,
        test_claude_release_proof_normalizes_no_token_contract_shape,
        test_claude_release_proof_red_when_session_flag_missing,
        test_claude_release_proof_preserves_development_channel_failure_code,
        test_claude_release_proof_preserves_detached_pty_failure_context,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
