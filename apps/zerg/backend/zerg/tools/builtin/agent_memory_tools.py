"""Agent memory tools for persistent key-value storage.

These tools allow agents to store and retrieve persistent data across
conversations. Memory is scoped per-user and supports tagging for
organization and retrieval.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from sqlalchemy import or_

from zerg.context import get_worker_context
from zerg.connectors.context import get_credential_resolver
from zerg.database import db_session
from zerg.models.models import AgentMemoryKV
from zerg.tools.error_envelope import ErrorType, tool_error, tool_success

logger = logging.getLogger(__name__)


def _get_user_id() -> int | None:
    """Get user_id from context.

    Try multiple sources:
    1. Worker context (for background workers)
    2. Credential resolver (for agent execution)

    Returns:
        User ID if found, None otherwise
    """
    # Try worker context first
    worker_ctx = get_worker_context()
    if worker_ctx and worker_ctx.owner_id:
        return worker_ctx.owner_id

    # Try credential resolver
    resolver = get_credential_resolver()
    if resolver and resolver.owner_id:
        return resolver.owner_id

    return None


def _parse_iso8601(date_str: str | None) -> datetime | None:
    """Parse ISO8601 date string to datetime.

    Args:
        date_str: ISO8601 formatted date string (e.g., "2025-12-31T23:59:59Z")

    Returns:
        datetime object or None if invalid/empty
    """
    if not date_str:
        return None

    try:
        # Try with timezone
        if date_str.endswith('Z'):
            return datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        return datetime.fromisoformat(date_str)
    except (ValueError, AttributeError) as e:
        logger.warning(f"Failed to parse date string '{date_str}': {e}")
        return None


class MemorySetInput(BaseModel):
    """Input schema for agent_memory_set."""
    key: str = Field(description="Unique key to store the value under")
    value: Any = Field(description="Value to store (can be any JSON-serializable data: dict, list, string, number, etc.)")
    tags: List[str] | None = Field(default=None, description="Optional tags for organizing and filtering memories")
    expires_at: str | None = Field(default=None, description="Optional expiration date in ISO8601 format (e.g., '2025-12-31T23:59:59Z')")


def agent_memory_set(
    key: str,
    value: Any,
    tags: List[str] | None = None,
    expires_at: str | None = None,
) -> Dict[str, Any]:
    """Store a key-value pair in persistent memory.

    Use this to:
    - Remember user preferences
    - Store context across conversations
    - Cache computed results
    - Track state over time

    Args:
        key: Unique identifier for this memory
        value: Any JSON-serializable data (dict, list, string, number, etc.)
        tags: Optional list of tags for filtering
        expires_at: Optional expiration date in ISO8601 format

    Returns:
        Dictionary with stored data or error

    Example:
        >>> agent_memory_set(
        ...     key="user_preferences",
        ...     value={"theme": "dark", "language": "en"},
        ...     tags=["settings", "ui"]
        ... )
        {"ok": True, "data": {"key": "user_preferences", "value": {...}, "tags": [...]}}
    """
    try:
        # Get user context
        user_id = _get_user_id()
        if not user_id:
            return tool_error(
                error_type=ErrorType.EXECUTION_ERROR,
                user_message="No user context available. This tool can only be used within an agent execution.",
            )

        # Validate key
        if not key or not key.strip():
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message="Key cannot be empty",
            )

        # Parse expiration date if provided
        expires_datetime = _parse_iso8601(expires_at)
        if expires_at and not expires_datetime:
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message=f"Invalid expiration date format: {expires_at}. Use ISO8601 format (e.g., '2025-12-31T23:59:59Z')",
            )

        # Ensure tags is a list
        tag_list = tags or []

        # Store or update the memory
        with db_session() as db:
            # Check if entry exists
            entry = db.query(AgentMemoryKV).filter(
                AgentMemoryKV.user_id == user_id,
                AgentMemoryKV.key == key.strip()
            ).first()

            if entry:
                # Update existing entry
                entry.value = value
                entry.tags = tag_list
                entry.expires_at = expires_datetime
            else:
                # Create new entry
                entry = AgentMemoryKV(
                    user_id=user_id,
                    key=key.strip(),
                    value=value,
                    tags=tag_list,
                    expires_at=expires_datetime,
                )
                db.add(entry)

            db.flush()

            result = {
                "key": entry.key,
                "value": entry.value,
                "tags": entry.tags,
                "expires_at": entry.expires_at.isoformat() if entry.expires_at else None,
                "created_at": entry.created_at.isoformat() if entry.created_at else None,
                "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
            }

        logger.info(f"Set memory key '{key}' for user {user_id}")
        return tool_success(result)

    except Exception as e:
        logger.exception("Error setting memory")
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message=f"Failed to set memory: {str(e)}",
        )


class MemoryGetInput(BaseModel):
    """Input schema for agent_memory_get."""
    key: str | None = Field(default=None, description="Specific key to retrieve. If provided, returns that entry.")
    tags: List[str] | None = Field(default=None, description="Filter by tags. Returns all entries matching ANY of these tags.")
    limit: int = Field(default=100, description="Maximum number of entries to return (default: 100)")


def agent_memory_get(
    key: str | None = None,
    tags: List[str] | None = None,
    limit: int = 100,
) -> Dict[str, Any]:
    """Retrieve memory entries by key or tags.

    Use this to:
    - Fetch a specific value by key
    - Search memories by tags
    - List all stored memories

    Behavior:
    - If `key` is provided: Return that specific entry (or null if not found)
    - If `tags` is provided: Return all entries matching ANY of the tags
    - If neither: Return all entries for the user (with limit)

    Args:
        key: Specific key to retrieve
        tags: List of tags to filter by (OR logic)
        limit: Maximum number of entries to return

    Returns:
        Dictionary with memory data or error

    Example:
        >>> agent_memory_get(key="user_preferences")
        {"ok": True, "data": {"key": "user_preferences", "value": {...}}}

        >>> agent_memory_get(tags=["settings", "ui"], limit=10)
        {"ok": True, "data": {"entries": [...], "count": 2}}
    """
    try:
        # Get user context
        user_id = _get_user_id()
        if not user_id:
            return tool_error(
                error_type=ErrorType.EXECUTION_ERROR,
                user_message="No user context available. This tool can only be used within an agent execution.",
            )

        # Validate limit
        if limit < 1 or limit > 1000:
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message=f"Invalid limit: {limit}. Must be between 1 and 1000",
            )

        with db_session() as db:
            # Case 1: Fetch specific key
            if key:
                entry = db.query(AgentMemoryKV).filter(
                    AgentMemoryKV.user_id == user_id,
                    AgentMemoryKV.key == key.strip()
                ).first()

                if not entry:
                    return tool_success({"key": key, "value": None, "found": False})

                result = {
                    "key": entry.key,
                    "value": entry.value,
                    "tags": entry.tags,
                    "expires_at": entry.expires_at.isoformat() if entry.expires_at else None,
                    "created_at": entry.created_at.isoformat() if entry.created_at else None,
                    "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
                    "found": True,
                }
                logger.info(f"Retrieved memory key '{key}' for user {user_id}")
                return tool_success(result)

            # Case 2: Filter by tags
            query = db.query(AgentMemoryKV).filter(AgentMemoryKV.user_id == user_id)

            if tags:
                # Match entries that have ANY of the provided tags
                # For JSON array columns, we need to check if any tag is in the array
                # This is database-specific, so we'll fetch all and filter in Python for SQLite compatibility
                all_entries = query.all()
                entries = []
                for entry in all_entries:
                    entry_tags = entry.tags or []
                    if any(tag in entry_tags for tag in tags):
                        entries.append(entry)
                entries = entries[:limit]
            else:
                # No filters, get all entries
                entries = query.limit(limit).all()

            # Serialize entries
            entries_data = []
            for entry in entries:
                entries_data.append({
                    "key": entry.key,
                    "value": entry.value,
                    "tags": entry.tags,
                    "expires_at": entry.expires_at.isoformat() if entry.expires_at else None,
                    "created_at": entry.created_at.isoformat() if entry.created_at else None,
                    "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
                })

        logger.info(f"Retrieved {len(entries_data)} memory entries for user {user_id} (tags={tags})")
        return tool_success({
            "entries": entries_data,
            "count": len(entries_data),
            "limit": limit,
        })

    except Exception as e:
        logger.exception("Error retrieving memory")
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message=f"Failed to retrieve memory: {str(e)}",
        )


class MemoryDeleteInput(BaseModel):
    """Input schema for agent_memory_delete."""
    key: str | None = Field(default=None, description="Specific key to delete")
    tags: List[str] | None = Field(default=None, description="Delete all entries with ANY of these tags")


def agent_memory_delete(
    key: str | None = None,
    tags: List[str] | None = None,
) -> Dict[str, Any]:
    """Delete memory entries by key or tags.

    Use this to:
    - Remove a specific memory
    - Clear all memories with certain tags
    - Clean up expired or obsolete data

    Args:
        key: Specific key to delete
        tags: Delete all entries matching ANY of these tags

    Returns:
        Dictionary confirming deletion or error

    Note:
        Must provide at least one of `key` or `tags`.

    Example:
        >>> agent_memory_delete(key="temp_data")
        {"ok": True, "data": {"deleted_count": 1}}

        >>> agent_memory_delete(tags=["temporary", "cache"])
        {"ok": True, "data": {"deleted_count": 5}}
    """
    try:
        # Get user context
        user_id = _get_user_id()
        if not user_id:
            return tool_error(
                error_type=ErrorType.EXECUTION_ERROR,
                user_message="No user context available. This tool can only be used within an agent execution.",
            )

        # Validate at least one parameter is provided
        if not key and not tags:
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message="Must provide at least one of 'key' or 'tags' to delete",
            )

        with db_session() as db:
            # Case 1: Delete specific key
            if key:
                result = db.query(AgentMemoryKV).filter(
                    AgentMemoryKV.user_id == user_id,
                    AgentMemoryKV.key == key.strip()
                ).delete()

                logger.info(f"Deleted memory key '{key}' for user {user_id} (count={result})")
                return tool_success({"deleted_count": result, "key": key})

            # Case 2: Delete by tags
            if tags:
                # For SQLite compatibility, fetch entries and filter in Python
                all_entries = db.query(AgentMemoryKV).filter(AgentMemoryKV.user_id == user_id).all()
                deleted_count = 0
                for entry in all_entries:
                    entry_tags = entry.tags or []
                    if any(tag in entry_tags for tag in tags):
                        db.delete(entry)
                        deleted_count += 1

                logger.info(f"Deleted {deleted_count} memory entries by tags {tags} for user {user_id}")
                return tool_success({"deleted_count": deleted_count, "tags": tags})

    except Exception as e:
        logger.exception("Error deleting memory")
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message=f"Failed to delete memory: {str(e)}",
        )


class MemoryExportInput(BaseModel):
    """Input schema for agent_memory_export."""
    pass


def agent_memory_export() -> Dict[str, Any]:
    """Export all memory entries for the user.

    Use this to:
    - Get a complete dump of stored memories
    - Backup user data
    - Debug memory contents

    Returns:
        Dictionary with all memory entries or error

    Note:
        Limited to 1000 entries to prevent memory blowout.

    Example:
        >>> agent_memory_export()
        {"ok": True, "data": {"entries": [...], "count": 42, "truncated": False}}
    """
    try:
        # Get user context
        user_id = _get_user_id()
        if not user_id:
            return tool_error(
                error_type=ErrorType.EXECUTION_ERROR,
                user_message="No user context available. This tool can only be used within an agent execution.",
            )

        # Fetch all entries with a safety limit
        MAX_ENTRIES = 1000
        with db_session() as db:
            query = db.query(AgentMemoryKV).filter(AgentMemoryKV.user_id == user_id)
            total_count = query.count()
            entries = query.limit(MAX_ENTRIES).all()

            # Serialize entries
            entries_data = []
            for entry in entries:
                entries_data.append({
                    "key": entry.key,
                    "value": entry.value,
                    "tags": entry.tags,
                    "expires_at": entry.expires_at.isoformat() if entry.expires_at else None,
                    "created_at": entry.created_at.isoformat() if entry.created_at else None,
                    "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
                })

        truncated = total_count > MAX_ENTRIES
        logger.info(f"Exported {len(entries_data)} memory entries for user {user_id} (total={total_count}, truncated={truncated})")

        return tool_success({
            "entries": entries_data,
            "count": len(entries_data),
            "total_count": total_count,
            "truncated": truncated,
        })

    except Exception as e:
        logger.exception("Error exporting memory")
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message=f"Failed to export memory: {str(e)}",
        )


# Export tools
TOOLS = [
    StructuredTool.from_function(
        func=agent_memory_set,
        name="agent_memory_set",
        description=(
            "Store a key-value pair in persistent memory that survives across conversations. "
            "Use this to remember user preferences, cache data, or track state over time. "
            "Values can be any JSON-serializable data (dict, list, string, number, etc.). "
            "Optionally tag entries for easier retrieval and organization."
        ),
        args_schema=MemorySetInput,
    ),
    StructuredTool.from_function(
        func=agent_memory_get,
        name="agent_memory_get",
        description=(
            "Retrieve memory entries by key or tags. "
            "If 'key' is provided, returns that specific entry. "
            "If 'tags' is provided, returns all entries matching ANY of those tags. "
            "If neither is provided, returns all entries for the user (with limit)."
        ),
        args_schema=MemoryGetInput,
    ),
    StructuredTool.from_function(
        func=agent_memory_delete,
        name="agent_memory_delete",
        description=(
            "Delete memory entries by key or tags. "
            "Use this to clean up temporary data or remove obsolete memories. "
            "Must provide at least one of 'key' or 'tags'."
        ),
        args_schema=MemoryDeleteInput,
    ),
    StructuredTool.from_function(
        func=agent_memory_export,
        name="agent_memory_export",
        description=(
            "Export all memory entries for the user. "
            "Returns a complete dump of stored memories (limited to 1000 entries). "
            "Useful for debugging or backing up user data."
        ),
        args_schema=MemoryExportInput,
    ),
]
