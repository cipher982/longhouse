"""AgentRunner – asynchronous one-turn execution helper.

This class bridges:

• Agent ORM row (system instructions, model name, …)
• ThreadService for DB persistence
• ReAct execution loop (LangGraph-free by default, LangGraph for rollback)

Design goals
------------
1. Fully *async* – uses ``await runnable.ainvoke`` so no ``Future`` objects
   ever propagate.
2. Keep DB interactions synchronous for now (SQLAlchemy sync API).  These DB
   calls run inside FastAPI's request thread so they remain thread-safe.
3. Provide a thin synchronous wrapper ``run_thread_sync`` so legacy tests that
   call the method directly don't break.  This wrapper simply delegates to
   the async implementation via ``asyncio.run`` and will be removed once all
   call-sites are async.
4. Handle interrupt/resume pattern for async tool execution (spawn_worker).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any
from typing import Dict
from typing import Sequence
from typing import Tuple

from sqlalchemy.orm import Session

from zerg.agents_def import zerg_react_agent
from zerg.callbacks.token_stream import set_current_thread_id
from zerg.connectors.context import set_credential_resolver
from zerg.connectors.resolver import CredentialResolver
from zerg.connectors.status_builder import build_agent_context
from zerg.crud import crud
from zerg.models.models import Agent as AgentModel
from zerg.models.models import Thread as ThreadModel
from zerg.models.models import ThreadMessage as ThreadMessageModel
from zerg.prompts.connector_protocols import get_connector_protocols
from zerg.services.thread_service import ThreadService

# Feature flag: Use LangGraph runnable for initial supervisor run (rollback path)
# Default is False (use new LangGraph-free engine)
USE_LANGGRAPH_RUN_THREAD = os.getenv("USE_LANGGRAPH_RUN_THREAD", "0") == "1"

logger = logging.getLogger(__name__)


class AgentInterrupted(Exception):
    """Raised when the agent execution is interrupted (waiting for external input).

    This happens when spawn_worker() calls interrupt() - the graph checkpoints
    and returns control. The caller should set the run status to WAITING.
    """

    def __init__(self, interrupt_value: dict):
        self.interrupt_value = interrupt_value
        super().__init__(f"Agent interrupted: {interrupt_value}")


# ---------------------------------------------------------------------------
# Local in-memory cache for compiled LangGraph runnables.
# Keyed by (agent_id, agent_updated_at, stream_flag, model, reasoning_effort) so that any edit to the
# agent definition automatically busts the cache.  The cache is deliberately
# **process-local** – workers in a multi-process Gunicorn deployment will each
# compile their own runnable once on first use which is acceptable given the
# small cost (~100 ms).
# ---------------------------------------------------------------------------

_RUNNABLE_CACHE: Dict[Tuple[int, str, bool, str, str], Any] = {}


def _normalize_reasoning_effort(value: str | None) -> str:
    if not value:
        return ""
    v = value.strip().lower()
    if v in ("", "none"):
        return ""
    return v


@dataclass(frozen=True)
class AgentRuntimeView:
    """Read-only runtime view of an Agent row.

    IMPORTANT: This avoids mutating the SQLAlchemy-managed Agent ORM object.
    Per-request overrides (model, reasoning_effort) must not be persisted to DB
    and must not leak across concurrent runs.
    """

    id: int
    owner_id: int
    updated_at: Any
    model: str
    config: dict
    allowed_tools: Any


class AgentRunner:  # noqa: D401 – naming follows project conventions
    """Run one agent turn (async)."""

    def __init__(
        self,
        agent_row: AgentModel,
        *,
        thread_service: ThreadService | None = None,
        model_override: str | None = None,
        reasoning_effort: str | None = None,
    ):
        runtime_cfg = dict(getattr(agent_row, "config", {}) or {})
        if reasoning_effort is not None:
            runtime_cfg["reasoning_effort"] = reasoning_effort

        self.agent = AgentRuntimeView(
            id=agent_row.id,
            owner_id=agent_row.owner_id,
            updated_at=getattr(agent_row, "updated_at", None),
            model=model_override or agent_row.model,
            config=runtime_cfg,
            allowed_tools=getattr(agent_row, "allowed_tools", None),
        )
        self.thread_service = thread_service or ThreadService
        # Aggregated usage for the last run (provider metadata only)
        self.usage_prompt_tokens: int | None = None
        self.usage_completion_tokens: int | None = None
        self.usage_total_tokens: int | None = None
        self.usage_reasoning_tokens: int | None = None  # For reasoning models (gpt-5.x, o1, o3)

        # Whether this runner/LLM emits per-token chunks – treat env value
        # case-insensitively; anything truthy like "1", "true", "yes" enables
        # the feature.
        # Re-evaluate the *LLM_TOKEN_STREAM* env var **at runtime** so tests
        # that toggle the flag via ``monkeypatch.setenv`` after
        # ``zerg.constants`` was initially imported still take effect.

        # Resolve feature flag via *central* settings object so tests can
        # override through ``os.environ`` + ``constants._refresh_feature_flags``.

        from zerg.config import get_settings

        self.enable_token_stream = get_settings().llm_token_stream

        # ------------------------------------------------------------------
        # Lazily compile (or fetch from cache) the LangGraph runnable.  Using
        # a small in-process cache avoids the expensive graph compilation on
        # every single run (~100 ms) while still picking up changes whenever
        # the Agent row is modified (updated_at changes).
        # ------------------------------------------------------------------

        updated_at_str = agent_row.updated_at.isoformat() if getattr(agent_row, "updated_at", None) else "0"
        cache_key = (
            agent_row.id,
            updated_at_str,
            self.enable_token_stream,
            self.agent.model,
            _normalize_reasoning_effort(self.agent.config.get("reasoning_effort")),
        )

        if cache_key in _RUNNABLE_CACHE:
            self._runnable = _RUNNABLE_CACHE[cache_key]
            logger.debug("AgentRunner: using cached runnable for agent %s", agent_row.id)
        else:
            self._runnable = zerg_react_agent.get_runnable(self.agent)
            _RUNNABLE_CACHE[cache_key] = self._runnable
            logger.debug("AgentRunner: compiled & cached runnable for agent %s", agent_row.id)

    # ------------------------------------------------------------------
    # Public API – asynchronous
    # ------------------------------------------------------------------

    async def run_thread(self, db: Session, thread: ThreadModel) -> Sequence[ThreadMessageModel]:
        """Process unprocessed messages and return created assistant message rows."""

        logger.info(f"[AgentRunner] Starting run_thread for thread {thread.id}, agent {self.agent.id}", extra={"tag": "AGENT"})

        # Load conversation history from DB (excludes system messages - those are injected fresh)
        db_messages = self.thread_service.get_thread_messages_as_langchain(db, thread.id)

        # Filter out any system messages from DB (they're stale - we inject fresh below)
        conversation_msgs = [msg for msg in db_messages if not (hasattr(msg, "type") and msg.type == "system")]
        logger.debug(
            f"[AgentRunner] Retrieved {len(conversation_msgs)} conversation messages from thread (filtered out stale system messages)"
        )

        # ------------------------------------------------------------------
        # ALWAYS inject fresh system prompt from agent configuration
        # This ensures the agent always runs with current instructions,
        # even after history clears or prompt updates
        # ------------------------------------------------------------------
        from langchain_core.messages import SystemMessage

        # Load agent from DB to get current system_instructions
        agent_row = crud.get_agent(db, self.agent.id)
        if not agent_row or not agent_row.system_instructions:
            raise RuntimeError(f"Agent {self.agent.id} has no system_instructions")

        # Build system message with connector protocols prepended
        protocols = get_connector_protocols()
        system_content = f"{protocols}\n\n{agent_row.system_instructions}"
        system_msg = SystemMessage(content=system_content)

        # Start with system message
        original_msgs = [system_msg] + conversation_msgs
        logger.debug(
            f"[AgentRunner] Injected fresh system prompt ({len(agent_row.system_instructions)} chars) + {len(conversation_msgs)} conversation messages"
        )

        # ------------------------------------------------------------------
        # Inject connector status context into messages
        # Per PRD: Inject after system message but before conversation history
        # This gives the agent fresh awareness of which connectors are available
        # IMPORTANT: These injected messages are NOT saved to DB - they're
        # ephemeral context that gets regenerated fresh on every turn
        # ------------------------------------------------------------------
        try:
            context_text = build_agent_context(
                db=db,
                owner_id=self.agent.owner_id,
                agent_id=self.agent.id,
                allowed_tools=getattr(agent_row, "allowed_tools", None),
                compact_json=True,
            )
            # Inject as SystemMessage - this is background context, NOT user input
            # The agent should be aware of connector status but not discuss it
            # unless the user explicitly asks about integrations
            from langchain_core.messages import SystemMessage

            context_system_msg = SystemMessage(content=f"[INTERNAL CONTEXT - Do not mention unless asked]\n{context_text}")

            # Insert after main system message (index 0) if it exists
            if original_msgs and hasattr(original_msgs[0], "type") and original_msgs[0].type == "system":
                original_msgs = [original_msgs[0], context_system_msg] + original_msgs[1:]
            else:
                # No system message, prepend context
                original_msgs = [context_system_msg] + original_msgs

            logger.debug(
                "[AgentRunner] Injected connector context for agent %s (owner_id=%s)",
                self.agent.id,
                self.agent.owner_id,
            )
        except Exception as e:
            # Graceful degradation: if context injection fails, agent still runs
            logger.warning(
                "[AgentRunner] Failed to inject connector context: %s. Agent will run without status awareness.",
                e,
                exc_info=True,
                extra={"tag": "AGENT"},
            )

        unprocessed_rows = crud.get_unprocessed_messages(db, thread.id)
        logger.debug(f"[AgentRunner] Found {len(unprocessed_rows)} unprocessed messages")

        if not unprocessed_rows:
            logger.info("No unprocessed messages for thread %s", thread.id, extra={"tag": "AGENT"})
            return []  # Return empty list if no work

        # Configuration for thread persistence
        config = {
            "configurable": {
                "thread_id": str(thread.id),
            }
        }
        logger.debug(f"[AgentRunner] LangGraph config: {config}")

        # ------------------------------------------------------------------
        # Token-streaming context handling: set the *current* thread so the
        # ``WsTokenCallback`` can resolve the correct topic when forwarding
        # tokens.  We make sure to *always* reset afterwards to avoid leaking
        # state across concurrent agent turns.
        # ------------------------------------------------------------------

        # Set the context var and keep the **token** so we can restore safely
        _ctx_token = set_current_thread_id(thread.id)
        logger.debug("[AgentRunner] Set current thread ID context token")

        # ------------------------------------------------------------------
        # Credential resolver context: inject the resolver so connector tools
        # can access agent-specific credentials without explicit parameters.
        # The resolver now supports account-level fallback when owner_id is
        # provided (v2 account credentials architecture).
        # ------------------------------------------------------------------
        credential_resolver = CredentialResolver(
            agent_id=self.agent.id,
            db=db,
            owner_id=self.agent.owner_id,
        )
        _cred_ctx_token = set_credential_resolver(credential_resolver)
        logger.debug(
            "[AgentRunner] Set credential resolver context for agent %s (owner_id=%s)",
            self.agent.id,
            self.agent.owner_id,
        )

        try:
            # Track count of messages sent to LLM (including injected context)
            messages_with_context = len(original_msgs)
            logger.info(f"[AgentRunner] Calling LLM with {messages_with_context} messages (thread={thread.id})", extra={"tag": "LLM"})

            # Optional debug: dump full LLM input to file (set DEBUG_LLM_INPUT=1)
            if os.getenv("DEBUG_LLM_INPUT") == "1":
                import tempfile
                from pathlib import Path as PathLib

                debug_file = PathLib(tempfile.gettempdir()) / f"llm_input_agent{self.agent.id}_thread{thread.id}.txt"
                with open(debug_file, "w") as f:
                    f.write("=" * 80 + "\n")
                    f.write(f"LLM INPUT FOR AGENT {self.agent.id} (THREAD {thread.id})\n")
                    f.write("=" * 80 + "\n\n")
                    for i, msg in enumerate(original_msgs):
                        msg_type = type(msg).__name__
                        role = getattr(msg, "role", "unknown")
                        content = getattr(msg, "content", "")
                        f.write(f"Message {i} [{msg_type} role={role}]:\n")
                        f.write(f"{content}\n")
                        f.write("-" * 80 + "\n\n")
                logger.info(f"[DEBUG] Full LLM input written to: {debug_file}")

            # ------------------------------------------------------------------
            # Execute supervisor loop: LangGraph-free (default) or LangGraph (rollback)
            # ------------------------------------------------------------------
            if USE_LANGGRAPH_RUN_THREAD:
                # LEGACY PATH: Use LangGraph runnable
                logger.info("[AgentRunner] Using LangGraph runnable (USE_LANGGRAPH_RUN_THREAD=1)")

                # Reset LLM usage tracking before invoking
                from zerg.agents_def.zerg_react_agent import reset_llm_usage

                reset_llm_usage()

                result = await self._runnable.ainvoke(original_msgs, config)
            else:
                # NEW PATH: Use LangGraph-free supervisor_react_engine
                logger.info("[AgentRunner] Using LangGraph-free engine")

                from zerg.services.supervisor_react_engine import reset_llm_usage
                from zerg.services.supervisor_react_engine import run_supervisor_loop
                from zerg.tools.unified_access import get_tool_resolver

                # Get tools for this agent (use DB-loaded agent_row for fresh allowed_tools)
                resolver = get_tool_resolver()
                tools = resolver.filter_by_allowlist(agent_row.allowed_tools)

                reset_llm_usage()

                # Get run_id from supervisor context if available (set by SupervisorService)
                from zerg.services.supervisor_context import get_supervisor_context

                ctx = get_supervisor_context()
                run_id = ctx.run_id if ctx else None

                # Run the supervisor loop
                loop_result = await run_supervisor_loop(
                    messages=original_msgs,
                    agent_row=self.agent,
                    tools=tools,
                    run_id=run_id,
                    owner_id=self.agent.owner_id,
                    enable_token_stream=self.enable_token_stream,
                )

                # Capture usage from the new engine
                from zerg.services.supervisor_react_engine import get_llm_usage

                engine_usage = get_llm_usage()
                if engine_usage:
                    # Use explicit None check to preserve legitimate 0 values
                    self.usage_prompt_tokens = engine_usage.get("prompt_tokens")
                    self.usage_completion_tokens = engine_usage.get("completion_tokens")
                    self.usage_total_tokens = engine_usage.get("total_tokens")
                    self.usage_reasoning_tokens = engine_usage.get("reasoning_tokens")

                # Handle interrupt from new engine
                if loop_result.interrupted:
                    # Persist new messages before raising interrupt
                    if len(loop_result.messages) > messages_with_context:
                        new_messages = loop_result.messages[messages_with_context:]
                        # Filter out SystemMessages (ephemeral, not persisted)
                        new_messages = [m for m in new_messages if not (hasattr(m, "type") and m.type == "system")]
                        if new_messages:
                            created_rows = self.thread_service.save_new_messages(
                                db,
                                thread_id=thread.id,
                                messages=new_messages,
                                processed=True,
                            )
                            # Persist usage on last assistant message
                            usage_payload = {
                                "prompt_tokens": self.usage_prompt_tokens,
                                "completion_tokens": self.usage_completion_tokens,
                                "total_tokens": self.usage_total_tokens,
                                "reasoning_tokens": self.usage_reasoning_tokens,
                            }
                            if any(v is not None for v in usage_payload.values()):
                                last_assistant_row = next(
                                    (row for row in reversed(created_rows) if row.role == "assistant"),
                                    None,
                                )
                                if last_assistant_row is not None:
                                    existing_meta = dict(last_assistant_row.message_metadata or {})
                                    existing_meta["usage"] = usage_payload
                                    last_assistant_row.message_metadata = existing_meta
                                    db.commit()

                    # Mark user messages processed
                    self.thread_service.mark_messages_processed(db, (row.id for row in unprocessed_rows))
                    self.thread_service.touch_thread_timestamp(db, thread.id)

                    logger.info(f"[AgentRunner] Supervisor interrupted: {loop_result.interrupt_value}", extra={"tag": "AGENT"})
                    raise AgentInterrupted(loop_result.interrupt_value or {})

                # Convert SupervisorResult to expected format for downstream processing
                result = loop_result.messages

            # ------------------------------------------------------------------
            # Capture usage metadata (LangGraph path only - new engine captures above)
            # ------------------------------------------------------------------
            if USE_LANGGRAPH_RUN_THREAD:
                from zerg.agents_def.zerg_react_agent import get_llm_usage

                ctx_usage = get_llm_usage()
                logger.debug(f"[AgentRunner] Context variable usage: {ctx_usage}")

                if ctx_usage:
                    p_sum = ctx_usage.get("prompt_tokens", 0) or 0
                    c_sum = ctx_usage.get("completion_tokens", 0) or 0
                    t_sum = ctx_usage.get("total_tokens", 0) or 0
                    r_sum = ctx_usage.get("reasoning_tokens", 0) or 0

                    if p_sum or c_sum or t_sum or r_sum:
                        self.usage_prompt_tokens = p_sum if p_sum else None
                        self.usage_completion_tokens = c_sum if c_sum else None
                        self.usage_total_tokens = t_sum if t_sum else None
                        self.usage_reasoning_tokens = r_sum if r_sum else None

            # Log usage (both paths)
            if self.usage_total_tokens is not None:
                logger.info(
                    f"[AgentRunner] Usage: prompt={self.usage_prompt_tokens}, completion={self.usage_completion_tokens}, "
                    f"total={self.usage_total_tokens}, reasoning={self.usage_reasoning_tokens}",
                    extra={"tag": "LLM"},
                )

            # ------------------------------------------------------------------
            # Handle LangGraph interrupt (only for LangGraph path)
            # The new engine handles interrupts above and raises AgentInterrupted directly.
            # ------------------------------------------------------------------
            if USE_LANGGRAPH_RUN_THREAD and isinstance(result, dict) and "__interrupt__" in result:
                interrupts = result.get("__interrupt__") or []
                interrupt_value = None
                if interrupts:
                    interrupt_info = interrupts[0]
                    interrupt_value = getattr(interrupt_info, "value", interrupt_info)

                # CRITICAL: Persist the AIMessage with tool_calls BEFORE returning interrupt.
                # Without this, subsequent runs will see orphaned ToolMessages and OpenAI will reject.
                #
                # Strategy:
                # 1. FIRST: Try to get the actual AIMessage from context variable (set by zerg_react_agent)
                # 2. FALLBACK: Reconstruct from interrupt payload if context var is empty
                from zerg.agents_def.zerg_react_agent import clear_pending_ai_message
                from zerg.agents_def.zerg_react_agent import get_pending_ai_message

                pending_msg = get_pending_ai_message()
                logger.info(f"[INTERRUPT DEBUG] pending_msg={pending_msg}, type={type(pending_msg) if pending_msg else None}")
                if pending_msg and hasattr(pending_msg, "tool_calls") and pending_msg.tool_calls:
                    # Use the actual AIMessage from context (most accurate)
                    logger.info(f"[INTERRUPT] Persisting AIMessage from context var with {len(pending_msg.tool_calls)} tool call(s)")
                    self.thread_service.save_new_messages(
                        db,
                        thread_id=thread.id,
                        messages=[pending_msg],
                        processed=True,
                    )
                    clear_pending_ai_message()
                elif isinstance(interrupt_value, dict) and interrupt_value.get("tool_call_id"):
                    # Fallback: Reconstruct from interrupt payload
                    from langchain_core.messages import AIMessage

                    tool_call_id = interrupt_value["tool_call_id"]
                    task = interrupt_value.get("task", "")
                    model = interrupt_value.get("model")

                    reconstructed_ai_msg = AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "spawn_worker",
                                "args": {"task": task, "model": model},
                                "id": tool_call_id,
                                "type": "tool_call",
                            }
                        ],
                    )
                    logger.info(f"[INTERRUPT] Persisting reconstructed AIMessage with tool_call_id={tool_call_id}")
                    self.thread_service.save_new_messages(
                        db,
                        thread_id=thread.id,
                        messages=[reconstructed_ai_msg],
                        processed=True,
                    )

                # Best-effort persistence: if the graph returned message history, persist any new rows
                # before surfacing the interrupt to the caller.
                updated_messages_for_persist = result.get("messages") if isinstance(result.get("messages"), list) else None
                if updated_messages_for_persist and len(updated_messages_for_persist) > messages_with_context:
                    new_messages = updated_messages_for_persist[messages_with_context:]
                    logger.debug(
                        "[AgentRunner] Graph interrupted; persisting %s new message(s) before returning",
                        len(new_messages),
                        extra={"tag": "AGENT"},
                    )
                    created_rows = self.thread_service.save_new_messages(
                        db,
                        thread_id=thread.id,
                        messages=new_messages,
                        processed=True,
                    )

                    # Persist per-response token usage onto the *final* assistant message row (best-effort)
                    usage_payload = {
                        "prompt_tokens": self.usage_prompt_tokens,
                        "completion_tokens": self.usage_completion_tokens,
                        "total_tokens": self.usage_total_tokens,
                        "reasoning_tokens": self.usage_reasoning_tokens,
                    }
                    if any(v is not None for v in usage_payload.values()):
                        last_assistant_row = next((row for row in reversed(created_rows) if row.role == "assistant"), None)
                        if last_assistant_row is not None:
                            existing_meta = dict(last_assistant_row.message_metadata or {})
                            existing_meta["usage"] = usage_payload
                            last_assistant_row.message_metadata = existing_meta
                            db.commit()

                # Mark user messages processed even though the run is not complete yet.
                # The graph checkpoint captures the continuation state.
                self.thread_service.mark_messages_processed(db, (row.id for row in unprocessed_rows))
                self.thread_service.touch_thread_timestamp(db, thread.id)

                logger.info(f"[AgentRunner] Graph interrupted: {interrupt_value}", extra={"tag": "AGENT"})
                raise AgentInterrupted(interrupt_value or {})

            # Normal completion: result is a list of messages or a state dict with messages
            if isinstance(result, dict):
                updated_messages = result.get("messages")
                if not isinstance(updated_messages, list):
                    raise RuntimeError(f"Unexpected runnable result dict (no messages): {list(result.keys())}")
            else:
                updated_messages = result

            logger.info(
                f"[AgentRunner] Runnable completed. Received {len(updated_messages)} total messages",
                extra={"tag": "AGENT"},
            )

        except AgentInterrupted:
            # Interrupts are part of normal control flow for async tools (spawn_worker).
            raise
        except Exception as e:
            logger.exception(f"[AgentRunner] Exception during runnable.ainvoke: {e}")
            raise
        finally:
            # Reset context so unrelated calls aren't attributed to this thread
            # Use the tokens to restore previous state (Carmack-approved)
            from zerg.callbacks.token_stream import reset_current_thread_id
            from zerg.connectors.context import reset_credential_resolver

            reset_current_thread_id(_ctx_token)
            reset_credential_resolver(_cred_ctx_token)
            logger.debug("[AgentRunner] Reset thread ID and credential resolver context")

        # Extract only the new messages since our last context
        # The zerg_react_agent returns ALL messages including the history
        # We use messages_with_context to slice correctly - this includes the
        # ephemeral context injection that should NOT be saved to the database.
        if len(updated_messages) <= messages_with_context:
            logger.warning("No new messages generated by agent for thread %s", thread.id, extra={"tag": "AGENT"})
            return []

        new_messages = updated_messages[messages_with_context:]

        # Filter out SystemMessages (ephemeral context, not persisted to DB)
        new_messages = [m for m in new_messages if not (hasattr(m, "type") and m.type == "system")]
        logger.debug(f"[AgentRunner] Extracted {len(new_messages)} new messages (excluding system)")

        # Log each new message for debugging
        for i, msg in enumerate(new_messages):
            msg_type = type(msg).__name__
            role = getattr(msg, "role", "unknown")
            content_len = len(getattr(msg, "content", ""))
            logger.debug(f"[AgentRunner] New message {i}: {msg_type}, role={role}, content_length={content_len}")

        # Persist the assistant & tool messages
        logger.debug(f"[AgentRunner] Saving {len(new_messages)} new messages to database")
        created_rows = self.thread_service.save_new_messages(
            db,
            thread_id=thread.id,
            messages=new_messages,
            processed=True,
        )
        logger.info(f"[AgentRunner] Saved {len(created_rows)} message rows to database", extra={"tag": "AGENT"})

        # Persist per-response token usage onto the *final* assistant message row
        usage_payload = {
            "prompt_tokens": self.usage_prompt_tokens,
            "completion_tokens": self.usage_completion_tokens,
            "total_tokens": self.usage_total_tokens,
            "reasoning_tokens": self.usage_reasoning_tokens,
        }
        if any(v is not None for v in usage_payload.values()):
            last_assistant_row = next((row for row in reversed(created_rows) if row.role == "assistant"), None)
            if last_assistant_row is not None:
                existing_meta = dict(last_assistant_row.message_metadata or {})
                existing_meta["usage"] = usage_payload
                last_assistant_row.message_metadata = existing_meta
                db.commit()
                logger.debug("[AgentRunner] Stored usage metadata on assistant message row id=%s", last_assistant_row.id)

        # Mark user messages processed
        logger.debug(f"[AgentRunner] Marking {len(unprocessed_rows)} user messages as processed")
        self.thread_service.mark_messages_processed(db, (row.id for row in unprocessed_rows))

        # Touch timestamp
        self.thread_service.touch_thread_timestamp(db, thread.id)
        logger.debug("[AgentRunner] Updated thread timestamp")

        # ------------------------------------------------------------------
        # Safety net – if we *had* unprocessed user messages but the runnable
        # failed to generate **any** new assistant/tool message we treat this
        # as an error.
        # ------------------------------------------------------------------

        if unprocessed_rows and not created_rows:
            error_msg = "Agent produced no messages despite pending user input."
            logger.error(f"[AgentRunner] {error_msg}", extra={"tag": "AGENT"})
            raise RuntimeError(error_msg)

        logger.info(f"[AgentRunner] run_thread completed successfully for thread {thread.id}", extra={"tag": "AGENT"})
        return created_rows

    # ------------------------------------------------------------------
    # Continuation API (LangGraph-free path for worker resume)
    # ------------------------------------------------------------------

    async def run_continuation(
        self,
        db: Session,
        thread: ThreadModel,
        tool_call_id: str,
        tool_result: str,
        *,
        run_id: int | None = None,
    ) -> Sequence[ThreadMessageModel]:
        """Continue supervisor execution after worker completion.

        This method is called when a worker completes and the supervisor needs to
        resume. Unlike run_thread(), this does NOT use LangGraph checkpointing.

        Args:
            db: Database session.
            thread: Thread to continue.
            tool_call_id: The tool_call_id from the spawn_worker call.
            tool_result: The worker's result to inject as ToolMessage.
            run_id: Supervisor run ID for event correlation.

        Returns:
            List of new message rows created during continuation.

        Raises:
            AgentInterrupted: If spawn_worker is called again (sequential workers).
        """
        from langchain_core.messages import SystemMessage
        from langchain_core.messages import ToolMessage

        from zerg.connectors.context import reset_credential_resolver
        from zerg.connectors.context import set_credential_resolver
        from zerg.connectors.resolver import CredentialResolver
        from zerg.services.supervisor_react_engine import run_supervisor_loop
        from zerg.tools.unified_access import get_tool_resolver

        logger.info(
            f"[AgentRunner] Starting run_continuation for thread {thread.id}, tool_call_id={tool_call_id}",
            extra={"tag": "AGENT"},
        )

        # Load conversation history from DB
        db_messages = self.thread_service.get_thread_messages_as_langchain(db, thread.id)
        conversation_msgs = [msg for msg in db_messages if not (hasattr(msg, "type") and msg.type == "system")]

        # Check if ToolMessage for this tool_call_id already exists (idempotency)
        existing_tool_response = any(isinstance(m, ToolMessage) and m.tool_call_id == tool_call_id for m in db_messages)

        if existing_tool_response:
            logger.info(f"[AgentRunner] ToolMessage for tool_call_id={tool_call_id} already exists, skipping creation")
            # Reload conversation with existing tool message
            tool_msg = next(m for m in db_messages if isinstance(m, ToolMessage) and m.tool_call_id == tool_call_id)
        else:
            # Create ToolMessage for the worker result
            tool_msg = ToolMessage(
                content=f"Worker completed:\n\n{tool_result}",
                tool_call_id=tool_call_id,
                name="spawn_worker",
            )

            # Persist ToolMessage immediately
            self.thread_service.save_new_messages(
                db,
                thread_id=thread.id,
                messages=[tool_msg],
                processed=True,
            )
            logger.debug(f"[AgentRunner] Persisted ToolMessage for tool_call_id={tool_call_id}")

        # Build fresh system prompt
        agent_row = crud.get_agent(db, self.agent.id)
        if not agent_row or not agent_row.system_instructions:
            raise RuntimeError(f"Agent {self.agent.id} has no system_instructions")

        protocols = get_connector_protocols()
        system_content = f"{protocols}\n\n{agent_row.system_instructions}"
        system_msg = SystemMessage(content=system_content)

        # Build full message list for continuation
        # conversation_msgs already includes the AIMessage with tool_calls
        if existing_tool_response:
            # ToolMessage already in conversation_msgs, don't add again
            full_messages = [system_msg] + conversation_msgs
        else:
            # Add the newly created tool result
            full_messages = [system_msg] + conversation_msgs + [tool_msg]

        # Inject connector status context (same as run_thread)
        try:
            context_text = build_agent_context(
                db=db,
                owner_id=self.agent.owner_id,
                agent_id=self.agent.id,
                allowed_tools=getattr(agent_row, "allowed_tools", None),
                compact_json=True,
            )
            context_system_msg = SystemMessage(content=f"[INTERNAL CONTEXT - Do not mention unless asked]\n{context_text}")
            # Insert after main system message
            full_messages = [full_messages[0], context_system_msg] + full_messages[1:]
        except Exception as e:
            logger.warning(
                "[AgentRunner] Failed to inject connector context in continuation: %s",
                e,
                exc_info=True,
            )

        messages_with_context = len(full_messages)

        # Set up credential resolver context
        credential_resolver = CredentialResolver(
            agent_id=self.agent.id,
            db=db,
            owner_id=self.agent.owner_id,
        )
        _cred_ctx_token = set_credential_resolver(credential_resolver)

        # Set up thread context for token streaming
        _ctx_token = set_current_thread_id(thread.id)

        try:
            # Get tools for the agent
            resolver = get_tool_resolver()
            allowed_tools = getattr(agent_row, "allowed_tools", None)
            tools = resolver.filter_by_allowlist(allowed_tools)

            # Reset usage tracking
            from zerg.services.supervisor_react_engine import reset_llm_usage

            reset_llm_usage()

            # Run the supervisor loop using the new engine
            result = await run_supervisor_loop(
                messages=full_messages,
                agent_row=self.agent,
                tools=tools,
                run_id=run_id,
                owner_id=self.agent.owner_id,
                enable_token_stream=self.enable_token_stream,
            )

            # Capture usage
            self.usage_prompt_tokens = result.usage.get("prompt_tokens")
            self.usage_completion_tokens = result.usage.get("completion_tokens")
            self.usage_total_tokens = result.usage.get("total_tokens")
            self.usage_reasoning_tokens = result.usage.get("reasoning_tokens")

            # Handle interrupt (supervisor spawned another worker)
            if result.interrupted:
                logger.info(
                    f"[AgentRunner] Continuation interrupted: {result.interrupt_value}",
                    extra={"tag": "AGENT"},
                )
                # Persist any new messages before raising
                if len(result.messages) > messages_with_context:
                    new_messages = result.messages[messages_with_context:]
                    # Filter out SystemMessages (ephemeral, not persisted)
                    new_messages = [m for m in new_messages if not (hasattr(m, "type") and m.type == "system")]
                    if new_messages:
                        self.thread_service.save_new_messages(
                            db,
                            thread_id=thread.id,
                            messages=new_messages,
                            processed=True,
                        )
                raise AgentInterrupted(result.interrupt_value or {})

            # Normal completion: extract new messages
            if len(result.messages) <= messages_with_context:
                logger.warning(
                    "No new messages generated during continuation for thread %s",
                    thread.id,
                    extra={"tag": "AGENT"},
                )
                return []

            new_messages = result.messages[messages_with_context:]

            # Filter out SystemMessages (they shouldn't be persisted - injected fresh each run)
            new_messages = [m for m in new_messages if not (hasattr(m, "type") and m.type == "system")]
            logger.debug(f"[AgentRunner] Extracted {len(new_messages)} new messages from continuation (excluding system)")

            # Persist new messages (excluding the ToolMessage we already saved)
            created_rows = self.thread_service.save_new_messages(
                db,
                thread_id=thread.id,
                messages=new_messages,
                processed=True,
            )
            logger.info(
                f"[AgentRunner] Saved {len(created_rows)} message rows from continuation",
                extra={"tag": "AGENT"},
            )

            # Persist usage on final assistant message
            usage_payload = {
                "prompt_tokens": self.usage_prompt_tokens,
                "completion_tokens": self.usage_completion_tokens,
                "total_tokens": self.usage_total_tokens,
                "reasoning_tokens": self.usage_reasoning_tokens,
            }
            if any(v is not None for v in usage_payload.values()):
                last_assistant_row = next(
                    (row for row in reversed(created_rows) if row.role == "assistant"),
                    None,
                )
                if last_assistant_row is not None:
                    existing_meta = dict(last_assistant_row.message_metadata or {})
                    existing_meta["usage"] = usage_payload
                    last_assistant_row.message_metadata = existing_meta
                    db.commit()

            # Touch thread timestamp
            self.thread_service.touch_thread_timestamp(db, thread.id)

            return created_rows

        finally:
            # Reset contexts
            from zerg.callbacks.token_stream import reset_current_thread_id

            reset_current_thread_id(_ctx_token)
            reset_credential_resolver(_cred_ctx_token)

    # No synchronous wrapper – all call-sites should be async going forward.
