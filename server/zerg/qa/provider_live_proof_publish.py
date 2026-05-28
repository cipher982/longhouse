#!/usr/bin/env python3
"""Publish stable local provider live-proof sidecars.

This is the dogfood-machine owner for real upstream provider operation proof.
It runs the packaged provider live canary, keeps timestamped evidence, and
atomically publishes the latest matching artifact to LONGHOUSE_PROVIDER_LIVE_PROOF_DIR
for local-health to consume.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from zerg.provider_live_proof import configured_provider_live_proof_dir
from zerg.qa.provider_live_canary import default_repo_root
from zerg.qa.provider_live_canary import run_provider_live_canary
from zerg.services.longhouse_paths import resolve_longhouse_home

DEFAULT_PROVIDERS = ("claude", "opencode", "antigravity")
SUPPORTED_PROVIDERS = ("codex", *DEFAULT_PROVIDERS)
PROVIDER_STATUS_SCHEMA_VERSION = 1
LIVE_PROOF_ARTIFACT_KIND = "provider_live_canary"
CANARY_SCRIPT_HELP = "Debug/test override for the provider-live canary executable."


def _default_proof_dir() -> Path:
    return configured_provider_live_proof_dir()


def _source_checkout_root(repo_root: Path) -> bool:
    contract_path = repo_root / "server/zerg/config/managed_provider_contracts.json"
    return contract_path.exists() and (repo_root / "scripts/qa").exists()


def _default_evidence_base(repo_root: Path) -> Path:
    if _source_checkout_root(repo_root):
        return repo_root / ".build/canaries/provider-live"
    return resolve_longhouse_home() / "canaries/provider-live"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _read_artifact(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_artifact(path: Path, artifact: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _fallback_artifact(
    *,
    provider: str,
    artifact_path: Path,
    evidence_root: Path,
    returncode: int | None,
    stdout: str = "",
    stderr: str = "",
    failure_code: str,
    message: str,
) -> dict[str, Any]:
    return {
        "schema_version": PROVIDER_STATUS_SCHEMA_VERSION,
        "artifact_kind": LIVE_PROOF_ARTIFACT_KIND,
        "provider": provider,
        "provider_version": None,
        "generated_at": _now_iso(),
        "verdict": "red",
        "failure_code": failure_code,
        "recommendation": "investigate_before_managed_use",
        "artifact_path": str(artifact_path),
        "canaries": {
            "provider_live_canary": {
                "status": "fail",
                "failure_code": failure_code,
                "message": message,
                "artifact_path": str(artifact_path),
                "returncode": returncode,
                "stdout": stdout[-4000:],
                "stderr": stderr[-4000:],
            }
        },
        "evidence_root": str(evidence_root),
    }


def _publish_result(
    *,
    provider: str,
    artifact: dict[str, Any],
    artifact_path: Path,
    stable_path: Path,
    returncode: int | None,
    used_fallback: bool = False,
) -> dict[str, Any]:
    _write_artifact(stable_path, artifact)
    return {
        "provider": provider,
        "status": "published_fallback" if used_fallback else "published",
        "returncode": returncode,
        "verdict": artifact.get("verdict"),
        "failure_code": artifact.get("failure_code"),
        "artifact_path": str(artifact.get("artifact_path") or artifact_path),
        "stable_path": str(stable_path),
    }


def _run_canary_script(
    *,
    args: argparse.Namespace,
    provider: str,
    canary_script: Path,
    evidence_root: Path,
    artifact_path: Path,
) -> tuple[int | None, dict[str, Any], bool]:
    if not canary_script.exists():
        artifact = _fallback_artifact(
            provider=provider,
            artifact_path=artifact_path,
            evidence_root=evidence_root,
            returncode=None,
            failure_code="live_canary_script_missing",
            message=f"provider live canary script missing at {canary_script}",
        )
        _write_artifact(artifact_path, artifact)
        return None, artifact, True

    argv = [
        sys.executable,
        str(canary_script),
        "--repo-root",
        str(args.repo_root),
        "--provider",
        provider,
        "--evidence-root",
        str(evidence_root),
        "--artifact",
        str(artifact_path),
        "--json",
    ]
    try:
        result = subprocess.run(
            argv,
            cwd=str(args.repo_root),
            text=True,
            capture_output=True,
            check=False,
            timeout=args.timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        artifact = _fallback_artifact(
            provider=provider,
            artifact_path=artifact_path,
            evidence_root=evidence_root,
            returncode=None,
            stdout=str(exc.stdout or ""),
            stderr=str(exc.stderr or ""),
            failure_code="live_canary_timeout",
            message=f"provider live canary timed out after {args.timeout_s}s",
        )
        _write_artifact(artifact_path, artifact)
        return None, artifact, True

    artifact = _read_artifact(artifact_path)
    if artifact is None:
        artifact = _fallback_artifact(
            provider=provider,
            artifact_path=artifact_path,
            evidence_root=evidence_root,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            failure_code="live_canary_failed_to_emit_artifact",
            message=f"provider live canary exited {result.returncode} without writing artifact",
        )
        _write_artifact(artifact_path, artifact)
        return result.returncode, artifact, True
    return result.returncode, artifact, False


def _run_packaged_canary(
    *,
    args: argparse.Namespace,
    provider: str,
    evidence_root: Path,
    artifact_path: Path,
) -> tuple[int | None, dict[str, Any], bool]:
    try:
        artifact = run_provider_live_canary(
            {
                "repo_root": args.repo_root,
                "provider": provider,
                "provider_bin": None,
                "artifact": artifact_path,
                "evidence_root": evidence_root,
                "wait_ready_secs": args.wait_ready_secs,
                "json": False,
            }
        )
    except Exception as exc:  # noqa: BLE001
        artifact = _fallback_artifact(
            provider=provider,
            artifact_path=artifact_path,
            evidence_root=evidence_root,
            returncode=None,
            failure_code="live_canary_exception",
            message=f"{type(exc).__name__}: {exc}",
        )
        _write_artifact(artifact_path, artifact)
        return None, artifact, True
    returncode = 1 if artifact.get("verdict") == "red" else 0
    return returncode, artifact, False


def _publish_provider(args: argparse.Namespace, provider: str, run_timestamp: str) -> dict[str, Any]:
    evidence_root = args.evidence_root / provider / run_timestamp
    artifact_path = evidence_root / "provider-live-canary.json"
    stable_path = args.proof_dir / f"{provider}.json"
    evidence_root.mkdir(parents=True, exist_ok=True)

    if args.canary_script is None:
        returncode, artifact, used_fallback = _run_packaged_canary(
            args=args,
            provider=provider,
            evidence_root=evidence_root,
            artifact_path=artifact_path,
        )
    else:
        returncode, artifact, used_fallback = _run_canary_script(
            args=args,
            provider=provider,
            canary_script=args.canary_script,
            evidence_root=evidence_root,
            artifact_path=artifact_path,
        )

    return _publish_result(
        provider=provider,
        artifact=artifact,
        artifact_path=artifact_path,
        stable_path=stable_path,
        returncode=returncode,
        used_fallback=used_fallback,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=default_repo_root())
    parser.add_argument(
        "--provider",
        action="append",
        choices=list(SUPPORTED_PROVIDERS),
        help="Provider to prove. Repeat to run more than one. Defaults to all non-Codex shared live canaries.",
    )
    parser.add_argument("--proof-dir", type=Path, default=None)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--canary-script", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--wait-ready-secs", type=float, default=15.0)
    parser.add_argument("--timeout-s", type=float, default=120.0)
    parser.add_argument("--json", action="store_true")
    return parser


def run_provider_live_proof_publish(args: argparse.Namespace | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(args, Mapping):
        args = argparse.Namespace(**dict(args))
    else:
        args = argparse.Namespace(**vars(args))
    args.repo_root = Path(args.repo_root).expanduser().resolve()
    args.proof_dir = args.proof_dir or _default_proof_dir()
    args.proof_dir = args.proof_dir.expanduser().resolve()
    args.evidence_root = (args.evidence_root or _default_evidence_base(args.repo_root)).expanduser().resolve()
    args.canary_script = None if args.canary_script is None else Path(args.canary_script).expanduser().resolve()
    providers = tuple(args.provider or DEFAULT_PROVIDERS)
    run_timestamp = _timestamp()

    results = [_publish_provider(args, provider, run_timestamp) for provider in providers]
    payload = {
        "schema_version": PROVIDER_STATUS_SCHEMA_VERSION,
        "artifact_kind": "provider_live_proof_publish",
        "generated_at": _now_iso(),
        "proof_dir": str(args.proof_dir),
        "providers": list(providers),
        "results": results,
    }
    return payload


def publish_exit_code(payload: dict[str, Any]) -> int:
    for result in payload.get("results") or []:
        if result.get("status") == "published_fallback":
            return 1
        if result.get("verdict") == "red":
            return 1
        if result.get("returncode") not in (0, None):
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    payload = run_provider_live_proof_publish(args)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return publish_exit_code(payload)


if __name__ == "__main__":
    raise SystemExit(main())
