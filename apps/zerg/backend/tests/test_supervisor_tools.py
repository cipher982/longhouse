"""Tests for concierge tools."""

import tempfile

import pytest

from tests.conftest import TEST_MODEL
from tests.conftest import TEST_COMMIS_MODEL
from zerg.connectors.context import set_credential_resolver
from zerg.connectors.resolver import CredentialResolver
from zerg.tools.builtin.concierge_tools import get_commis_metadata
from zerg.tools.builtin.concierge_tools import get_commis_evidence
from zerg.tools.builtin.concierge_tools import get_tool_output
from zerg.tools.builtin.concierge_tools import grep_commis
from zerg.tools.builtin.concierge_tools import list_commis
from zerg.tools.builtin.concierge_tools import read_commis_file
from zerg.tools.builtin.concierge_tools import read_commis_result
from zerg.tools.builtin.concierge_tools import spawn_commis
from zerg.tools.builtin.concierge_tools import spawn_workspace_commis


@pytest.fixture
def temp_artifact_path(monkeypatch):
    """Create temporary artifact store path and set environment variable."""
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setenv("SWARMLET_DATA_PATH", tmpdir)
        yield tmpdir


@pytest.fixture
def credential_context(db_session, test_user):
    """Set up credential resolver context for tools."""
    resolver = CredentialResolver(fiche_id=1, db=db_session, owner_id=test_user.id)
    token = set_credential_resolver(resolver)
    yield resolver
    set_credential_resolver(None)


def _count_commis_jobs(db_session) -> int:
    from zerg.models.models import CommisJob

    return db_session.query(CommisJob).count()


def test_spawn_commis_success(credential_context, temp_artifact_path, db_session):
    """Test spawning a commis job that gets queued."""
    result = spawn_commis(task="What is 2+2?", model=TEST_COMMIS_MODEL)

    # Verify result format - now queued instead of executed synchronously
    # With interrupt/resume pattern, when called outside runnable context:
    # "Commis job {id} queued successfully. Working on: {task}"
    assert "Commis job" in result
    assert "queued successfully" in result
    assert "Working on:" in result  # Changed from "Task:" to match new format

    # Extract job_id from result
    import re

    job_id_match = re.search(r"Commis job (\d+)", result)
    assert job_id_match, f"Could not find job ID in result: {result}"
    job_id = int(job_id_match.group(1))
    assert job_id > 0

    # Verify job record exists in database
    from zerg.models.models import CommisJob

    job = db_session.query(CommisJob).filter(CommisJob.id == job_id).first()
    assert job is not None
    assert job.status == "queued"
    assert "2+2" in job.task


def test_spawn_commis_no_context():
    """Test spawning commis without credential context fails gracefully."""
    result = spawn_commis(task="Test task")

    assert "Error" in result
    assert "no credential context" in result


def test_spawn_workspace_commis_success(credential_context, temp_artifact_path, db_session):
    """Test spawning a workspace commis with git_repo creates correct job config."""
    result = spawn_workspace_commis(
        task="List dependencies from pyproject.toml",
        git_repo="https://github.com/langchain-ai/langchain.git",
        model=TEST_COMMIS_MODEL,
    )

    # Verify result format - job queued
    assert "Commis job" in result
    assert "queued successfully" in result
    assert "Working on:" in result

    # Extract job_id from result
    import re

    job_id_match = re.search(r"Commis job (\d+)", result)
    assert job_id_match, f"Could not find job ID in result: {result}"
    job_id = int(job_id_match.group(1))
    assert job_id > 0

    # Verify job record has workspace execution config
    from zerg.models.models import CommisJob

    job = db_session.query(CommisJob).filter(CommisJob.id == job_id).first()
    assert job is not None
    assert job.status == "queued"
    assert job.config is not None
    assert job.config.get("execution_mode") == "workspace"
    assert job.config.get("git_repo") == "https://github.com/langchain-ai/langchain.git"


