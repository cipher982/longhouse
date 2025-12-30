"""Live prompt quality tests - regression testing for worker efficiency.

These tests spawn actual workers and measure tool call efficiency.
They catch regressions like "8 tool calls for a simple disk check".

Run with:
    cd apps/zerg/backend
    uv run pytest tests/live/test_prompt_quality.py --live-token <JWT>

Or use the Makefile target (requires backend running):
    make test-prompts
"""

import os
from pathlib import Path

import pytest


def count_tool_calls(worker_id: str) -> int:
    """Count tool calls by checking files in worker's tool_calls/ directory.

    Args:
        worker_id: Worker identifier (e.g., "2024-12-30T12-00-00_check-disk")

    Returns:
        Number of tool call files found
    """
    # Find worker artifacts base path
    base_paths = [
        Path(os.getenv("SWARMLET_DATA_PATH", "/data/swarmlet/workers")),
        Path("/data/swarmlet/workers"),
        Path("/app/data/swarmlet/workers"),
        Path("/tmp/swarmlet/workers"),
    ]

    for base_path in base_paths:
        worker_dir = base_path / worker_id / "tool_calls"
        if worker_dir.exists():
            # Count files in tool_calls directory (excludes directories)
            return sum(1 for f in worker_dir.iterdir() if f.is_file())

    # If no worker directory found, fail the test
    pytest.fail(f"Worker directory not found for {worker_id} in any of: {base_paths}")
    return 0


@pytest.mark.live
def test_simple_disk_check_efficiency(supervisor_client):
    """Simple disk check should use ≤2 tool calls.

    This caught a regression where workers were making 8+ calls for simple tasks.
    Expected: 1 tool call (df -h)
    Acceptable: 2 tool calls (connection check + df)
    """
    task = "Check disk space on localhost using runner_exec or ssh_exec"

    # Dispatch and wait
    run_id = supervisor_client.dispatch(task)
    result = supervisor_client.wait_for_completion(run_id, timeout=60)

    # Extract worker_id from result (supervisor should mention it)
    # For now, we'll need to query the worker artifacts directory
    # This is a limitation - we don't have a clean API to get worker_id from run_id
    # For a minimal implementation, we can check the most recent worker

    # Find most recent worker directory
    base_paths = [
        Path(os.getenv("SWARMLET_DATA_PATH", "/data/swarmlet/workers")),
        Path("/data/swarmlet/workers"),
        Path("/app/data/swarmlet/workers"),
        Path("/tmp/swarmlet/workers"),
    ]

    worker_id = None
    latest_time = None

    for base_path in base_paths:
        if not base_path.exists():
            continue

        for worker_dir in base_path.iterdir():
            if worker_dir.is_dir() and not worker_dir.name.startswith("."):
                mtime = worker_dir.stat().st_mtime
                if latest_time is None or mtime > latest_time:
                    latest_time = mtime
                    worker_id = worker_dir.name

    if worker_id is None:
        pytest.skip("No worker artifacts found - may need SWARMLET_DATA_PATH configured")

    tool_calls = count_tool_calls(worker_id)

    # Assert efficiency
    assert tool_calls <= 2, (
        f"Simple disk check used {tool_calls} tool calls (expected ≤2). "
        f"This indicates prompt regression. Worker: {worker_id}"
    )


@pytest.mark.live
def test_simple_memory_check_efficiency(supervisor_client):
    """Simple memory check should use ≤2 tool calls."""
    task = "Check memory usage on localhost using runner_exec or ssh_exec"

    run_id = supervisor_client.dispatch(task)
    result = supervisor_client.wait_for_completion(run_id, timeout=60)

    # Find most recent worker
    base_paths = [
        Path(os.getenv("SWARMLET_DATA_PATH", "/data/swarmlet/workers")),
        Path("/data/swarmlet/workers"),
        Path("/app/data/swarmlet/workers"),
        Path("/tmp/swarmlet/workers"),
    ]

    worker_id = None
    latest_time = None

    for base_path in base_paths:
        if not base_path.exists():
            continue

        for worker_dir in base_path.iterdir():
            if worker_dir.is_dir() and not worker_dir.name.startswith("."):
                mtime = worker_dir.stat().st_mtime
                if latest_time is None or mtime > latest_time:
                    latest_time = mtime
                    worker_id = worker_dir.name

    if worker_id is None:
        pytest.skip("No worker artifacts found")

    tool_calls = count_tool_calls(worker_id)

    assert tool_calls <= 2, (
        f"Simple memory check used {tool_calls} tool calls (expected ≤2). "
        f"Worker: {worker_id}"
    )


@pytest.mark.live
def test_container_list_efficiency(supervisor_client):
    """Listing containers should use ≤2 tool calls."""
    task = "List running docker containers on localhost"

    run_id = supervisor_client.dispatch(task)
    result = supervisor_client.wait_for_completion(run_id, timeout=60)

    # Find most recent worker
    base_paths = [
        Path(os.getenv("SWARMLET_DATA_PATH", "/data/swarmlet/workers")),
        Path("/data/swarmlet/workers"),
        Path("/app/data/swarmlet/workers"),
        Path("/tmp/swarmlet/workers"),
    ]

    worker_id = None
    latest_time = None

    for base_path in base_paths:
        if not base_path.exists():
            continue

        for worker_dir in base_path.iterdir():
            if worker_dir.is_dir() and not worker_dir.name.startswith("."):
                mtime = worker_dir.stat().st_mtime
                if latest_time is None or mtime > latest_time:
                    latest_time = mtime
                    worker_id = worker_dir.name

    if worker_id is None:
        pytest.skip("No worker artifacts found")

    tool_calls = count_tool_calls(worker_id)

    assert tool_calls <= 2, (
        f"Container list used {tool_calls} tool calls (expected ≤2). "
        f"Worker: {worker_id}"
    )
