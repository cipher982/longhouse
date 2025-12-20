"""Tests for task management tools."""

import pytest

from zerg.connectors.context import set_credential_resolver
from zerg.connectors.resolver import CredentialResolver
from zerg.models.models import UserTask
from zerg.tools.builtin.task_tools import task_create
from zerg.tools.builtin.task_tools import task_delete
from zerg.tools.builtin.task_tools import task_list
from zerg.tools.builtin.task_tools import task_update


@pytest.fixture
def credential_context(db_session, test_user):
    """Set up credential resolver context for tools."""
    resolver = CredentialResolver(agent_id=1, db=db_session, owner_id=test_user.id)
    token = set_credential_resolver(resolver)
    yield resolver
    set_credential_resolver(None)


def test_task_create_success(credential_context, db_session):
    """Test creating a new task."""
    result = task_create(
        title="Test task",
        notes="This is a test note",
        due_at="2025-12-31T23:59:59Z",
    )

    # Verify result format
    assert result["ok"] is True
    assert "data" in result
    data = result["data"]
    assert data["title"] == "Test task"
    assert data["notes"] == "This is a test note"
    assert data["status"] == "pending"
    assert data["due_at"] == "2025-12-31T23:59:59+00:00"
    assert "id" in data
    assert "created_at" in data

    # Verify task exists in database
    task = db_session.query(UserTask).filter(UserTask.id == data["id"]).first()
    assert task is not None
    assert task.title == "Test task"
    assert task.user_id == credential_context.owner_id


def test_task_create_minimal(credential_context, db_session):
    """Test creating task with only required fields."""
    result = task_create(title="Minimal task")

    assert result["ok"] is True
    data = result["data"]
    assert data["title"] == "Minimal task"
    assert data["notes"] is None
    assert data["due_at"] is None
    assert data["status"] == "pending"


def test_task_create_empty_title(credential_context):
    """Test that empty title is rejected."""
    result = task_create(title="")

    assert result["ok"] is False
    assert result["error_type"] == "validation_error"
    assert "empty" in result["user_message"].lower()


def test_task_create_invalid_due_date(credential_context):
    """Test that invalid due date is rejected."""
    result = task_create(title="Test task", due_at="not-a-date")

    assert result["ok"] is False
    assert result["error_type"] == "validation_error"
    assert "Invalid due date" in result["user_message"]


def test_task_create_no_context():
    """Test that task_create fails without credential context."""
    result = task_create(title="Test task")

    assert result["ok"] is False
    assert result["error_type"] == "execution_error"
    assert "No user context" in result["user_message"]


def test_task_list_empty(credential_context):
    """Test listing tasks when none exist."""
    result = task_list()

    assert result["ok"] is True
    data = result["data"]
    assert data["tasks"] == []
    assert data["total"] == 0
    assert data["limit"] == 50
    assert data["offset"] == 0


def test_task_list_with_tasks(credential_context, db_session):
    """Test listing tasks after creating some."""
    # Create multiple tasks
    task_create(title="Task 1", notes="First task")
    task_create(title="Task 2", notes="Second task")
    task_create(title="Task 3", notes="Third task")

    result = task_list()

    assert result["ok"] is True
    data = result["data"]
    assert data["total"] == 3
    assert len(data["tasks"]) == 3

    # Verify tasks are ordered by created_at DESC (newest first)
    titles = [task["title"] for task in data["tasks"]]
    assert "Task 3" in titles
    assert "Task 2" in titles
    assert "Task 1" in titles


def test_task_list_status_filter(credential_context):
    """Test filtering tasks by status."""
    # Create tasks with different statuses
    task_create(title="Pending task 1")
    task_create(title="Pending task 2")

    # Create and mark one as done
    done_result = task_create(title="Done task")
    task_update(task_id=done_result["data"]["id"], status="done")

    # List pending tasks
    pending_result = task_list(status="pending")
    assert pending_result["ok"] is True
    assert pending_result["data"]["total"] == 2

    # List done tasks
    done_result_list = task_list(status="done")
    assert done_result_list["ok"] is True
    assert done_result_list["data"]["total"] == 1
    assert done_result_list["data"]["tasks"][0]["title"] == "Done task"