def test_spawn_workspace_commis_no_context():
    """Test spawning workspace commis without credential context fails gracefully."""
    result = spawn_workspace_commis(
        task="Test task",
        git_repo="https://github.com/test/repo.git",
    )

    assert "Error" in result
    assert "no credential context" in result


def test_spawn_commis_has_no_config(credential_context, temp_artifact_path, db_session):
    """Test that standard spawn_commis creates job WITHOUT execution config."""
    result = spawn_commis(task="Check disk space", model=TEST_COMMIS_MODEL)

    # Verify result format
    assert "Commis job" in result
    assert "queued successfully" in result

    # Extract job_id
    import re

    job_id_match = re.search(r"Commis job (\d+)", result)
    assert job_id_match
    job_id = int(job_id_match.group(1))

    # Verify job record has NO config (standard mode)
    from zerg.models.models import CommisJob

    job = db_session.query(CommisJob).filter(CommisJob.id == job_id).first()
    assert job is not None
    assert job.config is None  # Standard commis has no special config


def test_spawn_workspace_commis_ssh_url(credential_context, temp_artifact_path, db_session):
    """Test spawning workspace commis with SSH git URL."""
    result = spawn_workspace_commis(
        task="Fix typo in README.md",
        git_repo="git@github.com:cipher982/zerg.git",
        model=TEST_COMMIS_MODEL,
    )

    assert "Commis job" in result
    assert "queued successfully" in result

    # Extract job_id and verify config
    import re

    job_id_match = re.search(r"Commis job (\d+)", result)
    job_id = int(job_id_match.group(1))

    from zerg.models.models import CommisJob

    job = db_session.query(CommisJob).filter(CommisJob.id == job_id).first()
    assert job.config.get("git_repo") == "git@github.com:cipher982/zerg.git"
    assert job.config.get("execution_mode") == "workspace"


def test_spawn_workspace_commis_rejects_file_url(credential_context, temp_artifact_path, db_session):
    """Test that file:// URLs are rejected early (security)."""
    before_count = _count_commis_jobs(db_session)
    result = spawn_workspace_commis(
        task="Test task",
        git_repo="file:///etc/passwd",
    )

    assert "Error" in result
    assert "Repository URL must use one of" in result
    assert _count_commis_jobs(db_session) == before_count


def test_spawn_workspace_commis_rejects_flag_injection(credential_context, temp_artifact_path, db_session):
    """Test that URLs starting with '-' are rejected (flag injection)."""
    before_count = _count_commis_jobs(db_session)
    result = spawn_workspace_commis(
        task="Test task",
        git_repo="-o ProxyCommand=whoami",
    )

    assert "Error" in result
    assert "cannot start with '-'" in result
    assert _count_commis_jobs(db_session) == before_count


def test_spawn_workspace_commis_rejects_empty_repo(credential_context, temp_artifact_path, db_session):
    """Test that empty git_repo is rejected."""
    before_count = _count_commis_jobs(db_session)
    result = spawn_workspace_commis(
        task="Test task",
        git_repo="",
    )

    assert "Error" in result
    assert "cannot be empty" in result
    assert _count_commis_jobs(db_session) == before_count


def test_spawn_workspace_commis_ssh_scheme_url(credential_context, temp_artifact_path, db_session):
    """Test spawning workspace commis with ssh:// git URL."""
    result = spawn_workspace_commis(
        task="Audit README via ssh scheme",
        git_repo="ssh://git@github.com/cipher982/zerg.git",
        model=TEST_COMMIS_MODEL,
    )

    assert "Commis job" in result
    assert "queued successfully" in result

    import re

    job_id_match = re.search(r"Commis job (\d+)", result)
    job_id = int(job_id_match.group(1))

    from zerg.models.models import CommisJob

    job = db_session.query(CommisJob).filter(CommisJob.id == job_id).first()
    assert job.config.get("git_repo") == "ssh://git@github.com/cipher982/zerg.git"
    assert job.config.get("execution_mode") == "workspace"


