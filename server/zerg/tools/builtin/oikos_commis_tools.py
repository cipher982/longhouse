"""Oikos commis tools — spawn, check, and cancel background jobs.

Three tools replacing the previous thirteen. The complexity lives in
services/commis.py, not here.
"""

from __future__ import annotations

import asyncio
import logging

from zerg.connectors.context import get_credential_resolver
from zerg.models.models import CommisJob
from zerg.services.oikos_context import get_oikos_context
from zerg.tools.error_envelope import ErrorType
from zerg.tools.error_envelope import tool_error

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# spawn_commis
# ---------------------------------------------------------------------------


def spawn_commis(
    task: str,
    git_repo: str | None = None,
    model: str | None = None,
    backend: str | None = None,
    resume_session_id: str | None = None,
) -> str:
    """Spawn a background agent to execute a task. Sync wrapper."""
    return asyncio.get_event_loop().run_until_complete(spawn_commis_async(task, git_repo, model, backend, resume_session_id))


async def spawn_commis_async(
    task: str,
    git_repo: str | None = None,
    model: str | None = None,
    backend: str | None = None,
    resume_session_id: str | None = None,
    *,
    _tool_call_id: str | None = None,
) -> str:
    """Spawn a background agent to execute a task.

    Creates a CommisJob, fires run_commis_job as a background task, and
    raises RunnerInterrupted to pause Oikos until the job completes.
    """
    from zerg.managers.runtime_runner import RunnerInterrupted
    from zerg.services.commis import run_commis_job

    resolver = get_credential_resolver()
    if not resolver:
        return tool_error(ErrorType.MISSING_CONTEXT, "Cannot spawn commis — no credential context")

    db = resolver.db
    owner_id = resolver.owner_id
    ctx = get_oikos_context()
    oikos_run_id = ctx.run_id if ctx else None

    # Operator capability ceiling enforcement
    if ctx and ctx.operator_capability_ceiling:
        ceiling = ctx.operator_capability_ceiling
        if ceiling in ("notify_only", "observe_only"):
            return tool_error(
                ErrorType.PERMISSION_DENIED,
                f"Capped below autonomous continuation — operator ceiling is '{ceiling}'. "
                "Cannot spawn a background commis. Notify the user instead.",
            )
        if ceiling == "bounded_autonomy" and ctx.operator_target_session_id and resume_session_id:
            if resume_session_id != ctx.operator_target_session_id:
                return tool_error(
                    ErrorType.PERMISSION_DENIED,
                    "Cannot spawn commis for this session — operator policy restricts "
                    f"to the exact session named in the turn-loop message ({ctx.operator_target_session_id}).",
                )

    # Idempotency: check for existing job with same tool_call_id
    if _tool_call_id and oikos_run_id:
        existing = db.query(CommisJob).filter(CommisJob.oikos_run_id == oikos_run_id, CommisJob.tool_call_id == _tool_call_id).first()
        if existing:
            if existing.status in ("success", "failed"):
                return f"Commis job {existing.id} already {existing.status}."
            return f"Commis job {existing.id} already {existing.status} — waiting."

    # Build config
    job_config: dict = {"execution_mode": "workspace"}
    if backend:
        job_config["backend"] = backend
    if git_repo:
        job_config["git_repo"] = git_repo
    if resume_session_id:
        job_config["resume_session_id"] = resume_session_id

    job = CommisJob(
        owner_id=owner_id,
        oikos_run_id=oikos_run_id,
        tool_call_id=_tool_call_id,
        task=task,
        model=model,
        config=job_config,
        status="queued",
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    logger.info("Spawned commis job %s for task: %s", job.id, task[:100])

    # Fire and forget
    asyncio.create_task(run_commis_job(job.id))

    # Pause Oikos until the job completes
    raise RunnerInterrupted({"type": "commis_pending", "job_id": job.id})


# ---------------------------------------------------------------------------
# check_commis_status
# ---------------------------------------------------------------------------


def check_commis_status(job_id: int | None = None) -> str:
    """Check status of a commis job or list active jobs. Sync wrapper."""
    return asyncio.get_event_loop().run_until_complete(check_commis_status_async(job_id))


async def check_commis_status_async(job_id: int | None = None) -> str:
    """Check status of a specific commis job, or list all active jobs."""
    resolver = get_credential_resolver()
    if not resolver:
        return tool_error(ErrorType.MISSING_CONTEXT, "No credential context")

    db = resolver.db
    owner_id = resolver.owner_id

    if job_id:
        job = db.query(CommisJob).filter(CommisJob.id == job_id, CommisJob.owner_id == owner_id).first()
        if not job:
            return f"Commis job {job_id} not found."
        elapsed = ""
        if job.started_at and job.finished_at:
            elapsed = f" ({(job.finished_at - job.started_at).total_seconds():.0f}s)"
        elif job.started_at:
            from datetime import datetime
            from datetime import timezone

            elapsed = f" (running {(datetime.now(timezone.utc) - job.started_at).total_seconds():.0f}s)"
        return f"Job {job.id}: {job.status}{elapsed}\nTask: {job.task[:200]}\nError: {job.error or 'none'}"

    # List active jobs
    active = (
        db.query(CommisJob)
        .filter(CommisJob.owner_id == owner_id, CommisJob.status.in_(["queued", "running"]))
        .order_by(CommisJob.created_at.desc())
        .limit(10)
        .all()
    )
    if not active:
        return "No active commis jobs."
    lines = [f"- Job {j.id}: {j.status} — {j.task[:80]}" for j in active]
    return f"Active commis jobs ({len(active)}):\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# cancel_commis
# ---------------------------------------------------------------------------


def cancel_commis(job_id: int) -> str:
    """Cancel a commis job. Sync wrapper."""
    return asyncio.get_event_loop().run_until_complete(cancel_commis_async(job_id))


async def cancel_commis_async(job_id: int) -> str:
    """Cancel a queued or running commis job."""
    resolver = get_credential_resolver()
    if not resolver:
        return tool_error(ErrorType.MISSING_CONTEXT, "No credential context")

    db = resolver.db
    owner_id = resolver.owner_id

    job = db.query(CommisJob).filter(CommisJob.id == job_id, CommisJob.owner_id == owner_id).first()
    if not job:
        return f"Commis job {job_id} not found."
    if job.status in ("success", "failed", "cancelled"):
        return f"Job {job_id} already {job.status}."

    job.status = "cancelled"
    job.error = "Cancelled by user"
    db.commit()
    return f"Commis job {job_id} cancelled."
