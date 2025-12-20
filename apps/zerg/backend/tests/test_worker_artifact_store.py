"""Tests for WorkerArtifactStore service."""

import json
import tempfile
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from tests.conftest import TEST_MODEL
from zerg.services.worker_artifact_store import WorkerArtifactStore


@pytest.fixture
def temp_store():
    """Create a temporary artifact store for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield WorkerArtifactStore(base_path=tmpdir)


def test_create_worker(temp_store):
    """Test creating a worker directory structure."""
    worker_id = temp_store.create_worker(
        task="Check disk space on all servers",
        config={"model": TEST_MODEL, "timeout": 300},
    )

    # Verify worker_id format
    assert "_" in worker_id
    timestamp_part, slug_part = worker_id.split("_", 1)
    assert "T" in timestamp_part  # ISO timestamp format
    # Slug is truncated to 30 chars, may vary depending on task length
    assert slug_part.startswith("check-disk-space")
    assert len(slug_part) <= 30

    # Verify directory structure
    worker_dir = temp_store.base_path / worker_id
    assert worker_dir.exists()
    assert (worker_dir / "tool_calls").exists()
    assert (worker_dir / "metadata.json").exists()

    # Verify metadata content
    with open(worker_dir / "metadata.json", "r") as f:
        metadata = json.load(f)

    assert metadata["worker_id"] == worker_id
    assert metadata["task"] == "Check disk space on all servers"
    assert metadata["config"]["model"] == TEST_MODEL
    assert metadata["status"] == "created"
    assert metadata["created_at"] is not None
    assert metadata["started_at"] is None
    assert metadata["finished_at"] is None

    # Verify index updated
    index = temp_store._read_index()
    assert len(index) == 1
    assert index[0]["worker_id"] == worker_id


def test_slugify(temp_store):
    """Test slug generation from various task descriptions."""
    test_cases = [
        ("Check disk space", "check-disk-space"),
        ("Run SSH command on cube server", "run-ssh-command-on-cube-server"),  # 31 chars, will be truncated
        ("Deploy to production!!!", "deploy-to-production"),
        ("Test_with_underscores", "test-with-underscores"),
        ("Multiple   spaces   here", "multiple-spaces-here"),
    ]

    for task, expected_slug in test_cases:
        worker_id = temp_store.create_worker(task)
        _, slug_part = worker_id.split("_", 1)
        # Slug is truncated to 30 chars max
        expected_truncated = expected_slug[:30]
        assert slug_part == expected_truncated


def test_save_tool_output(temp_store):
    """Test saving tool outputs."""
    worker_id = temp_store.create_worker("Test task")

    # Save multiple tool outputs
    path1 = temp_store.save_tool_output(worker_id, "ssh_exec", "Output from SSH command", sequence=1)
    path2 = temp_store.save_tool_output(worker_id, "http_request", '{"status": "ok"}', sequence=2)

    assert path1 == "tool_calls/001_ssh_exec.txt"
    assert path2 == "tool_calls/002_http_request.txt"

    # Verify files exist and content correct
    worker_dir = temp_store.base_path / worker_id
    with open(worker_dir / path1, "r") as f:
        assert f.read() == "Output from SSH command"
    with open(worker_dir / path2, "r") as f:
        assert f.read() == '{"status": "ok"}'


def test_save_message(temp_store):
    """Test saving messages to thread.jsonl."""
    worker_id = temp_store.create_worker("Test task")

    # Save multiple messages
    messages = [
        {"role": "user", "content": "What's the disk space?"},
        {"role": "assistant", "content": "Let me check...", "tool_calls": []},
        {"role": "tool", "content": "Disk usage: 45%"},
        {"role": "assistant", "content": "The disk is at 45% capacity."},
    ]

    for msg in messages:
        temp_store.save_message(worker_id, msg)

    # Verify thread.jsonl content
    worker_dir = temp_store.base_path / worker_id
    thread_path = worker_dir / "thread.jsonl"
    assert thread_path.exists()

    # Read and verify each line
    with open(thread_path, "r") as f:
        lines = f.readlines()

    assert len(lines) == 4
    for i, line in enumerate(lines):
        parsed = json.loads(line)
        assert parsed["role"] == messages[i]["role"]
        assert parsed["content"] == messages[i]["content"]


def test_save_result(temp_store):
    """Test saving final result."""
    worker_id = temp_store.create_worker("Test task")

    result_text = "The disk space check completed successfully. All servers have adequate space."
    temp_store.save_result(worker_id, result_text)

    # Verify result.txt
    worker_dir = temp_store.base_path / worker_id
    result_path = worker_dir / "result.txt"
    assert result_path.exists()

    with open(result_path, "r") as f:
        assert f.read() == result_text


def test_start_and_complete_worker(temp_store):
    """Test worker lifecycle: create -> start -> complete."""
    worker_id = temp_store.create_worker("Test task")

    # Start worker
    temp_store.start_worker(worker_id)
    metadata = temp_store.get_worker_metadata(worker_id)
    assert metadata["status"] == "running"
    assert metadata["started_at"] is not None

    # Complete worker
    temp_store.complete_worker(worker_id, status="success")
    metadata = temp_store.get_worker_metadata(worker_id)
    assert metadata["status"] == "success"
    assert metadata["finished_at"] is not None
    assert metadata["duration_ms"] is not None
    assert metadata["duration_ms"] >= 0


def test_complete_worker_with_error(temp_store):
    """Test completing worker with error."""
    worker_id = temp_store.create_worker("Test task")
    temp_store.start_worker(worker_id)

    error_msg = "Connection timeout to server"
    temp_store.complete_worker(worker_id, status="failed", error=error_msg)

    metadata = temp_store.get_worker_metadata(worker_id)
    assert metadata["status"] == "failed"
    assert metadata["error"] == error_msg
    assert metadata["finished_at"] is not None


def test_get_worker_metadata(temp_store):
    """Test reading worker metadata."""
    worker_id = temp_store.create_worker("Test task", config={"model": TEST_MODEL, "timeout": 300})

    metadata = temp_store.get_worker_metadata(worker_id)
    assert metadata["worker_id"] == worker_id
    assert metadata["task"] == "Test task"
    assert metadata["config"]["model"] == TEST_MODEL
    assert metadata["status"] == "created"


def test_get_worker_metadata_not_found(temp_store):
    """Test reading metadata for non-existent worker."""
    with pytest.raises(FileNotFoundError):
        temp_store.get_worker_metadata("nonexistent-worker")


def test_get_worker_result(temp_store):
    """Test reading worker result."""
    worker_id = temp_store.create_worker("Test task")
    result_text = "Task completed successfully"
    temp_store.save_result(worker_id, result_text)

    result = temp_store.get_worker_result(worker_id)
    assert result == result_text


def test_get_worker_result_not_found(temp_store):
    """Test reading result when file doesn't exist."""
    worker_id = temp_store.create_worker("Test task")

    with pytest.raises(FileNotFoundError):
        temp_store.get_worker_result(worker_id)


