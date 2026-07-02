from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any

from zerg.services.longhouse_paths import get_agent_db_path

from ._shared import _to_rfc3339
from .constants import ACTIVITY_RECENCY_BANDS
from .constants import ACTIVITY_RECENT_MINUTES
from .constants import RECENT_TOUCH_LIMIT


def _session_context_payloads(path: Path, *, max_lines: int) -> Iterator[Any]:
    try:
        with path.open() as handle:
            for index, raw_line in enumerate(handle):
                if index >= max_lines:
                    break
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                yield payload
    except OSError:
        return


def _payload_metadata_context(meta: dict[str, Any]) -> tuple[str | None, str | None]:
    cwd = meta.get("cwd") if isinstance(meta.get("cwd"), str) else None
    branch = None
    if isinstance(meta.get("git"), dict):
        branch = meta["git"].get("branch") if isinstance(meta["git"].get("branch"), str) else None
    return cwd, branch


def _payload_message_context(
    message: dict[str, Any],
    *,
    cwd: str | None,
    branch: str | None,
) -> tuple[str | None, str | None]:
    cwd = cwd or (message.get("cwd") if isinstance(message.get("cwd"), str) else None)
    branch = branch or (message.get("gitBranch") if isinstance(message.get("gitBranch"), str) else None)
    return cwd, branch


def _session_context_from_payload(payload: Any) -> tuple[str | None, str | None]:
    cwd = None
    branch = None
    if not isinstance(payload, dict):
        return cwd, branch

    if isinstance(payload.get("payload"), dict):
        cwd, branch = _payload_metadata_context(payload["payload"])
    if isinstance(payload.get("message"), dict):
        cwd, branch = _payload_message_context(payload["message"], cwd=cwd, branch=branch)
    return cwd, branch


def _read_session_context(path: Path, *, max_lines: int = 6) -> tuple[str | None, str | None]:
    """Extract cwd and branch from the first few JSONL records when available."""
    for payload in _session_context_payloads(path, max_lines=max_lines):
        cwd, branch = _session_context_from_payload(payload)
        if cwd or branch:
            return cwd, branch

    return None, None


def _derive_workspace_label(source_path: Path, *, cwd: str | None) -> str | None:
    if cwd:
        name = Path(cwd).name.strip()
        if name:
            return name

    parts = source_path.parts
    if "projects" in parts:
        try:
            encoded = parts[parts.index("projects") + 1]
        except (ValueError, IndexError):
            return None
        encoded = encoded.lstrip("-")
        if "-git-" in encoded:
            return encoded.split("-git-", 1)[1] or None
        if encoded:
            return encoded.rsplit("-", 1)[-1] or None
    return None


def _recent_touch_entry(source_path: str, provider: str, last_updated: str) -> dict[str, Any]:
    path = Path(source_path)
    cwd, branch = _read_session_context(path)
    workspace_label = _derive_workspace_label(path, cwd=cwd)
    return {
        "provider": provider,
        "last_updated": last_updated,
        "workspace_label": workspace_label,
        "branch": branch,
        "is_subagent": "subagents" in path.parts,
    }


def _empty_activity_summary(db_path: Path) -> dict[str, Any]:
    return {
        "path": str(db_path),
        "exists": db_path.exists(),
        "error": None,
        "sessions_today": 0,
        "sessions_recent": 0,
        "provider_counts_today": {},
        "provider_counts_recent": {},
        "session_recency_bands": [],
        "recent_touches": [],
        "latest_activity_at": None,
        "recent_window_minutes": ACTIVITY_RECENT_MINUTES,
    }


def _activity_cutoffs(now: datetime) -> tuple[str, str, list[str]]:
    local_now = now.astimezone()
    start_of_day_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_day_utc = start_of_day_local.astimezone(timezone.utc)
    today_cutoff = _to_rfc3339(start_of_day_utc)
    recent_cutoff = _to_rfc3339(now - timedelta(minutes=ACTIVITY_RECENT_MINUTES))
    band_edges = [_to_rfc3339(now - delta) for _, delta in ACTIVITY_RECENCY_BANDS]
    return today_cutoff, recent_cutoff, band_edges


def _activity_sql_fragments() -> tuple[str, str, str]:
    session_expr = "provider || ':' || COALESCE(NULLIF(session_id, ''), NULLIF(provider_session_id, ''), path)"
    provider_session_expr = "COALESCE(NULLIF(session_id, ''), NULLIF(provider_session_id, ''), path)"
    session_files_predicate = "path LIKE '%.jsonl'"
    return session_expr, provider_session_expr, session_files_predicate


def _populate_activity_aggregate(
    summary: dict[str, Any],
    conn: sqlite3.Connection,
    *,
    session_expr: str,
    session_files_predicate: str,
    today_cutoff: str,
    recent_cutoff: str,
) -> None:
    aggregate_row = conn.execute(
        f"""
        SELECT
            MAX(last_updated),
            COUNT(DISTINCT CASE
                WHEN julianday(last_updated) >= julianday(?) THEN {session_expr}
            END),
            COUNT(DISTINCT CASE
                WHEN julianday(last_updated) >= julianday(?) THEN {session_expr}
            END)
        FROM file_state
        WHERE {session_files_predicate}
        """,
        (today_cutoff, recent_cutoff),
    ).fetchone()
    if aggregate_row is not None:
        summary["latest_activity_at"] = aggregate_row[0]
        summary["sessions_today"] = int(aggregate_row[1] or 0)
        summary["sessions_recent"] = int(aggregate_row[2] or 0)


