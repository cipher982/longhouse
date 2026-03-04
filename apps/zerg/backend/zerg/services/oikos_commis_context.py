"""Commis inbox context helpers for Oikos run orchestration."""

from __future__ import annotations

import logging
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from sqlalchemy.orm import Session

from zerg.models.models import CommisJob
from zerg.models.models import ThreadMessage
from zerg.services.commis_artifact_store import CommisArtifactStore

logger = logging.getLogger(__name__)

# Configuration for recent commis history injection
RECENT_COMMIS_HISTORY_LIMIT = 5
RECENT_COMMIS_HISTORY_MINUTES = 10
# Marker to identify ephemeral context messages (for cleanup)
RECENT_COMMIS_CONTEXT_MARKER = "<!-- RECENT_COMMIS_CONTEXT -->"


def build_recent_commis_context(db: Session, owner_id: int) -> tuple[str | None, list[int]]:
    """Build inbox context with active commis and unacknowledged results.

    Returns:
        Tuple of (context_string, job_ids_to_acknowledge).
        context_string is None when there is no commis context to inject.
    """
    active_jobs = (
        db.query(CommisJob)
        .filter(
            CommisJob.owner_id == owner_id,
            CommisJob.status.in_(["queued", "running"]),
        )
        .order_by(CommisJob.created_at.desc())
        .limit(RECENT_COMMIS_HISTORY_LIMIT)
        .all()
    )

    unacknowledged_jobs = (
        db.query(CommisJob)
        .filter(
            CommisJob.owner_id == owner_id,
            CommisJob.status.in_(["success", "failed", "cancelled"]),
            CommisJob.acknowledged == False,  # noqa: E712
        )
        .order_by(CommisJob.created_at.desc())
        .limit(RECENT_COMMIS_HISTORY_LIMIT)
        .all()
    )

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=RECENT_COMMIS_HISTORY_MINUTES)
    recent_acknowledged_jobs = (
        db.query(CommisJob)
        .filter(
            CommisJob.owner_id == owner_id,
            CommisJob.status.in_(["success", "failed", "cancelled"]),
            CommisJob.acknowledged == True,  # noqa: E712
            CommisJob.created_at >= cutoff,
        )
        .order_by(CommisJob.created_at.desc())
        .limit(3)
        .all()
    )

    if not active_jobs and not unacknowledged_jobs and not recent_acknowledged_jobs:
        return None, []

    artifact_store = None
    try:
        artifact_store = CommisArtifactStore()
    except (OSError, PermissionError) as e:
        logger.warning("CommisArtifactStore unavailable, using task summaries only: %s", e)

    def get_elapsed_str(job_time: datetime) -> str:
        if job_time.tzinfo is None:
            job_time = job_time.replace(tzinfo=timezone.utc)
        elapsed = datetime.now(timezone.utc) - job_time
        if elapsed.total_seconds() >= 3600:
            return f"{int(elapsed.total_seconds() / 3600)}h ago"
        if elapsed.total_seconds() >= 60:
            return f"{int(elapsed.total_seconds() / 60)}m ago"
        return f"{int(elapsed.total_seconds())}s ago"

    def get_summary(job: CommisJob, max_chars: int = 150) -> str:
        summary = None
        if artifact_store and job.commis_id and job.status in ["success", "failed"]:
            try:
                metadata = artifact_store.get_commis_metadata(job.commis_id)
                summary = metadata.get("summary")
            except Exception:
                pass
        if not summary:
            summary = job.task[:max_chars] + "..." if len(job.task) > max_chars else job.task
        return summary

    lines = [
        RECENT_COMMIS_CONTEXT_MARKER,
        "## Commis Inbox",
    ]

    if active_jobs:
        lines.append("\n**Active Commiss:**")
        for job in active_jobs:
            elapsed_str = get_elapsed_str(job.started_at or job.created_at)
            status_icon = "⏳" if job.status == "queued" else "⋯"
            task_preview = job.task[:80] + "..." if len(job.task) > 80 else job.task
            lines.append(f"- Job {job.id} [{status_icon} {job.status.upper()}] ({elapsed_str})")
            lines.append(f"  Task: {task_preview}")

    jobs_to_acknowledge: list[int] = []
    if unacknowledged_jobs:
        lines.append("\n**New Results (unread):**")
        for job in unacknowledged_jobs:
            elapsed_str = get_elapsed_str(job.finished_at or job.created_at)
            status_icon = "✓" if job.status == "success" else "✗"
            summary = get_summary(job)
            lines.append(f"- Job {job.id} [{status_icon} {job.status.upper()}] ({elapsed_str})")
            lines.append(f"  {summary}")
            jobs_to_acknowledge.append(job.id)

    if recent_acknowledged_jobs and not unacknowledged_jobs:
        lines.append("\n**Recent Work:**")
        for job in recent_acknowledged_jobs:
            elapsed_str = get_elapsed_str(job.finished_at or job.created_at)
            status_icon = "✓" if job.status == "success" else "✗"
            task_preview = job.task[:60] + "..." if len(job.task) > 60 else job.task
            lines.append(f"- Job {job.id} [{status_icon}] {task_preview} ({elapsed_str})")

    lines.append("")
    if unacknowledged_jobs:
        lines.append("Use `read_commis_result(job_id)` for full details.")
    if active_jobs:
        lines.append("Use `check_commis_status()` to see commis progress.")
        lines.append("Use `wait_for_commis(job_id)` if you need to block for a result.")

    return "\n".join(lines), jobs_to_acknowledge


def acknowledge_commis_jobs(db: Session, job_ids: list[int]) -> None:
    """Mark commis jobs as acknowledged after context message persistence."""
    if not job_ids:
        return

    db.query(CommisJob).filter(CommisJob.id.in_(job_ids)).update(
        {"acknowledged": True},
        synchronize_session=False,
    )
    db.commit()
    logger.debug("Marked %s commis jobs as acknowledged", len(job_ids))


def cleanup_stale_commis_context(db: Session, thread_id: int, min_age_seconds: float = 5.0) -> int:
    """Delete previous injected commis context system messages from a thread."""
    age_cutoff = datetime.now(timezone.utc) - timedelta(seconds=min_age_seconds)

    all_marked = (
        db.query(ThreadMessage)
        .filter(
            ThreadMessage.thread_id == thread_id,
            ThreadMessage.role == "system",
            ThreadMessage.content.contains(RECENT_COMMIS_CONTEXT_MARKER),
        )
        .order_by(ThreadMessage.sent_at.desc())
        .all()
    )

    if not all_marked:
        return 0

    newest = all_marked[0]
    newest_sent_at = newest.sent_at
    if newest_sent_at.tzinfo is None:
        newest_sent_at = newest_sent_at.replace(tzinfo=timezone.utc)

    if newest_sent_at >= age_cutoff:
        messages_to_delete = all_marked[1:]
    else:
        messages_to_delete = all_marked

    for msg in messages_to_delete:
        db.delete(msg)

    count = len(messages_to_delete)
    if count > 0:
        logger.debug("Cleaned up %s stale commis context message(s) from thread %s", count, thread_id)

    return count


__all__ = [
    "RECENT_COMMIS_HISTORY_LIMIT",
    "RECENT_COMMIS_HISTORY_MINUTES",
    "RECENT_COMMIS_CONTEXT_MARKER",
    "build_recent_commis_context",
    "acknowledge_commis_jobs",
    "cleanup_stale_commis_context",
]