def test_read_worker_file(temp_store):
    """Test reading arbitrary files from worker directory."""
    worker_id = temp_store.create_worker("Test task")
    temp_store.save_tool_output(worker_id, "ssh_exec", "SSH output", sequence=1)

    # Read tool output file
    content = temp_store.read_worker_file(worker_id, "tool_calls/001_ssh_exec.txt")
    assert content == "SSH output"

    # Read metadata
    metadata_content = temp_store.read_worker_file(worker_id, "metadata.json")
    metadata = json.loads(metadata_content)
    assert metadata["worker_id"] == worker_id


def test_read_worker_file_security(temp_store):
    """Test security: prevent directory traversal."""
    worker_id = temp_store.create_worker("Test task")

    # Attempt directory traversal
    with pytest.raises(ValueError, match="Invalid relative path"):
        temp_store.read_worker_file(worker_id, "../../../etc/passwd")

    with pytest.raises(ValueError, match="Invalid relative path"):
        temp_store.read_worker_file(worker_id, "/etc/passwd")


def test_list_workers(temp_store):
    """Test listing workers."""
    # Create multiple workers
    worker_ids = []
    for i in range(5):
        worker_id = temp_store.create_worker(f"Task {i}")
        worker_ids.append(worker_id)

    # List all workers
    workers = temp_store.list_workers(limit=10)
    assert len(workers) == 5

    # Verify sorted by created_at descending (newest first)
    for i in range(len(workers) - 1):
        assert workers[i]["created_at"] >= workers[i + 1]["created_at"]


def test_list_workers_with_limit(temp_store):
    """Test listing workers with limit."""
    # Create multiple workers
    for i in range(5):
        temp_store.create_worker(f"Task {i}")

    # List with limit
    workers = temp_store.list_workers(limit=3)
    assert len(workers) == 3


def test_list_workers_filter_by_status(temp_store):
    """Test filtering workers by status."""
    # Create workers with different statuses
    worker1 = temp_store.create_worker("Task 1")
    temp_store.start_worker(worker1)
    temp_store.complete_worker(worker1, status="success")

    worker2 = temp_store.create_worker("Task 2")
    temp_store.start_worker(worker2)
    temp_store.complete_worker(worker2, status="failed", error="Test error")

    worker3 = temp_store.create_worker("Task 3")
    temp_store.start_worker(worker3)

    # Filter by success
    success_workers = temp_store.list_workers(status="success")
    assert len(success_workers) == 1
    assert success_workers[0]["worker_id"] == worker1

    # Filter by failed
    failed_workers = temp_store.list_workers(status="failed")
    assert len(failed_workers) == 1
    assert failed_workers[0]["worker_id"] == worker2

    # Filter by running
    running_workers = temp_store.list_workers(status="running")
    assert len(running_workers) == 1
    assert running_workers[0]["worker_id"] == worker3