def _activity_provider_counts(
    conn: sqlite3.Connection,
    *,
    provider_session_expr: str,
    session_files_predicate: str,
    cutoff: str,
) -> dict[str, int]:
    provider_counts: dict[str, int] = {}
    for provider, count in conn.execute(
        f"""
        SELECT provider, COUNT(DISTINCT {provider_session_expr})
        FROM file_state
        WHERE {session_files_predicate}
          AND julianday(last_updated) >= julianday(?)
        GROUP BY provider
        """,
        (cutoff,),
    ):
        provider_name = str(provider or "").strip()
        if not provider_name:
            continue
        provider_counts[provider_name] = int(count or 0)
    return provider_counts


def _activity_band_specs(today_cutoff: str, band_edges: list[str]) -> list[dict[str, str | None]]:
    return [
        {"label": "0-1m", "newer_than": band_edges[0], "older_than": None},
        {"label": "1-5m", "newer_than": band_edges[1], "older_than": band_edges[0]},
        {"label": "5-15m", "newer_than": band_edges[2], "older_than": band_edges[1]},
        {"label": "15-60m", "newer_than": band_edges[3], "older_than": band_edges[2]},
        {"label": "1-6h", "newer_than": band_edges[4], "older_than": band_edges[3]},
        {"label": "6h+", "newer_than": today_cutoff, "older_than": band_edges[4]},
    ]


def _activity_recency_bands(
    conn: sqlite3.Connection,
    *,
    session_expr: str,
    session_files_predicate: str,
    band_specs: list[dict[str, str | None]],
) -> list[dict[str, Any]]:
    band_clauses: list[str] = []
    band_params: list[Any] = []
    for spec in band_specs:
        clause = "COUNT(DISTINCT CASE WHEN julianday(last_updated) >= julianday(?)"
        band_params.append(spec["newer_than"])
        older_than = spec["older_than"]
        if older_than is not None:
            clause += " AND julianday(last_updated) < julianday(?)"
            band_params.append(older_than)
        clause += f" THEN {session_expr} END)"
        band_clauses.append(clause)

    band_row = conn.execute(
        f"""
        SELECT {", ".join(band_clauses)}
        FROM file_state
        WHERE {session_files_predicate}
        """,
        tuple(band_params),
    ).fetchone()
    if band_row is None:
        return []
    return [
        {
            "label": spec["label"],
            "session_count": int(band_row[index] or 0),
        }
        for index, spec in enumerate(band_specs)
    ]


def _activity_recent_touches(
    conn: sqlite3.Connection,
    *,
    provider_session_expr: str,
    session_files_predicate: str,
) -> list[dict[str, Any]]:
    recent_touches: list[dict[str, Any]] = []
    for provider, last_updated, path in conn.execute(
        f"""
        SELECT provider, last_updated, path
        FROM (
            SELECT
                provider,
                {provider_session_expr} AS session_key,
                path,
                MAX(last_updated) AS last_updated
            FROM file_state
            WHERE {session_files_predicate}
            GROUP BY provider, session_key
        )
        ORDER BY julianday(last_updated) DESC
        LIMIT ?
        """,
        (RECENT_TOUCH_LIMIT,),
    ):
        provider_name = str(provider or "").strip()
        if not provider_name or not last_updated:
            continue
        recent_touches.append(_recent_touch_entry(str(path), provider_name, str(last_updated)))
    return recent_touches


def _collect_activity_summary(base_dir: Path, *, now: datetime) -> dict[str, Any]:
    db_path = get_agent_db_path(base_dir)
    summary = _empty_activity_summary(db_path)
    if not db_path.exists():
        return summary

    today_cutoff, recent_cutoff, band_edges = _activity_cutoffs(now)
    session_expr, provider_session_expr, session_files_predicate = _activity_sql_fragments()

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
    except sqlite3.Error as exc:
        summary["error"] = str(exc)
        return summary

    try:
        _populate_activity_aggregate(
            summary,
            conn,
            session_expr=session_expr,
            session_files_predicate=session_files_predicate,
            today_cutoff=today_cutoff,
            recent_cutoff=recent_cutoff,
        )
        summary["provider_counts_today"] = _activity_provider_counts(
            conn,
            provider_session_expr=provider_session_expr,
            session_files_predicate=session_files_predicate,
            cutoff=today_cutoff,
        )
        summary["provider_counts_recent"] = _activity_provider_counts(
            conn,
            provider_session_expr=provider_session_expr,
            session_files_predicate=session_files_predicate,
            cutoff=recent_cutoff,
        )
        summary["session_recency_bands"] = _activity_recency_bands(
            conn,
            session_expr=session_expr,
            session_files_predicate=session_files_predicate,
            band_specs=_activity_band_specs(today_cutoff, band_edges),
        )
        summary["recent_touches"] = _activity_recent_touches(
            conn,
            provider_session_expr=provider_session_expr,
            session_files_predicate=session_files_predicate,
        )
        return summary
    except sqlite3.Error as exc:
        summary["error"] = str(exc)
        return summary
    finally:
        conn.close()


__all__ = [
    "_session_context_payloads",
    "_payload_metadata_context",
    "_payload_message_context",
    "_session_context_from_payload",
    "_read_session_context",
    "_derive_workspace_label",
    "_recent_touch_entry",
    "_empty_activity_summary",
    "_activity_cutoffs",
    "_activity_sql_fragments",
    "_populate_activity_aggregate",
    "_activity_provider_counts",
    "_activity_band_specs",
    "_activity_recency_bands",
    "_activity_recent_touches",
    "_collect_activity_summary",
]
