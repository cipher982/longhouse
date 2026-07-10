from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from uuid import uuid4

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-long-enough")
os.environ.setdefault("INTERNAL_API_SECRET", "test-internal-secret-long-enough")
os.environ.setdefault("AUTH_DISABLED", "1")

from zerg.routers.agents_ingest import _archive_ingest_batches
from zerg.routers.agents_ingest import _merge_archive_primary_states
from zerg.routers.agents_ingest import _merge_ingest_results
from zerg.services.agents import EventIngest
from zerg.services.agents import IngestResult
from zerg.services.agents import SessionIngest
from zerg.services.agents import SourceLineIngest
from zerg.services.agents.models import SourceRewindHintIngest


def _session(n: int) -> SessionIngest:
    now = datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc)
    events = [
        EventIngest(
            role="assistant",
            content_text=f"event {idx}",
            timestamp=now,
            source_path="/tmp/session.jsonl",
            source_offset=idx,
            raw_json=f'{{"i":{idx}}}',
        )
        for idx in range(n)
    ]
    source_lines = [
        SourceLineIngest(
            source_path="/tmp/session.jsonl",
            source_offset=idx,
            raw_json=f'{{"i":{idx}}}',
        )
        for idx in range(n)
    ]
    return SessionIngest(
        id=uuid4(),
        provider="codex",
        environment="production",
        started_at=now,
        events=events,
        source_lines=source_lines,
        rewind_hints=[
            SourceRewindHintIngest(
                source_path="/tmp/session.jsonl",
                source_offset=0,
                reason="truncate",
            )
        ],
    )


def test_archive_ingest_batches_are_bounded_and_keep_rewind_on_first_batch_only():
    data = _session(5)

    batches = _archive_ingest_batches(data, max_items=2)

    assert [len(batch.events) for batch in batches] == [2, 2, 1]
    assert [len(batch.source_lines) for batch in batches] == [2, 2, 1]
    assert [len(batch.rewind_hints) for batch in batches] == [1, 0, 0]
    assert all(batch.id == data.id for batch in batches)


def test_archive_ingest_batches_preserve_source_line_only_payloads():
    data = _session(3)
    data = data.model_copy(update={"events": []})

    batches = _archive_ingest_batches(data, max_items=2)

    assert [len(batch.events) for batch in batches] == [0, 0]
    assert [len(batch.source_lines) for batch in batches] == [2, 1]


def test_merge_ingest_results_sums_counts_and_keeps_latest_event_id():
    session_id = uuid4()
    merged = _merge_ingest_results(
        [
            IngestResult(
                session_id=session_id,
                events_inserted=2,
                events_skipped=1,
                latest_inserted_event_id=10,
                session_created=True,
                commit_count=3,
                commit_ms_total=1.5,
                source_lines_inserted=2,
                store_stage_ms={"events": 1.0},
            ),
            IngestResult(
                session_id=session_id,
                events_inserted=1,
                events_skipped=4,
                latest_inserted_event_id=12,
                session_created=False,
                commit_count=2,
                commit_ms_total=2.0,
                source_lines_inserted=1,
                store_stage_ms={"events": 2.0, "source_lines": 3.0},
            ),
        ]
    )

    assert merged.session_id == session_id
    assert merged.events_inserted == 3
    assert merged.events_skipped == 5
    assert merged.latest_inserted_event_id == 12
    assert merged.session_created is True
    assert merged.commit_count == 5
    assert merged.commit_ms_total == 3.5
    assert merged.source_lines_inserted == 3
    assert merged.store_stage_ms == {"events": 3.0, "source_lines": 3.0}


def test_merge_archive_primary_states_prioritizes_written():
    assert _merge_archive_primary_states([]) == "disabled"
    assert _merge_archive_primary_states(["disabled"]) == "disabled"
    assert _merge_archive_primary_states(["written", "disabled"]) == "written"
    assert _merge_archive_primary_states(["prepared"]) == "prepared"
