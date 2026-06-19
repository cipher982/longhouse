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
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SUPPORTED_PROVIDERS = ("claude", "codex", "opencode", "antigravity")
LIVE_CANARY_PROVIDERS = frozenset({"claude", "opencode", "antigravity"})
CODEX_API_URL_ENV = "CODEX_API_URL"
CODEX_AGENTS_TOKEN_ENV = "CODEX_AGENTS_TOKEN"
ANTIGRAVITY_BIN_ENV = "LONGHOUSE_ANTIGRAVITY_BIN"


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
    return "yellow", "provider_release_proof_unknown_verdict", "investigate_before_upgrade"


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
        if item in {"--agents-token", "--codex-agents-token"}:
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


def _run_source_canary(args: argparse.Namespace, raw_dir: Path) -> tuple[dict[str, Any], dict[str, str], int | None]:
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


def _merge_antigravity_real_send_proof(source: dict[str, Any], control: dict[str, Any]) -> dict[str, Any]:
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
    for operation, evidence in dict(antigravity.get("operation_evidence") or {}).items():
        if isinstance(operation, str) and isinstance(evidence, dict):
            operation_evidence[operation] = evidence
    merged["canaries"] = canaries
    merged["operation_evidence"] = operation_evidence
    if antigravity.get("status") == "fail":
        merged["verdict"] = "red"
        merged["failure_code"] = str(antigravity.get("failure_code") or "antigravity_real_agy_send_failed")
        merged["recommendation"] = "block_upgrade_recommendation"
    return merged


def _fail_control_canary(code: str, message: str, **fields: Any) -> dict[str, Any]:
    payload = {"status": "fail", "failure_code": code, "message": message}
    payload.update(fields)
    return payload


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


def run_provider_release_proof(args: argparse.Namespace) -> dict[str, Any]:
    args.repo_root = args.repo_root.expanduser().resolve()
    args.evidence_root = (args.evidence_root or Path.cwd() / "provider-release-proof-evidence").expanduser()
    args.artifact = (args.artifact or args.evidence_root / "provider-release-proof.json").expanduser()
    if args.provider_bin is not None:
        args.provider_bin = args.provider_bin.expanduser()
    if args.provider == "codex":
        args.codex_api_url = args.codex_api_url or os.getenv(CODEX_API_URL_ENV)
        args.codex_agents_token = args.codex_agents_token or os.getenv(CODEX_AGENTS_TOKEN_ENV)

    raw_dir = args.evidence_root / "raw"
    normalized_dir = args.evidence_root / "normalized"
    normalized_dir.mkdir(parents=True, exist_ok=True)
    source_artifact, raw_artifacts, returncode = _run_source_canary(args, raw_dir)
    if args.provider == "antigravity" and args.antigravity_run_real_agy_send:
        control_artifact, control_artifacts, control_returncode = _run_antigravity_real_send_proof(args, raw_dir)
        raw_artifacts.update(control_artifacts)
        source_artifact = _merge_antigravity_real_send_proof(source_artifact, control_artifact)
        returncode = returncode or control_returncode
    normalized = _normalize_source_artifact(source_artifact)
    normalized_path = normalized_dir / "contract.json"
    _write_json(normalized_path, normalized)

    verdict, failure_code, recommendation = _classify(
        source_artifact,
        source_canary_returncode=returncode,
    )
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
    parser.add_argument("--codex-api-url")
    parser.add_argument("--codex-agents-token")
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
        print(f"artifact: {artifact['artifact_path']}")
        print(f"evidence_root: {artifact['artifacts']['evidence_root']}")
        if artifact.get("failure_code"):
            print(f"failure_code: {artifact['failure_code']}")
    return 1 if artifact["verdict"] == "red" else 0


if __name__ == "__main__":
    raise SystemExit(main())
