"""Provider release-status artifacts consumed by local health.

Sauron owns the release watcher and publishes one latest status artifact per
provider. Longhouse treats those artifacts as advisory release-safety signals;
they do not replace local process/runtime truth.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.request import Request
from urllib.request import urlopen

from zerg.services.managed_provider_contracts import managed_provider_names

PROVIDER_RELEASE_STATUS_DIR_ENV = "LONGHOUSE_PROVIDER_RELEASE_STATUS_DIR"
PROVIDER_RELEASE_STATUS_URL_ENV = "LONGHOUSE_PROVIDER_RELEASE_STATUS_URL"
CODEX_RELEASE_STATUS_FILE_ENV = "LONGHOUSE_CODEX_RELEASE_STATUS_FILE"
CODEX_RELEASE_STATUS_URL_ENV = "LONGHOUSE_CODEX_RELEASE_STATUS_URL"
PROVIDER_STATUS_SCHEMA_VERSION = 1
DEFAULT_PROVIDER_RELEASE_STATUS_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")


def normalize_provider_version(raw: Any) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    match = _VERSION_RE.search(text)
    return match.group(0) if match else text


def _version_tuple(version: str | None) -> tuple[int, int, int] | None:
    if version is None or not _VERSION_RE.fullmatch(version):
        return None
    major, minor, patch = version.split(".", 2)
    return int(major), int(minor), int(patch)


def _compare_versions(left: str | None, right: str | None) -> int | None:
    left_tuple = _version_tuple(left)
    right_tuple = _version_tuple(right)
    if left_tuple is None or right_tuple is None:
        return None
    return (left_tuple > right_tuple) - (left_tuple < right_tuple)


def _env_value(key: str) -> str | None:
    raw = os.getenv(key, "").strip()
    return raw or None


def _max_artifact_age_seconds() -> int:
    return DEFAULT_PROVIDER_RELEASE_STATUS_MAX_AGE_SECONDS


def _parse_rfc3339(raw: Any) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


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


def _read_json_url(url: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        with urlopen(Request(url, method="GET"), timeout=1.5) as response:
            raw = response.read(512 * 1024)
    except HTTPError as exc:
        if exc.code == 404:
            return None, "missing"
        return None, f"{type(exc).__name__}: {exc}"
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(payload, dict):
        return None, "artifact root is not an object"
    return payload, None


def _provider_file_candidates(provider: str) -> list[tuple[Path, bool]]:
    candidates: list[tuple[Path, bool]] = []
    if provider == "codex":
        raw = _env_value(CODEX_RELEASE_STATUS_FILE_ENV)
        if raw:
            candidates.append((Path(raw).expanduser(), True))
    status_dir = _env_value(PROVIDER_RELEASE_STATUS_DIR_ENV)
    if status_dir:
        root = Path(status_dir).expanduser()
        candidates.append((root / f"{provider}.json", False))
        candidates.append((root / f"{provider}-latest.json", False))
    return candidates


def _provider_url_candidates(provider: str) -> list[str]:
    candidates: list[str] = []
    if provider == "codex":
        raw = _env_value(CODEX_RELEASE_STATUS_URL_ENV)
        if raw:
            candidates.append(raw)
    raw = _env_value(PROVIDER_RELEASE_STATUS_URL_ENV)
    if raw:
        candidates.append(raw.format(provider=provider) if "{provider}" in raw else raw.rstrip("/") + f"/{provider}")
    return candidates


def _load_provider_artifact(provider: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    for path, required in _provider_file_candidates(provider):
        payload, error = _read_json_file(path)
        attempts.append({"source": "file", "path": str(path), "error": error, "required": required})
        if payload is not None:
            return _normalize_provider_artifact(provider, payload), {
                "source": "file",
                "path": str(path),
                "attempts": attempts,
            }
    for url in _provider_url_candidates(provider):
        payload, error = _read_json_url(url)
        attempts.append({"source": "url", "url": url, "error": error})
        if payload is not None:
            return _normalize_provider_artifact(provider, payload), {"source": "url", "url": url, "attempts": attempts}
    return None, {"source": "none", "attempts": attempts}


def _normalize_provider_artifact(provider: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Accept raw artifacts and Sauron API envelopes.

    The jobs pack writes raw JSON artifacts, while the Sauron runtime exposes
    them as ``{"provider": "...", "artifact": {...}}`` for API clarity.
    Local health should classify the inner artifact, not the transport wrapper.
    """
    embedded = payload.get("artifact")
    if isinstance(embedded, dict) and str(payload.get("provider") or "").strip().lower() == provider:
        return dict(embedded)
    return payload


def _normalize_operation_evidence(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}

    evidence: dict[str, dict[str, Any]] = {}
    for operation, raw_info in raw.items():
        if not isinstance(operation, str) or not operation.strip() or not isinstance(raw_info, dict):
            continue
        info = {
            key: value
            for key, value in raw_info.items()
            if key
            in {
                "status",
                "level",
                "source",
                "failure_code",
                "message",
                "generated_at",
                "canary",
                "canaries",
                "next",
            }
        }
        evidence[operation.strip()] = info
    return evidence


