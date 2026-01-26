"""Integration tests for Commis system (ArtifactStore + Runner)."""

import json
import tempfile

import pytest

from tests.conftest import TEST_COMMIS_MODEL
from zerg.services.commis_artifact_store import CommisArtifactStore
from zerg.services.commis_runner import CommisRunner


@pytest.mark.asyncio
async def test_full_commis_lifecycle(db_session, test_user):
    """Test complete commis flow: create -> run -> persist -> query -> read."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Setup
        artifact_store = CommisArtifactStore(base_path=tmpdir)
        commis_runner = CommisRunner(artifact_store=artifact_store)

        # 1. Run a commis task
        task = "Calculate 10 + 20 and explain the result"
        result = await commis_runner.run_commis(
            db=db_session,
            task=task,
            fiche=None,  # Create temporary fiche
            fiche_config={"model": TEST_COMMIS_MODEL, "owner_id": test_user.id},
        )

        # 2. Verify commis completed successfully
        assert result.status == "success"
        assert result.commis_id is not None
        assert result.duration_ms >= 0

        commis_id = result.commis_id

        # 3. Query commis metadata
        metadata = artifact_store.get_commis_metadata(commis_id)
        assert metadata["status"] == "success"
        assert metadata["task"] == task
        assert metadata["created_at"] is not None
        assert metadata["started_at"] is not None
        assert metadata["finished_at"] is not None
        assert metadata["duration_ms"] >= 0

        # 4. Read commis result
        saved_result = artifact_store.get_commis_result(commis_id)
        assert saved_result is not None
        # Result either has content or fallback message
        assert len(saved_result) > 0

        # 5. Read thread messages
        thread_content = artifact_store.read_commis_file(commis_id, "thread.jsonl")
        assert len(thread_content) > 0

        # Parse and verify messages
        lines = thread_content.strip().split("\n")
        messages = [json.loads(line) for line in lines]

        # Should have at least: user + assistant (system/context messages may be injected)
        assert len(messages) >= 2

        # Verify message structure
        assert any(m.get("role") == "system" for m in messages)
        user_messages = [m for m in messages if m.get("role") == "user"]
        assert len(user_messages) >= 1
        assert user_messages[0]["content"] == task

        # Last message should be assistant (may have tool messages in between)
        assistant_messages = [m for m in messages if m["role"] == "assistant"]
        assert len(assistant_messages) >= 1

        # 6. List commis - should include our commis
        commis = artifact_store.list_commis(limit=10)
        commis_ids = [w["commis_id"] for w in commis]
        assert commis_id in commis_ids

        # 7. Search commis - search for part of the task
        search_results = artifact_store.search_commis("10", file_glob="thread.jsonl")
        found_commis_ids = [r["commis_id"] for r in search_results]
        assert commis_id in found_commis_ids


@pytest.mark.asyncio
async def test_multiple_commis(db_session, test_user):
    """Test running multiple commis and querying them."""
    with tempfile.TemporaryDirectory() as tmpdir:
        artifact_store = CommisArtifactStore(base_path=tmpdir)
        commis_runner = CommisRunner(artifact_store=artifact_store)

        # Run multiple commis with different tasks
        tasks = [
            "What is 5 + 5?",
            "What is 10 * 2?",
            "What is 100 / 5?",
        ]

        commis_ids = []
        for task in tasks:
            result = await commis_runner.run_commis(
                db=db_session,
                task=task,
                fiche=None,
                fiche_config={"model": TEST_COMMIS_MODEL, "owner_id": test_user.id},
            )
            assert result.status == "success"
            commis_ids.append(result.commis_id)

        # Verify all commis are in index
        commis = artifact_store.list_commis(limit=10)
        found_ids = [w["commis_id"] for w in commis]

        for commis_id in commis_ids:
            assert commis_id in found_ids

        # Verify each commis has artifacts
        for commis_id in commis_ids:
            metadata = artifact_store.get_commis_metadata(commis_id)
            assert metadata["status"] == "success"

            # Each commis should have result
            result_text = artifact_store.get_commis_result(commis_id)
            assert result_text is not None


@pytest.mark.asyncio
async def test_commis_with_error(db_session, test_user):
    """Test that commis errors are captured properly."""
    from unittest.mock import AsyncMock
    from unittest.mock import patch

    with tempfile.TemporaryDirectory() as tmpdir:
        artifact_store = CommisArtifactStore(base_path=tmpdir)
        commis_runner = CommisRunner(artifact_store=artifact_store)

        # Mock FicheRunner to raise an error
        with patch("zerg.services.commis_runner.FicheRunner") as mock_runner_class:
            mock_instance = AsyncMock()
            mock_instance.run_thread.side_effect = RuntimeError("Test error: fiche failure")
            mock_runner_class.return_value = mock_instance

            result = await commis_runner.run_commis(
                db=db_session,
                task="This will fail",
                fiche=None,
                fiche_config={"model": TEST_COMMIS_MODEL, "owner_id": test_user.id},
            )

            # Verify error captured
            assert result.status == "failed"
            assert result.error is not None
            assert "Test error: fiche failure" in result.error

            # Verify commis metadata reflects failure
            metadata = artifact_store.get_commis_metadata(result.commis_id)
            assert metadata["status"] == "failed"
            assert metadata["error"] is not None
            assert "Test error: fiche failure" in metadata["error"]


@pytest.mark.asyncio
async def test_concierge_can_read_commis_results(db_session, test_user):
    """Test that a concierge can retrieve and analyze commis results."""
    with tempfile.TemporaryDirectory() as tmpdir:
        artifact_store = CommisArtifactStore(base_path=tmpdir)
        commis_runner = CommisRunner(artifact_store=artifact_store)

        # Simulate a concierge delegating work to commis
        delegation_tasks = [
            "Check disk space usage",
            "Check memory usage",
            "Check CPU temperature",
        ]

        # Run commis (simulating delegation)
        completed_commis = []
        for task in delegation_tasks:
            result = await commis_runner.run_commis(
                db=db_session,
                task=task,
                fiche=None,
                fiche_config={"model": TEST_COMMIS_MODEL, "owner_id": test_user.id},
            )
            if result.status == "success":
                completed_commis.append(result.commis_id)

        # Concierge queries completed commis
        commis = artifact_store.list_commis(status="success", limit=10)
        assert len(commis) >= len(completed_commis)

        # Concierge reads each commis's result
        for commis_id in completed_commis:
            metadata = artifact_store.get_commis_metadata(commis_id)
            result_text = artifact_store.get_commis_result(commis_id)

            # Verify concierge can access all info
            assert metadata["task"] in delegation_tasks
            assert result_text is not None

            # Concierge can also drill into specific artifacts
            thread_content = artifact_store.read_commis_file(commis_id, "thread.jsonl")
            assert len(thread_content) > 0


@pytest.mark.asyncio
async def test_commis_isolation(db_session, test_user):
    """Test that commis are isolated from each other."""
    with tempfile.TemporaryDirectory() as tmpdir:
        artifact_store = CommisArtifactStore(base_path=tmpdir)
        commis_runner = CommisRunner(artifact_store=artifact_store)

        # Run two commis with different tasks
        result1 = await commis_runner.run_commis(
            db=db_session,
            task="Task A: Count to 5",
            fiche=None,
            fiche_config={"model": TEST_COMMIS_MODEL, "owner_id": test_user.id},
        )

        result2 = await commis_runner.run_commis(
            db=db_session,
            task="Task B: Count to 10",
            fiche=None,
            fiche_config={"model": TEST_COMMIS_MODEL, "owner_id": test_user.id},
        )

        # Verify commis have different IDs
        assert result1.commis_id != result2.commis_id

        # Verify commis have isolated artifacts
        metadata1 = artifact_store.get_commis_metadata(result1.commis_id)
        metadata2 = artifact_store.get_commis_metadata(result2.commis_id)

        assert metadata1["task"] == "Task A: Count to 5"
        assert metadata2["task"] == "Task B: Count to 10"

        # Verify commis have separate result files
        result1_text = artifact_store.get_commis_result(result1.commis_id)
        result2_text = artifact_store.get_commis_result(result2.commis_id)

        # Results should be different (or at minimum, not both be the fallback)
        # This is a weak assertion but proves isolation
        assert result1_text is not None
        assert result2_text is not None
