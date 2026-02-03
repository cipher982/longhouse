"""PromptContext - Unified prompt construction for FicheRunner flows.

This module centralizes prompt construction logic to ensure consistent behavior
across run_thread(), run_continuation(), and run_batch_continuation().

Key responsibilities:
1. PromptContext dataclass: Holds all components of a prompt
2. build_prompt(): Unified helper for all three flows
3. derive_memory_query(): Consistent memory query extraction
4. get_or_create_tool_message(): DB-level idempotency for tool results
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any
from typing import List
from typing import Optional
from typing import Sequence

from zerg.types.messages import BaseMessage
from zerg.types.messages import HumanMessage
from zerg.types.messages import ToolMessage

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from zerg.managers.fiche_runner import RuntimeView
    from zerg.models.models import Fiche as FicheModel
    from zerg.skills.integration import SkillIntegration

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PromptContext dataclass
# ---------------------------------------------------------------------------


@dataclass
class PromptContext:
    """Holds all components of a prompt for LLM invocation.

    This dataclass provides a structured representation of prompt components,
    making it easier to audit, test, and debug prompt construction.

    Attributes:
        system_prompt: The system message content (protocols + instructions + skills)
        conversation_history: List of conversation messages (human, assistant, tool)
        tool_messages: New tool messages to inject (commis results)
        dynamic_context: Tagged context blocks (connector status, memory)
        skill_integration: Optional skill integration for tool binding
        message_count_with_context: Total message count for slicing new messages
    """

    system_prompt: str
    conversation_history: List[BaseMessage] = field(default_factory=list)
    tool_messages: List[ToolMessage] = field(default_factory=list)
    dynamic_context: List[DynamicContextBlock] = field(default_factory=list)
    skill_integration: Optional[SkillIntegration] = None
    message_count_with_context: int = 0


@dataclass(frozen=True)
class DynamicContextBlock:
    """A tagged block of dynamic context for clearer auditing.

    Tags help identify the source of dynamic context when debugging:
    - "CONNECTOR_STATUS": Connection state for integrations
    - "MEMORY": Episodic memory and knowledge base hits
    - "USER_CONTEXT": User-specific context

    Attributes:
        tag: Identifier for the context type
        content: The actual context content
    """

    tag: str
    content: str


# ---------------------------------------------------------------------------
# Memory query derivation
# ---------------------------------------------------------------------------


def derive_memory_query(
    *,
    unprocessed_rows: Optional[Sequence[Any]] = None,
    conversation_msgs: Optional[Sequence[BaseMessage]] = None,
) -> Optional[str]:
    """Derive the memory search query from available sources.

    This function provides consistent memory query extraction across all flows.
    Priority order:
    1. Latest non-internal user message from unprocessed_rows
    2. Latest HumanMessage from conversation_msgs

    Args:
        unprocessed_rows: Unprocessed message rows (from run_thread)
        conversation_msgs: Conversation messages (from continuations)

    Returns:
        The query string to use for memory search, or None if no query found
    """
    import re

    # Strip timestamp prefix pattern: [2024-01-15T10:30:00Z]
    timestamp_re = re.compile(r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\]\s*")

    # Priority 1: unprocessed_rows (run_thread flow)
    if unprocessed_rows:
        for row in reversed(unprocessed_rows):
            if row.role == "user" and not getattr(row, "internal", False):
                content = (row.content or "").strip()
                if content:
                    return content

    # Priority 2: conversation_msgs (continuation flows)
    if conversation_msgs:
        for msg in reversed(conversation_msgs):
            if isinstance(msg, HumanMessage):
                content = timestamp_re.sub("", msg.content or "").strip()
                if content:
                    return content

    return None


# ---------------------------------------------------------------------------
# Tool message idempotency
# ---------------------------------------------------------------------------


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

    This function ensures tool results are never duplicated in the database,
    providing consistent behavior for retries and concurrent executions.

    Args:
        db: Database session
        thread_id: Thread ID for the message
        tool_call_id: The tool_call_id from spawn_commis
        result: The commis result content
        error: Optional error message if commis failed
        status: Status string (completed, failed)
        parent_id: Parent assistant message ID for UI grouping

    Returns:
        Tuple of (ToolMessage, created) where created is True if new message was created
    """
    from zerg.models.thread import ThreadMessage as ThreadMessageModel
    from zerg.services.thread_service import ThreadService

    # Check if ToolMessage for this tool_call_id already exists
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
        logger.debug(f"[PromptContext] ToolMessage for tool_call_id={tool_call_id} already exists (id={existing.id})")
        tool_msg = ToolMessage(
            content=existing.content,
            tool_call_id=tool_call_id,
            name=existing.name or "spawn_commis",
        )
        return tool_msg, False

    # Create content based on status
    if error or status == "failed":
        content = f"Commis failed:\n\nError: {error}\n\nPartial result: {result}"
    else:
        content = f"Commis completed:\n\n{result}"

    tool_msg = ToolMessage(
        content=content,
        tool_call_id=tool_call_id,
        name="spawn_commis",
    )

    # Persist with parent_id for UI grouping
    ThreadService.save_new_messages(
        db,
        thread_id=thread_id,
        messages=[tool_msg],
        processed=True,
        parent_id=parent_id,
    )

    logger.debug(f"[PromptContext] Created ToolMessage for tool_call_id={tool_call_id} (parent_id={parent_id})")
    return tool_msg, True


