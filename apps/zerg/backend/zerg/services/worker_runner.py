"""Worker Runner – execute agent tasks as disposable workers with artifact persistence.

This service runs an agent as a "worker" - a disposable execution unit that persists
all outputs (tool calls, messages, results) to the filesystem. Supervisors can later
retrieve and analyze worker results.

The WorkerRunner is a thin wrapper around AgentRunner that intercepts tool calls
and messages to persist them via WorkerArtifactStore.
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

from zerg.context import WorkerContext
from zerg.context import reset_worker_context
from zerg.context import set_worker_context
from zerg.crud import crud
from zerg.events import EventType
from zerg.events import event_bus
from zerg.managers.agent_runner import AgentRunner
from zerg.models.models import Agent as AgentModel
from zerg.models_config import DEFAULT_WORKER_MODEL_ID
from zerg.prompts import build_worker_prompt
from zerg.services.thread_service import ThreadService
from zerg.services.thread_service import _db_to_langchain
from zerg.services.worker_artifact_store import WorkerArtifactStore

logger = logging.getLogger(__name__)


@dataclass
class WorkerResult:
    """Result from a worker execution.

    Attributes
    ----------
    worker_id
        Unique identifier for the worker
    status
        Final status: "success", "failed", "timeout"
    result
        Natural language result from the agent (full text)
    summary
        Compressed summary (~150 chars) for context efficiency
    error
        Error message if status is "failed"
    duration_ms
        Execution duration in milliseconds
    """

    worker_id: str
    status: str
    result: str
    summary: str = ""
    error: str | None = None
    duration_ms: int = 0


class WorkerRunner:
    """Execute agents as disposable workers with automatic artifact persistence."""

    def __init__(self, artifact_store: WorkerArtifactStore | None = None):
        """Initialize the worker runner.

        Parameters
        ----------
        artifact_store
            Optional artifact store instance. If None, creates a default one.
        """
        self.artifact_store = artifact_store or WorkerArtifactStore()

    async def run_worker(
        self,
        db: Session,
        task: str,
        agent: AgentModel | None = None,
        agent_config: dict[str, Any] | None = None,
        timeout: int = 300,
        event_context: dict[str, Any] | None = None,
        job_id: int | None = None,
    ) -> WorkerResult:
        """Execute a task as a worker agent.

        This method:
        1. Creates a worker directory via artifact_store
        2. Creates a fresh thread for this worker
        3. Runs the agent with the task
        4. Captures all tool calls and persists to files
        5. Captures all messages to thread.jsonl
        6. Extracts final assistant message as result
        7. Marks worker complete
        8. Returns WorkerResult with worker_id and result text

        Parameters
        ----------
        db
            Active SQLAlchemy Session
        task
            Task instructions for the worker
        agent
            Optional AgentModel to use. If None, creates a temporary agent.
        agent_config
            Optional config overrides (model, tools, system prompt, etc.)
        timeout
            Maximum execution time in seconds (not yet enforced)

        Returns
        -------
        WorkerResult
            Result object with worker_id, status, and result text

        Raises
        ------
        Exception
            If agent execution fails
        """
        start_time = datetime.now(timezone.utc)
        event_ctx = event_context or {}
        owner_for_events = None
        if agent is not None:
            owner_for_events = getattr(agent, "owner_id", None)
        elif agent_config:
            owner_for_events = agent_config.get("owner_id")

        # Create worker directory
        config = agent_config or {}
        if agent:
            config.setdefault("agent_id", agent.id)
            config.setdefault("model", agent.model)
        worker_id = self.artifact_store.create_worker(task, config=config)
        logger.info(f"Created worker {worker_id} for task: {task[:50]}...")

        # Set up worker context for tool event emission
        # This context is read by zerg_react_agent._call_tool_async to emit
        # WORKER_TOOL_STARTED/COMPLETED/FAILED events
        # job_id is critical for roundabout event correlation
        worker_context = WorkerContext(
            worker_id=worker_id,
            owner_id=owner_for_events,
            run_id=event_ctx.get("run_id"),
            job_id=job_id,
            task=task[:100],
        )
        context_token = set_worker_context(worker_context)

        # Set up metrics collector for performance tracking
        from zerg.worker_metrics import MetricsCollector
        from zerg.worker_metrics import reset_metrics_collector
        from zerg.worker_metrics import set_metrics_collector
        metrics_collector = MetricsCollector(worker_id)
        set_metrics_collector(metrics_collector)

        temp_agent = False  # Track temporary agent for cleanup on failure
        try:
            # Start worker (marks as running)
            self.artifact_store.start_worker(worker_id)
            if event_context is not None:
                await self._emit_event(
                    EventType.WORKER_STARTED,
                    {
                        "event_type": EventType.WORKER_STARTED,
                        "job_id": job_id,
                        "worker_id": worker_id,
                        "owner_id": owner_for_events,
                        "run_id": event_ctx.get("run_id"),
                        "task": task[:100],
                    },
                )

            # Create or use existing agent
            if agent is None:
                # Create temporary agent for this worker
                agent = await self._create_temporary_agent(db, task, config)
                temp_agent = True
            else:
                temp_agent = False

            # Create fresh thread for this worker
            title = f"Worker: {task[:50]}"
            thread = ThreadService.create_thread_with_system_message(
                db,
                agent,
                title=title,
                thread_type="manual",  # Use "manual" for worker executions
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

            # Run agent and capture messages (with timeout enforcement)
            runner = AgentRunner(agent)
            try:
                created_messages = await asyncio.wait_for(
                    runner.run_thread(db, thread),
                    timeout=timeout
                )
            except asyncio.TimeoutError:
                raise RuntimeError(f"Worker execution timed out after {timeout} seconds")

            # Convert database models to LangChain messages for processing
            langchain_messages = [_db_to_langchain(msg) for msg in created_messages]

            # Persist messages to thread.jsonl
            await self._persist_messages(worker_id, thread.id, db)

            # Persist tool calls to separate files
            await self._persist_tool_calls(worker_id, langchain_messages)

            # Extract final result (last assistant message)
            result_text = self._extract_result(langchain_messages)

            # Fallback: if no final assistant message, synthesize from tool outputs
            # This handles cases where the LLM produced tool calls but no final summary
            if not result_text:
                result_text = self._synthesize_from_tool_outputs(langchain_messages, task)
                if result_text:
                    logger.info(
                        f"Worker {worker_id}: synthesized result from tool outputs "
                        "(no final assistant message)"
                    )

            # Phase 6: Check for critical errors
            # If a critical error occurred during execution, mark as failed
            if worker_context.has_critical_error:
                # Calculate duration
                end_time = datetime.now(timezone.utc)
                duration_ms = int((end_time - start_time).total_seconds() * 1000)

                # Save the error message as result
                error_result = result_text or worker_context.critical_error_message or "(Critical error)"
                self.artifact_store.save_result(worker_id, error_result)

                # Mark worker failed
                self.artifact_store.complete_worker(
                    worker_id,
                    status="failed",
                    error=worker_context.critical_error_message
                )

                if event_context is not None:
                    await self._emit_event(
                        EventType.WORKER_COMPLETE,
                        {
                            "event_type": EventType.WORKER_COMPLETE,
                            "job_id": job_id,
                            "worker_id": worker_id,
                            "status": "failed",
                            "error": worker_context.critical_error_message,
                            "duration_ms": duration_ms,
                            "owner_id": owner_for_events,
                            "run_id": event_ctx.get("run_id"),
                        },
                    )

                logger.error(f"Worker {worker_id} failed due to critical error after {duration_ms}ms")

                return WorkerResult(
                    worker_id=worker_id,
                    status="failed",
                    result=error_result,
                    error=worker_context.critical_error_message,
                    duration_ms=duration_ms,
                )

            # Always save result, even if empty (for consistency)
            saved_result = result_text or "(No result generated)"
            self.artifact_store.save_result(worker_id, saved_result)

            # Calculate duration
            end_time = datetime.now(timezone.utc)
            duration_ms = int((end_time - start_time).total_seconds() * 1000)

            # Mark worker complete (system status - BEFORE summary extraction)
            self.artifact_store.complete_worker(worker_id, status="success")

            # Extract summary (post-completion, safe to fail)
            result_for_summary = result_text or "(No result generated)"
            summary, summary_meta = await self._extract_summary(task, result_for_summary)
            self.artifact_store.update_summary(worker_id, summary, summary_meta)

            if event_context is not None:
                await self._emit_event(
                    EventType.WORKER_COMPLETE,
                    {
                        "event_type": EventType.WORKER_COMPLETE,
                        "job_id": job_id,
                        "worker_id": worker_id,
                        "status": "success",
                        "duration_ms": duration_ms,
                        "owner_id": owner_for_events,
                        "run_id": event_ctx.get("run_id"),
                    },
                )

                if summary:
                    await self._emit_event(
                        EventType.WORKER_SUMMARY_READY,
                        {
                            "event_type": EventType.WORKER_SUMMARY_READY,
                            "job_id": job_id,
                            "worker_id": worker_id,
                            "summary": summary,
                            "owner_id": owner_for_events,
                            "run_id": event_ctx.get("run_id"),
                        },
                    )

            # Clean up temporary agent if created
            if temp_agent:
                # Cleanup is best-effort and should not flip a successful worker run into a failure.
                try:
                    crud.delete_agent(db, agent.id)
                    temp_agent = False  # Prevent cleanup in finally
                except Exception:
                    db.rollback()
                    logger.warning(
                        "Failed to clean up temporary agent %s after worker success",
                        getattr(agent, "id", None),
                        exc_info=True,
                    )

            logger.info(f"Worker {worker_id} completed successfully in {duration_ms}ms")

            return WorkerResult(
                worker_id=worker_id,
                status="success",
                result=result_text or "",
                summary=summary,
                duration_ms=duration_ms,
            )

        except Exception as e:
            # Calculate duration
            end_time = datetime.now(timezone.utc)
            duration_ms = int((end_time - start_time).total_seconds() * 1000)

            # Mark worker failed
            error_msg = str(e)
            self.artifact_store.complete_worker(worker_id, status="failed", error=error_msg)

            logger.exception(f"Worker {worker_id} failed after {duration_ms}ms")

            if event_context is not None:
                await self._emit_event(
                    EventType.WORKER_COMPLETE,
                    {
                        "event_type": EventType.WORKER_COMPLETE,
                        "job_id": job_id,
                        "worker_id": worker_id,
                        "status": "failed",
                        "error": error_msg,
                        "duration_ms": duration_ms,
                        "owner_id": owner_for_events,
                        "run_id": event_ctx.get("run_id"),
                    },
                )

            return WorkerResult(
                worker_id=worker_id,
                status="failed",
                result="",
                error=error_msg,
                duration_ms=duration_ms,
            )
        finally:
            # Flush metrics to disk (best-effort)
            try:
                metrics_collector.flush(self.artifact_store)
            except Exception:
                logger.warning("Failed to flush metrics for worker %s", worker_id, exc_info=True)
            finally:
                reset_metrics_collector()

            # Always reset worker context to prevent leaking to other calls
            reset_worker_context(context_token)

            # Ensure temporary agents are not left behind on failure paths
            if temp_agent and agent:
                try:
                    crud.delete_agent(db, agent.id)
                except Exception:
                    db.rollback()
                    logger.warning("Failed to clean up temporary agent after failure", exc_info=True)

    async def _emit_event(self, event_type: EventType, payload: dict[str, Any]) -> None:
        """Best-effort event emission for worker lifecycle."""
        try:
            await event_bus.publish(event_type, payload)
        except Exception:
            logger.warning("Failed to emit worker event %s", event_type, exc_info=True)

    async def _create_temporary_agent(
        self, db: Session, task: str, config: dict[str, Any]
    ) -> AgentModel:
        """Create a temporary agent for a worker run.

        Workers get access to infrastructure tools (ssh_exec, http_request, etc.)
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
        AgentModel
            Created agent row
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
                raise ValueError("No users found - cannot create worker agent")
            owner_id = user.id
        else:
            # Fetch user object for context-aware prompt composition
            user = crud.get_user(db, owner_id)
            if not user:
                raise ValueError(f"User {owner_id} not found")

        # Default worker tools: infrastructure access + utilities
        #
        # runner_exec is the production/multi-user connector (outbound runner daemons).
        # ssh_exec remains available for legacy/power-user setups but should be treated
        # as a fallback over time.
        default_worker_tools = config.get(
            "allowed_tools",
            [
                "runner_exec",       # Preferred: execute via user-owned runner daemons
                "ssh_exec",          # Legacy fallback (requires backend key/network access)
                "http_request",      # API calls and web requests
                "get_current_time",  # Time lookups
                "send_email",        # Notifications (if configured)
                "knowledge_search",  # V1.1: user knowledge base search
                "web_search",        # V1.2: web search via Tavily
                "web_fetch",         # V1.2: fetch and parse web pages
            ],
        )

        # Create agent (status is set automatically to "idle")
        agent = crud.create_agent(
            db=db,
            owner_id=owner_id,
            name=f"Worker: {task[:30]}",
            model=config.get("model", DEFAULT_WORKER_MODEL_ID),
            system_instructions=config.get(
                "system_instructions",
                build_worker_prompt(user),
            ),
            task_instructions=task,
        )

        # Set allowed tools for infrastructure access
        agent.allowed_tools = default_worker_tools
        db.commit()
        db.refresh(agent)

        logger.debug(f"Created temporary agent {agent.id} for worker")
        return agent

    async def _persist_messages(
        self, worker_id: str, thread_id: int, db: Session
    ) -> None:
        """Persist all thread messages to thread.jsonl.

        Parameters
        ----------
        worker_id
            Worker identifier
        thread_id
            Thread ID to read messages from
        db
            SQLAlchemy session
        """
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

            self.artifact_store.save_message(worker_id, message_dict)

    async def _persist_tool_calls(
        self, worker_id: str, messages: list[BaseMessage]
    ) -> None:
        """Persist tool call outputs to separate files.

        Parameters
        ----------
        worker_id
            Worker identifier
        messages
            List of LangChain messages (assistant + tool messages)
        """
        sequence = 1

        for msg in messages:
            if isinstance(msg, ToolMessage):
                # Extract tool name from the message
                tool_name = getattr(msg, "name", "unknown_tool")
                output = msg.content

                # Save tool output
                self.artifact_store.save_tool_output(
                    worker_id, tool_name, output, sequence
                )
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
                    text_parts = [
                        part["text"] if isinstance(part, dict) else str(part)
                        for part in content
                        if part
                    ]
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
        parts = ["[Worker completed task but produced no final summary. Tool outputs below:]"]
        for tool_name, output in reversed(tool_outputs):  # Chronological order
            parts.append(f"\n--- {tool_name} ---\n{output}")

        return "\n".join(parts)

    async def _extract_summary(
        self, task: str, result: str
    ) -> tuple[str, dict[str, Any]]:
        """Extract compressed summary for context efficiency.

        Uses LLM to generate a concise summary focusing on outcomes.
        Falls back to truncation if LLM fails.

        Parameters
        ----------
        task
            Original task description
        result
            Full result text from the worker

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
                    model=DEFAULT_WORKER_MODEL_ID,
                    messages=[{"role": "user", "content": prompt}],
                    max_completion_tokens=50,
                ),
                timeout=5.0,
            )

            end_time = datetime.now(timezone.utc)

            # Record metrics if collector is available
            from zerg.worker_metrics import get_metrics_collector
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
                    model=DEFAULT_WORKER_MODEL_ID,
                    start_ts=start_time,
                    end_ts=end_time,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                )

                # Tier 3 telemetry: Structured logging for dev visibility (opaque to LLMs)
                # This provides real-time grep-able logs alongside metrics.jsonl for monitoring/debugging
                try:
                    from zerg.context import get_worker_context
                    ctx = get_worker_context()
                    log_extra = {
                        "phase": "summary",
                        "model": DEFAULT_WORKER_MODEL_ID,
                        "duration_ms": duration_ms,
                    }
                    if ctx:
                        log_extra["worker_id"] = ctx.worker_id
                    if prompt_tokens is not None:
                        log_extra["prompt_tokens"] = prompt_tokens
                    if completion_tokens is not None:
                        log_extra["completion_tokens"] = completion_tokens
                    if total_tokens is not None:
                        log_extra["total_tokens"] = total_tokens

                    logger.info("llm_call_complete", extra=log_extra)
                except Exception:
                    # Telemetry logging is best-effort - don't fail the worker
                    pass

            summary = response.choices[0].message.content.strip()
            if len(summary) > MAX_CHARS:
                summary = summary[: MAX_CHARS - 3] + "..."

            return summary, {
                "version": SUMMARY_VERSION,
                "model": DEFAULT_WORKER_MODEL_ID,
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


__all__ = ["WorkerRunner", "WorkerResult"]
