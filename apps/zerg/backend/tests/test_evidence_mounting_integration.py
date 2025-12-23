"""Integration tests for evidence mounting system (Phase 2 of Mount → Reason → Prune).

These tests verify the end-to-end flow:
1. spawn_worker returns compact payload with evidence marker
2. EvidenceMountingLLM expands marker before LLM call
3. Expanded evidence is NOT persisted to thread_messages
"""

import tempfile
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from langchain_core.messages import ToolMessage

from zerg.services.evidence_mounting_llm import EVIDENCE_MARKER_PATTERN
from zerg.services.evidence_mounting_llm import EvidenceMountingLLM
from zerg.services.roundabout_monitor import RoundaboutResult
from zerg.services.roundabout_monitor import ToolIndexEntry
from zerg.services.roundabout_monitor import format_roundabout_result
from zerg.services.worker_artifact_store import WorkerArtifactStore


@pytest.fixture
def temp_artifact_path(monkeypatch):
    """Create temporary artifact store path and set environment variable."""
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setenv("SWARMLET_DATA_PATH", tmpdir)
        yield tmpdir


class TestSpawnWorkerReturnFormat:
    """Test that spawn_worker returns compact payload with evidence marker."""

    def test_format_includes_tool_index(self):
        """Test that formatted result includes tool index."""
        result = RoundaboutResult(
            status="complete",
            job_id=123,
            worker_id="test-worker-123",
            duration_seconds=10.5,
            summary="Worker completed task",
            tool_index=[
                ToolIndexEntry(sequence=1, tool_name="ssh_exec", exit_code=0, duration_ms=234, output_bytes=1847, failed=False),
                ToolIndexEntry(sequence=2, tool_name="ssh_exec", exit_code=1, duration_ms=156, output_bytes=523, failed=True),
            ],
            run_id=48,
        )

        formatted = format_roundabout_result(result)

        # Should include tool index
        assert "Tool Index:" in formatted
        assert "1. ssh_exec [exit=0, 234ms, 1847B]" in formatted
        assert "2. ssh_exec [FAILED, 156ms, 523B]" in formatted

    def test_format_includes_evidence_marker(self):
        """Test that formatted result includes evidence marker."""
        result = RoundaboutResult(
            status="complete",
            job_id=123,
            worker_id="test-worker-123",
            duration_seconds=10.5,
            summary="Worker completed",
            run_id=48,
        )

        formatted = format_roundabout_result(result)

        # Should include evidence marker
        assert "[EVIDENCE:run_id=48,job_id=123,worker_id=test-worker-123]" in formatted

        # Verify marker is parseable
        match = EVIDENCE_MARKER_PATTERN.search(formatted)
        assert match is not None
        assert match.group(1) == "48"  # run_id
        assert match.group(2) == "123"  # job_id
        assert match.group(3) == "test-worker-123"  # worker_id

    def test_format_without_run_id_no_marker(self):
        """Test that formatted result omits marker when run_id is None."""
        result = RoundaboutResult(
            status="complete",
            job_id=123,
            worker_id="test-worker-123",
            duration_seconds=10.5,
            summary="Worker completed",
            run_id=None,  # No supervisor context
        )

        formatted = format_roundabout_result(result)

        # Should NOT include evidence marker
        assert "[EVIDENCE:" not in formatted


