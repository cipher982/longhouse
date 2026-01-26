"""Tests for CommisArtifactStore service."""

import json
import tempfile
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from tests.conftest import TEST_MODEL
from zerg.services.commis_artifact_store import CommisArtifactStore


@pytest.fixture
def temp_store():
    """Create a temporary artifact store for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield CommisArtifactStore(base_path=tmpdir)


def test_create_commis(temp_store):
    """Test creating a commis directory structure."""
    commis_id = temp_store.create_commis(
        task="Check disk space on all servers",
        config={"model": TEST_MODEL, "timeout": 300},
    )

    # Verify commis_id format
    assert "_" in commis_id
    timestamp_part, slug_part, suffix = commis_id.split("_", 2)
    assert "T" in timestamp_part  # ISO timestamp format
    # Slug is truncated to 30 chars, may vary depending on task length
    assert slug_part.startswith("check-disk-space")
    assert len(slug_part) <= 30
    assert len(suffix) == 6

    # Verify directory structure
    commis_dir = temp_store.base_path / commis_id
    assert commis_dir.exists()
    assert (commis_dir / "tool_calls").exists()
    assert (commis_dir / "metadata.json").exists()

    # Verify metadata content
    with open(commis_dir / "metadata.json", "r") as f:
        metadata = json.load(f)

    assert metadata["commis_id"] == commis_id
    assert metadata["task"] == "Check disk space on all servers"
    assert metadata["config"]["model"] == TEST_MODEL
    assert metadata["status"] == "created"
    assert metadata["created_at"] is not None
    assert metadata["started_at"] is None
    assert metadata["finished_at"] is None

    # Verify index updated
    index = temp_store._read_index()
    assert len(index) == 1
    assert index[0]["commis_id"] == commis_id


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
        commis_id = temp_store.create_commis(task)
        _, slug_part, _ = commis_id.split("_", 2)
        # Slug is truncated to 30 chars max
        expected_truncated = expected_slug[:30]
        assert slug_part == expected_truncated


def test_save_tool_output(temp_store):
    """Test saving tool outputs."""
    commis_id = temp_store.create_commis("Test task")

    # Save multiple tool outputs
    path1 = temp_store.save_tool_output(commis_id, "ssh_exec", "Output from SSH command", sequence=1)
    path2 = temp_store.save_tool_output(commis_id, "http_request", '{"status": "ok"}', sequence=2)

    assert path1 == "tool_calls/001_ssh_exec.txt"
    assert path2 == "tool_calls/002_http_request.txt"

    # Verify files exist and content correct
    commis_dir = temp_store.base_path / commis_id
    with open(commis_dir / path1, "r") as f:
        assert f.read() == "Output from SSH command"
    with open(commis_dir / path2, "r") as f:
        assert f.read() == '{"status": "ok"}'


def test_save_message(temp_store):
    """Test saving messages to thread.jsonl."""
    commis_id = temp_store.create_commis("Test task")

    # Save multiple messages
    messages = [
        {"role": "user", "content": "What's the disk space?"},
        {"role": "assistant", "content": "Let me check...", "tool_calls": []},
        {"role": "tool", "content": "Disk usage: 45%"},
        {"role": "assistant", "content": "The disk is at 45% capacity."},
    ]

    for msg in messages:
        temp_store.save_message(commis_id, msg)

    # Verify thread.jsonl content
    commis_dir = temp_store.base_path / commis_id
    thread_path = commis_dir / "thread.jsonl"
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
    commis_id = temp_store.create_commis("Test task")

    result_text = "The disk space check completed successfully. All servers have adequate space."
    temp_store.save_result(commis_id, result_text)

    # Verify result.txt
    commis_dir = temp_store.base_path / commis_id
    result_path = commis_dir / "result.txt"
    assert result_path.exists()

    with open(result_path, "r") as f:
        assert f.read() == result_text


def test_start_and_complete_commis(temp_store):
    """Test commis lifecycle: create -> start -> complete."""
    commis_id = temp_store.create_commis("Test task")

    # Start commis
    temp_store.start_commis(commis_id)
    metadata = temp_store.get_commis_metadata(commis_id)
    assert metadata["status"] == "running"
    assert metadata["started_at"] is not None

    # Complete commis
    temp_store.complete_commis(commis_id, status="success")
    metadata = temp_store.get_commis_metadata(commis_id)
    assert metadata["status"] == "success"
    assert metadata["finished_at"] is not None
    assert metadata["duration_ms"] is not None
    assert metadata["duration_ms"] >= 0


def test_complete_commis_with_error(temp_store):
    """Test completing commis with error."""
    commis_id = temp_store.create_commis("Test task")
    temp_store.start_commis(commis_id)

    error_msg = "Connection timeout to server"
    temp_store.complete_commis(commis_id, status="failed", error=error_msg)

    metadata = temp_store.get_commis_metadata(commis_id)
    assert metadata["status"] == "failed"
    assert metadata["error"] == error_msg
    assert metadata["finished_at"] is not None


def test_get_commis_metadata(temp_store):
    """Test reading commis metadata."""
    commis_id = temp_store.create_commis("Test task", config={"model": TEST_MODEL, "timeout": 300})

    metadata = temp_store.get_commis_metadata(commis_id)
    assert metadata["commis_id"] == commis_id
    assert metadata["task"] == "Test task"
    assert metadata["config"]["model"] == TEST_MODEL
    assert metadata["status"] == "created"


def test_get_commis_metadata_not_found(temp_store):
    """Test reading metadata for non-existent commis."""
    with pytest.raises(FileNotFoundError):
        temp_store.get_commis_metadata("nonexistent-commis")


def test_get_commis_result(temp_store):
    """Test reading commis result."""
    commis_id = temp_store.create_commis("Test task")
    result_text = "Task completed successfully"
    temp_store.save_result(commis_id, result_text)

    result = temp_store.get_commis_result(commis_id)
    assert result == result_text


def test_get_commis_result_not_found(temp_store):
    """Test reading result when file doesn't exist."""
    commis_id = temp_store.create_commis("Test task")

    with pytest.raises(FileNotFoundError):
        temp_store.get_commis_result(commis_id)


