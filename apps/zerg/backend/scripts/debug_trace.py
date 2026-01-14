#!/usr/bin/env python3
"""Debug a trace by showing the full timeline across all tables.

This script queries agent_runs, worker_jobs, and llm_audit_log tables
by trace_id and builds a unified timeline for debugging.

Usage:
    uv run python scripts/debug_trace.py <trace_id>
    uv run python scripts/debug_trace.py <trace_id> --level full
    uv run python scripts/debug_trace.py <trace_id> --level errors
    uv run python scripts/debug_trace.py --recent     # Show recent traces

Levels:
    summary (default): High-level timeline with key events
    full: Include LLM message details
    errors: Only show errors and anomalies
"""

import argparse
import json
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Any


@dataclass
class TimelineEvent:
    """A single event in the trace timeline."""

    timestamp: datetime
    event_type: str
    source: str  # 'run', 'worker', 'llm'
    details: dict[str, Any]
    is_error: bool = False

    def format_time(self) -> str:
        """Format timestamp for display."""
        return self.timestamp.strftime("%H:%M:%S.%f")[:-3]

    def format_details(self, max_len: int = 60) -> str:
        """Format details for display."""
        parts = []
        for k, v in self.details.items():
            if v is not None:
                s = f"{k}={v}"
                if len(s) > max_len:
                    s = s[:max_len] + "..."
                parts.append(s)
        return " ".join(parts)


def get_trace_data(db, trace_id: uuid.UUID) -> dict:
    """Query all tables for a given trace_id."""
    from zerg.models.llm_audit import LLMAuditLog
    from zerg.models.models import AgentRun
    from zerg.models.models import WorkerJob

    # Get all runs with this trace
    runs = db.query(AgentRun).filter(AgentRun.trace_id == trace_id).all()

    # Get all worker jobs with this trace
    workers = db.query(WorkerJob).filter(WorkerJob.trace_id == trace_id).all()

    # Get all LLM audit logs with this trace
    llm_logs = db.query(LLMAuditLog).filter(LLMAuditLog.trace_id == trace_id).all()

    return {
        "runs": runs,
        "workers": workers,
        "llm_logs": llm_logs,
    }


def build_timeline(data: dict) -> list[TimelineEvent]:
    """Build a unified timeline from all data sources."""
    events = []

    # Add run events
    for run in data["runs"]:
        # Run started
        if run.started_at:
            ts = run.started_at.replace(tzinfo=timezone.utc) if run.started_at.tzinfo is None else run.started_at
            events.append(
                TimelineEvent(
                    timestamp=ts,
                    event_type="supervisor.run.started",
                    source="run",
                    details={
                        "run_id": run.id,
                        "agent_id": run.agent_id,
                        "thread_id": run.thread_id,
                        "model": run.model,
                        "status": run.status.value if run.status else None,
                    },
                )
            )

        # Run finished
        if run.finished_at:
            ts = run.finished_at.replace(tzinfo=timezone.utc) if run.finished_at.tzinfo is None else run.finished_at
            is_error = run.status and run.status.value in ("failed", "error")
            events.append(
                TimelineEvent(
                    timestamp=ts,
                    event_type=f"supervisor.run.{run.status.value if run.status else 'finished'}",
                    source="run",
                    details={
                        "run_id": run.id,
                        "duration_ms": run.duration_ms,
                        "total_tokens": run.total_tokens,
                        "error": run.error[:100] if run.error else None,
                    },
                    is_error=is_error,
                )
            )

    # Add worker events
    for worker in data["workers"]:
        # Worker created/queued
        if worker.created_at:
            ts = worker.created_at.replace(tzinfo=timezone.utc) if worker.created_at.tzinfo is None else worker.created_at
            events.append(
                TimelineEvent(
                    timestamp=ts,
                    event_type="worker.spawned",
                    source="worker",
                    details={
                        "job_id": worker.id,
                        "task": worker.task[:50] if worker.task else None,
                        "model": worker.model,
                        "status": worker.status,
                    },
                )
            )

        # Worker started
        if worker.started_at:
            ts = worker.started_at.replace(tzinfo=timezone.utc) if worker.started_at.tzinfo is None else worker.started_at
            events.append(
                TimelineEvent(
                    timestamp=ts,
                    event_type="worker.started",
                    source="worker",
                    details={
                        "job_id": worker.id,
                        "worker_id": worker.worker_id,
                    },
                )
            )

        # Worker completed
        if worker.finished_at:
            ts = worker.finished_at.replace(tzinfo=timezone.utc) if worker.finished_at.tzinfo is None else worker.finished_at
            is_error = worker.status in ("failed", "error")
            # Compute duration from timestamps (WorkerJob doesn't have duration_ms column)
            duration_ms = None
            if worker.started_at and worker.finished_at:
                delta = worker.finished_at - worker.started_at
                duration_ms = int(delta.total_seconds() * 1000)
            events.append(
                TimelineEvent(
                    timestamp=ts,
                    event_type=f"worker.{worker.status}" if worker.status else "worker.finished",
                    source="worker",
                    details={
                        "job_id": worker.id,
                        "duration_ms": duration_ms,
                        "error": worker.error[:100] if worker.error else None,
                    },
                    is_error=is_error,
                )
            )

    # Add LLM events
    for log in data["llm_logs"]:
        if log.created_at:
            ts = log.created_at.replace(tzinfo=timezone.utc) if log.created_at.tzinfo is None else log.created_at
            is_error = log.error is not None
            events.append(
                TimelineEvent(
                    timestamp=ts,
                    event_type="llm.generate",
                    source="llm",
                    details={
                        "phase": log.phase,
                        "model": log.model,
                        "msg_count": log.message_count,
                        "in_tokens": log.input_tokens,
                        "out_tokens": log.output_tokens,
                        "duration_ms": log.duration_ms,
                        "error": log.error[:50] if log.error else None,
                    },
                    is_error=is_error,
                )
            )

    # Sort by timestamp
    events.sort(key=lambda e: e.timestamp)
    return events


