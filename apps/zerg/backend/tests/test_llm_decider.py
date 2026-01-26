"""Tests for the LLM decider module."""

import asyncio
from datetime import datetime
from datetime import timezone
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from zerg.services.llm_decider import LLMDeciderStats
from zerg.services.llm_decider import LLMDecisionPayload
from zerg.services.llm_decider import LLMDecisionResult
from zerg.services.llm_decider import build_decision_payload
from zerg.services.llm_decider import build_llm_prompt
from zerg.services.llm_decider import call_llm_decider
from zerg.services.llm_decider import decide_roundabout_action
from zerg.services.roundabout_monitor import DecisionContext
from zerg.services.roundabout_monitor import ToolActivity


class TestLLMDeciderStats:
    """Tests for LLMDeciderStats tracking."""

    def test_initial_state(self):
        """Stats should start at zero."""
        stats = LLMDeciderStats()
        assert stats.calls_made == 0
        assert stats.calls_succeeded == 0
        assert stats.calls_timed_out == 0
        assert stats.calls_errored == 0

    def test_record_successful_call(self):
        """Should track successful calls."""
        stats = LLMDeciderStats()
        result = LLMDecisionResult(
            action="wait",
            rationale="LLM decided: wait",
            response_time_ms=500.0,
            was_fallback=False,
        )
        stats.record_call(result)

        assert stats.calls_made == 1
        assert stats.calls_succeeded == 1
        assert stats.total_response_time_ms == 500.0

    def test_record_timeout_call(self):
        """Should track timeout as fallback."""
        stats = LLMDeciderStats()
        result = LLMDecisionResult(
            action="wait",
            rationale="LLM timeout after 1500ms",
            response_time_ms=1500.0,
            was_fallback=True,
        )
        stats.record_call(result)

        assert stats.calls_made == 1
        assert stats.calls_timed_out == 1
        assert stats.calls_succeeded == 0

    def test_record_error_call(self):
        """Should track errors as fallback."""
        stats = LLMDeciderStats()
        result = LLMDecisionResult(
            action="wait",
            rationale="LLM error: API key invalid",
            response_time_ms=100.0,
            was_fallback=True,
        )
        stats.record_call(result)

        assert stats.calls_made == 1
        assert stats.calls_errored == 1

    def test_record_skip_budget(self):
        """Should track budget skips."""
        stats = LLMDeciderStats()
        stats.record_skip("budget")
        assert stats.calls_skipped_budget == 1

    def test_record_skip_interval(self):
        """Should track interval skips."""
        stats = LLMDeciderStats()
        stats.record_skip("interval")
        assert stats.calls_skipped_interval == 1

    def test_to_dict(self):
        """Should convert to dict for activity summary."""
        stats = LLMDeciderStats()
        stats.calls_made = 3
        stats.calls_succeeded = 2
        stats.calls_timed_out = 1
        stats.total_response_time_ms = 1500.0

        result = stats.to_dict()

        assert result["llm_calls"] == 3
        assert result["llm_calls_succeeded"] == 2
        assert result["llm_timeouts"] == 1
        assert result["llm_avg_response_ms"] == 500.0

    def test_to_dict_excludes_zero_counts(self):
        """Should not include zero counts in output."""
        stats = LLMDeciderStats()
        stats.calls_made = 1
        stats.calls_succeeded = 1
        stats.total_response_time_ms = 500.0

        result = stats.to_dict()

        assert "llm_timeouts" not in result
        assert "llm_errors" not in result
        assert "llm_skipped_budget" not in result

    def test_to_dict_includes_skips_without_calls(self):
        """Should include skip counters even when no calls were made.

        This ensures observability when jobs stay entirely in skip state.
        """
        stats = LLMDeciderStats()
        # No calls made, but skips recorded
        stats.calls_skipped_budget = 5
        stats.calls_skipped_interval = 10

        result = stats.to_dict()

        # Should include skip counters
        assert result["llm_skipped_budget"] == 5
        assert result["llm_skipped_interval"] == 10
        # Should NOT include call stats (no calls made)
        assert "llm_calls" not in result
        assert "llm_calls_succeeded" not in result
        assert "llm_avg_response_ms" not in result