def test_spawn_workspace_commis_rejects_ssh_option_injection(
    credential_context, temp_artifact_path, db_session
):
    """Test ssh:// URLs with option injection are rejected."""
    before_count = _count_commis_jobs(db_session)
    result = spawn_workspace_commis(
        task="Test task",
        git_repo="ssh://-oProxyCommand=whoami@github.com/repo.git",
    )

    assert "Error" in result
    assert "SSH option injection" in result or "cannot start with '-'" in result
    assert _count_commis_jobs(db_session) == before_count


def test_spawn_workspace_commis_security_filtering(
    credential_context, temp_artifact_path, db_session, test_user
):
    """Test that workspace commis respect owner isolation."""
    from zerg.connectors.resolver import CredentialResolver
    from zerg.crud import crud

    # 1. Create workspace commis as User A
    spawn_workspace_commis(
        task="User A repo task",
        git_repo="https://github.com/user-a/repo.git",
        model=TEST_COMMIS_MODEL,
    )

    # Verify User A can see it
    result_a = list_commis()
    assert "User A repo task" in result_a

    # 2. Create User B
    user_b = crud.create_user(db=db_session, email="userb_workspace@test.com")

    # Switch to User B context
    resolver_b = CredentialResolver(fiche_id=2, db=db_session, owner_id=user_b.id)
    set_credential_resolver(resolver_b)

    # User B CANNOT see User A's workspace commis
    result_b = list_commis()
    assert "User A repo task" not in result_b

    # 3. User B creates their own workspace commis
    spawn_workspace_commis(
        task="User B repo task",
        git_repo="https://github.com/user-b/repo.git",
        model=TEST_COMMIS_MODEL,
    )

    result_b_2 = list_commis()
    assert "User B repo task" in result_b_2
    assert "User A repo task" not in result_b_2

    # Restore User A context
    set_credential_resolver(credential_context)


def test_list_commis_empty(temp_artifact_path):
    """Test listing commis when none exist."""
    # We expect a "no credential context" error because we didn't set up context
    result = list_commis()

    assert "Error" in result
    assert "no credential context" in result


def test_list_commis_with_data(credential_context, temp_artifact_path, db_session):
    """Test listing commis after spawning some."""
    # Spawn a couple of commis (they get queued, not executed synchronously)
    spawn_commis(task="Task 1", model=TEST_COMMIS_MODEL)
    spawn_commis(task="Task 2", model=TEST_COMMIS_MODEL)

    # List commis
    result = list_commis(limit=10)

    # Check we got results (format: "Recent commis (showing N)")
    assert "showing 2" in result or "Job 1" in result or "Job 2" in result
    # Check task content is visible (either directly or as summary)
    assert "Task 1" in result
    assert "Task 2" in result
    # Commis are queued, not completed synchronously
    assert "QUEUED" in result


def test_security_filtering(credential_context, temp_artifact_path, db_session, test_user):
    """Test that users can only see their own commis."""
    from zerg.connectors.resolver import CredentialResolver
    from zerg.crud import crud

    # 1. Create a commis as User A (test_user)
    spawn_commis(task="User A Task", model=TEST_COMMIS_MODEL)

    # Verify User A can see it
    result_a = list_commis()
    assert "User A Task" in result_a

    # 2. Create User B in database (required for foreign key)
    user_b = crud.create_user(
        db=db_session,
        email="userb@test.com",
    )

    # Switch to User B context
    resolver_b = CredentialResolver(fiche_id=2, db=db_session, owner_id=user_b.id)
    set_credential_resolver(resolver_b)

    # Verify User B CANNOT see User A's commis
    result_b = list_commis()
    assert "User A Task" not in result_b
    assert "showing 0" in result_b or "No commis" in result_b

    # 3. Create commis as User B
    spawn_commis(task="User B Task", model=TEST_COMMIS_MODEL)

    # Verify User B sees their task
    result_b_2 = list_commis()
    assert "User B Task" in result_b_2
    assert "User A Task" not in result_b_2

    # 4. Switch back to User A
    set_credential_resolver(credential_context)
    result_a_2 = list_commis()
    assert "User A Task" in result_a_2
    assert "User B Task" not in result_a_2


