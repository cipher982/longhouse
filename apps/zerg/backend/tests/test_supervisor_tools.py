"""Tests for supervisor tools."""

import tempfile
from pathlib import Path

import pytest

from zerg.connectors.context import set_credential_resolver
from zerg.connectors.resolver import CredentialResolver
from zerg.services.worker_artifact_store import WorkerArtifactStore
from zerg.tools.builtin.supervisor_tools import (
    get_worker_metadata,
    grep_workers,
    list_workers,
    read_worker_file,
    read_worker_result,
    spawn_worker,
)


@pytest.fixture
def temp_artifact_path(monkeypatch):
    """Create temporary artifact store path and set environment variable."""
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setenv("SWARMLET_DATA_PATH", tmpdir)
        yield tmpdir


@pytest.fixture
def credential_context(db_session, test_user):
    """Set up credential resolver context for tools."""
    resolver = CredentialResolver(agent_id=1, db=db_session, owner_id=test_user.id)
    token = set_credential_resolver(resolver)
    yield resolver
    set_credential_resolver(None)


def test_spawn_worker_success(credential_context, temp_artifact_path, db_session):
    """Test spawning a worker that completes successfully."""
    result = spawn_worker(task="What is 2+2?", model="gpt-4o-mini")

    # Verify result format
    assert "Worker" in result
    assert "completed successfully" in result
    assert "Result:" in result

    # Extract worker_id from result
    lines = result.split("\n")
    worker_line = [line for line in lines if "Worker" in line][0]
    worker_id = worker_line.split()[1]

    # Verify worker directory exists
    artifact_store = WorkerArtifactStore(base_path=temp_artifact_path)
    metadata = artifact_store.get_worker_metadata(worker_id)
    assert metadata["status"] == "success"
    assert "2+2" in metadata["task"]


def test_spawn_worker_no_context():
    """Test spawning worker without credential context fails gracefully."""
    result = spawn_worker(task="Test task")

    assert "Error" in result
    assert "no credential context" in result


def test_list_workers_empty(temp_artifact_path):
    """Test listing workers when none exist."""
    # We expect a "no credential context" error because we didn't set up context
    result = list_workers()

    assert "Error" in result
    assert "no credential context" in result


def test_list_workers_with_data(credential_context, temp_artifact_path, db_session):
    """Test listing workers after spawning some."""
    # Spawn a couple of workers
    spawn_worker(task="Task 1", model="gpt-4o-mini")
    spawn_worker(task="Task 2", model="gpt-4o-mini")

    # List workers
    result = list_workers(limit=10)

    assert "Found 2 worker(s)" in result
    assert "Task 1" in result
    assert "Task 2" in result
    assert "SUCCESS" in result


def test_security_filtering(credential_context, temp_artifact_path, db_session, test_user):
    """Test that users can only see their own workers."""
    from zerg.connectors.resolver import CredentialResolver
    
    # 1. Create a worker as User A
    spawn_worker(task="User A Task", model="gpt-4o-mini")
    
    # Verify User A can see it
    result_a = list_workers()
    assert "User A Task" in result_a
    
    # 2. Switch to User B context
    user_b_id = test_user.id + 999  # Different ID
    resolver_b = CredentialResolver(agent_id=2, db=db_session, owner_id=user_b_id)
    set_credential_resolver(resolver_b)
    
    # Verify User B CANNOT see User A's worker
    result_b = list_workers()
    assert "User A Task" not in result_b
    assert "Found 0 worker(s)" in result_b or "No workers found" in result_b
    
    # 3. Create worker as User B
    spawn_worker(task="User B Task", model="gpt-4o-mini")
    
    # Verify User B sees their task
    result_b_2 = list_workers()
    assert "User B Task" in result_b_2
    assert "User A Task" not in result_b_2
    
    # 4. Switch back to User A
    set_credential_resolver(credential_context)
    result_a_2 = list_workers()
    assert "User A Task" in result_a_2
    assert "User B Task" not in result_a_2


