"""Trace debugging service for unified trace timeline analysis.

Provides trace_id-based debugging across runs, commis_jobs, and llm_audit_log tables.
This service extracts the logic from scripts/debug_trace.py into a reusable service class
for API exposure.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Any

from sqlalchemy.orm import Session

# Secret patterns to redact
SECRET_PATTERNS = [
    r"(api[_-]?key[\"']?\s*[:=]\s*[\"']?)([^\"'\s,}]+)",
    r"(password[\"']?\s*[:=]\s*[\"']?)([^\"'\s,}]+)",
    r"(secret[\"']?\s*[:=]\s*[\"']?)([^\"'\s,}]+)",
    r"(token[\"']?\s*[:=]\s*[\"']?)([^\"'\s,}]+)",
    r"(bearer\s+)([A-Za-z0-9\-_]+\.?[A-Za-z0-9\-_]*\.?[A-Za-z0-9\-_]*)",
    r"(authorization[\"']?\s*[:=]\s*[\"']?)([^\"'\s,}]+)",
]


@dataclass
class TimelineEvent:
    """A single event in the trace timeline."""

    timestamp: datetime
    event_type: str
    source: str  # 'run', 'commis', 'llm'
    details: dict[str, Any]
    is_error: bool = False
    duration_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "event_type": self.event_type,
            "source": self.source,
            "details": self.details,
            "is_error": self.is_error,
            "duration_ms": self.duration_ms,
        }


class TraceDebugger:
    """Service for debugging traces across all tables."""

    def __init__(self, db: Session):
        self.db = db

    def _get_trace_data(self, trace_id: uuid.UUID, max_items: int = 100) -> dict:
        """Query all tables for a given trace_id with limits to prevent memory issues."""
        from zerg.models.llm_audit import LLMAuditLog
        from zerg.models.models import CommisJob
        from zerg.models.models import Run

        # Get runs with this trace, ordered by created_at desc to get most recent on long traces
        runs = self.db.query(Run).filter(Run.trace_id == trace_id).order_by(Run.created_at.desc()).limit(max_items).all()

        # Get commis jobs with this trace, ordered by created_at desc
        commis = (
            self.db.query(CommisJob).filter(CommisJob.trace_id == trace_id).order_by(CommisJob.created_at.desc()).limit(max_items).all()
        )

        # Get LLM audit logs with this trace, ordered by created_at desc
        llm_logs = (
            self.db.query(LLMAuditLog)
            .filter(LLMAuditLog.trace_id == trace_id)
            .order_by(LLMAuditLog.created_at.desc())
            .limit(max_items)
            .all()
        )

        return {
            "runs": runs,
            "commis": commis,
            "llm_logs": llm_logs,
        }

    def _build_timeline(self, data: dict) -> list[TimelineEvent]:
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
                        event_type="oikos.run.started",
                        source="run",
                        details={
                            "run_id": run.id,
                            "fiche_id": run.fiche_id,
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
                        event_type=f"oikos.run.{run.status.value if run.status else 'finished'}",
                        source="run",
                        details={
                            "run_id": run.id,
                            "duration_ms": run.duration_ms,
                            "total_tokens": run.total_tokens,
                            "error": run.error[:100] if run.error else None,
                        },
                        is_error=is_error,
                        duration_ms=run.duration_ms,
                    )
                )

        # Add commis events
        for commis in data["commis"]:
            # Commis created/queued
            if commis.created_at:
                ts = commis.created_at.replace(tzinfo=timezone.utc) if commis.created_at.tzinfo is None else commis.created_at
                events.append(
                    TimelineEvent(
                        timestamp=ts,
                        event_type="commis.spawned",
                        source="commis",
                        details={
                            "job_id": commis.id,
                            "task": commis.task[:50] if commis.task else None,
                            "model": commis.model,
                            "status": commis.status,
                        },
                    )
                )

            # Commis started
            if commis.started_at:
                ts = commis.started_at.replace(tzinfo=timezone.utc) if commis.started_at.tzinfo is None else commis.started_at
                events.append(
                    TimelineEvent(
                        timestamp=ts,
                        event_type="commis.started",
                        source="commis",
                        details={
                            "job_id": commis.id,
                            "commis_id": commis.commis_id,
                        },
                    )
                )

            # Commis completed
            if commis.finished_at:
                ts = commis.finished_at.replace(tzinfo=timezone.utc) if commis.finished_at.tzinfo is None else commis.finished_at
                is_error = commis.status in ("failed", "error")
                # Compute duration from timestamps (normalize both to UTC to avoid mixed tz issues)
                duration_ms = None
                if commis.started_at and commis.finished_at:
                    started = commis.started_at.replace(tzinfo=timezone.utc) if commis.started_at.tzinfo is None else commis.started_at
                    finished = commis.finished_at.replace(tzinfo=timezone.utc) if commis.finished_at.tzinfo is None else commis.finished_at
                    delta = finished - started
                    duration_ms = int(delta.total_seconds() * 1000)
                events.append(
                    TimelineEvent(
                        timestamp=ts,
                        event_type=f"commis.{commis.status}" if commis.status else "commis.finished",
                        source="commis",
                        details={
                            "job_id": commis.id,
                            "duration_ms": duration_ms,
                            "error": commis.error[:100] if commis.error else None,
                        },
                        is_error=is_error,
                        duration_ms=duration_ms,
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
                        duration_ms=log.duration_ms,
                    )
                )

        # Sort by timestamp
        events.sort(key=lambda e: e.timestamp)
        return events

    def _detect_anomalies(self, data: dict, events: list[TimelineEvent]) -> list[str]:
        """Detect common issues in the trace."""
        anomalies = []

        # Check for failed runs
        for run in data["runs"]:
            if run.status and run.status.value == "failed":
                anomalies.append(f"Run {run.id} FAILED: {run.error or 'no error message'}")

        # Check for failed commis
        for commis in data["commis"]:
            if commis.status == "failed":
                anomalies.append(f"Commis {commis.id} FAILED: {commis.error or 'no error message'}")

        # Check for LLM errors
        for log in data["llm_logs"]:
            if log.error:
                anomalies.append(f"LLM error in {log.phase}: {log.error}")

        # Check for stuck commis (created but never started)
        for commis in data["commis"]:
            if commis.created_at and not commis.started_at and commis.status not in ("cancelled", "failed"):
                anomalies.append(f"Commis {commis.id} never started (status: {commis.status})")

        # Check for very long LLM calls (>60s)
        for log in data["llm_logs"]:
            if log.duration_ms and log.duration_ms > 60000:
                anomalies.append(f"Slow LLM call: {log.phase} took {log.duration_ms / 1000:.1f}s")

        return anomalies

    def redact_secrets(self, data: dict[str, Any]) -> dict[str, Any]:
        """Redact sensitive data from trace output.

        Scans tool outputs and details for patterns like api_key, password, secret, token.
        """

        def redact_string(s: str) -> str:
            """Redact secrets from a string."""
            result = s
            for pattern in SECRET_PATTERNS:
                result = re.sub(pattern, r"\1[REDACTED]", result, flags=re.IGNORECASE)
            return result

        def redact_value(value: Any) -> Any:
            """Recursively redact secrets from any value."""
            if isinstance(value, str):
                return redact_string(value)
            elif isinstance(value, dict):
                return {k: redact_value(v) for k, v in value.items()}
            elif isinstance(value, list):
                return [redact_value(item) for item in value]
            else:
                return value

        return redact_value(data)

    def get_trace(self, trace_id: str, level: str = "summary", max_events: int = 100, max_items: int = 100) -> dict[str, Any] | None:
        """Get unified trace timeline.

        Args:
            trace_id: UUID of the trace
            level: Detail level - 'summary', 'full', or 'errors'
            max_events: Maximum number of events to return (pagination)

        Returns:
            Trace timeline data or None if not found
        """
        try:
            trace_uuid = uuid.UUID(trace_id)
        except ValueError:
            return None

        # Use max of max_events and max_items for DB query to ensure we can return requested events
        query_limit = max(max_events, max_items)
        data = self._get_trace_data(trace_uuid, max_items=query_limit)

        if not data["runs"] and not data["commis"] and not data["llm_logs"]:
            return None

        # Build timeline
        events = self._build_timeline(data)

        # Filter for errors only if requested
        if level == "errors":
            events = [e for e in events if e.is_error]

        # Apply pagination limit
        events = events[:max_events]

        # Detect anomalies
        anomalies = self._detect_anomalies(data, events)

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

        result = {
            "trace_id": trace_id,
            "status": overall_status,
            "started_at": start_time.isoformat() if start_time else None,
            "duration_seconds": duration,
            "counts": {
                "runs": len(data["runs"]),
                "commis": len(data["commis"]),
                "llm_calls": len(data["llm_logs"]),
            },
            "anomalies": anomalies,
            "timeline": [e.to_dict() for e in events],
        }

        # Add LLM details if full level requested
        if level == "full":
            result["llm_details"] = []
            for log in data["llm_logs"]:
                detail = {
                    "phase": log.phase,
                    "model": log.model,
                    "message_count": log.message_count,
                    "input_tokens": log.input_tokens,
                    "output_tokens": log.output_tokens,
                    "duration_ms": log.duration_ms,
                    "error": log.error,
                }
                if log.response_content:
                    detail["response_preview"] = str(log.response_content)[:200]
                if log.response_tool_calls:
                    detail["tool_calls"] = [{"name": tc.get("name"), "args": tc.get("args")} for tc in log.response_tool_calls]
                result["llm_details"].append(detail)

        return result

    def list_recent(self, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        """List recent traces for discovery.

        Args:
            limit: Maximum number of traces to return
            offset: Number of traces to skip

        Returns:
            List of recent traces with basic info
        """
        from zerg.models.models import Run

        # Get recent runs with trace_id set
        runs = self.db.query(Run).filter(Run.trace_id.isnot(None)).order_by(Run.created_at.desc()).offset(offset).limit(limit).all()

        traces = []
        for run in runs:
            traces.append(
                {
                    "trace_id": str(run.trace_id) if run.trace_id else None,
                    "run_id": run.id,
                    "status": run.status.value if run.status else None,
                    "model": run.model,
                    "started_at": run.started_at.isoformat() if run.started_at else None,
                    "duration_ms": run.duration_ms,
                }
            )

        return {
            "traces": traces,
            "limit": limit,
            "offset": offset,
            "count": len(traces),
        }
