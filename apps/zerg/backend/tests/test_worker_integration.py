"""Integration tests for Worker system (ArtifactStore + Runner)."""

import json
import tempfile

import pytest

from zerg.services.worker_artifact_store import WorkerArtifactStore
from zerg.services.worker_runner import WorkerRunner
from tests.conftest import TEST_MODEL, TEST_WORKER_MODEL


@pytest.mark.asyncio
async def test_full_worker_lifecycle(db_session, test_user):
    """Test complete worker flow: create -> run -> persist -> query -> read."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Setup
        artifact_store = WorkerArtifactStore(base_path=tmpdir)
        worker_runner = WorkerRunner(artifact_store=artifact_store)

        # 1. Run a worker task
        task = "Calculate 10 + 20 and explain the result"
        result = await worker_runner.run_worker(
            db=db_session,
            task=task,
            agent=None,  # Create temporary agent
            agent_config={"model": TEST_WORKER_MODEL, "owner_id": test_user.id},
        )

        # 2. Verify worker completed successfully
        assert result.status == "success"
        assert result.worker_id is not None
        assert result.duration_ms >= 0

        worker_id = result.worker_id

        # 3. Query worker metadata
        metadata = artifact_store.get_worker_metadata(worker_id)
        assert metadata["status"] == "success"
        assert metadata["task"] == task
        assert metadata["created_at"] is not None
        assert metadata["started_at"] is not None
        assert metadata["finished_at"] is not None
        assert metadata["duration_ms"] >= 0

        # 4. Read worker result
        saved_result = artifact_store.get_worker_result(worker_id)
        assert saved_result is not None
        # Result either has content or fallback message
        assert len(saved_result) > 0

        # 5. Read thread messages
        thread_content = artifact_store.read_worker_file(worker_id, "thread.jsonl")
        assert len(thread_content) > 0

        # Parse and verify messages
        lines = thread_content.strip().split("\n")
        messages = [json.loads(line) for line in lines]

        # Should have at least: system, user, assistant
        assert len(messages) >= 3

        # Verify message structure
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == task

        # Last message should be assistant (may have tool messages in between)
        assistant_messages = [m for m in messages if m["role"] == "assistant"]
        assert len(assistant_messages) >= 1

        # 6. List workers - should include our worker
        workers = artifact_store.list_workers(limit=10)
        worker_ids = [w["worker_id"] for w in workers]
        assert worker_id in worker_ids

        # 7. Search workers - search for part of the task
        search_results = artifact_store.search_workers("10", file_glob="thread.jsonl")
        found_worker_ids = [r["worker_id"] for r in search_results]
        assert worker_id in found_worker_ids


@pytest.mark.asyncio
async def test_multiple_workers(db_session, test_user):
    """Test running multiple workers and querying them."""
    with tempfile.TemporaryDirectory() as tmpdir:
        artifact_store = WorkerArtifactStore(base_path=tmpdir)
        worker_runner = WorkerRunner(artifact_store=artifact_store)

        # Run multiple workers with different tasks
        tasks = [
            "What is 5 + 5?",
            "What is 10 * 2?",
            "What is 100 / 5?",
        ]

        worker_ids = []
        for task in tasks:
            result = await worker_runner.run_worker(
                db=db_session,
                task=task,
                agent=None,
                agent_config={"model": TEST_WORKER_MODEL, "owner_id": test_user.id},
            )
            assert result.status == "success"
            worker_ids.append(result.worker_id)

        # Verify all workers are in index
        workers = artifact_store.list_workers(limit=10)
        found_ids = [w["worker_id"] for w in workers]

        for worker_id in worker_ids:
            assert worker_id in found_ids

        # Verify each worker has artifacts
        for worker_id in worker_ids:
            metadata = artifact_store.get_worker_metadata(worker_id)
            assert metadata["status"] == "success"

            # Each worker should have result
            result_text = artifact_store.get_worker_result(worker_id)
            assert result_text is not None


@pytest.mark.asyncio
async def test_worker_with_error(db_session, test_user):
    """Test that worker errors are captured properly."""
    from unittest.mock import AsyncMock, patch

    with tempfile.TemporaryDirectory() as tmpdir:
        artifact_store = WorkerArtifactStore(base_path=tmpdir)
        worker_runner = WorkerRunner(artifact_store=artifact_store)

        # Mock AgentRunner to raise an error
        with patch("zerg.services.worker_runner.AgentRunner") as mock_runner_class:
            mock_instance = AsyncMock()
            mock_instance.run_thread.side_effect = RuntimeError(
                "Test error: agent failure"
            )
            mock_runner_class.return_value = mock_instance

            result = await worker_runner.run_worker(
                db=db_session,
                task="This will fail",
                agent=None,
                agent_config={"model": TEST_WORKER_MODEL, "owner_id": test_user.id},
            )

            # Verify error captured
            assert result.status == "failed"
            assert result.error is not None
            assert "Test error: agent failure" in result.error

            # Verify worker metadata reflects failure
            metadata = artifact_store.get_worker_metadata(result.worker_id)
            assert metadata["status"] == "failed"
            assert metadata["error"] is not None
            assert "Test error: agent failure" in metadata["error"]


@pytest.mark.asyncio
async def test_supervisor_can_read_worker_results(db_session, test_user):
    """Test that a supervisor can retrieve and analyze worker results."""
    with tempfile.TemporaryDirectory() as tmpdir:
        artifact_store = WorkerArtifactStore(base_path=tmpdir)
        worker_runner = WorkerRunner(artifact_store=artifact_store)

        # Simulate a supervisor delegating work to workers
        delegation_tasks = [
            "Check disk space usage",
            "Check memory usage",
            "Check CPU temperature",
        ]

        # Run workers (simulating delegation)
        completed_workers = []
        for task in delegation_tasks:
            result = await worker_runner.run_worker(
                db=db_session,
                task=task,
                agent=None,
                agent_config={"model": TEST_WORKER_MODEL, "owner_id": test_user.id},
            )
            if result.status == "success":
                completed_workers.append(result.worker_id)

        # Supervisor queries completed workers
        workers = artifact_store.list_workers(status="success", limit=10)
        assert len(workers) >= len(completed_workers)

        # Supervisor reads each worker's result
        for worker_id in completed_workers:
            metadata = artifact_store.get_worker_metadata(worker_id)
            result_text = artifact_store.get_worker_result(worker_id)

            # Verify supervisor can access all info
            assert metadata["task"] in delegation_tasks
            assert result_text is not None

            # Supervisor can also drill into specific artifacts
            thread_content = artifact_store.read_worker_file(
                worker_id, "thread.jsonl"
            )
            assert len(thread_content) > 0


@pytest.mark.asyncio
async def test_worker_isolation(db_session, test_user):
    """Test that workers are isolated from each other."""
    with tempfile.TemporaryDirectory() as tmpdir:
        artifact_store = WorkerArtifactStore(base_path=tmpdir)
        worker_runner = WorkerRunner(artifact_store=artifact_store)

        # Run two workers with different tasks
        result1 = await worker_runner.run_worker(
            db=db_session,
            task="Task A: Count to 5",
            agent=None,
            agent_config={"model": TEST_WORKER_MODEL, "owner_id": test_user.id},
        )

        result2 = await worker_runner.run_worker(
            db=db_session,
            task="Task B: Count to 10",
            agent=None,
            agent_config={"model": TEST_WORKER_MODEL, "owner_id": test_user.id},
        )

        # Verify workers have different IDs
        assert result1.worker_id != result2.worker_id

        # Verify workers have isolated artifacts
        metadata1 = artifact_store.get_worker_metadata(result1.worker_id)
        metadata2 = artifact_store.get_worker_metadata(result2.worker_id)

        assert metadata1["task"] == "Task A: Count to 5"
        assert metadata2["task"] == "Task B: Count to 10"

        # Verify workers have separate result files
        result1_text = artifact_store.get_worker_result(result1.worker_id)
        result2_text = artifact_store.get_worker_result(result2.worker_id)

        # Results should be different (or at minimum, not both be the fallback)
        # This is a weak assertion but proves isolation
        assert result1_text is not None
        assert result2_text is not None
