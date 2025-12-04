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

from openai import AsyncOpenAI

from langchain_core.messages import AIMessage
from langchain_core.messages import BaseMessage
from langchain_core.messages import ToolMessage
from sqlalchemy.orm import Session

from zerg.crud import crud
from zerg.managers.agent_runner import AgentRunner
from zerg.models.models import Agent as AgentModel
from zerg.models_config import DEFAULT_WORKER_MODEL_ID
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

        # Create worker directory
        config = agent_config or {}
        if agent:
            config.setdefault("agent_id", agent.id)
            config.setdefault("model", agent.model)
        worker_id = self.artifact_store.create_worker(task, config=config)
        logger.info(f"Created worker {worker_id} for task: {task[:50]}...")

        temp_agent = False  # Track temporary agent for cleanup on failure
        try:
            # Start worker (marks as running)
            self.artifact_store.start_worker(worker_id)

            # Create or use existing agent
            if agent is None:
                # Create temporary agent for this worker
                agent = await self._create_temporary_agent(db, task, config)
                temp_agent = True
            else:
                temp_agent = False

            # Create fresh thread for this worker
            timestamp_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
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

            # Clean up temporary agent if created
            if temp_agent:
                crud.delete_agent(db, agent.id)
                db.commit()
                temp_agent = False  # Prevent cleanup in finally

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

            return WorkerResult(
                worker_id=worker_id,
                status="failed",
                result="",
                error=error_msg,
                duration_ms=duration_ms,
            )
        finally:
            # Ensure temporary agents are not left behind on failure paths
            if temp_agent and agent:
                try:
                    crud.delete_agent(db, agent.id)
                    db.commit()
                except Exception:
                    logger.warning("Failed to clean up temporary agent after failure", exc_info=True)

    async def _create_temporary_agent(
        self, db: Session, task: str, config: dict[str, Any]
    ) -> AgentModel:
        """Create a temporary agent for a worker run.

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

        # Create agent (status is set automatically to "idle")
        agent = crud.create_agent(
            db=db,
            owner_id=owner_id,
            name=f"Worker: {task[:30]}",
            model=config.get("model", DEFAULT_WORKER_MODEL_ID),
            system_instructions=config.get(
                "system_instructions",
                "You are a helpful assistant executing a specific task. "
                "Complete the task and provide a clear, concise result.",
            ),
            task_instructions=task,
        )
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
            client = AsyncOpenAI()
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=DEFAULT_WORKER_MODEL_ID,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=50,
                ),
                timeout=5.0,
            )

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
