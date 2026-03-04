"""Oikos tools for spawning and managing commis agents.

This module provides tools that allow oikos agents to delegate tasks to
disposable commis agents, retrieve their results, and drill into their artifacts.

The oikos/commis pattern enables complex delegation scenarios where a oikos
can spawn multiple commiss for parallel execution or break down complex tasks.

Commis execution flow (async inbox model):
- spawn_workspace_commis() creates CommisJob and returns immediately (non-blocking)
- Commis runs in background via CommisJobProcessor
- Results surface in the oikos inbox on the next turn
- wait_for_commis() is the explicit opt-in blocking path (raises FicheInterrupted)
"""

import logging
import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import List

from zerg.connectors.context import get_credential_resolver
from zerg.services.commis_artifact_store import CommisArtifactStore
from zerg.services.oikos_context import get_oikos_context
from zerg.tools.builtin.oikos_commis_artifact_tools import cancel_commis
from zerg.tools.builtin.oikos_commis_artifact_tools import cancel_commis_async
from zerg.tools.builtin.oikos_commis_artifact_tools import check_commis_status
from zerg.tools.builtin.oikos_commis_artifact_tools import check_commis_status_async
from zerg.tools.builtin.oikos_commis_artifact_tools import get_commis_evidence
from zerg.tools.builtin.oikos_commis_artifact_tools import get_commis_evidence_async
from zerg.tools.builtin.oikos_commis_artifact_tools import get_tool_output
from zerg.tools.builtin.oikos_commis_artifact_tools import get_tool_output_async
from zerg.tools.builtin.oikos_commis_artifact_tools import peek_commis_output
from zerg.tools.builtin.oikos_commis_artifact_tools import peek_commis_output_async
from zerg.tools.builtin.oikos_commis_artifact_tools import read_commis_file
from zerg.tools.builtin.oikos_commis_artifact_tools import read_commis_file_async
from zerg.tools.builtin.oikos_commis_artifact_tools import read_commis_result
from zerg.tools.builtin.oikos_commis_artifact_tools import read_commis_result_async
from zerg.tools.builtin.oikos_commis_artifact_tools import wait_for_commis
from zerg.tools.builtin.oikos_commis_artifact_tools import wait_for_commis_async
from zerg.tools.error_envelope import ErrorType
from zerg.tools.error_envelope import tool_error
from zerg.types.tools import Tool as StructuredTool

logger = logging.getLogger(__name__)


