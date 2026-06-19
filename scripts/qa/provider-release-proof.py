#!/usr/bin/env python3
"""Normalize Longhouse provider release proof into one artifact.

This is the Longhouse-owned entrypoint Sauron should call for upstream provider
release checks. Provider-specific canaries own behavior; this wrapper owns the
release-proof artifact shape.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SUPPORTED_PROVIDERS = ("claude", "codex", "opencode", "antigravity", "gemini")
LIVE_CANARY_PROVIDERS = frozenset({"claude", "opencode", "antigravity"})
GEMINI_PARSER_FIXTURES = {
    "basic": "gemini_session.json",
    "schema_drift": "gemini_drift.json",
    "tool_results": "gemini_tool_results.json",
}


def _repo_root_from_script() -> Path:
    return Path(__file__).resolve().parents[2]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
    if info.get("reason") is not None:
        compact["reason"] = info.get("reason")
    if info.get("version") is not None:
        compact["version"] = info.get("version")
    protocol_fingerprints = _compact_protocol_fingerprints(info)
    if protocol_fingerprints is not None:
        compact["protocol_fingerprints"] = protocol_fingerprints
    return compact


def _compact_claude_canary(info: dict[str, Any]) -> dict[str, Any]:
    compact = _compact_status(info)
    for key in ("missing", "reason", "platform"):
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
        "provider_version": artifact.get("provider_version") or artifact.get("codex_version"),
        "verdict": artifact.get("verdict"),
        "failure_code": artifact.get("failure_code"),
        "canaries": {
            name: canary_compactor(info)
            for name, info in sorted(dict(artifact.get("canaries") or {}).items())
            if isinstance(info, dict)
        },
        "operation_evidence": {
            name: _compact_operation(info)
            for name, info in sorted(dict(artifact.get("operation_evidence") or {}).items())
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
        command_shape = canaries.get("command_shape") if isinstance(canaries.get("command_shape"), dict) else {}
        channels_shape = canaries.get("channels_shape") if isinstance(canaries.get("channels_shape"), dict) else {}
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


def _classify(source_artifact: dict[str, Any]) -> tuple[str, str | None, str]:
    verdict = str(source_artifact.get("verdict") or "").lower()
    failure_code = source_artifact.get("failure_code")
    recommendation = source_artifact.get("recommendation")
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
    return "yellow", "provider_release_proof_unknown_verdict", "investigate_before_upgrade"


def _command_evidence(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return {
        "argv": list(result.args) if isinstance(result.args, list) else result.args,
        "returncode": result.returncode,
        "stdout": (result.stdout or "")[-4000:],
        "stderr": (result.stderr or "")[-4000:],
    }


def _copy_fixture(src: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    dest.write_bytes(src.read_bytes())
    return dest


def _fixture_check(status: bool, *, failure_code: str | None = None, **extra: Any) -> dict[str, Any]:
    payload = {"status": "pass" if status else "fail"}
    if not status:
        payload["failure_code"] = failure_code or "gemini_parser_fixture_contract_failed"
    payload.update({key: value for key, value in extra.items() if value is not None})
    return payload


def _gemini_string_messages(messages: list[Any]) -> list[dict[str, Any]]:
    return [
        msg
        for msg in messages
        if isinstance(msg, dict)
        and msg.get("type") in {"user", "gemini"}
        and isinstance(msg.get("content"), str)
    ]


def _gemini_tool_result_outputs(tool_call: dict[str, Any]) -> list[str]:
    outputs: list[str] = []
    for item in tool_call.get("result") or []:
        if not isinstance(item, dict):
            continue
        response = (
            item.get("functionResponse", {})
            if isinstance(item.get("functionResponse"), dict)
            else {}
        ).get("response", {})
        if not isinstance(response, dict):
            continue
        for key in ("output", "error"):
            value = response.get(key)
            if isinstance(value, str):
                outputs.append(value)
    return outputs


def _run_gemini_parser_fixture_source(
    args: argparse.Namespace,
    raw_dir: Path,
) -> tuple[dict[str, Any], dict[str, str], int | None]:
    source_path = raw_dir / "gemini-parser-fixture-proof.json"
    stdout_path = raw_dir / "stdout.log"
    stderr_path = raw_dir / "stderr.log"
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")

    fixture_root = args.repo_root / "server" / "tests" / "integration" / "fixtures"
    fixture_copy_root = raw_dir / "fixtures"
    fixtures: dict[str, dict[str, Any]] = {}
    checks: dict[str, dict[str, Any]] = {}
    failures: list[str] = []

    for key, filename in GEMINI_PARSER_FIXTURES.items():
        path = fixture_root / filename
        try:
            copied = _copy_fixture(path, fixture_copy_root)
            payload = _read_json(path)
            messages = payload.get("messages")
            if not isinstance(messages, list):
                raise ValueError("messages is not a list")
            fixtures[key] = {
                "path": str(copied),
                "session_id": payload.get("sessionId"),
                "message_count": len(messages),
                "messages": messages,
            }
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            failures.append(key)
            checks[key] = _fixture_check(
                False,
                failure_code="gemini_parser_fixture_unavailable",
                reason=f"{type(exc).__name__}: {exc}",
            )

    basic = fixtures.get("basic")
    if basic is not None:
        messages = basic["messages"]
        string_messages = _gemini_string_messages(messages)
        roles = [msg.get("type") for msg in string_messages]
        assistant_texts = [
            msg.get("content", "")
            for msg in string_messages
            if msg.get("type") == "gemini"
        ]
        ok = roles == ["user", "gemini"] and any(
            text.strip() == "gemini ok" for text in assistant_texts
        )
        if not ok:
            failures.append("basic")
        checks["basic_session_fixture"] = _fixture_check(
            ok,
            roles=roles,
            string_message_count=len(string_messages),
            assistant_exact_marker="gemini ok",
        )

    drift = fixtures.get("schema_drift")
    if drift is not None:
        messages = drift["messages"]
        string_messages = _gemini_string_messages(messages)
        object_content_count = sum(
            1
            for msg in messages
            if isinstance(msg, dict)
            and msg.get("type") == "gemini"
            and isinstance(msg.get("content"), dict)
        )
        preserved_text = "\n".join(
            str(msg.get("content") or "") for msg in string_messages
        )
        ok = (
            len(string_messages) >= 3
            and object_content_count >= 1
            and "valid string message" in preserved_text
            and "follow-up after object content" in preserved_text
        )
        if not ok:
            failures.append("schema_drift")
        checks["schema_drift_fixture"] = _fixture_check(
            ok,
            string_message_count=len(string_messages),
            object_content_count=object_content_count,
        )

    tool_fixture = fixtures.get("tool_results")
    if tool_fixture is not None:
        tool_calls: list[dict[str, Any]] = []
        for msg in tool_fixture["messages"]:
            if isinstance(msg, dict):
                tool_calls.extend(
                    tc for tc in (msg.get("toolCalls") or []) if isinstance(tc, dict)
                )
        tool_call_ids = sorted(
            str(tc.get("id")) for tc in tool_calls if tc.get("id") is not None
        )
        result_ids = sorted(
            str(
                (
                    item.get("functionResponse", {})
                    if isinstance(item.get("functionResponse"), dict)
                    else {}
                ).get("id")
            )
            for tc in tool_calls
            for item in (tc.get("result") or [])
            if isinstance(item, dict)
            and (
                item.get("functionResponse", {})
                if isinstance(item.get("functionResponse"), dict)
                else {}
            ).get("id")
            is not None
        )
        outputs = [output for tc in tool_calls for output in _gemini_tool_result_outputs(tc)]
        ok = (
            tool_call_ids == ["tc-read", "tc-write"]
            and result_ids == ["tc-read", "tc-write"]
            and any("README content" in output for output in outputs)
            and any("cancelled" in output.lower() for output in outputs)
        )
        if not ok:
            failures.append("tool_results")
        checks["tool_result_fixture"] = _fixture_check(
            ok,
            tool_call_ids=tool_call_ids,
            tool_result_ids=result_ids,
            tool_result_count=len(outputs),
        )

    golden_path = args.repo_root / "engine" / "tests" / "fixtures" / "golden" / "gemini" / "basic.expected.json"
    try:
        golden = _read_json(golden_path)
        events = golden.get("events")
        event_count = golden.get("event_count")
        ok = (
            event_count == 2
            and isinstance(events, list)
            and [event.get("raw_type") for event in events] == [
                "gemini_user",
                "gemini_assistant",
            ]
        )
        if not ok:
            failures.append("golden_snapshot")
        checks["golden_parser_snapshot"] = _fixture_check(
            ok,
            event_count=event_count,
            raw_types=[
                event.get("raw_type")
                for event in events
                if isinstance(event, dict)
            ]
            if isinstance(events, list)
            else None,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        failures.append("golden_snapshot")
        checks["golden_parser_snapshot"] = _fixture_check(
            False,
            failure_code="gemini_golden_parser_snapshot_unavailable",
            reason=f"{type(exc).__name__}: {exc}",
        )

    verdict = "green" if not failures else "red"
    failure_code = None if not failures else "gemini_parser_fixture_contract_failed"
    provider_version = args.provider_version or "gemini-parser-fixtures"
    session_projection = {
        "artifact_kind": "provider_release_proof_session_projection",
        "provider": "gemini",
        "status": "fixture",
        "sessions": [
            {
                "fixture": key,
                "session_id": value.get("session_id"),
                "message_count": value.get("message_count"),
            }
            for key, value in sorted(fixtures.items())
        ],
    }
    fixture_projection_ok = verdict == "green"
    source = {
        "artifact_kind": "provider_release_proof_source",
        "provider": "gemini",
        "provider_version": provider_version,
        "verdict": verdict,
        "failure_code": failure_code,
        "recommendation": "upgrade_allowed"
        if verdict == "green"
        else "block_upgrade_recommendation",
        "canaries": checks,
        "operation_evidence": {
            "transcript_log_parse": {
                "status": "pass" if checks.get("golden_parser_snapshot", {}).get("status") == "pass" else "fail",
                "level": "fixture",
                "canary": "golden_parser_snapshot",
                "failure_code": None
                if checks.get("golden_parser_snapshot", {}).get("status") == "pass"
                else "gemini_parser_fixture_contract_failed",
            },
            "ingest_into_longhouse": {
                "status": "pass" if checks.get("basic_session_fixture", {}).get("status") == "pass" else "fail",
                "level": "fixture",
                "canary": "basic_session_fixture",
                "failure_code": None
                if checks.get("basic_session_fixture", {}).get("status") == "pass"
                else "gemini_parser_fixture_contract_failed",
            },
            "tool_tool_result_shape": {
                "status": "pass" if checks.get("tool_result_fixture", {}).get("status") == "pass" else "fail",
                "level": "fixture",
                "canary": "tool_result_fixture",
                "failure_code": None
                if checks.get("tool_result_fixture", {}).get("status") == "pass"
                else "gemini_parser_fixture_contract_failed",
            },
            "timeline_session_projection": {
                "status": "pass" if fixture_projection_ok else "fail",
                "level": "fixture",
                "canary": "gemini_fixture_sessions",
                "failure_code": None if fixture_projection_ok else failure_code,
            },
        },
        "session_projection": session_projection,
    }
    _write_json(source_path, source)
    return (
        source,
        {
            "source_artifact": str(source_path),
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
        },
        None,
    )


def _run_source_canary(args: argparse.Namespace, raw_dir: Path) -> tuple[dict[str, Any], dict[str, str], int | None]:
    raw_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = raw_dir / "stdout.log"
    stderr_path = raw_dir / "stderr.log"

    if args.provider == "gemini":
        return _run_gemini_parser_fixture_source(args, raw_dir)

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
        if args.codex_run_fake_app_server:
            argv.append("--run-fake-app-server")
        if args.codex_run_raw_fresh_remote:
            argv.append("--run-raw-fresh-remote")
        if args.codex_run_managed_tui_attach:
            argv.append("--run-managed-tui-attach")
        if args.codex_run_detached_ui:
            argv.append("--run-detached-ui")
    else:
        raise ValueError(f"unsupported provider: {args.provider}")

    raw_artifacts = {
        "source_artifact": str(source_path),
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
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
                        "command": _command_evidence(result),
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
                    "command": _command_evidence(result),
                }
            },
            "operation_evidence": {},
        }
        _write_json(source_path, source)
    return source, raw_artifacts, result.returncode


def run_provider_release_proof(args: argparse.Namespace) -> dict[str, Any]:
    args.repo_root = args.repo_root.expanduser().resolve()
    args.evidence_root = (args.evidence_root or Path.cwd() / "provider-release-proof-evidence").expanduser()
    args.artifact = (args.artifact or args.evidence_root / "provider-release-proof.json").expanduser()
    if args.provider_bin is not None:
        args.provider_bin = args.provider_bin.expanduser()

    raw_dir = args.evidence_root / "raw"
    normalized_dir = args.evidence_root / "normalized"
    normalized_dir.mkdir(parents=True, exist_ok=True)
    source_artifact, raw_artifacts, returncode = _run_source_canary(args, raw_dir)
    normalized = _normalize_source_artifact(source_artifact)
    normalized_path = normalized_dir / "contract.json"
    _write_json(normalized_path, normalized)

    verdict, failure_code, recommendation = _classify(source_artifact)
    contract = _load_provider_contract(args.repo_root, args.provider)
    contract_operations = dict(contract.get("operation_evidence") or {}) if contract else {}
    provider_version = (
        args.provider_version
        or normalized.get("provider_version")
        or source_artifact.get("provider_version")
        or source_artifact.get("codex_version")
    )
    provider_contract_path = normalized_dir / "provider_contract.json"
    operation_evidence_path = normalized_dir / "operation_evidence.json"
    session_projection_path = normalized_dir / "session_projection.json"
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
    _write_json(provider_contract_path, provider_contract)
    _write_json(operation_evidence_path, operation_evidence_artifact)
    _write_json(session_projection_path, session_projection)
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "provider_release_proof",
        "provider": args.provider,
        "provider_version": provider_version,
        "generated_at": _now_iso(),
        "scenario_id": f"{args.provider}-release-proof-v1",
        "scenario_version": 1,
        "verdict": verdict,
        "failure_code": failure_code,
        "recommendation": recommendation,
        "source_canary_returncode": returncode,
        "canaries": {
            "source_canary": {
                "status": "pass" if verdict == "green" else "fail" if verdict == "red" else "warn",
                "verdict": source_artifact.get("verdict"),
                "failure_code": source_artifact.get("failure_code"),
                "artifact_path": raw_artifacts["source_artifact"],
            }
        },
        "operation_evidence": normalized.get("operation_evidence") or {},
        "normalized": normalized,
        "contract_operations": contract_operations,
        "artifacts": {
            **raw_artifacts,
            "normalized_contract": str(normalized_path),
            "provider_contract": str(provider_contract_path),
            "operation_evidence": str(operation_evidence_path),
            "session_projection": str(session_projection_path),
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
    parser.add_argument("--codex-run-fake-app-server", action="store_true")
    parser.add_argument("--codex-run-raw-fresh-remote", action="store_true")
    parser.add_argument("--codex-run-managed-tui-attach", action="store_true")
    parser.add_argument("--codex-run-detached-ui", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    artifact = run_provider_release_proof(args)
    if args.json:
        print(json.dumps(artifact, indent=2, sort_keys=True))
    else:
        print(f"provider release proof: {artifact['provider']} {artifact['verdict']}")
        print(f"artifact: {artifact['artifact_path']}")
        print(f"evidence_root: {artifact['artifacts']['evidence_root']}")
        if artifact.get("failure_code"):
            print(f"failure_code: {artifact['failure_code']}")
    return 1 if artifact["verdict"] == "red" else 0


if __name__ == "__main__":
    raise SystemExit(main())
