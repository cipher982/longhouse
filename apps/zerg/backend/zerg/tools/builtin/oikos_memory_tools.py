"""Memory tools for Oikos agents.

Simple tools for saving and retrieving persistent memories.
These are designed for natural "remember X" / "what do you know about Y" usage.
"""

from __future__ import annotations

import logging
from typing import List

from langchain_core.tools import StructuredTool

from zerg.connectors.context import get_credential_resolver
from zerg.services.memory_store import get_memory_store
from zerg.services.oikos_context import get_oikos_context
from zerg.tools.error_envelope import ErrorType
from zerg.tools.error_envelope import tool_error

logger = logging.getLogger(__name__)


def _get_context() -> tuple[int | None, int | None]:
    """Get user_id and fiche_id from execution context.

    Returns:
        (user_id, fiche_id) tuple. fiche_id may be None for global scope.

    Note: Currently fiche_id is always None because OikosContext doesn't track it.
    All memories are global (user-level) for now. Fiche-scoped memories will be
    added in a future phase when we add fiche_id to OikosContext.
    """
    # Try oikos context first
    ctx = get_oikos_context()
    if ctx and ctx.owner_id:
        # Note: OikosContext doesn't have fiche_id yet - all memories are global
        return ctx.owner_id, None

    # Fall back to credential resolver
    resolver = get_credential_resolver()
    if resolver and resolver.owner_id:
        return resolver.owner_id, None

    return None, None


async def save_memory_async(
    content: str,
    type: str | None = None,
    scope: str = "global",
) -> str:
    """Save a memory for later retrieval.

    Use this to remember important information, decisions, bugs, preferences,
    or any facts that should persist across conversations.

    Args:
        content: What to remember (be specific and descriptive)
        type: Category - "note", "decision", "bug", "preference", "fact" (optional)
        scope: "global" (available to all agents) or "fiche" (only this agent)

    Returns:
        Confirmation message

    Examples:
        save_memory("User prefers dark mode", type="preference")
        save_memory("spawn_commis parallel bug: returns SUCCESS instead of WAITING", type="bug")
        save_memory("Decided to use PostgreSQL for the memory system", type="decision")
    """
    user_id, fiche_id = _get_context()
    if not user_id:
        return tool_error(
            ErrorType.MISSING_CONTEXT,
            "Cannot save memory - no user context available",
        )

    if not content or not content.strip():
        return tool_error(
            ErrorType.VALIDATION_ERROR,
            "Content cannot be empty",
        )

    # Determine scope
    memory_fiche_id = None
    if scope == "fiche" and fiche_id:
        memory_fiche_id = fiche_id

    store = get_memory_store()
    try:
        store.save(
            user_id=user_id,
            content=content.strip(),
            fiche_id=memory_fiche_id,
            type=type,
            source="oikos",
        )
        scope_label = "fiche-specific" if memory_fiche_id else "global"
        type_label = f" ({type})" if type else ""
        return f"Memory saved ({scope_label}{type_label}): {content[:100]}{'...' if len(content) > 100 else ''}"
    except Exception as e:
        logger.exception("Failed to save memory")
        return tool_error(ErrorType.EXECUTION_ERROR, f"Failed to save memory: {e}")


def save_memory(
    content: str,
    type: str | None = None,
    scope: str = "global",
) -> str:
    """Sync wrapper for save_memory_async."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(save_memory_async(content, type, scope))


async def search_memory_async(
    query: str,
    type: str | None = None,
    limit: int = 10,
) -> str:
    """Search memories for relevant information.

    Use this to recall information that was previously saved.

    Args:
        query: What to search for (keywords or phrases)
        type: Optional filter by type - "note", "decision", "bug", "preference", "fact"
        limit: Maximum results (default 10)

    Returns:
        Matching memories or message if none found

    Examples:
        search_memory("user preferences")
        search_memory("spawn_commis", type="bug")
        search_memory("authentication")
    """
    user_id, fiche_id = _get_context()
    if not user_id:
        return tool_error(
            ErrorType.MISSING_CONTEXT,
            "Cannot search memories - no user context available",
        )

    if not query or not query.strip():
        return tool_error(
            ErrorType.VALIDATION_ERROR,
            "Query cannot be empty",
        )

    store = get_memory_store()
    try:
        results = store.search(
            user_id=user_id,
            query=query.strip(),
            fiche_id=fiche_id,  # Include fiche-specific + global
            type=type,
            limit=limit,
        )

        if not results:
            return f"No memories found matching '{query}'"

        lines = [f"Found {len(results)} memory/memories:\n"]
        for i, mem in enumerate(results, 1):
            type_label = f" [{mem.type}]" if mem.type else ""
            scope_label = " (fiche)" if mem.fiche_id else ""
            date_str = mem.created_at.strftime("%Y-%m-%d") if mem.created_at else "?"
            lines.append(f"{i}. {date_str}{type_label}{scope_label}")
            # Truncate long content
            content = mem.content
            if len(content) > 200:
                content = content[:200] + "..."
            lines.append(f"   {content}\n")

        return "\n".join(lines)
    except Exception as e:
        logger.exception("Failed to search memories")
        return tool_error(ErrorType.EXECUTION_ERROR, f"Failed to search memories: {e}")


def search_memory(
    query: str,
    type: str | None = None,
    limit: int = 10,
) -> str:
    """Sync wrapper for search_memory_async."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(search_memory_async(query, type, limit))


