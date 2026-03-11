"""Helpers for persisting proactive Oikos wakeup handling."""

from __future__ import annotations

from typing import Any

from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from zerg.models.work import OikosWakeup

WAKEUP_STATUS_SUPPRESSED = "suppressed"
WAKEUP_STATUS_ENQUEUED = "enqueued"
WAKEUP_STATUS_IGNORED = "ignored"
WAKEUP_STATUS_ACTED = "acted"
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


def finalize_wakeups_for_run(
    db: Session,
    *,
    run_id: int,
    status: str,
    reason: str | None = None,
    payload_updates: dict[str, Any] | None = None,
) -> int:
    """Finalize any still-enqueued wakeups tied to a run."""
    try:
        rows = (
            db.query(OikosWakeup)
            .filter(
                OikosWakeup.run_id == run_id,
                OikosWakeup.status == WAKEUP_STATUS_ENQUEUED,
            )
            .all()
        )
    except (OperationalError, ProgrammingError):
        return 0
    if not rows:
        return 0

    for row in rows:
        row.status = status
        row.reason = reason
        if payload_updates:
            payload = dict(row.payload or {})
            payload.update(payload_updates)
            row.payload = payload
    return len(rows)


def classify_wakeup_outcome_for_run(db: Session, *, run_id: int) -> int:
    """Mark an enqueued wakeup as acted or ignored based on launched follow-up work."""
    from zerg.models.models import CommisJob

    jobs = db.query(CommisJob).filter(CommisJob.oikos_run_id == run_id).all()
    if not jobs:
        return finalize_wakeups_for_run(
            db,
            run_id=run_id,
            status=WAKEUP_STATUS_IGNORED,
            reason="no_action",
            payload_updates={"outcome": "ignore"},
        )

    job_ids: list[int] = []
    resumed_session_ids: list[str] = []
    for job in jobs:
        job_ids.append(int(job.id))
        config = job.config if isinstance(job.config, dict) else {}
        resume_session_id = config.get("resume_session_id")
        if resume_session_id:
            resumed_session_ids.append(str(resume_session_id))

    if resumed_session_ids:
        return finalize_wakeups_for_run(
            db,
            run_id=run_id,
            status=WAKEUP_STATUS_ACTED,
            reason="continue_session",
            payload_updates={
                "outcome": "continue_session",
                "job_ids": job_ids,
                "resume_session_ids": sorted(set(resumed_session_ids)),
            },
        )

    return finalize_wakeups_for_run(
        db,
        run_id=run_id,
        status=WAKEUP_STATUS_ACTED,
        reason="delegated_follow_up",
        payload_updates={
            "outcome": "delegated_follow_up",
            "job_ids": job_ids,
        },
    )
