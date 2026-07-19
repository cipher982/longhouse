from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from pathlib import Path

from zerg.managed_phase_contract import display_label_for_phase
from zerg.managed_phase_contract import is_known_raw_phase
from zerg.services.longhouse_paths import get_agent_db_path
from zerg.services.longhouse_paths import get_agent_outbox_dir
from zerg.services.session_runtime import phase_freshness_ms

from ._shared import _max_rfc3339
from ._shared import _normalize_optional_string
from ._shared import _parse_rfc3339
from ._shared import _to_rfc3339
from .constants import _MANAGED_FINISHED_RETENTION_SECONDS


def _load_persisted_managed_session_phase_rows(base_dir: Path) -> dict[str, dict[str, str | None]]:
    db_path = get_agent_db_path(base_dir)
    if not db_path.exists():
        return {}

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
    except sqlite3.Error:
        return {}

    try:
        rows = conn.execute(
            """
            SELECT
                session_id,
                provider,
                NULL AS workspace_path,
                NULL AS workspace_label,
                phase,
                tool_name,
                source,
                observed_at,
                observed_at AS last_activity_at
            FROM session_phase_state
            ORDER BY observed_at DESC
            """
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()

    merged: dict[str, dict[str, str | None]] = {}
    for (
        session_id,
        provider,
        workspace_path,
        workspace_label,
        phase_kind,
        tool_name,
        phase_source,
        phase_observed_at,
        last_activity_at,
    ) in rows:
        normalized_session_id = _normalize_optional_string(session_id)
        normalized_phase = _normalize_optional_string(phase_kind)
        normalized_observed_at = _parse_rfc3339(str(phase_observed_at or ""))
        if normalized_session_id is None or normalized_phase is None or normalized_observed_at is None:
            continue
        merged[normalized_session_id] = {
            "provider": _normalize_optional_string(provider),
            "workspace_path": _normalize_optional_string(workspace_path),
            "workspace_label": _normalize_optional_string(workspace_label),
            "phase": normalized_phase,
            "tool_name": _normalize_optional_string(tool_name),
            "source": _normalize_optional_string(phase_source),
            "observed_at": _to_rfc3339(normalized_observed_at),
            "last_activity_at": _max_rfc3339(last_activity_at, phase_observed_at),
        }
    return merged


def _load_outbox_session_phase_rows(base_dir: Path) -> dict[str, dict[str, str | None]]:
    outbox_dir = get_agent_outbox_dir(base_dir)
    if not outbox_dir.exists():
        return {}

    merged: dict[str, dict[str, str | None]] = {}
    for path in sorted(outbox_dir.glob("*.json")):
        if path.name.startswith("."):
            continue
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue

        session_id = _normalize_optional_string(payload.get("session_id"))
        phase = _normalize_optional_string(payload.get("state"))
        provider = _normalize_optional_string(payload.get("provider")) or "claude"
        tool_name = _normalize_optional_string(payload.get("tool_name"))
        observed_at = _parse_rfc3339(payload.get("occurred_at"))
        if observed_at is None:
            try:
                observed_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            except OSError:
                observed_at = None
        if session_id is None or phase is None or observed_at is None:
            continue

        next_row = {
            "provider": provider,
            "phase": phase,
            "tool_name": tool_name,
            "source": f"{provider}_hook",
            "observed_at": _to_rfc3339(observed_at),
            "last_activity_at": _to_rfc3339(observed_at),
        }
        current = merged.get(session_id)
        latest_observed_at = _max_rfc3339(next_row["observed_at"], current.get("observed_at")) if current else None
        if current is None or latest_observed_at == next_row["observed_at"]:
            merged[session_id] = next_row
    return merged


def _phase_display_label(phase: str | None, tool_name: str | None) -> str | None:
    return display_label_for_phase(_normalize_optional_string(phase), _normalize_optional_string(tool_name))


def _managed_phase_is_unknown(raw_phase: str | None) -> bool:
    normalized_phase = _normalize_optional_string(raw_phase)
    if normalized_phase is None:
        return False
    return not is_known_raw_phase(normalized_phase)


def _should_keep_managed_phase_row(row: Mapping[str, str | None], *, now: datetime) -> bool:
    """Keep only phase observations that are still current for local display.

    The raw row remains in the Machine Agent DB for diagnosis, but a stale
    activity observation is not current activity. Local-health must agree with
    the Runtime Host that expiry becomes unknown rather than leaving an old
    `thinking`, `running`, or `idle` label visible forever.
    """
    phase = _normalize_optional_string(row.get("phase"))
    observed_raw = _normalize_optional_string(row.get("observed_at"))
    if phase is None or observed_raw is None:
        return False
    observed_at = _parse_rfc3339(observed_raw)
    if observed_at is None:
        return False
    if phase == "finished":
        freshness_seconds = _MANAGED_FINISHED_RETENTION_SECONDS
    else:
        freshness = phase_freshness_ms(phase)
        if freshness is None:
            # Unknown raw phases remain visible as contract-drift diagnostics.
            # They are not normalized to a current known activity state.
            return True
        freshness_seconds = freshness / 1000
    return (now - observed_at).total_seconds() <= freshness_seconds


def _load_managed_session_phase_state(base_dir: Path, *, now: datetime) -> dict[str, dict[str, str | None]]:
    merged = _load_persisted_managed_session_phase_rows(base_dir)
    for session_id, row in _load_outbox_session_phase_rows(base_dir).items():
        current = merged.get(session_id)
        latest_observed_at = _max_rfc3339(row.get("observed_at"), current.get("observed_at")) if current else None
        if current is None or latest_observed_at == row.get("observed_at"):
            next_row = dict(row)
            if current is not None:
                for field_name in ("workspace_path", "workspace_label"):
                    if _normalize_optional_string(next_row.get(field_name)) is None:
                        next_row[field_name] = current.get(field_name)
            next_row["last_activity_at"] = _max_rfc3339(
                row.get("last_activity_at"),
                current.get("last_activity_at") if current else None,
            )
            merged[session_id] = next_row
    return {session_id: row for session_id, row in merged.items() if _should_keep_managed_phase_row(row, now=now)}


__all__ = [
    "_load_persisted_managed_session_phase_rows",
    "_load_outbox_session_phase_rows",
    "_phase_display_label",
    "_managed_phase_is_unknown",
    "_should_keep_managed_phase_row",
    "_load_managed_session_phase_state",
]
