"""Task management tools for agents.

These tools allow agents to create and manage tasks for their users.
Tasks provide a lightweight way for agents to track to-do items without
requiring external integrations.
"""

import logging
from datetime import datetime
from typing import Any, Dict

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from sqlalchemy import desc

from zerg.context import get_worker_context
from zerg.connectors.context import get_credential_resolver
from zerg.database import db_session
from zerg.models.models import UserTask
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


class TaskCreateInput(BaseModel):
    """Input schema for task_create."""
    title: str = Field(description="Task title/summary")
    notes: str | None = Field(default=None, description="Additional task notes or description")
    due_at: str | None = Field(default=None, description="Optional due date in ISO8601 format (e.g., '2025-12-31T23:59:59Z')")


def task_create(
    title: str,
    notes: str | None = None,
    due_at: str | None = None,
) -> Dict[str, Any]:
    """Create a new task for the user.

    Use this when:
    - User asks you to remember something
    - You want to track a follow-up action
    - Breaking down a larger request into steps

    Args:
        title: Short task title/summary
        notes: Optional detailed notes
        due_at: Optional due date in ISO8601 format

    Returns:
        Dictionary with task details or error

    Example:
        >>> task_create(
        ...     title="Review Q4 budget proposal",
        ...     notes="Focus on marketing spend variance",
        ...     due_at="2025-12-31T17:00:00Z"
        ... )
        {"ok": True, "data": {"id": 1, "title": "Review Q4 budget proposal", ...}}
    """
    try:
        # Get user context
        user_id = _get_user_id()
        if not user_id:
            return tool_error(
                error_type=ErrorType.EXECUTION_ERROR,
                user_message="No user context available. This tool can only be used within an agent execution.",
            )

        # Validate title
        if not title or not title.strip():
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message="Task title cannot be empty",
            )

        # Parse due date if provided
        due_datetime = _parse_iso8601(due_at)
        if due_at and not due_datetime:
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message=f"Invalid due date format: {due_at}. Use ISO8601 format (e.g., '2025-12-31T23:59:59Z')",
            )

        # Create task
        with db_session() as db:
            task = UserTask(
                user_id=user_id,
                title=title.strip(),
                notes=notes.strip() if notes else None,
                status="pending",
                due_at=due_datetime,
            )
            db.add(task)
            db.flush()  # Get the ID

            task_data = {
                "id": task.id,
                "title": task.title,
                "notes": task.notes,
                "status": task.status,
                "due_at": task.due_at.isoformat() if task.due_at else None,
                "created_at": task.created_at.isoformat() if task.created_at else None,
            }

        logger.info(f"Created task {task.id} for user {user_id}: {title}")
        return tool_success(task_data)

    except Exception as e:
        logger.exception("Error creating task")
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message=f"Failed to create task: {str(e)}",
        )


class TaskListInput(BaseModel):
    """Input schema for task_list."""
    status: str | None = Field(default=None, description="Filter by status: 'pending', 'done', or 'cancelled'. Omit to see all tasks.")
    limit: int = Field(default=50, description="Maximum number of tasks to return (default: 50)")
    offset: int = Field(default=0, description="Number of tasks to skip for pagination (default: 0)")


