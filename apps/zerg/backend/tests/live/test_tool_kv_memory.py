import time


def test_kv_memory_lifecycle(concierge_client):
    """Test creating, reading, and verifying a KV memory entry."""

    timestamp = int(time.time())
    key = f"live_test_{timestamp}"
    value = f"secret_value_{timestamp}"

    # 1. Store
    course_id = concierge_client.dispatch(f"Use your memory tool to save the value '{value}' under the key '{key}'.")
    result = concierge_client.wait_for_completion(course_id)
    # Ideally we check result for confirmation, but retrieving it is the real test.

    # 2. Retrieve
    course_id = concierge_client.dispatch(
        f"What is the value stored in your memory under '{key}'? Answer with just the value."
    )
    result = concierge_client.wait_for_completion(course_id)

    assert value in result, f"Expected '{value}' to be retrieved, but got: '{result}'"
