"""Commis job-management tools extracted from oikos_tools."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from zerg.connectors.context import get_credential_resolver
from zerg.services.commis_artifact_store import CommisArtifactStore
from zerg.services.oikos_context import get_oikos_context
from zerg.tools.error_envelope import ErrorType
from zerg.tools.error_envelope import tool_error

logger = logging.getLogger(__name__)


def operator_resume_permission_error(
    *,
    db,
    owner_id: int,
    ctx,
    resume_session_id: str | None,
) -> str | None:
    """Return a policy error when operator-mode resume is not allowed."""
    if not resume_session_id:
        return None
    if getattr(ctx, "source_surface_id", None) != "operator":
        return None

    from zerg.services.oikos_operator_policy import get_operator_policy

    policy = get_operator_policy(db, owner_id)
    if policy.enabled and policy.allow_continue:
        return None

    return "Operator-mode session continuation is disabled by policy. " "Ignore the wakeup or escalate to the user instead."


async def _spawn_workspace_commis_core_async(
    task: str,
    model: str | None = None,
    backend: str | None = None,
    git_repo: str | None = None,
    resume_session_id: str | None = None,
    *,
    _tool_call_id: str | None = None,
    _return_structured: bool = False,
    _skills: list[str] | None = None,
) -> str | dict:
    """Spawn a commis agent to execute a task and queue background execution."""
    from zerg.models.models import CommisJob

    resolver = get_credential_resolver()
    if not resolver:
        return tool_error(
            ErrorType.MISSING_CONTEXT,
            "Cannot spawn commis - no credential context available",
        )

    db = resolver.db
    owner_id = resolver.owner_id

    ctx = get_oikos_context()
    oikos_run_id = ctx.run_id if ctx else None
    trace_id = ctx.trace_id if ctx else None

    commis_model = model
    commis_reasoning_effort = (ctx.reasoning_effort if ctx else None) or "none"

    policy_error = operator_resume_permission_error(
        db=db,
        owner_id=owner_id,
        ctx=ctx,
        resume_session_id=resume_session_id,
    )
    if policy_error:
        return tool_error(ErrorType.PERMISSION_DENIED, policy_error)

    # All commis execution is workspace mode. Missing git_repo = scratch workspace.
    job_config = {"execution_mode": "workspace"}
    if backend:
        job_config["backend"] = backend
    if git_repo:
        job_config["git_repo"] = git_repo
    if resume_session_id:
        job_config["resume_session_id"] = resume_session_id
    if _skills:
        job_config["skills"] = _skills

    try:
        commis_job = None
        existing_job = None

        # Primary idempotency path: same tool_call_id within same run.
        if _tool_call_id and oikos_run_id:
            existing_job = (
                db.query(CommisJob)
                .filter(
                    CommisJob.oikos_run_id == oikos_run_id,
                    CommisJob.tool_call_id == _tool_call_id,
                )
                .first()
            )
            if existing_job:
                logger.info("[IDEMPOTENT] Found existing job %s for tool_call_id=%s", existing_job.id, _tool_call_id)

        # Fallback idempotency path when tool_call_id is unavailable.
        if existing_job is None:
            completed_jobs = (
                db.query(CommisJob)
                .filter(
                    CommisJob.oikos_run_id == oikos_run_id,
                    CommisJob.owner_id == owner_id,
                    CommisJob.status == "success",
                )
                .order_by(CommisJob.created_at.desc())
                .limit(20)
                .all()
            )

            if completed_jobs:
                for job in completed_jobs:
                    if job.task == task:
                        existing_job = job
                        break

        if existing_job is None:
            existing_job = (
                db.query(CommisJob)
                .filter(
                    CommisJob.oikos_run_id == oikos_run_id,
                    CommisJob.task == task,
                    CommisJob.owner_id == owner_id,
                    CommisJob.status.in_(["queued", "running"]),
                )
                .first()
            )

        if existing_job:
            if existing_job.status == "success":
                logger.debug("Existing job %s already succeeded, returning cached result", existing_job.id)
                if existing_job.commis_id:
                    try:
                        artifact_store = CommisArtifactStore()
                        metadata = artifact_store.get_commis_metadata(existing_job.commis_id)
                        summary = metadata.get("summary")
                        if summary:
                            return f"Commis job {existing_job.id} completed:\n\n{summary}"
                        result = artifact_store.get_commis_result(existing_job.commis_id)
                        return f"Commis job {existing_job.id} completed:\n\n{result}"
                    except FileNotFoundError:
                        logger.warning(
                            "Job %s SUCCESS but no result artifact, creating new job",
                            existing_job.id,
                        )
                else:
                    logger.warning("Job %s SUCCESS but no commis_id, creating new job", existing_job.id)
            else:
                commis_job = existing_job
                logger.debug("Reusing existing commis job %s (status: %s)", commis_job.id, existing_job.status)

        if commis_job is None:
            import uuid as uuid_module

            commis_job = CommisJob(
                owner_id=owner_id,
                oikos_run_id=oikos_run_id,
                tool_call_id=_tool_call_id,
                trace_id=uuid_module.UUID(trace_id) if trace_id else None,
                task=task,
                model=commis_model,
                reasoning_effort=commis_reasoning_effort,
                status="queued",
                config=job_config,
            )
            db.add(commis_job)
            db.commit()
            db.refresh(commis_job)
            logger.info("[SPAWN] Created commis job %s with tool_call_id=%s", commis_job.id, _tool_call_id)

            if oikos_run_id is not None:
                from zerg.services.event_store import append_run_event

                await append_run_event(
                    run_id=oikos_run_id,
                    event_type="commis_spawned",
                    payload={
                        "job_id": commis_job.id,
                        "tool_call_id": _tool_call_id,
                        "task": task[:100],
                        "model": commis_model,
                        "backend": backend,
                        "owner_id": owner_id,
                        "trace_id": trace_id,
                    },
                )

        logger.debug("spawn_workspace_commis returning queued response for job %s", commis_job.id)
        if _return_structured:
            return {"job_id": commis_job.id, "status": "queued", "task": task[:100]}
        return f"Commis job {commis_job.id} queued successfully. Working on: {task[:100]}"

    except Exception as e:
        logger.exception("Failed to spawn workspace commis for task: %s", task)
        db.rollback()
        return tool_error(ErrorType.EXECUTION_ERROR, f"Error spawning workspace commis: {e}")


async def spawn_workspace_commis_async(
    task: str,
    git_repo: str | None = None,
    model: str | None = None,
    backend: str | None = None,
    resume_session_id: str | None = None,
    skills: list[str] | None = None,
    *,
    _tool_call_id: str | None = None,
    _return_structured: bool = False,
) -> str | dict:
    """Spawn a commis to execute a task in a workspace."""
    from zerg.services.workspace_manager import validate_git_repo_url

    if git_repo:
        try:
            validate_git_repo_url(git_repo)
        except ValueError as exc:
            return tool_error(ErrorType.VALIDATION_ERROR, str(exc))

    resolved_skills: list[str] | None = None
    if skills:
        try:
            resolver = get_credential_resolver()
            if resolver:
                from zerg.skills.integration import SkillIntegration

                integration = SkillIntegration(
                    db=resolver.db,
                    owner_id=resolver.owner_id,
                    include_user=True,
                )
                resolved = []
                for skill_name in skills:
                    content = integration.get_skill_content(skill_name)
                    if content:
                        resolved.append(content)
                    else:
                        logger.warning("Skill '%s' not found for commis injection", skill_name)
                if resolved:
                    resolved_skills = resolved
        except Exception:
            logger.warning("Failed to resolve skills for commis", exc_info=True)

    return await _spawn_workspace_commis_core_async(
        task=task,
        model=model,
        backend=backend,
        git_repo=git_repo,
        resume_session_id=resume_session_id,
        _tool_call_id=_tool_call_id,
        _return_structured=_return_structured,
        _skills=resolved_skills,
    )


def spawn_workspace_commis(
    task: str,
    git_repo: str | None = None,
    model: str | None = None,
    backend: str | None = None,
    resume_session_id: str | None = None,
    skills: list[str] | None = None,
) -> str:
    """Sync wrapper for spawn_workspace_commis_async."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(
        spawn_workspace_commis_async(
            task=task,
            git_repo=git_repo,
            model=model,
            backend=backend,
            resume_session_id=resume_session_id,
            skills=skills,
        )
    )