class TestEvidenceMountingIntegration:
    """Test integration between EvidenceCompiler and EvidenceMountingLLM."""

    @pytest.mark.asyncio
    async def test_evidence_expansion_with_mock_compiler(self):
        """Test that evidence markers trigger expansion via EvidenceCompiler."""
        from unittest.mock import AsyncMock
        from unittest.mock import MagicMock

        # Create LLM wrapper with mocked compiler
        mock_base_llm = AsyncMock()
        mock_base_llm.ainvoke = AsyncMock(return_value="Test response")

        mock_db = MagicMock()

        wrapper = EvidenceMountingLLM(
            base_llm=mock_base_llm,
            run_id=48,
            owner_id=100,
            db=mock_db,
        )

        # Mock the compiler's compile method to return test evidence
        with patch.object(wrapper.compiler, "compile") as mock_compile:
            mock_compile.return_value = {
                123: "--- Evidence for Worker 123 ---\nTool 1: ssh_exec [exit=0]\nTool 2: http_request [ok]\n--- End Evidence ---"
            }

            # Create messages with evidence marker
            messages = [
                ToolMessage(
                    content="Worker completed.\n[EVIDENCE:run_id=48,job_id=123,worker_id=test-worker]",
                    tool_call_id="tc1",
                    name="spawn_worker",
                ),
            ]

            # Call LLM (should expand evidence)
            await wrapper.ainvoke(messages)

            # Verify compiler was called with correct parameters
            mock_compile.assert_called_once_with(run_id=48, owner_id=100, db=mock_db)

            # Verify base LLM was called with expanded evidence
            mock_base_llm.ainvoke.assert_called_once()
            call_args = mock_base_llm.ainvoke.call_args[0][0]

            # Check that evidence was expanded
            expanded_msg = call_args[0]
            assert isinstance(expanded_msg, ToolMessage)
            assert "[EVIDENCE:run_id=48,job_id=123,worker_id=test-worker]" in expanded_msg.content
            assert "--- Evidence for Worker 123 ---" in expanded_msg.content
            assert "Tool 1: ssh_exec" in expanded_msg.content

    @pytest.mark.asyncio
    async def test_no_expansion_without_context(self):
        """Test that evidence mounting is skipped when no context is available."""
        from unittest.mock import AsyncMock

        mock_base_llm = AsyncMock()
        mock_base_llm.ainvoke = AsyncMock(return_value="Test response")

        # Create wrapper WITHOUT context
        wrapper = EvidenceMountingLLM(base_llm=mock_base_llm)

        messages = [
            ToolMessage(
                content="Worker completed.\n[EVIDENCE:run_id=48,job_id=123,worker_id=test-worker]",
                tool_call_id="tc1",
                name="spawn_worker",
            ),
        ]

        # Call LLM (should NOT expand evidence)
        await wrapper.ainvoke(messages)

        # Verify base LLM was called with original messages (no expansion)
        mock_base_llm.ainvoke.assert_called_once()
        call_args = mock_base_llm.ainvoke.call_args[0][0]

        original_msg = call_args[0]
        assert original_msg.content == "Worker completed.\n[EVIDENCE:run_id=48,job_id=123,worker_id=test-worker]"
        assert "--- Evidence for Worker" not in original_msg.content


class TestEvidencePersistence:
    """Test that expanded evidence is NOT persisted to thread_messages."""

    @pytest.mark.asyncio
    async def test_only_compact_payload_persisted(self):
        """Test that thread_messages only contains compact payload, not expanded evidence.

        This is a critical invariant: the evidence marker is persisted, but the
        expanded evidence (which can be 32KB+) is NOT saved to the database.
        """
        from unittest.mock import AsyncMock
        from unittest.mock import MagicMock

        # Create wrapper with mocked compiler
        mock_db = MagicMock()
        mock_base_llm = AsyncMock()
        mock_base_llm.ainvoke = AsyncMock(return_value="Test response")

        wrapper = EvidenceMountingLLM(base_llm=mock_base_llm, run_id=48, owner_id=100, db=mock_db)

        # Create compact message (what gets persisted)
        compact_message = ToolMessage(
            content="Worker completed.\n[EVIDENCE:run_id=48,job_id=123,worker_id=test-worker]",
            tool_call_id="tc1",
            name="spawn_worker",
        )

        # Simulate persistence check
        original_content = compact_message.content
        original_size = len(original_content)

        # Mock compiler to return large evidence
        with patch.object(wrapper.compiler, "compile") as mock_compile:
            large_evidence = "--- Evidence for Worker 123 ---\n" + ("Tool output line\n" * 1000) + "--- End Evidence ---"
            mock_compile.return_value = {123: large_evidence}

            # Call LLM (expands evidence internally)
            await wrapper.ainvoke([compact_message])

            # Verify original message is unchanged (evidence expanded only in-flight)
            assert compact_message.content == original_content
            assert len(compact_message.content) == original_size

            # Verify expansion happened (by checking LLM received expanded content)
            call_args = mock_base_llm.ainvoke.call_args[0][0]
            expanded_msg = call_args[0]
            expanded_size = len(expanded_msg.content)

            # Expanded content should be MUCH larger than compact
            assert expanded_size > original_size * 2
            assert "--- Evidence for Worker" in expanded_msg.content

            # But original message (what would be persisted) is unchanged
            assert len(compact_message.content) < 500  # Still compact