def task_list(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    """List user's tasks with optional filtering.

    Use this to:
    - Show user their current tasks
    - Check what's pending before creating duplicates
    - Review completed tasks

    Args:
        status: Filter by status ('pending', 'done', 'cancelled'), or None for all
        limit: Maximum number of tasks to return (default: 50)
        offset: Number of tasks to skip for pagination (default: 0)

    Returns:
        Dictionary with list of tasks or error

    Example:
        >>> task_list(status="pending", limit=10)
        {"ok": True, "data": {"tasks": [...], "total": 5, "limit": 10, "offset": 0}}
    """
    try:
        # Get user context
        user_id = _get_user_id()
        if not user_id:
            return tool_error(
                error_type=ErrorType.EXECUTION_ERROR,
                user_message="No user context available. This tool can only be used within an agent execution.",
            )

        # Validate status
        if status and status not in ["pending", "done", "cancelled"]:
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message=f"Invalid status: {status}. Must be 'pending', 'done', or 'cancelled'",
            )

        # Validate pagination params
        if limit < 1 or limit > 1000:
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message=f"Invalid limit: {limit}. Must be between 1 and 1000",
            )

        if offset < 0:
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message=f"Invalid offset: {offset}. Must be non-negative",
            )

        # Query tasks
        with db_session() as db:
            query = db.query(UserTask).filter(UserTask.user_id == user_id)

            # Apply status filter
            if status:
                query = query.filter(UserTask.status == status)

            # Get total count
            total = query.count()

            # Apply pagination and ordering (newest first)
            tasks_query = query.order_by(desc(UserTask.created_at)).offset(offset).limit(limit)
            tasks = tasks_query.all()

            # Serialize tasks
            tasks_data = []
            for task in tasks:
                tasks_data.append({
                    "id": task.id,
                    "title": task.title,
                    "notes": task.notes,
                    "status": task.status,
                    "due_at": task.due_at.isoformat() if task.due_at else None,
                    "created_at": task.created_at.isoformat() if task.created_at else None,
                    "updated_at": task.updated_at.isoformat() if task.updated_at else None,
                })

        logger.info(f"Listed {len(tasks_data)} tasks for user {user_id} (status={status}, total={total})")
        return tool_success({
            "tasks": tasks_data,
            "total": total,
            "limit": limit,
            "offset": offset,
        })

    except Exception as e:
        logger.exception("Error listing tasks")
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message=f"Failed to list tasks: {str(e)}",
        )


class TaskUpdateInput(BaseModel):
    """Input schema for task_update."""
    task_id: int = Field(description="ID of the task to update")
    title: str | None = Field(default=None, description="New task title")
    notes: str | None = Field(default=None, description="New task notes")
    status: str | None = Field(default=None, description="New status: 'pending', 'done', or 'cancelled'")
    due_at: str | None = Field(default=None, description="New due date in ISO8601 format (use empty string '' to clear)")


def task_update(
    task_id: int,
    title: str | None = None,
    notes: str | None = None,
    status: str | None = None,
    due_at: str | None = None,
) -> Dict[str, Any]:
    """Update an existing task.

    Use this to:
    - Mark tasks as done/cancelled
    - Update task details
    - Change due dates

    Args:
        task_id: ID of the task to update
        title: New title (optional)
        notes: New notes (optional)
        status: New status: 'pending', 'done', or 'cancelled' (optional)
        due_at: New due date in ISO8601 format, or empty string to clear (optional)

    Returns:
        Dictionary with updated task details or error

    Example:
        >>> task_update(task_id=1, status="done")
        {"ok": True, "data": {"id": 1, "status": "done", ...}}
    """
    try:
        # Get user context
        user_id = _get_user_id()
        if not user_id:
            return tool_error(
                error_type=ErrorType.EXECUTION_ERROR,
                user_message="No user context available. This tool can only be used within an agent execution.",
            )

        # Validate at least one field is being updated
        if not any([title, notes, status, due_at is not None]):
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message="At least one field must be provided to update",
            )

        # Validate status if provided
        if status and status not in ["pending", "done", "cancelled"]:
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message=f"Invalid status: {status}. Must be 'pending', 'done', or 'cancelled'",
            )

        # Parse due date if provided (empty string means clear it)
        due_datetime = None
        clear_due_date = False
        if due_at is not None:
            if due_at == "":
                clear_due_date = True
            else:
                due_datetime = _parse_iso8601(due_at)
                if not due_datetime:
                    return tool_error(
                        error_type=ErrorType.VALIDATION_ERROR,
                        user_message=f"Invalid due date format: {due_at}. Use ISO8601 format or empty string to clear",
                    )

        # Update task
        with db_session() as db:
            task = db.query(UserTask).filter(
                UserTask.id == task_id,
                UserTask.user_id == user_id  # Security: ensure user owns this task
            ).first()

            if not task:
                return tool_error(
                    error_type=ErrorType.VALIDATION_ERROR,
                    user_message=f"Task {task_id} not found or you don't have permission to access it",
                )

            # Apply updates
            if title:
                task.title = title.strip()
            if notes:
                task.notes = notes.strip()
            if status:
                task.status = status
            if clear_due_date:
                task.due_at = None
            elif due_datetime:
                task.due_at = due_datetime

            db.flush()

            task_data = {
                "id": task.id,
                "title": task.title,
                "notes": task.notes,
                "status": task.status,
                "due_at": task.due_at.isoformat() if task.due_at else None,
                "created_at": task.created_at.isoformat() if task.created_at else None,
                "updated_at": task.updated_at.isoformat() if task.updated_at else None,
            }

        logger.info(f"Updated task {task_id} for user {user_id}")
        return tool_success(task_data)

    except Exception as e:
        logger.exception("Error updating task")
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message=f"Failed to update task: {str(e)}",
        )


