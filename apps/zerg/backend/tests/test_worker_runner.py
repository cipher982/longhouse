"""Tests for WorkerRunner service."""

import tempfile
from pathlib import Path

import pytest

from zerg.services.worker_artifact_store import WorkerArtifactStore
from zerg.services.worker_runner import WorkerRunner


@pytest.fixture
def temp_store():
    """Create a temporary artifact store for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield WorkerArtifactStore(base_path=tmpdir)


@pytest.fixture
def worker_runner(temp_store):
    """Create a WorkerRunner with temp storage."""
    return WorkerRunner(artifact_store=temp_store)


@pytest.mark.asyncio
async def test_run_worker_simple_task(worker_runner, temp_store, db_session, test_user):
    """Test running a simple worker task."""
    from zerg.crud import crud

    # Create a test agent
    agent = crud.create_agent(
        db=db_session,
        owner_id=test_user.id,
        name="Test Agent",
        model="gpt-4o-mini",
        system_instructions="You are a helpful assistant.",
        task_instructions="",
        
    )
    db_session.commit()
    db_session.refresh(agent)

    # Run worker with simple task
    task = "What is 2+2?"
    result = await worker_runner.run_worker(
        db=db_session,
        task=task,
        agent=agent,
    )

    # Verify result structure
    assert result.worker_id is not None
    assert result.status == "success"
    assert result.duration_ms >= 0
    # Result content depends on LLM, so we just check it exists
    assert result.result is not None

    # Verify worker directory created
    worker_dir = temp_store.base_path / result.worker_id
    assert worker_dir.exists()

    # Verify metadata
    metadata = temp_store.get_worker_metadata(result.worker_id)
    assert metadata["status"] == "success"
    assert metadata["task"] == task
    assert metadata["finished_at"] is not None
    assert metadata["duration_ms"] >= 0

    # Verify result.txt exists
    result_path = worker_dir / "result.txt"
    assert result_path.exists()

    # Verify thread.jsonl exists
    thread_path = worker_dir / "thread.jsonl"
    assert thread_path.exists()


@pytest.mark.asyncio
async def test_run_worker_without_agent(worker_runner, temp_store, db_session, test_user):
    """Test running a worker without providing an agent (creates temporary agent)."""
    task = "Say hello world"

    result = await worker_runner.run_worker(
        db=db_session,
        task=task,
        agent=None,  # No agent provided
        agent_config={"model": "gpt-4o-mini"},
    )

    # Verify result
    assert result.worker_id is not None
    assert result.status == "success"

    # Verify worker artifacts
    metadata = temp_store.get_worker_metadata(result.worker_id)
    assert metadata["status"] == "success"
    assert metadata["config"]["model"] == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_run_worker_with_tool_calls(worker_runner, temp_store, db_session, test_user):
    """Test that tool calls are captured and persisted."""
    from zerg.crud import crud

    # Create agent with tools enabled
    agent = crud.create_agent(
        db=db_session,
        owner_id=test_user.id,
        name="Test Agent with Tools",
        model="gpt-4o-mini",
        system_instructions="You are a helpful assistant with access to tools.",
        task_instructions="",
        
    )
    db_session.commit()
    db_session.refresh(agent)

    # Run worker with task that likely triggers tools
    # Note: This depends on LLM behavior and tools available
    task = "Check the current time"

    result = await worker_runner.run_worker(
        db=db_session,
        task=task,
        agent=agent,
    )

    # Verify worker completed
    assert result.status == "success"

    # Check if tool_calls directory has files (may be empty if no tools used)
    worker_dir = temp_store.base_path / result.worker_id
    tool_calls_dir = worker_dir / "tool_calls"
    assert tool_calls_dir.exists()

    # Thread should have messages
    thread_path = worker_dir / "thread.jsonl"
    assert thread_path.exists()
    with open(thread_path, "r") as f:
        lines = f.readlines()
        assert len(lines) >= 2  # At least system + user message


@pytest.mark.asyncio
async def test_run_worker_handles_errors(worker_runner, temp_store, db_session, test_user):
    """Test that worker errors are captured properly."""
    from zerg.crud import crud
    from unittest.mock import patch, AsyncMock

    # Create test agent
    agent = crud.create_agent(
        db=db_session,
        owner_id=test_user.id,
        name="Test Agent",
        model="gpt-4o-mini",
        system_instructions="You are a helpful assistant.",
        task_instructions="",
        
    )
    db_session.commit()
    db_session.refresh(agent)

    # Mock AgentRunner to raise an error
    with patch("zerg.services.worker_runner.AgentRunner") as mock_runner_class:
        mock_instance = AsyncMock()
        mock_instance.run_thread.side_effect = RuntimeError("Simulated agent failure")
        mock_runner_class.return_value = mock_instance

        result = await worker_runner.run_worker(
            db=db_session,
            task="This should fail",
            agent=agent,
        )

        # Verify error captured
        assert result.status == "failed"
        assert result.error is not None
        assert "Simulated agent failure" in result.error

        # Verify worker metadata reflects failure
        metadata = temp_store.get_worker_metadata(result.worker_id)
        assert metadata["status"] == "failed"
        assert metadata["error"] is not None


@pytest.mark.asyncio
async def test_worker_message_persistence(worker_runner, temp_store, db_session, test_user):
    """Test that all messages are persisted to thread.jsonl."""
    from zerg.crud import crud

    agent = crud.create_agent(
        db=db_session,
        owner_id=test_user.id,
        name="Test Agent",
        model="gpt-4o-mini",
        system_instructions="You are a helpful assistant.",
        task_instructions="",
        
    )
    db_session.commit()
    db_session.refresh(agent)

    result = await worker_runner.run_worker(
        db=db_session,
        task="Say hello",
        agent=agent,
    )

    # Read thread.jsonl
    worker_dir = temp_store.base_path / result.worker_id
    thread_path = worker_dir / "thread.jsonl"

    import json

    with open(thread_path, "r") as f:
        messages = [json.loads(line) for line in f]

    # Should have at least: system, user, assistant
    assert len(messages) >= 3

    # Verify message structure
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert messages[1]["content"] == "Say hello"

    # Last message should be assistant (may have tool messages in between)
    assistant_messages = [m for m in messages if m["role"] == "assistant"]
    assert len(assistant_messages) >= 1


@pytest.mark.asyncio
async def test_worker_result_extraction(worker_runner, temp_store, db_session, test_user):
    """Test that final result is correctly extracted from assistant messages."""
    from zerg.crud import crud

    agent = crud.create_agent(
        db=db_session,
        owner_id=test_user.id,
        name="Test Agent",
        model="gpt-4o-mini",
        system_instructions="You are a helpful assistant. Always end your response with 'DONE'.",
        task_instructions="",
        
    )
    db_session.commit()
    db_session.refresh(agent)

    result = await worker_runner.run_worker(
        db=db_session,
        task="Count to three",
        agent=agent,
    )

    # Verify result extracted
    assert result.status == "success"
    assert result.result is not None
    # Result may be empty if LLM doesn't return content (just has tool calls)
    # The important thing is that it doesn't fail

    # Verify result saved to file
    saved_result = temp_store.get_worker_result(result.worker_id)
    # If result is empty, saved_result will be "(No result generated)"
    if result.result:
        assert saved_result == result.result
    else:
        assert saved_result == "(No result generated)"


@pytest.mark.asyncio
async def test_worker_config_persistence(worker_runner, temp_store, db_session, test_user):
    """Test that worker config is persisted in metadata."""
    from zerg.crud import crud

    agent = crud.create_agent(
        db=db_session,
        owner_id=test_user.id,
        name="Test Agent",
        model="gpt-4o-mini",
        system_instructions="You are a helpful assistant.",
        task_instructions="",
        
    )
    db_session.commit()
    db_session.refresh(agent)

    config = {
        "model": "gpt-4o-mini",
        "timeout": 300,
        "custom_param": "test_value",
    }

    result = await worker_runner.run_worker(
        db=db_session,
        task="Test task",
        agent=agent,
        agent_config=config,
    )

    # Verify config in metadata
    metadata = temp_store.get_worker_metadata(result.worker_id)
    assert metadata["config"]["model"] == "gpt-4o-mini"
    assert metadata["config"]["timeout"] == 300
    assert metadata["config"]["custom_param"] == "test_value"


@pytest.mark.asyncio
async def test_worker_artifacts_readable(worker_runner, temp_store, db_session, test_user):
    """Test that all worker artifacts are readable after completion."""
    from zerg.crud import crud

    agent = crud.create_agent(
        db=db_session,
        owner_id=test_user.id,
        name="Test Agent",
        model="gpt-4o-mini",
        system_instructions="You are a helpful assistant.",
        task_instructions="",
        
    )
    db_session.commit()
    db_session.refresh(agent)

    result = await worker_runner.run_worker(
        db=db_session,
        task="Explain what 2+2 equals",
        agent=agent,
    )

    # Test reading various artifacts
    worker_id = result.worker_id

    # Read metadata
    metadata = temp_store.get_worker_metadata(worker_id)
    assert metadata["worker_id"] == worker_id

    # Read result
    saved_result = temp_store.get_worker_result(worker_id)
    assert saved_result is not None

    # Read thread messages
    thread_content = temp_store.read_worker_file(worker_id, "thread.jsonl")
    assert len(thread_content) > 0

    # List should include this worker
    workers = temp_store.list_workers(limit=10)
    worker_ids = [w["worker_id"] for w in workers]
    assert worker_id in worker_ids
