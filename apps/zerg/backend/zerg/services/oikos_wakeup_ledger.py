"""Helpers for persisting proactive Oikos wakeup handling."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from zerg.models.work import OikosWakeup

WAKEUP_STATUS_SUPPRESSED = "suppressed"
WAKEUP_STATUS_ENQUEUED = "enqueued"
WAKEUP_STATUS_FAILED = "failed"


def append_wakeup(
    db: Session,
    *,
    owner_id: int | None,
    source: str,
    trigger_type: str,
    status: str,
    reason: str | None = None,
    session_id: str | None = None,
    conversation_id: str | None = None,
    wakeup_key: str | None = None,
    run_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> OikosWakeup:
    """Append one wakeup ledger row to the current DB session."""

    row = OikosWakeup(
        owner_id=owner_id,
        source=source,
        trigger_type=trigger_type,
        status=status,
        reason=reason,
        session_id=session_id,
        conversation_id=conversation_id,
        wakeup_key=wakeup_key,
        run_id=run_id,
        payload=dict(payload or {}),
    )
    db.add(row)
    return row
