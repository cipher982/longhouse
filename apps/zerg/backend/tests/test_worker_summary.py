"""Tests for worker summary extraction (Phase 2.5).

Summary extraction enables supervisors to scan 50+ workers without context overflow.
- result.txt is canonical (source of truth)
- summary is derived, compressed, safe to fail
- status is system-determined (not from LLM)
"""

import tempfile
from datetime import datetime
from datetime import timezone
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from zerg.models_config import DEFAULT_WORKER_MODEL_ID
from zerg.services.worker_artifact_store import WorkerArtifactStore
from zerg.services.worker_runner import WorkerRunner


@pytest.fixture
def temp_store():
    """Create a temporary artifact store for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield WorkerArtifactStore(base_path=tmpdir)


@pytest.fixture
def worker_runner(temp_store):
    """Create a WorkerRunner with temp artifact store."""
    return WorkerRunner(artifact_store=temp_store)


class TestUpdateSummary:
    """Tests for WorkerArtifactStore.update_summary()"""

    def test_update_summary_success(self, temp_store):
        """Summary is saved to metadata and index."""
        # Create a worker
        worker_id = temp_store.create_worker("Test task")
        temp_store.start_worker(worker_id)
        temp_store.save_result(worker_id, "Full result text here")
        temp_store.complete_worker(worker_id, status="success")

        # Update with summary
        summary = "Completed task successfully, 3 items processed"
        summary_meta = {
            "version": 1,
            "model": DEFAULT_WORKER_MODEL_ID,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        temp_store.update_summary(worker_id, summary, summary_meta)

        # Verify metadata has summary
        metadata = temp_store.get_worker_metadata(worker_id)
        assert metadata["summary"] == summary
        assert metadata["summary_meta"]["version"] == 1
        assert metadata["summary_meta"]["model"] == DEFAULT_WORKER_MODEL_ID

        # Verify index has summary
        index = temp_store._read_index()
        worker_entry = next(e for e in index if e["worker_id"] == worker_id)
        assert worker_entry["summary"] == summary

    def test_update_summary_failure_is_nonfatal(self, temp_store):
        """Summary update failure doesn't crash - just logs warning."""
        # Try to update summary for non-existent worker
        # Should not raise, just log warning
        temp_store.update_summary(
            "nonexistent-worker",
            "summary",
            {"version": 1},
        )
        # If we get here without exception, test passes


class TestExtractSummary:
    """Tests for WorkerRunner._extract_summary()"""

    @pytest.mark.asyncio
    async def test_extract_summary_success(self, worker_runner):
        """Summary extracted via LLM when available."""
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Task completed: 3 files processed"

        with patch("zerg.services.worker_runner.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
            mock_openai.return_value = mock_client

            summary, meta = await worker_runner._extract_summary(
                "Process files in /tmp",
                "Successfully processed 3 files: a.txt, b.txt, c.txt",
            )

        assert summary == "Task completed: 3 files processed"
        assert meta["version"] == 1
        assert meta["model"] == DEFAULT_WORKER_MODEL_ID
        assert "generated_at" in meta
        assert "error" not in meta

    @pytest.mark.asyncio
    async def test_extract_summary_truncates_long_summary(self, worker_runner):
        """Long summaries are truncated to 150 chars."""
        # Create a summary longer than 150 chars
        long_summary = "A" * 200

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = long_summary

        with patch("zerg.services.worker_runner.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
            mock_openai.return_value = mock_client

            summary, meta = await worker_runner._extract_summary("Task", "Result")

        assert len(summary) <= 150
        assert summary.endswith("...")

    @pytest.mark.asyncio
    async def test_extract_summary_fallback_on_error(self, worker_runner):
        """Falls back to truncation when LLM fails."""
        with patch("zerg.services.worker_runner.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_client.chat.completions.create = AsyncMock(side_effect=Exception("API rate limit exceeded"))
            mock_openai.return_value = mock_client

            result_text = "Full result with lots of details about what happened"
            summary, meta = await worker_runner._extract_summary("Task", result_text)

        # Should fallback to truncation
        assert meta["model"] == "truncation-fallback"
        assert "error" in meta
        assert "API rate limit" in meta["error"]
        # Summary should be start of result
        assert summary == result_text  # Short enough, no truncation needed

    @pytest.mark.asyncio
    async def test_extract_summary_fallback_truncates_long_result(self, worker_runner):
        """Fallback truncates long results to 150 chars."""
        long_result = "X" * 300

        with patch("zerg.services.worker_runner.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_client.chat.completions.create = AsyncMock(side_effect=Exception("Timeout"))
            mock_openai.return_value = mock_client

            summary, meta = await worker_runner._extract_summary("Task", long_result)

        assert len(summary) <= 150
        assert summary.endswith("...")
        assert meta["model"] == "truncation-fallback"

    @pytest.mark.asyncio
    async def test_extract_summary_timeout(self, worker_runner):
        """Timeout triggers fallback."""
        import asyncio

        async def slow_api(*args, **kwargs):
            await asyncio.sleep(10)  # Longer than 5s timeout
            return MagicMock()

        with patch("zerg.services.worker_runner.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_client.chat.completions.create = slow_api
            mock_openai.return_value = mock_client

            summary, meta = await worker_runner._extract_summary("Task", "Result")

        # Should timeout and fallback
        assert meta["model"] == "truncation-fallback"
        assert "error" in meta


class TestWorkerResultWithSummary:
    """Tests for WorkerResult dataclass with summary field."""

    def test_worker_result_has_summary_field(self):
        """WorkerResult includes summary field."""
        from zerg.services.worker_runner import WorkerResult

        result = WorkerResult(
            worker_id="test-123",
            status="success",
            result="Full result text",
            summary="Compressed summary",
            duration_ms=100,
        )

        assert result.summary == "Compressed summary"

    def test_worker_result_summary_default_empty(self):
        """Summary defaults to empty string."""
        from zerg.services.worker_runner import WorkerResult

        result = WorkerResult(
            worker_id="test-123",
            status="success",
            result="Full result text",
        )

        assert result.summary == ""


class TestListWorkersWithSummaries:
    """Tests for list_commis returning summaries."""

    def test_list_commis_index_has_summary(self, temp_store):
        """Workers in index include summary after update."""
        # Create and complete a worker
        worker_id = temp_store.create_worker("Check disk space")
        temp_store.start_worker(worker_id)
        temp_store.save_result(worker_id, "Disk usage: 45% of 500GB")
        temp_store.complete_worker(worker_id, status="success")

        # Update summary
        temp_store.update_summary(
            worker_id,
            "Disk at 45% capacity (225GB used)",
            {"version": 1, "model": DEFAULT_WORKER_MODEL_ID},
        )

        # List workers and verify summary in index
        workers = temp_store.list_commis(limit=10)
        assert len(workers) == 1
        assert workers[0]["summary"] == "Disk at 45% capacity (225GB used)"

    def test_list_commis_without_summary_fallback(self, temp_store):
        """Workers without summary use task in index (no summary field)."""
        # Create worker without summary
        worker_id = temp_store.create_worker("Old worker without summary")
        temp_store.start_worker(worker_id)
        temp_store.complete_worker(worker_id, status="success")

        workers = temp_store.list_commis(limit=10)
        assert len(workers) == 1
        # No summary field in index entry
        assert "summary" not in workers[0] or workers[0].get("summary") is None
