"""Local provider live-proof artifacts consumed by local health.

Sauron owns provider release-status verdicts. Local machines with real provider
CLIs can separately prove operation behavior for the installed version. This
module keeps that proof as an independent feed so release readiness and local
operation evidence do not collapse into one envelope.
"""

from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from zerg.provider_release_status import PROVIDER_STATUS_SCHEMA_VERSION
from zerg.provider_release_status import _max_artifact_age_seconds
from zerg.provider_release_status import _normalize_operation_evidence
from zerg.provider_release_status import _parse_rfc3339
from zerg.provider_release_status import _provider_version_from_cli
from zerg.provider_release_status import normalize_provider_version
from zerg.services.longhouse_paths import get_provider_live_proof_dir
from zerg.services.managed_provider_contracts import managed_provider_names

LIVE_PROOF_ARTIFACT_KIND = "provider_live_canary"
SUPPORTED_LIVE_PROOF_PROVIDERS = ("claude", "opencode", "antigravity")


def _read_json_file(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "missing"
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(payload, dict):
        return None, "artifact root is not an object"
    return payload, None


def _proof_file_candidates(provider: str, *, base_dir: Path | None = None) -> list[Path]:
    return [get_provider_live_proof_dir(base_dir) / f"{provider}.json"]


def _load_live_proof_artifact(
    provider: str,
    *,
    base_dir: Path | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    for path in _proof_file_candidates(provider, base_dir=base_dir):
        payload, error = _read_json_file(path)
        attempts.append({"source": "file", "path": str(path), "error": error})
        if payload is not None:
            return payload, {
                "source": "file",
                "path": str(path),
                "attempts": attempts,
            }
    return None, {"source": "none", "attempts": attempts}


def _freshness(generated_at: Any) -> tuple[str, int | None]:
    generated_at_dt = _parse_rfc3339(generated_at)
    if generated_at_dt is None:
        return "missing", None
    age_seconds = int((datetime.now(UTC) - generated_at_dt).total_seconds())
    if age_seconds > _max_artifact_age_seconds():
        return "stale", age_seconds
    return "fresh", age_seconds


def _version_match_state(
    *,
    normalized_current: str | None,
    normalized_artifact: str | None,
    version_error: str | None,
) -> str:
    if version_error:
        return "unknown_local"
    if not normalized_current:
        return "unknown_local"
    if not normalized_artifact:
        return "unknown_artifact"
    if normalized_current == normalized_artifact:
        return "match"
    return "mismatch"


def _status_for_provider(
    provider: str,
    provider_cli: dict[str, Any],
    *,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    artifact, source = _load_live_proof_artifact(provider, base_dir=base_dir)
    configured = bool(source.get("attempts"))
    if artifact is None:
        attempts = list(source.get("attempts") or [])
        missing_only = bool(attempts) and all(attempt.get("error") == "missing" for attempt in attempts)
        status = "not_configured" if not attempts or missing_only else "unavailable"
        return {
            "provider": provider,
            "configured": configured and not missing_only,
            "status": status,
            "applies": False,
            "source": source,
        }

    schema_version = artifact.get("schema_version")
    schema_status = "ok" if schema_version == PROVIDER_STATUS_SCHEMA_VERSION else "mismatch"
    artifact_kind = str(artifact.get("artifact_kind") or "").strip()
    artifact_kind_status = "ok" if artifact_kind == LIVE_PROOF_ARTIFACT_KIND else "mismatch"
    artifact_provider = str(artifact.get("provider") or "").strip().lower()
    provider_status = "ok" if artifact_provider == provider else "mismatch"
    generated_at = artifact.get("generated_at")
    freshness_status, generated_at_age_seconds = _freshness(generated_at)

    current_version, version_error = _provider_version_from_cli(provider_cli.get("path"))
    artifact_version = artifact.get("provider_version")
    normalized_current = normalize_provider_version(current_version)
    normalized_artifact = normalize_provider_version(artifact_version)
    version_match = _version_match_state(
        normalized_current=normalized_current,
        normalized_artifact=normalized_artifact,
        version_error=version_error,
    )

    if schema_status != "ok":
        status = "schema_mismatch"
    elif artifact_kind_status != "ok":
        status = "artifact_kind_mismatch"
    elif provider_status != "ok":
        status = "provider_mismatch"
    elif freshness_status != "fresh":
        status = "stale"
    elif version_match == "match":
        status = "ok"
    elif version_match == "mismatch":
        status = "version_mismatch"
    elif version_match == "unknown_local":
        status = "unknown_local_version"
    else:
        status = "unknown_artifact_version"

    applies = (status, schema_status, artifact_kind_status, provider_status, version_match) == (
        "ok",
        "ok",
        "ok",
        "ok",
        "match",
    )

    return {
        "provider": provider,
        "configured": True,
        "status": status,
        "applies": applies,
        "artifact_schema_version": schema_version,
        "schema_status": schema_status,
        "artifact_kind": artifact_kind,
        "artifact_kind_status": artifact_kind_status,
        "artifact_provider": artifact_provider,
        "artifact_provider_status": provider_status,
        "artifact_version": artifact_version,
        "current_version": current_version,
        "normalized_artifact_version": normalized_artifact,
        "normalized_current_version": normalized_current,
        "version_match": version_match,
        "version_error": version_error,
        "generated_at": generated_at,
        "generated_at_age_seconds": generated_at_age_seconds,
        "freshness_status": freshness_status,
        "verdict": artifact.get("verdict"),
        "failure_code": artifact.get("failure_code"),
        "recommendation": artifact.get("recommendation"),
        "operation_evidence": _normalize_operation_evidence(artifact.get("operation_evidence")),
        "evidence_root": artifact.get("evidence_root"),
        "source": source,
    }


def collect_provider_live_proof(
    provider_clis: dict[str, Any],
    *,
    fast: bool = False,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    if fast:
        return {
            "schema_version": PROVIDER_STATUS_SCHEMA_VERSION,
            "enabled": False,
            "skipped_reason": "fast_local_health",
            "statuses": {},
        }

    statuses: dict[str, Any] = {}
    providers = sorted((set(provider_clis) | set(managed_provider_names())) & set(SUPPORTED_LIVE_PROOF_PROVIDERS))
    for provider in providers:
        statuses[provider] = _status_for_provider(
            provider,
            dict(provider_clis.get(provider) or {}),
            base_dir=base_dir,
        )

    return {
        "schema_version": PROVIDER_STATUS_SCHEMA_VERSION,
        "enabled": any(item.get("configured") for item in statuses.values()),
        "statuses": statuses,
    }


__all__ = ["LIVE_PROOF_ARTIFACT_KIND", "collect_provider_live_proof"]