def test_read_commis_file(temp_store):
    """Test reading arbitrary files from commis directory."""
    commis_id = temp_store.create_commis("Test task")
    temp_store.save_tool_output(commis_id, "ssh_exec", "SSH output", sequence=1)

    # Read tool output file
    content = temp_store.read_commis_file(commis_id, "tool_calls/001_ssh_exec.txt")
    assert content == "SSH output"

    # Read metadata
    metadata_content = temp_store.read_commis_file(commis_id, "metadata.json")
    metadata = json.loads(metadata_content)
    assert metadata["commis_id"] == commis_id


def test_read_commis_file_security(temp_store):
    """Test security: prevent directory traversal."""
    commis_id = temp_store.create_commis("Test task")

    # Attempt directory traversal
    with pytest.raises(ValueError, match="Invalid relative path"):
        temp_store.read_commis_file(commis_id, "../../../etc/passwd")

    with pytest.raises(ValueError, match="Invalid relative path"):
        temp_store.read_commis_file(commis_id, "/etc/passwd")


def test_list_commis(temp_store):
    """Test listing commis."""
    # Create multiple commis
    commis_ids = []
    for i in range(5):
        commis_id = temp_store.create_commis(f"Task {i}")
        commis_ids.append(commis_id)

    # List all commis
    commis = temp_store.list_commis(limit=10)
    assert len(commis) == 5

    # Verify sorted by created_at descending (newest first)
    for i in range(len(commis) - 1):
        assert commis[i]["created_at"] >= commis[i + 1]["created_at"]


