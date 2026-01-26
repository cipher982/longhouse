"""Tests for CommisRunner service."""

import tempfile

import pytest

from tests.conftest import TEST_COMMIS_MODEL
from zerg.services.commis_artifact_store import CommisArtifactStore
from zerg.services.commis_runner import CommisRunner


@pytest.fixture
def temp_store():
    """Create a temporary artifact store for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield CommisArtifactStore(base_path=tmpdir)


@pytest.fixture
def commis_runner(temp_store):
    """Create a CommisRunner with temp storage."""
    return CommisRunner(artifact_store=temp_store)


@pytest.mark.asyncio
async def test_run_commis_simple_task(commis_runner, temp_store, db_session, test_user):
    """Test running a simple commis task."""
    from zerg.crud import crud

    # Create a test fiche
    fiche = crud.create_fiche(
        db=db_session,
        owner_id=test_user.id,
        name="Test Fiche",
        model=TEST_COMMIS_MODEL,
        system_instructions="You are a helpful assistant.",
        task_instructions="",
    )
    db_session.commit()
    db_session.refresh(fiche)

    # Run commis with simple task
    task = "What is 2+2?"
    result = await commis_runner.run_commis(
        db=db_session,
        task=task,
        fiche=fiche,
    )

    # Verify result structure
    assert result.commis_id is not None
    assert result.status == "success"
    assert result.duration_ms >= 0
    # Result content depends on LLM, so we just check it exists
    assert result.result is not None

    # Verify commis directory created
    commis_dir = temp_store.base_path / result.commis_id
    assert commis_dir.exists()

    # Verify metadata
    metadata = temp_store.get_commis_metadata(result.commis_id)
    assert metadata["status"] == "success"
    assert metadata["task"] == task
    assert metadata["finished_at"] is not None
    assert metadata["duration_ms"] >= 0

    # Verify result.txt exists
    result_path = commis_dir / "result.txt"
    assert result_path.exists()

    # Verify thread.jsonl exists
    thread_path = commis_dir / "thread.jsonl"
    assert thread_path.exists()


@pytest.mark.asyncio
async def test_run_commis_without_agent(commis_runner, temp_store, db_session, test_user):
    """Test running a commis without providing an fiche (creates temporary fiche)."""
    task = "Say hello world"

    result = await commis_runner.run_commis(
        db=db_session,
        task=task,
        fiche=None,  # No fiche provided
        fiche_config={"model": TEST_COMMIS_MODEL},
    )

    # Verify result
    assert result.commis_id is not None
    assert result.status == "success"

    # Verify commis artifacts
    metadata = temp_store.get_commis_metadata(result.commis_id)
    assert metadata["status"] == "success"
    assert metadata["config"]["model"] == TEST_COMMIS_MODEL


@pytest.mark.asyncio
async def test_run_commis_with_tool_calls(commis_runner, temp_store, db_session, test_user):
    """Test that tool calls are captured and persisted."""
    from zerg.crud import crud

    # Create fiche with tools enabled
    fiche = crud.create_fiche(
        db=db_session,
        owner_id=test_user.id,
        name="Test Fiche with Tools",
        model=TEST_COMMIS_MODEL,
        system_instructions="You are a helpful assistant with access to tools.",
        task_instructions="",
    )
    db_session.commit()
    db_session.refresh(fiche)

    # Run commis with task that likely triggers tools
    # Note: This depends on LLM behavior and tools available
    task = "Check the current time"

    result = await commis_runner.run_commis(
        db=db_session,
        task=task,
        fiche=fiche,
    )

    # Verify commis completed
    assert result.status == "success"

    # Check if tool_calls directory has files (may be empty if no tools used)
    commis_dir = temp_store.base_path / result.commis_id
    tool_calls_dir = commis_dir / "tool_calls"
    assert tool_calls_dir.exists()

    # Thread should have messages
    thread_path = commis_dir / "thread.jsonl"
    assert thread_path.exists()
    with open(thread_path, "r") as f:
        lines = f.readlines()
        assert len(lines) >= 2  # At least system + user message


@pytest.mark.asyncio
async def test_run_commis_handles_errors(commis_runner, temp_store, db_session, test_user):
    """Test that commis errors are captured properly."""
    from unittest.mock import AsyncMock
    from unittest.mock import patch

    from zerg.crud import crud

    # Create test fiche
    fiche = crud.create_fiche(
        db=db_session,
        owner_id=test_user.id,
        name="Test Fiche",
        model=TEST_COMMIS_MODEL,
        system_instructions="You are a helpful assistant.",
        task_instructions="",
    )
    db_session.commit()
    db_session.refresh(fiche)

    # Mock FicheRunner to raise an error
    with patch("zerg.services.commis_runner.FicheRunner") as mock_runner_class:
        mock_instance = AsyncMock()
        mock_instance.run_thread.side_effect = RuntimeError("Simulated fiche failure")
        mock_runner_class.return_value = mock_instance

        result = await commis_runner.run_commis(
            db=db_session,
            task="This should fail",
            fiche=fiche,
        )

        # Verify error captured
        assert result.status == "failed"
        assert result.error is not None
        assert "Simulated fiche failure" in result.error

        # Verify commis metadata reflects failure
        metadata = temp_store.get_commis_metadata(result.commis_id)
        assert metadata["status"] == "failed"
        assert metadata["error"] is not None


@pytest.mark.asyncio
async def test_commis_message_persistence(commis_runner, temp_store, db_session, test_user):
    """Test that all messages are persisted to thread.jsonl."""
    from zerg.crud import crud

    fiche = crud.create_fiche(
        db=db_session,
        owner_id=test_user.id,
        name="Test Fiche",
        model=TEST_COMMIS_MODEL,
        system_instructions="You are a helpful assistant.",
        task_instructions="",
    )
    db_session.commit()
    db_session.refresh(fiche)

    result = await commis_runner.run_commis(
        db=db_session,
        task="Say hello",
        fiche=fiche,
    )

    # Read thread.jsonl
    commis_dir = temp_store.base_path / result.commis_id
    thread_path = commis_dir / "thread.jsonl"

    import json

    with open(thread_path, "r") as f:
        messages = [json.loads(line) for line in f]

    # Should have at least: user + assistant (system/context messages may be injected)
    assert len(messages) >= 2

    assert any(m.get("role") == "system" for m in messages)
    user_messages = [m for m in messages if m.get("role") == "user"]
    assert len(user_messages) >= 1
    assert user_messages[0]["content"] == "Say hello"

    # Last message should be assistant (may have tool messages in between)
    assistant_messages = [m for m in messages if m["role"] == "assistant"]
    assert len(assistant_messages) >= 1


@pytest.mark.asyncio
async def test_commis_result_extraction(commis_runner, temp_store, db_session, test_user):
    """Test that final result is correctly extracted from assistant messages."""
    from zerg.crud import crud

    fiche = crud.create_fiche(
        db=db_session,
        owner_id=test_user.id,
        name="Test Fiche",
        model=TEST_COMMIS_MODEL,
        system_instructions="You are a helpful assistant. Always end your response with 'DONE'.",
        task_instructions="",
    )
    db_session.commit()
    db_session.refresh(fiche)

    result = await commis_runner.run_commis(
        db=db_session,
        task="Count to three",
        fiche=fiche,
    )

    # Verify result extracted
    assert result.status == "success"
    assert result.result is not None
    # Result may be empty if LLM doesn't return content (just has tool calls)
    # The important thing is that it doesn't fail

    # Verify result saved to file
    saved_result = temp_store.get_commis_result(result.commis_id)
    # If result is empty, saved_result will be "(No result generated)"
    if result.result:
        assert saved_result == result.result
    else:
        assert saved_result == "(No result generated)"


@pytest.mark.asyncio
async def test_commis_config_persistence(commis_runner, temp_store, db_session, test_user):
    """Test that commis config is persisted in metadata."""
    from zerg.crud import crud

    fiche = crud.create_fiche(
        db=db_session,
        owner_id=test_user.id,
        name="Test Fiche",
        model=TEST_COMMIS_MODEL,
        system_instructions="You are a helpful assistant.",
        task_instructions="",
    )
    db_session.commit()
    db_session.refresh(fiche)

    config = {
        "model": TEST_COMMIS_MODEL,
        "timeout": 300,
        "custom_param": "test_value",
    }

    result = await commis_runner.run_commis(
        db=db_session,
        task="Test task",
        fiche=fiche,
        fiche_config=config,
    )

    # Verify config in metadata
    metadata = temp_store.get_commis_metadata(result.commis_id)
    assert metadata["config"]["model"] == TEST_COMMIS_MODEL
    assert metadata["config"]["timeout"] == 300
    assert metadata["config"]["custom_param"] == "test_value"


@pytest.mark.asyncio
async def test_commis_artifacts_readable(commis_runner, temp_store, db_session, test_user):
    """Test that all commis artifacts are readable after completion."""
    from zerg.crud import crud

    fiche = crud.create_fiche(
        db=db_session,
        owner_id=test_user.id,
        name="Test Fiche",
        model=TEST_COMMIS_MODEL,
        system_instructions="You are a helpful assistant.",
        task_instructions="",
    )
    db_session.commit()
    db_session.refresh(fiche)

    result = await commis_runner.run_commis(
        db=db_session,
        task="Explain what 2+2 equals",
        fiche=fiche,
    )

    # Test reading various artifacts
    commis_id = result.commis_id

    # Read metadata
    metadata = temp_store.get_commis_metadata(commis_id)
    assert metadata["commis_id"] == commis_id

    # Read result
    saved_result = temp_store.get_commis_result(commis_id)
    assert saved_result is not None

    # Read thread messages
    thread_content = temp_store.read_commis_file(commis_id, "thread.jsonl")
    assert len(thread_content) > 0

    # List should include this commis
    commis = temp_store.list_commis(limit=10)
    commis_ids = [w["commis_id"] for w in commis]
    assert commis_id in commis_ids


@pytest.mark.asyncio
async def test_temporary_fiche_has_infrastructure_tools(commis_runner, temp_store, db_session, test_user):
    """Test that temporary fiches created for commis have ssh_exec and other infra tools."""
    from zerg.crud import crud

    # Run commis without providing an fiche (creates temporary fiche)
    task = "Test infrastructure tools"
    result = await commis_runner.run_commis(
        db=db_session,
        task=task,
        fiche=None,
        fiche_config={"model": TEST_COMMIS_MODEL, "owner_id": test_user.id},
    )

    # Commis should complete (temporary fiche is cleaned up)
    assert result.status == "success"

    # Verify metadata captures expected tools in config
    # The temporary fiche gets deleted, but we can verify from the commis's perspective
    metadata = temp_store.get_commis_metadata(result.commis_id)
    assert metadata["status"] == "success"

    # Verify the commis runner gives infrastructure tools to temp fiches
    # by checking the defaults in the code
    from zerg.services.commis_runner import CommisRunner

    # Create a new temporary fiche to inspect its tools
    runner = CommisRunner(artifact_store=temp_store)
    temp_fiche = await runner._create_temporary_fiche(
        db=db_session,
        task="test",
        config={"owner_id": test_user.id, "model": TEST_COMMIS_MODEL},
    )

    try:
        # Verify the fiche has infrastructure tools
        assert "ssh_exec" in temp_fiche.allowed_tools
        assert "http_request" in temp_fiche.allowed_tools
        assert "get_current_time" in temp_fiche.allowed_tools
        # V1.1: knowledge_search should be available to commis
        assert "knowledge_search" in temp_fiche.allowed_tools
        # V1.2: web research tools should be available to commis
        assert "web_search" in temp_fiche.allowed_tools
        assert "web_fetch" in temp_fiche.allowed_tools
    finally:
        # Clean up the test fiche
        crud.delete_fiche(db_session, temp_fiche.id)
        db_session.commit()


class TestSynthesizeFromToolOutputs:
    """Tests for _synthesize_from_tool_outputs fallback method."""

    def test_synthesize_with_tool_outputs(self, temp_store):
        """Test synthesizing result when tool outputs exist but final message is empty."""
        from langchain_core.messages import AIMessage
        from langchain_core.messages import ToolMessage

        runner = CommisRunner(artifact_store=temp_store)
        messages = [
            AIMessage(content="", tool_calls=[{"id": "call_1", "name": "ssh_exec", "args": {}}]),
            ToolMessage(content="disk usage: 50GB used, 100GB total", tool_call_id="call_1", name="ssh_exec"),
            AIMessage(content=""),  # Empty final message
        ]

        result = runner._synthesize_from_tool_outputs(messages, "check disk space")

        assert result is not None
        assert "ssh_exec" in result
        assert "50GB" in result
        assert "Commis completed task but produced no final summary" in result

    def test_synthesize_no_tool_outputs(self, temp_store):
        """Test that synthesis returns None when no tool outputs exist."""
        from langchain_core.messages import AIMessage

        runner = CommisRunner(artifact_store=temp_store)
        messages = [
            AIMessage(content=""),  # Empty message, no tools
        ]

        result = runner._synthesize_from_tool_outputs(messages, "some task")

        assert result is None

    def test_synthesize_limits_to_three_tools(self, temp_store):
        """Test that synthesis limits to 3 most recent tool outputs."""
        from langchain_core.messages import AIMessage
        from langchain_core.messages import ToolMessage

        runner = CommisRunner(artifact_store=temp_store)
        messages = [
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "call_1", "name": "tool1", "args": {}},
                    {"id": "call_2", "name": "tool2", "args": {}},
                    {"id": "call_3", "name": "tool3", "args": {}},
                    {"id": "call_4", "name": "tool4", "args": {}},
                ],
            ),
            ToolMessage(content="output1", tool_call_id="call_1", name="tool1"),
            ToolMessage(content="output2", tool_call_id="call_2", name="tool2"),
            ToolMessage(content="output3", tool_call_id="call_3", name="tool3"),
            ToolMessage(content="output4", tool_call_id="call_4", name="tool4"),
            AIMessage(content=""),
        ]

        result = runner._synthesize_from_tool_outputs(messages, "multi-tool task")

        # Should contain the 3 most recent (tool2, tool3, tool4)
        assert result is not None
        assert "tool2" in result
        assert "tool3" in result
        assert "tool4" in result
        # tool1 should NOT be in result (oldest, excluded)
        assert "tool1" not in result

    def test_synthesize_truncates_long_output(self, temp_store):
        """Test that very long tool outputs are truncated."""
        from langchain_core.messages import AIMessage
        from langchain_core.messages import ToolMessage

        runner = CommisRunner(artifact_store=temp_store)
        long_output = "x" * 3000  # Longer than 2000 char limit
        messages = [
            AIMessage(content="", tool_calls=[{"id": "call_1", "name": "ssh_exec", "args": {}}]),
            ToolMessage(content=long_output, tool_call_id="call_1", name="ssh_exec"),
            AIMessage(content=""),
        ]

        result = runner._synthesize_from_tool_outputs(messages, "long output task")

        assert result is not None
        # Output should be truncated to 2000 chars
        assert len(result) < 3000 + 200  # 200 for header text


class TestTimestampFix:
    """Tests for the timestamp prefix fix in _db_to_langchain."""

    def test_empty_assistant_content_no_timestamp(self, db_session, test_user):
        """Test that empty assistant content doesn't get masked by timestamp."""
        from zerg.crud import crud
        from zerg.services.thread_service import _db_to_langchain

        # Create a thread message with empty content
        fiche = crud.create_fiche(
            db=db_session,
            owner_id=test_user.id,
            name="Test Fiche",
            model="gpt-4o-mini",
            system_instructions="test",
            task_instructions="",
        )
        thread = crud.create_thread(
            db=db_session,
            fiche_id=fiche.id,
            title="Test Thread",
            active=True,
            fiche_state={},
            memory_strategy="buffer",
            thread_type="chat",
        )

        # Create assistant message with empty content
        msg = crud.create_thread_message(
            db=db_session,
            thread_id=thread.id,
            role="assistant",
            content="",  # Empty content
            processed=True,
        )
        db_session.commit()
        db_session.refresh(msg)

        # Convert to LangChain message
        lc_msg = _db_to_langchain(msg)

        # Content should still be empty, NOT just a timestamp
        assert lc_msg.content == ""

    def test_non_empty_assistant_gets_timestamp(self, db_session, test_user):
        """Test that non-empty assistant content does get timestamp."""
        from zerg.crud import crud
        from zerg.services.thread_service import _db_to_langchain

        fiche = crud.create_fiche(
            db=db_session,
            owner_id=test_user.id,
            name="Test Fiche",
            model="gpt-4o-mini",
            system_instructions="test",
            task_instructions="",
        )
        thread = crud.create_thread(
            db=db_session,
            fiche_id=fiche.id,
            title="Test Thread",
            active=True,
            fiche_state={},
            memory_strategy="buffer",
            thread_type="chat",
        )

        # Create assistant message with content
        msg = crud.create_thread_message(
            db=db_session,
            thread_id=thread.id,
            role="assistant",
            content="Hello world",
            processed=True,
        )
        db_session.commit()
        db_session.refresh(msg)

        lc_msg = _db_to_langchain(msg)

        # Content should have timestamp prefix
        assert "Hello world" in lc_msg.content
        # If sent_at is set, should have timestamp
        if msg.sent_at:
            assert lc_msg.content.startswith("[")

    def test_whitespace_only_assistant_no_timestamp(self, db_session, test_user):
        """Test that whitespace-only content doesn't get timestamp."""
        from zerg.crud import crud
        from zerg.services.thread_service import _db_to_langchain

        fiche = crud.create_fiche(
            db=db_session,
            owner_id=test_user.id,
            name="Test Fiche",
            model="gpt-4o-mini",
            system_instructions="test",
            task_instructions="",
        )
        thread = crud.create_thread(
            db=db_session,
            fiche_id=fiche.id,
            title="Test Thread",
            active=True,
            fiche_state={},
            memory_strategy="buffer",
            thread_type="chat",
        )

        # Create assistant message with whitespace only
        msg = crud.create_thread_message(
            db=db_session,
            thread_id=thread.id,
            role="assistant",
            content="   ",  # Whitespace only
            processed=True,
        )
        db_session.commit()
        db_session.refresh(msg)

        lc_msg = _db_to_langchain(msg)

        # Content should still be whitespace, NOT timestamp + whitespace
        assert lc_msg.content == "   "
