"""Background commis execution — spawn hatch, capture result, ingest session.

This is the entire commis system. No job queue, no artifact store, no barriers.
Accept a task, run hatch as a subprocess, ingest the session, report back.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import uuid
from datetime import datetime
from datetime import timezone
from pathlib import Path

from zerg.database import db_session
from zerg.models.enums import RunStatus
from zerg.models.models import CommisTask
from zerg.models.models import Run
from zerg.services.cloud_executor import CloudExecutionResult
from zerg.services.cloud_executor import CloudExecutor

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Job execution
# ---------------------------------------------------------------------------


async def run_commis_job(job_id: int, *, session_factory=None) -> None:
    """Execute a commis job end-to-end: workspace → hatch → ingest → resume the parent run.

    Fire-and-forget background task. Opens its own DB sessions to avoid holding
    connections during long-running subprocess execution.

    session_factory: optional override for testing (defaults to global factory).
    """
    # 1. Load job, mark running
    with db_session(session_factory) as db:
        job = db.query(CommisTask).filter(CommisTask.id == job_id).first()
        if not job or job.status != "queued":
            return
        job.status = "running"
        job.started_at = datetime.now(timezone.utc)
        db.commit()

        # Extract what we need before closing the session
        task = job.task
        model = job.model
        config = job.config or {}
        parent_run_id = job.parent_run_id
        tool_call_id = job.tool_call_id

    git_repo = config.get("git_repo")
    backend = config.get("backend")
    resume_session_id = config.get("resume_session_id")
    base_branch = config.get("base_branch", "main")
    job_started_at = datetime.now(timezone.utc)

    executor = CloudExecutor()
    result: CloudExecutionResult | None = None
    workspace = None
    scratch_dir: Path | None = None

    try:
        # 2. Set up workspace
        if git_repo:
            from zerg.services.workspace_manager import WorkspaceManager

            wm = WorkspaceManager()
            workspace = await wm.setup(
                repo_url=git_repo,
                run_id=f"commis-{job_id}-{uuid.uuid4().hex[:8]}",
                base_branch=base_branch,
            )
            workspace_path = workspace.path
        else:
            scratch_dir = Path(tempfile.mkdtemp(prefix=f"commis-{job_id}-"))
            workspace_path = scratch_dir

        # 3. Inject workspace config (best-effort)
        _inject_workspace_config(workspace_path, config)

        # 4. Prepare session resume if needed
        prepared_resume_id = None
        if resume_session_id:
            prepared_resume_id = await _prepare_resume(resume_session_id, workspace_path, session_factory=session_factory)

        # 5. Run hatch
        result = await executor.run_commis(
            task=task,
            workspace_path=workspace_path,
            model=model,
            backend=backend,
            resume_session_id=prepared_resume_id,
        )

        # 6. Ingest session JSONL into timeline (best-effort)
        if result and result.status == "success":
            try:
                _ingest_workspace_session(workspace_path, job_id, job_started_at, session_factory=session_factory)
            except Exception as e:
                logger.warning("Failed to ingest workspace session for job %s: %s", job_id, e)

    except Exception as e:
        logger.exception("Commis job %s failed", job_id)
        result = CloudExecutionResult(
            status="failed",
            output="",
            error=str(e),
            exit_code=-1,
        )
    finally:
        if scratch_dir and scratch_dir.exists():
            shutil.rmtree(scratch_dir, ignore_errors=True)

    # 7. Update job status
    with db_session(session_factory) as db:
        job = db.query(CommisTask).filter(CommisTask.id == job_id).first()
        if not job:
            return
        if job.status == "cancelled":
            # Still need to resume the parent run so it doesn't hang
            if parent_run_id:
                await _resume_parent_run(parent_run_id, tool_call_id, "Commis job was cancelled by user.", session_factory=session_factory)
            return

        job.finished_at = datetime.now(timezone.utc)
        if result and result.status == "success":
            job.status = "success"
        else:
            job.status = "failed"
            job.error = result.error if result else "Unknown error"
        job.exit_code = result.exit_code if result else -1
        db.commit()

    # 8. resume the parent run if it was waiting
    if parent_run_id:
        result_text = _build_result_text(result)
        await _resume_parent_run(parent_run_id, tool_call_id, result_text, session_factory=session_factory)


# ---------------------------------------------------------------------------
# Workspace helpers
# ---------------------------------------------------------------------------


def _inject_workspace_config(workspace_path: Path, config: dict) -> None:
    """Best-effort injection of AGENTS.md, MCP settings, hooks."""
    try:
        from zerg.config import get_settings
        from zerg.services.workspace_manager import inject_agents_md
        from zerg.services.workspace_manager import inject_codex_mcp_settings
        from zerg.services.workspace_manager import inject_commis_hooks
        from zerg.services.workspace_manager import inject_mcp_settings

        inject_agents_md(workspace_path, project_name=config.get("project"))

        settings = get_settings()
        api_url = settings.public_site_url or "http://localhost:8080"
        inject_mcp_settings(workspace_path, api_url=api_url)
        inject_codex_mcp_settings(workspace_path, api_url=api_url)
        inject_commis_hooks(workspace_path, verify_command=config.get("verify_command"))
    except Exception as e:
        logger.warning("Failed to inject workspace config: %s", e)


async def _prepare_resume(session_id: str, workspace_path: Path, *, session_factory=None) -> str | None:
    """Prepare a Claude session file for --resume."""
    try:
        from zerg.services.session_continuity import prepare_claude_session_for_resume

        with db_session(session_factory) as db:
            return await prepare_claude_session_for_resume(
                session_id=session_id,
                workspace_path=workspace_path,
                db=db,
            )
    except Exception as e:
        logger.warning("Failed to prepare session for resume: %s", e)
        return None


# ---------------------------------------------------------------------------
# Session ingestion (lifted from old commis_job_processor)
# ---------------------------------------------------------------------------


def _ingest_workspace_session(
    workspace_path: Path,
    job_id: int,
    job_started_at: datetime,
    *,
    session_factory=None,
) -> str | None:
    """Ingest Claude Code session JSONL from workspace into the agent timeline.

    Returns the ingested session UUID, or None.
    """
    from zerg.services.agents_store import AgentsStore
    from zerg.services.agents_store import EventIngest
    from zerg.services.agents_store import SessionIngest
    from zerg.services.agents_store import SourceLineIngest
    from zerg.services.session_continuity import encode_cwd_for_claude
    from zerg.services.session_continuity import get_claude_config_dir
    from zerg.services.shipper.parser import extract_session_metadata
    from zerg.services.shipper.parser import parse_session_file

    config_dir = get_claude_config_dir()
    encoded_cwd = encode_cwd_for_claude(str(workspace_path.absolute()))
    session_dir = config_dir / "projects" / encoded_cwd

    if not session_dir.exists():
        return None

    # Find JSONL files modified after the job started
    started_naive = job_started_at.replace(tzinfo=None) if job_started_at.tzinfo else job_started_at
    candidates = [p for p in session_dir.glob("*.jsonl") if datetime.utcfromtimestamp(p.stat().st_mtime) >= started_naive]
    if not candidates:
        return None

    session_file = max(candidates, key=lambda p: p.stat().st_mtime)
    logger.info("Ingesting workspace session from %s for job %s", session_file, job_id)

    metadata = extract_session_metadata(session_file)
    source_path = str(session_file)

    # Lossless source-line archive
    source_lines: list[SourceLineIngest] = []
    with session_file.open("rb") as fh:
        offset = 0
        for raw in fh:
            raw_line = raw.rstrip(b"\r\n").decode("utf-8", errors="replace")
            source_lines.append(SourceLineIngest(source_path=source_path, source_offset=offset, raw_json=raw_line))
            offset += len(raw)

    if not source_lines:
        return None

    try:
        events = list(parse_session_file(session_file))
    except Exception as exc:
        logger.warning("Failed to parse workspace session %s: %s", session_file, exc)
        events = []

    event_ingests = [
        EventIngest(
            role=e.role,
            content_text=e.content_text,
            tool_name=e.tool_name,
            tool_input_json=e.tool_input_json,
            tool_output_text=e.tool_output_text,
            tool_call_id=e.tool_call_id,
            timestamp=e.timestamp,
        )
        for e in events
    ]

    session_ingest = SessionIngest(
        provider="claude",
        provider_session_id=metadata.session_id or f"commis-{job_id}",
        started_at=metadata.started_at or job_started_at,
        ended_at=metadata.ended_at,
        cwd=str(workspace_path),
        git_repo=None,  # ParsedSession doesn't track git_repo URL
        git_branch=metadata.git_branch,
        device_id=f"commis-{job_id}",
        environment="cloud",
        events=event_ingests,
        source_lines=source_lines,
    )

    with db_session(session_factory) as db:
        store = AgentsStore(db)
        result = store.ingest_session(session_ingest)
        db.commit()
        return str(result.session_id) if result else None


# ---------------------------------------------------------------------------
# Parent run resume
# ---------------------------------------------------------------------------


def _build_result_text(result: CloudExecutionResult | None) -> str:
    """Build a human-readable result string for the parent run."""
    if not result:
        return "Commis job failed: unknown error"
    if result.status == "success":
        output = (result.output or "").strip()
        return output[:2000] if output else "Commis completed successfully (no output)"
    return f"Commis failed: {result.error or 'unknown error'}"


async def _resume_parent_run(run_id: int, tool_call_id: str | None, result_text: str, *, session_factory=None) -> None:
    """Resume a parent run that was waiting for this commis.

    Handles serial chaining: if the continuation spawns another commis
    (RunnerInterrupted), we go back to WAITING instead of failing.
    """
    from zerg.managers.runtime_interrupt import RunnerInterrupted
    from zerg.managers.runtime_runner import RuntimeRunner

    try:
        with db_session(session_factory) as db:
            run = db.query(Run).filter(Run.id == run_id).first()
            if not run or run.status != RunStatus.WAITING:
                return

            # Resolve tool_call_id
            effective_tool_call_id = tool_call_id
            if run.pending_tool_call_id:
                effective_tool_call_id = run.pending_tool_call_id
                run.pending_tool_call_id = None
                db.commit()

            if not effective_tool_call_id:
                # Fallback: find most recent commis job for this run
                job = (
                    db.query(CommisTask)
                    .filter(CommisTask.parent_run_id == run_id, CommisTask.tool_call_id.isnot(None))
                    .order_by(CommisTask.created_at.desc())
                    .first()
                )
                if job:
                    effective_tool_call_id = job.tool_call_id

            if not effective_tool_call_id:
                logger.error("Cannot resume run %s: no tool_call_id", run_id)
                run.status = RunStatus.FAILED
                run.error = "No tool_call_id for commis resume"
                run.finished_at = datetime.now(timezone.utc)
                db.commit()
                return

            # Atomically WAITING → RUNNING
            updated = db.query(Run).filter(Run.id == run_id, Run.status == RunStatus.WAITING).update({Run.status: RunStatus.RUNNING})
            db.commit()
            if updated == 0:
                return

            # Run continuation
            run = db.query(Run).filter(Run.id == run_id).first()
            if not run:
                return

            runner = RuntimeRunner(
                run.fiche,
                model_override=run.model,
                reasoning_effort=run.reasoning_effort,
            )
            try:
                await runner.run_continuation(
                    db=db,
                    thread=run.thread,
                    tool_call_id=effective_tool_call_id,
                    tool_result=result_text,
                    run_id=run_id,
                )
            except RunnerInterrupted:
                # Serial chaining: continuation spawned another commis.
                # The spawn tool already created the job and fired the task.
                # Go back to WAITING.
                run.status = RunStatus.WAITING
                if runner.usage_total_tokens is not None:
                    run.total_tokens = (run.total_tokens or 0) + runner.usage_total_tokens
                db.commit()
                logger.info("Parent run %s re-interrupted during continuation (serial chain)", run_id)
                return

            run.status = RunStatus.SUCCESS
            run.finished_at = datetime.now(timezone.utc)
            if runner.usage_total_tokens is not None:
                run.total_tokens = (run.total_tokens or 0) + runner.usage_total_tokens
            db.commit()

    except Exception:
        logger.exception("Failed to resume parent run %s", run_id)
        try:
            with db_session(session_factory) as db:
                run = db.query(Run).filter(Run.id == run_id).first()
                if run and run.status == RunStatus.RUNNING:
                    run.status = RunStatus.FAILED
                    run.error = "Commis resume failed"
                    run.finished_at = datetime.now(timezone.utc)
                    db.commit()
        except Exception:
            pass