def test_list_commis_with_limit(temp_store):
    """Test listing commis with limit."""
    # Create multiple commis
    for i in range(5):
        temp_store.create_commis(f"Task {i}")

    # List with limit
    commis = temp_store.list_commis(limit=3)
    assert len(commis) == 3


def test_list_commis_filter_by_status(temp_store):
    """Test filtering commis by status."""
    # Create commis with different statuses
    commis1 = temp_store.create_commis("Task 1")
    temp_store.start_commis(commis1)
    temp_store.complete_commis(commis1, status="success")

    commis2 = temp_store.create_commis("Task 2")
    temp_store.start_commis(commis2)
    temp_store.complete_commis(commis2, status="failed", error="Test error")

    commis3 = temp_store.create_commis("Task 3")
    temp_store.start_commis(commis3)

    # Filter by success
    success_commis = temp_store.list_commis(status="success")
    assert len(success_commis) == 1
    assert success_commis[0]["commis_id"] == commis1

    # Filter by failed
    failed_commis = temp_store.list_commis(status="failed")
    assert len(failed_commis) == 1
    assert failed_commis[0]["commis_id"] == commis2

    # Filter by running
    running_commis = temp_store.list_commis(status="running")
    assert len(running_commis) == 1
    assert running_commis[0]["commis_id"] == commis3


def test_list_commis_filter_by_since(temp_store):
    """Test filtering commis by creation time."""
    # Create commis at different times
    commis1 = temp_store.create_commis("Task 1")

    # Get timestamp after first commis
    cutoff_time = datetime.now(timezone.utc)

    # Create more commis
    commis2 = temp_store.create_commis("Task 2")
    commis3 = temp_store.create_commis("Task 3")

    # Filter by since
    recent_commis = temp_store.list_commis(since=cutoff_time)
    commis_ids = [w["commis_id"] for w in recent_commis]

    # Should only include commis2 and commis3 (created after cutoff)
    # Note: Due to timestamp precision, commis1 might be included if created
    # at exactly the same time, so we check that at least commis2/commis3 are there
    assert commis2 in commis_ids
    assert commis3 in commis_ids


def test_search_commis(temp_store):
    """Test searching across commis artifacts."""
    # Create commis with searchable content
    commis1 = temp_store.create_commis("Disk check")
    temp_store.save_result(commis1, "Disk usage is at 45% on server cube")

    commis2 = temp_store.create_commis("Memory check")
    temp_store.save_result(commis2, "Memory usage is at 67% on server clifford")

    commis3 = temp_store.create_commis("CPU check")
    temp_store.save_result(commis3, "CPU usage is at 23% on server cube")

    # Search for "cube" - use wildcard glob since result.txt is at root
    matches = temp_store.search_commis("cube", file_glob="*.txt")
    assert len(matches) == 2

    commis_ids = [m["commis_id"] for m in matches]
    assert commis1 in commis_ids
    assert commis3 in commis_ids

    # Verify match content
    for match in matches:
        assert "cube" in match["content"].lower()
        assert match["file"] == "result.txt"
        assert "line" in match


def test_search_commis_filters_by_ids(temp_store):
    """Search can be restricted to specific commis IDs."""
    commis1 = temp_store.create_commis("Disk check")
    temp_store.save_result(commis1, "Disk usage is at 45% on server cube")

    commis2 = temp_store.create_commis("CPU check")
    temp_store.save_result(commis2, "CPU usage is high on server cube")

    # Limit search to commis2 only
    matches = temp_store.search_commis("cube", file_glob="*.txt", commis_ids=[commis2])

    assert len(matches) == 1
    assert matches[0]["commis_id"] == commis2


def test_commis_collision(temp_store):
    """Test that commis_id collision is detected."""
    from unittest.mock import patch

    # Create first commis
    commis_id = temp_store.create_commis("Test task")

    # Mock _generate_commis_id to return the same ID
    with patch.object(temp_store, "_generate_commis_id", return_value=commis_id):
        # Should raise ValueError on collision
        with pytest.raises(ValueError, match="Commis directory already exists"):
            temp_store.create_commis("Test task")