def find_parent_assistant_id(
    db: Session,
    *,
    thread_id: int,
    tool_call_ids: Sequence[str],
    fallback_to_latest: bool = True,
) -> Optional[int]:
    """Find the parent assistant message ID that issued the given tool_call_ids.

    Args:
        db: Database session
        thread_id: Thread ID to search in
        tool_call_ids: Tool call IDs to match
        fallback_to_latest: If True and no matching tool_call_id found, return the
            most recent assistant message with tool_calls (matches old fiche_runner behavior)

    Returns:
        The parent message ID or None if not found
    """
    from zerg.models.thread import ThreadMessage as ThreadMessageModel

    # Query for assistant messages with tool_calls in reverse chronological order
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

    # First, try to find exact match by tool_call_id
    tool_call_set = set(tool_call_ids)
    for msg in parent_msgs:
        if msg.tool_calls:
            for tc in msg.tool_calls:
                if tc.get("id") in tool_call_set:
                    return msg.id

    # Fallback: return most recent assistant with tool_calls (matches old batch behavior)
    if fallback_to_latest and parent_msgs:
        return parent_msgs[0].id

    return None


# ---------------------------------------------------------------------------
# Unified prompt builder
# ---------------------------------------------------------------------------


def build_prompt(
    db: Session,
    agent: RuntimeView,
    fiche: FicheModel,
    *,
    thread_id: Optional[int] = None,
    conversation_msgs: Optional[List[BaseMessage]] = None,
    tool_messages: Optional[List[ToolMessage]] = None,
    unprocessed_rows: Optional[Sequence[Any]] = None,
    allowed_tools: Any = None,
    include_skills: bool = True,
    thread_service: Any = None,
) -> PromptContext:
    """Build a unified prompt for any FicheRunner flow.

    This function provides a single entry point for prompt construction,
    ensuring consistent behavior across run_thread(), run_continuation(),
    and run_batch_continuation().

    Args:
        db: Database session
        agent: RuntimeView with agent configuration
        fiche: Fiche ORM row with system_instructions
        thread_id: Thread ID for loading conversation (run_thread flow)
        conversation_msgs: Pre-loaded conversation messages (continuation flows)
        tool_messages: New tool messages to inject (requires conversation_msgs)
        unprocessed_rows: Unprocessed message rows for query extraction
        allowed_tools: Agent's allowed tools for connector filtering
        include_skills: Whether to include skills prompt
        thread_service: Thread service for loading messages

    Returns:
        PromptContext with all prompt components

    Raises:
        ValueError: If neither thread_id nor conversation_msgs provided
        ValueError: If tool_messages provided without conversation_msgs (would be silently dropped)
    """
    from zerg.managers.message_array_builder import MessageArrayBuilder

    if thread_id is None and conversation_msgs is None:
        raise ValueError("Must provide either thread_id or conversation_msgs")

    # HIGH: Prevent silent loss of tool_messages when using thread_id path
    # Tool messages must be merged with conversation_msgs, not thread_id loading
    if tool_messages and conversation_msgs is None:
        raise ValueError(
            "tool_messages requires conversation_msgs to be provided. "
            "When using thread_id, load db_messages first and pass as conversation_msgs."
        )

    # Initialize builder
    builder = MessageArrayBuilder(db, agent)
    builder.with_system_prompt(fiche, include_skills=include_skills)

    # Add conversation - either from thread_id or pre-loaded messages
    if thread_id is not None and conversation_msgs is None:
        builder.with_conversation(thread_id, thread_service=thread_service)
    elif conversation_msgs is not None:
        # For continuations, append tool messages to conversation
        full_conversation = list(conversation_msgs)
        if tool_messages:
            full_conversation.extend(tool_messages)
        builder.with_conversation_messages(full_conversation, filter_system=True)

    # Add dynamic context with consistent memory query derivation
    builder.with_dynamic_context(
        allowed_tools=allowed_tools,
        unprocessed_rows=unprocessed_rows,
        conversation_msgs=conversation_msgs,
    )

    # Build result
    result = builder.build()

    # Extract dynamic context blocks for auditing
    dynamic_blocks = _extract_dynamic_blocks(result.messages)

    # Create PromptContext with structured data
    context = PromptContext(
        system_prompt=_extract_system_prompt(result.messages),
        conversation_history=_extract_conversation(result.messages),
        tool_messages=list(tool_messages or []),
        dynamic_context=dynamic_blocks,
        skill_integration=result.skill_integration,
        message_count_with_context=result.message_count_with_context,
    )

    return context


