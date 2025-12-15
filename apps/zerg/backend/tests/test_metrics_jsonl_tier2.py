"""Test to verify Tier 2 (Progressive Disclosure) - metrics.jsonl functionality."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from zerg.services.worker_artifact_store import WorkerArtifactStore
from zerg.worker_metrics import (
    MetricsCollector,
    get_metrics_collector,
    reset_metrics_collector,
    set_metrics_collector,
)


@pytest.fixture
def metrics_artifact_store(tmp_path):
    """Create a test artifact store for metrics verification."""
    return WorkerArtifactStore(base_path=str(tmp_path))


def test_metrics_jsonl_creation(metrics_artifact_store):
    """Verify that metrics.jsonl is created with proper structure."""
    # Create a test worker
    worker_id = metrics_artifact_store.create_worker("Test metrics collection", config={})
    metrics_artifact_store.start_worker(worker_id)

    # Set up metrics collector
    collector = MetricsCollector(worker_id)
    set_metrics_collector(collector)

    # Record test metrics
    start_ts = datetime.now(timezone.utc)
    end_ts = datetime.now(timezone.utc)

    collector.record_llm_call(
        phase="test_phase",
        model="gpt-5-mini",
        start_ts=start_ts,
        end_ts=end_ts,
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
    )

    collector.record_tool_call(
        tool_name="test_tool",
        start_ts=start_ts,
        end_ts=end_ts,
        success=True,
    )

    # Flush metrics to disk
    collector.flush(metrics_artifact_store)
    reset_metrics_collector()

    # Complete the worker
    metrics_artifact_store.complete_worker(worker_id, status="success")

    # Verify metrics.jsonl exists
    worker_dir = Path(metrics_artifact_store.base_path) / worker_id
    metrics_path = worker_dir / "metrics.jsonl"

    assert metrics_path.exists(), f"metrics.jsonl should exist at {metrics_path}"

    # Read and validate JSONL structure
    with open(metrics_path) as f:
        lines = f.readlines()

    assert len(lines) >= 2, f"Should have at least 2 events (llm_call + tool_call), got {len(lines)}"

    # Parse each line as JSON
    events = []
    for i, line in enumerate(lines):
        event = json.loads(line)  # Should not raise
        events.append(event)

    # Verify we have both event types
    event_types = [e.get("event") for e in events]
    assert "llm_call" in event_types, "Should have llm_call event"
    assert "tool_call" in event_types, "Should have tool_call event"

    # Verify llm_call structure
    llm_event = next(e for e in events if e.get("event") == "llm_call")
    assert "phase" in llm_event, "llm_call should have phase"
    assert "model" in llm_event, "llm_call should have model"
    assert "duration_ms" in llm_event, "llm_call should have duration_ms"
    assert "prompt_tokens" in llm_event, "llm_call should have prompt_tokens"
    assert "completion_tokens" in llm_event, "llm_call should have completion_tokens"
    assert "total_tokens" in llm_event, "llm_call should have total_tokens"

    # Verify tool_call structure
    tool_event = next(e for e in events if e.get("event") == "tool_call")
    assert "tool" in tool_event, "tool_call should have tool"
    assert "duration_ms" in tool_event, "tool_call should have duration_ms"
    assert "success" in tool_event, "tool_call should have success"
    assert tool_event["success"] is True, "tool_call should be successful"


def test_read_worker_file_can_access_metrics(metrics_artifact_store):
    """Verify that read_worker_file can access metrics.jsonl (for supervisor access)."""
    # Create a test worker with metrics
    worker_id = metrics_artifact_store.create_worker("Test supervisor access", config={})
    metrics_artifact_store.start_worker(worker_id)

    # Create and flush metrics
    collector = MetricsCollector(worker_id)
    set_metrics_collector(collector)

    start_ts = datetime.now(timezone.utc)
    end_ts = datetime.now(timezone.utc)

    collector.record_llm_call(
        phase="test",
        model="gpt-5-mini",
        start_ts=start_ts,
        end_ts=end_ts,
    )

    collector.flush(metrics_artifact_store)
    reset_metrics_collector()

    metrics_artifact_store.complete_worker(worker_id, status="success")

    # Verify supervisor can read metrics.jsonl via read_worker_file
    metrics_content = metrics_artifact_store.read_worker_file(worker_id, "metrics.jsonl")

    assert metrics_content, "metrics.jsonl should have content"
    assert "llm_call" in metrics_content, "metrics.jsonl should contain llm_call events"

    # Verify it's valid JSONL
    lines = metrics_content.strip().split("\n")
    for line in lines:
        event = json.loads(line)  # Should not raise
        assert "event" in event, "Each event should have 'event' field"


def test_metrics_collector_context_isolation():
    """Verify that metrics collectors are properly isolated via context vars."""
    # Initially no collector
    assert get_metrics_collector() is None

    # Set collector 1
    collector1 = MetricsCollector("worker-1")
    set_metrics_collector(collector1)
    assert get_metrics_collector() is collector1

    # Reset
    reset_metrics_collector()
    assert get_metrics_collector() is None

    # Set collector 2
    collector2 = MetricsCollector("worker-2")
    set_metrics_collector(collector2)
    assert get_metrics_collector() is collector2

    # Reset again
    reset_metrics_collector()
    assert get_metrics_collector() is None
