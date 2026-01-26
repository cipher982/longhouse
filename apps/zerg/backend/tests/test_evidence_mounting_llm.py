"""Tests for EvidenceMountingLLM wrapper.

Tests the Phase 2 evidence mounting logic that expands markers before LLM calls.
"""

import pytest
from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage
from langchain_core.messages import ToolMessage
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

from zerg.services.evidence_mounting_llm import EVIDENCE_MARKER_PATTERN
from zerg.services.evidence_mounting_llm import EvidenceMountingLLM


@pytest.fixture
def mock_base_llm():
    """Mock base LLM that returns a simple response."""
    llm = AsyncMock()
    llm.ainvoke = AsyncMock(return_value=AIMessage(content="Test response"))
    return llm


@pytest.fixture
def mock_compiler():
    """Mock EvidenceCompiler that returns test evidence."""
    with patch("zerg.services.evidence_mounting_llm.EvidenceCompiler") as MockCompiler:
        compiler = MagicMock()
        compiler.compile.return_value = {
            123: "--- Evidence for Commis 123 ---\nFull tool outputs here\n--- End Evidence ---",
            456: "--- Evidence for Commis 456 ---\nOther commis evidence\n--- End Evidence ---",
        }
        MockCompiler.return_value = compiler
        yield compiler


class TestEvidenceMarkerPattern:
    """Test the evidence marker regex pattern."""

    def test_matches_valid_marker(self):
        """Test pattern matches valid evidence markers."""
        marker = "[EVIDENCE:course_id=48,job_id=123,commis_id=abc-123]"
        match = EVIDENCE_MARKER_PATTERN.search(marker)

        assert match is not None
        assert match.group(1) == "48"  # course_id
        assert match.group(2) == "123"  # job_id
        assert match.group(3) == "abc-123"  # commis_id

    def test_matches_in_larger_text(self):
        """Test pattern finds marker within larger text."""
        text = "Commis completed.\n[EVIDENCE:course_id=10,job_id=99,commis_id=xyz-456]\nSummary: Done."
        match = EVIDENCE_MARKER_PATTERN.search(text)

        assert match is not None
        assert match.group(1) == "10"
        assert match.group(2) == "99"
        assert match.group(3) == "xyz-456"

    def test_no_match_invalid_format(self):
        """Test pattern doesn't match invalid formats."""
        invalid_markers = [
            "[EVIDENCE:invalid]",
            "[EVIDENCE:course_id=48]",  # Missing fields
            "[EVIDENCE:job_id=123,commis_id=abc]",  # Missing course_id
            "EVIDENCE:course_id=48,job_id=123,commis_id=abc",  # Missing brackets
        ]

        for marker in invalid_markers:
            match = EVIDENCE_MARKER_PATTERN.search(marker)
            assert match is None, f"Should not match: {marker}"