class TestBuildDecisionPayload:
    """Tests for building the LLM decision payload."""

    def _make_context(self, **overrides) -> DecisionContext:
        """Helper to create a DecisionContext with defaults."""
        defaults = {
            "job_id": 1,
            "commis_id": "test-commis-123",
            "task": "Test task",
            "status": "running",
            "elapsed_seconds": 10.0,
            "tool_activities": [],
            "current_operation": None,
            "is_stuck": False,
            "stuck_seconds": 0.0,
            "polls_without_progress": 0,
            "last_tool_output": None,
        }
        defaults.update(overrides)
        return DecisionContext(**defaults)

    def test_basic_payload(self):
        """Should build basic payload from context."""
        ctx = self._make_context()
        payload = build_decision_payload(ctx)

        assert payload.job_id == 1
        assert payload.status == "running"
        assert payload.elapsed_seconds == 10.0
        # v2.0: No current operation, so op fields are None
        assert payload.current_op_elapsed_seconds is None
        assert payload.current_op_name is None
        assert payload.current_op_args is None

    def test_last_3_tools_only(self):
        """Should only include last 3 tool activities."""
        activities = [
            ToolActivity("tool1", "completed", datetime.now(timezone.utc)),
            ToolActivity("tool2", "completed", datetime.now(timezone.utc)),
            ToolActivity("tool3", "completed", datetime.now(timezone.utc)),
            ToolActivity("tool4", "completed", datetime.now(timezone.utc)),
            ToolActivity("tool5", "started", datetime.now(timezone.utc)),
        ]
        ctx = self._make_context(tool_activities=activities)
        payload = build_decision_payload(ctx)

        assert len(payload.last_3_tools) == 3
        assert payload.last_3_tools[0]["name"] == "tool3"
        assert payload.last_3_tools[2]["name"] == "tool5"

    def test_activity_counts(self):
        """Should count tool activities correctly."""
        activities = [
            ToolActivity("tool1", "completed", datetime.now(timezone.utc)),
            ToolActivity("tool2", "completed", datetime.now(timezone.utc)),
            ToolActivity("tool3", "failed", datetime.now(timezone.utc), error="error"),
            ToolActivity("tool4", "started", datetime.now(timezone.utc)),
        ]
        ctx = self._make_context(tool_activities=activities)
        payload = build_decision_payload(ctx)

        assert payload.activity_counts["total"] == 4
        assert payload.activity_counts["completed"] == 2
        assert payload.activity_counts["failed"] == 1

    def test_log_tail_truncation(self):
        """Should truncate long output to ~600 chars."""
        long_output = "x" * 1000
        ctx = self._make_context(last_tool_output=long_output)
        payload = build_decision_payload(ctx)

        assert len(payload.log_tail) <= 603  # 600 + "..."
        assert payload.log_tail.startswith("...")

    def test_error_truncation(self):
        """Should truncate long error messages."""
        activities = [
            ToolActivity(
                "tool1",
                "failed",
                datetime.now(timezone.utc),
                error="x" * 200,
            ),
        ]
        ctx = self._make_context(tool_activities=activities)
        payload = build_decision_payload(ctx)

        assert len(payload.last_3_tools[0]["error"]) == 100

    def test_current_operation_context(self):
        """v2.0: Should include current operation context for stuck judgment.

        Instead of pre-computed is_stuck boolean, we pass raw timing and
        operation details so the LLM can make contextual decisions.
        """
        current_op = ToolActivity(
            tool_name="ssh_exec",
            status="started",
            timestamp=datetime.now(timezone.utc),
            args_preview="du -sh /var/*",
        )
        ctx = self._make_context(
            current_operation=current_op,
            stuck_seconds=45.0,  # 45s might be stuck for ls, but normal for du
        )
        payload = build_decision_payload(ctx)

        # v2.0: Raw timing and operation context
        assert payload.current_op_elapsed_seconds == 45.0
        assert payload.current_op_name == "ssh_exec"
        assert payload.current_op_args == "du -sh /var/*"


class TestBuildLLMPrompt:
    """Tests for the LLM prompt builder."""

    def test_prompt_structure(self):
        """Should build valid system and user prompts."""
        payload = LLMDecisionPayload(
            job_id=1,
            status="running",
            elapsed_seconds=10.0,
            current_op_elapsed_seconds=None,
            current_op_name=None,
            current_op_args=None,
            last_3_tools=[],
            activity_counts={"total": 0, "completed": 0, "failed": 0},
            log_tail="",
        )
        system_prompt, user_prompt = build_llm_prompt(payload)

        # System prompt should contain decision options
        assert "wait" in system_prompt
        assert "exit" in system_prompt
        assert "cancel" in system_prompt
        assert "peek" in system_prompt

        # User prompt should contain job info
        assert "job_id" in user_prompt
        assert "running" in user_prompt


