"""AgentRunner – asynchronous one-turn execution helper.

This class bridges:

• Agent ORM row (system instructions, model name, …)
• ThreadService for DB persistence
• LangGraph **runnable** compiled from the functional ReAct definition.

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
"""

from __future__ import annotations

import logging
from typing import Any
from typing import Dict
from typing import Sequence
from typing import Tuple

from langchain_core.messages import AIMessage
from sqlalchemy.orm import Session

from zerg.agents_def import zerg_react_agent

# Token streaming context helper
from zerg.callbacks.token_stream import set_current_thread_id

# Connector credential context helper
from zerg.connectors.context import set_credential_resolver
from zerg.connectors.resolver import CredentialResolver

# Connector status injection for agent context awareness
from zerg.connectors.status_builder import build_agent_context
from zerg.crud import crud
from zerg.models.models import Agent as AgentModel
from zerg.models.models import Thread as ThreadModel
from zerg.models.models import ThreadMessage as ThreadMessageModel

# Static connector protocols for system prompt
from zerg.prompts.connector_protocols import get_connector_protocols
from zerg.services.thread_service import ThreadService

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Local in-memory cache for compiled LangGraph runnables.
# Keyed by (agent_id, agent_updated_at, stream_flag) so that any edit to the
# agent definition automatically busts the cache.  The cache is deliberately
# **process-local** – workers in a multi-process Gunicorn deployment will each
# compile their own runnable once on first use which is acceptable given the
# small cost (~100 ms).
# ---------------------------------------------------------------------------

_RUNNABLE_CACHE: Dict[Tuple[int, str, bool], Any] = {}


