"""Commis Runner – execute fiche tasks as disposable commis with artifact persistence.

This service runs a fiche as a "commis" - a disposable execution unit that persists
all outputs (tool calls, messages, results) to the filesystem. Concierges can later
retrieve and analyze commis results.

The CommisRunner is a thin wrapper around FicheRunner that intercepts tool calls
and messages to persist them via CommisArtifactStore.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.messages import BaseMessage
from langchain_core.messages import ToolMessage
from openai import AsyncOpenAI
from sqlalchemy.orm import Session

from zerg.context import CommisContext
from zerg.context import reset_commis_context
from zerg.context import set_commis_context
from zerg.crud import crud
from zerg.events import CommisEmitter
from zerg.events import reset_emitter
from zerg.events import set_emitter
from zerg.managers.fiche_runner import FicheRunner
from zerg.models.models import Fiche as FicheModel
from zerg.models_config import DEFAULT_COMMIS_MODEL_ID
from zerg.prompts import build_commis_prompt
from zerg.services.commis_artifact_store import CommisArtifactStore
from zerg.services.thread_service import ThreadService
from zerg.services.thread_service import _db_to_langchain

logger = logging.getLogger(__name__)


@dataclass
class CommisResult:
    """Result from a commis execution.

    Attributes
    ----------
    commis_id
        Unique identifier for the commis
    status
        Final status: "success", "failed", "timeout"
    result
        Natural language result from the fiche (full text)
    summary
        Compressed summary (~150 chars) for context efficiency
    error
        Error message if status is "failed"
    duration_ms
        Execution duration in milliseconds
    """

    commis_id: str
    status: str
    result: str
    summary: str = ""
    error: str | None = None
    duration_ms: int = 0


class CommisRunner:
    """Execute fiches as disposable commis with automatic artifact persistence."""

    def __init__(self, artifact_store: CommisArtifactStore | None = None):
        """Initialize the commis runner.

        Parameters
        ----------
        artifact_store
            Optional artifact store instance. If None, commis runs without artifact storage.
        """
        self.artifact_store = artifact_store

    async def run_commis(
        self,
        db: Session,
        task: str,
        fiche: FicheModel | None = None,
        fiche_config: dict[str, Any] | None = None,
        timeout: int = 300,
        event_context: dict[str, Any] | None = None,
        job_id: int | None = None,
    ) -> CommisResult:
        """Execute a task as a commis fiche.

        This method:
        1. Creates a commis directory via artifact_store
        2. Creates a fresh thread for this commis
        3. Runs the fiche with the task
        4. Captures all tool calls and persists to files
        5. Captures all messages to thread.jsonl
        6. Extracts final assistant message as result
        7. Marks commis complete
        8. Returns CommisResult with commis_id and result text

        Parameters
        ----------
        db
            Active SQLAlchemy Session
        task
            Task instructions for the commis
        fiche
            Optional FicheModel to use. If None, creates a temporary fiche.
        fiche_config
            Optional config overrides (model, tools, system prompt, etc.)
        timeout
            Maximum execution time in seconds (enforced via asyncio.wait_for)

        Returns
        -------
        CommisResult
            Result object with commis_id, status, and result text

        Raises
        ------
        Exception
            If fiche execution fails
        """
        start_time = datetime.now(timezone.utc)
        event_ctx = event_context or {}
        trace_id = event_ctx.get("trace_id") if event_ctx else None
        owner_for_events = None
        if fiche is not None:
            owner_for_events = getattr(fiche, "owner_id", None)
        elif fiche_config:
            owner_for_events = fiche_config.get("owner_id")

        # Create commis directory
        config = fiche_config or {}
        if fiche:
            config.setdefault("fiche_id", fiche.id)
            config.setdefault("model", fiche.model)

        # Create commis - artifact store is optional
        if self.artifact_store:
            commis_id = self.artifact_store.create_commis(task, config=config)
        else:
            # Generate commis_id without artifact storage
            import uuid

            commis_id = f"commis-{uuid.uuid4().hex[:12]}"
        logger.info(f"Created commis {commis_id} for task: {task[:50]}...")

        # Set up commis context for tool event emission
        # This context is read by concierge_react_engine._call_tool_async to emit
        # COMMIS_TOOL_STARTED/COMPLETED/FAILED events
        # job_id is critical for roundabout event correlation
        # trace_id enables end-to-end debugging (inherited from concierge via CommisJob)
        commis_context = CommisContext(
            commis_id=commis_id,
            owner_id=owner_for_events,
            course_id=event_ctx.get("course_id"),
            job_id=job_id,
            trace_id=event_ctx.get("trace_id"),
            task=task[:100],
        )
        context_token = set_commis_context(commis_context)

        # Set up injected emitter for event emission (Phase 2 of emitter refactor)
        # CommisEmitter (aliased as CommisEmitter) always emits commis_tool_* events regardless of contextvar state
        # Note: Emitter does NOT hold a DB session - event emission opens its own session
        commis_emitter = CommisEmitter(
            commis_id=commis_id,
            owner_id=owner_for_events,
            course_id=event_ctx.get("course_id"),
            job_id=job_id,
            trace_id=trace_id,
        )
        emitter_token = set_emitter(commis_emitter)

        # Set up metrics collector for performance tracking
        from zerg.commis_metrics import MetricsCollector
        from zerg.commis_metrics import reset_metrics_collector
        from zerg.commis_metrics import set_metrics_collector

        metrics_collector = MetricsCollector(commis_id)
        set_metrics_collector(metrics_collector)

        temp_fiche = False  # Track temporary fiche for cleanup on failure
        try:
            # Start commis (marks as running)
            if self.artifact_store:
                self.artifact_store.start_commis(commis_id)
            if event_context is not None and event_ctx.get("course_id"):
                await self._emit_event(
                    db=db,
                    course_id=event_ctx["course_id"],
                    event_type="commis_started",
                    payload={
                        "job_id": job_id,
                        "commis_id": commis_id,
                        "owner_id": owner_for_events,
                        "task": task[:100],
                        "trace_id": trace_id,
                    },
                )

            # Create or use existing fiche
            if fiche is None:
                # Create temporary fiche for this commis
                fiche = await self._create_temporary_fiche(db, task, config)
                temp_fiche = True
            else:
                temp_fiche = False

            # Create fresh thread for this commis
            title = f"Commis: {task[:50]}"
            thread = ThreadService.create_thread_with_system_message(
                db,
                fiche,
                title=title,
                thread_type="manual",  # Use "manual" for commis executions
                active=False,
            )

            # Insert task as user message
            crud.create_thread_message(
                db=db,
                thread_id=thread.id,
                role="user",
                content=task,
                processed=False,
            )

            # Run fiche and capture messages (with timeout enforcement)
            # Use reasoning_effort from config (inherited from concierge) or default to "none"
            commis_reasoning_effort = config.get("reasoning_effort", "none")
            runner = FicheRunner(fiche, reasoning_effort=commis_reasoning_effort)
            try:
                created_messages = await asyncio.wait_for(runner.run_thread(db, thread), timeout=timeout)
            except asyncio.TimeoutError:
                raise RuntimeError(f"Commis execution timed out after {timeout} seconds")

            # Convert database models to LangChain messages for processing
            langchain_messages = [_db_to_langchain(msg) for msg in created_messages]

            # Persist messages to thread.jsonl (include injected system/context)
            await self._persist_messages(commis_id, thread.id, db, fiche_id=fiche.id)

            # Persist tool calls to separate files
            await self._persist_tool_calls(commis_id, langchain_messages)

            # Extract final result (last assistant message)
            result_text = self._extract_result(langchain_messages)

            # Fallback: if no final assistant message, synthesize from tool outputs
            # This handles cases where the LLM produced tool calls but no final summary
            if not result_text:
                result_text = self._synthesize_from_tool_outputs(langchain_messages, task)
                if result_text:
                    logger.info(f"Commis {commis_id}: synthesized result from tool outputs (no final assistant message)")

            # Phase 6: Check for critical errors
            # If a critical error occurred during execution, mark as failed
            if commis_context.has_critical_error:
                # Calculate duration
                end_time = datetime.now(timezone.utc)
                duration_ms = int((end_time - start_time).total_seconds() * 1000)

                # Save the error message as result
                error_result = result_text or commis_context.critical_error_message or "(Critical error)"
                if self.artifact_store:
                    self.artifact_store.save_result(commis_id, error_result)

                # Mark commis failed
                if self.artifact_store:
                    self.artifact_store.complete_commis(commis_id, status="failed", error=commis_context.critical_error_message)

                if event_context is not None and event_ctx.get("course_id"):
                    await self._emit_event(
                        db=db,
                        course_id=event_ctx["course_id"],
                        event_type="commis_complete",
                        payload={
                            "job_id": job_id,
                            "commis_id": commis_id,
                            "status": "failed",
                            "error": commis_context.critical_error_message,
                            "duration_ms": duration_ms,
                            "owner_id": owner_for_events,
                            "trace_id": trace_id,
                        },
                    )

                    # Resume concierge if it was waiting for this commis (interrupt/resume pattern)
                    await self._resume_concierge_if_waiting(
                        db=db,
                        course_id=event_ctx["course_id"],
                        status="failed",
                        error=commis_context.critical_error_message,
                        job_id=job_id,
                    )

                logger.error(f"Commis {commis_id} failed due to critical error after {duration_ms}ms")

                return CommisResult(
                    commis_id=commis_id,
                    status="failed",
                    result=error_result,
                    error=commis_context.critical_error_message,
                    duration_ms=duration_ms,
                )

            # Always save result, even if empty (for consistency)
            saved_result = result_text or "(No result generated)"
            if self.artifact_store:
                self.artifact_store.save_result(commis_id, saved_result)

            # Calculate duration
            end_time = datetime.now(timezone.utc)
            duration_ms = int((end_time - start_time).total_seconds() * 1000)

            # Mark commis complete (system status - BEFORE summary extraction)
            if self.artifact_store:
                self.artifact_store.complete_commis(commis_id, status="success")

            # Extract summary (post-completion, safe to fail)
            result_for_summary = result_text or "(No result generated)"
            summary, summary_meta = await self._extract_summary(task, result_for_summary)
            if self.artifact_store:
                self.artifact_store.update_summary(commis_id, summary, summary_meta)

            if event_context is not None and event_ctx.get("course_id"):
                await self._emit_event(
                    db=db,
                    course_id=event_ctx["course_id"],
                    event_type="commis_complete",
                    payload={
                        "job_id": job_id,
                        "commis_id": commis_id,
                        "status": "success",
                        "duration_ms": duration_ms,
                        "owner_id": owner_for_events,
                        "trace_id": trace_id,
                    },
                )

                if summary:
                    await self._emit_event(
                        db=db,
                        course_id=event_ctx["course_id"],
                        event_type="commis_summary_ready",
                        payload={
                            "job_id": job_id,
                            "commis_id": commis_id,
                            "summary": summary,
                            "owner_id": owner_for_events,
                            "trace_id": trace_id,
                        },
                    )

                # Resume concierge if it was waiting for this commis (interrupt/resume pattern)
                await self._resume_concierge_if_waiting(
                    db=db,
                    course_id=event_ctx["course_id"],
                    status="success",
                    result_summary=summary or result_text,
                    job_id=job_id,
                )

            # Clean up temporary fiche if created
            if temp_fiche:
                # Cleanup is best-effort and should not flip a successful commis run into a failure.
                try:
                    crud.delete_fiche(db, fiche.id)
                    temp_fiche = False  # Prevent cleanup in finally
                except Exception:
                    db.rollback()
                    logger.warning(
                        "Failed to clean up temporary fiche %s after commis success",
                        getattr(fiche, "id", None),
                        exc_info=True,
                    )

            logger.info(f"Commis {commis_id} completed successfully in {duration_ms}ms")

            return CommisResult(
                commis_id=commis_id,
                status="success",
                result=result_text or "",
                summary=summary,
                duration_ms=duration_ms,
            )

        except Exception as e:
            # Calculate duration
            end_time = datetime.now(timezone.utc)
            duration_ms = int((end_time - start_time).total_seconds() * 1000)

            # Mark commis failed
            error_msg = str(e)
            if self.artifact_store:
                self.artifact_store.complete_commis(commis_id, status="failed", error=error_msg)

            logger.exception(f"Commis {commis_id} failed after {duration_ms}ms")

            if event_context is not None and event_ctx.get("course_id"):
                await self._emit_event(
                    db=db,
                    course_id=event_ctx["course_id"],
                    event_type="commis_complete",
                    payload={
                        "job_id": job_id,
                        "commis_id": commis_id,
                        "status": "failed",
                        "error": error_msg,
                        "duration_ms": duration_ms,
                        "owner_id": owner_for_events,
                        "trace_id": trace_id,
                    },
                )

                # Resume concierge if it was waiting for this commis (interrupt/resume pattern)
                await self._resume_concierge_if_waiting(
                    db=db,
                    course_id=event_ctx["course_id"],
                    status="failed",
                    error=error_msg,
                    job_id=job_id,
                )

            return CommisResult(
                commis_id=commis_id,
                status="failed",
                result="",
                error=error_msg,
                duration_ms=duration_ms,
            )
        finally:
            # Flush metrics to disk (best-effort)
            try:
                if self.artifact_store:
                    metrics_collector.flush(self.artifact_store)
            except Exception:
                logger.warning("Failed to flush metrics for commis %s", commis_id, exc_info=True)
            finally:
                reset_metrics_collector()

            # Always reset commis context and emitter to prevent leaking to other calls
            reset_commis_context(context_token)
            reset_emitter(emitter_token)

            # Ensure temporary fiches are not left behind on failure paths
            if temp_fiche and fiche:
                try:
                    crud.delete_fiche(db, fiche.id)
                except Exception:
                    db.rollback()
                    logger.warning("Failed to clean up temporary fiche after failure", exc_info=True)

    async def _emit_event(self, db: Session, course_id: int, event_type: str, payload: dict[str, Any]) -> None:
        """Best-effort event emission for commis lifecycle using durable event store."""
        try:
            from zerg.services.event_store import emit_course_event

            await emit_course_event(
                db=db,
                course_id=course_id,
                event_type=event_type,
                payload=payload,
            )
        except Exception:
            logger.warning("Failed to emit commis event %s", event_type, exc_info=True)

    async def _create_temporary_fiche(self, db: Session, task: str, config: dict[str, Any]) -> FicheModel:
        """Create a temporary fiche for a commis run.

        Commis get access to infrastructure tools (ssh_exec, http_request, etc.)
        following the "shell-first philosophy" - the terminal is the primitive.

        Parameters
        ----------
        db
            SQLAlchemy session
        task
            Task instructions
        config
            Configuration dict with optional model, system_instructions, owner_id, etc.

        Returns
        -------
        FicheModel
            Created fiche row
        """
        # Get owner_id from config or use first available user
        owner_id = config.get("owner_id")
        if owner_id is None:
            # Query for any user - this is a fallback for tests
            # In production, owner_id should always be provided
            from sqlalchemy import select

            from zerg.models.models import User

            result = db.execute(select(User).limit(1))
            user = result.scalar_one_or_none()
            if user is None:
                raise ValueError("No users found - cannot create commis fiche")
            owner_id = user.id
        else:
            # Fetch user object for context-aware prompt composition
            user = crud.get_user(db, owner_id)
            if not user:
                raise ValueError(f"User {owner_id} not found")

        # Default commis tools: infrastructure access + utilities
        #
        # runner_exec is the production/multi-user connector (outbound runner daemons).
        # ssh_exec remains available for legacy/power-user setups but should be treated
        # as a fallback over time.
        default_commis_tools = config.get(
            "allowed_tools",
            [
                "runner_exec",  # Preferred: execute via user-owned runner daemons
                "ssh_exec",  # Legacy fallback (requires backend key/network access)
                "http_request",  # API calls and web requests
                "get_current_time",  # Time lookups
                "send_email",  # Notifications (if configured)
                "contact_user",  # V1.3: notify owner about task completion/errors
                "knowledge_search",  # V1.1: user knowledge base search
                "web_search",  # V1.2: web search via Tavily
                "web_fetch",  # V1.2: fetch and parse web pages
            ],
        )

        # Create fiche (status is set automatically to "idle")
        fiche = crud.create_fiche(
            db=db,
            owner_id=owner_id,
            name=f"Commis: {task[:30]}",
            model=config.get("model", DEFAULT_COMMIS_MODEL_ID),
            system_instructions=config.get(
                "system_instructions",
                build_commis_prompt(user),
            ),
            task_instructions=task,
        )

        # Set allowed tools for infrastructure access
        fiche.allowed_tools = default_commis_tools
        db.commit()
        db.refresh(fiche)

        logger.debug(f"Created temporary fiche {fiche.id} for commis")
        return fiche

    async def _persist_messages(self, commis_id: str, thread_id: int, db: Session, *, fiche_id: int) -> None:
        """Persist all thread messages to thread.jsonl.

        Parameters
        ----------
        commis_id
            Commis identifier
        thread_id
            Thread ID to read messages from
        db
            SQLAlchemy session
        fiche_id
            Fiche ID used for runtime system prompt injection
        """
        # Skip if no artifact store
        if not self.artifact_store:
            return

        # Include runtime-injected system/context messages for debugging.
        # These are NOT stored in the DB (see FicheRunner.run_thread), but they
        # are critical for understanding commis behavior post-hoc.
        try:
            from zerg.connectors.status_builder import build_fiche_context
            from zerg.crud import crud as _crud
            from zerg.prompts.connector_protocols import get_connector_protocols

            fiche_row = _crud.get_fiche(db, fiche_id)
            if fiche_row and fiche_row.system_instructions:
                protocols = get_connector_protocols()
                system_content = f"{protocols}\n\n{fiche_row.system_instructions}"
                self.artifact_store.save_message(
                    commis_id,
                    {
                        "role": "system",
                        "content": system_content,
                        "timestamp": None,
                    },
                )

                # Connector status context (ephemeral at runtime) – include for debugging.
                context_text = build_fiche_context(
                    db=db,
                    owner_id=fiche_row.owner_id,
                    fiche_id=fiche_row.id,
                    allowed_tools=getattr(fiche_row, "allowed_tools", None),
                    compact_json=True,
                )
                self.artifact_store.save_message(
                    commis_id,
                    {
                        "role": "system",
                        "content": f"[INTERNAL CONTEXT - Do not mention unless asked]\n{context_text}",
                        "timestamp": None,
                    },
                )
        except Exception:
            logger.debug("Failed to persist injected system/context messages for commis %s", commis_id, exc_info=True)

        messages = crud.get_thread_messages(db, thread_id=thread_id)

        for msg in messages:
            message_dict = {
                "role": msg.role,
                "content": msg.content,
                "timestamp": msg.sent_at.isoformat() if msg.sent_at else None,
            }

            # Include tool_calls for assistant messages
            if msg.role == "assistant" and msg.tool_calls:
                message_dict["tool_calls"] = msg.tool_calls

            # Include tool_call_id for tool messages
            if msg.role == "tool" and msg.tool_call_id:
                message_dict["tool_call_id"] = msg.tool_call_id
                message_dict["name"] = msg.name

            self.artifact_store.save_message(commis_id, message_dict)

    async def _persist_tool_calls(self, commis_id: str, messages: list[BaseMessage]) -> None:
        """Persist tool call outputs to separate files.

        Parameters
        ----------
        commis_id
            Commis identifier
        messages
            List of LangChain messages (assistant + tool messages)
        """
        # Skip if no artifact store
        if not self.artifact_store:
            return

        sequence = 1

        for msg in messages:
            if isinstance(msg, ToolMessage):
                # Extract tool name from the message
                tool_name = getattr(msg, "name", "unknown_tool")
                output = msg.content

                # Save tool output
                self.artifact_store.save_tool_output(commis_id, tool_name, output, sequence)
                sequence += 1

    def _extract_result(self, messages: list[BaseMessage]) -> str | None:
        """Extract the final result from assistant messages.

        The result is the content of the last assistant message (after all tool calls).

        Parameters
        ----------
        messages
            List of LangChain messages

        Returns
        -------
        str | None
            Final assistant message content, or None if not found
        """
        # Find last assistant message
        for msg in reversed(messages):
            if isinstance(msg, AIMessage):
                # Get content - may be string or list
                content = msg.content
                if isinstance(content, list):
                    # Handle list of content blocks (multimodal messages)
                    text_parts = [part["text"] if isinstance(part, dict) else str(part) for part in content if part]
                    content = " ".join(text_parts)
                elif content:
                    content = str(content)
                else:
                    content = ""

                # Skip if it's just tool calls with no text
                if content and content.strip():
                    return content.strip()

        return None

    def _synthesize_from_tool_outputs(self, messages: list[BaseMessage], task: str) -> str | None:
        """Synthesize a result from tool outputs when assistant message is empty.

        This is a fallback mechanism when the LLM produces an empty final message
        but tool calls were successfully executed. We extract the last few tool
        outputs and create a minimal summary.

        Parameters
        ----------
        messages
            List of LangChain messages
        task
            Original task for context

        Returns
        -------
        str | None
            Synthesized result from tool outputs, or None if no useful tools found
        """
        # Collect tool outputs (most recent first, up to 3)
        tool_outputs: list[tuple[str, str]] = []
        for msg in reversed(messages):
            if isinstance(msg, ToolMessage):
                tool_name = getattr(msg, "name", "tool")
                content = msg.content
                if isinstance(content, str) and content.strip():
                    # Truncate very long outputs
                    truncated = content[:2000] if len(content) > 2000 else content
                    tool_outputs.append((tool_name, truncated))
                    if len(tool_outputs) >= 3:
                        break

        if not tool_outputs:
            return None

        # Build synthesized result
        parts = ["[Commis completed task but produced no final summary. Tool outputs below:]"]
        for tool_name, output in reversed(tool_outputs):  # Chronological order
            parts.append(f"\n--- {tool_name} ---\n{output}")

        return "\n".join(parts)

    async def _extract_summary(self, task: str, result: str) -> tuple[str, dict[str, Any]]:
        """Extract compressed summary for context efficiency.

        Uses LLM to generate a concise summary focusing on outcomes.
        Falls back to truncation if LLM fails.

        Parameters
        ----------
        task
            Original task description
        result
            Full result text from the commis

        Returns
        -------
        tuple[str, dict]
            (summary, summary_meta) tuple
        """
        SUMMARY_VERSION = 1
        MAX_CHARS = 150

        try:
            # LLM extraction
            prompt = f"""Task: {task}
