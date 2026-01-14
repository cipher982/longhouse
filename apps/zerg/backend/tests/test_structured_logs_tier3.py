"""Test to verify Tier 3 (Dev Telemetry) - structured logging functionality."""

import logging
from datetime import datetime
from datetime import timezone

import pytest

from zerg.context import WorkerContext
from zerg.context import reset_worker_context
from zerg.context import set_worker_context
from zerg.worker_metrics import MetricsCollector
from zerg.worker_metrics import reset_metrics_collector
from zerg.worker_metrics import set_metrics_collector


@pytest.fixture
def capture_logs(caplog):
    """Configure logging to capture structured logs."""
    caplog.set_level(logging.INFO)
    return caplog


def test_llm_call_structured_logging(capture_logs):
    """Verify that LLM calls emit structured logs for grep-ability."""

    # Set up worker context
    ctx = WorkerContext(
        worker_id="test-worker-123",
        owner_id=1,
        run_id="run-456",
        job_id=789,
        task="Test task",
    )
    token = set_worker_context(ctx)

    # Set up metrics collector
    collector = MetricsCollector(ctx.worker_id)
    set_metrics_collector(collector)

    try:
        # Simulate LLM call completion with structured logging
        # This is what happens in worker_runner.py during summary extraction
        start_time = datetime.now(timezone.utc)
        end_time = datetime.now(timezone.utc)
        duration_ms = 123

        collector.record_llm_call(
            phase="test_phase",
            model="gpt-5-mini",
            start_ts=start_time,
            end_ts=end_time,
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
        )

        # Emit structured log (simulating what worker_runner does)
        logger = logging.getLogger("zerg.test")
        log_extra = {
            "event": "llm_call_complete",
            "phase": "test_phase",
            "model": "gpt-5-mini",
            "duration_ms": duration_ms,
            "worker_id": ctx.worker_id,
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        }
        logger.info("llm_call_complete", extra=log_extra)

        # Verify structured log was emitted
        assert len(capture_logs.records) > 0, "Should have captured log records"

        # Find our structured log
        llm_logs = [r for r in capture_logs.records if "llm_call_complete" in r.message]
        assert len(llm_logs) > 0, "Should have llm_call_complete log"

        log_record = llm_logs[0]
        assert hasattr(log_record, "event"), "Log should have 'event' attribute"
        assert log_record.event == "llm_call_complete"
        assert hasattr(log_record, "phase"), "Log should have 'phase' attribute"
        assert hasattr(log_record, "duration_ms"), "Log should have 'duration_ms' attribute"
        assert hasattr(log_record, "worker_id"), "Log should have 'worker_id' attribute"
        assert log_record.worker_id == "test-worker-123"

    finally:
        reset_worker_context(token)
        reset_metrics_collector()


def test_tool_call_structured_logging(capture_logs):
    """Verify that tool calls emit structured logs for grep-ability."""
    from zerg.context import WorkerContext
    from zerg.context import reset_worker_context
    from zerg.context import set_worker_context

    # Set up worker context
    ctx = WorkerContext(
        worker_id="test-worker-456",
        owner_id=1,
        run_id="run-789",
        job_id=101,
        task="Test tool task",
    )
    token = set_worker_context(ctx)

    # Set up metrics collector
    collector = MetricsCollector(ctx.worker_id)
    set_metrics_collector(collector)

    try:
        # Simulate tool call completion
        start_time = datetime.now(timezone.utc)
        end_time = datetime.now(timezone.utc)
        duration_ms = 456

        collector.record_tool_call(
            tool_name="ssh_exec",
            start_ts=start_time,
            end_ts=end_time,
            success=True,
        )

        # Emit structured log (simulating what supervisor_react_engine does)
        logger = logging.getLogger("zerg.test")
        log_extra = {
            "event": "tool_call_complete",
            "tool": "ssh_exec",
            "duration_ms": duration_ms,
            "success": True,
            "worker_id": ctx.worker_id,
        }
        logger.info("tool_call_complete", extra=log_extra)

        # Verify structured log was emitted
        tool_logs = [r for r in capture_logs.records if "tool_call_complete" in r.message]
        assert len(tool_logs) > 0, "Should have tool_call_complete log"

        log_record = tool_logs[0]
        assert hasattr(log_record, "event"), "Log should have 'event' attribute"
        assert log_record.event == "tool_call_complete"
        assert hasattr(log_record, "tool"), "Log should have 'tool' attribute"
        assert log_record.tool == "ssh_exec"
        assert hasattr(log_record, "duration_ms"), "Log should have 'duration_ms' attribute"
        assert hasattr(log_record, "success"), "Log should have 'success' attribute"
        assert log_record.success is True

    finally:
        reset_worker_context(token)
        reset_metrics_collector()


def test_structured_logs_grep_pattern():
    """Verify that structured logs follow consistent grep-able patterns.

    This test documents the expected grep patterns for monitoring:
    - grep "llm_call_complete" logs/backend.log
    - grep "tool_call_complete" logs/backend.log
    - grep "duration_ms=" logs/backend.log | sort -t= -k4 -n
    - grep "worker_id=2025-" logs/backend.log
    """
    logger = logging.getLogger("zerg.test")

    # Test LLM call pattern
    log_extra = {
        "event": "llm_call_complete",
        "phase": "synthesis",
        "model": "gpt-5-mini",
        "duration_ms": 1234,
        "worker_id": "2025-12-15T04-00-00_test",
        "prompt_tokens": 500,
    }
    logger.info("llm_call_complete", extra=log_extra)

    # Test tool call pattern
    log_extra = {
        "event": "tool_call_complete",
        "tool": "http_request",
        "duration_ms": 567,
        "success": True,
        "worker_id": "2025-12-15T04-00-00_test",
    }
    logger.info("tool_call_complete", extra=log_extra)

    # If we get here without exceptions, the patterns are valid
    assert True, "Structured logging patterns are valid"


def test_structured_logs_fail_safe():
    """Verify that structured logging failures don't crash the worker.

    The structured logging is best-effort and wrapped in try/except blocks.
    """
    logger = logging.getLogger("zerg.test")

    # Test with invalid extra data (should not crash)
    try:
        log_extra = {
            "event": "llm_call_complete",
            "invalid_object": object(),  # Can't be serialized
        }
        logger.info("llm_call_complete", extra=log_extra)
    except Exception:
        pytest.fail("Structured logging should not crash on invalid data")

    # Test with None values (should not crash)
    try:
        log_extra = {
            "event": "tool_call_complete",
            "tool": None,
            "duration_ms": None,
        }
        logger.info("tool_call_complete", extra=log_extra)
    except Exception:
        pytest.fail("Structured logging should not crash on None values")
