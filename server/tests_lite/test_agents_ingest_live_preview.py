"""Hot-path live preview extraction for /agents/ingest."""

from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from uuid import uuid4

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.routers.agents_ingest import _build_live_ingest_transcript_preview
from zerg.services.agents.models import EventIngest
from zerg.services.agents.models import SessionIngest


def _ingest_with_events(events: list[EventIngest]) -> SessionIngest:
    return SessionIngest(
        id=uuid4(),
        provider="claude",
        environment="production",
        project="test",
        started_at=datetime.now(timezone.utc),
        events=events,
    )


def _live_trace() -> dict:
    return {
        "schema": "ship_trace.v1",
        "work_context": "live_transcript",
        "trace_id": "trace-1",
        "new_offset": 2048,
    }


def test_build_live_ingest_transcript_preview_uses_latest_assistant_text():
    ts = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    payload = _ingest_with_events(
        [
            EventIngest(
                role="assistant",
                content_text="  final answer  ",
                timestamp=ts,
                source_path="/tmp/session.jsonl",
                source_offset=1024,
            )
        ]
    )

    preview = _build_live_ingest_transcript_preview(payload, _live_trace())

    assert preview == {
        "event_id": 1024,
        "text": "final answer",
        "event_origin": "live_provisional",
        "timestamp": "2026-05-22T12:00:00Z",
        "is_provisional": True,
        "is_complete": True,
        "content_cursor": "ingest-live:trace-1:1024",
        "is_stale": False,
        "stale_reason": None,
    }


def test_build_live_ingest_transcript_preview_ignores_non_live_or_non_text_events():
    ts = datetime.now(timezone.utc)
    assistant_tool = _ingest_with_events(
        [
            EventIngest(
                role="assistant",
                tool_name="Bash",
                content_text="tool preamble",
                timestamp=ts,
                source_offset=1,
            )
        ]
    )
    latest_user = _ingest_with_events(
        [
            EventIngest(
                role="user",
                content_text="hello",
                timestamp=ts,
                source_offset=1,
            )
        ]
    )
    replay_trace = {**_live_trace(), "work_context": "reconciliation_scan"}

    assert _build_live_ingest_transcript_preview(assistant_tool, _live_trace()) is None
    assert _build_live_ingest_transcript_preview(latest_user, _live_trace()) is None
    assert _build_live_ingest_transcript_preview(latest_user, replay_trace) is None