async def list_memories_async(
    type: str | None = None,
    limit: int = 20,
) -> str:
    """List recent memories.

    Use this to review what has been remembered.

    Args:
        type: Optional filter by type - "note", "decision", "bug", "preference", "fact"
        limit: Maximum results (default 20)

    Returns:
        List of recent memories
    """
    user_id, fiche_id = _get_context()
    if not user_id:
        return tool_error(
            ErrorType.MISSING_CONTEXT,
            "Cannot list memories - no user context available",
        )

    store = get_memory_store()
    try:
        results = store.list(
            user_id=user_id,
            fiche_id=fiche_id,
            type=type,
            limit=limit,
        )

        if not results:
            type_filter = f" of type '{type}'" if type else ""
            return f"No memories found{type_filter}"

        type_filter = f" (type: {type})" if type else ""
        lines = [f"Recent memories{type_filter} ({len(results)} shown):\n"]
        for i, mem in enumerate(results, 1):
            type_label = f" [{mem.type}]" if mem.type else ""
            scope_label = " (fiche)" if mem.fiche_id else ""
            date_str = mem.created_at.strftime("%Y-%m-%d") if mem.created_at else "?"
            lines.append(f"{i}. {date_str}{type_label}{scope_label}: {mem.id[:8]}...")
            # Truncate long content
            content = mem.content
            if len(content) > 150:
                content = content[:150] + "..."
            lines.append(f"   {content}\n")

        return "\n".join(lines)
    except Exception as e:
        logger.exception("Failed to list memories")
        return tool_error(ErrorType.EXECUTION_ERROR, f"Failed to list memories: {e}")


def list_memories(
    type: str | None = None,
    limit: int = 20,
) -> str:
    """Sync wrapper for list_memories_async."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(list_memories_async(type, limit))


async def forget_memory_async(memory_id: str) -> str:
    """Delete a specific memory.

    Use this to remove outdated or incorrect memories.

    Args:
        memory_id: The memory ID (from list_memories or search_memory)

    Returns:
        Confirmation message
    """
    user_id, _ = _get_context()
    if not user_id:
        return tool_error(
            ErrorType.MISSING_CONTEXT,
            "Cannot forget memory - no user context available",
        )

    if not memory_id or not memory_id.strip():
        return tool_error(
            ErrorType.VALIDATION_ERROR,
            "Memory ID cannot be empty",
        )

    store = get_memory_store()
    try:
        # Handle partial IDs (first 8 chars)
        full_id = memory_id.strip()

        deleted = store.delete(user_id=user_id, memory_id=full_id)
        if deleted:
            return f"Memory {full_id[:8]}... deleted"
        else:
            return tool_error(ErrorType.NOT_FOUND, f"Memory {full_id[:8]}... not found")
    except ValueError as e:
        return tool_error(ErrorType.VALIDATION_ERROR, f"Invalid memory ID format: {e}")
    except Exception as e:
        logger.exception("Failed to forget memory")
        return tool_error(ErrorType.EXECUTION_ERROR, f"Failed to forget memory: {e}")


def forget_memory(memory_id: str) -> str:
    """Sync wrapper for forget_memory_async."""
    from zerg.utils.async_utils import run_async_safely

    return run_async_safely(forget_memory_async(memory_id))


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

TOOLS: List[StructuredTool] = [
    StructuredTool.from_function(
        func=save_memory,
        coroutine=save_memory_async,
        name="save_memory",
        description="Save a memory for later retrieval. "
        "Use to remember bugs, decisions, preferences, facts, or notes. "
        "Scope: 'global' (all agents see it) or 'fiche' (only this agent).",
    ),
    StructuredTool.from_function(
        func=search_memory,
        coroutine=search_memory_async,
        name="search_memory",
        description="Search memories by keyword. "
        "Returns matching memories from your saved knowledge. "
        "Optionally filter by type (note, decision, bug, preference, fact).",
    ),
    StructuredTool.from_function(
        func=list_memories,
        coroutine=list_memories_async,
        name="list_memories",
        description="List recent memories. " "Shows what has been remembered, most recent first. " "Optionally filter by type.",
    ),
    StructuredTool.from_function(
        func=forget_memory,
        coroutine=forget_memory_async,
        name="forget_memory",
        description="Delete a specific memory by its ID. " "Use to remove outdated or incorrect information.",
    ),
]

# Tool names for registration
OIKOS_MEMORY_TOOL_NAMES: frozenset[str] = frozenset(t.name for t in TOOLS)