async def _spawn_workspace_commis_core_async(
    task: str,
    model: str | None = None,
    backend: str | None = None,
    git_repo: str | None = None,
    resume_session_id: str | None = None,
    *,
    _tool_call_id: str | None = None,  # Internal: passed by _call_tool_async for idempotency
    _return_structured: bool = False,  # Internal: return dict instead of string for oikos_react_engine
    _skills: list[str] | None = None,  # Internal: resolved skill content for commis prompt injection
) -> str | dict:
    """Spawn a commis agent to execute a task and wait for completion.

    The commis runs in the background. Creates a CommisJob and returns job info
    for the caller to handle interruption and resumption.

    Args:
        task: Natural language description of what the commis should do
        model: Optional model override for the commis
        backend: Optional backend override (zai/codex/gemini/bedrock/anthropic)
        git_repo: Optional Git repository URL for repo workspace mode.
            If omitted, the commis uses a scratch workspace.
        resume_session_id: Session UUID to resume in workspace mode.
            Enables cross-environment session continuity.

    Returns:
        The commis's result after completion

    Example:
        spawn_workspace_commis("Fix typo in README", git_repo="git@github.com:user/repo.git")
        spawn_workspace_commis("Analyze dependencies", git_repo="https://github.com/org/repo.git")
        spawn_workspace_commis("Continue work", git_repo="...", resume_session_id="abc-123")
    """
    from zerg.models.models import CommisJob

    # Get database session from credential resolver context
    resolver = get_credential_resolver()
    if not resolver:
        return tool_error(
            ErrorType.MISSING_CONTEXT,
            "Cannot spawn commis - no credential context available",
        )

    db = resolver.db
    owner_id = resolver.owner_id

    # Get oikos run_id and trace_id from context (for SSE event correlation and debugging)
    ctx = get_oikos_context()
    oikos_run_id = ctx.run_id if ctx else None
    trace_id = ctx.trace_id if ctx else None

    # Commis model is explicit override only (no implicit defaults).
    commis_model = model
    commis_reasoning_effort = (ctx.reasoning_effort if ctx else None) or "none"

    # All commis execute through workspace mode. Without git_repo this becomes
    # a scratch workspace (ephemeral temp dir) in CommisJobProcessor.
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
        # IDEMPOTENCY: Prevent duplicate commiss on retry/resume.
        #
        # Primary strategy: Use tool_call_id (unique per LLM response) for exact idempotency.
        # Fallback strategy: Prefix matching on task string (handles cases without tool_call_id).
        #
        # This prevents duplicate commiss while allowing legitimate multi-commis scenarios.

        commis_job = None
        existing_job = None

        # PRIMARY: Check for existing job with same tool_call_id (most reliable)
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
                logger.info(f"[IDEMPOTENT] Found existing job {existing_job.id} for tool_call_id={_tool_call_id}")

        # FALLBACK: Check for completed/in-progress commiss using task matching
        # (only if tool_call_id lookup didn't find anything)
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
                # Exact task match only - prefix matching was removed as unsafe
                # (near-matches could return wrong commis results if tasks share prefixes)
                for job in completed_jobs:
                    if job.task == task:
                        existing_job = job
                        break

        if existing_job is None:
            # No completed match - check for in-progress job with EXACT task match
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
                # Already completed - return cached result immediately
                # This prevents duplicate commiss on retry
                logger.debug(f"Existing job {existing_job.id} already succeeded, returning cached result")
                if existing_job.commis_id:
                    try:
                        artifact_store = CommisArtifactStore()
                        # Use summary-first approach (consistent with resume path)
                        metadata = artifact_store.get_commis_metadata(existing_job.commis_id)
                        summary = metadata.get("summary")
                        if summary:
                            return f"Commis job {existing_job.id} completed:\n\n{summary}"
                        # Fall back to full result if no summary
                        result = artifact_store.get_commis_result(existing_job.commis_id)
                        return f"Commis job {existing_job.id} completed:\n\n{result}"
                    except FileNotFoundError:
                        # Result artifact not available, treat as if job doesn't exist
                        logger.warning(f"Job {existing_job.id} SUCCESS but no result artifact, creating new job")
                else:
                    logger.warning(f"Job {existing_job.id} SUCCESS but no commis_id, creating new job")
                # Fall through to create new job
            else:
                # queued or running - reuse and wait via interrupt
                commis_job = existing_job
                logger.debug(f"Reusing existing commis job {commis_job.id} (status: {existing_job.status})")

        if commis_job is None:
            # Create new commis job record with tool_call_id for idempotency
            import uuid as uuid_module

            commis_job = CommisJob(
                owner_id=owner_id,
                oikos_run_id=oikos_run_id,
                tool_call_id=_tool_call_id,  # Enables idempotency on retry/resume
                trace_id=uuid_module.UUID(trace_id) if trace_id else None,  # Inherit from oikos for debugging
                task=task,
                model=commis_model,
                reasoning_effort=commis_reasoning_effort,  # Inherit from oikos
                status="queued",
                config=job_config,  # Cloud execution config (execution_mode, git_repo)
            )
            db.add(commis_job)
            db.commit()
            db.refresh(commis_job)
            logger.info(f"[SPAWN] Created commis job {commis_job.id} with tool_call_id={_tool_call_id}")

            # Emit COMMIS_SPAWNED event durably (replays on reconnect)
            # Only persist if we have a oikos run_id (test mocks may not have one)
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

        # Return job info for caller to handle interruption.
        # oikos_react_engine raises FicheInterrupted
        # to pause execution until commis completes.
        logger.debug(f"spawn_workspace_commis returning queued response for job {commis_job.id}")
        if _return_structured:
            return {"job_id": commis_job.id, "status": "queued", "task": task[:100]}
        return f"Commis job {commis_job.id} queued successfully. Working on: {task[:100]}"

    except Exception as e:
        logger.exception(f"Failed to spawn workspace commis for task: {task}")
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
    """Spawn a commis to execute a task in a workspace.

    If `git_repo` is provided, the repository is cloned to an isolated workspace.
    Otherwise, the commis runs in an ephemeral scratch workspace.

    Args:
        task: What to do in the workspace (analyze code, fix bug, etc)
        git_repo: Optional repository URL
            (https://github.com/org/repo.git or git@github.com:org/repo.git)
        model: Optional model override for the commis
        backend: Optional backend override (zai/codex/gemini/bedrock/anthropic)
        resume_session_id: Life Hub session UUID to resume (for session continuity)
        skills: List of skill names to activate for the commis. The full
            skill content is resolved from the user's loaded skills and
            injected into the commis prompt.

    Returns:
        The commis's result after completion

    Example:
        spawn_workspace_commis("List dependencies from pyproject.toml", "https://github.com/langchain-ai/langchain.git")
        spawn_workspace_commis("Fix the typo in README.md", "git@github.com:user/repo.git")
        spawn_workspace_commis("Investigate a shell command issue")
    """
    # Early validation: reject dangerous URLs before job creation (defense in depth)
    # Delegate to shared validator to stay consistent with workspace_manager rules.
    from zerg.services.workspace_manager import validate_git_repo_url

    if git_repo:
        try:
            validate_git_repo_url(git_repo)
        except ValueError as exc:
            return tool_error(ErrorType.VALIDATION_ERROR, str(exc))

    # Resolve skill content from user's loaded skills (if any requested)
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

    # Delegate to the core implementation with workspace mode forced
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
    """List recent commis jobs with SUMMARIES ONLY.

    Returns compressed summaries for scanning. To get full details,
    call read_commis_result(job_id).

    This prevents context overflow when scanning 50+ commiss.

    Args:
        limit: Maximum number of jobs to return (default: 20)
        status: Filter by status ("queued", "running", "success", "failed", or None for all)
        since_hours: Only show jobs from the last N hours

    Returns:
        Formatted list of commis jobs with summaries (not full results)
    """
    from zerg.crud import crud

    # Get owner_id from context for security filtering
    resolver = get_credential_resolver()
    if not resolver:
        return tool_error(
            ErrorType.MISSING_CONTEXT,
            "Cannot list commiss - no credential context available",
        )

    db = resolver.db

    try:
        # Query commis jobs with filtering
        query = db.query(crud.CommisJob).filter(crud.CommisJob.owner_id == resolver.owner_id)

        if status:
            query = query.filter(crud.CommisJob.status == status)

        if since_hours is not None:
            since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
            query = query.filter(crud.CommisJob.created_at >= since)

        jobs = query.order_by(crud.CommisJob.created_at.desc()).limit(limit).all()

        if not jobs:
            return "No commis jobs found matching criteria."

        # Get artifact store for summary lookup
        artifact_store = CommisArtifactStore()

        # Format output - compact with summaries
        lines = [f"Recent commiss (showing {len(jobs)}):\n"]
        for job in jobs:
            job_id = job.id
            job_status = job.status

            # Get summary from artifact store if available, else truncate task
            summary = None
            if job.commis_id and job.status in ["success", "failed"]:
                try:
                    metadata = artifact_store.get_commis_metadata(job.commis_id)
                    summary = metadata.get("summary")
                except Exception:
                    pass  # Fall back to task truncation

            if not summary:
                # Fallback: truncate task for display
                summary = job.task[:150] + "..." if len(job.task) > 150 else job.task

            # Compact format with summary
            lines.append(f"- Job {job_id} [{job_status.upper()}]")
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
    """Sync wrapper for list_commiss_async. Used for CLI/tests."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(list_commiss_async(limit, status, since_hours))


async def grep_commiss_async(pattern: str, since_hours: int = 24) -> str:
    """Search across commis job artifacts for a pattern.

    Args:
        pattern: Text pattern to search for (case-insensitive)
        since_hours: Only search jobs from the last N hours (default: 24)

    Returns:
        Matches with job IDs and context
    """
    from zerg.crud import crud

    # Get owner_id from context for security filtering
    resolver = get_credential_resolver()
    if not resolver:
        return tool_error(
            ErrorType.MISSING_CONTEXT,
            "Cannot grep commiss - no credential context available",
        )

    db = resolver.db
    artifact_store = CommisArtifactStore()

    try:
        # Get completed jobs with commis_ids
        query = db.query(crud.CommisJob).filter(
            crud.CommisJob.owner_id == resolver.owner_id,
            crud.CommisJob.commis_id.isnot(None),
            crud.CommisJob.status.in_(["success", "failed"]),
        )

        if since_hours:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
            query = query.filter(crud.CommisJob.created_at >= cutoff)

        jobs = query.all()

        # Use case-insensitive regex
        import re

        case_insensitive_pattern = f"(?i){re.escape(pattern)}"

        # Search across artifacts for each job
        all_matches = []
        for job in jobs:
            try:
                matches = artifact_store.search_commiss(
                    pattern=case_insensitive_pattern,
                    file_glob="**/*.txt",
                    commis_ids=[job.commis_id],  # Only search this commis
                )
                # Add job_id to each match
                for match in matches:
                    match["job_id"] = job.id
                all_matches.extend(matches)
            except Exception as e:
                logger.warning(f"Failed to search commis {job.commis_id}: {e}")
                continue

        if not all_matches:
            return f"No matches found for pattern '{pattern}' in last {since_hours} hours"

        # Format results
        lines = [f"Found {len(all_matches)} match(es) for '{pattern}':\n"]
        for match in all_matches[:50]:  # Limit to 50 matches
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
        logger.exception(f"Failed to grep commiss: {pattern}")
        return tool_error(ErrorType.EXECUTION_ERROR, f"Error searching commiss: {e}")


def grep_commiss(pattern: str, since_hours: int = 24) -> str:
    """Sync wrapper for grep_commiss_async. Used for CLI/tests."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(grep_commiss_async(pattern, since_hours))


