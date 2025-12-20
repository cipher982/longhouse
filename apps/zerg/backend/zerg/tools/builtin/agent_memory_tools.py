from typing import Optional, List, Any, Dict
from datetime import datetime
from pydantic import BaseModel, Field
from langchain.tools import StructuredTool
from zerg.services.user_context import get_worker_context
from zerg.models.models import AgentMemoryKV
from zerg.database import db_session
from sqlalchemy import select, delete
from sqlalchemy.dialects.postgresql import insert
import logging

logger = logging.getLogger(__name__)

# --- Models ---

class MemorySetSchema(BaseModel):
    key: str = Field(..., description="Unique key for this memory item")
    value: Dict[str, Any] = Field(..., description="JSON value to store")
    tags: Optional[List[str]] = Field(None, description="Optional tags for organization")
    expires_at: Optional[datetime] = Field(None, description="Optional expiration time")

class MemoryGetSchema(BaseModel):
    key: Optional[str] = Field(None, description="Specific key to retrieve")
    tags: Optional[List[str]] = Field(None, description="Filter by tags (ANY match)")
    limit: int = Field(100, description="Max results")

class MemoryDeleteSchema(BaseModel):
    key: Optional[str] = Field(None, description="Delete specific key")
    tags: Optional[List[str]] = Field(None, description="Delete all matching tags")

# --- Implementations ---

def agent_memory_set(key: str, value: Dict[str, Any], tags: Optional[List[str]] = None, expires_at: Optional[datetime] = None) -> dict:
    """Store a key-value pair in long-term memory."""
    ctx = get_worker_context()
    if not ctx:
        return {"error": "No user context found"}

    try:
        with db_session() as session:
            stmt = insert(AgentMemoryKV).values(
                user_id=ctx.user_id,
                key=key,
                value=value,
                tags=tags or [],
                expires_at=expires_at
            ).on_conflict_do_update(
                index_elements=['user_id', 'key'],
                set_={
                    "value": value,
                    "tags": tags or [],
                    "expires_at": expires_at,
                    "updated_at": datetime.now()
                }
            )
            session.execute(stmt)
            session.commit()
            return {"status": "stored", "key": key}
    except Exception as e:
        logger.exception("Error setting memory")
        return {"error": str(e)}

def agent_memory_get(key: Optional[str] = None, tags: Optional[List[str]] = None, limit: int = 100) -> dict:
    """Retrieve items from memory."""
    ctx = get_worker_context()
    if not ctx:
        return {"error": "No user context found"}

    try:
        with db_session() as session:
            query = select(AgentMemoryKV).where(AgentMemoryKV.user_id == ctx.user_id)

            if key:
                query = query.where(AgentMemoryKV.key == key)

            if tags:
                # PostgreqSQL array overlap operator &&
                query = query.where(AgentMemoryKV.tags.overlap(tags))

            query = query.limit(limit)
            results = session.execute(query).scalars().all()

            return {
                "count": len(results),
                "items": [
                    {
                        "key": r.key,
                        "value": r.value,
                        "tags": r.tags,
                        "created_at": r.created_at.isoformat()
                    } for r in results
                ]
            }
    except Exception as e:
        logger.exception("Error getting memory")
        return {"error": str(e)}

def agent_memory_delete(key: Optional[str] = None, tags: Optional[List[str]] = None) -> dict:
    """Delete items from memory."""
    ctx = get_worker_context()
    if not ctx:
        return {"error": "No user context found"}

    if not key and not tags:
        return {"error": "Must specify key or tags to delete"}

    try:
        with db_session() as session:
            query = delete(AgentMemoryKV).where(AgentMemoryKV.user_id == ctx.user_id)

            if key:
                query = query.where(AgentMemoryKV.key == key)
            elif tags:
                query = query.where(AgentMemoryKV.tags.overlap(tags))

            result = session.execute(query)
            session.commit()

            return {"status": "deleted", "count": result.rowcount}
    except Exception as e:
        logger.exception("Error deleting memory")
        return {"error": str(e)}

# --- Tool Definitions ---

TOOLS = [
    StructuredTool.from_function(
        func=agent_memory_set,
        name="agent_memory_set",
        description="Save data to persistent memory (key-value).",
        args_schema=MemorySetSchema
    ),
    StructuredTool.from_function(
        func=agent_memory_get,
        name="agent_memory_get",
        description="Retrieve data from persistent memory by key or tags.",
        args_schema=MemoryGetSchema
    ),
    StructuredTool.from_function(
        func=agent_memory_delete,
        name="agent_memory_delete",
        description="Delete data from persistent memory.",
        args_schema=MemoryDeleteSchema
    ),
]