class TestEvidenceMountingLLM:
    """Test EvidenceMountingLLM wrapper."""

    @pytest.mark.asyncio
    async def test_passthrough_without_context(self, mock_base_llm):
        """Test LLM passes through when no context is set."""
        wrapper = EvidenceMountingLLM(base_llm=mock_base_llm)

        messages = [
            HumanMessage(content="Test message"),
            ToolMessage(content="[EVIDENCE:course_id=1,job_id=2,commis_id=abc]", tool_call_id="123", name="spawn_commis"),
        ]

        result = await wrapper.ainvoke(messages)

        # Should call base LLM with original messages (no expansion)
        assert result.content == "Test response"
        mock_base_llm.ainvoke.assert_called_once()
        call_args = mock_base_llm.ainvoke.call_args[0][0]
        assert len(call_args) == 2
        assert call_args[1].content == "[EVIDENCE:course_id=1,job_id=2,commis_id=abc]"  # Unchanged

    @pytest.mark.asyncio
    async def test_passthrough_without_markers(self, mock_base_llm, mock_compiler):
        """Test LLM passes through when no evidence markers present."""
        mock_db = MagicMock()
        wrapper = EvidenceMountingLLM(base_llm=mock_base_llm, course_id=1, owner_id=100, db=mock_db)

        messages = [
            HumanMessage(content="Test message"),
            ToolMessage(content="Commis completed successfully", tool_call_id="123", name="spawn_commis"),
        ]

        result = await wrapper.ainvoke(messages)

        # Should call base LLM with original messages (no markers to expand)
        assert result.content == "Test response"
        mock_base_llm.ainvoke.assert_called_once()
        call_args = mock_base_llm.ainvoke.call_args[0][0]
        assert len(call_args) == 2
        assert call_args[1].content == "Commis completed successfully"  # Unchanged

        # Compiler should not be called (no markers)
        mock_compiler.compile.assert_not_called()

    @pytest.mark.asyncio
    async def test_expands_single_marker(self, mock_base_llm, mock_compiler):
        """Test expansion of a single evidence marker."""
        mock_db = MagicMock()
        wrapper = EvidenceMountingLLM(base_llm=mock_base_llm, course_id=48, owner_id=100, db=mock_db)

        messages = [
            HumanMessage(content="Check the server"),
            ToolMessage(
                content="Commis completed.\n[EVIDENCE:course_id=48,job_id=123,commis_id=abc-123]",
                tool_call_id="tc1",
                name="spawn_commis",
            ),
        ]

        result = await wrapper.ainvoke(messages)

        # Compiler should be called once
        mock_compiler.compile.assert_called_once_with(course_id=48, owner_id=100, db=mock_db)

        # Should call base LLM with expanded messages
        assert result.content == "Test response"
        mock_base_llm.ainvoke.assert_called_once()
        call_args = mock_base_llm.ainvoke.call_args[0][0]

        # Second message should have evidence expanded
        expanded_msg = call_args[1]
        assert isinstance(expanded_msg, ToolMessage)
        assert "[EVIDENCE:course_id=48,job_id=123,commis_id=abc-123]" in expanded_msg.content
        assert "--- Evidence for Commis 123 ---" in expanded_msg.content
        assert "Full tool outputs here" in expanded_msg.content

    @pytest.mark.asyncio
    async def test_expands_multiple_markers(self, mock_base_llm, mock_compiler):
        """Test expansion of multiple evidence markers in different messages."""
        mock_db = MagicMock()
        wrapper = EvidenceMountingLLM(base_llm=mock_base_llm, course_id=48, owner_id=100, db=mock_db)

        messages = [
            HumanMessage(content="Check servers"),
            ToolMessage(
                content="Commis 1 done.\n[EVIDENCE:course_id=48,job_id=123,commis_id=abc-123]",
                tool_call_id="tc1",
                name="spawn_commis",
            ),
            ToolMessage(
                content="Commis 2 done.\n[EVIDENCE:course_id=48,job_id=456,commis_id=xyz-456]",
                tool_call_id="tc2",
                name="spawn_commis",
            ),
        ]

        result = await wrapper.ainvoke(messages)

        # Compiler should be called once (for entire run)
        mock_compiler.compile.assert_called_once_with(course_id=48, owner_id=100, db=mock_db)

        # Both messages should have evidence expanded
        call_args = mock_base_llm.ainvoke.call_args[0][0]
        msg1 = call_args[1]
        msg2 = call_args[2]

        assert "--- Evidence for Commis 123 ---" in msg1.content
        assert "--- Evidence for Commis 456 ---" in msg2.content

    @pytest.mark.asyncio
    async def test_handles_missing_evidence_gracefully(self, mock_base_llm, mock_compiler):
        """Test handling when evidence is not found for a commis."""
        # Mock compiler returns evidence for job 123 but not 999
        mock_compiler.compile.return_value = {
            123: "--- Evidence for Commis 123 ---\nData here\n--- End Evidence ---"
        }

        mock_db = MagicMock()
        wrapper = EvidenceMountingLLM(base_llm=mock_base_llm, course_id=48, owner_id=100, db=mock_db)

        messages = [
            ToolMessage(
                content="Commis unknown.\n[EVIDENCE:course_id=48,job_id=999,commis_id=missing]",
                tool_call_id="tc1",
                name="spawn_commis",
            ),
        ]

        result = await wrapper.ainvoke(messages)

        # Should add "unavailable" note instead of crashing
        call_args = mock_base_llm.ainvoke.call_args[0][0]
        expanded_msg = call_args[0]
        assert "[EVIDENCE:course_id=48,job_id=999,commis_id=missing]" in expanded_msg.content
        assert "[Evidence unavailable for this commis]" in expanded_msg.content

    @pytest.mark.asyncio
    async def test_validates_course_id_mismatch(self, mock_base_llm, mock_compiler):
        """Test validation when marker course_id doesn't match context."""
        mock_db = MagicMock()
        wrapper = EvidenceMountingLLM(base_llm=mock_base_llm, course_id=48, owner_id=100, db=mock_db)

        messages = [
            ToolMessage(
                content="Commis done.\n[EVIDENCE:course_id=99,job_id=123,commis_id=abc]",  # Wrong course_id
                tool_call_id="tc1",
                name="spawn_commis",
            ),
        ]

        result = await wrapper.ainvoke(messages)

        # Should skip expansion and pass through original (with warning logged)
        call_args = mock_base_llm.ainvoke.call_args[0][0]
        msg = call_args[0]
        assert msg.content == "Commis done.\n[EVIDENCE:course_id=99,job_id=123,commis_id=abc]"  # Unchanged

    @pytest.mark.asyncio
    async def test_handles_compiler_error_gracefully(self, mock_base_llm):
        """Test graceful handling when EvidenceCompiler raises an error."""
        with patch("zerg.services.evidence_mounting_llm.EvidenceCompiler") as MockCompiler:
            compiler = MagicMock()
            compiler.compile.side_effect = Exception("Database connection failed")
            MockCompiler.return_value = compiler

            mock_db = MagicMock()
            wrapper = EvidenceMountingLLM(base_llm=mock_base_llm, course_id=48, owner_id=100, db=mock_db)

            messages = [
                ToolMessage(
                    content="Commis done.\n[EVIDENCE:course_id=48,job_id=123,commis_id=abc]",
                    tool_call_id="tc1",
                    name="spawn_commis",
                ),
            ]

            result = await wrapper.ainvoke(messages)

            # Should pass through original messages (not crash)
            assert result.content == "Test response"
            call_args = mock_base_llm.ainvoke.call_args[0][0]
            msg = call_args[0]
            assert msg.content == "Commis done.\n[EVIDENCE:course_id=48,job_id=123,commis_id=abc]"  # Unchanged

    @pytest.mark.asyncio
    async def test_messages_not_mutated(self, mock_base_llm, mock_compiler):
        """Test that original messages are never mutated (copies are made)."""
        mock_db = MagicMock()
        wrapper = EvidenceMountingLLM(base_llm=mock_base_llm, course_id=48, owner_id=100, db=mock_db)

        original_content = "Commis done.\n[EVIDENCE:course_id=48,job_id=123,commis_id=abc]"
        messages = [
            ToolMessage(
                content=original_content,
                tool_call_id="tc1",
                name="spawn_commis",
            ),
        ]

        await wrapper.ainvoke(messages)

        # Original message should be unchanged
        assert messages[0].content == original_content

    def test_bind_tools_creates_new_wrapper(self, mock_base_llm):
        """Test that bind_tools returns a new wrapper with bound tools."""
        mock_db = MagicMock()
        wrapper = EvidenceMountingLLM(base_llm=mock_base_llm, course_id=48, owner_id=100, db=mock_db)

        # Mock bind_tools to return a new mock LLM (not async)
        bound_mock_llm = MagicMock()
        mock_base_llm.bind_tools = MagicMock(return_value=bound_mock_llm)

        tools = [{"name": "test_tool"}]
        bound_wrapper = wrapper.bind_tools(tools)

        # Should return new wrapper
        assert isinstance(bound_wrapper, EvidenceMountingLLM)
        assert bound_wrapper.base_llm is bound_mock_llm
        assert bound_wrapper.course_id == 48
        assert bound_wrapper.owner_id == 100

        # Original wrapper unchanged
        assert wrapper.base_llm is mock_base_llm

    def test_getattr_delegates_to_base_llm(self, mock_base_llm):
        """Test that unknown attributes are delegated to base LLM."""
        mock_base_llm.custom_attribute = "test_value"
        mock_base_llm.custom_method = lambda: "test_result"

        wrapper = EvidenceMountingLLM(base_llm=mock_base_llm)

        assert wrapper.custom_attribute == "test_value"
        assert wrapper.custom_method() == "test_result"