async def list_commiss_async(
    limit: int = 20,
    status: str | None = None,
    since_hours: int | None = None,
) -> str:
    """List recent commis jobs with compact summaries."""
    from zerg.crud import crud

    resolver = get_credential_resolver()
    if not resolver:
        return tool_error(
            ErrorType.MISSING_CONTEXT,
            "Cannot list commiss - no credential context available",
        )

    db = resolver.db

    try:
        query = db.query(crud.CommisJob).filter(crud.CommisJob.owner_id == resolver.owner_id)

        if status:
            query = query.filter(crud.CommisJob.status == status)

        if since_hours is not None:
            since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
            query = query.filter(crud.CommisJob.created_at >= since)

        jobs = query.order_by(crud.CommisJob.created_at.desc()).limit(limit).all()

        if not jobs:
            return "No commis jobs found matching criteria."

        artifact_store = CommisArtifactStore()

        lines = [f"Recent commiss (showing {len(jobs)}):\n"]
        for job in jobs:
            summary = None
            if job.commis_id and job.status in ["success", "failed"]:
                try:
                    metadata = artifact_store.get_commis_metadata(job.commis_id)
                    summary = metadata.get("summary")
                except Exception:
                    pass

            if not summary:
                summary = job.task[:150] + "..." if len(job.task) > 150 else job.task

            lines.append(f"- Job {job.id} [{job.status.upper()}]")
            lines.append(f"  {summary}\n")

        lines.append("Use read_commis_result(job_id) for full details.")
        return "\n".join(lines)

    except Exception as e:
        logger.exception("Failed to list commis jobs")
        return tool_error(ErrorType.EXECUTION_ERROR, f"Error listing commis jobs: {e}")


