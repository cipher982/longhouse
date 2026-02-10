"""MessageBuilder - Cache-optimized message array construction.

Centralizes message array construction for FicheRunner flows (run_thread,
run_continuation, run_batch_continuation).

Layout: [system] -> [conversation] -> [connectors] -> [memory] -> [time]
        ^cacheable  ^cacheable        ^rarely changes  ^per-query  ^per-minute

Both OpenAI and Anthropic use prefix-based caching: identical prefix = cache hit.
Dynamic context is split into separate SystemMessages ordered by stability so that
only the *changed* segment busts its cache.

Also provides DB-level helpers for tool message idempotency and parent assistant
message lookup used by commis resume flows.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any
from typing import List
from typing import Optional
from typing import Sequence

from zerg.types.messages import BaseMessage
from zerg.types.messages import HumanMessage
from zerg.types.messages import SystemMessage
from zerg.types.messages import ToolMessage

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from zerg.managers.fiche_runner import RuntimeView
    from zerg.models.models import Fiche as FicheModel
    from zerg.skills.integration import SkillIntegration

logger = logging.getLogger(__name__)

_TIMESTAMP_PREFIX_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\]\s*")


def _strip_timestamp_prefix(text: str) -> str:
    """Strip timestamp prefix from message content."""
    return _TIMESTAMP_PREFIX_RE.sub("", text or "").strip()


def _truncate(text: str, max_chars: int = 220) -> str:
    """Truncate text to max_chars with ellipsis."""
    if not text:
        return ""
    clean = " ".join(text.split())
    if len(clean) <= max_chars:
        return clean
    return clean[:max_chars].rstrip() + "..."


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MessageArrayResult:
    """Result of MessageArrayBuilder.build()."""

    messages: List[BaseMessage]
    message_count_with_context: int
    skill_integration: Optional[SkillIntegration]


# ---------------------------------------------------------------------------
# MessageArrayBuilder
# ---------------------------------------------------------------------------


class MessageArrayBuilder:
    """Cache-optimized message array construction.

    Layout: [system] -> [conversation] -> [connectors] -> [memory] -> [time]
            ^cacheable  ^cacheable        ^rarely changes  ^per-query  ^per-minute

    Usage:
        result = (
            MessageArrayBuilder(db, agent)
            .with_system_prompt(agent_row)
            .with_conversation(thread_id)
            .with_tool_messages(tool_msgs)  # optional
            .with_dynamic_context(memory_query=query, allowed_tools=tools)
            .build()
        )
        messages = result.messages
    """

    def __init__(self, db: Session, agent: RuntimeView) -> None:
        self._db = db
        self._agent = agent
        self._messages: List[BaseMessage] = []
        self._skill_integration: Optional[SkillIntegration] = None
        self._skill_max: Optional[int] = None
        self._built = False

    def with_system_prompt(
        self,
        fiche: FicheModel,
        *,
        include_skills: bool = True,
    ) -> MessageArrayBuilder:
        """Add system prompt (protocols + instructions + skills)."""
        if not fiche or not fiche.system_instructions:
            raise RuntimeError(f"Fiche {self._agent.id} has no system_instructions")

        from zerg.prompts.connector_protocols import get_connector_protocols

        protocols = get_connector_protocols()
        system_content = f"{protocols}\n\n{fiche.system_instructions}"

        if include_skills:
            self._skill_integration, self._skill_max = self._build_skill_integration(fiche)
            if self._skill_integration:
                try:
                    skills_prompt = self._skill_integration.get_prompt(include_content=False, max_skills=self._skill_max)
                    if skills_prompt:
                        system_content = f"{system_content}\n\n{skills_prompt}"
                        logger.debug("[Builder] Injected skills prompt for agent %s", self._agent.id)
                except Exception as e:
                    logger.warning("[Builder] Failed to inject skills prompt: %s", e, exc_info=True)

        self._messages.append(SystemMessage(content=system_content))
        return self

    def with_conversation(
        self,
        thread_id: int,
        *,
        filter_system: bool = True,
        thread_service: Any = None,
    ) -> MessageArrayBuilder:
        """Add conversation history from database."""
        if thread_service is None:
            from zerg.services.thread_service import ThreadService

            thread_service = ThreadService

        db_messages = thread_service.get_thread_messages_as_langchain(self._db, thread_id)
        return self._add_conversation_messages(
            db_messages,
            filter_system=filter_system,
            log_source=f"thread {thread_id}",
        )

    def with_conversation_messages(
        self,
        conversation_msgs: Sequence[BaseMessage],
        *,
        filter_system: bool = True,
    ) -> MessageArrayBuilder:
        """Add conversation history from preloaded messages."""
        return self._add_conversation_messages(
            conversation_msgs,
            filter_system=filter_system,
            log_source="provided messages",
        )

    def with_tool_messages(
        self,
        tool_messages: Sequence[ToolMessage],
    ) -> MessageArrayBuilder:
        """Add tool messages (e.g., commis results)."""
        if not tool_messages:
            return self
        self._messages.extend(tool_messages)
        logger.debug("[Builder] Added %d tool messages", len(tool_messages))
        return self

    def with_dynamic_context(
        self,
        *,
        memory_query: Optional[str] = None,
        allowed_tools: Any = None,
        unprocessed_rows: Optional[List[Any]] = None,
        conversation_msgs: Optional[List[BaseMessage]] = None,
    ) -> MessageArrayBuilder:
        """Add dynamic context as separate SystemMessages for cache optimization.

        Order (most stable first, least stable last):
            1. Connector status  -- rarely changes
            2. Memory context    -- changes when query triggers different recalls
            3. Current time      -- changes every minute
        """
        # 1. Connector status context
        connector_context_msg: Optional[SystemMessage] = None
        time_context_msg: Optional[SystemMessage] = None
        try:
            from zerg.connectors.status_builder import build_fiche_context_parts

            parts = build_fiche_context_parts(
                db=self._db,
                owner_id=self._agent.owner_id,
                fiche_id=self._agent.id,
                allowed_tools=allowed_tools,
                compact_json=True,
            )
            connector_context_msg = SystemMessage(content=f"[INTERNAL CONTEXT - Do not mention unless asked]\n{parts.connector_status}")
            time_context_msg = SystemMessage(content=f"[INTERNAL CONTEXT - Do not mention unless asked]\n{parts.current_time}")
            logger.debug(
                "[Builder] Built connector + time context for agent %s (owner_id=%s)",
                self._agent.id,
                self._agent.owner_id,
            )
        except Exception as e:
            logger.warning(
                "[Builder] Failed to build connector context: %s. Agent will run without status awareness.",
                e,
                exc_info=True,
            )

        # 2. Memory recall context
        memory_context_msg: Optional[SystemMessage] = None
        query = memory_query
        if query is None:
            query = derive_memory_query(
                unprocessed_rows=unprocessed_rows,
                conversation_msgs=conversation_msgs,
            )

        if query:
            memory_context = self._build_memory_context(query)
            if memory_context:
                memory_context_msg = SystemMessage(content=memory_context)
                logger.debug("[Builder] Built memory context")

        # Inject: connectors -> memory -> time (most stable first)
        if connector_context_msg:
            self._messages.append(connector_context_msg)
        if memory_context_msg:
            self._messages.append(memory_context_msg)
        if time_context_msg:
            self._messages.append(time_context_msg)

        if connector_context_msg or memory_context_msg or time_context_msg:
            logger.debug(
                "[Builder] Injected %d dynamic context messages at end of message array",
                sum(1 for m in (connector_context_msg, memory_context_msg, time_context_msg) if m),
            )

        return self

    def build(self) -> MessageArrayResult:
        """Build and return the final message array."""
        if self._built:
            raise RuntimeError("Builder already built - create a new builder instance")
        self._built = True

        return MessageArrayResult(
            messages=self._messages.copy(),
            message_count_with_context=len(self._messages),
            skill_integration=self._skill_integration,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _add_conversation_messages(
        self,
        conversation_msgs: Sequence[BaseMessage],
        *,
        filter_system: bool,
        log_source: str,
    ) -> MessageArrayBuilder:
        """Append conversation messages with optional system filtering."""
        if filter_system:
            filtered = [msg for msg in conversation_msgs if not (hasattr(msg, "type") and msg.type == "system")]
        else:
            filtered = list(conversation_msgs)

        logger.debug(
            "[Builder] Retrieved %d conversation messages (%s, filtered=%s)",
            len(filtered),
            log_source,
            filter_system,
        )

        self._messages.extend(filtered)
        return self

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

    def _resolve_skill_settings(self, fiche: FicheModel) -> tuple[Optional[List[str]], bool, Optional[int], Optional[str], bool]:
        """Resolve skill settings from agent config and user context."""
        from zerg.crud import crud

        cfg = dict(getattr(fiche, "config", {}) or {})
        allowed = cfg.get("skills_allowlist")
        include_user = cfg.get("skills_include_user")
        max_skills = cfg.get("skills_max")
        workspace_path = cfg.get("skills_workspace_path") or cfg.get("workspace_path")
        enabled = cfg.get("skills_enabled")

        user = crud.get_user(self._db, self._agent.owner_id)
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

    def _build_skill_integration(self, fiche: FicheModel) -> tuple[Optional[SkillIntegration], Optional[int]]:
        """Build SkillIntegration with resolved settings."""
        from zerg.skills.integration import SkillIntegration

        allowed_skills, include_user, max_skills, workspace_path, enabled = self._resolve_skill_settings(fiche)
        if not enabled:
            return None, None

        integration = SkillIntegration(
            workspace_path=workspace_path,
            allowed_skills=allowed_skills,
            db=self._db,
            owner_id=self._agent.owner_id,
            include_user=include_user,
        )
        return integration, max_skills

    def _build_memory_context(
        self,
        query: str,
        memory_limit: int = 3,
        knowledge_limit: int = 3,
    ) -> Optional[str]:
        """Build memory context from episodic memory + knowledge base."""
        from zerg.config import get_settings
        from zerg.crud import knowledge_crud
        from zerg.services import memory_embeddings
        from zerg.services import memory_search as memory_search_service
        from zerg.tools.builtin.knowledge_tools import extract_snippets

        settings = get_settings()
        use_embeddings = memory_embeddings.embeddings_enabled(settings)

        try:
            memory_hits = memory_search_service.search_memory_files(
                self._db,
                owner_id=self._agent.owner_id,
                query=query,
                limit=memory_limit,
                use_embeddings=use_embeddings,
            )
        except Exception as e:
            logger.warning("Memory search failed: %s", e)
            memory_hits = []

        try:
            knowledge_hits = knowledge_crud.search_knowledge_documents(
                self._db,
                owner_id=self._agent.owner_id,
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


# ---------------------------------------------------------------------------
# Free functions (DB helpers for commis resume)
# ---------------------------------------------------------------------------


def derive_memory_query(
    *,
    unprocessed_rows: Optional[Sequence[Any]] = None,
    conversation_msgs: Optional[Sequence[BaseMessage]] = None,
) -> Optional[str]:
    """Derive the memory search query from available sources.

    Priority:
    1. Latest non-internal user message from unprocessed_rows
    2. Latest HumanMessage from conversation_msgs
    """
    if unprocessed_rows:
        for row in reversed(unprocessed_rows):
            if row.role == "user" and not getattr(row, "internal", False):
                content = (row.content or "").strip()
                if content:
                    return content

    if conversation_msgs:
        for msg in reversed(conversation_msgs):
            if isinstance(msg, HumanMessage):
                content = _strip_timestamp_prefix(msg.content)
                if content:
                    return content

    return None


def get_or_create_tool_message(
    db: Session,
    *,
    thread_id: int,
    tool_call_id: str,
    result: str,
    error: Optional[str] = None,
    status: str = "completed",
    parent_id: Optional[int] = None,
) -> tuple[ToolMessage, bool]:
    """Get or create a ToolMessage with DB-level idempotency.

    Returns:
        Tuple of (ToolMessage, created) where created is True if new message was created
    """
    from zerg.models.thread import ThreadMessage as ThreadMessageModel
    from zerg.services.thread_service import ThreadService

    existing = (
        db.query(ThreadMessageModel)
        .filter(
            ThreadMessageModel.thread_id == thread_id,
            ThreadMessageModel.role == "tool",
            ThreadMessageModel.tool_call_id == tool_call_id,
        )
        .first()
    )

    if existing:
        logger.debug(f"ToolMessage for tool_call_id={tool_call_id} already exists (id={existing.id})")
        tool_msg = ToolMessage(
            content=existing.content,
            tool_call_id=tool_call_id,
            name=existing.name or "spawn_commis",
        )
        return tool_msg, False

    if error or status == "failed":
        content = f"Commis failed:\n\nError: {error}\n\nPartial result: {result}"
    else:
        content = f"Commis completed:\n\n{result}"

    tool_msg = ToolMessage(
        content=content,
        tool_call_id=tool_call_id,
        name="spawn_commis",
    )

    ThreadService.save_new_messages(
        db,
        thread_id=thread_id,
        messages=[tool_msg],
        processed=True,
        parent_id=parent_id,
    )

    logger.debug(f"Created ToolMessage for tool_call_id={tool_call_id} (parent_id={parent_id})")
    return tool_msg, True


def find_parent_assistant_id(
    db: Session,
    *,
    thread_id: int,
    tool_call_ids: Sequence[str],
    fallback_to_latest: bool = True,
) -> Optional[int]:
    """Find the parent assistant message ID that issued the given tool_call_ids."""
    from zerg.models.thread import ThreadMessage as ThreadMessageModel

    parent_msgs = (
        db.query(ThreadMessageModel)
        .filter(
            ThreadMessageModel.thread_id == thread_id,
            ThreadMessageModel.role == "assistant",
            ThreadMessageModel.tool_calls.isnot(None),
        )
        .order_by(ThreadMessageModel.sent_at.desc())
        .all()
    )

    tool_call_set = set(tool_call_ids)
    for msg in parent_msgs:
        if msg.tool_calls:
            for tc in msg.tool_calls:
                if tc.get("id") in tool_call_set:
                    return msg.id

    if fallback_to_latest and parent_msgs:
        return parent_msgs[0].id

    return None
