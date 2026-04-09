"""Turn review analysis and classification (run-level finalization).

Extracted from session_turn_reviews.py — post-hoc classification
is a separate concern from review orchestration.
"""

from __future__ import annotations

import logging

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from zerg.models import CommisJob
from zerg.models.agents import SessionTurnReview

logger = logging.getLogger(__name__)

_EXPECTED_IGNORE_OUTCOME = "ignore"
_EXPECTED_NOTIFY_OUTCOME = "notify_user"
_EXPECTED_CONTINUE_OUTCOME = "continue_session"


def _expected_outcome(review: SessionTurnReview) -> str:
    if review.execution_state == "would_auto_continue":
        return _EXPECTED_CONTINUE_OUTCOME
    if review.execution_state in {"awaiting_user_approval", "needs_human"}:
        return _EXPECTED_NOTIFY_OUTCOME
    return _EXPECTED_IGNORE_OUTCOME


def _classify_alignment(expected_outcome: str | None, actual_outcome: str | None) -> str | None:
    if not expected_outcome or not actual_outcome:
        return None
    if expected_outcome == actual_outcome:
        return "matched"
    if actual_outcome == "failed":
        return "failed"
    if expected_outcome == _EXPECTED_CONTINUE_OUTCOME and actual_outcome == _EXPECTED_IGNORE_OUTCOME:
        return "more_conservative"
    if expected_outcome == _EXPECTED_NOTIFY_OUTCOME and actual_outcome == _EXPECTED_IGNORE_OUTCOME:
        return "more_conservative"
    if expected_outcome == _EXPECTED_IGNORE_OUTCOME and actual_outcome in {
        _EXPECTED_CONTINUE_OUTCOME,
        "delegated_follow_up",
    }:
        return "more_aggressive"
    if expected_outcome == _EXPECTED_NOTIFY_OUTCOME and actual_outcome in {
        _EXPECTED_CONTINUE_OUTCOME,
        "delegated_follow_up",
    }:
        return "more_aggressive"
    return "different"


def _has_turn_review_table(db: Session) -> bool:
    """Check whether the session_turn_reviews table exists."""
    from sqlalchemy import inspect as sa_inspect

    try:
        inspector = sa_inspect(db.bind)
        return "session_turn_reviews" in inspector.get_table_names()
    except Exception:
        return False


def finalize_turn_reviews_for_run(
    db: Session,
    *,
    run_id: int,
    status: str,
    reason: str | None = None,
    actual_outcome: str | None = None,
) -> int:
    if not _has_turn_review_table(db):
        return 0
    try:
        rows = (
            db.query(SessionTurnReview)
            .filter(
                SessionTurnReview.run_id == run_id,
                SessionTurnReview.status == "enqueued",
            )
            .all()
        )
    except OperationalError:
        logger.debug("Skipping turn review finalization because the table is unavailable", exc_info=True)
        return 0
    if not rows:
        return 0
    for row in rows:
        row.status = status
        row.reason = reason
        row.actual_outcome = actual_outcome
        row.shadow_alignment = _classify_alignment(_expected_outcome(row), actual_outcome)
    return len(rows)


def classify_turn_review_outcome_for_run(db: Session, *, run_id: int) -> int:
    if not _has_turn_review_table(db):
        return 0
    try:
        rows = (
            db.query(SessionTurnReview)
            .filter(
                SessionTurnReview.run_id == run_id,
                SessionTurnReview.status == "enqueued",
            )
            .all()
        )
    except OperationalError:
        logger.debug("Skipping turn review classification because the table is unavailable", exc_info=True)
        return 0
    if not rows:
        return 0

    jobs = db.query(CommisJob).filter(CommisJob.oikos_run_id == run_id).all()
    if not jobs:
        for row in rows:
            expected = _expected_outcome(row)
            if expected == _EXPECTED_NOTIFY_OUTCOME:
                row.status = "enqueued"
                row.reason = "notify_user"
                row.actual_outcome = _EXPECTED_NOTIFY_OUTCOME
                row.shadow_alignment = _classify_alignment(expected, _EXPECTED_NOTIFY_OUTCOME)
                continue
            row.status = "ignored"
            row.reason = "no_action"
            row.actual_outcome = _EXPECTED_IGNORE_OUTCOME
            row.shadow_alignment = _classify_alignment(expected, _EXPECTED_IGNORE_OUTCOME)
        return len(rows)

    resumed_session_ids: list[str] = []
    for job in jobs:
        config = job.config if isinstance(job.config, dict) else {}
        resume_session_id = config.get("resume_session_id")
        if resume_session_id:
            resumed_session_ids.append(str(resume_session_id))
    if resumed_session_ids:
        return finalize_turn_reviews_for_run(
            db,
            run_id=run_id,
            status="acted",
            reason="continue_session",
            actual_outcome=_EXPECTED_CONTINUE_OUTCOME,
        )

    return finalize_turn_reviews_for_run(
        db,
        run_id=run_id,
        status="acted",
        reason="delegated_follow_up",
        actual_outcome="delegated_follow_up",
    )
