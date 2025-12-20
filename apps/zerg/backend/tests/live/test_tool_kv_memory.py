import time


def test_kv_memory_lifecycle(supervisor_client):
    """Test creating, reading, and verifying a KV memory entry."""

    timestamp = int(time.time())
    key = f"live_test_{timestamp}"
    value = f"secret_value_{timestamp}"

    # 1. Store
    run_id = supervisor_client.dispatch(f"Use your memory tool to save the value '{value}' under the key '{key}'.")
    result = supervisor_client.wait_for_completion(run_id)
    # Ideally we check result for confirmation, but retrieving it is the real test.

    # 2. Retrieve
    run_id = supervisor_client.dispatch(
        f"What is the value stored in your memory under '{key}'? Answer with just the value."
    )
    result = supervisor_client.wait_for_completion(run_id)

    assert value in result, f"Expected '{value}' to be retrieved, but got: '{result}'"