def test_task_list_invalid_status(credential_context):
    """Test that invalid status is rejected."""
    result = task_list(status="invalid-status")

    assert result["ok"] is False
    assert result["error_type"] == "validation_error"
    assert "Invalid status" in result["user_message"]


def test_task_list_pagination(credential_context):
    """Test pagination of task list."""
    # Create 5 tasks
    for i in range(5):
        task_create(title=f"Task {i}")

    # Get first 2 tasks
    result1 = task_list(limit=2, offset=0)
    assert result1["ok"] is True
    assert len(result1["data"]["tasks"]) == 2
    assert result1["data"]["total"] == 5

    # Get next 2 tasks
    result2 = task_list(limit=2, offset=2)
    assert result2["ok"] is True
    assert len(result2["data"]["tasks"]) == 2
    assert result2["data"]["total"] == 5


def test_task_list_no_context():
    """Test that task_list fails without credential context."""
    result = task_list()

    assert result["ok"] is False
    assert result["error_type"] == "execution_error"


def test_task_update_status(credential_context):
    """Test updating task status."""
    # Create a task
    create_result = task_create(title="Task to update")
    task_id = create_result["data"]["id"]

    # Mark as done
    result = task_update(task_id=task_id, status="done")

    assert result["ok"] is True
    data = result["data"]
    assert data["id"] == task_id
    assert data["status"] == "done"
    assert data["title"] == "Task to update"


def test_task_update_title_and_notes(credential_context):
    """Test updating task title and notes."""
    # Create a task
    create_result = task_create(title="Original title")
    task_id = create_result["data"]["id"]

    # Update title and notes
    result = task_update(
        task_id=task_id,
        title="New title",
        notes="Updated notes",
    )

    assert result["ok"] is True
    data = result["data"]
    assert data["title"] == "New title"
    assert data["notes"] == "Updated notes"


def test_task_update_due_date(credential_context):
    """Test updating task due date."""
    # Create a task
    create_result = task_create(title="Task with due date")
    task_id = create_result["data"]["id"]

    # Set due date
    result = task_update(task_id=task_id, due_at="2025-12-31T23:59:59Z")

    assert result["ok"] is True
    assert result["data"]["due_at"] == "2025-12-31T23:59:59+00:00"


def test_task_update_clear_due_date(credential_context):
    """Test clearing task due date."""
    # Create a task with due date
    create_result = task_create(title="Task", due_at="2025-12-31T23:59:59Z")
    task_id = create_result["data"]["id"]

    # Clear due date with empty string
    result = task_update(task_id=task_id, due_at="")

    assert result["ok"] is True
    assert result["data"]["due_at"] is None


def test_task_update_not_found(credential_context):
    """Test updating non-existent task."""
    result = task_update(task_id=99999, title="New title")

    assert result["ok"] is False
    assert result["error_type"] == "validation_error"
    assert "not found" in result["user_message"].lower()


def test_task_update_no_fields(credential_context):
    """Test that update requires at least one field."""
    create_result = task_create(title="Task")
    task_id = create_result["data"]["id"]

    result = task_update(task_id=task_id)

    assert result["ok"] is False
    assert result["error_type"] == "validation_error"
    assert "at least one field" in result["user_message"].lower()


def test_task_update_invalid_status(credential_context):
    """Test that invalid status is rejected."""
    create_result = task_create(title="Task")
    task_id = create_result["data"]["id"]

    result = task_update(task_id=task_id, status="invalid-status")

    assert result["ok"] is False
    assert result["error_type"] == "validation_error"
    assert "Invalid status" in result["user_message"]


def test_task_update_no_context():
    """Test that task_update fails without credential context."""
    result = task_update(task_id=1, title="New title")

    assert result["ok"] is False
    assert result["error_type"] == "execution_error"


