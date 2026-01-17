"""Memory file tools for long-term agent memory."""

from __future__ import annotations

import logging
from typing import Any
from typing import Dict
from typing import List

from langchain_core.tools import StructuredTool
from pydantic import BaseModel
from pydantic import Field

from zerg.connectors.context import get_credential_resolver
from zerg.context import get_worker_context
from zerg.crud import memory_crud
from zerg.database import db_session
from zerg.services import memory_embeddings
from zerg.services import memory_search as memory_search_service
from zerg.tools.error_envelope import ErrorType
from zerg.tools.error_envelope import tool_error
from zerg.tools.error_envelope import tool_success

logger = logging.getLogger(__name__)


def _get_owner_id() -> int | None:
    """Resolve owner_id from execution context."""
    worker_ctx = get_worker_context()
    if worker_ctx and worker_ctx.owner_id:
        return worker_ctx.owner_id

    resolver = get_credential_resolver()
    if resolver and resolver.owner_id:
        return resolver.owner_id

    return None


class MemoryWriteInput(BaseModel):
    path: str = Field(description="Memory file path (e.g., 'episodes/2026-01-17/summary.md')")
    content: str = Field(description="Full file content")
    title: str | None = Field(default=None, description="Optional title for the memory file")
    tags: List[str] | None = Field(default=None, description="Optional tags for filtering")
    metadata: Dict[str, Any] | None = Field(default=None, description="Optional metadata (run_id, thread_id, etc.)")


def memory_write(
    path: str,
    content: str,
    title: str | None = None,
    tags: List[str] | None = None,
    metadata: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Create or overwrite a memory file."""
    owner_id = _get_owner_id()
    if not owner_id:
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message="No user context available. memory_write requires authenticated execution context.",
        )

    if not path or not path.strip():
        return tool_error(
            error_type=ErrorType.VALIDATION_ERROR,
            user_message="Path cannot be empty.",
        )

    with db_session() as db:
        row = memory_crud.upsert_memory_file(
            db,
            owner_id=owner_id,
            path=path.strip(),
            title=title,
            content=content,
            tags=tags,
            metadata=metadata,
        )

        # Best-effort embedding update (skips in tests/disabled envs)
        memory_embeddings.maybe_upsert_embedding(
            db,
            owner_id=owner_id,
            memory_file_id=row.id,
            content=row.content,
        )

        result = {
            "path": row.path,
            "title": row.title,
            "content": row.content,
            "tags": row.tags or [],
            "metadata": row.file_metadata or {},
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    return tool_success(result)


class MemoryReadInput(BaseModel):
    path: str = Field(description="Memory file path to read")


def memory_read(path: str) -> Dict[str, Any]:
    """Read a memory file by path."""
    owner_id = _get_owner_id()
    if not owner_id:
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message="No user context available. memory_read requires authenticated execution context.",
        )

    if not path or not path.strip():
        return tool_error(
            error_type=ErrorType.VALIDATION_ERROR,
            user_message="Path cannot be empty.",
        )

    with db_session() as db:
        row = memory_crud.get_memory_file_by_path(db, owner_id=owner_id, path=path.strip())

    if not row:
        return tool_error(
            error_type=ErrorType.VALIDATION_ERROR,
            user_message=f"Memory file not found: {path}",
        )

    result = {
        "path": row.path,
        "title": row.title,
        "content": row.content,
        "tags": row.tags or [],
        "metadata": row.file_metadata or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    return tool_success(result)


class MemoryLsInput(BaseModel):
    prefix: str | None = Field(default=None, description="Optional path prefix to filter")
    limit: int = Field(default=100, description="Max number of files to return", ge=1, le=500)


def memory_ls(prefix: str | None = None, limit: int = 100) -> Dict[str, Any]:
    """List memory files under a prefix."""
    owner_id = _get_owner_id()
    if not owner_id:
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message="No user context available. memory_ls requires authenticated execution context.",
        )

    with db_session() as db:
        rows = memory_crud.list_memory_files(db, owner_id=owner_id, prefix=prefix, limit=limit)

    files = [
        {
            "path": row.path,
            "title": row.title,
            "tags": row.tags or [],
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        for row in rows
    ]
    return tool_success({"files": files, "count": len(files)})


class MemorySearchInput(BaseModel):
    query: str = Field(description="Search query")
    tags: List[str] | None = Field(default=None, description="Optional tag filter")
    limit: int = Field(default=5, description="Max results to return", ge=1, le=20)
    use_embeddings: bool = Field(default=True, description="Use embeddings-first search")


def memory_search(
    query: str,
    tags: List[str] | None = None,
    limit: int = 5,
    use_embeddings: bool = True,
) -> Dict[str, Any]:
    """Search memory files (embeddings-first with keyword fallback)."""
    owner_id = _get_owner_id()
    if not owner_id:
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message="No user context available. memory_search requires authenticated execution context.",
        )

    if not query or not query.strip():
        return tool_error(
            error_type=ErrorType.VALIDATION_ERROR,
            user_message="Query cannot be empty.",
        )

    with db_session() as db:
        results = memory_search_service.search_memory_files(
            db,
            owner_id=owner_id,
            query=query,
            tags=tags,
            limit=limit,
            use_embeddings=use_embeddings,
        )

    return tool_success({"results": results, "count": len(results)})


class MemoryDeleteInput(BaseModel):
    path: str = Field(description="Memory file path to delete")


def memory_delete(path: str) -> Dict[str, Any]:
    """Delete a memory file by path."""
    owner_id = _get_owner_id()
    if not owner_id:
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message="No user context available. memory_delete requires authenticated execution context.",
        )

    if not path or not path.strip():
        return tool_error(
            error_type=ErrorType.VALIDATION_ERROR,
            user_message="Path cannot be empty.",
        )

    with db_session() as db:
        deleted = memory_crud.delete_memory_file(db, owner_id=owner_id, path=path.strip())

    if not deleted:
        return tool_error(
            error_type=ErrorType.VALIDATION_ERROR,
            user_message=f"Memory file not found: {path}",
        )

    return tool_success({"deleted": True, "path": path})


memory_write_tool = StructuredTool.from_function(
    func=memory_write,
    name="memory_write",
    description="Create or overwrite a memory file (virtual filesystem entry).",
    args_schema=MemoryWriteInput,
)

memory_read_tool = StructuredTool.from_function(
    func=memory_read,
    name="memory_read",
    description="Read a memory file by path.",
    args_schema=MemoryReadInput,
)

memory_ls_tool = StructuredTool.from_function(
    func=memory_ls,
    name="memory_ls",
    description="List memory files under an optional prefix.",
    args_schema=MemoryLsInput,
)

memory_search_tool = StructuredTool.from_function(
    func=memory_search,
    name="memory_search",
    description="Search memory files using embeddings-first retrieval.",
    args_schema=MemorySearchInput,
)

memory_delete_tool = StructuredTool.from_function(
    func=memory_delete,
    name="memory_delete",
    description="Delete a memory file by path.",
    args_schema=MemoryDeleteInput,
)

TOOLS = [
    memory_write_tool,
    memory_read_tool,
    memory_ls_tool,
    memory_search_tool,
    memory_delete_tool,
]
