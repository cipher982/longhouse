#!/usr/bin/env python3
"""Publish stable local provider live-proof sidecars.

This is the dogfood-machine owner for real upstream provider operation proof.
It shells out to the repo wrapper for the packaged `longhouse provider-live
canary` command, keeps timestamped evidence under the repo build directory, and
atomically publishes the latest matching artifact to LONGHOUSE_PROVIDER_LIVE_PROOF_DIR
for local-health to consume.
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

DEFAULT_PROVIDERS = ("claude", "opencode", "antigravity")
PROVIDER_STATUS_SCHEMA_VERSION = 1
LIVE_PROOF_ARTIFACT_KIND = "provider_live_canary"
LIVE_PROOF_DIR_ENV = "LONGHOUSE_PROVIDER_LIVE_PROOF_DIR"


def _repo_root_from_script() -> Path:
    return Path(__file__).resolve().parents[2]


def _fallback_default_proof_dir() -> Path:
    env_proof_dir = os.getenv(LIVE_PROOF_DIR_ENV, "").strip()
    if env_proof_dir:
        return Path(env_proof_dir).expanduser()
    longhouse_home = os.getenv("LONGHOUSE_HOME", "").strip()
    if longhouse_home:
        return Path(longhouse_home).expanduser() / "provider-live-proof"
    return Path.home() / ".longhouse" / "provider-live-proof"


def _default_proof_dir() -> Path:
    server_path = str(_repo_root_from_script() / "server")
    inserted = False
    if server_path not in sys.path:
        sys.path.insert(0, server_path)
        inserted = True
    try:
        from zerg.provider_live_proof import configured_provider_live_proof_dir
    except ImportError:
        return _fallback_default_proof_dir()
    finally:
        if inserted:
            try:
                sys.path.remove(server_path)
            except ValueError:
                pass
    return configured_provider_live_proof_dir()


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


def _publish_provider(args: argparse.Namespace, provider: str, run_timestamp: str) -> dict[str, Any]:
    live_script = args.repo_root / "scripts" / "qa" / "provider-live-canary.py"
    evidence_root = args.evidence_root / provider / run_timestamp
    artifact_path = evidence_root / "provider-live-canary.json"
    stable_path = args.proof_dir / f"{provider}.json"
    evidence_root.mkdir(parents=True, exist_ok=True)

    if not live_script.exists():
        artifact = _fallback_artifact(
            provider=provider,
            artifact_path=artifact_path,
            evidence_root=evidence_root,
            returncode=None,
            failure_code="live_canary_script_missing",
            message=f"provider-live-canary.py missing at {live_script}",
        )
        _write_artifact(artifact_path, artifact)
        _write_artifact(stable_path, artifact)
        return {
            "provider": provider,
            "status": "published_fallback",
            "returncode": None,
            "verdict": artifact["verdict"],
            "failure_code": artifact["failure_code"],
            "artifact_path": str(artifact_path),
            "stable_path": str(stable_path),
        }

    argv = [
        sys.executable,
        str(live_script),
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
        _write_artifact(stable_path, artifact)
        return {
            "provider": provider,
            "status": "published_fallback",
            "returncode": None,
            "verdict": artifact["verdict"],
            "failure_code": artifact["failure_code"],
            "artifact_path": str(artifact_path),
            "stable_path": str(stable_path),
        }

    artifact = _read_artifact(artifact_path)
    used_fallback = artifact is None
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

    _write_artifact(stable_path, artifact)
    return {
        "provider": provider,
        "status": "published_fallback" if used_fallback else "published",
        "returncode": result.returncode,
        "verdict": artifact.get("verdict"),
        "failure_code": artifact.get("failure_code"),
        "artifact_path": str(artifact_path),
        "stable_path": str(stable_path),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=_repo_root_from_script())
    parser.add_argument(
        "--provider",
        action="append",
        choices=["claude", "opencode", "antigravity"],
        help="Provider to prove. Repeat to run more than one. Defaults to all non-Codex shared live canaries.",
    )
    parser.add_argument("--proof-dir", type=Path, default=None)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--timeout-s", type=float, default=120.0)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.repo_root = args.repo_root.resolve()
    args.proof_dir = args.proof_dir or _default_proof_dir()
    args.proof_dir = args.proof_dir.expanduser().resolve()
    args.evidence_root = (args.evidence_root or args.repo_root / ".build/canaries/provider-live").resolve()
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
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 1 if any(result.get("status") == "published_fallback" or result.get("returncode") not in (0, None) for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
