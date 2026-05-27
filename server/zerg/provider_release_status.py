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
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.request import Request
from urllib.request import urlopen

PROVIDER_RELEASE_STATUS_DIR_ENV = "LONGHOUSE_PROVIDER_RELEASE_STATUS_DIR"
PROVIDER_RELEASE_STATUS_URL_ENV = "LONGHOUSE_PROVIDER_RELEASE_STATUS_URL"
CODEX_RELEASE_STATUS_FILE_ENV = "LONGHOUSE_CODEX_RELEASE_STATUS_FILE"
CODEX_RELEASE_STATUS_URL_ENV = "LONGHOUSE_CODEX_RELEASE_STATUS_URL"
PROVIDER_STATUS_SCHEMA_VERSION = 1
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?")


def normalize_provider_version(raw: Any) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    match = _VERSION_RE.search(text)
    return match.group(0) if match else text


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
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(payload, dict):
        return None, "artifact root is not an object"
    return payload, None


def _provider_file_candidates(provider: str) -> list[Path]:
    candidates: list[Path] = []
    if provider == "codex":
        raw = os.getenv(CODEX_RELEASE_STATUS_FILE_ENV)
        if raw:
            candidates.append(Path(raw).expanduser())
    status_dir = os.getenv(PROVIDER_RELEASE_STATUS_DIR_ENV)
    if status_dir:
        root = Path(status_dir).expanduser()
        candidates.append(root / f"{provider}.json")
        candidates.append(root / f"{provider}-latest.json")
    return candidates


def _provider_url_candidates(provider: str) -> list[str]:
    candidates: list[str] = []
    if provider == "codex":
        raw = os.getenv(CODEX_RELEASE_STATUS_URL_ENV)
        if raw:
            candidates.append(raw)
    raw = os.getenv(PROVIDER_RELEASE_STATUS_URL_ENV)
    if raw:
        candidates.append(raw.format(provider=provider) if "{provider}" in raw else raw.rstrip("/") + f"/{provider}")
    return candidates


def _load_provider_artifact(provider: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    for path in _provider_file_candidates(provider):
        payload, error = _read_json_file(path)
        attempts.append({"source": "file", "path": str(path), "error": error})
        if payload is not None:
            return payload, {"source": "file", "path": str(path), "attempts": attempts}
    for url in _provider_url_candidates(provider):
        payload, error = _read_json_url(url)
        attempts.append({"source": "url", "url": url, "error": error})
        if payload is not None:
            return payload, {"source": "url", "url": url, "attempts": attempts}
    return None, {"source": "none", "attempts": attempts}


def _provider_version_from_cli(path: str | None) -> tuple[str | None, str | None]:
    if not path:
        return None, "provider CLI path missing"
    try:
        result = subprocess.run(
            [path, "--version"],
            text=True,
            capture_output=True,
            timeout=2.0,
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
        return {
            "provider": provider,
            "configured": bool(source.get("attempts")),
            "status": "not_configured" if not source.get("attempts") else "unavailable",
            "source": source,
        }

    current_version, version_error = _provider_version_from_cli(provider_cli.get("path"))
    artifact_version = artifact.get("codex_version") or artifact.get("provider_version")
    normalized_current = normalize_provider_version(current_version)
    normalized_artifact = normalize_provider_version(artifact_version)
    local_version_matches = bool(normalized_current and normalized_artifact and normalized_current == normalized_artifact)
    verdict = str(artifact.get("verdict") or "unknown").lower()

    if verdict == "red" and local_version_matches:
        status = "blocked"
    elif verdict == "yellow" and local_version_matches:
        status = "caution"
    elif verdict in {"green", "yellow", "red"}:
        status = "ok"
    else:
        status = "unknown"

    return {
        "provider": provider,
        "configured": True,
        "status": status,
        "verdict": verdict,
        "failure_code": artifact.get("failure_code"),
        "recommendation": artifact.get("recommendation"),
        "artifact_version": artifact_version,
        "current_version": current_version,
        "normalized_artifact_version": normalized_artifact,
        "normalized_current_version": normalized_current,
        "local_version_matches": local_version_matches,
        "version_error": version_error,
        "generated_at": artifact.get("generated_at"),
        "evidence_root": artifact.get("evidence_root"),
        "source": source,
    }


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
    for provider in ("codex",):
        statuses[provider] = _status_for_provider(provider, dict(provider_clis.get(provider) or {}))

    blocking_count = sum(1 for item in statuses.values() if item.get("status") == "blocked")
    warning_count = sum(1 for item in statuses.values() if item.get("status") == "caution")
    enabled = any(item.get("configured") for item in statuses.values())
    return {
        "schema_version": PROVIDER_STATUS_SCHEMA_VERSION,
        "enabled": enabled,
        "statuses": statuses,
        "blocking_count": blocking_count,
        "warning_count": warning_count,
    }