def test_list_workers_filter_by_since(temp_store):
    """Test filtering workers by creation time."""
    # Create workers at different times
    worker1 = temp_store.create_worker("Task 1")

    # Get timestamp after first worker
    cutoff_time = datetime.now(timezone.utc)

    # Create more workers
    worker2 = temp_store.create_worker("Task 2")
    worker3 = temp_store.create_worker("Task 3")

    # Filter by since
    recent_workers = temp_store.list_workers(since=cutoff_time)
    worker_ids = [w["worker_id"] for w in recent_workers]

    # Should only include worker2 and worker3 (created after cutoff)
    # Note: Due to timestamp precision, worker1 might be included if created
    # at exactly the same time, so we check that at least worker2/worker3 are there
    assert worker2 in worker_ids
    assert worker3 in worker_ids


def test_search_workers(temp_store):
    """Test searching across worker artifacts."""
    # Create workers with searchable content
    worker1 = temp_store.create_worker("Disk check")
    temp_store.save_result(worker1, "Disk usage is at 45% on server cube")

    worker2 = temp_store.create_worker("Memory check")
    temp_store.save_result(worker2, "Memory usage is at 67% on server clifford")

    worker3 = temp_store.create_worker("CPU check")
    temp_store.save_result(worker3, "CPU usage is at 23% on server cube")

    # Search for "cube" - use wildcard glob since result.txt is at root
    matches = temp_store.search_workers("cube", file_glob="*.txt")
    assert len(matches) == 2

    worker_ids = [m["worker_id"] for m in matches]
    assert worker1 in worker_ids
    assert worker3 in worker_ids

    # Verify match content
    for match in matches:
        assert "cube" in match["content"].lower()
        assert match["file"] == "result.txt"
        assert "line" in match


def test_search_workers_filters_by_ids(temp_store):
    """Search can be restricted to specific worker IDs."""
    worker1 = temp_store.create_worker("Disk check")
    temp_store.save_result(worker1, "Disk usage is at 45% on server cube")

    worker2 = temp_store.create_worker("CPU check")
    temp_store.save_result(worker2, "CPU usage is high on server cube")

    # Limit search to worker2 only
    matches = temp_store.search_workers("cube", file_glob="*.txt", worker_ids=[worker2])

    assert len(matches) == 1
    assert matches[0]["worker_id"] == worker2


def test_worker_collision(temp_store):
    """Test that worker_id collision is detected."""
    from unittest.mock import patch

    # Create first worker
    worker_id = temp_store.create_worker("Test task")

    # Mock _generate_worker_id to return the same ID
    with patch.object(temp_store, "_generate_worker_id", return_value=worker_id):
        # Should raise ValueError on collision
        with pytest.raises(ValueError, match="Worker directory already exists"):
            temp_store.create_worker("Test task")


def test_index_persistence(temp_store):
    """Test that index persists across operations."""
    # Create worker
    worker_id = temp_store.create_worker("Test task")

    # Verify index has entry
    index = temp_store._read_index()
    assert len(index) == 1
    assert index[0]["worker_id"] == worker_id

    # Start worker (should update index)
    temp_store.start_worker(worker_id)
    index = temp_store._read_index()
    assert index[0]["status"] == "running"

    # Complete worker (should update index)
    temp_store.complete_worker(worker_id, status="success")
    index = temp_store._read_index()
    assert index[0]["status"] == "success"
    assert index[0]["finished_at"] is not None


def test_multiple_stores_same_path():
    """Test that multiple store instances can access same data."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create worker with first store instance
        store1 = WorkerArtifactStore(base_path=tmpdir)
        worker_id = store1.create_worker("Test task")
        store1.save_result(worker_id, "Result from store1")

        # Access same worker with second store instance
        store2 = WorkerArtifactStore(base_path=tmpdir)
        result = store2.get_worker_result(worker_id)
        assert result == "Result from store1"

        # List workers from second instance
        workers = store2.list_workers()
        assert len(workers) == 1
        assert workers[0]["worker_id"] == worker_id


def test_env_var_base_path(monkeypatch):
    """Test that SWARMLET_DATA_PATH environment variable is respected."""
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setenv("SWARMLET_DATA_PATH", tmpdir)

        # Create store without explicit base_path
        store = WorkerArtifactStore()
        assert str(store.base_path) == tmpdir

        # Verify it works
        worker_id = store.create_worker("Test task")
        assert Path(tmpdir, worker_id).exists()