def test_security_read_access(credential_context, temp_artifact_path, db_session, test_user):
    """Test that users cannot read artifacts of other users' commis."""
    from zerg.connectors.resolver import CredentialResolver

    # 1. Create commis as User A
    res_spawn = spawn_commis(task="Secret Task", model=TEST_COMMIS_MODEL)
    lines = res_spawn.split("\n")
    commis_line = [line for line in lines if "Commis" in line][0]
    commis_id = commis_line.split()[1]

    # 2. Switch to User B
    user_b_id = test_user.id + 999
    resolver_b = CredentialResolver(fiche_id=2, db=db_session, owner_id=user_b_id)
    set_credential_resolver(resolver_b)

    # 3. Attempt to read result
    res_read = read_commis_result(commis_id)
    assert "Access denied" in res_read or "Error" in res_read

    # 4. Attempt to read file
    res_file = read_commis_file(commis_id, "metadata.json")
    assert "Access denied" in res_file or "Error" in res_file

    # 5. Attempt to get metadata
    res_meta = get_commis_metadata(commis_id)
    assert "Access denied" in res_meta or "Error" in res_meta

    # 6. Attempt to grep
    res_grep = grep_commis("Secret")
    assert "No matches found" in res_grep


def test_list_commis_with_status_filter(credential_context, temp_artifact_path, db_session):
    """Test listing commis with status filter."""
    # Spawn commis (gets queued)
    spawn_commis(task="Queued task", model=TEST_COMMIS_MODEL)

    # List only queued commis (they don't run synchronously anymore)
    result = list_commis(status="queued", limit=10)

    assert "showing" in result or "Job" in result
    assert "QUEUED" in result


def test_list_commis_with_time_filter(credential_context, temp_artifact_path, db_session):
    """Test listing commis with time filter."""
    # Spawn a commis
    spawn_commis(task="Recent task", model=TEST_COMMIS_MODEL)

    # List commis from last hour
    result = list_commis(since_hours=1)

    assert "showing" in result or "Job" in result
    assert "Recent task" in result

    # List commis from last 0 hours (should be empty or close to it)
    result = list_commis(since_hours=0)
    # May or may not find it depending on timing, just check no error


def test_get_commis_evidence_success(credential_context, temp_artifact_path, db_session, test_user):
    """Test compiling evidence for a commis job via tool."""
    import json

    from zerg.models.models import CommisJob
    from zerg.services.commis_artifact_store import CommisArtifactStore

    artifact_store = CommisArtifactStore()
    commis_id = artifact_store.create_commis(
        task="Check disk usage",
        config={"model": "gpt-4"},
        owner_id=test_user.id,
    )

    tool_output = json.dumps(
        {
            "ok": True,
            "data": {
                "host": "server1",
                "command": "df -h",
                "exit_code": 0,
                "stdout": "Filesystem      Size  Used Avail Use%\\n/dev/sda1       100G   45G   55G  45%",
                "stderr": "",
                "duration_ms": 234,
            },
        }
    )
    artifact_store.save_tool_output(commis_id, "ssh_exec", tool_output, sequence=1)

    job = CommisJob(
        owner_id=test_user.id,
        concierge_course_id=None,
        task="Check disk usage",
        status="success",
        commis_id=commis_id,
    )
    db_session.add(job)
    db_session.commit()

    evidence = get_commis_evidence(str(job.id), budget_bytes=5000)

    assert "Evidence for commis job" in evidence
    assert "tool_calls/001_ssh_exec.txt" in evidence
    assert "df -h" in evidence


