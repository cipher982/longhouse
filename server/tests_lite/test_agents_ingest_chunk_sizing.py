"""Phase 5: per-label chunk sizing + commit telemetry on `ingest_session`.

Replaces the cross-request `IngestCoalescer` proposal. Codex review
showed cross-request coalescing buys near-zero locally because
`ingest_session` already commits internally, so the throughput win comes
from larger archive/replay chunks rather than a shared transaction.

These tests pin:

- `chunk_size` actually controls the commit cadence inside the loop
- the router's label → chunk-size table is what we expect
- `IngestResult` carries the new `commit_count` / `commit_ms_total`
  telemetry that the engine and dashboards key off of
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone

from sqlalchemy.orm import sessionmaker

from zerg.database import Base
from zerg.database import make_engine
from zerg.routers.agents_ingest import _ingest_chunk_for_label
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest


def _make_db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'ingest.db'}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _payload(n_events: int) -> SessionIngest:
    base = datetime(2026, 1, 31, tzinfo=timezone.utc)
    return SessionIngest(
        provider="codex",
        environment="test",
        project="zerg",
        device_id="dev-machine",
        cwd="/tmp",
        started_at=base,
        events=[
            EventIngest(
                role="user",
                content_text=f"msg-{i}",
                timestamp=base,
                source_path="/tmp/session.jsonl",
                source_offset=i,
            )
            for i in range(n_events)
        ],
    )


def test_chunk_size_controls_commit_cadence(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        # Small chunks → many intra-loop commits + the two trailing commits
        # (final ingest commit, turn-materialize commit). A 50-event payload
        # at chunk=10 should commit once per chunk = 5 chunk commits.
        result = AgentsStore(db).ingest_session(_payload(50), chunk_size=10)
        assert result.events_inserted == 50
        # 5 chunked commits + 1 trailing ingest commit + 1 turn-materialize.
        # We only assert the lower bound that proves chunking actually fired.
        assert result.commit_count >= 6
        assert result.commit_ms_total >= 0.0


def test_chunk_size_amortises_over_large_payload(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        # Large chunk: a 50-event payload should never trip the chunk
        # boundary, so we expect only the trailing commits, not 5+.
        result = AgentsStore(db).ingest_session(_payload(50), chunk_size=10_000)
        assert result.events_inserted == 50
        # Just the trailing ingest commit + the turn-materialize commit.
        assert result.commit_count <= 3


def test_default_chunk_size_matches_legacy_behavior(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        # No chunk_size → falls back to the historical 200 default. A small
        # payload must not trip any chunk boundary.
        result = AgentsStore(db).ingest_session(_payload(5))
        assert result.events_inserted == 5
        assert result.commit_count <= 3


def test_router_chunk_table_is_stable():
    # If you change these defaults, update the docket epic notes too —
    # they are load-bearing for the phase-1 bench numbers.
    assert _ingest_chunk_for_label("ingest-live") == 200
    assert _ingest_chunk_for_label("ingest") == 500
    assert _ingest_chunk_for_label("ingest-replay") == 1000
    assert _ingest_chunk_for_label("ingest-scan") == 1000
    # Unknown labels stay conservative.
    assert _ingest_chunk_for_label("does-not-exist") == 200