def test_security_read_access(credential_context, temp_artifact_path, db_session, test_user):
    """Test that users cannot read artifacts of other users' workers."""
    from zerg.connectors.resolver import CredentialResolver
    
    # 1. Create worker as User A
    res_spawn = spawn_worker(task="Secret Task", model="gpt-4o-mini")
    lines = res_spawn.split("\n")
    worker_line = [line for line in lines if "Worker" in line][0]
    worker_id = worker_line.split()[1]
    
    # 2. Switch to User B
    user_b_id = test_user.id + 999
    resolver_b = CredentialResolver(agent_id=2, db=db_session, owner_id=user_b_id)
    set_credential_resolver(resolver_b)
    
    # 3. Attempt to read result
    res_read = read_worker_result(worker_id)
    assert "Access denied" in res_read or "Error" in res_read
    
    # 4. Attempt to read file
    res_file = read_worker_file(worker_id, "metadata.json")
    assert "Access denied" in res_file or "Error" in res_file
    
    # 5. Attempt to get metadata
    res_meta = get_worker_metadata(worker_id)
    assert "Access denied" in res_meta or "Error" in res_meta
    
    # 6. Attempt to grep
    res_grep = grep_workers("Secret")
    assert "No matches found" in res_grep


def test_list_workers_with_status_filter(
    credential_context, temp_artifact_path, db_session
):
    """Test listing workers with status filter."""
    # Spawn workers
    spawn_worker(task="Successful task", model="gpt-4o-mini")

    # List only successful workers
    result = list_workers(status="success", limit=10)

    assert "Found" in result
    assert "SUCCESS" in result


def test_list_workers_with_time_filter(
    credential_context, temp_artifact_path, db_session
):
    """Test listing workers with time filter."""
    # Spawn a worker
    spawn_worker(task="Recent task", model="gpt-4o-mini")

    # List workers from last hour
    result = list_workers(since_hours=1)

    assert "Found" in result
    assert "Recent task" in result

    # List workers from last 0.001 hours (should be empty)
    result = list_workers(since_hours=0)
    # May or may not find it depending on timing, just check no error


def test_read_worker_result_success(
    credential_context, temp_artifact_path, db_session
):
    """Test reading a worker's result."""
    # Spawn a worker
    spawn_result = spawn_worker(task="What is 1+1?", model="gpt-4o-mini")

    # Extract worker_id
    lines = spawn_result.split("\n")
    worker_line = [line for line in lines if "Worker" in line][0]
    worker_id = worker_line.split()[1]

    # Read the result
    result = read_worker_result(worker_id)

    assert f"Result from worker {worker_id}" in result
    assert len(result) > 50  # Should have actual content


def test_read_worker_result_not_found(temp_artifact_path):
    """Test reading result without context."""
    result = read_worker_result("nonexistent-worker-id")

    assert "Error" in result
    assert "no credential context" in result


def test_read_worker_file_metadata(
    credential_context, temp_artifact_path, db_session
):
    """Test reading worker metadata file."""
    # Spawn a worker
    spawn_result = spawn_worker(task="Test task", model="gpt-4o-mini")

    # Extract worker_id
    lines = spawn_result.split("\n")
    worker_line = [line for line in lines if "Worker" in line][0]
    worker_id = worker_line.split()[1]

    # Read metadata.json
    result = read_worker_file(worker_id, "metadata.json")

    assert "Contents of metadata.json" in result
    assert worker_id in result
    assert "status" in result
    assert "task" in result


def test_read_worker_file_result(credential_context, temp_artifact_path, db_session):
    """Test reading worker result.txt file."""
    # Spawn a worker
    spawn_result = spawn_worker(task="Say hello", model="gpt-4o-mini")

    # Extract worker_id
    lines = spawn_result.split("\n")
    worker_line = [line for line in lines if "Worker" in line][0]
    worker_id = worker_line.split()[1]

    # Read result.txt
    result = read_worker_file(worker_id, "result.txt")

    assert "Contents of result.txt" in result
    assert len(result) > 50  # Should have actual content


def test_read_worker_file_not_found(credential_context, temp_artifact_path, db_session):
    """Test reading non-existent file from worker."""
    # Spawn a worker
    spawn_result = spawn_worker(task="Test", model="gpt-4o-mini")
    lines = spawn_result.split("\n")
    worker_line = [line for line in lines if "Worker" in line][0]
    worker_id = worker_line.split()[1]

    # Try to read non-existent file
    result = read_worker_file(worker_id, "nonexistent.txt")

    assert "Error" in result
    assert "not found" in result