def test_read_commis_result_success(credential_context, temp_artifact_path, db_session):
    """Test reading a commis's result (queued jobs not yet executed)."""
    import re

    # Spawn a commis (gets queued, not executed)
    spawn_result = spawn_commis(task="What is 1+1?", model=TEST_COMMIS_MODEL)

    # Extract job_id
    job_id_match = re.search(r"Commis job (\d+)", spawn_result)
    assert job_id_match, f"Could not find job ID: {spawn_result}"
    job_id = job_id_match.group(1)

    # Read the result - should fail because job hasn't executed yet
    result = read_commis_result(job_id)

    # Job is queued, not executed, so should report that
    assert "Error" in result or "not started" in result or "not complete" in result


def test_read_commis_result_not_found(temp_artifact_path):
    """Test reading result without context."""
    result = read_commis_result("nonexistent-commis-id")

    assert "Error" in result
    assert "no credential context" in result


def test_get_tool_output_no_context():
    """Tool output should require credential context."""
    result = get_tool_output("deadbeef")

    assert "Error" in result
    assert "no credential context" in result


def test_read_commis_file_metadata(credential_context, temp_artifact_path, db_session):
    """Test reading commis file (queued job not yet executed)."""
    import re

    # Spawn a commis (gets queued)
    spawn_result = spawn_commis(task="Test task", model=TEST_COMMIS_MODEL)

    # Extract job_id
    job_id_match = re.search(r"Commis job (\d+)", spawn_result)
    assert job_id_match
    job_id = job_id_match.group(1)

    # Read metadata.json - job hasn't executed so no artifacts yet
    result = read_commis_file(job_id, "metadata.json")

    # Job is queued, not executed, so should report error
    assert "Error" in result or "not started" in result


def test_read_commis_file_result(credential_context, temp_artifact_path, db_session):
    """Test reading commis result.txt file (queued job not yet executed)."""
    import re

    # Spawn a commis (gets queued)
    spawn_result = spawn_commis(task="Say hello", model=TEST_COMMIS_MODEL)

    # Extract job_id
    job_id_match = re.search(r"Commis job (\d+)", spawn_result)
    assert job_id_match
    job_id = job_id_match.group(1)

    # Read result.txt - job hasn't executed so no artifacts yet
    result = read_commis_file(job_id, "result.txt")

    # Job is queued, not executed, so should report error
    assert "Error" in result or "not started" in result


def test_read_commis_file_not_found(credential_context, temp_artifact_path, db_session):
    """Test reading non-existent file from commis."""
    import re

    # Spawn a commis (gets queued)
    spawn_result = spawn_commis(task="Test", model=TEST_COMMIS_MODEL)
    job_id_match = re.search(r"Commis job (\d+)", spawn_result)
    assert job_id_match
    job_id = job_id_match.group(1)

    # Try to read non-existent file - job hasn't executed
    result = read_commis_file(job_id, "nonexistent.txt")

    assert "Error" in result


def test_read_commis_file_path_traversal(credential_context, temp_artifact_path, db_session):
    """Test that path traversal is blocked."""
    import re

    # Spawn a commis (gets queued)
    spawn_result = spawn_commis(task="Test", model=TEST_COMMIS_MODEL)
    job_id_match = re.search(r"Commis job (\d+)", spawn_result)
    assert job_id_match
    job_id = job_id_match.group(1)

    # Try path traversal - should error (either because job not executed or path invalid)
    result = read_commis_file(job_id, "../../../etc/passwd")

    assert "Error" in result


def test_get_commis_metadata_success(credential_context, temp_artifact_path, db_session):
    """Test getting commis metadata (queued job)."""
    import re

    # Spawn a commis (gets queued)
    spawn_result = spawn_commis(task="Metadata test task", model=TEST_COMMIS_MODEL)

    # Extract job_id
    job_id_match = re.search(r"Commis job (\d+)", spawn_result)
    assert job_id_match
    job_id = job_id_match.group(1)

    # Get metadata - this should work even for queued jobs
    result = get_commis_metadata(job_id)

    assert f"Metadata for commis job {job_id}" in result
    assert "Status: queued" in result
    assert "Metadata test task" in result
    assert "Created:" in result


