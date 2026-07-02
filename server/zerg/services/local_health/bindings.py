from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any

from zerg.services.longhouse_paths import get_agent_db_path

from ._shared import _normalize_optional_string
from ._shared import _parse_rfc3339


def _normalize_binding_path(path: str | None) -> str | None:
    normalized = _normalize_optional_string(path)
    if normalized is None:
        return None
    return str(Path(normalized).expanduser().resolve(strict=False))


def _load_session_binding_rows(base_dir: Path) -> list[dict[str, str | None]]:
    db_path = get_agent_db_path(base_dir)
    if not db_path.exists():
        return []

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
    except sqlite3.Error:
        return []

    try:
        return [
            {
                "path": str(path or ""),
                "session_id": str(session_id or ""),
                "provider": str(provider or ""),
                "updated_at": str(updated_at or ""),
            }
            for path, session_id, provider, updated_at in conn.execute(
                "SELECT path, session_id, provider, updated_at FROM session_binding ORDER BY updated_at DESC"
            )
        ]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _binding_by_session_id(base_dir: Path) -> dict[str, dict[str, str | None]]:
    rows = _load_session_binding_rows(base_dir)
    latest: dict[str, dict[str, str | None]] = {}
    for row in rows:
        session_id = _normalize_optional_string(row.get("session_id"))
        if session_id is None or session_id in latest:
            continue
        latest[session_id] = row
    return latest


def _collect_provider_binding_diagnostics(base_dir: Path, *, now: datetime, fast: bool) -> dict[str, Any]:
    """Summarize recently observed provider-session-binding diagnostics.

    Read-only over the local agent SQLite DB via the same guarded ``mode=ro``
    path as the other local-health readers — deliberately NOT through the ORM
    sessionmaker, and never against hosted state. Reports *observed* diagnostics
    (conflict/missing observation rows), not authoritative current session
    state; see ``provider_binding_diagnostics.py``.

    Returns ``{"status": "skipped"}`` on the fast path and
    ``{"status": "unavailable", ...}`` on any DB error, so consumers never
    confuse "not checked" with "clean".
    """

    if fast:
        return {"status": "skipped", "skipped_reason": "fast"}

    db_path = get_agent_db_path(base_dir)
    if not db_path.exists():
        return {"status": "unavailable", "skipped_reason": "db_missing"}

    cutoff = (now - timedelta(days=7)).astimezone(timezone.utc)

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
    except sqlite3.Error as exc:
        return {"status": "unavailable", "skipped_reason": "db_open_failed", "error": str(exc)}

    try:
        # No SQL time filter: SQLite stores DateTime as 'YYYY-MM-DD HH:MM:SS.ffffff'
        # (space-separated, no tz), which does not lexically compare against an ISO
        # 'T'/'+00:00' cutoff. Binding diagnostics are rare, so filter in Python
        # using the tz-aware parser instead.
        all_rows = conn.execute(
            """
            SELECT kind, provider, session_id, observed_at, payload_json
            FROM session_observations
            WHERE kind IN ('provider_binding_conflict', 'provider_binding_missing')
            """,
        ).fetchall()
    except sqlite3.Error as exc:
        return {"status": "unavailable", "skipped_reason": "query_failed", "error": str(exc)}
    finally:
        conn.close()

    parsed_rows = []
    for kind, provider, session_id, observed_at, payload_json in all_rows:
        observed_dt = _parse_rfc3339(observed_at)
        if observed_dt is None or observed_dt < cutoff:
            continue
        parsed_rows.append((kind, provider, session_id, observed_dt, payload_json))
    parsed_rows.sort(key=lambda row: row[3], reverse=True)

    conflict_count = 0
    missing_count = 0
    affected_sessions: list[str] = []
    seen_sessions: set[str] = set()
    affected_native_ids: list[str] = []
    seen_native_ids: set[str] = set()
    most_recent: str | None = None
    samples: list[dict[str, Any]] = []

    for kind, provider, session_id, observed_dt, payload_json in parsed_rows:
        if kind == "provider_binding_conflict":
            conflict_count += 1
        elif kind == "provider_binding_missing":
            missing_count += 1
        else:
            continue

        payload: dict[str, Any] = {}
        if payload_json:
            try:
                parsed = json.loads(payload_json)
                if isinstance(parsed, dict):
                    payload = parsed
            except (ValueError, TypeError):
                payload = {}

        provider_session_id = str(payload.get("provider_session_id") or "").strip() or None
        normalized_observed_at = observed_dt.isoformat()
        if most_recent is None:
            most_recent = normalized_observed_at

        normalized_session_id = _normalize_optional_string(session_id)
        if normalized_session_id and normalized_session_id not in seen_sessions:
            seen_sessions.add(normalized_session_id)
            affected_sessions.append(normalized_session_id)
        if provider_session_id and provider_session_id not in seen_native_ids:
            seen_native_ids.add(provider_session_id)
            affected_native_ids.append(provider_session_id)

        if len(samples) < 20:
            sample = {
                "kind": kind,
                "provider": _normalize_optional_string(provider),
                "provider_session_id": provider_session_id,
                "session_id": normalized_session_id,
                "observed_at": normalized_observed_at,
            }
            existing_thread_id = payload.get("existing_thread_id")
            requested_thread_id = payload.get("requested_thread_id")
            if existing_thread_id is not None:
                sample["existing_thread_id"] = str(existing_thread_id).strip() or None
            if requested_thread_id is not None:
                sample["requested_thread_id"] = str(requested_thread_id).strip() or None
            samples.append(sample)

    return {
        "status": "ok",
        "conflict_count": conflict_count,
        "missing_count": missing_count,
        "total": conflict_count + missing_count,
        "affected_session_ids": affected_sessions,
        "affected_provider_session_ids": affected_native_ids,
        "most_recent_observed_at": most_recent,
        "samples": samples,
    }


__all__ = [
    "_normalize_binding_path",
    "_load_session_binding_rows",
    "_binding_by_session_id",
    "_collect_provider_binding_diagnostics",
]