def test_task_delete_success(credential_context, db_session):
    """Test deleting a task."""
    # Create a task
    create_result = task_create(title="Task to delete")
    task_id = create_result["data"]["id"]

    # Delete the task
    result = task_delete(task_id=task_id)

    assert result["ok"] is True
    data = result["data"]
    assert data["deleted"] is True
    assert data["task_id"] == task_id

    # Verify task is gone from database
    task = db_session.query(UserTask).filter(UserTask.id == task_id).first()
    assert task is None


def test_task_delete_not_found(credential_context):
    """Test deleting non-existent task."""
    result = task_delete(task_id=99999)

    assert result["ok"] is False
    assert result["error_type"] == "validation_error"
    assert "not found" in result["user_message"].lower()


def test_task_delete_no_context():
    """Test that task_delete fails without credential context."""
    result = task_delete(task_id=1)

    assert result["ok"] is False
    assert result["error_type"] == "execution_error"


def test_user_isolation(credential_context, db_session, test_user):
    """Test that users can only access their own tasks."""
    from zerg.crud import crud

    # Create task as User A
    create_result = task_create(title="User A task")
    task_id = create_result["data"]["id"]

    # Verify User A can see the task
    list_result = task_list()
    assert list_result["ok"] is True
    assert len(list_result["data"]["tasks"]) == 1

    # Create User B
    user_b = crud.create_user(db=db_session, email="userb@test.com")

    # Switch to User B context
    resolver_b = CredentialResolver(agent_id=2, db=db_session, owner_id=user_b.id)
    set_credential_resolver(resolver_b)

    # Verify User B cannot see User A's task
    list_result_b = task_list()
    assert list_result_b["ok"] is True
    assert len(list_result_b["data"]["tasks"]) == 0

    # Verify User B cannot update User A's task
    update_result = task_update(task_id=task_id, title="Hacked")
    assert update_result["ok"] is False
    assert "not found" in update_result["user_message"].lower()

    # Verify User B cannot delete User A's task
    delete_result = task_delete(task_id=task_id)
    assert delete_result["ok"] is False
    assert "not found" in delete_result["user_message"].lower()

    # Create task as User B
    create_result_b = task_create(title="User B task")
    task_id_b = create_result_b["data"]["id"]

    # Verify User B can see their own task
    list_result_b2 = task_list()
    assert list_result_b2["ok"] is True
    assert len(list_result_b2["data"]["tasks"]) == 1
    assert list_result_b2["data"]["tasks"][0]["title"] == "User B task"

    # Switch back to User A
    set_credential_resolver(credential_context)

    # Verify User A still has their task
    list_result_a = task_list()
    assert list_result_a["ok"] is True
    assert len(list_result_a["data"]["tasks"]) == 1
    assert list_result_a["data"]["tasks"][0]["title"] == "User A task"

    # Verify User A cannot update User B's task
    update_result_a = task_update(task_id=task_id_b, title="Hacked")
    assert update_result_a["ok"] is False


def test_complete_workflow(credential_context):
    """Test complete task management workflow."""
    # Create a task
    create_result = task_create(
        title="Complete project documentation",
        notes="Include architecture diagrams",
        due_at="2025-12-31T23:59:59Z",
    )
    assert create_result["ok"] is True
    task_id = create_result["data"]["id"]

    # List tasks and verify it exists
    list_result = task_list(status="pending")
    assert list_result["ok"] is True
    assert list_result["data"]["total"] == 1
    assert list_result["data"]["tasks"][0]["title"] == "Complete project documentation"

    # Update the task
    update_result = task_update(
        task_id=task_id,
        notes="Added section about database schema",
    )
    assert update_result["ok"] is True
    assert "database schema" in update_result["data"]["notes"]

    # Mark as done
    done_result = task_update(task_id=task_id, status="done")
    assert done_result["ok"] is True
    assert done_result["data"]["status"] == "done"

    # List pending tasks (should be empty)
    pending_result = task_list(status="pending")
    assert pending_result["data"]["total"] == 0

    # List done tasks (should have 1)
    done_list = task_list(status="done")
    assert done_list["data"]["total"] == 1

    # Delete the task
    delete_result = task_delete(task_id=task_id)
    assert delete_result["ok"] is True

    # Verify it's gone
    final_list = task_list()
    assert final_list["data"]["total"] == 0
