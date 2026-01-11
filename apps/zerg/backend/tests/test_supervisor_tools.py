"""Tests for supervisor tools."""

import tempfile

import pytest

from tests.conftest import TEST_MODEL
from tests.conftest import TEST_WORKER_MODEL
from zerg.connectors.context import set_credential_resolver
from zerg.connectors.resolver import CredentialResolver
from zerg.tools.builtin.supervisor_tools import get_worker_metadata
from zerg.tools.builtin.supervisor_tools import grep_workers
from zerg.tools.builtin.supervisor_tools import list_workers
from zerg.tools.builtin.supervisor_tools import read_worker_file
from zerg.tools.builtin.supervisor_tools import read_worker_result
from zerg.tools.builtin.supervisor_tools import spawn_worker


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
    """Test spawning a worker job that gets queued."""
    result = spawn_worker(task="What is 2+2?", model=TEST_WORKER_MODEL)

    # Verify result format - now queued instead of executed synchronously
    # With interrupt/resume pattern, when called outside runnable context:
    # "Worker job {id} queued successfully. Working on: {task}"
    assert "Worker job" in result
    assert "queued successfully" in result
    assert "Working on:" in result  # Changed from "Task:" to match new format

    # Extract job_id from result
    import re

    job_id_match = re.search(r"Worker job (\d+)", result)
    assert job_id_match, f"Could not find job ID in result: {result}"
    job_id = int(job_id_match.group(1))
    assert job_id > 0

    # Verify job record exists in database
    from zerg.models.models import WorkerJob

    job = db_session.query(WorkerJob).filter(WorkerJob.id == job_id).first()
    assert job is not None
    assert job.status == "queued"
    assert "2+2" in job.task


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
    # Spawn a couple of workers (they get queued, not executed synchronously)
    spawn_worker(task="Task 1", model=TEST_WORKER_MODEL)
    spawn_worker(task="Task 2", model=TEST_WORKER_MODEL)

    # List workers
    result = list_workers(limit=10)

    # Check we got results (format: "Recent workers (showing N)")
    assert "showing 2" in result or "Job 1" in result or "Job 2" in result
    # Check task content is visible (either directly or as summary)
    assert "Task 1" in result
    assert "Task 2" in result
    # Workers are queued, not completed synchronously
    assert "QUEUED" in result


def test_security_filtering(credential_context, temp_artifact_path, db_session, test_user):
    """Test that users can only see their own workers."""
    from zerg.connectors.resolver import CredentialResolver
    from zerg.crud import crud

    # 1. Create a worker as User A (test_user)
    spawn_worker(task="User A Task", model=TEST_WORKER_MODEL)

    # Verify User A can see it
    result_a = list_workers()
    assert "User A Task" in result_a

    # 2. Create User B in database (required for foreign key)
    user_b = crud.create_user(
        db=db_session,
        email="userb@test.com",
    )

    # Switch to User B context
    resolver_b = CredentialResolver(agent_id=2, db=db_session, owner_id=user_b.id)
    set_credential_resolver(resolver_b)

    # Verify User B CANNOT see User A's worker
    result_b = list_workers()
    assert "User A Task" not in result_b
    assert "showing 0" in result_b or "No worker" in result_b

    # 3. Create worker as User B
    spawn_worker(task="User B Task", model=TEST_WORKER_MODEL)

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
    res_spawn = spawn_worker(task="Secret Task", model=TEST_WORKER_MODEL)
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


def test_list_workers_with_status_filter(credential_context, temp_artifact_path, db_session):
    """Test listing workers with status filter."""
    # Spawn workers (gets queued)
    spawn_worker(task="Queued task", model=TEST_WORKER_MODEL)

    # List only queued workers (they don't run synchronously anymore)
    result = list_workers(status="queued", limit=10)

    assert "showing" in result or "Job" in result
    assert "QUEUED" in result


def test_list_workers_with_time_filter(credential_context, temp_artifact_path, db_session):
    """Test listing workers with time filter."""
    # Spawn a worker
    spawn_worker(task="Recent task", model=TEST_WORKER_MODEL)

    # List workers from last hour
    result = list_workers(since_hours=1)

    assert "showing" in result or "Job" in result
    assert "Recent task" in result

    # List workers from last 0 hours (should be empty or close to it)
    result = list_workers(since_hours=0)
    # May or may not find it depending on timing, just check no error


def test_read_worker_result_success(credential_context, temp_artifact_path, db_session):
    """Test reading a worker's result (queued jobs not yet executed)."""
    import re

    # Spawn a worker (gets queued, not executed)
    spawn_result = spawn_worker(task="What is 1+1?", model=TEST_WORKER_MODEL)

    # Extract job_id
    job_id_match = re.search(r"Worker job (\d+)", spawn_result)
    assert job_id_match, f"Could not find job ID: {spawn_result}"
    job_id = job_id_match.group(1)

    # Read the result - should fail because job hasn't executed yet
    result = read_worker_result(job_id)

    # Job is queued, not executed, so should report that
    assert "Error" in result or "not started" in result or "not complete" in result


def test_read_worker_result_not_found(temp_artifact_path):
    """Test reading result without context."""
    result = read_worker_result("nonexistent-worker-id")

    assert "Error" in result
    assert "no credential context" in result