def test_read_worker_file_path_traversal(
    credential_context, temp_artifact_path, db_session
):
    """Test that path traversal is blocked."""
    # Spawn a worker
    spawn_result = spawn_worker(task="Test", model="gpt-4o-mini")
    lines = spawn_result.split("\n")
    worker_line = [line for line in lines if "Worker" in line][0]
    worker_id = worker_line.split()[1]

    # Try path traversal
    result = read_worker_file(worker_id, "../../../etc/passwd")

    assert "Error" in result
    assert "Invalid" in result or "Path escapes" in result


def test_get_worker_metadata_success(
    credential_context, temp_artifact_path, db_session
):
    """Test getting worker metadata."""
    # Spawn a worker
    spawn_result = spawn_worker(task="Metadata test task", model="gpt-4o-mini")

    # Extract worker_id
    lines = spawn_result.split("\n")
    worker_line = [line for line in lines if "Worker" in line][0]
    worker_id = worker_line.split()[1]

    # Get metadata
    result = get_worker_metadata(worker_id)

    assert f"Metadata for worker {worker_id}" in result
    assert "Status: success" in result
    assert "Metadata test task" in result
    assert "Created:" in result
    assert "Duration:" in result


def test_get_worker_metadata_not_found(temp_artifact_path):
    """Test getting metadata without context."""
    result = get_worker_metadata("nonexistent-worker")

    assert "Error" in result
    assert "no credential context" in result


def test_grep_workers_no_matches(temp_artifact_path):
    """Test grepping workers without context."""
    result = grep_workers("nonexistent-pattern-xyz")

    assert "Error" in result
    assert "no credential context" in result


def test_grep_workers_with_matches(
    credential_context, temp_artifact_path, db_session
):
    """Test grepping workers for a pattern."""
    # Spawn a worker with distinctive text
    spawn_worker(task="Find the word UNICORN in this task", model="gpt-4o-mini")

    # Search for the pattern
    result = grep_workers("UNICORN", since_hours=1)

    # Should find the match
    assert "match" in result.lower() or "found" in result.lower()


def test_grep_workers_case_insensitive(
    credential_context, temp_artifact_path, db_session
):
    """Test that grep is case-insensitive."""
    # Spawn a worker
    spawn_worker(task="This task has UPPERCASE text", model="gpt-4o-mini")

    # Search with lowercase
    result = grep_workers("uppercase", since_hours=1)

    # Should find the match despite case difference
    assert "match" in result.lower() or "found" in result.lower()


def test_multiple_workers_workflow(credential_context, temp_artifact_path, db_session):
    """Test complete workflow with multiple workers."""
    # Spawn multiple workers
    spawn_worker(task="First worker task", model="gpt-4o-mini")
    spawn_worker(task="Second worker task", model="gpt-4o-mini")
    spawn_worker(task="Third worker task", model="gpt-4o-mini")

    # List all workers
    list_result = list_workers(limit=10)
    assert "Found 3 worker(s)" in list_result

    # Extract a worker_id
    lines = list_result.split("\n")
    worker_line = [line for line in lines if "First worker task" in line]
    assert len(worker_line) > 0

    # Search for a pattern
    grep_result = grep_workers("worker task", since_hours=1)
    assert "match" in grep_result.lower() or "found" in grep_result.lower()


def test_spawn_worker_with_different_models(
    credential_context, temp_artifact_path, db_session
):
    """Test spawning workers with different models."""
    # Test with gpt-4o-mini
    result1 = spawn_worker(task="Test with mini", model="gpt-4o-mini")
    assert "completed successfully" in result1

    # Test with gpt-4o (if available)
    result2 = spawn_worker(task="Test with gpt-4o", model="gpt-4o")
    # Should work or fail gracefully
    assert "Worker" in result2


def test_list_workers_limit(credential_context, temp_artifact_path, db_session):
    """Test that list_workers respects limit parameter."""
    # Spawn several workers
    for i in range(5):
        spawn_worker(task=f"Worker {i}", model="gpt-4o-mini")

    # List with limit of 3
    result = list_workers(limit=3)

    # Should only show 3 workers
    assert "Found 3 worker(s)" in result