def test_commis_id_unique_with_same_timestamp(monkeypatch, temp_store):
    """Commis IDs should remain unique even with identical timestamps."""
    fixed_time = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001 - test helper
            return fixed_time

    monkeypatch.setattr("zerg.services.commis_artifact_store.datetime", FixedDatetime)

    commis1 = temp_store.create_commis("Same task")
    commis2 = temp_store.create_commis("Same task")

    assert commis1 != commis2
    assert (temp_store.base_path / commis1).exists()
    assert (temp_store.base_path / commis2).exists()


def test_index_persistence(temp_store):
    """Test that index persists across operations."""
    # Create commis
    commis_id = temp_store.create_commis("Test task")

    # Verify index has entry
    index = temp_store._read_index()
    assert len(index) == 1
    assert index[0]["commis_id"] == commis_id

    # Start commis (should update index)
    temp_store.start_commis(commis_id)
    index = temp_store._read_index()
    assert index[0]["status"] == "running"

    # Complete commis (should update index)
    temp_store.complete_commis(commis_id, status="success")
    index = temp_store._read_index()
    assert index[0]["status"] == "success"
    assert index[0]["finished_at"] is not None


def test_index_updates_are_atomic_under_concurrency(temp_store, monkeypatch):
    """Test that concurrent index updates don't clobber each other."""
    import threading
    import time

    original_read = temp_store._read_index
    original_write = temp_store._write_index

    barrier = threading.Barrier(2)
    both_read = threading.Event()
    proceed = threading.Event()

    def blocked_read():
        data = original_read()
        try:
            barrier.wait(timeout=0.5)
            both_read.set()
            proceed.wait(timeout=0.5)
        except threading.BrokenBarrierError:
            # Locking may serialize access; proceed without forcing interleave.
            pass
        return data

    def blocked_write(index):
        return original_write(index)

    monkeypatch.setattr(temp_store, "_read_index", blocked_read)
    monkeypatch.setattr(temp_store, "_write_index", blocked_write)

    def update(commis_id):
        temp_store._update_index(commis_id, {"commis_id": commis_id, "status": "created"})

    t1 = threading.Thread(target=update, args=("commis-one",))
    t2 = threading.Thread(target=update, args=("commis-two",))

    t1.start()
    t2.start()

    # If both threads reached the read barrier, allow them to proceed together.
    if both_read.wait(timeout=0.5):
        proceed.set()

    t1.join(timeout=2)
    t2.join(timeout=2)

    # Final index must contain both entries
    final_index = original_read()
    commis_ids = {entry.get("commis_id") for entry in final_index}
    assert {"commis-one", "commis-two"}.issubset(commis_ids)


def test_multiple_stores_same_path():
    """Test that multiple store instances can access same data."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create commis with first store instance
        store1 = CommisArtifactStore(base_path=tmpdir)
        commis_id = store1.create_commis("Test task")
        store1.save_result(commis_id, "Result from store1")

        # Access same commis with second store instance
        store2 = CommisArtifactStore(base_path=tmpdir)
        result = store2.get_commis_result(commis_id)
        assert result == "Result from store1"

        # List commis from second instance
        commis = store2.list_commis()
        assert len(commis) == 1
        assert commis[0]["commis_id"] == commis_id


def test_env_var_base_path(monkeypatch):
    """Test that SWARMLET_DATA_PATH environment variable is respected."""
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setenv("SWARMLET_DATA_PATH", tmpdir)

        # Create store without explicit base_path
        store = CommisArtifactStore()
        assert str(store.base_path) == tmpdir

        # Verify it works
        commis_id = store.create_commis("Test task")
        assert Path(tmpdir, commis_id).exists()