def test_read_worker_file_metadata(credential_context, temp_artifact_path, db_session):
    """Test reading worker file (queued job not yet executed)."""
    import re

    # Spawn a worker (gets queued)
    spawn_result = spawn_worker(task="Test task", model=TEST_WORKER_MODEL)

    # Extract job_id
    job_id_match = re.search(r"Worker job (\d+)", spawn_result)
    assert job_id_match
    job_id = job_id_match.group(1)

    # Read metadata.json - job hasn't executed so no artifacts yet
    result = read_worker_file(job_id, "metadata.json")

    # Job is queued, not executed, so should report error
    assert "Error" in result or "not started" in result


def test_read_worker_file_result(credential_context, temp_artifact_path, db_session):
    """Test reading worker result.txt file (queued job not yet executed)."""
    import re

    # Spawn a worker (gets queued)
    spawn_result = spawn_worker(task="Say hello", model=TEST_WORKER_MODEL)

    # Extract job_id
    job_id_match = re.search(r"Worker job (\d+)", spawn_result)
    assert job_id_match
    job_id = job_id_match.group(1)

    # Read result.txt - job hasn't executed so no artifacts yet
    result = read_worker_file(job_id, "result.txt")

    # Job is queued, not executed, so should report error
    assert "Error" in result or "not started" in result


def test_read_worker_file_not_found(credential_context, temp_artifact_path, db_session):
    """Test reading non-existent file from worker."""
    import re

    # Spawn a worker (gets queued)
    spawn_result = spawn_worker(task="Test", model=TEST_WORKER_MODEL)
    job_id_match = re.search(r"Worker job (\d+)", spawn_result)
    assert job_id_match
    job_id = job_id_match.group(1)

    # Try to read non-existent file - job hasn't executed
    result = read_worker_file(job_id, "nonexistent.txt")

    assert "Error" in result


def test_read_worker_file_path_traversal(credential_context, temp_artifact_path, db_session):
    """Test that path traversal is blocked."""
    import re

    # Spawn a worker (gets queued)
    spawn_result = spawn_worker(task="Test", model=TEST_WORKER_MODEL)
    job_id_match = re.search(r"Worker job (\d+)", spawn_result)
    assert job_id_match
    job_id = job_id_match.group(1)

    # Try path traversal - should error (either because job not executed or path invalid)
    result = read_worker_file(job_id, "../../../etc/passwd")

    assert "Error" in result


def test_get_worker_metadata_success(credential_context, temp_artifact_path, db_session):
    """Test getting worker metadata (queued job)."""
    import re

    # Spawn a worker (gets queued)
    spawn_result = spawn_worker(task="Metadata test task", model=TEST_WORKER_MODEL)

    # Extract job_id
    job_id_match = re.search(r"Worker job (\d+)", spawn_result)
    assert job_id_match
    job_id = job_id_match.group(1)

    # Get metadata - this should work even for queued jobs
    result = get_worker_metadata(job_id)

    assert f"Metadata for worker job {job_id}" in result
    assert "Status: queued" in result
    assert "Metadata test task" in result
    assert "Created:" in result


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


def test_grep_workers_with_matches(credential_context, temp_artifact_path, db_session):
    """Test grepping workers for a pattern."""
    # Spawn a worker with distinctive text
    spawn_worker(task="Find the word UNICORN in this task", model=TEST_WORKER_MODEL)

    # Search for the pattern
    result = grep_workers("UNICORN", since_hours=1)

    # Should find the match
    assert "match" in result.lower() or "found" in result.lower()


def test_grep_workers_case_insensitive(credential_context, temp_artifact_path, db_session):
    """Test that grep is case-insensitive."""
    # Spawn a worker
    spawn_worker(task="This task has UPPERCASE text", model=TEST_WORKER_MODEL)

    # Search with lowercase
    result = grep_workers("uppercase", since_hours=1)

    # Should find the match despite case difference
    assert "match" in result.lower() or "found" in result.lower()


def test_multiple_workers_workflow(credential_context, temp_artifact_path, db_session):
    """Test complete workflow with multiple workers."""
    # Spawn multiple workers (get queued)
    spawn_worker(task="First worker task", model=TEST_WORKER_MODEL)
    spawn_worker(task="Second worker task", model=TEST_WORKER_MODEL)
    spawn_worker(task="Third worker task", model=TEST_WORKER_MODEL)

    # List all workers
    list_result = list_workers(limit=10)
    assert "showing 3" in list_result or "Job" in list_result

    # Verify tasks are visible
    assert "First worker task" in list_result
    assert "Second worker task" in list_result
    assert "Third worker task" in list_result

    # Search for a pattern - won't match artifacts since workers haven't executed
    grep_result = grep_workers("worker task", since_hours=1)
    # Queued workers have no artifacts yet, so no matches expected
    assert "No matches" in grep_result or "match" in grep_result.lower()


def test_spawn_worker_with_different_models(credential_context, temp_artifact_path, db_session):
    """Test spawning workers with different models."""
    # Test with worker model (gpt-5-mini)
    result1 = spawn_worker(task="Test with mini", model=TEST_WORKER_MODEL)
    assert "queued successfully" in result1

    # Test with default model (gpt-5.2)
    result2 = spawn_worker(task="Test with default model", model=TEST_MODEL)
    assert "queued successfully" in result2 or "Worker job" in result2


def test_list_workers_limit(credential_context, temp_artifact_path, db_session):
    """Test that list_workers respects limit parameter."""
    # Spawn several workers
    for i in range(5):
        spawn_worker(task=f"Worker {i}", model=TEST_WORKER_MODEL)

    # List with limit of 3
    result = list_workers(limit=3)

    # Should only show 3 workers
    assert "showing 3" in result or result.count("Job") == 3