class TestCallLLMDecider:
    """Tests for the LLM decider API call."""

    @pytest.mark.asyncio
    async def test_successful_wait_response(self):
        """Should handle valid 'wait' response."""
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "wait"

        with patch("openai.AsyncOpenAI") as mock_client:
            mock_client.return_value.chat.completions.create = AsyncMock(return_value=mock_response)

            payload = LLMDecisionPayload(
                job_id=1,
                status="running",
                elapsed_seconds=10.0,
                current_op_elapsed_seconds=None,
                current_op_name=None,
                current_op_args=None,
                last_3_tools=[],
                activity_counts={},
                log_tail="",
            )
            result = await call_llm_decider(payload)

            assert result.action == "wait"
            assert result.was_fallback is False

    @pytest.mark.asyncio
    async def test_successful_exit_response(self):
        """Should handle valid 'exit' response."""
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "exit"

        with patch("openai.AsyncOpenAI") as mock_client:
            mock_client.return_value.chat.completions.create = AsyncMock(return_value=mock_response)

            payload = LLMDecisionPayload(
                job_id=1,
                status="running",
                elapsed_seconds=10.0,
                current_op_elapsed_seconds=None,
                current_op_name=None,
                current_op_args=None,
                last_3_tools=[],
                activity_counts={},
                log_tail="Result: success",
            )
            result = await call_llm_decider(payload)

            assert result.action == "exit"
            assert result.was_fallback is False

    @pytest.mark.asyncio
    async def test_successful_cancel_response(self):
        """Should handle valid 'cancel' response - operation stuck for long time."""
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "cancel"

        with patch("openai.AsyncOpenAI") as mock_client:
            mock_client.return_value.chat.completions.create = AsyncMock(return_value=mock_response)

            # v2.0: Pass raw timing - LLM decides if 90s on simple command is stuck
            payload = LLMDecisionPayload(
                job_id=1,
                status="running",
                elapsed_seconds=90.0,
                current_op_elapsed_seconds=90.0,
                current_op_name="ssh_exec",
                current_op_args="ls -la",  # Simple command, 90s is definitely stuck
                last_3_tools=[],
                activity_counts={},
                log_tail="",
            )
            result = await call_llm_decider(payload)

            assert result.action == "cancel"
            assert result.was_fallback is False

    @pytest.mark.asyncio
    async def test_invalid_response_fallback(self):
        """Should fallback to wait on invalid response."""
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "invalid_action"

        with patch("openai.AsyncOpenAI") as mock_client:
            mock_client.return_value.chat.completions.create = AsyncMock(return_value=mock_response)

            payload = LLMDecisionPayload(
                job_id=1,
                status="running",
                elapsed_seconds=10.0,
                current_op_elapsed_seconds=None,
                current_op_name=None,
                current_op_args=None,
                last_3_tools=[],
                activity_counts={},
                log_tail="",
            )
            result = await call_llm_decider(payload)

            assert result.action == "wait"
            assert result.was_fallback is True
            assert "invalid" in result.rationale.lower()

    @pytest.mark.asyncio
    async def test_timeout_fallback(self):
        """Should fallback to wait on timeout."""

        async def slow_response(*args, **kwargs):
            await asyncio.sleep(10)  # Longer than timeout

        with patch("openai.AsyncOpenAI") as mock_client:
            mock_client.return_value.chat.completions.create = slow_response

            payload = LLMDecisionPayload(
                job_id=1,
                status="running",
                elapsed_seconds=10.0,
                current_op_elapsed_seconds=None,
                current_op_name=None,
                current_op_args=None,
                last_3_tools=[],
                activity_counts={},
                log_tail="",
            )
            result = await call_llm_decider(payload, timeout_seconds=0.01)

            assert result.action == "wait"
            assert result.was_fallback is True
            assert "timeout" in result.rationale.lower()

    @pytest.mark.asyncio
    async def test_api_error_fallback(self):
        """Should fallback to wait on API error."""
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_client.return_value.chat.completions.create = AsyncMock(side_effect=Exception("API Error"))

            payload = LLMDecisionPayload(
                job_id=1,
                status="running",
                elapsed_seconds=10.0,
                current_op_elapsed_seconds=None,
                current_op_name=None,
                current_op_args=None,
                last_3_tools=[],
                activity_counts={},
                log_tail="",
            )
            result = await call_llm_decider(payload)

            assert result.action == "wait"
            assert result.was_fallback is True
            assert "error" in result.rationale.lower()


class TestDecideRoundaboutAction:
    """Tests for the high-level decision function."""

    def _make_context(self, **overrides) -> DecisionContext:
        """Helper to create a DecisionContext with defaults."""
        defaults = {
            "job_id": 1,
            "commis_id": "test-commis-123",
            "task": "Test task",
            "status": "running",
            "elapsed_seconds": 10.0,
            "tool_activities": [],
            "current_operation": None,
            "is_stuck": False,
            "stuck_seconds": 0.0,
            "polls_without_progress": 0,
            "last_tool_output": None,
        }
        defaults.update(overrides)
        return DecisionContext(**defaults)

    @pytest.mark.asyncio
    async def test_returns_action_rationale_result(self):
        """Should return tuple of action, rationale, and result."""
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "wait"

        with patch("openai.AsyncOpenAI") as mock_client:
            mock_client.return_value.chat.completions.create = AsyncMock(return_value=mock_response)

            ctx = self._make_context()
            action, rationale, result = await decide_roundabout_action(ctx)

            assert action == "wait"
            assert isinstance(rationale, str)
            assert isinstance(result, LLMDecisionResult)