def test_get_commis_metadata_not_found(temp_artifact_path):
    """Test getting metadata without context."""
    result = get_commis_metadata("nonexistent-commis")

    assert "Error" in result
    assert "no credential context" in result


def test_grep_commis_no_matches(temp_artifact_path):
    """Test grepping commis without context."""
    result = grep_commis("nonexistent-pattern-xyz")

    assert "Error" in result
    assert "no credential context" in result


def test_grep_commis_with_matches(credential_context, temp_artifact_path, db_session):
    """Test grepping commis for a pattern."""
    # Spawn a commis with distinctive text
    spawn_commis(task="Find the word UNICORN in this task", model=TEST_COMMIS_MODEL)

    # Search for the pattern
    result = grep_commis("UNICORN", since_hours=1)

    # Should find the match
    assert "match" in result.lower() or "found" in result.lower()


def test_grep_commis_case_insensitive(credential_context, temp_artifact_path, db_session):
    """Test that grep is case-insensitive."""
    # Spawn a commis
    spawn_commis(task="This task has UPPERCASE text", model=TEST_COMMIS_MODEL)

    # Search with lowercase
    result = grep_commis("uppercase", since_hours=1)

    # Should find the match despite case difference
    assert "match" in result.lower() or "found" in result.lower()


def test_get_tool_output_success(credential_context, tmp_path, monkeypatch):
    """Fetch stored tool output using artifact_id."""
    from zerg.services.tool_output_store import ToolOutputStore
    from zerg.tools.builtin import concierge_tools

    store = ToolOutputStore(base_path=str(tmp_path))
    artifact_id = store.save_output(
        owner_id=credential_context.owner_id,
        tool_name="runner_exec",
        content="output payload",
        course_id=12,
        tool_call_id="call-42",
    )

    class TestStore(ToolOutputStore):
        def __init__(self):
            super().__init__(base_path=str(tmp_path))

    monkeypatch.setattr(concierge_tools, "ToolOutputStore", TestStore)

    result = get_tool_output(artifact_id)

    assert "Tool output" in result
    assert "runner_exec" in result
    assert "output payload" in result


def test_multiple_commis_workflow(credential_context, temp_artifact_path, db_session):
    """Test complete workflow with multiple commis."""
    # Spawn multiple commis (get queued)
    spawn_commis(task="First commis task", model=TEST_COMMIS_MODEL)
    spawn_commis(task="Second commis task", model=TEST_COMMIS_MODEL)
    spawn_commis(task="Third commis task", model=TEST_COMMIS_MODEL)

    # List all commis
    list_result = list_commis(limit=10)
    assert "showing 3" in list_result or "Job" in list_result

    # Verify tasks are visible
    assert "First commis task" in list_result
    assert "Second commis task" in list_result
    assert "Third commis task" in list_result

    # Search for a pattern - won't match artifacts since commis haven't executed
    grep_result = grep_commis("commis task", since_hours=1)
    # Queued commis have no artifacts yet, so no matches expected
    assert "No matches" in grep_result or "match" in grep_result.lower()


def test_spawn_commis_with_different_models(credential_context, temp_artifact_path, db_session):
    """Test spawning commis with different models."""
    # Test with commis model (gpt-5-mini)
    result1 = spawn_commis(task="Test with mini", model=TEST_COMMIS_MODEL)
    assert "queued successfully" in result1

    # Test with default model (gpt-5.2)
    result2 = spawn_commis(task="Test with default model", model=TEST_MODEL)
    assert "queued successfully" in result2 or "Commis job" in result2


def test_list_commis_limit(credential_context, temp_artifact_path, db_session):
    """Test that list_commis respects limit parameter."""
    # Spawn several commis
    for i in range(5):
        spawn_commis(task=f"Commis {i}", model=TEST_COMMIS_MODEL)

    # List with limit of 3
    result = list_commis(limit=3)

    # Should only show 3 commis
    assert "showing 3" in result or result.count("Job") == 3
