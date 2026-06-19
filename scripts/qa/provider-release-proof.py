#!/usr/bin/env python3
"""Normalize Longhouse provider release proof into one artifact.

This is the Longhouse-owned entrypoint Sauron should call for upstream provider
release checks. Provider-specific canaries own behavior; this wrapper owns the
release-proof artifact shape.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SUPPORTED_PROVIDERS = ("claude", "codex", "opencode", "antigravity")
LIVE_CANARY_PROVIDERS = frozenset({"claude", "opencode", "antigravity"})
CODEX_API_URL_ENV = "CODEX_API_URL"
CODEX_AGENTS_TOKEN_ENV = "CODEX_AGENTS_TOKEN"
CLAUDE_API_URL_ENV = "CLAUDE_API_URL"
CLAUDE_AGENTS_TOKEN_ENV = "CLAUDE_AGENTS_TOKEN"
CLAUDE_DEVICE_ID_ENV = "CLAUDE_DEVICE_ID"
CLAUDE_BIN_ENV = "LONGHOUSE_CLAUDE_BIN"
OPENCODE_BIN_ENV = "LONGHOUSE_OPENCODE_BIN"
ANTIGRAVITY_BIN_ENV = "LONGHOUSE_ANTIGRAVITY_BIN"
DEFAULT_OPERATION_POLL_INTERVAL_S = 2.0
RETRYABLE_STATUS_CODES = {0, 408, 429, 500, 502, 503, 504}
CLAUDE_MACHINE_LIVE_REQUIRED_OPERATIONS = (
    "send_input",
    "transcript_binding",
    "steer_active_turn",
)
DEFAULT_UNIVERSAL_SCENARIOS = (
    "probe_identity",
    "collect_raw_evidence",
    "action_matrix",
    "control_surface",
    "session_projection",
    "timeline_projection",
    "run_prompt_once",
    "launch_managed_session",
    "send_receive",
)
UNIVERSAL_SCENARIOS = (
    "probe_identity",
    "collect_raw_evidence",
    "action_matrix",
    "control_surface",
    "parse_ingest_project",
    "db_ingest_project",
    "session_projection",
    "timeline_projection",
    "run_prompt_once",
    "launch_managed_session",
    "send_receive",
    "managed_session_e2e",
    "interrupt_cancel",
    "tool_call_result",
    "resume_reattach",
    "live_token_streaming",
    "old_new_release_diff",
)
UNIVERSAL_YELLOW_STATUSES = {
    "unsupported_gap",
    "not_applicable",
    "blocked",
    "flaky",
    "xfail_with_expiry",
}


def _repo_root_from_script() -> Path:
    return Path(__file__).resolve().parents[2]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} did not contain a JSON object")
    return payload


def _load_provider_contract(repo_root: Path, provider: str) -> dict[str, Any] | None:
    path = repo_root / "server" / "zerg" / "config" / "managed_provider_contracts.json"
    try:
        payload = _read_json(path)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    for item in payload.get("providers") or []:
        if isinstance(item, dict) and item.get("provider") == provider:
            return dict(item)
    return None


def _compact_status(info: dict[str, Any]) -> dict[str, Any]:
    return {
        key: info.get(key)
        for key in ("status", "failure_code")
        if info.get(key) is not None
    }


def _compact_protocol_fingerprints(info: dict[str, Any]) -> dict[str, Any] | None:
    fingerprints = info.get("protocol_fingerprints")
    if not isinstance(fingerprints, dict):
        return None
    return {
        key: fingerprints.get(key)
        for key in (
            "status",
            "responses",
            "notifications",
            "server_requests",
            "response_errors",
        )
        if key in fingerprints
    }


def _compact_codex_canary(info: dict[str, Any]) -> dict[str, Any]:
    compact = _compact_status(info)
    for key in (
        "reason",
        "version",
        "command_status",
        "command_exit_code",
        "command_exact_match",
        "output_exact_match",
        "command_event_count",
        "agent_message_count",
        "last_turn_status",
    ):
        if info.get(key) is not None:
            compact[key] = info.get(key)
    protocol_fingerprints = _compact_protocol_fingerprints(info)
    if protocol_fingerprints is not None:
        compact["protocol_fingerprints"] = protocol_fingerprints
    return compact


def _compact_claude_canary(info: dict[str, Any]) -> dict[str, Any]:
    compact = _compact_status(info)
    for key in (
        "missing",
        "reason",
        "platform",
        "verdict",
        "device_id",
        "command_id",
        "operation_id",
        "message",
    ):
        if info.get(key) is not None:
            compact[key] = info.get(key)
    return compact


def _compact_operation(info: dict[str, Any]) -> dict[str, Any]:
    return {
        key: info.get(key)
        for key in ("status", "level", "canary", "failure_code")
        if info.get(key) is not None
    }


def _normalize_source_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    provider = artifact.get("provider")
    if provider == "codex":
        canary_compactor = _compact_codex_canary
    elif provider == "claude":
        canary_compactor = _compact_claude_canary
    else:
        canary_compactor = _compact_status
    normalized: dict[str, Any] = {
        "artifact_kind": artifact.get("artifact_kind"),
        "provider": provider,
        "provider_version": artifact.get("provider_version")
        or artifact.get("codex_version"),
        "verdict": artifact.get("verdict"),
        "failure_code": artifact.get("failure_code"),
        "canaries": {
            name: canary_compactor(info)
            for name, info in sorted(dict(artifact.get("canaries") or {}).items())
            if isinstance(info, dict)
        },
        "operation_evidence": {
            name: _compact_operation(info)
            for name, info in sorted(
                dict(artifact.get("operation_evidence") or {}).items()
            )
            if isinstance(info, dict)
        },
    }
    if artifact.get("provider") == "codex":
        source_review = artifact.get("source_review")
        normalized["source_review"] = {
            "status": source_review.get("status")
            if isinstance(source_review, dict)
            else None
        }
        normalized["codex"] = {
            "binary_present": bool(artifact.get("codex_bin")),
            "longhouse_commit_present": bool(artifact.get("longhouse_commit")),
        }
    if provider == "claude":
        canaries = dict(artifact.get("canaries") or {})
        command_shape = (
            canaries.get("command_shape")
            if isinstance(canaries.get("command_shape"), dict)
            else {}
        )
        channels_shape = (
            canaries.get("channels_shape")
            if isinstance(canaries.get("channels_shape"), dict)
            else {}
        )
        detached_pty_shape = (
            canaries.get("detached_pty_shape")
            if isinstance(canaries.get("detached_pty_shape"), dict)
            else {}
        )
        normalized["claude"] = {
            "launch_flags_missing": list(command_shape.get("missing") or []),
            "launch_flags_failure_code": command_shape.get("failure_code"),
            "development_channels_status": channels_shape.get("status"),
            "development_channels_missing": list(channels_shape.get("missing") or []),
            "development_channels_failure_code": channels_shape.get("failure_code"),
            "development_channels_reason": channels_shape.get("reason"),
            "detached_pty_status": detached_pty_shape.get("status"),
            "detached_pty_failure_code": detached_pty_shape.get("failure_code"),
            "detached_pty_reason": detached_pty_shape.get("reason"),
            "detached_pty_platform": detached_pty_shape.get("platform"),
        }
    universal = artifact.get("universal_harness")
    if isinstance(universal, dict):
        normalized["universal_harness"] = {
            key: universal.get(key)
            for key in (
                "artifact_kind",
                "verdict",
                "providers",
                "scenarios",
                "result_count",
                "failure_count",
                "unsupported_gap_count",
            )
            if universal.get(key) is not None
        }
    action_matrix = artifact.get("action_matrix")
    if isinstance(action_matrix, dict):
        normalized["action_matrix"] = {
            key: action_matrix.get(key)
            for key in (
                "artifact_kind",
                "provider",
                "action_count",
                "status_counts",
                "action_matrix_path",
                "raw_inputs_path",
            )
            if action_matrix.get(key) is not None
        }
    control_surface = artifact.get("control_surface")
    if isinstance(control_surface, dict):
        normalized["control_surface"] = {
            key: control_surface.get(key)
            for key in (
                "artifact_kind",
                "provider",
                "action_count",
                "status_counts",
                "control_surface_path",
                "raw_inputs_path",
            )
            if control_surface.get(key) is not None
        }
    return normalized


def _session_projection_artifact(
    *,
    provider: str,
    provider_version: str | None,
    source_artifact: dict[str, Any],
) -> dict[str, Any]:
    projection = source_artifact.get("session_projection")
    if isinstance(projection, dict):
        return {
            "artifact_kind": "provider_release_proof_session_projection",
            "provider": provider,
            "provider_version": provider_version,
            "status": "captured",
            "projection": projection,
        }
    return {
        "artifact_kind": "provider_release_proof_session_projection",
        "provider": provider,
        "provider_version": provider_version,
        "status": "not_captured",
        "reason": "source canary did not emit a normalized session projection artifact",
    }


def _classify(
    source_artifact: dict[str, Any],
    *,
    source_canary_returncode: int | None = None,
) -> tuple[str, str | None, str]:
    verdict = str(source_artifact.get("verdict") or "").lower()
    failure_code = source_artifact.get("failure_code")
    recommendation = source_artifact.get("recommendation")
    if source_canary_returncode not in (None, 0) and verdict == "green":
        return (
            "red",
            "source_canary_returncode_mismatch",
            "block_upgrade_recommendation",
        )
    if verdict == "red":
        return (
            "red",
            str(failure_code or "provider_release_proof_failed"),
            str(recommendation or "block_upgrade_recommendation"),
        )
    if verdict == "yellow":
        return (
            "yellow",
            str(failure_code or "insufficient_coverage"),
            str(recommendation or "investigate_before_upgrade"),
        )
    if verdict == "green":
        return "green", None, str(recommendation or "upgrade_allowed")
    return (
        "yellow",
        "provider_release_proof_unknown_verdict",
        "investigate_before_upgrade",
    )


def _redact_argv(argv: Any, secrets: list[str] | None = None) -> Any:
    if not isinstance(argv, list):
        return argv
    secrets = [secret for secret in (secrets or []) if secret]
    redacted: list[Any] = []
    redact_next = False
    for item in argv:
        if redact_next:
            redacted.append("<redacted>")
            redact_next = False
            continue
        redacted.append("<redacted>" if item in secrets else item)
        if item in {"--agents-token", "--codex-agents-token", "--claude-agents-token"}:
            redact_next = True
    return redacted


def _redact_text(text: str, secrets: list[str] | None = None) -> str:
    redacted = text
    for secret in secrets or []:
        if secret:
            redacted = redacted.replace(secret, "<redacted>")
    return redacted


def _command_evidence(
    result: subprocess.CompletedProcess[str],
    *,
    secrets: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "argv": _redact_argv(result.args, secrets),
        "returncode": result.returncode,
        "stdout": _redact_text((result.stdout or "")[-4000:], secrets),
        "stderr": _redact_text((result.stderr or "")[-4000:], secrets),
    }


def _scenario_profile(args: argparse.Namespace) -> str:
    if args.provider == "codex" and args.codex_run_real_tool:
        return "real-tool"
    if args.provider == "codex" and args.codex_run_managed_live_interrupt:
        return "managed-live-interrupt"
    if args.provider == "codex" and args.codex_run_managed_live_send:
        return "managed-live-send"
    if args.provider == "claude" and args.claude_run_machine_live_proof:
        return "machine-live"
    if args.provider == "claude" and args.claude_run_real_print:
        return "real-print"
    if args.provider == "opencode" and args.opencode_run_real_tool:
        return "real-tool"
    if args.provider == "antigravity" and args.antigravity_run_real_agy_send:
        return "real-agy-send"
    return "default"


def _scenario_id(args: argparse.Namespace) -> str:
    if args.scenario_id:
        return args.scenario_id
    profile = _scenario_profile(args)
    if profile == "default":
        return f"{args.provider}-release-proof-v1"
    return f"{args.provider}-{profile}-release-proof-v1"


def _preflight_check(
    name: str,
    ok: bool,
    *,
    failure_code: str | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": name, "status": "pass" if ok else "fail"}
    if not ok and failure_code:
        payload["failure_code"] = failure_code
    if not ok and message:
        payload["message"] = message
    return payload


def _proof_preflight(args: argparse.Namespace) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    if args.provider_bin is not None:
        checks.append(
            _preflight_check(
                "provider_binary",
                args.provider_bin.exists(),
                failure_code="provider_binary_not_found",
                message=f"Provider binary not found: {args.provider_bin}",
            )
        )
    if args.provider == "codex" and (
        args.codex_run_managed_tui_attach
        or args.codex_run_detached_ui
        or args.codex_run_managed_live_send
        or args.codex_run_managed_live_interrupt
    ):
        checks.append(
            _preflight_check(
                "codex_api_url",
                bool(args.codex_api_url),
                failure_code="codex_runtime_host_api_url_missing",
                message="Set CODEX_API_URL or pass --codex-api-url.",
            )
        )
        checks.append(
            _preflight_check(
                "codex_agents_token",
                bool(args.codex_agents_token),
                failure_code="codex_runtime_host_agents_token_missing",
                message="Set CODEX_AGENTS_TOKEN or pass --codex-agents-token.",
            )
        )
    if args.provider == "claude" and args.claude_run_machine_live_proof:
        checks.append(
            _preflight_check(
                "claude_api_url",
                bool(args.claude_api_url),
                failure_code="claude_runtime_host_api_url_missing",
                message="Set CLAUDE_API_URL or pass --claude-api-url.",
            )
        )
        checks.append(
            _preflight_check(
                "claude_agents_token",
                bool(args.claude_agents_token),
                failure_code="claude_runtime_host_agents_token_missing",
                message="Set CLAUDE_AGENTS_TOKEN or pass --claude-agents-token.",
            )
        )
        checks.append(
            _preflight_check(
                "claude_device_id",
                bool(args.claude_device_id),
                failure_code="claude_runtime_host_device_id_missing",
                message="Set CLAUDE_DEVICE_ID or pass --claude-device-id.",
            )
        )
    failed = [check for check in checks if check.get("status") == "fail"]
    red_codes = {"provider_binary_not_found"}
    verdict = "green"
    failure_code = None
    recommendation = "upgrade_allowed"
    if failed:
        first_code = str(
            failed[0].get("failure_code") or "provider_release_proof_preflight_failed"
        )
        verdict = "red" if first_code in red_codes else "yellow"
        failure_code = (
            first_code
            if verdict == "red"
            else "provider_release_proof_prerequisites_missing"
        )
        recommendation = (
            "block_upgrade_recommendation"
            if verdict == "red"
            else "investigate_before_upgrade"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "provider_release_proof_preflight",
        "provider": args.provider,
        "provider_version": args.provider_version,
        "generated_at": _now_iso(),
        "scenario_id": _scenario_id(args),
        "scenario_profile": _scenario_profile(args),
        "verdict": verdict,
        "failure_code": failure_code,
        "recommendation": recommendation,
        "checks": checks,
    }


def _run_source_canary(
    args: argparse.Namespace, raw_dir: Path
) -> tuple[dict[str, Any], dict[str, str], int | None]:
    raw_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = raw_dir / "stdout.log"
    stderr_path = raw_dir / "stderr.log"

    if args.provider in LIVE_CANARY_PROVIDERS:
        source_path = raw_dir / "provider-live-canary.json"
        argv = [
            sys.executable,
            str(args.repo_root / "scripts" / "qa" / "provider-live-canary.py"),
            "--provider",
            args.provider,
            "--artifact",
            str(source_path),
            "--evidence-root",
            str(raw_dir / "provider-live-evidence"),
            "--json",
        ]
        if args.provider_bin:
            argv.extend(["--provider-bin", str(args.provider_bin)])
    elif args.provider == "codex":
        source_path = raw_dir / "codex-provider-release-canary.json"
        argv = [
            sys.executable,
            str(args.repo_root / "scripts" / "qa" / "codex-provider-release-canary.py"),
            "--repo-root",
            str(args.repo_root),
            "--artifact",
            str(source_path),
            "--evidence-root",
            str(raw_dir / "codex-provider-release-evidence"),
            "--source-review-status",
            args.source_review_status,
            "--source-review-note",
            args.source_review_note,
            "--json",
        ]
        if args.provider_bin:
            argv.extend(["--codex-bin", str(args.provider_bin)])
        if args.provider_version:
            argv.extend(["--provider-version", str(args.provider_version)])
        if args.codex_api_url:
            argv.extend(["--api-url", args.codex_api_url])
        if args.codex_run_fake_app_server:
            argv.append("--run-fake-app-server")
        if args.codex_run_raw_fresh_remote:
            argv.append("--run-raw-fresh-remote")
        if args.codex_run_managed_tui_attach:
            argv.append("--run-managed-tui-attach")
        if args.codex_run_detached_ui:
            argv.append("--run-detached-ui")
        if args.codex_run_managed_live_send:
            argv.append("--run-managed-live-send")
        if args.codex_run_managed_live_interrupt:
            argv.extend(
                [
                    "--run-managed-live-interrupt",
                    "--live-interrupt-timeout-secs",
                    str(args.codex_live_interrupt_timeout_secs),
                ]
            )
        if args.codex_run_real_tool:
            argv.extend(
                [
                    "--run-real-tool",
                    "--real-tool-timeout-secs",
                    str(args.codex_real_tool_timeout_secs),
                ]
            )
    else:
        raise ValueError(f"unsupported provider: {args.provider}")

    if source_path.exists():
        source_path.unlink()

    raw_artifacts = {
        "source_artifact": str(source_path),
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
    }
    secrets = [args.codex_agents_token] if args.provider == "codex" else []
    run_env = None
    if args.provider == "codex" and args.codex_agents_token:
        run_env = os.environ.copy()
        run_env[CODEX_AGENTS_TOKEN_ENV] = args.codex_agents_token
    try:
        result = subprocess.run(
            argv,
            cwd=str(args.repo_root),
            env=run_env,
            text=True,
            capture_output=True,
            check=False,
            timeout=args.timeout_secs,
        )
    except subprocess.TimeoutExpired as exc:
        stdout_path.write_text(str(exc.stdout or ""), encoding="utf-8")
        stderr_path.write_text(str(exc.stderr or ""), encoding="utf-8")
        source = {
            "artifact_kind": "provider_release_proof_source",
            "provider": args.provider,
            "provider_version": args.provider_version,
            "verdict": "red",
            "failure_code": "provider_release_proof_timeout",
            "recommendation": "block_upgrade_recommendation",
            "canaries": {
                "release_proof": {
                    "status": "fail",
                    "failure_code": "provider_release_proof_timeout",
                    "message": f"source canary timed out after {args.timeout_secs}s",
                }
            },
            "operation_evidence": {},
        }
        _write_json(source_path, source)
        return source, raw_artifacts, None
    except OSError as exc:
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text(str(exc), encoding="utf-8")
        source = {
            "artifact_kind": "provider_release_proof_source",
            "provider": args.provider,
            "provider_version": args.provider_version,
            "verdict": "red",
            "failure_code": "provider_release_proof_source_exec_failed",
            "recommendation": "block_upgrade_recommendation",
            "canaries": {
                "release_proof": {
                    "status": "fail",
                    "failure_code": "provider_release_proof_source_exec_failed",
                    "message": f"{type(exc).__name__}: {exc}",
                }
            },
            "operation_evidence": {},
        }
        _write_json(source_path, source)
        return source, raw_artifacts, None

    stdout_path.write_text(result.stdout, encoding="utf-8")
    stderr_path.write_text(result.stderr, encoding="utf-8")
    if source_path.exists():
        try:
            source = _read_json(source_path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            source = {
                "artifact_kind": "provider_release_proof_source",
                "provider": args.provider,
                "provider_version": args.provider_version,
                "verdict": "red",
                "failure_code": "provider_release_proof_source_invalid_json",
                "recommendation": "block_upgrade_recommendation",
                "canaries": {
                    "release_proof": {
                        "status": "fail",
                        "failure_code": "provider_release_proof_source_invalid_json",
                        "message": f"{type(exc).__name__}: {exc}",
                        "command": _command_evidence(result, secrets=secrets),
                    }
                },
                "operation_evidence": {},
            }
            _write_json(source_path, source)
    else:
        source = {
            "artifact_kind": "provider_release_proof_source",
            "provider": args.provider,
            "provider_version": args.provider_version,
            "verdict": "red",
            "failure_code": "provider_release_proof_source_missing",
            "recommendation": "block_upgrade_recommendation",
            "canaries": {
                "release_proof": {
                    "status": "fail",
                    "failure_code": "provider_release_proof_source_missing",
                    "command": _command_evidence(result, secrets=secrets),
                }
            },
            "operation_evidence": {},
        }
        _write_json(source_path, source)
    return source, raw_artifacts, result.returncode


def _universal_scenarios(args: argparse.Namespace) -> tuple[str, ...]:
    scenarios = list(args.universal_scenario or DEFAULT_UNIVERSAL_SCENARIOS)
    if args.universal_fixture_path and "parse_ingest_project" not in scenarios:
        scenarios.append("parse_ingest_project")
    if (
        args.universal_old_proof_artifact
        and args.universal_new_proof_artifact
        and "old_new_release_diff" not in scenarios
    ):
        scenarios.append("old_new_release_diff")
    return tuple(dict.fromkeys(scenarios))


def _run_universal_harness(
    args: argparse.Namespace, raw_dir: Path
) -> tuple[dict[str, Any], dict[str, str], int | None]:
    evidence_root = args.evidence_root / "universal-agent-harness"
    stdout_path = raw_dir / "universal-agent-harness-stdout.log"
    stderr_path = raw_dir / "universal-agent-harness-stderr.log"
    artifact_path = evidence_root / "universal-agent-harness.json"
    scenarios = _universal_scenarios(args)
    harness_script = Path(__file__).resolve().with_name("universal-agent-harness.py")
    if any(
        scenario
        in {
            "db_ingest_project",
            "managed_session_e2e",
            "interrupt_cancel",
            "tool_call_result",
            "resume_reattach",
            "live_token_streaming",
        }
        for scenario in scenarios
    ):
        harness_project = _repo_root_from_script() / "server"
        argv = [
            "uv",
            "run",
            "--project",
            str(harness_project),
            "python",
            str(harness_script),
            "--provider",
            args.provider,
            "--evidence-root",
            str(evidence_root),
            "--json",
        ]
    else:
        argv = [
            sys.executable,
            str(harness_script),
            "--provider",
            args.provider,
            "--evidence-root",
            str(evidence_root),
            "--json",
        ]
    for scenario in scenarios:
        argv.extend(["--scenario", scenario])
    if args.provider_bin:
        argv.extend(["--provider-bin", str(args.provider_bin)])
    if args.universal_fixture_path:
        argv.extend(["--fixture-path", str(args.universal_fixture_path)])
    if args.universal_prompt:
        argv.extend(["--prompt", args.universal_prompt])
    if args.universal_old_proof_artifact:
        argv.extend(["--old-proof-artifact", str(args.universal_old_proof_artifact)])
    if args.universal_new_proof_artifact:
        argv.extend(["--new-proof-artifact", str(args.universal_new_proof_artifact)])
    if args.universal_baseline_root:
        argv.extend(["--baseline-root", str(args.universal_baseline_root)])

    raw_artifacts = {
        "universal_harness_artifact": str(artifact_path),
        "universal_harness_stdout": str(stdout_path),
        "universal_harness_stderr": str(stderr_path),
        "universal_harness_evidence_root": str(evidence_root),
    }
    try:
        result = subprocess.run(
            argv,
            cwd=str(args.repo_root),
            text=True,
            capture_output=True,
            check=False,
            timeout=args.timeout_secs,
        )
    except subprocess.TimeoutExpired as exc:
        stdout_path.write_text(str(exc.stdout or ""), encoding="utf-8")
        stderr_path.write_text(str(exc.stderr or ""), encoding="utf-8")
        artifact = {
            "schema_version": SCHEMA_VERSION,
            "artifact_kind": "universal_agent_harness_run",
            "providers": [args.provider],
            "scenarios": list(scenarios),
            "evidence_root": str(evidence_root),
            "verdict": "red",
            "failure_code": "universal_harness_timeout",
            "results": [
                {
                    "provider": args.provider,
                    "scenario": "universal_harness",
                    "status": "fail",
                    "failure_code": "universal_harness_timeout",
                    "message": f"universal harness timed out after {args.timeout_secs}s",
                    "evidence_root": str(evidence_root),
                }
            ],
        }
        _write_json(artifact_path, artifact)
        return artifact, raw_artifacts, None
    except OSError as exc:
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text(str(exc), encoding="utf-8")
        artifact = {
            "schema_version": SCHEMA_VERSION,
            "artifact_kind": "universal_agent_harness_run",
            "providers": [args.provider],
            "scenarios": list(scenarios),
            "evidence_root": str(evidence_root),
            "verdict": "red",
            "failure_code": "universal_harness_exec_failed",
            "results": [
                {
                    "provider": args.provider,
                    "scenario": "universal_harness",
                    "status": "fail",
                    "failure_code": "universal_harness_exec_failed",
                    "message": f"{type(exc).__name__}: {exc}",
                    "evidence_root": str(evidence_root),
                }
            ],
        }
        _write_json(artifact_path, artifact)
        return artifact, raw_artifacts, None

    stdout_path.write_text(result.stdout, encoding="utf-8")
    stderr_path.write_text(result.stderr, encoding="utf-8")
    if artifact_path.exists():
        try:
            artifact = _read_json(artifact_path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            artifact = {
                "schema_version": SCHEMA_VERSION,
                "artifact_kind": "universal_agent_harness_run",
                "providers": [args.provider],
                "scenarios": list(scenarios),
                "evidence_root": str(evidence_root),
                "verdict": "red",
                "failure_code": "universal_harness_invalid_json",
                "results": [
                    {
                        "provider": args.provider,
                        "scenario": "universal_harness",
                        "status": "fail",
                        "failure_code": "universal_harness_invalid_json",
                        "message": f"{type(exc).__name__}: {exc}",
                        "evidence_root": str(evidence_root),
                    }
                ],
            }
            _write_json(artifact_path, artifact)
    else:
        artifact = {
            "schema_version": SCHEMA_VERSION,
            "artifact_kind": "universal_agent_harness_run",
            "providers": [args.provider],
            "scenarios": list(scenarios),
            "evidence_root": str(evidence_root),
            "verdict": "red",
            "failure_code": "universal_harness_missing_artifact",
            "results": [
                {
                    "provider": args.provider,
                    "scenario": "universal_harness",
                    "status": "fail",
                    "failure_code": "universal_harness_missing_artifact",
                    "evidence_root": str(evidence_root),
                    "command": _command_evidence(result),
                }
            ],
        }
        _write_json(artifact_path, artifact)
    return artifact, raw_artifacts, result.returncode


def _universal_result_status(status: str) -> str:
    if status == "pass":
        return "pass"
    if status in UNIVERSAL_YELLOW_STATUSES:
        return "warn"
    return "fail"


def _read_universal_projection(result: dict[str, Any]) -> dict[str, Any] | None:
    data = result.get("data")
    if not isinstance(data, dict):
        return None
    raw_path = data.get("session_projection_path")
    if not raw_path:
        return None
    try:
        return _read_json(Path(str(raw_path)))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _merge_universal_harness(
    source: dict[str, Any], universal: dict[str, Any]
) -> dict[str, Any]:
    merged = dict(source)
    results = [
        item for item in universal.get("results") or [] if isinstance(item, dict)
    ]
    provider_results = [
        item for item in results if item.get("provider") == source.get("provider")
    ]
    failure_count = sum(1 for item in provider_results if item.get("status") == "fail")
    unsupported_gap_count = sum(
        1
        for item in provider_results
        if item.get("status") in UNIVERSAL_YELLOW_STATUSES
    )
    universal_summary = {
        "artifact_kind": universal.get("artifact_kind"),
        "verdict": universal.get("verdict"),
        "providers": list(universal.get("providers") or []),
        "scenarios": list(universal.get("scenarios") or []),
        "result_count": len(provider_results),
        "failure_count": failure_count,
        "unsupported_gap_count": unsupported_gap_count,
    }
    canaries = dict(merged.get("canaries") or {})
    canaries["universal_agent_harness"] = {
        "status": "fail"
        if universal.get("verdict") == "red"
        else "warn"
        if universal.get("verdict") == "yellow"
        else "pass",
        **universal_summary,
    }
    operation_evidence = dict(merged.get("operation_evidence") or {})
    universal_projection: dict[str, Any] | None = None
    action_matrix_summary: dict[str, Any] | None = None
    control_surface_summary: dict[str, Any] | None = None
    for result in provider_results:
        scenario = str(result.get("scenario") or "unknown")
        status = str(result.get("status") or "fail")
        canaries[f"universal_{scenario}"] = {
            "status": _universal_result_status(status),
            "failure_code": result.get("failure_code"),
            "message": result.get("message"),
            "evidence_root": result.get("evidence_root"),
        }
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        for operation, evidence in dict(data.get("operation_evidence") or {}).items():
            if not isinstance(evidence, dict):
                continue
            key = f"universal_{operation}"
            operation_evidence[key] = {
                "status": evidence.get("status") or status,
                "level": evidence.get("level"),
                "canary": evidence.get("canary") or f"universal_{scenario}",
                "failure_code": evidence.get("failure_code"),
            }
        if scenario == "action_matrix" and isinstance(data.get("actions"), list):
            action_matrix_summary = {
                "artifact_kind": "provider_release_proof_action_matrix",
                "provider": source.get("provider"),
                "action_count": data.get("action_count"),
                "action_ids": list(data.get("action_ids") or []),
                "status_counts": dict(data.get("status_counts") or {}),
                "action_matrix_path": data.get("action_matrix_path"),
                "raw_inputs_path": data.get("raw_inputs_path"),
                "actions": list(data.get("actions") or []),
            }
        if scenario == "control_surface" and isinstance(data.get("actions"), list):
            control_surface_summary = {
                "artifact_kind": "provider_release_proof_control_surface",
                "provider": source.get("provider"),
                "action_count": data.get("action_count"),
                "action_ids": list(data.get("action_ids") or []),
                "status_counts": dict(data.get("status_counts") or {}),
                "control_surface_path": data.get("control_surface_path"),
                "raw_inputs_path": data.get("raw_inputs_path"),
                "actions": list(data.get("actions") or []),
            }
        if universal_projection is None:
            universal_projection = _read_universal_projection(result)
    merged["canaries"] = canaries
    merged["operation_evidence"] = operation_evidence
    merged["universal_harness"] = universal_summary
    if action_matrix_summary is not None:
        merged["action_matrix"] = action_matrix_summary
    if control_surface_summary is not None:
        merged["control_surface"] = control_surface_summary
    if universal_projection is not None:
        merged["universal_session_projection"] = universal_projection
        if not isinstance(merged.get("session_projection"), dict):
            merged["session_projection"] = {
                "artifact_kind": "universal_agent_harness_session_projection",
                "provider": source.get("provider"),
                "status": "captured",
                "projection": universal_projection,
            }
    if universal.get("verdict") == "red":
        merged["verdict"] = "red"
        merged["failure_code"] = (
            universal.get("failure_code") or "universal_harness_failed"
        )
        merged["recommendation"] = "block_upgrade_recommendation"
    return merged


def _merge_antigravity_real_send_proof(
    source: dict[str, Any], control: dict[str, Any]
) -> dict[str, Any]:
    merged = dict(source)
    canaries = dict(merged.get("canaries") or {})
    operation_evidence = dict(merged.get("operation_evidence") or {})
    antigravity = (control.get("canaries") or {}).get("antigravity")
    if not isinstance(antigravity, dict):
        antigravity = _fail_control_canary(
            "antigravity_real_agy_send_missing",
            "provider-control-e2e did not emit an Antigravity canary result.",
        )
    canaries["antigravity_real_agy_send"] = antigravity
    for operation, evidence in dict(
        antigravity.get("operation_evidence") or {}
    ).items():
        if isinstance(operation, str) and isinstance(evidence, dict):
            operation_evidence[operation] = evidence
    merged["canaries"] = canaries
    merged["operation_evidence"] = operation_evidence
    if antigravity.get("status") == "fail":
        merged["verdict"] = "red"
        merged["failure_code"] = str(
            antigravity.get("failure_code") or "antigravity_real_agy_send_failed"
        )
        merged["recommendation"] = "block_upgrade_recommendation"
    return merged


def _merge_opencode_real_tool_proof(
    source: dict[str, Any], control: dict[str, Any]
) -> dict[str, Any]:
    merged = dict(source)
    canaries = dict(merged.get("canaries") or {})
    operation_evidence = dict(merged.get("operation_evidence") or {})
    opencode = (control.get("canaries") or {}).get("opencode")
    if not isinstance(opencode, dict):
        opencode = _fail_control_canary(
            "opencode_real_tool_proof_missing",
            "provider-control-e2e did not emit an OpenCode canary result.",
        )
    canaries["opencode_real_tool_result_shape"] = opencode
    for operation, evidence in dict(opencode.get("operation_evidence") or {}).items():
        if isinstance(operation, str) and isinstance(evidence, dict):
            operation_evidence[operation] = evidence
    merged["canaries"] = canaries
    merged["operation_evidence"] = operation_evidence
    if opencode.get("status") == "fail":
        merged["verdict"] = "red"
        merged["failure_code"] = str(
            opencode.get("failure_code") or "opencode_real_tool_proof_failed"
        )
        merged["recommendation"] = "block_upgrade_recommendation"
    return merged


def _merge_claude_real_print_proof(
    source: dict[str, Any], control: dict[str, Any]
) -> dict[str, Any]:
    merged = dict(source)
    canaries = dict(merged.get("canaries") or {})
    operation_evidence = dict(merged.get("operation_evidence") or {})
    claude = (control.get("canaries") or {}).get("claude")
    if not isinstance(claude, dict):
        claude = _fail_control_canary(
            "claude_real_print_proof_missing",
            "provider-control-e2e did not emit a Claude canary result.",
        )
    canaries["claude_real_print"] = claude
    for operation, evidence in dict(claude.get("operation_evidence") or {}).items():
        if isinstance(operation, str) and isinstance(evidence, dict):
            operation_evidence[operation] = evidence
    merged["canaries"] = canaries
    merged["operation_evidence"] = operation_evidence
    if claude.get("status") == "fail":
        merged["verdict"] = "red"
        merged["failure_code"] = str(
            claude.get("failure_code") or "claude_real_print_proof_failed"
        )
        merged["recommendation"] = "block_upgrade_recommendation"
    return merged


def _merge_claude_machine_live_proof(
    source: dict[str, Any], machine: dict[str, Any]
) -> dict[str, Any]:
    merged = dict(source)
    canaries = dict(merged.get("canaries") or {})
    operation_evidence = dict(merged.get("operation_evidence") or {})
    machine_canary = (machine.get("canaries") or {}).get("claude_machine_live_proof")
    if not isinstance(machine_canary, dict):
        machine_canary = _fail_control_canary(
            "claude_machine_live_proof_missing",
            "Claude machine live proof did not emit a proof canary.",
        )
    canaries["claude_machine_live_proof"] = machine_canary
    for operation, evidence in dict(machine.get("operation_evidence") or {}).items():
        if isinstance(operation, str) and isinstance(evidence, dict):
            operation_evidence[operation] = evidence
    merged["canaries"] = canaries
    merged["operation_evidence"] = operation_evidence
    if not merged.get("provider_version") and machine.get("provider_version"):
        merged["provider_version"] = machine.get("provider_version")

    machine_verdict = str(machine.get("verdict") or "").lower()
    source_verdict = str(source.get("verdict") or "").lower()
    if machine_verdict == "red":
        merged["verdict"] = "red"
        merged["failure_code"] = str(
            machine.get("failure_code") or "claude_machine_live_proof_failed"
        )
        merged["recommendation"] = "block_upgrade_recommendation"
    elif source_verdict != "red" and machine_verdict == "green":
        merged["verdict"] = "green"
        merged["failure_code"] = None
        merged["recommendation"] = "upgrade_allowed"
    elif source_verdict != "red" and machine_verdict == "yellow":
        merged["verdict"] = "yellow"
        merged["failure_code"] = str(
            machine.get("failure_code") or "claude_machine_live_proof_warn"
        )
        merged["recommendation"] = "investigate_before_upgrade"
    return merged


def _fail_control_canary(code: str, message: str, **fields: Any) -> dict[str, Any]:
    payload = {"status": "fail", "failure_code": code, "message": message}
    payload.update(fields)
    return payload


def _detail_message(payload: dict[str, Any]) -> str:
    detail = payload.get("detail")
    if isinstance(detail, dict):
        code = detail.get("code")
        message = detail.get("message")
        if code and message:
            return f"{code}: {message}"
        if message:
            return str(message)
        if code:
            return str(code)
    if isinstance(detail, str) and detail:
        return detail
    error = payload.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message")
        if code and message:
            return f"{code}: {message}"
        if message:
            return str(message)
    return json.dumps(payload, sort_keys=True)[-1000:]


def _legacy_live_token_contract_rejected(status: int, payload: dict[str, Any]) -> bool:
    if status != 422:
        return False
    detail = payload.get("detail")
    if not isinstance(detail, list):
        return False
    rejected_fields = set()
    for item in detail:
        if not isinstance(item, dict):
            continue
        loc = item.get("loc")
        if isinstance(loc, list) and len(loc) >= 2 and loc[0] == "body":
            rejected_fields.add(str(loc[1]))
    return bool(
        rejected_fields & {"run_live_token_contract", "live_token_timeout_secs"}
    )


def _request_json(
    *,
    method: str,
    url: str,
    token: str,
    body: dict[str, Any] | None,
    timeout_s: float,
) -> tuple[int, dict[str, Any]]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "longhouse-provider-release-proof/1",
        "X-Agents-Token": token,
    }
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw = response.read().decode("utf-8")
            status = getattr(response, "status", None)
            if status is None and hasattr(response, "getcode"):
                status = response.getcode()
            payload = json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"detail": raw[-2000:]}
        if not isinstance(payload, dict):
            payload = {"detail": raw[-2000:]}
        return exc.code, payload
    except (TimeoutError, urllib.error.URLError) as exc:
        return 0, {"detail": {"code": "request_error", "message": str(exc)}}
    except json.JSONDecodeError:
        return 502, {
            "detail": {"code": "invalid_json", "message": "response was not JSON"}
        }
    if not isinstance(payload, dict):
        return 502, {
            "detail": {
                "code": "invalid_json",
                "message": "response was not a JSON object",
            }
        }
    return int(status or 200), payload


def _poll_operation(
    *,
    api_url: str,
    token: str,
    device_id: str,
    provider: str,
    accepted: dict[str, Any],
    http_timeout_s: float,
    poll_timeout_s: float,
) -> tuple[int, dict[str, Any]]:
    status_url = str(accepted.get("status_url") or "").strip()
    operation_id = str(accepted.get("operation_id") or "").strip()
    if not status_url or not operation_id:
        return 502, {
            "detail": {
                "code": "provider_live_operation_malformed",
                "message": "provider live proof did not return an operation",
            }
        }
    url = f"{api_url}{status_url}" if status_url.startswith("/") else status_url
    deadline = time.monotonic() + max(1.0, poll_timeout_s)
    while True:
        status, payload = _request_json(
            method="GET",
            url=url,
            token=token,
            body=None,
            timeout_s=http_timeout_s,
        )
        if status != 200:
            if status in RETRYABLE_STATUS_CODES and time.monotonic() < deadline:
                time.sleep(DEFAULT_OPERATION_POLL_INTERVAL_S)
                continue
            return status, payload
        operation_status = str(payload.get("status") or "")
        if operation_status == "succeeded":
            result = payload.get("result")
            if not isinstance(result, dict):
                return 502, {
                    "detail": {
                        "code": "provider_live_operation_result_malformed",
                        "message": "provider live proof operation succeeded without a result",
                    }
                }
            return 200, {
                "device_id": device_id,
                "provider": provider,
                "command_id": str(payload.get("command_id") or operation_id),
                "result": result,
                "operation_id": operation_id,
            }
        if operation_status in {"failed", "timed_out"}:
            error = (
                payload.get("error") if isinstance(payload.get("error"), dict) else {}
            )
            code = str(error.get("code") or "provider_live_operation_failed")
            return 502, {
                "detail": {
                    "code": code,
                    "message": str(
                        error.get("message")
                        or f"provider live proof operation {operation_status}"
                    ),
                },
                "operation_id": operation_id,
            }
        if time.monotonic() >= deadline:
            return 503, {
                "detail": {
                    "code": "provider_live_operation_poll_timeout",
                    "message": f"provider live proof operation {operation_id} did not finish before client timeout",
                },
                "operation_id": operation_id,
                "last_status": operation_status,
            }
        time.sleep(DEFAULT_OPERATION_POLL_INTERVAL_S)


def _post_machine_live_proof(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    process_timeout_s = max(1, min(int(args.timeout_secs), 900))
    live_token_timeout_s = max(1, min(int(args.timeout_secs), 600))
    api_url = args.claude_api_url.rstrip("/")
    base_body: dict[str, Any] = {
        "provider": "claude",
        "publish": True,
        "timeout_secs": process_timeout_s,
    }
    if args.provider_version:
        base_body["expected_provider_version"] = args.provider_version
    body: dict[str, Any] = {
        **base_body,
        "run_live_token_contract": True,
        "live_token_timeout_secs": live_token_timeout_s,
    }
    status, payload = _request_json(
        method="POST",
        url=f"{api_url}/api/agents/machines/{args.claude_device_id}/provider-live-proof",
        token=args.claude_agents_token,
        body=body,
        timeout_s=process_timeout_s + 30,
    )
    if _legacy_live_token_contract_rejected(status, payload):
        status, payload = _request_json(
            method="POST",
            url=f"{api_url}/api/agents/machines/{args.claude_device_id}/provider-live-proof",
            token=args.claude_agents_token,
            body=base_body,
            timeout_s=process_timeout_s + 30,
        )
    if status != 202:
        return status, payload
    return _poll_operation(
        api_url=api_url,
        token=args.claude_agents_token,
        device_id=args.claude_device_id,
        provider="claude",
        accepted=payload,
        http_timeout_s=process_timeout_s + 30,
        poll_timeout_s=process_timeout_s + 60,
    )


def _missing_claude_machine_live_operations(
    artifact: dict[str, Any],
) -> list[str]:
    evidence = artifact.get("operation_evidence")
    if not isinstance(evidence, dict):
        evidence = {}
    missing: list[str] = []
    for operation in CLAUDE_MACHINE_LIVE_REQUIRED_OPERATIONS:
        item = evidence.get(operation)
        if not isinstance(item, dict):
            missing.append(operation)
            continue
        if item.get("status") != "pass" or item.get("level") != "manual_live_token":
            missing.append(operation)
    return missing


def _run_claude_machine_live_proof(
    args: argparse.Namespace,
    raw_dir: Path,
) -> tuple[dict[str, Any], dict[str, str], int | None]:
    artifact_path = raw_dir / "claude-machine-live-proof.json"
    raw_artifacts = {"claude_machine_live_artifact": str(artifact_path)}
    status, payload = _post_machine_live_proof(args)
    _write_json(artifact_path, payload)
    if status != 200:
        message = _detail_message(payload)
        artifact = {
            "artifact_kind": "provider_live_canary",
            "provider": "claude",
            "provider_version": args.provider_version,
            "verdict": "red",
            "failure_code": "claude_machine_live_proof_failed",
            "recommendation": "block_upgrade_recommendation",
            "canaries": {
                "claude_machine_live_proof": _fail_control_canary(
                    "claude_machine_live_proof_failed",
                    f"Runtime Host provider-live-proof returned HTTP {status}: {message}",
                )
            },
            "operation_evidence": {},
        }
        return artifact, raw_artifacts, 1

    result = payload.get("result")
    live_artifact = result.get("artifact") if isinstance(result, dict) else None
    if not isinstance(live_artifact, dict):
        artifact = {
            "artifact_kind": "provider_live_canary",
            "provider": "claude",
            "provider_version": args.provider_version,
            "verdict": "red",
            "failure_code": "claude_machine_live_artifact_missing",
            "recommendation": "block_upgrade_recommendation",
            "canaries": {
                "claude_machine_live_proof": _fail_control_canary(
                    "claude_machine_live_artifact_missing",
                    "Runtime Host provider-live-proof response did not include a live artifact.",
                )
            },
            "operation_evidence": {},
        }
        return artifact, raw_artifacts, 1

    live_artifact = dict(live_artifact)
    missing_operations = _missing_claude_machine_live_operations(live_artifact)
    insufficient_message = None
    if live_artifact.get("verdict") != "red" and missing_operations:
        insufficient_message = (
            "Runtime Host provider-live-proof did not return required "
            "manual live-token evidence for: " + ", ".join(missing_operations)
        )
        live_artifact["verdict"] = "yellow"
        live_artifact["failure_code"] = "claude_machine_live_insufficient_coverage"
        live_artifact["recommendation"] = "investigate_before_upgrade"
    canaries = dict(live_artifact.get("canaries") or {})
    verdict = str(live_artifact.get("verdict") or "").lower()
    canary_status = (
        "fail" if verdict == "red" else "warn" if verdict == "yellow" else "pass"
    )
    canaries["claude_machine_live_proof"] = {
        "status": canary_status,
        "verdict": live_artifact.get("verdict"),
        "failure_code": live_artifact.get("failure_code"),
        "device_id": payload.get("device_id"),
        "command_id": payload.get("command_id"),
        "operation_id": payload.get("operation_id"),
    }
    if insufficient_message is not None:
        canaries["claude_machine_live_proof"]["message"] = insufficient_message
        canaries["claude_machine_live_proof"]["missing_operations"] = missing_operations
    live_artifact["canaries"] = canaries
    returncode = None
    if isinstance(result, dict) and result.get("exit_code") is not None:
        returncode = int(result.get("exit_code"))
    return live_artifact, raw_artifacts, returncode


def _run_claude_real_print_proof(
    args: argparse.Namespace,
    raw_dir: Path,
) -> tuple[dict[str, Any], dict[str, str], int | None]:
    artifact_path = raw_dir / "claude-control-e2e.json"
    evidence_root = raw_dir / "claude-control-evidence"
    stdout_path = raw_dir / "claude-control-stdout.log"
    stderr_path = raw_dir / "claude-control-stderr.log"
    argv = [
        sys.executable,
        str(args.repo_root / "scripts" / "qa" / "provider-control-e2e-canary.py"),
        "--repo-root",
        str(args.repo_root),
        "--provider",
        "claude",
        "--artifact",
        str(artifact_path),
        "--evidence-root",
        str(evidence_root),
        "--claude-run-real-print",
        "--claude-print-timeout-secs",
        str(args.claude_print_timeout_secs),
        "--json",
    ]
    raw_artifacts = {
        "claude_control_artifact": str(artifact_path),
        "claude_control_stdout": str(stdout_path),
        "claude_control_stderr": str(stderr_path),
    }
    if artifact_path.exists():
        artifact_path.unlink()
    run_env = os.environ.copy()
    if args.provider_bin is not None:
        run_env[CLAUDE_BIN_ENV] = str(args.provider_bin)
    try:
        result = subprocess.run(
            argv,
            cwd=str(args.repo_root),
            env=run_env,
            text=True,
            capture_output=True,
            check=False,
            timeout=max(args.timeout_secs, args.claude_print_timeout_secs + 30, 75),
        )
    except subprocess.TimeoutExpired as exc:
        stdout_path.write_text(str(exc.stdout or ""), encoding="utf-8")
        stderr_path.write_text(str(exc.stderr or ""), encoding="utf-8")
        artifact = {
            "schema_version": 1,
            "provider": "claude",
            "verdict": "red",
            "failure_code": "claude_real_print_timeout",
            "canaries": {
                "claude": _fail_control_canary(
                    "claude_real_print_timeout",
                    f"provider-control-e2e timed out after {args.timeout_secs}s",
                )
            },
            "evidence_root": str(evidence_root),
        }
        _write_json(artifact_path, artifact)
        return artifact, raw_artifacts, None
    stdout_path.write_text(result.stdout, encoding="utf-8")
    stderr_path.write_text(result.stderr, encoding="utf-8")
    if artifact_path.exists():
        try:
            artifact = _read_json(artifact_path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            artifact = {
                "schema_version": 1,
                "provider": "claude",
                "verdict": "red",
                "failure_code": "claude_real_print_invalid_json",
                "canaries": {
                    "claude": _fail_control_canary(
                        "claude_real_print_invalid_json",
                        f"{type(exc).__name__}: {exc}",
                        command=_command_evidence(result),
                    )
                },
                "evidence_root": str(evidence_root),
            }
            _write_json(artifact_path, artifact)
    else:
        artifact = {
            "schema_version": 1,
            "provider": "claude",
            "verdict": "red",
            "failure_code": "claude_real_print_missing_artifact",
            "canaries": {
                "claude": _fail_control_canary(
                    "claude_real_print_missing_artifact",
                    "provider-control-e2e exited without writing an artifact.",
                    command=_command_evidence(result),
                )
            },
            "evidence_root": str(evidence_root),
        }
        _write_json(artifact_path, artifact)
    return artifact, raw_artifacts, result.returncode


def _run_antigravity_real_send_proof(
    args: argparse.Namespace,
    raw_dir: Path,
) -> tuple[dict[str, Any], dict[str, str], int | None]:
    artifact_path = raw_dir / "antigravity-control-e2e.json"
    evidence_root = raw_dir / "antigravity-control-evidence"
    stdout_path = raw_dir / "antigravity-control-stdout.log"
    stderr_path = raw_dir / "antigravity-control-stderr.log"
    argv = [
        sys.executable,
        str(args.repo_root / "scripts" / "qa" / "provider-control-e2e-canary.py"),
        "--repo-root",
        str(args.repo_root),
        "--provider",
        "antigravity",
        "--artifact",
        str(artifact_path),
        "--evidence-root",
        str(evidence_root),
        "--antigravity-real-agy-send",
        "--antigravity-print-timeout-secs",
        str(args.antigravity_print_timeout_secs),
        "--json",
    ]
    raw_artifacts = {
        "antigravity_control_artifact": str(artifact_path),
        "antigravity_control_stdout": str(stdout_path),
        "antigravity_control_stderr": str(stderr_path),
    }
    if artifact_path.exists():
        artifact_path.unlink()
    run_env = os.environ.copy()
    if args.provider_bin is not None:
        run_env[ANTIGRAVITY_BIN_ENV] = str(args.provider_bin)
    try:
        result = subprocess.run(
            argv,
            cwd=str(args.repo_root),
            env=run_env,
            text=True,
            capture_output=True,
            check=False,
            timeout=args.timeout_secs,
        )
    except subprocess.TimeoutExpired as exc:
        stdout_path.write_text(str(exc.stdout or ""), encoding="utf-8")
        stderr_path.write_text(str(exc.stderr or ""), encoding="utf-8")
        artifact = {
            "schema_version": 1,
            "provider": "antigravity",
            "verdict": "red",
            "failure_code": "antigravity_real_agy_send_timeout",
            "canaries": {
                "antigravity": _fail_control_canary(
                    "antigravity_real_agy_send_timeout",
                    f"provider-control-e2e timed out after {args.timeout_secs}s",
                )
            },
            "evidence_root": str(evidence_root),
        }
        _write_json(artifact_path, artifact)
        return artifact, raw_artifacts, None
    stdout_path.write_text(result.stdout, encoding="utf-8")
    stderr_path.write_text(result.stderr, encoding="utf-8")
    if artifact_path.exists():
        try:
            artifact = _read_json(artifact_path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            artifact = {
                "schema_version": 1,
                "provider": "antigravity",
                "verdict": "red",
                "failure_code": "antigravity_real_agy_send_invalid_json",
                "canaries": {
                    "antigravity": _fail_control_canary(
                        "antigravity_real_agy_send_invalid_json",
                        f"{type(exc).__name__}: {exc}",
                        command=_command_evidence(result),
                    )
                },
                "evidence_root": str(evidence_root),
            }
            _write_json(artifact_path, artifact)
    else:
        artifact = {
            "schema_version": 1,
            "provider": "antigravity",
            "verdict": "red",
            "failure_code": "antigravity_real_agy_send_missing_artifact",
            "canaries": {
                "antigravity": _fail_control_canary(
                    "antigravity_real_agy_send_missing_artifact",
                    "provider-control-e2e exited without writing an artifact.",
                    command=_command_evidence(result),
                )
            },
            "evidence_root": str(evidence_root),
        }
        _write_json(artifact_path, artifact)
    return artifact, raw_artifacts, result.returncode


def _run_opencode_real_tool_proof(
    args: argparse.Namespace,
    raw_dir: Path,
) -> tuple[dict[str, Any], dict[str, str], int | None]:
    artifact_path = raw_dir / "opencode-control-e2e.json"
    evidence_root = raw_dir / "opencode-control-evidence"
    stdout_path = raw_dir / "opencode-control-stdout.log"
    stderr_path = raw_dir / "opencode-control-stderr.log"
    argv = [
        sys.executable,
        str(args.repo_root / "scripts" / "qa" / "provider-control-e2e-canary.py"),
        "--repo-root",
        str(args.repo_root),
        "--provider",
        "opencode",
        "--artifact",
        str(artifact_path),
        "--evidence-root",
        str(evidence_root),
        "--opencode-run-real-tool",
        "--opencode-run-timeout-secs",
        str(args.opencode_run_timeout_secs),
        "--json",
    ]
    raw_artifacts = {
        "opencode_control_artifact": str(artifact_path),
        "opencode_control_stdout": str(stdout_path),
        "opencode_control_stderr": str(stderr_path),
    }
    if artifact_path.exists():
        artifact_path.unlink()
    run_env = os.environ.copy()
    if args.provider_bin is not None:
        run_env[OPENCODE_BIN_ENV] = str(args.provider_bin)
    try:
        result = subprocess.run(
            argv,
            cwd=str(args.repo_root),
            env=run_env,
            text=True,
            capture_output=True,
            check=False,
            timeout=args.timeout_secs,
        )
    except subprocess.TimeoutExpired as exc:
        stdout_path.write_text(str(exc.stdout or ""), encoding="utf-8")
        stderr_path.write_text(str(exc.stderr or ""), encoding="utf-8")
        artifact = {
            "schema_version": 1,
            "provider": "opencode",
            "verdict": "red",
            "failure_code": "opencode_real_tool_proof_timeout",
            "canaries": {
                "opencode": _fail_control_canary(
                    "opencode_real_tool_proof_timeout",
                    f"provider-control-e2e timed out after {args.timeout_secs}s",
                )
            },
            "evidence_root": str(evidence_root),
        }
        _write_json(artifact_path, artifact)
        return artifact, raw_artifacts, None
    stdout_path.write_text(result.stdout, encoding="utf-8")
    stderr_path.write_text(result.stderr, encoding="utf-8")
    if artifact_path.exists():
        try:
            artifact = _read_json(artifact_path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            artifact = {
                "schema_version": 1,
                "provider": "opencode",
                "verdict": "red",
                "failure_code": "opencode_real_tool_proof_invalid_json",
                "canaries": {
                    "opencode": _fail_control_canary(
                        "opencode_real_tool_proof_invalid_json",
                        f"{type(exc).__name__}: {exc}",
                        command=_command_evidence(result),
                    )
                },
                "evidence_root": str(evidence_root),
            }
            _write_json(artifact_path, artifact)
    else:
        artifact = {
            "schema_version": 1,
            "provider": "opencode",
            "verdict": "red",
            "failure_code": "opencode_real_tool_proof_missing_artifact",
            "canaries": {
                "opencode": _fail_control_canary(
                    "opencode_real_tool_proof_missing_artifact",
                    "provider-control-e2e exited without writing an artifact.",
                    command=_command_evidence(result),
                )
            },
            "evidence_root": str(evidence_root),
        }
        _write_json(artifact_path, artifact)
    return artifact, raw_artifacts, result.returncode


def run_provider_release_proof(args: argparse.Namespace) -> dict[str, Any]:
    args.repo_root = args.repo_root.expanduser().resolve()
    args.evidence_root = (
        args.evidence_root or Path.cwd() / "provider-release-proof-evidence"
    ).expanduser()
    args.artifact = (
        args.artifact or args.evidence_root / "provider-release-proof.json"
    ).expanduser()
    if args.provider_bin is not None:
        args.provider_bin = args.provider_bin.expanduser()
    if args.universal_fixture_path is not None:
        args.universal_fixture_path = args.universal_fixture_path.expanduser()
    if args.universal_old_proof_artifact is not None:
        args.universal_old_proof_artifact = args.universal_old_proof_artifact.expanduser()
    if args.universal_new_proof_artifact is not None:
        args.universal_new_proof_artifact = args.universal_new_proof_artifact.expanduser()
    if args.universal_baseline_root is not None:
        args.universal_baseline_root = args.universal_baseline_root.expanduser()
    if args.provider == "codex":
        args.codex_api_url = args.codex_api_url or os.getenv(CODEX_API_URL_ENV)
        args.codex_agents_token = args.codex_agents_token or os.getenv(
            CODEX_AGENTS_TOKEN_ENV
        )
    if args.provider == "claude":
        args.claude_api_url = args.claude_api_url or os.getenv(CLAUDE_API_URL_ENV)
        args.claude_agents_token = args.claude_agents_token or os.getenv(
            CLAUDE_AGENTS_TOKEN_ENV
        )
        args.claude_device_id = args.claude_device_id or os.getenv(CLAUDE_DEVICE_ID_ENV)
    preflight = _proof_preflight(args)
    if args.preflight_only:
        preflight["artifact_path"] = str(args.artifact)
        _write_json(args.artifact, preflight)
        return preflight

    raw_dir = args.evidence_root / "raw"
    normalized_dir = args.evidence_root / "normalized"
    normalized_dir.mkdir(parents=True, exist_ok=True)
    source_artifact, raw_artifacts, returncode = _run_source_canary(args, raw_dir)
    if args.run_universal_harness:
        universal_artifact, universal_artifacts, universal_returncode = (
            _run_universal_harness(args, raw_dir)
        )
        raw_artifacts.update(universal_artifacts)
        source_artifact = _merge_universal_harness(source_artifact, universal_artifact)
        returncode = returncode or universal_returncode
    if args.provider == "claude" and args.claude_run_machine_live_proof:
        machine_artifact, machine_artifacts, machine_returncode = (
            _run_claude_machine_live_proof(args, raw_dir)
        )
        raw_artifacts.update(machine_artifacts)
        source_artifact = _merge_claude_machine_live_proof(
            source_artifact, machine_artifact
        )
        returncode = returncode or machine_returncode
    if args.provider == "claude" and args.claude_run_real_print:
        control_artifact, control_artifacts, control_returncode = (
            _run_claude_real_print_proof(args, raw_dir)
        )
        raw_artifacts.update(control_artifacts)
        source_artifact = _merge_claude_real_print_proof(
            source_artifact, control_artifact
        )
        returncode = returncode or control_returncode
    if args.provider == "opencode" and args.opencode_run_real_tool:
        control_artifact, control_artifacts, control_returncode = (
            _run_opencode_real_tool_proof(args, raw_dir)
        )
        raw_artifacts.update(control_artifacts)
        source_artifact = _merge_opencode_real_tool_proof(
            source_artifact, control_artifact
        )
        returncode = returncode or control_returncode
    if args.provider == "antigravity" and args.antigravity_run_real_agy_send:
        control_artifact, control_artifacts, control_returncode = (
            _run_antigravity_real_send_proof(args, raw_dir)
        )
        raw_artifacts.update(control_artifacts)
        source_artifact = _merge_antigravity_real_send_proof(
            source_artifact, control_artifact
        )
        returncode = returncode or control_returncode
    normalized = _normalize_source_artifact(source_artifact)
    normalized_path = normalized_dir / "contract.json"
    _write_json(normalized_path, normalized)

    verdict, failure_code, recommendation = _classify(
        source_artifact,
        source_canary_returncode=returncode,
    )
    contract = _load_provider_contract(args.repo_root, args.provider)
    contract_operations = (
        dict(contract.get("operation_evidence") or {}) if contract else {}
    )
    provider_version = (
        args.provider_version
        or normalized.get("provider_version")
        or source_artifact.get("provider_version")
        or source_artifact.get("codex_version")
    )
    provider_contract_path = normalized_dir / "provider_contract.json"
    operation_evidence_path = normalized_dir / "operation_evidence.json"
    session_projection_path = normalized_dir / "session_projection.json"
    action_matrix_path = normalized_dir / "action_matrix.json"
    control_surface_path = normalized_dir / "control_surface.json"
    provider_contract = {
        "artifact_kind": "provider_release_proof_provider_contract",
        "provider": args.provider,
        "provider_version": provider_version,
        "contract_operations": contract_operations,
    }
    operation_evidence_artifact = {
        "artifact_kind": "provider_release_proof_operation_evidence",
        "provider": args.provider,
        "provider_version": provider_version,
        "operation_evidence": normalized.get("operation_evidence") or {},
    }
    session_projection = _session_projection_artifact(
        provider=args.provider,
        provider_version=provider_version,
        source_artifact=source_artifact,
    )
    action_matrix_artifact = {
        "artifact_kind": "provider_release_proof_action_matrix",
        "provider": args.provider,
        "provider_version": provider_version,
        "status": "captured"
        if isinstance(source_artifact.get("action_matrix"), dict)
        else "not_captured",
        "action_matrix": source_artifact.get("action_matrix")
        if isinstance(source_artifact.get("action_matrix"), dict)
        else None,
    }
    control_surface_artifact = {
        "artifact_kind": "provider_release_proof_control_surface",
        "provider": args.provider,
        "provider_version": provider_version,
        "status": "captured"
        if isinstance(source_artifact.get("control_surface"), dict)
        else "not_captured",
        "control_surface": source_artifact.get("control_surface")
        if isinstance(source_artifact.get("control_surface"), dict)
        else None,
    }
    _write_json(provider_contract_path, provider_contract)
    _write_json(operation_evidence_path, operation_evidence_artifact)
    _write_json(session_projection_path, session_projection)
    _write_json(action_matrix_path, action_matrix_artifact)
    _write_json(control_surface_path, control_surface_artifact)
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "provider_release_proof",
        "provider": args.provider,
        "provider_version": provider_version,
        "generated_at": _now_iso(),
        "scenario_id": _scenario_id(args),
        "scenario_profile": _scenario_profile(args),
        "scenario_version": 1,
        "preflight": preflight,
        "verdict": verdict,
        "failure_code": failure_code,
        "recommendation": recommendation,
        "source_canary_returncode": returncode,
        "canaries": {
            "source_canary": {
                "status": "pass"
                if verdict == "green"
                else "fail"
                if verdict == "red"
                else "warn",
                "verdict": source_artifact.get("verdict"),
                "failure_code": source_artifact.get("failure_code"),
                "artifact_path": raw_artifacts["source_artifact"],
            }
        },
        "operation_evidence": normalized.get("operation_evidence") or {},
        "action_matrix": source_artifact.get("action_matrix"),
        "control_surface": source_artifact.get("control_surface"),
        "normalized": normalized,
        "contract_operations": contract_operations,
        "artifacts": {
            **raw_artifacts,
            "normalized_contract": str(normalized_path),
            "provider_contract": str(provider_contract_path),
            "operation_evidence": str(operation_evidence_path),
            "session_projection": str(session_projection_path),
            "action_matrix": str(action_matrix_path),
            "control_surface": str(control_surface_path),
            "evidence_root": str(args.evidence_root),
        },
    }
    artifact["artifact_path"] = str(args.artifact)
    _write_json(args.artifact, artifact)
    return artifact


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=_repo_root_from_script())
    parser.add_argument("--provider", choices=SUPPORTED_PROVIDERS, required=True)
    parser.add_argument("--provider-bin", type=Path)
    parser.add_argument("--provider-version")
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--scenario-id")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--timeout-secs", type=int, default=180)
    parser.add_argument(
        "--source-review-status",
        choices=["not_run", "pass", "warn", "fail"],
        default="not_run",
        help="External source-review status passed through to providers that require it.",
    )
    parser.add_argument(
        "--source-review-note",
        default="Provider release proof did not include external source-review evidence.",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--run-universal-harness", action="store_true")
    parser.add_argument(
        "--universal-scenario",
        action="append",
        choices=UNIVERSAL_SCENARIOS,
        help="Universal harness scenario to attach. Repeatable; defaults to release-proof MVP scenarios.",
    )
    parser.add_argument(
        "--universal-fixture-path",
        type=Path,
        help="JSONL fixture for universal parse_ingest_project.",
    )
    parser.add_argument(
        "--universal-prompt", help="Prompt for universal prompt/session scenarios."
    )
    parser.add_argument(
        "--universal-old-proof-artifact",
        type=Path,
        help="Old provider release-proof artifact for universal old_new_release_diff.",
    )
    parser.add_argument(
        "--universal-new-proof-artifact",
        type=Path,
        help="New provider release-proof artifact for universal old_new_release_diff.",
    )
    parser.add_argument(
        "--universal-baseline-root",
        type=Path,
        help="Baseline root passed to universal old_new_release_diff.",
    )
    parser.add_argument("--codex-run-fake-app-server", action="store_true")
    parser.add_argument("--codex-run-raw-fresh-remote", action="store_true")
    parser.add_argument("--codex-run-managed-tui-attach", action="store_true")
    parser.add_argument("--codex-run-detached-ui", action="store_true")
    parser.add_argument("--codex-run-managed-live-send", action="store_true")
    parser.add_argument("--codex-run-managed-live-interrupt", action="store_true")
    parser.add_argument("--codex-run-real-tool", action="store_true")
    parser.add_argument("--codex-live-interrupt-timeout-secs", type=int, default=45)
    parser.add_argument("--codex-real-tool-timeout-secs", type=int, default=180)
    parser.add_argument("--codex-api-url")
    parser.add_argument("--codex-agents-token")
    parser.add_argument("--claude-run-machine-live-proof", action="store_true")
    parser.add_argument("--claude-run-real-print", action="store_true")
    parser.add_argument("--claude-api-url")
    parser.add_argument("--claude-agents-token")
    parser.add_argument("--claude-device-id")
    parser.add_argument(
        "--claude-print-timeout-secs",
        type=int,
        default=180,
        help="Timeout for the real claude --print run; provider-control-e2e-canary enforces a minimum of 45 seconds.",
    )
    parser.add_argument("--opencode-run-real-tool", action="store_true")
    parser.add_argument(
        "--opencode-run-timeout-secs",
        type=int,
        default=180,
        help="Timeout for the real opencode tool run; provider-control-e2e-canary enforces a minimum of 45 seconds.",
    )
    parser.add_argument("--antigravity-run-real-agy-send", action="store_true")
    parser.add_argument("--antigravity-print-timeout-secs", type=int, default=45)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    artifact = run_provider_release_proof(args)
    if args.json:
        print(json.dumps(artifact, indent=2, sort_keys=True))
    else:
        print(f"provider release proof: {artifact['provider']} {artifact['verdict']}")
        if artifact.get("artifact_path"):
            print(f"artifact: {artifact['artifact_path']}")
        artifacts = (
            artifact.get("artifacts")
            if isinstance(artifact.get("artifacts"), dict)
            else {}
        )
        if artifacts.get("evidence_root"):
            print(f"evidence_root: {artifacts['evidence_root']}")
        if artifact.get("failure_code"):
            print(f"failure_code: {artifact['failure_code']}")
    return 1 if artifact["verdict"] == "red" else 0


if __name__ == "__main__":
    raise SystemExit(main())