def _provider_version_from_cli(path: str | None) -> tuple[str | None, str | None]:
    if not path:
        return None, "provider CLI path missing"
    try:
        result = subprocess.run(
            [path, "--version"],
            text=True,
            capture_output=True,
            timeout=8.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if result.returncode != 0:
        return None, (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
    return (result.stdout or result.stderr).strip(), None


def _status_for_provider(provider: str, provider_cli: dict[str, Any]) -> dict[str, Any]:
    artifact, source = _load_provider_artifact(provider)
    if artifact is None:
        attempts = list(source.get("attempts") or [])
        missing_only = bool(attempts) and all(
            (attempt.get("source") == "file" and attempt.get("required") is False and attempt.get("error") == "missing")
            or (attempt.get("source") == "url" and attempt.get("error") == "missing")
            for attempt in attempts
        )
        has_url_attempt = any(attempt.get("source") == "url" for attempt in attempts)
        configured_without_artifact = missing_only and has_url_attempt
        if configured_without_artifact:
            status = "no_artifact"
        elif not attempts or missing_only:
            status = "not_configured"
        else:
            status = "unavailable"
        return {
            "provider": provider,
            "configured": configured_without_artifact or (bool(attempts) and not missing_only),
            "status": status,
            "risk": "none" if not attempts or missing_only else "warning",
            "source": source,
        }

    schema_version = artifact.get("schema_version")
    schema_status = "ok" if schema_version == PROVIDER_STATUS_SCHEMA_VERSION else "mismatch"
    generated_at = artifact.get("generated_at")
    generated_at_dt = _parse_rfc3339(generated_at)
    generated_at_age_seconds: int | None = None
    freshness_status = "fresh"
    if generated_at_dt is None:
        freshness_status = "missing"
    else:
        generated_at_age_seconds = int((datetime.now(UTC) - generated_at_dt).total_seconds())
        if generated_at_age_seconds > _max_artifact_age_seconds():
            freshness_status = "stale"

    current_version, version_error = _provider_version_from_cli(provider_cli.get("path"))
    artifact_version = artifact.get("codex_version") or artifact.get("provider_version")
    normalized_current = normalize_provider_version(current_version)
    normalized_artifact = normalize_provider_version(artifact_version)
    versions_available = bool(normalized_current and normalized_artifact)
    local_version_matches = versions_available and normalized_current == normalized_artifact
    artifact_version_delta = _compare_versions(normalized_artifact, normalized_current)
    verdict = str(artifact.get("verdict") or "unknown").lower()

    risk = "none"
    if version_error:
        status = "unknown_local_version"
        risk = "none"
    elif verdict == "red" and local_version_matches:
        status = "blocked"
        risk = "blocking"
    elif verdict == "yellow" and local_version_matches:
        status = "caution"
        risk = "warning"
    elif verdict == "green" and local_version_matches:
        status = "ok"
    elif versions_available and not local_version_matches:
        if artifact_version_delta is not None and artifact_version_delta > 0:
            status = "candidate_newer_than_local"
        else:
            status = "unknown_for_current_version"
            risk = "warning"
    else:
        status = "unknown"
        risk = "warning"

    if schema_status != "ok" and risk == "none":
        status = "schema_mismatch"
        risk = "warning"
    if freshness_status != "fresh" and risk == "none":
        status = "stale"
        risk = "warning"

    return {
        "provider": provider,
        "configured": True,
        "status": status,
        "risk": risk,
        "verdict": verdict,
        "failure_code": artifact.get("failure_code"),
        "recommendation": artifact.get("recommendation"),
        "artifact_schema_version": schema_version,
        "schema_status": schema_status,
        "artifact_version": artifact_version,
        "current_version": current_version,
        "normalized_artifact_version": normalized_artifact,
        "normalized_current_version": normalized_current,
        "local_version_matches": local_version_matches,
        "artifact_version_delta": artifact_version_delta,
        "version_error": version_error,
        "generated_at": generated_at,
        "generated_at_age_seconds": generated_at_age_seconds,
        "freshness_status": freshness_status,
        "operation_evidence": _normalize_operation_evidence(artifact.get("operation_evidence")),
        "evidence_root": artifact.get("evidence_root"),
        "source": source,
    }


def _is_blocking_release_status(info: dict[str, Any]) -> bool:
    return info.get("risk") == "blocking" or info.get("status") == "blocked"


def _is_warning_release_status(info: dict[str, Any]) -> bool:
    return info.get("risk") == "warning"


def collect_provider_release_status(
    provider_clis: dict[str, Any],
    *,
    fast: bool = False,
) -> dict[str, Any]:
    if fast:
        return {
            "schema_version": PROVIDER_STATUS_SCHEMA_VERSION,
            "enabled": False,
            "skipped_reason": "fast_local_health",
            "statuses": {},
            "blocking_count": 0,
            "warning_count": 0,
        }

    statuses: dict[str, Any] = {}
    providers = sorted(set(provider_clis) | set(managed_provider_names()))
    for provider in providers:
        statuses[provider] = _status_for_provider(provider, dict(provider_clis.get(provider) or {}))

    blocking_count = sum(1 for item in statuses.values() if _is_blocking_release_status(dict(item or {})))
    warning_count = sum(1 for item in statuses.values() if _is_warning_release_status(dict(item or {})))
    enabled = any(item.get("configured") for item in statuses.values())
    return {
        "schema_version": PROVIDER_STATUS_SCHEMA_VERSION,
        "enabled": enabled,
        "statuses": statuses,
        "blocking_count": blocking_count,
        "warning_count": warning_count,
    }