class AgentRunner:  # noqa: D401 – naming follows project conventions
    """Run one agent turn (async)."""

    def __init__(self, agent_row: AgentModel, *, thread_service: ThreadService | None = None):
        self.agent = agent_row
        self.thread_service = thread_service or ThreadService
        # Aggregated usage for the last run (provider metadata only)
        self.usage_prompt_tokens: int | None = None
        self.usage_completion_tokens: int | None = None
        self.usage_total_tokens: int | None = None

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
        cache_key = (agent_row.id, updated_at_str, self.enable_token_stream)

        if cache_key in _RUNNABLE_CACHE:
            self._runnable = _RUNNABLE_CACHE[cache_key]
            logger.debug("AgentRunner: using cached runnable for agent %s", agent_row.id)
        else:
            self._runnable = zerg_react_agent.get_runnable(agent_row)
            _RUNNABLE_CACHE[cache_key] = self._runnable
            logger.debug("AgentRunner: compiled & cached runnable for agent %s", agent_row.id)

    # ------------------------------------------------------------------
    # Public API – asynchronous
    # ------------------------------------------------------------------

    async def run_thread(self, db: Session, thread: ThreadModel) -> Sequence[ThreadMessageModel]:
        """Process unprocessed messages and return created assistant message rows."""

        logger.info(f"[AgentRunner] Starting run_thread for thread {thread.id}, agent {self.agent.id}")

        original_msgs = self.thread_service.get_thread_messages_as_langchain(db, thread.id)
        original_msg_count = len(original_msgs)  # Track count BEFORE injection
        logger.info(f"[AgentRunner] Retrieved {original_msg_count} original messages from thread")

        # ------------------------------------------------------------------
        # Prepend connector protocols to system message
        # Per PRD: Static protocols are part of system prompt (cacheable)
        # They define HOW to interpret the dynamic connector_status injected per-turn
        # ------------------------------------------------------------------
        if original_msgs and hasattr(original_msgs[0], "type") and original_msgs[0].type == "system":
            from langchain_core.messages import SystemMessage
            protocols = get_connector_protocols()
            original_system_content = original_msgs[0].content
            # Prepend protocols to system message
            enhanced_system_message = SystemMessage(
                content=f"{protocols}\n\n{original_system_content}"
            )
            original_msgs = [enhanced_system_message] + original_msgs[1:]
            logger.debug(
                "[AgentRunner] Prepended connector protocols to system message for agent %s",
                self.agent.id,
            )
        else:
            logger.warning(
                "[AgentRunner] No system message found in thread %s, skipping protocol injection",
                thread.id,
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
            )
            # Inject as SystemMessage - this is background context, NOT user input
            # The agent should be aware of connector status but not discuss it
            # unless the user explicitly asks about integrations
            from langchain_core.messages import SystemMessage

            context_system_msg = SystemMessage(
                content=f"[INTERNAL CONTEXT - Do not mention unless asked]\n{context_text}"
            )

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
            )

        unprocessed_rows = crud.get_unprocessed_messages(db, thread.id)
        logger.info(f"[AgentRunner] Found {len(unprocessed_rows)} unprocessed messages")

        if not unprocessed_rows:
            logger.info("No unprocessed messages for thread %s", thread.id)
            return []  # Return empty list if no work

        # Configuration for thread persistence
        config = {
            "configurable": {
                "thread_id": str(thread.id),
            }
        }
        logger.info(f"[AgentRunner] LangGraph config: {config}")

        # ------------------------------------------------------------------
        # Token-streaming context handling: set the *current* thread so the
        # ``WsTokenCallback`` can resolve the correct topic when forwarding
        # tokens.  We make sure to *always* reset afterwards to avoid leaking
        # state across concurrent agent turns.
        # ------------------------------------------------------------------

        # Set the context var and keep the **token** so we can restore safely
        _ctx_token = set_current_thread_id(thread.id)
        logger.info("[AgentRunner] Set current thread ID context token")

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
            # TODO: Token streaming needs LangChain version compatibility investigation
            # Track count of messages sent to LLM (including injected context)
            messages_with_context = len(original_msgs)
            logger.info(f"[AgentRunner] Calling runnable.ainvoke with {messages_with_context} messages")
            # Use **async** invoke with the entrypoint
            # Pass the messages list directly to the function
            # For Functional API, we use .ainvoke method with the config
            # The entrypoint function will return the full message history
            updated_messages = await self._runnable.ainvoke(original_msgs, config)
            logger.info(f"[AgentRunner] Runnable completed. Received {len(updated_messages)} total messages")
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
            logger.info("[AgentRunner] Reset thread ID and credential resolver context")

        # Extract only the new messages since our last context
        # The zerg_react_agent returns ALL messages including the history
        # We use messages_with_context to slice correctly - this includes the
        # ephemeral context injection that should NOT be saved to the database.
        if len(updated_messages) <= messages_with_context:
            logger.warning("No new messages generated by agent for thread %s", thread.id)
            return []

        new_messages = updated_messages[messages_with_context:]
        logger.info(f"[AgentRunner] Extracted {len(new_messages)} new messages")

        # Aggregate usage strictly from provider metadata
        p_sum = 0
        c_sum = 0
        t_sum = 0
        for msg in new_messages:
            if isinstance(msg, AIMessage):
                meta = getattr(msg, "response_metadata", None) or {}
                addl = getattr(msg, "additional_kwargs", None) or {}
                usage = (
                    meta.get("token_usage") or meta.get("usage") or addl.get("token_usage") or addl.get("usage") or {}
                )
                try:
                    p = int(usage.get("prompt_tokens", 0))
                    c = int(usage.get("completion_tokens", 0))
                    # Some providers include total_tokens; otherwise derive strictly if both components present
                    if "total_tokens" in usage:
                        t = int(usage.get("total_tokens", 0))
                    else:
                        t = p + c if ("prompt_tokens" in usage and "completion_tokens" in usage) else 0
                except Exception:
                    p = c = t = 0
                p_sum += p
                c_sum += c
                t_sum += t

        if p_sum or c_sum or t_sum:
            self.usage_prompt_tokens = p_sum if p_sum else None
            self.usage_completion_tokens = c_sum if c_sum else None
            # Prefer explicit totals if any contributed them; otherwise keep None when 0
            self.usage_total_tokens = t_sum if t_sum else None

        # Log each new message for debugging
        for i, msg in enumerate(new_messages):
            msg_type = type(msg).__name__
            role = getattr(msg, "role", "unknown")
            content_len = len(getattr(msg, "content", ""))
            logger.info(f"[AgentRunner] New message {i}: {msg_type}, role={role}, content_length={content_len}")

        # Persist the assistant & tool messages
        logger.info(f"[AgentRunner] Saving {len(new_messages)} new messages to database")
        created_rows = self.thread_service.save_new_messages(
            db,
            thread_id=thread.id,
            messages=new_messages,
            processed=True,
        )
        logger.info(f"[AgentRunner] Saved {len(created_rows)} message rows to database")

        # Mark user messages processed
        logger.info(f"[AgentRunner] Marking {len(unprocessed_rows)} user messages as processed")
        self.thread_service.mark_messages_processed(db, (row.id for row in unprocessed_rows))

        # Touch timestamp
        self.thread_service.touch_thread_timestamp(db, thread.id)
        logger.info("[AgentRunner] Updated thread timestamp")

        # ------------------------------------------------------------------
        # Safety net – if we *had* unprocessed user messages but the runnable
        # failed to generate **any** new assistant/tool message we treat this
        # as an error.  Without this guard the request would appear to succeed
        # (HTTP 202) yet the user sees no response in the UI and the bug can
        # stay unnoticed.
        # ------------------------------------------------------------------

        if unprocessed_rows and not created_rows:
            error_msg = "Agent produced no messages despite pending user input."
            logger.error(f"[AgentRunner] {error_msg}")
            raise RuntimeError(error_msg)

        # Return *all* created rows so callers can decide how to emit them
        # over WebSocket (assistant **and** tool messages).  The caller can
        # easily derive subsets by inspecting the ``role`` field.

        logger.info(f"[AgentRunner] run_thread completed successfully. Returning {len(created_rows)} created rows")
        return created_rows

    # No synchronous wrapper – all call-sites should be async going forward.
