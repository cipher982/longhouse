"""Live prompt quality tests - regression testing for worker efficiency.

These tests spawn actual workers and measure tool call efficiency via SSE events.
They catch regressions like "8 tool calls for a simple disk check".

Run with:
    cd apps/zerg/backend
    uv run pytest tests/live/test_prompt_quality.py --live-token <JWT> --live-url http://localhost:30080 --timeout=120

Or use the Makefile target (requires backend running):
    make test-prompts TOKEN=<JWT>
"""

import pytest


def count_worker_tool_calls(events: list, run_id: int) -> int:
    """Count worker tool calls from SSE events for a specific run_id.

    Args:
        events: List of SSE events from collect_events()
        run_id: Run ID to filter events by

    Returns:
        Number of worker_tool_started events for this run
    """
    count = 0
    for event in events:
        if event["type"] == "worker_tool_started":
            # Access run_id from nested payload
            payload = event["data"].get("payload", {})
            event_run_id = payload.get("run_id")
            if event_run_id == run_id:
                count += 1
    return count


def get_supervisor_result(events: list) -> str:
    """Extract the final supervisor result from events.

    Args:
        events: List of SSE events from collect_events()

    Returns:
        The final result message
    """
    for event in reversed(events):
        if event["type"] == "supervisor_complete":
            # Access result from nested payload
            payload = event["data"].get("payload", {})
            return payload.get("result", "")
    return ""


@pytest.mark.live
def test_simple_disk_check_efficiency(supervisor_client):
    """Simple disk check should use ≤2 tool calls.

    This caught a regression where workers were making 8+ calls for simple tasks.
    Expected: 1 tool call (df -h)
    Acceptable: 2 tool calls (connection check + df)
    """
    task = "Check disk space on localhost using runner_exec or ssh_exec"

    # Dispatch and collect events
    run_id = supervisor_client.dispatch(task)
    events = supervisor_client.collect_events(run_id, timeout=90)

    # Count tool calls via SSE
    tool_calls = count_worker_tool_calls(events, run_id)

    # Get result for debugging
    result = get_supervisor_result(events)

    # Verify success
    assert result, f"No result returned for run {run_id}"
    assert "disk" in result.lower() or "df" in result.lower(), (
        f"Result doesn't appear to contain disk info. Got: {result[:200]}"
    )

    # Ensure we actually measured something (prevents false pass if SSE parsing fails)
    assert tool_calls >= 1, f"Expected at least 1 tool call but measured {tool_calls}. SSE parsing may be broken."

    # Assert efficiency
    assert tool_calls <= 2, (
        f"Simple disk check used {tool_calls} tool calls (expected ≤2). "
        f"This indicates prompt regression. Run ID: {run_id}"
    )


@pytest.mark.live
def test_simple_memory_check_efficiency(supervisor_client):
    """Simple memory check should use ≤2 tool calls."""
    task = "Check memory usage on localhost using runner_exec or ssh_exec"

    run_id = supervisor_client.dispatch(task)
    events = supervisor_client.collect_events(run_id, timeout=90)

    tool_calls = count_worker_tool_calls(events, run_id)
    result = get_supervisor_result(events)

    # Verify success
    assert result, f"No result returned for run {run_id}"
    assert "memory" in result.lower() or "mem" in result.lower() or "ram" in result.lower(), (
        f"Result doesn't appear to contain memory info. Got: {result[:200]}"
    )

    # Ensure we actually measured something (prevents false pass if SSE parsing fails)
    assert tool_calls >= 1, f"Expected at least 1 tool call but measured {tool_calls}. SSE parsing may be broken."

    assert tool_calls <= 2, (
        f"Simple memory check used {tool_calls} tool calls (expected ≤2). "
        f"Run ID: {run_id}"
    )


@pytest.mark.live
def test_container_list_efficiency(supervisor_client):
    """Listing containers should use ≤2 tool calls."""
    task = "List running docker containers on localhost"

    run_id = supervisor_client.dispatch(task)
    events = supervisor_client.collect_events(run_id, timeout=90)

    tool_calls = count_worker_tool_calls(events, run_id)
    result = get_supervisor_result(events)

    # Verify success
    assert result, f"No result returned for run {run_id}"
    assert "docker" in result.lower() or "container" in result.lower(), (
        f"Result doesn't appear to contain container info. Got: {result[:200]}"
    )

    # Ensure we actually measured something (prevents false pass if SSE parsing fails)
    assert tool_calls >= 1, f"Expected at least 1 tool call but measured {tool_calls}. SSE parsing may be broken."

    assert tool_calls <= 2, (
        f"Container list used {tool_calls} tool calls (expected ≤2). "
        f"Run ID: {run_id}"
    )