def _extract_system_prompt(messages: List[BaseMessage]) -> str:
    """Extract system prompt from messages."""
    from zerg.types.messages import SystemMessage

    for msg in messages:
        if isinstance(msg, SystemMessage):
            # First system message is the main prompt
            content = msg.content or ""
            if "[INTERNAL CONTEXT" not in content and "[MEMORY CONTEXT]" not in content:
                return content
    return ""


def _extract_conversation(messages: List[BaseMessage]) -> List[BaseMessage]:
    """Extract conversation messages (excluding system and dynamic context)."""
    from zerg.types.messages import SystemMessage

    result = []
    for msg in messages:
        if isinstance(msg, SystemMessage):
            content = msg.content or ""
            # Skip system prompts and dynamic context
            if "[INTERNAL CONTEXT" in content or "[MEMORY CONTEXT]" in content:
                continue
            continue  # Skip all system messages
        result.append(msg)
    return result


def _extract_dynamic_blocks(messages: List[BaseMessage]) -> List[DynamicContextBlock]:
    """Extract tagged dynamic context blocks from messages.

    Note: MessageArrayBuilder combines connector and memory context into a single
    SystemMessage. We extract it as a single DYNAMIC block to avoid duplication
    when using context_to_messages().
    """
    from zerg.types.messages import SystemMessage

    blocks = []
    for msg in messages:
        if isinstance(msg, SystemMessage):
            content = msg.content or ""
            # Check for dynamic context markers (connector status and/or memory)
            has_connector = "[INTERNAL CONTEXT" in content
            has_memory = "[MEMORY CONTEXT]" in content

            if has_connector or has_memory:
                # Store as single DYNAMIC block to avoid duplication in context_to_messages()
                # The builder already combines these, so we preserve the combined form
                blocks.append(DynamicContextBlock(tag="DYNAMIC", content=content))
    return blocks


# ---------------------------------------------------------------------------
# Helper to convert PromptContext to message array for LLM
# ---------------------------------------------------------------------------


def context_to_messages(context: PromptContext) -> List[BaseMessage]:
    """Convert PromptContext to a message array for LLM invocation.

    This is a convenience function for cases where you need the raw
    message array instead of the structured PromptContext.

    Args:
        context: PromptContext to convert

    Returns:
        List of messages suitable for LLM invocation
    """
    from zerg.types.messages import SystemMessage

    messages: List[BaseMessage] = []

    # System prompt
    if context.system_prompt:
        messages.append(SystemMessage(content=context.system_prompt))

    # Conversation history (includes tool messages from continuations)
    messages.extend(context.conversation_history)

    # Dynamic context at the end
    if context.dynamic_context:
        combined = "\n\n".join(block.content for block in context.dynamic_context)
        messages.append(SystemMessage(content=combined))

    return messages
