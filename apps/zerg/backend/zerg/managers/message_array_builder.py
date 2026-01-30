"""MessageArrayBuilder - Cache-optimized message array construction.

This module centralizes message array construction, eliminating code duplication
across run_thread(), run_continuation(), and run_batch_continuation() in FicheRunner.

Layout: [system] -> [conversation] -> [tool_messages] -> [dynamic_context]
        ^cacheable  ^cacheable        ^per-turn          ^per-turn

Both OpenAI and Anthropic use prefix-based caching: identical prefix = cache hit.
Dynamic content placed at the end maximizes cache hits on the static prefix.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import IntEnum
from enum import auto
from typing import TYPE_CHECKING
from typing import Any
from typing import List
from typing import Optional
from typing import Sequence

from langchain_core.messages import BaseMessage
from langchain_core.messages import HumanMessage
from langchain_core.messages import SystemMessage
from langchain_core.messages import ToolMessage

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


@dataclass(frozen=True)
class MessageArrayResult:
    """Result of MessageArrayBuilder.build()."""

    messages: List[BaseMessage]
    message_count_with_context: int
    skill_integration: Optional[SkillIntegration]


class BuildPhase(IntEnum):
    """Tracks builder state to prevent wrong ordering/double calls."""

    INIT = auto()
    SYSTEM_PROMPT = auto()
    CONVERSATION = auto()
    TOOL_MESSAGES = auto()
    DYNAMIC_CONTEXT = auto()
    BUILT = auto()


class MessageArrayBuilder:
    """Cache-optimized message array construction.

    Layout: [system] -> [conversation] -> [tool_messages] -> [dynamic_context]
            ^cacheable  ^cacheable        ^per-turn          ^per-turn

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

    State tracking prevents:
    - Adding system prompt twice
    - Adding conversation before system prompt
    - Adding dynamic context before conversation
    - Building twice
    """

    def __init__(self, db: Session, agent: RuntimeView) -> None:
        """Initialize builder.

        Args:
            db: Database session (eager-load ORM objects within request scope)
            agent: RuntimeView with agent configuration
        """
        self._db = db
        self._agent = agent
        self._phase = BuildPhase.INIT
        self._messages: List[BaseMessage] = []
        self._skill_integration: Optional[SkillIntegration] = None
        self._skill_max: Optional[int] = None

    def _check_phase(self, required: BuildPhase, target: BuildPhase) -> None:
        """Check and advance builder phase."""
        if self._phase >= target:
            raise RuntimeError(f"Builder already past {target.name} phase (current: {self._phase.name})")
        if self._phase < required:
            raise RuntimeError(f"Must call {required.name} phase before {target.name}")
        self._phase = target

    def with_system_prompt(
        self,
        fiche: FicheModel,
        *,
        include_skills: bool = True,
    ) -> MessageArrayBuilder:
        """Add system prompt (protocols + instructions + skills).

        Args:
            fiche: Fiche ORM row with system_instructions
            include_skills: Whether to include skills prompt

        Returns:
            Self for chaining
        """
        self._check_phase(BuildPhase.INIT, BuildPhase.SYSTEM_PROMPT)

        if not fiche or not fiche.system_instructions:
            raise RuntimeError(f"Fiche {self._agent.id} has no system_instructions")

        from zerg.prompts.connector_protocols import get_connector_protocols

        # Build system content: protocols + instructions
        protocols = get_connector_protocols()
        system_content = f"{protocols}\n\n{fiche.system_instructions}"

        # Add skills prompt if enabled
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
        """Add conversation history from database.

        Args:
            thread_id: Thread ID to load messages from
            filter_system: Filter out stale system messages (recommended)
            thread_service: Thread service for loading messages (defaults to ThreadService)

        Returns:
            Self for chaining
        """
        self._check_phase(BuildPhase.SYSTEM_PROMPT, BuildPhase.CONVERSATION)

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
        """Add conversation history from preloaded messages.

        Args:
            conversation_msgs: Conversation messages already loaded (e.g., from DB)
            filter_system: Filter out stale system messages (recommended)

        Returns:
            Self for chaining
        """
        self._check_phase(BuildPhase.SYSTEM_PROMPT, BuildPhase.CONVERSATION)
        return self._add_conversation_messages(
            conversation_msgs,
            filter_system=filter_system,
            log_source="provided messages",
        )

    def with_tool_messages(
        self,
        tool_messages: Sequence[ToolMessage],
    ) -> MessageArrayBuilder:
        """Add tool messages (e.g., commis results).

        Args:
            tool_messages: Tool messages to append

        Returns:
            Self for chaining
        """
        # Allow skipping TOOL_MESSAGES phase if no tool messages
        if not tool_messages:
            return self

        self._check_phase(BuildPhase.CONVERSATION, BuildPhase.TOOL_MESSAGES)
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
        """Add dynamic context (connector status + memory) at end.

        Dynamic context is placed last to maximize prefix cache hits.

        Args:
            memory_query: Query string for memory/knowledge search (optional)
            allowed_tools: Agent's allowed tools for connector filtering
            unprocessed_rows: Unprocessed message rows for query extraction
            conversation_msgs: Conversation messages for query extraction (fallback)

        Returns:
            Self for chaining
        """
        # Allow calling after CONVERSATION or TOOL_MESSAGES
        if self._phase == BuildPhase.CONVERSATION:
            self._phase = BuildPhase.DYNAMIC_CONTEXT
        elif self._phase == BuildPhase.TOOL_MESSAGES:
            self._phase = BuildPhase.DYNAMIC_CONTEXT
        else:
            raise RuntimeError(f"with_dynamic_context must be called after CONVERSATION or TOOL_MESSAGES (current: {self._phase.name})")

        dynamic_context_parts: List[str] = []

        # 1. Connector status context
        try:
            from zerg.connectors.status_builder import build_fiche_context

            context_text = build_fiche_context(
                db=self._db,
                owner_id=self._agent.owner_id,
                fiche_id=self._agent.id,
                allowed_tools=allowed_tools,
                compact_json=True,
            )
            dynamic_context_parts.append(f"[INTERNAL CONTEXT - Do not mention unless asked]\n{context_text}")
            logger.debug(
                "[Builder] Built connector context for agent %s (owner_id=%s)",
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
        # Derive query from unprocessed rows or conversation if not provided
        query = memory_query
        if query is None:
            query = self._extract_user_query(unprocessed_rows, conversation_msgs)

        if query:
            memory_context = self._build_memory_context(query)
            if memory_context:
                dynamic_context_parts.append(memory_context)
                logger.debug("[Builder] Built memory context")

        # Inject dynamic context as SystemMessage at end (cache-optimized position)
        if dynamic_context_parts:
            combined_context = "\n\n".join(dynamic_context_parts)
            context_msg = SystemMessage(content=combined_context)
            self._messages.append(context_msg)
            logger.debug("[Builder] Injected dynamic context at end of message array")

        return self

    def build(self) -> MessageArrayResult:
        """Build and return the final message array.

        Returns:
            MessageArrayResult with messages, count, and skill integration
        """
        if self._phase == BuildPhase.BUILT:
            raise RuntimeError("Builder already built - create a new builder instance")
        if self._phase < BuildPhase.CONVERSATION:
            raise RuntimeError("Must at least call with_system_prompt and with_conversation before build")

        self._phase = BuildPhase.BUILT

        return MessageArrayResult(
            messages=self._messages.copy(),
            message_count_with_context=len(self._messages),
            skill_integration=self._skill_integration,
        )

    # ------------------------------------------------------------------
    # Private helpers (extracted from fiche_runner.py)
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
            # Filter out system messages - they're injected fresh above
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

    def _extract_user_query(
        self,
        unprocessed_rows: Optional[List[Any]] = None,
        conversation_msgs: Optional[List[BaseMessage]] = None,
    ) -> Optional[str]:
        """Extract latest user query for memory search."""
        if unprocessed_rows:
            for row in reversed(unprocessed_rows):
                if row.role == "user" and not getattr(row, "internal", False):
                    return (row.content or "").strip() or None

        # Fallback to provided conversation messages
        msgs_to_search = conversation_msgs if conversation_msgs is not None else self._messages

        if msgs_to_search:
            for msg in reversed(msgs_to_search):
                if isinstance(msg, HumanMessage):
                    return _strip_timestamp_prefix(msg.content)

        return None

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
