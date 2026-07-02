from __future__ import annotations

from typing import Any


def _collect_cursor_discovery(*, fast: bool) -> dict[str, Any]:
    """Read-only discovery of local Cursor agent sessions.

    Surfaces cursor-agent ``store.db`` sessions on the machine as *unmanaged*
    rows (control_path=unmanaged, liveness_model=transcript, state=detached)
    without ingesting them. This is observed discovery only — it does not
    bind sessions to the timeline or create ingest state; ``longhouse cursor
    import`` is the durable backfill path. The three axes (control_path,
    liveness_model, state) are kept separate per the local-health contract.
    """
    if fast:
        return {"status": "skipped", "skipped_reason": "fast", "sessions": []}
    sessions: list[dict[str, Any]] = []
    legacy_count = 0
    try:
        from zerg.services import local_health as _local_health_pkg

        summaries = list(_local_health_pkg.iter_local_cursor_session_summaries())
    except Exception as exc:  # noqa: BLE001 - discovery must never break local_health
        return {"status": "unavailable", "error": str(exc), "sessions": []}
    for s in summaries:
        if s.legacy:
            legacy_count += 1
        sessions.append(
            {
                "provider": "cursor",
                "provider_session_id": s.agent_id,
                "control_path": s.control_path,
                "liveness_model": s.liveness_model,
                "state": s.state,
                "title": s.title,
                "workspace": s.workspace,
                "model": s.model,
                "created_at_ms": s.created_at_ms,
                "updated_at_ms": s.updated_at_ms,
                "legacy_format": s.legacy,
                "store_path": str(s.store_path),
            }
        )
    sessions.sort(key=lambda row: row.get("updated_at_ms") or row.get("created_at_ms") or 0, reverse=True)
    return {
        "status": "ok",
        "session_count": len(sessions),
        "legacy_format_count": legacy_count,
        "sessions": sessions,
    }


__all__ = ["_collect_cursor_discovery"]
