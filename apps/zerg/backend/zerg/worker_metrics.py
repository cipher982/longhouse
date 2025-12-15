"""Metrics collection for worker performance tracking.

This module provides context managers and utilities for capturing detailed
performance metrics during worker execution. Metrics are written to
metrics.jsonl in the worker artifact directory for offline analysis.

Design goals:
- Non-intrusive: Metrics collection should not slow down execution
- Fail-safe: Errors in metrics collection must not crash the worker
- Structured: Use consistent JSONL format for easy parsing
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from datetime import datetime
from datetime import timezone
from typing import Any

logger = logging.getLogger(__name__)

# Context variable to hold the current metrics collector (if any)
_metrics_collector: ContextVar["MetricsCollector | None"] = ContextVar(
    "metrics_collector", default=None
)


class MetricsCollector:
    """Collects and buffers metrics events for a worker.

    This class is designed to be used as a context manager and stores metrics
    in memory during worker execution. Call flush() to write to disk.

    Usage:
        collector = MetricsCollector(worker_id)
        set_metrics_collector(collector)
        try:
            # ... worker execution ...
            collector.record_llm_call(phase="tool_decision", ...)
            collector.record_tool_call(tool_name="ssh_exec", ...)
        finally:
            collector.flush(artifact_store)
            reset_metrics_collector()
    """

    def __init__(self, worker_id: str):
        """Initialize metrics collector for a worker.

        Parameters
        ----------
        worker_id
            Unique worker identifier
        """
        self.worker_id = worker_id
        self.events: list[dict[str, Any]] = []

    def record_llm_call(
        self,
        phase: str,
        model: str,
        start_ts: datetime,
        end_ts: datetime,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
    ) -> None:
        """Record an LLM call event.

        Parameters
        ----------
        phase
            Phase name (e.g., "tool_decision", "synthesis", "summary")
        model
            Model identifier (e.g., "gpt-5-mini")
        start_ts
            Start timestamp (UTC)
        end_ts
            End timestamp (UTC)
        prompt_tokens
            Number of prompt tokens (if available)
        completion_tokens
            Number of completion tokens (if available)
        total_tokens
            Total tokens (if available)
        """
        duration_ms = int((end_ts - start_ts).total_seconds() * 1000)

        event = {
            "event": "llm_call",
            "phase": phase,
            "model": model,
            "start_ts": start_ts.isoformat(),
            "end_ts": end_ts.isoformat(),
            "duration_ms": duration_ms,
        }

        # Include token counts if available
        if prompt_tokens is not None:
            event["prompt_tokens"] = prompt_tokens
        if completion_tokens is not None:
            event["completion_tokens"] = completion_tokens
        if total_tokens is not None:
            event["total_tokens"] = total_tokens

        self.events.append(event)

    def record_tool_call(
        self,
        tool_name: str,
        start_ts: datetime,
        end_ts: datetime,
        success: bool = True,
        error: str | None = None,
    ) -> None:
        """Record a tool call event.

        Parameters
        ----------
        tool_name
            Name of the tool
        start_ts
            Start timestamp (UTC)
        end_ts
            End timestamp (UTC)
        success
            Whether the tool call succeeded
        error
            Error message if failed
        """
        duration_ms = int((end_ts - start_ts).total_seconds() * 1000)

        event = {
            "event": "tool_call",
            "tool": tool_name,
            "start_ts": start_ts.isoformat(),
            "end_ts": end_ts.isoformat(),
            "duration_ms": duration_ms,
            "success": success,
        }

        if error:
            event["error"] = error

        self.events.append(event)

    def flush(self, artifact_store) -> None:
        """Write all collected metrics to disk.

        Parameters
        ----------
        artifact_store
            WorkerArtifactStore instance to write metrics to
        """
        if not self.events:
            return

        try:
            for event in self.events:
                artifact_store.save_metric(self.worker_id, event)
            logger.debug(f"Flushed {len(self.events)} metrics for worker {self.worker_id}")
        except Exception as e:
            # Metrics collection is best-effort - don't fail the worker
            logger.warning(f"Failed to flush metrics for worker {self.worker_id}: {e}")


def set_metrics_collector(collector: MetricsCollector | None) -> None:
    """Set the current metrics collector.

    Parameters
    ----------
    collector
        MetricsCollector instance or None to clear
    """
    _metrics_collector.set(collector)


def reset_metrics_collector() -> None:
    """Clear the current metrics collector."""
    _metrics_collector.set(None)


def get_metrics_collector() -> MetricsCollector | None:
    """Get the current metrics collector (if any).

    Returns
    -------
    MetricsCollector | None
        Current collector or None if not in a metrics context
    """
    return _metrics_collector.get()


__all__ = [
    "MetricsCollector",
    "set_metrics_collector",
    "reset_metrics_collector",
    "get_metrics_collector",
]