class TaskDeleteInput(BaseModel):
    """Input schema for task_delete."""
    task_id: int = Field(description="ID of the task to delete")


def task_delete(task_id: int) -> Dict[str, Any]:
    """Delete a task permanently.

    Use this to:
    - Remove tasks that are no longer needed
    - Clean up cancelled or obsolete tasks

    Args:
        task_id: ID of the task to delete

    Returns:
        Dictionary confirming deletion or error

    Example:
        >>> task_delete(task_id=1)
        {"ok": True, "data": {"deleted": True, "task_id": 1}}
    """
    try:
        # Get user context
        user_id = _get_user_id()
        if not user_id:
            return tool_error(
                error_type=ErrorType.EXECUTION_ERROR,
                user_message="No user context available. This tool can only be used within an agent execution.",
            )

        # Delete task
        with db_session() as db:
            task = db.query(UserTask).filter(
                UserTask.id == task_id,
                UserTask.user_id == user_id  # Security: ensure user owns this task
            ).first()

            if not task:
                return tool_error(
                    error_type=ErrorType.VALIDATION_ERROR,
                    user_message=f"Task {task_id} not found or you don't have permission to delete it",
                )

            db.delete(task)

        logger.info(f"Deleted task {task_id} for user {user_id}")
        return tool_success({
            "deleted": True,
            "task_id": task_id,
        })

    except Exception as e:
        logger.exception("Error deleting task")
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message=f"Failed to delete task: {str(e)}",
        )


# Export tools
TOOLS = [
    StructuredTool.from_function(
        func=task_create,
        name="task_create",
        description=(
            "Create a new task for the user. Use this to track to-do items, "
            "follow-ups, or reminders. Tasks can have optional notes and due dates."
        ),
        args_schema=TaskCreateInput,
    ),
    StructuredTool.from_function(
        func=task_list,
        name="task_list",
        description=(
            "List user's tasks with optional filtering by status. "
            "Use this to check existing tasks before creating duplicates "
            "or to show the user their task list."
        ),
        args_schema=TaskListInput,
    ),
    StructuredTool.from_function(
        func=task_update,
        name="task_update",
        description=(
            "Update an existing task. Can change title, notes, status "
            "(pending/done/cancelled), or due date. Useful for marking "
            "tasks complete or modifying details."
        ),
        args_schema=TaskUpdateInput,
    ),
    StructuredTool.from_function(
        func=task_delete,
        name="task_delete",
        description=(
            "Permanently delete a task. Use this to remove tasks that are "
            "no longer needed. Consider marking as 'cancelled' instead if "
            "you want to keep a record."
        ),
        args_schema=TaskDeleteInput,
    ),
]