def list_commiss(
    limit: int = 20,
    status: str | None = None,
    since_hours: int | None = None,
) -> str:
    """Sync wrapper for list_commiss_async."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(list_commiss_async(limit, status, since_hours))


async def grep_commiss_async(pattern: str, since_hours: int = 24) -> str:
    """Search completed commis job artifacts for a case-insensitive text pattern."""
    from zerg.crud import crud

    resolver = get_credential_resolver()
    if not resolver:
        return tool_error(
            ErrorType.MISSING_CONTEXT,
            "Cannot grep commiss - no credential context available",
        )

    db = resolver.db
    artifact_store = CommisArtifactStore()

    try:
        query = db.query(crud.CommisJob).filter(
            crud.CommisJob.owner_id == resolver.owner_id,
            crud.CommisJob.commis_id.isnot(None),
            crud.CommisJob.status.in_(["success", "failed"]),
        )

        if since_hours:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
            query = query.filter(crud.CommisJob.created_at >= cutoff)

        jobs = query.all()
        case_insensitive_pattern = f"(?i){re.escape(pattern)}"

        all_matches = []
        for job in jobs:
            try:
                matches = artifact_store.search_commiss(
                    pattern=case_insensitive_pattern,
                    file_glob="**/*.txt",
                    commis_ids=[job.commis_id],
                )
                for match in matches:
                    match["job_id"] = job.id
                all_matches.extend(matches)
            except Exception as e:
                logger.warning("Failed to search commis %s: %s", job.commis_id, e)
                continue

        if not all_matches:
            return f"No matches found for pattern '{pattern}' in last {since_hours} hours"

        lines = [f"Found {len(all_matches)} match(es) for '{pattern}':\n"]
        for match in all_matches[:50]:
            job_id = match.get("job_id", "unknown")
            commis_id = match.get("commis_id", "unknown")
            file_name = match.get("file", "unknown")
            line_num = match.get("line", 0)
            content = match.get("content", "")

            lines.append(f"\nJob {job_id} (commis {commis_id})/{file_name}:{line_num}\n  {content[:200]}")

        if len(all_matches) > 50:
            lines.append(f"\n... and {len(all_matches) - 50} more matches (truncated)")

        return "\n".join(lines)

    except Exception as e:
        logger.exception("Failed to grep commiss: %s", pattern)
        return tool_error(ErrorType.EXECUTION_ERROR, f"Error searching commiss: {e}")


def grep_commiss(pattern: str, since_hours: int = 24) -> str:
    """Sync wrapper for grep_commiss_async."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(grep_commiss_async(pattern, since_hours))