def detect_anomalies(data: dict, events: list[TimelineEvent]) -> list[str]:
    """Detect common issues in the trace."""
    anomalies = []

    # Check for failed runs
    for run in data["runs"]:
        if run.status and run.status.value == "failed":
            anomalies.append(f"Run {run.id} FAILED: {run.error or 'no error message'}")

    # Check for failed workers
    for worker in data["workers"]:
        if worker.status == "failed":
            anomalies.append(f"Worker {worker.id} FAILED: {worker.error or 'no error message'}")

    # Check for LLM errors
    for log in data["llm_logs"]:
        if log.error:
            anomalies.append(f"LLM error in {log.phase}: {log.error}")

    # Check for stuck workers (created but never started)
    for worker in data["workers"]:
        if worker.created_at and not worker.started_at and worker.status not in ("cancelled", "failed"):
            anomalies.append(f"Worker {worker.id} never started (status: {worker.status})")

    # Check for very long LLM calls (>60s)
    for log in data["llm_logs"]:
        if log.duration_ms and log.duration_ms > 60000:
            anomalies.append(f"Slow LLM call: {log.phase} took {log.duration_ms/1000:.1f}s")

    return anomalies


def format_timeline_output(
    trace_id: str,
    data: dict,
    events: list[TimelineEvent],
    anomalies: list[str],
    level: str,
) -> str:
    """Format the timeline for display."""
    lines = []

    # Header
    lines.append(f"Trace: {trace_id}")

    # Calculate overall stats
    start_time = events[0].timestamp if events else None
    end_time = events[-1].timestamp if events else None
    duration = (end_time - start_time).total_seconds() if start_time and end_time else 0

    # Determine overall status
    overall_status = "UNKNOWN"
    for run in data["runs"]:
        if run.status:
            overall_status = run.status.value.upper()
            break

    if start_time:
        lines.append(f"Started: {start_time.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    lines.append(f"Duration: {duration:.2f}s")
    lines.append(f"Status: {overall_status}")
    lines.append(f"Runs: {len(data['runs'])} | Workers: {len(data['workers'])} | LLM calls: {len(data['llm_logs'])}")
    lines.append("")

    # Timeline
    if level != "errors" and events:
        lines.append("Timeline:")
        lines.append("-" * 80)

        for event in events:
            prefix = "ERROR " if event.is_error else "      "
            details = event.format_details()
            line = f"{event.format_time()}  {prefix}{event.event_type:<30} {details}"
            lines.append(line)

        lines.append("-" * 80)
        lines.append("")

    # Anomalies/Errors
    if anomalies:
        lines.append("Anomalies/Errors:")
        for a in anomalies:
            lines.append(f"  - {a}")
        lines.append("")

    # Full details (if requested)
    if level == "full" and data["llm_logs"]:
        lines.append("LLM Details:")
        lines.append("-" * 80)
        for log in data["llm_logs"]:
            lines.append(f"\n[{log.phase}] {log.model}")
            lines.append(f"  Messages: {log.message_count}")
            lines.append(f"  Tokens: in={log.input_tokens} out={log.output_tokens}")
            lines.append(f"  Duration: {log.duration_ms}ms")

            if log.response_content:
                preview = str(log.response_content)[:200].replace("\n", " ")
                lines.append(f"  Response: {preview}...")

            if log.response_tool_calls:
                for tc in log.response_tool_calls:
                    lines.append(f"  Tool: {tc.get('name')}({tc.get('args')})")

            if log.error:
                lines.append(f"  ERROR: {log.error}")
        lines.append("")

    return "\n".join(lines)


def show_recent_traces(db, limit: int = 20) -> None:
    """Show recent traces for discovery."""
    from zerg.models.models import AgentRun

    # Get recent runs with trace_id set
    runs = (
        db.query(AgentRun)
        .filter(AgentRun.trace_id.isnot(None))
        .order_by(AgentRun.created_at.desc())
        .limit(limit)
        .all()
    )

    print(f"Recent traces (last {limit}):")
    print("-" * 100)
    print(f"{'trace_id':<40} {'run_id':<8} {'status':<12} {'model':<20} {'started_at'}")
    print("-" * 100)

    for run in runs:
        trace_str = str(run.trace_id)[:36] if run.trace_id else "N/A"
        status = run.status.value if run.status else "N/A"
        model = (run.model or "N/A")[:18]
        started = run.started_at.strftime("%Y-%m-%d %H:%M:%S") if run.started_at else "N/A"
        print(f"{trace_str:<40} {run.id:<8} {status:<12} {model:<20} {started}")


def main():
    parser = argparse.ArgumentParser(
        description="Debug a trace by showing the full timeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("trace_id", nargs="?", help="Trace ID (UUID) to debug")
    parser.add_argument(
        "--level",
        choices=["summary", "full", "errors"],
        default="summary",
        help="Detail level: summary (default), full, or errors",
    )
    parser.add_argument(
        "--recent",
        action="store_true",
        help="Show recent traces instead of debugging a specific one",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON instead of formatted text",
    )

    args = parser.parse_args()

    # Load database
    from zerg.database import get_session_factory

    SessionLocal = get_session_factory()
    db = SessionLocal()

    try:
        if args.recent:
            show_recent_traces(db)
            return

        if not args.trace_id:
            parser.error("trace_id is required (or use --recent)")

        # Parse trace_id
        try:
            trace_uuid = uuid.UUID(args.trace_id)
        except ValueError:
            print(f"Error: Invalid trace_id format: {args.trace_id}", file=sys.stderr)
            print("Expected UUID format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx", file=sys.stderr)
            sys.exit(1)

        # Get trace data
        data = get_trace_data(db, trace_uuid)

        if not data["runs"] and not data["workers"] and not data["llm_logs"]:
            print(f"No data found for trace_id: {args.trace_id}", file=sys.stderr)
            print("\nTip: Use --recent to see available traces", file=sys.stderr)
            sys.exit(1)

        # Build timeline
        events = build_timeline(data)

        # Detect anomalies
        anomalies = detect_anomalies(data, events)

        # Output
        if args.json:
            output = {
                "trace_id": args.trace_id,
                "runs": len(data["runs"]),
                "workers": len(data["workers"]),
                "llm_calls": len(data["llm_logs"]),
                "anomalies": anomalies,
                "timeline": [
                    {
                        "timestamp": e.timestamp.isoformat(),
                        "event_type": e.event_type,
                        "source": e.source,
                        "details": e.details,
                        "is_error": e.is_error,
                    }
                    for e in events
                ],
            }
            print(json.dumps(output, indent=2, default=str))
        else:
            print(format_timeline_output(args.trace_id, data, events, anomalies, args.level))

    finally:
        db.close()


if __name__ == "__main__":
    main()
