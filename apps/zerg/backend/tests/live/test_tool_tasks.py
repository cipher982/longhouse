import time
import pytest

def test_task_management_lifecycle(supervisor_client):
    """Test creating and listing a user task."""

    timestamp = int(time.time())
    task_name = f"Live Test Task {timestamp}"

    # 1. Create Task
    run_id = supervisor_client.dispatch(
        f"Create a new task on my list called '{task_name}'."
    )
    supervisor_client.wait_for_completion(run_id)

    # 2. List Tasks to Verify
    run_id = supervisor_client.dispatch(
        "List my current tasks and tell me if you see the one we just created."
    )
    result = supervisor_client.wait_for_completion(run_id)

    assert task_name in result, f"Expected task '{task_name}' to be in list, got: '{result}'"