async def get_commis_metadata_async(job_id: str) -> str:
    """Get metadata for a commis job."""
    from zerg.crud import crud

    resolver = get_credential_resolver()
    if not resolver:
        return tool_error(
            ErrorType.MISSING_CONTEXT,
            "Cannot get commis metadata - no credential context available",
        )

    db = resolver.db

    try:
        job_id_int = int(job_id)

        job = (
            db.query(crud.CommisJob)
            .filter(
                crud.CommisJob.id == job_id_int,
                crud.CommisJob.owner_id == resolver.owner_id,
            )
            .first()
        )

        if not job:
            return tool_error(ErrorType.NOT_FOUND, f"Commis job {job_id} not found")

        lines = [
            f"Metadata for commis job {job_id}:\n",
            f"Status: {job.status}",
            f"Task: {job.task}",
            f"Model: {job.model}",
            "\nTimestamps:",
            f"  Created: {job.created_at.isoformat() if job.created_at else 'N/A'}",
            f"  Started: {job.started_at.isoformat() if job.started_at else 'N/A'}",
            f"  Finished: {job.finished_at.isoformat() if job.finished_at else 'N/A'}",
        ]

        from zerg.utils.time import utc_now_naive

        duration_str = "N/A"
        if job.started_at and job.finished_at:
            duration = (job.finished_at - job.started_at).total_seconds() * 1000
            duration_str = f"{int(duration)}ms"
        elif job.started_at and job.status == "running":
            duration = (utc_now_naive() - job.started_at).total_seconds() * 1000
            duration_str = f"{int(duration)}ms (running)"

        lines.append(f"  Duration: {duration_str}")

        if job.commis_id:
            lines.append(f"\nCommis ID: {job.commis_id}")

        if job.error:
            lines.append(f"\nError: {job.error}")

        return "\n".join(lines)

    except ValueError:
        return tool_error(ErrorType.VALIDATION_ERROR, f"Invalid job ID format: {job_id}")
    except Exception as e:
        logger.exception("Failed to get commis metadata: %s", job_id)
        return tool_error(ErrorType.EXECUTION_ERROR, f"Error getting commis metadata: {e}")


def get_commis_metadata(job_id: str) -> str:
    """Sync wrapper for get_commis_metadata_async."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(get_commis_metadata_async(job_id))


async def request_session_selection_async(
    query: str | None = None,
    project: str | None = None,
) -> str:
    """Request user session selection from the frontend modal."""
    ctx = get_oikos_context()
    if not ctx:
        return tool_error(
            ErrorType.MISSING_CONTEXT,
            "Cannot request session selection - no oikos context available",
        )

    filters = {}
    if query:
        filters["query"] = query
    if project:
        filters["project"] = project

    from zerg.services.event_store import append_run_event

    if ctx.run_id is not None:
        await append_run_event(
            run_id=ctx.run_id,
            event_type="show_session_picker",
            payload={
                "filters": filters if filters else None,
                "owner_id": ctx.owner_id,
                "trace_id": ctx.trace_id,
            },
        )

    return (
        "Session picker opened. Waiting for user to select a session. "
        "Once they select one, they'll send a follow-up message with the session ID."
    )


def request_session_selection(
    query: str | None = None,
    project: str | None = None,
) -> str:
    """Sync wrapper for request_session_selection_async."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(request_session_selection_async(query, project))


__all__ = [
    "spawn_workspace_commis",
    "spawn_workspace_commis_async",
    "list_commiss",
    "list_commiss_async",
    "grep_commiss",
    "grep_commiss_async",
    "get_commis_metadata",
    "get_commis_metadata_async",
    "request_session_selection",
    "request_session_selection_async",
]
