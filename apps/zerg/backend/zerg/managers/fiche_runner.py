"""Runner – asynchronous one-turn execution helper.

This class bridges:

• Fiche ORM row (system instructions, model name, …)
• ThreadService for DB persistence
• ReAct execution loop (LangGraph-free)

Design goals
------------
1. Fully *async* – uses ``await runnable.ainvoke`` so no ``Future`` objects
   ever propagate.
2. Keep DB interactions synchronous for now (SQLAlchemy sync API).  These DB
   calls run inside FastAPI's request thread so they remain thread-safe.
3. Handle interrupt/resume pattern for async tool execution (spawn_commis).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any
from typing import List
from typing import Optional
from typing import Sequence

from langchain_core.messages import BaseMessage
from langchain_core.messages import HumanMessage
from langchain_core.messages import SystemMessage
from sqlalchemy.orm import Session

from zerg.callbacks.token_stream import set_current_thread_id
from zerg.config import get_settings
from zerg.connectors.context import set_credential_resolver
from zerg.connectors.resolver import CredentialResolver
from zerg.connectors.status_builder import build_fiche_context
from zerg.crud import crud
from zerg.crud import knowledge_crud
from zerg.models.models import Fiche as FicheModel
from zerg.models.models import Thread as ThreadModel
from zerg.models.models import ThreadMessage as ThreadMessageModel
from zerg.prompts.connector_protocols import get_connector_protocols
from zerg.services import memory_embeddings
from zerg.services import memory_search as memory_search_service
from zerg.services.thread_service import ThreadService
from zerg.tools.builtin.knowledge_tools import extract_snippets

logger = logging.getLogger(__name__)


_TIMESTAMP_PREFIX_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\]\s*")


def _strip_timestamp_prefix(text: str) -> str:
    return _TIMESTAMP_PREFIX_RE.sub("", text or "").strip()


def _truncate(text: str, max_chars: int = 220) -> str:
    if not text:
        return ""
    clean = " ".join(text.split())
    if len(clean) <= max_chars:
        return clean
    return clean[:max_chars].rstrip() + "…"


def _build_memory_context(
    db: Session,
    *,
    owner_id: int,
    query: str | None,
    memory_limit: int = 3,
    knowledge_limit: int = 3,
) -> str | None:
    if not query:
        return None

    settings = get_settings()
    use_embeddings = memory_embeddings.embeddings_enabled(settings)

    try:
        memory_hits = memory_search_service.search_memory_files(
            db,
            owner_id=owner_id,
            query=query,
            limit=memory_limit,
            use_embeddings=use_embeddings,
        )
    except Exception as e:
        logger.warning("Memory search failed: %s", e)
        memory_hits = []

    try:
        knowledge_hits = knowledge_crud.search_knowledge_documents(
            db,
            owner_id=owner_id,
            query=query,
            limit=knowledge_limit,
        )
    except Exception as e:
        logger.warning("Knowledge search failed: %s", e)
        knowledge_hits = []

    if not memory_hits and not knowledge_hits:
        return None

    lines = ["[MEMORY CONTEXT]"]

    if memory_hits:
        lines.append("Memory Files:")
        for hit in memory_hits:
            snippet = ""
            snippets = hit.get("snippets") or []
            if snippets:
                snippet = _truncate(snippets[0])
            lines.append(f"- {hit.get('path')}: {snippet}".rstrip())

    if knowledge_hits:
        lines.append("Knowledge Base:")
        for doc, source in knowledge_hits:
            snippets = extract_snippets(doc.content_text, query, max_snippets=1)
            snippet = _truncate(snippets[0]) if snippets else ""
            lines.append(f"- {source.name} :: {doc.path}: {snippet}".rstrip())

    return "\n".join(lines)


def _latest_user_query(
    *,
    unprocessed_rows: list[ThreadMessageModel] | None = None,
    conversation_msgs: list[BaseMessage] | None = None,
) -> str | None:
    if unprocessed_rows:
        for row in reversed(unprocessed_rows):
            if row.role == "user" and not getattr(row, "internal", False):
                return (row.content or "").strip() or None

    if conversation_msgs:
        for msg in reversed(conversation_msgs):
            if isinstance(msg, HumanMessage):
                return _strip_timestamp_prefix(msg.content)

    return None


class FicheInterrupted(Exception):
    """Raised when the agent execution is interrupted (waiting for external input).

    This happens when spawn_commis raises FicheInterrupted. The caller should
    set the run status to WAITING.
    """

    def __init__(self, interrupt_value: dict):
        self.interrupt_value = interrupt_value
        super().__init__(f"Fiche interrupted: {interrupt_value}")


@dataclass(frozen=True)
class RuntimeView:
    """Read-only runtime view of a Fiche row.

    IMPORTANT: This avoids mutating the SQLAlchemy-managed Fiche ORM object.
    Per-request overrides (model, reasoning_effort) must not be persisted to DB
    and must not leak across concurrent runs.
    """

    id: int
    owner_id: int
    updated_at: Any
    model: str
    config: dict
    allowed_tools: Any


class Runner:  # noqa: D401 – naming follows project conventions
    """Run one agent turn (async)."""

    def __init__(
        self,
        agent_row: FicheModel,
        *,
        thread_service: ThreadService | None = None,
        model_override: str | None = None,
        reasoning_effort: str | None = None,
    ):
        runtime_cfg = dict(getattr(agent_row, "config", {}) or {})
        if reasoning_effort is not None:
            runtime_cfg["reasoning_effort"] = reasoning_effort

        self.agent = RuntimeView(
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

    def _normalize_skill_allowlist(self, raw: Any) -> Optional[List[str]]:
        """Normalize allowed skills to a list of patterns or None."""
        if raw is None:
            return None
        if isinstance(raw, str):
            parts = [p.strip() for p in raw.split(",") if p.strip()]
            return parts or None
        if isinstance(raw, list):
            cleaned = [str(p).strip() for p in raw if str(p).strip()]
            return cleaned or None
        return None

    def _resolve_skill_settings(
        self, db: Session, agent_row: FicheModel
    ) -> tuple[Optional[List[str]], bool, Optional[int], Optional[str], bool]:
        """Resolve skill settings from agent config and user context."""
        cfg = dict(getattr(agent_row, "config", {}) or {})
        allowed = cfg.get("skills_allowlist")
        include_user = cfg.get("skills_include_user")
        max_skills = cfg.get("skills_max")
        workspace_path = cfg.get("skills_workspace_path") or cfg.get("workspace_path")
        enabled = cfg.get("skills_enabled")

        user = crud.get_user(db, self.agent.owner_id)
        ctx = user.context if user and isinstance(user.context, dict) else {}

        if allowed is None:
            allowed = ctx.get("skills_allowlist")
        if include_user is None:
            include_user = ctx.get("skills_include_user")
        if max_skills is None:
            max_skills = ctx.get("skills_max")
        if enabled is None:
            enabled = ctx.get("skills_enabled")

        allowed_list = self._normalize_skill_allowlist(allowed)
        include_user_flag = bool(include_user) if include_user is not None else False

        max_skills_val: Optional[int] = None
        if isinstance(max_skills, int):
            max_skills_val = max_skills
        elif isinstance(max_skills, str) and max_skills.strip().isdigit():
            max_skills_val = int(max_skills.strip())

        enabled_flag = True if enabled is None else bool(enabled)

        return allowed_list, include_user_flag, max_skills_val, workspace_path, enabled_flag

    def _build_skill_integration(
        self,
        db: Session,
        agent_row: FicheModel,
    ) -> tuple[Optional[Any], Optional[int]]:
        """Build SkillIntegration with resolved settings."""
        from zerg.skills.integration import SkillIntegration

        allowed_skills, include_user, max_skills, workspace_path, enabled = self._resolve_skill_settings(db, agent_row)
        if not enabled:
            return None, None

        integration = SkillIntegration(
            workspace_path=workspace_path,
            allowed_skills=allowed_skills,
            db=db,
            owner_id=self.agent.owner_id,
            include_user=include_user,
        )
        return integration, max_skills

    # ------------------------------------------------------------------
    # Public API – asynchronous
    # ------------------------------------------------------------------

    async def run_thread(self, db: Session, thread: ThreadModel) -> Sequence[ThreadMessageModel]:
        """Process unprocessed messages and return created assistant message rows."""

        logger.info(f"[Runner] Starting run_thread for thread {thread.id}, agent {self.agent.id}", extra={"tag": "AGENT"})

        # Load conversation history from DB (excludes system messages - those are injected fresh)
        db_messages = self.thread_service.get_thread_messages_as_langchain(db, thread.id)

        # Filter out any system messages from DB (they're stale - we inject fresh below)
        conversation_msgs = [msg for msg in db_messages if not (hasattr(msg, "type") and msg.type == "system")]
        logger.debug(f"[Runner] Retrieved {len(conversation_msgs)} conversation messages from thread (filtered out stale system messages)")

        # ------------------------------------------------------------------
        # ALWAYS inject fresh system prompt from agent configuration
        # This ensures the agent always runs with current instructions,
        # even after history clears or prompt updates
        # ------------------------------------------------------------------

        # Load agent from DB to get current system_instructions
        agent_row = crud.get_fiche(db, self.agent.id)
        if not agent_row or not agent_row.system_instructions:
            raise RuntimeError(f"Fiche {self.agent.id} has no system_instructions")

        # Build system message with connector protocols prepended
        protocols = get_connector_protocols()
        system_content = f"{protocols}\n\n{agent_row.system_instructions}"

        skill_integration, skill_max = self._build_skill_integration(db, agent_row)
        if skill_integration:
            try:
                skills_prompt = skill_integration.get_prompt(include_content=False, max_skills=skill_max)
                if skills_prompt:
                    system_content = f"{system_content}\n\n{skills_prompt}"
                    logger.debug("[Runner] Injected skills prompt for agent %s", self.agent.id)
            except Exception as e:
                logger.warning("[Runner] Failed to inject skills prompt: %s", e, exc_info=True)

        system_msg = SystemMessage(content=system_content)

        # Start with system message
        original_msgs = [system_msg] + conversation_msgs
        logger.debug(
            f"[Runner] Injected fresh system prompt ({len(agent_row.system_instructions)} chars) + {len(conversation_msgs)} conversation messages"
        )

        # ------------------------------------------------------------------
        # Inject connector status context into messages
        # Per PRD: Inject after system message but before conversation history
        # This gives the agent fresh awareness of which connectors are available
        # IMPORTANT: These injected messages are NOT saved to DB - they're
        # ephemeral context that gets regenerated fresh on every turn
        # ------------------------------------------------------------------
        try:
            context_text = build_fiche_context(
                db=db,
                owner_id=self.agent.owner_id,
                fiche_id=self.agent.id,
                allowed_tools=getattr(agent_row, "allowed_tools", None),
                compact_json=True,
            )
            # Inject as SystemMessage - this is background context, NOT user input
            # The agent should be aware of connector status but not discuss it
            # unless the user explicitly asks about integrations
            context_system_msg = SystemMessage(content=f"[INTERNAL CONTEXT - Do not mention unless asked]\n{context_text}")

            # Insert after main system message (index 0) if it exists
            if original_msgs and hasattr(original_msgs[0], "type") and original_msgs[0].type == "system":
                original_msgs = [original_msgs[0], context_system_msg] + original_msgs[1:]
            else:
                # No system message, prepend context
                original_msgs = [context_system_msg] + original_msgs

            logger.debug(
                "[Runner] Injected connector context for agent %s (owner_id=%s)",
                self.agent.id,
                self.agent.owner_id,
            )
        except Exception as e:
            # Graceful degradation: if context injection fails, agent still runs
            logger.warning(
                "[Runner] Failed to inject connector context: %s. Fiche will run without status awareness.",
                e,
                exc_info=True,
                extra={"tag": "AGENT"},
            )

        unprocessed_rows = crud.get_unprocessed_messages(db, thread.id)
        logger.debug(f"[Runner] Found {len(unprocessed_rows)} unprocessed messages")

        if not unprocessed_rows:
            logger.info("No unprocessed messages for thread %s", thread.id, extra={"tag": "AGENT"})
            return []  # Return empty list if no work

        # ------------------------------------------------------------------
        # Inject memory recall context (episodic + knowledge) before LLM call
        # ------------------------------------------------------------------
        memory_query = _latest_user_query(
            unprocessed_rows=unprocessed_rows,
            conversation_msgs=conversation_msgs,
        )
        memory_context = _build_memory_context(
            db,
            owner_id=self.agent.owner_id,
            query=memory_query,
        )
        if memory_context:
            memory_msg = SystemMessage(content=memory_context)
            insert_at = 1
            if len(original_msgs) > 1 and getattr(original_msgs[1], "type", None) == "system":
                insert_at = 2
            original_msgs = original_msgs[:insert_at] + [memory_msg] + original_msgs[insert_at:]
            logger.debug("[Runner] Injected memory context for thread %s", thread.id)

        # ------------------------------------------------------------------
        # Token-streaming context handling: set the *current* thread so the
        # ``WsTokenCallback`` can resolve the correct topic when forwarding
        # tokens.  We make sure to *always* reset afterwards to avoid leaking
        # state across concurrent agent turns.
        # ------------------------------------------------------------------

        # Set the context var and keep the **token** so we can restore safely
        _ctx_token = set_current_thread_id(thread.id)
        logger.debug("[Runner] Set current thread ID context token")

        # ------------------------------------------------------------------
        # Credential resolver context: inject the resolver so connector tools
        # can access agent-specific credentials without explicit parameters.
        # The resolver now supports account-level fallback when owner_id is
        # provided (v2 account credentials architecture).
        # ------------------------------------------------------------------
        credential_resolver = CredentialResolver(
            fiche_id=self.agent.id,
            db=db,
            owner_id=self.agent.owner_id,
        )
        _cred_ctx_token = set_credential_resolver(credential_resolver)
        logger.debug(
            "[Runner] Set credential resolver context for agent %s (owner_id=%s)",
            self.agent.id,
            self.agent.owner_id,
        )

        try:
            # Track count of messages sent to LLM (including injected context)
            messages_with_context = len(original_msgs)
            logger.info(f"[Runner] Calling LLM with {messages_with_context} messages (thread={thread.id})", extra={"tag": "LLM"})

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
            # Execute oikos loop (LangGraph-free)
            # ------------------------------------------------------------------
            from zerg.services.oikos_react_engine import get_llm_usage
            from zerg.services.oikos_react_engine import reset_llm_usage
            from zerg.services.oikos_react_engine import run_oikos_loop
            from zerg.tools.unified_access import get_tool_resolver

            # Get tools for this agent (use DB-loaded agent_row for fresh allowed_tools)
            resolver = get_tool_resolver()
            tools = resolver.filter_by_allowlist(agent_row.allowed_tools)
            if skill_integration:
                tool_map = {tool.name: tool for tool in tools}
                try:
                    skill_tools = skill_integration.get_tools(tool_map)
                    if skill_tools:
                        tools = tools + skill_tools
                except Exception as e:
                    logger.warning("[Runner] Failed to build skill tools: %s", e, exc_info=True)

            reset_llm_usage()

            # Get run_id and trace_id from context (oikos or commis)
            from zerg.context import get_commis_context
            from zerg.services.oikos_context import get_oikos_context

            sup_ctx = get_oikos_context()
            commis_ctx = get_commis_context()

            # Prefer oikos context, fall back to commis context for trace_id
            run_id = sup_ctx.run_id if sup_ctx else None
            trace_id = sup_ctx.trace_id if sup_ctx else (commis_ctx.trace_id if commis_ctx else None)

            # Run the oikos loop
            loop_result = await run_oikos_loop(
                messages=original_msgs,
                fiche_row=self.agent,
                tools=tools,
                run_id=run_id,
                owner_id=self.agent.owner_id,
                trace_id=trace_id,
                enable_token_stream=self.enable_token_stream,
            )

            # Capture usage from the engine
            engine_usage = get_llm_usage()
            if engine_usage:
                # Use explicit None check to preserve legitimate 0 values
                self.usage_prompt_tokens = engine_usage.get("prompt_tokens")
                self.usage_completion_tokens = engine_usage.get("completion_tokens")
                self.usage_total_tokens = engine_usage.get("total_tokens")
                self.usage_reasoning_tokens = engine_usage.get("reasoning_tokens")

            # Handle interrupt (spawn_commis was called)
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

                logger.info(f"[Runner] Oikos interrupted: {loop_result.interrupt_value}", extra={"tag": "AGENT"})
                raise FicheInterrupted(loop_result.interrupt_value or {})

            # Log usage
            if self.usage_total_tokens is not None:
                logger.info(
                    f"[Runner] Usage: prompt={self.usage_prompt_tokens}, completion={self.usage_completion_tokens}, "
                    f"total={self.usage_total_tokens}, reasoning={self.usage_reasoning_tokens}",
                    extra={"tag": "LLM"},
                )

            # Normal completion: result is a list of messages
            updated_messages = loop_result.messages

            logger.info(
                f"[Runner] Runnable completed. Received {len(updated_messages)} total messages",
                extra={"tag": "AGENT"},
            )

        except FicheInterrupted:
            # Interrupts are part of normal control flow for async tools (spawn_commis).
            raise
        except Exception as e:
            logger.exception(f"[Runner] Exception during runnable.ainvoke: {e}")
            raise
        finally:
            # Reset context so unrelated calls aren't attributed to this thread
            # Use the tokens to restore previous state (Carmack-approved)
            from zerg.callbacks.token_stream import reset_current_thread_id
            from zerg.connectors.context import reset_credential_resolver

            reset_current_thread_id(_ctx_token)
            reset_credential_resolver(_cred_ctx_token)
            logger.debug("[Runner] Reset thread ID and credential resolver context")

        # Extract only the new messages since our last context
        # The oikos loop returns ALL messages including the history
        # We use messages_with_context to slice correctly - this includes the
        # ephemeral context injection that should NOT be saved to the database.
        if len(updated_messages) <= messages_with_context:
            logger.warning("No new messages generated by agent for thread %s", thread.id, extra={"tag": "AGENT"})
            return []

        new_messages = updated_messages[messages_with_context:]

        # Filter out SystemMessages (ephemeral context, not persisted to DB)
        new_messages = [m for m in new_messages if not (hasattr(m, "type") and m.type == "system")]
        logger.debug(f"[Runner] Extracted {len(new_messages)} new messages (excluding system)")

        # Log each new message for debugging
        for i, msg in enumerate(new_messages):
            msg_type = type(msg).__name__
            role = getattr(msg, "role", "unknown")
            content_len = len(getattr(msg, "content", ""))
            logger.debug(f"[Runner] New message {i}: {msg_type}, role={role}, content_length={content_len}")

        # Persist the assistant & tool messages
        logger.debug(f"[Runner] Saving {len(new_messages)} new messages to database")
        created_rows = self.thread_service.save_new_messages(
            db,
            thread_id=thread.id,
            messages=new_messages,
            processed=True,
        )
        logger.info(f"[Runner] Saved {len(created_rows)} message rows to database", extra={"tag": "AGENT"})

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
                logger.debug("[Runner] Stored metadata on assistant message row id=%s", last_assistant_row.id)

        # Mark user messages processed
        logger.debug(f"[Runner] Marking {len(unprocessed_rows)} user messages as processed")
        self.thread_service.mark_messages_processed(db, (row.id for row in unprocessed_rows))

        # Touch timestamp
        self.thread_service.touch_thread_timestamp(db, thread.id)
        logger.debug("[Runner] Updated thread timestamp")

        # ------------------------------------------------------------------
        # Safety net – if we *had* unprocessed user messages but the runnable
        # failed to generate **any** new assistant/tool message we treat this
        # as an error.
        # ------------------------------------------------------------------

        if unprocessed_rows and not created_rows:
            error_msg = "Fiche produced no messages despite pending user input."
            logger.error(f"[Runner] {error_msg}", extra={"tag": "AGENT"})
            raise RuntimeError(error_msg)

        logger.info(f"[Runner] run_thread completed successfully for thread {thread.id}", extra={"tag": "AGENT"})
        return created_rows

    # ------------------------------------------------------------------
    # Continuation API (LangGraph-free path for commis resume)
    # ------------------------------------------------------------------

    async def run_continuation(
        self,
        db: Session,
        thread: ThreadModel,
        tool_call_id: str,
        tool_result: str,
        *,
        run_id: int | None = None,
        trace_id: str | None = None,
    ) -> Sequence[ThreadMessageModel]:
        """Continue oikos execution after commis completion.

        This method is called when a commis completes and the oikos needs to
        resume. Unlike run_thread(), this does NOT use LangGraph checkpointing.

        Args:
            db: Database session.
            thread: Thread to continue.
            tool_call_id: The tool_call_id from the spawn_commis call.
            tool_result: The commis's result to inject as ToolMessage.
            run_id: Oikos run ID for event correlation.
            trace_id: End-to-end trace ID for debugging.

        Returns:
            List of new message rows created during continuation.

        Raises:
            FicheInterrupted: If spawn_commis is called again (sequential commiss).
        """
        from langchain_core.messages import ToolMessage

        from zerg.connectors.context import reset_credential_resolver
        from zerg.connectors.context import set_credential_resolver
        from zerg.connectors.resolver import CredentialResolver
        from zerg.services.oikos_react_engine import run_oikos_loop
        from zerg.tools.unified_access import get_tool_resolver

        logger.info(
            f"[Runner] Starting run_continuation for thread {thread.id}, tool_call_id={tool_call_id}",
            extra={"tag": "AGENT"},
        )

        # Load conversation history from DB
        db_messages = self.thread_service.get_thread_messages_as_langchain(db, thread.id)
        conversation_msgs = [msg for msg in db_messages if not (hasattr(msg, "type") and msg.type == "system")]

        # Check if ToolMessage for this tool_call_id already exists (idempotency)
        existing_tool_response = any(isinstance(m, ToolMessage) and m.tool_call_id == tool_call_id for m in db_messages)

        if existing_tool_response:
            logger.info(f"[Runner] ToolMessage for tool_call_id={tool_call_id} already exists, skipping creation")
            # Reload conversation with existing tool message
            tool_msg = next(m for m in db_messages if isinstance(m, ToolMessage) and m.tool_call_id == tool_call_id)
        else:
            # Create ToolMessage for the commis result
            tool_msg = ToolMessage(
                content=f"Commis completed:\n\n{tool_result}",
                tool_call_id=tool_call_id,
                name="spawn_commis",
            )

            # Find the parent assistant message that issued this tool_call
            # so the UI can correctly group tool outputs after page refresh
            from zerg.models.thread import ThreadMessage as ThreadMessageModel

            parent_id = None
            parent_msg = (
                db.query(ThreadMessageModel)
                .filter(
                    ThreadMessageModel.thread_id == thread.id,
                    ThreadMessageModel.role == "assistant",
                    ThreadMessageModel.tool_calls.isnot(None),
                )
                .order_by(ThreadMessageModel.sent_at.desc())
                .all()
            )
            for msg in parent_msg:
                if msg.tool_calls:
                    for tc in msg.tool_calls:
                        if tc.get("id") == tool_call_id:
                            parent_id = msg.id
                            break
                if parent_id:
                    break

            # Persist ToolMessage with parent_id for UI grouping
            self.thread_service.save_new_messages(
                db,
                thread_id=thread.id,
                messages=[tool_msg],
                processed=True,
                parent_id=parent_id,
            )
            logger.debug(f"[Runner] Persisted ToolMessage for tool_call_id={tool_call_id} (parent_id={parent_id})")

        # Build fresh system prompt
        agent_row = crud.get_fiche(db, self.agent.id)
        if not agent_row or not agent_row.system_instructions:
            raise RuntimeError(f"Fiche {self.agent.id} has no system_instructions")

        protocols = get_connector_protocols()
        system_content = f"{protocols}\n\n{agent_row.system_instructions}"

        skill_integration, skill_max = self._build_skill_integration(db, agent_row)
        if skill_integration:
            try:
                skills_prompt = skill_integration.get_prompt(include_content=False, max_skills=skill_max)
                if skills_prompt:
                    system_content = f"{system_content}\n\n{skills_prompt}"
                    logger.debug("[Runner] Injected skills prompt for agent %s (continuation)", self.agent.id)
            except Exception as e:
                logger.warning("[Runner] Failed to inject skills prompt (continuation): %s", e, exc_info=True)

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
            context_text = build_fiche_context(
                db=db,
                owner_id=self.agent.owner_id,
                fiche_id=self.agent.id,
                allowed_tools=getattr(agent_row, "allowed_tools", None),
                compact_json=True,
            )
            context_system_msg = SystemMessage(content=f"[INTERNAL CONTEXT - Do not mention unless asked]\n{context_text}")
            # Insert after main system message
            full_messages = [full_messages[0], context_system_msg] + full_messages[1:]
        except Exception as e:
            logger.warning(
                "[Runner] Failed to inject connector context in continuation: %s",
                e,
                exc_info=True,
            )

        # Inject memory recall context (use latest user message)
        memory_query = _latest_user_query(conversation_msgs=conversation_msgs)
        memory_context = _build_memory_context(
            db,
            owner_id=self.agent.owner_id,
            query=memory_query,
        )
        if memory_context:
            memory_msg = SystemMessage(content=memory_context)
            insert_at = 1
            if len(full_messages) > 1 and getattr(full_messages[1], "type", None) == "system":
                insert_at = 2
            full_messages = full_messages[:insert_at] + [memory_msg] + full_messages[insert_at:]
            logger.debug("[Runner] Injected memory context for continuation thread %s", thread.id)

        messages_with_context = len(full_messages)

        # Set up credential resolver context
        credential_resolver = CredentialResolver(
            fiche_id=self.agent.id,
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
            if skill_integration:
                tool_map = {tool.name: tool for tool in tools}
                try:
                    skill_tools = skill_integration.get_tools(tool_map)
                    if skill_tools:
                        tools = tools + skill_tools
                except Exception as e:
                    logger.warning("[Runner] Failed to build skill tools (batch continuation): %s", e, exc_info=True)
            if skill_integration:
                tool_map = {tool.name: tool for tool in tools}
                try:
                    skill_tools = skill_integration.get_tools(tool_map)
                    if skill_tools:
                        tools = tools + skill_tools
                except Exception as e:
                    logger.warning("[Runner] Failed to build skill tools (continuation): %s", e, exc_info=True)

            # Reset usage tracking
            from zerg.services.oikos_react_engine import reset_llm_usage

            reset_llm_usage()

            # Run the oikos loop using the new engine
            result = await run_oikos_loop(
                messages=full_messages,
                fiche_row=self.agent,
                tools=tools,
                run_id=run_id,
                owner_id=self.agent.owner_id,
                trace_id=trace_id,
                enable_token_stream=self.enable_token_stream,
            )

            # Capture usage
            self.usage_prompt_tokens = result.usage.get("prompt_tokens")
            self.usage_completion_tokens = result.usage.get("completion_tokens")
            self.usage_total_tokens = result.usage.get("total_tokens")
            self.usage_reasoning_tokens = result.usage.get("reasoning_tokens")

            # Handle interrupt (oikos spawned another commis)
            if result.interrupted:
                logger.info(
                    f"[Runner] Continuation interrupted: {result.interrupt_value}",
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
                raise FicheInterrupted(result.interrupt_value or {})

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
            logger.debug(f"[Runner] Extracted {len(new_messages)} new messages from continuation (excluding system)")

            # Persist new messages (excluding the ToolMessage we already saved)
            created_rows = self.thread_service.save_new_messages(
                db,
                thread_id=thread.id,
                messages=new_messages,
                processed=True,
            )
            logger.info(
                f"[Runner] Saved {len(created_rows)} message rows from continuation",
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

    async def run_batch_continuation(
        self,
        db: Session,
        thread: ThreadModel,
        commis_results: list[dict],
        *,
        run_id: int | None = None,
        trace_id: str | None = None,
    ) -> Sequence[ThreadMessageModel]:
        """Continue oikos execution after ALL commiss complete (barrier pattern).

        This method is called when all commiss in a barrier complete and the oikos
        needs to resume with ALL results at once. Creates ToolMessages for each commis
        result and continues the ReAct loop.

        Args:
            db: Database session.
            thread: Thread to continue.
            commis_results: List of dicts with tool_call_id, result, error, status.
            run_id: Oikos run ID for event correlation.
            trace_id: End-to-end trace ID for debugging.

        Returns:
            List of new message rows created during continuation.

        Raises:
            FicheInterrupted: If spawn_commis is called again (new batch of commiss).
        """
        from langchain_core.messages import ToolMessage

        from zerg.connectors.context import reset_credential_resolver
        from zerg.connectors.context import set_credential_resolver
        from zerg.connectors.resolver import CredentialResolver
        from zerg.services.oikos_react_engine import run_oikos_loop
        from zerg.tools.unified_access import get_tool_resolver

        logger.info(
            f"[Runner] Starting run_batch_continuation for thread {thread.id}, {len(commis_results)} commiss",
            extra={"tag": "AGENT"},
        )

        # Load conversation history from DB
        db_messages = self.thread_service.get_thread_messages_as_langchain(db, thread.id)
        conversation_msgs = [msg for msg in db_messages if not (hasattr(msg, "type") and msg.type == "system")]

        # Find the parent assistant message for tool response grouping
        from zerg.models.thread import ThreadMessage as ThreadMessageModel

        parent_id = None
        parent_msg = (
            db.query(ThreadMessageModel)
            .filter(
                ThreadMessageModel.thread_id == thread.id,
                ThreadMessageModel.role == "assistant",
                ThreadMessageModel.tool_calls.isnot(None),
            )
            .order_by(ThreadMessageModel.sent_at.desc())
            .first()
        )
        if parent_msg:
            parent_id = parent_msg.id

        # Create ToolMessages for ALL commis results
        tool_messages = []
        for wr in commis_results:
            tool_call_id = wr.get("tool_call_id")
            result = wr.get("result") or ""
            error = wr.get("error")
            status = wr.get("status", "completed")

            # Check if ToolMessage for this tool_call_id already exists (idempotency)
            existing = any(isinstance(m, ToolMessage) and m.tool_call_id == tool_call_id for m in db_messages)

            if existing:
                logger.debug(f"[Runner] ToolMessage for tool_call_id={tool_call_id} already exists")
                # Find and reuse existing
                tool_msg = next(m for m in db_messages if isinstance(m, ToolMessage) and m.tool_call_id == tool_call_id)
            else:
                # Create ToolMessage with error context if failed
                if error or status == "failed":
                    content = f"Commis failed:\n\nError: {error}\n\nPartial result: {result}"
                else:
                    content = f"Commis completed:\n\n{result}"

                tool_msg = ToolMessage(
                    content=content,
                    tool_call_id=tool_call_id,
                    name="spawn_commis",
                )

                # Persist ToolMessage with parent_id for UI grouping
                self.thread_service.save_new_messages(
                    db,
                    thread_id=thread.id,
                    messages=[tool_msg],
                    processed=True,
                    parent_id=parent_id,
                )
                logger.debug(f"[Runner] Persisted ToolMessage for tool_call_id={tool_call_id}")

            tool_messages.append(tool_msg)

        # Build fresh system prompt
        agent_row = crud.get_fiche(db, self.agent.id)
        if not agent_row or not agent_row.system_instructions:
            raise RuntimeError(f"Fiche {self.agent.id} has no system_instructions")

        protocols = get_connector_protocols()
        system_content = f"{protocols}\n\n{agent_row.system_instructions}"

        skill_integration, skill_max = self._build_skill_integration(db, agent_row)
        if skill_integration:
            try:
                skills_prompt = skill_integration.get_prompt(include_content=False, max_skills=skill_max)
                if skills_prompt:
                    system_content = f"{system_content}\n\n{skills_prompt}"
                    logger.debug("[Runner] Injected skills prompt for agent %s (batch continuation)", self.agent.id)
            except Exception as e:
                logger.warning("[Runner] Failed to inject skills prompt (batch continuation): %s", e, exc_info=True)

        system_msg = SystemMessage(content=system_content)

        # Build full message list for continuation
        # Reload conversation to include newly persisted tool messages
        db_messages = self.thread_service.get_thread_messages_as_langchain(db, thread.id)
        conversation_msgs = [msg for msg in db_messages if not (hasattr(msg, "type") and msg.type == "system")]
        full_messages = [system_msg] + conversation_msgs

        # Inject connector status context
        try:
            context_text = build_fiche_context(
                db=db,
                owner_id=self.agent.owner_id,
                fiche_id=self.agent.id,
                allowed_tools=getattr(agent_row, "allowed_tools", None),
                compact_json=True,
            )
            context_system_msg = SystemMessage(content=f"[INTERNAL CONTEXT - Do not mention unless asked]\n{context_text}")
            full_messages = [full_messages[0], context_system_msg] + full_messages[1:]
        except Exception as e:
            logger.warning(
                "[Runner] Failed to inject connector context in batch continuation: %s",
                e,
                exc_info=True,
            )

        # Inject memory recall context (use latest user message)
        memory_query = _latest_user_query(conversation_msgs=conversation_msgs)
        memory_context = _build_memory_context(
            db,
            owner_id=self.agent.owner_id,
            query=memory_query,
        )
        if memory_context:
            memory_msg = SystemMessage(content=memory_context)
            insert_at = 1
            if len(full_messages) > 1 and getattr(full_messages[1], "type", None) == "system":
                insert_at = 2
            full_messages = full_messages[:insert_at] + [memory_msg] + full_messages[insert_at:]
            logger.debug("[Runner] Injected memory context for batch continuation thread %s", thread.id)

        messages_with_context = len(full_messages)

        # Set up credential resolver context
        credential_resolver = CredentialResolver(
            fiche_id=self.agent.id,
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
            from zerg.services.oikos_react_engine import reset_llm_usage

            reset_llm_usage()

            # Run the oikos loop using the new engine
            result = await run_oikos_loop(
                messages=full_messages,
                fiche_row=self.agent,
                tools=tools,
                run_id=run_id,
                owner_id=self.agent.owner_id,
                trace_id=trace_id,
                enable_token_stream=self.enable_token_stream,
            )

            # Capture usage
            self.usage_prompt_tokens = result.usage.get("prompt_tokens")
            self.usage_completion_tokens = result.usage.get("completion_tokens")
            self.usage_total_tokens = result.usage.get("total_tokens")
            self.usage_reasoning_tokens = result.usage.get("reasoning_tokens")

            # Handle interrupt (oikos spawned more commiss)
            if result.interrupted:
                logger.info(
                    f"[Runner] Batch continuation interrupted: {result.interrupt_value}",
                    extra={"tag": "AGENT"},
                )
                # Persist any new messages before raising
                if len(result.messages) > messages_with_context:
                    new_messages = result.messages[messages_with_context:]
                    new_messages = [m for m in new_messages if not (hasattr(m, "type") and m.type == "system")]
                    if new_messages:
                        self.thread_service.save_new_messages(
                            db,
                            thread_id=thread.id,
                            messages=new_messages,
                            processed=True,
                        )
                raise FicheInterrupted(result.interrupt_value or {})

            # Normal completion: extract new messages
            if len(result.messages) <= messages_with_context:
                logger.warning(
                    "No new messages generated during batch continuation for thread %s",
                    thread.id,
                    extra={"tag": "AGENT"},
                )
                return []

            new_messages = result.messages[messages_with_context:]

            # Filter out SystemMessages
            new_messages = [m for m in new_messages if not (hasattr(m, "type") and m.type == "system")]
            logger.debug(f"[Runner] Extracted {len(new_messages)} new messages from batch continuation")

            # Persist new messages
            created_rows = self.thread_service.save_new_messages(
                db,
                thread_id=thread.id,
                messages=new_messages,
                processed=True,
            )
            logger.info(
                f"[Runner] Saved {len(created_rows)} message rows from batch continuation",
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


FicheRunner = Runner