async def get_commis_metadata_async(job_id: str) -> str:
    """Get detailed metadata about a commis job execution.

    Args:
        job_id: The commis job ID (integer as string)

    Returns:
        Formatted metadata including task, status, timestamps, duration, config
    """
    from zerg.crud import crud

    # Get owner_id from context for security filtering
    resolver = get_credential_resolver()
    if not resolver:
        return tool_error(
            ErrorType.MISSING_CONTEXT,
            "Cannot get commis metadata - no credential context available",
        )

    db = resolver.db

    try:
        # Parse job ID
        job_id_int = int(job_id)

        # Get job record
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

        # Format nicely
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

        # Calculate duration
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

        # Add error if present
        if job.error:
            lines.append(f"\nError: {job.error}")

        return "\n".join(lines)

    except ValueError:
        return tool_error(ErrorType.VALIDATION_ERROR, f"Invalid job ID format: {job_id}")
    except Exception as e:
        logger.exception(f"Failed to get commis metadata: {job_id}")
        return tool_error(ErrorType.EXECUTION_ERROR, f"Error getting commis metadata: {e}")


def get_commis_metadata(job_id: str) -> str:
    """Sync wrapper for get_commis_metadata_async. Used for CLI/tests."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(get_commis_metadata_async(job_id))


# ---------------------------------------------------------------------------
# Session Selection Tool
# ---------------------------------------------------------------------------


async def request_session_selection_async(
    query: str | None = None,
    project: str | None = None,
) -> str:
    """Request user to select a session from Life Hub history.

    Use this when the user wants to resume a past session but hasn't
    provided a specific session ID. This opens a modal for them to
    browse and select a session.

    Args:
        query: Optional search query to pre-filter sessions
        project: Optional project name to pre-filter sessions

    Returns:
        Confirmation that the session picker was opened
    """
    # Get oikos context for run_id and trace_id
    ctx = get_oikos_context()
    if not ctx:
        return tool_error(
            ErrorType.MISSING_CONTEXT,
            "Cannot request session selection - no oikos context available",
        )

    # Build filters
    filters = {}
    if query:
        filters["query"] = query
    if project:
        filters["project"] = project

    # Emit SSE event to trigger frontend modal
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


# ---------------------------------------------------------------------------
# Tool registration and exports
# ---------------------------------------------------------------------------

# Note: We provide both func (sync) and coroutine (async) so LangChain
# can use whichever invocation method is appropriate for the runtime.
TOOLS: List[StructuredTool] = [
    StructuredTool.from_function(
        func=spawn_workspace_commis,
        coroutine=spawn_workspace_commis_async,
        name="spawn_workspace_commis",
        description="Spawn a commis to work in a workspace (PRIMARY tool for all commis work). "
        "Optionally clone a git repo by passing git_repo; otherwise uses an ephemeral scratch workspace. "
        "Runs a CLI agent (Claude Code) in an isolated workspace and captures changes. "
        "Use this for: reading code, analyzing dependencies, making changes, running tests, research. "
        "Pass skills=['skill-name'] to activate user skills in the commis prompt.",
    ),
    StructuredTool.from_function(
        func=list_commiss,
        coroutine=list_commiss_async,
        name="list_commiss",
        description="List recent commis jobs with SUMMARIES ONLY. "
        "Returns compressed summaries for quick scanning. "
        "Use read_commis_result(job_id) to get full details. "
        "This prevents context overflow when scanning 50+ commiss.",
    ),
    StructuredTool.from_function(
        func=read_commis_result,
        coroutine=read_commis_result_async,
        name="read_commis_result",
        description="Read the final result from a completed commis job. "
        "Provide the job ID (integer) to get the natural language result text.",
    ),
    StructuredTool.from_function(
        func=get_commis_evidence,
        coroutine=get_commis_evidence_async,
        name="get_commis_evidence",
        description="Compile raw tool evidence for a commis job within a byte budget. "
        "Use this to dereference [EVIDENCE:...] markers when you need full artifact details.",
    ),
    StructuredTool.from_function(
        func=get_tool_output,
        coroutine=get_tool_output_async,
        name="get_tool_output",
        description="Fetch a stored tool output by artifact_id. "
        "Use this to dereference [TOOL_OUTPUT:...] markers. "
        "Returns truncated output by default (max_bytes=32KB). Pass max_bytes=0 for full content.",
    ),
    StructuredTool.from_function(
        func=read_commis_file,
        coroutine=read_commis_file_async,
        name="read_commis_file",
        description="Read a specific file from a commis job's artifacts. "
        "Provide the job ID (integer) and file path to drill into commis details like "
        "tool outputs (tool_calls/*.txt), conversation history (thread.jsonl), or metadata (metadata.json).",
    ),
    StructuredTool.from_function(
        func=peek_commis_output,
        coroutine=peek_commis_output_async,
        name="peek_commis_output",
        description="Peek live output for a running commis (tail buffer). "
        "Provide the commis job ID and optional max_bytes. "
        "Best for seeing live runner_exec output without waiting for completion.",
    ),
    StructuredTool.from_function(
        func=grep_commiss,
        coroutine=grep_commiss_async,
        name="grep_commiss",
        description="Search across completed commis job artifacts for a text pattern. "
        "Performs case-insensitive search and returns matches with job IDs and context. "
        "Useful for finding jobs that encountered specific errors or outputs.",
    ),
    StructuredTool.from_function(
        func=get_commis_metadata,
        coroutine=get_commis_metadata_async,
        name="get_commis_metadata",
        description="Get detailed metadata about a commis job execution including "
        "task, status, timestamps, duration, and configuration. "
        "Provide the job ID (integer) to inspect job details.",
    ),
    # Async inbox model tools
    StructuredTool.from_function(
        func=check_commis_status,
        coroutine=check_commis_status_async,
        name="check_commis_status",
        description="Check the status of a specific commis or list all active commiss. "
        "Pass job_id for a specific commis, or call without arguments to see all active commiss. "
        "Use this to monitor background commiss without blocking.",
    ),
    StructuredTool.from_function(
        func=cancel_commis,
        coroutine=cancel_commis_async,
        name="cancel_commis",
        description="Cancel a running or queued commis job. "
        "The commis will abort at its next checkpoint. "
        "Use when a task is no longer needed or taking too long.",
    ),
    StructuredTool.from_function(
        func=wait_for_commis,
        coroutine=wait_for_commis_async,
        name="wait_for_commis",
        description="Wait for a specific commis to complete (blocking). "
        "Use sparingly - the async model is preferred. "
        "Only use when you need the result before proceeding.",
    ),
    # Session selection tool
    StructuredTool.from_function(
        func=request_session_selection,
        coroutine=request_session_selection_async,
        name="request_session_selection",
        description="Open a session picker modal for the user to select a past AI session. "
        "Use this when the user wants to resume a session but hasn't provided a specific ID. "
        "Optionally pre-filter by query text or project name.",
    ),
]

# ---------------------------------------------------------------------------
# Single source of truth for oikos tool names
# ---------------------------------------------------------------------------

# Tool names derived from TOOLS list - this is the canonical source
OIKOS_TOOL_NAMES: frozenset[str] = frozenset(t.name for t in TOOLS)

# Additional utility tools that oikoss need access to.
# These are NOT oikos-specific but are commonly used by the oikos agent.
# Organized by category for clarity.
_OIKOS_UTILITY_TOOL_LIST = [
    # Time/scheduling
    "get_current_time",
    # Web/HTTP
    "http_request",
    "web_search",
    "web_fetch",
    # Infrastructure
    "runner_list",
    "runner_create_enroll_token",
    # Communication
    "send_email",
    # Knowledge
    "knowledge_search",
    # Memory (persistent across sessions)
    "save_memory",
    "search_memory",
    "list_memories",
    "forget_memory",
    # Session discovery
    "search_sessions",
    "grep_sessions",
    "filter_sessions",
    "get_session_detail",
]

# Personal context tools are gated behind PERSONAL_TOOLS_ENABLED env var
# (Traccar/WHOOP/Obsidian are David-specific, not OSS core)
if os.getenv("PERSONAL_TOOLS_ENABLED", "").lower() in ("1", "true", "yes"):
    _OIKOS_UTILITY_TOOL_LIST.extend(
        [
            "get_current_location",
            "get_whoop_data",
            "search_notes",
        ]
    )

OIKOS_UTILITY_TOOLS: frozenset[str] = frozenset(_OIKOS_UTILITY_TOOL_LIST)


def get_oikos_allowed_tools() -> list[str]:
    """Get the complete list of tools a oikos agent should have access to.

    This is the SINGLE SOURCE OF TRUTH for oikos tool allowlists.
    Used by oikos_service.py when creating/updating oikos agents.

    Returns:
        Sorted list of tool names (oikos tools + utility tools)
    """
    return sorted(OIKOS_TOOL_NAMES | OIKOS_UTILITY_TOOLS)


# ---------------------------------------------------------------------------
# Commis tool subset — execution-focused, no coordinator tools
# ---------------------------------------------------------------------------

# Commis agents get a focused tool set for doing work in a workspace.
# They do NOT get coordinator tools (spawn_workspace_commis, manage commis jobs, etc.)
# because commis should not spawn other commis or inspect oikos state.
#
# Categories:
#   - Web/HTTP: fetch pages, search, API calls
#   - Project management: GitHub, Jira, Linear, Notion
#   - Communication: contact_user (ask questions), email, messaging
#   - Memory files: read/write workspace-scoped memory
#   - Knowledge: search knowledge base
#   - Session discovery: look up past session context
#   - Tasks: create/manage tasks
#   - Time: get current time
#   - Runner: execute commands on infrastructure

COMMIS_TOOL_NAMES: frozenset[str] = frozenset(
    [
        # Time
        "get_current_time",
        # Web/HTTP
        "http_request",
        "web_search",
        "web_fetch",
        # Communication
        "contact_user",
        "send_email",
        "send_slack_webhook",
        "send_discord_webhook",
        # Project management — GitHub
        "github_list_repositories",
        "github_create_issue",
        "github_list_issues",
        "github_get_issue",
        "github_add_comment",
        "github_list_pull_requests",
        "github_get_pull_request",
        # Project management — Jira
        "jira_create_issue",
        "jira_list_issues",
        "jira_get_issue",
        "jira_add_comment",
        "jira_transition_issue",
        "jira_update_issue",
        # Project management — Linear
        "linear_create_issue",
        "linear_list_issues",
        "linear_get_issue",
        "linear_update_issue",
        "linear_add_comment",
        "linear_list_teams",
        # Project management — Notion
        "notion_create_page",
        "notion_get_page",
        "notion_update_page",
        "notion_search",
        "notion_query_database",
        "notion_append_blocks",
        # Memory files (workspace-scoped persistent context)
        "memory_write",
        "memory_read",
        "memory_ls",
        "memory_search",
        "memory_delete",
        # Knowledge
        "knowledge_search",
        # Session discovery (look up past work for context)
        "search_sessions",
        "grep_sessions",
        "filter_sessions",
        "get_session_detail",
        # Tasks
        "task_create",
        "task_list",
        "task_update",
        "task_delete",
        # Runner execution
        "runner_exec",
    ]
)


def get_commis_allowed_tools() -> list[str]:
    """Get the complete list of tools a commis agent should have access to.

    Commis agents are execution-focused workers that operate in git workspaces.
    They get tools for doing work (web, project management, communication, etc.)
    but NOT coordinator tools (spawn_workspace_commis, manage commis jobs, etc.).

    This is the SINGLE SOURCE OF TRUTH for commis tool allowlists.

    Returns:
        Sorted list of tool names for commis agents
    """
    return sorted(COMMIS_TOOL_NAMES)
