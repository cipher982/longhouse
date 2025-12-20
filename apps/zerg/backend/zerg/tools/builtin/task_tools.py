from typing import Optional, List, Type
from datetime import datetime
from pydantic import BaseModel, Field
from langchain_core.tools import StructuredTool
from zerg.context import get_worker_context
from zerg.models.models import UserTask
from zerg.database import db_session
from sqlalchemy import select, update, delete
from sqlalchemy.orm import Session
import logging

logger = logging.getLogger(__name__)

# --- Models ---

class TaskCreateSchema(BaseModel):
    title: str = Field(..., description="Title of the task")
    notes: Optional[str] = Field(None, description="Optional notes or details")
    due_at: Optional[datetime] = Field(None, description="Optional due date/time (ISO format)")

class TaskListSchema(BaseModel):
    status: Optional[str] = Field(None, description="Filter by status (pending, done, cancelled)")
    limit: int = Field(50, description="Max number of tasks to return")
    offset: int = Field(0, description="Pagination offset")

class TaskUpdateSchema(BaseModel):
    task_id: int = Field(..., description="ID of the task to update")
    title: Optional[str] = Field(None, description="New title")
    notes: Optional[str] = Field(None, description="New notes")
    status: Optional[str] = Field(None, description="New status (pending, done, cancelled)")
    due_at: Optional[datetime] = Field(None, description="New due date/time")

class TaskDeleteSchema(BaseModel):
    task_id: int = Field(..., description="ID of the task to delete")

# --- Implementations ---

def task_create(title: str, notes: Optional[str] = None, due_at: Optional[datetime] = None) -> dict:
    """Create a new task for the user."""
    ctx = get_worker_context()
    if not ctx:
        return {"error": "No user context found"}

    try:
        with db_session() as session:
            task = UserTask(
                user_id=ctx.user_id,
                title=title,
                notes=notes,
                due_at=due_at,
                status="pending"
            )
            session.add(task)
            session.commit()
            session.refresh(task)
            return {
                "status": "created",
                "task": {
                    "id": task.id,
                    "title": task.title,
                    "status": task.status,
                    "created_at": task.created_at.isoformat()
                }
            }
    except Exception as e:
        logger.exception("Error creating task")
        return {"error": str(e)}

def task_list(status: Optional[str] = None, limit: int = 50, offset: int = 0) -> dict:
    """List tasks for the current user."""
    ctx = get_worker_context()
    if not ctx:
        return {"error": "No user context found"}

    try:
        with db_session() as session:
            query = select(UserTask).where(UserTask.user_id == ctx.user_id)

            if status:
                query = query.where(UserTask.status == status)

            # Default sort by created_at desc
            query = query.order_by(UserTask.created_at.desc()).limit(limit).offset(offset)

            tasks = session.execute(query).scalars().all()

            return {
                "count": len(tasks),
                "tasks": [
                    {
                        "id": t.id,
                        "title": t.title,
                        "status": t.status,
                        "due_at": t.due_at.isoformat() if t.due_at else None
                    } for t in tasks
                ]
            }
    except Exception as e:
        logger.exception("Error listing tasks")
        return {"error": str(e)}

def task_update(task_id: int, title: Optional[str] = None, notes: Optional[str] = None, status: Optional[str] = None, due_at: Optional[datetime] = None) -> dict:
    """Update an existing task."""
    ctx = get_worker_context()
    if not ctx:
        return {"error": "No user context found"}

    try:
        with db_session() as session:
            # Verify ownership first
            task = session.execute(
                select(UserTask).where(UserTask.id == task_id, UserTask.user_id == ctx.user_id)
            ).scalar_one_or_none()

            if not task:
                return {"error": "Task not found"}

            # Apply updates
            if title is not None: task.title = title
            if notes is not None: task.notes = notes
            if status is not None: task.status = status
            if due_at is not None: task.due_at = due_at

            session.commit()
            return {"status": "updated", "task_id": task_id}

    except Exception as e:
        logger.exception("Error updating task")
        return {"error": str(e)}

def task_delete(task_id: int) -> dict:
    """Delete a task."""
    ctx = get_worker_context()
    if not ctx:
        return {"error": "No user context found"}

    try:
        with db_session() as session:
            result = session.execute(
                delete(UserTask).where(UserTask.id == task_id, UserTask.user_id == ctx.user_id)
            )
            session.commit()

            if result.rowcount == 0:
                return {"error": "Task not found"}

            return {"status": "deleted", "task_id": task_id}
    except Exception as e:
        logger.exception("Error deleting task")
        return {"error": str(e)}

# --- Tool Definitions ---

TOOLS = [
    StructuredTool.from_function(
        func=task_create,
        name="task_create",
        description="Create a new task on the user's list.",
        args_schema=TaskCreateSchema
    ),
    StructuredTool.from_function(
        func=task_list,
        name="task_list",
        description="List the user's tasks with optional status filter.",
        args_schema=TaskListSchema
    ),
    StructuredTool.from_function(
        func=task_update,
        name="task_update",
        description="Update a task's title, notes, status, or due date.",
        args_schema=TaskUpdateSchema
    ),
    StructuredTool.from_function(
        func=task_delete,
        name="task_delete",
        description="Permanently delete a task.",
        args_schema=TaskDeleteSchema
    ),
]