Result: {result[:1000]}

Provide a {MAX_CHARS}-character summary focusing on outcomes, not actions.
Be factual and concise. Do NOT add status judgments.

Example: "Backup completed 157GB in 17s, no errors found"
"""
            # Track timing for metrics
            start_time = datetime.now(timezone.utc)

            client = AsyncOpenAI()
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=DEFAULT_COMMIS_MODEL_ID,
                    messages=[{"role": "user", "content": prompt}],
                    max_completion_tokens=50,
                ),
                timeout=5.0,
            )

            end_time = datetime.now(timezone.utc)

            # Record metrics if collector is available
            from zerg.commis_metrics import get_metrics_collector

            collector = get_metrics_collector()
            if collector:
                # Extract token usage from OpenAI response
                usage = response.usage
                duration_ms = int((end_time - start_time).total_seconds() * 1000)
                prompt_tokens = usage.prompt_tokens if usage else None
                completion_tokens = usage.completion_tokens if usage else None
                total_tokens = usage.total_tokens if usage else None

                collector.record_llm_call(
                    phase="summary",
                    model=DEFAULT_COMMIS_MODEL_ID,
                    start_ts=start_time,
                    end_ts=end_time,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                )

                # Tier 3 telemetry: Structured logging for dev visibility (opaque to LLMs)
                # This provides real-time grep-able logs alongside metrics.jsonl for monitoring/debugging
                try:
                    from zerg.context import get_commis_context

                    ctx = get_commis_context()
                    log_extra = {
                        "phase": "summary",
                        "model": DEFAULT_COMMIS_MODEL_ID,
                        "duration_ms": duration_ms,
                    }
                    if ctx:
                        log_extra["commis_id"] = ctx.commis_id
                    if prompt_tokens is not None:
                        log_extra["prompt_tokens"] = prompt_tokens
                    if completion_tokens is not None:
                        log_extra["completion_tokens"] = completion_tokens
                    if total_tokens is not None:
                        log_extra["total_tokens"] = total_tokens

                    logger.info("llm_call_complete", extra=log_extra)
                except Exception:
                    # Telemetry logging is best-effort - don't fail the commis
                    pass

            summary = response.choices[0].message.content.strip()
            if len(summary) > MAX_CHARS:
                summary = summary[: MAX_CHARS - 3] + "..."

            return summary, {
                "version": SUMMARY_VERSION,
                "model": DEFAULT_COMMIS_MODEL_ID,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }

        except Exception as e:
            # Fallback: truncation
            logger.warning(f"Summary extraction failed: {e}")
            summary = result[: MAX_CHARS - 3] + "..." if len(result) > MAX_CHARS else result

            return summary, {
                "version": SUMMARY_VERSION,
                "model": "truncation-fallback",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "error": str(e),
            }

    async def _resume_concierge_if_waiting(
        self,
        db: Session,
        course_id: int,
        status: str,
        result_summary: str | None = None,
        error: str | None = None,
        job_id: int | None = None,
    ) -> None:
        """Resume interrupted concierge if waiting for commis (NON-BLOCKING).

        Uses barrier pattern for parallel commis:
        - Checks if there's a CommisBarrier for this run
        - If so, updates barrier and triggers batch resume only when ALL commis complete
        - Falls back to single-commis resume for backwards compatibility

        This is fire-and-forget to prevent commis "duration" from including concierge synthesis time.

        Parameters
        ----------
        db
            SQLAlchemy session (NOTE: This session belongs to commis_runner - resume
            will need to create its own session)
        course_id
            Concierge run ID
        status
            Commis status: "success" or "failed"
        result_summary
            Brief summary of commis result
        error
            Error message if failed
        job_id
            Commis job ID (required for barrier pattern)
        """
        try:
            # Capture commis_id for E2E schema isolation - must happen BEFORE asyncio.create_task
            # which runs with an empty context (to prevent emitter leakage)
            from zerg.middleware.commis_db import current_commis_id

            captured_commis_id = current_commis_id.get()

            async def _run_resume_async():
                """Background task to resume with fresh DB session."""
                from zerg.database import get_session_factory
                from zerg.models.commis_barrier import CommisBarrier
                from zerg.models.enums import CourseStatus
                from zerg.models.models import Course
                from zerg.services.commis_resume import check_and_resume_if_all_complete
                from zerg.services.commis_resume import resume_concierge_batch
                from zerg.services.commis_resume import resume_concierge_with_commis_result

                # Restore commis_id context for E2E schema routing
                # (asyncio.create_task with Context() clears all contextvars)
                token = None
                if captured_commis_id is not None:
                    token = current_commis_id.set(captured_commis_id)

                session_factory = get_session_factory()
                fresh_db = session_factory()
                try:
                    # Race-safety: the commis may finish before the concierge flips the run to WAITING.
                    # Retry briefly so we don't miss resuming in that window.
                    max_checks = 10
                    check_sleep_s = 0.2

                    for _ in range(max_checks):
                        run = fresh_db.query(Course).filter(Course.id == course_id).first()
                        if not run:
                            return

                        if run.status == CourseStatus.WAITING:
                            break

                        # Terminal states - nothing to resume
                        if run.status in (CourseStatus.SUCCESS, CourseStatus.FAILED, CourseStatus.CANCELLED):
                            return

                        await asyncio.sleep(check_sleep_s)
                        # Ensure we don't serve a cached status across retries
                        fresh_db.expire_all()
                    else:
                        logger.info(
                            "Concierge run %s never entered WAITING after commis completion; skipping resume",
                            course_id,
                        )
                        return

                    summary_text = result_summary
                    if not summary_text:
                        if status == "failed":
                            summary_text = f"Commis failed: {error or 'Unknown error'}"
                        else:
                            summary_text = "(No result summary)"

                    # Check if this run uses barrier pattern (parallel commis)
                    barrier = fresh_db.query(CommisBarrier).filter(CommisBarrier.course_id == course_id).first()

                    if barrier and job_id:
                        # BARRIER PATTERN: Use atomic barrier check
                        logger.info(f"Using barrier pattern for run {course_id}, job {job_id}")

                        barrier_result = await check_and_resume_if_all_complete(
                            db=fresh_db,
                            course_id=course_id,
                            job_id=job_id,
                            result=summary_text,
                            error=error if status == "failed" else None,
                        )

                        # CRITICAL: Commit barrier state changes (check_and_resume uses nested transaction)
                        # Without this commit, CommisBarrierJob updates and completed_count are rolled back
                        fresh_db.commit()

                        if barrier_result["status"] == "resume":
                            # This commis is the last one - trigger batch resume
                            logger.info(
                                f"Barrier complete for run {course_id}, triggering batch resume "
                                f"with {len(barrier_result['commis_results'])} results"
                            )
                            await resume_concierge_batch(
                                db=fresh_db,
                                course_id=course_id,
                                commis_results=barrier_result["commis_results"],
                            )
                        elif barrier_result["status"] == "waiting":
                            logger.info(f"Barrier for run {course_id}: {barrier_result['completed']}/{barrier_result['expected']} complete")
                        else:
                            logger.debug(f"Barrier check skipped for run {course_id}: {barrier_result.get('reason')}")
                    else:
                        # SINGLE-COMMIS PATH: Fall back to original resume for backwards compatibility
                        logger.debug(f"No barrier for run {course_id}, using single-commis resume")
                        await resume_concierge_with_commis_result(
                            db=fresh_db,
                            course_id=course_id,
                            commis_result=summary_text,
                            job_id=job_id,
                        )

                except Exception as e:
                    logger.exception(f"Background resume failed for run {course_id}: {e}")
                finally:
                    fresh_db.close()
                    # Clean up the commis_id context
                    if token is not None:
                        current_commis_id.reset(token)

            # IMPORTANT: create_task() captures current contextvars.
            #
            # CommisRunner.run_commis() sets CommisContext via contextvars so that commis tool calls
            # emit COMMIS_TOOL_* events. If we schedule the concierge resume task while that context
            # is still set, the resume execution inherits it and the concierge's tool calls can be
            # misclassified/emitted as COMMIS_TOOL_* (e.g., a replayed spawn_commis showing up as a
            # nested tool inside the spawn_commis card).
            import contextvars

            coro = _run_resume_async()
            try:
                asyncio.create_task(coro, context=contextvars.Context())
            except Exception:
                # If scheduling fails, close the coroutine to avoid
                # "coroutine was never awaited" RuntimeWarnings.
                coro.close()
                raise
            logger.debug(f"Resume task scheduled for run {course_id}")

        except Exception as e:
            logger.exception(f"Failed to schedule resume for run {course_id}: {e}")


__all__ = ["CommisRunner", "CommisResult"]
