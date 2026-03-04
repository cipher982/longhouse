from datetime import datetime
from datetime import timezone

from fastapi.testclient import TestClient

from zerg.database import get_db
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest


def _ts(second: int) -> datetime:
    return datetime(2026, 1, 1, 0, 0, second, tzinfo=timezone.utc)


def _make_db(tmp_path):
    db_path = tmp_path / "sessions_context_mode.db"
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)
    return make_sessionmaker(engine)


def _get_client(session_factory):
    from zerg.main import api_app

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    api_app.dependency_overrides[get_db] = override_get_db
    client = TestClient(api_app)
    yield client
    api_app.dependency_overrides.clear()


def _seed_compacted_session(factory):
    db = factory()
    try:
        store = AgentsStore(db)
        source_path = "/tmp/context-mode-session.jsonl"
        store.ingest_session(
            SessionIngest(
                provider="claude",
                environment="production",
                project="zerg",
                device_id="dev-machine",
                cwd="/tmp",
                git_repo=None,
                git_branch=None,
                started_at=_ts(0),
                events=[
                    EventIngest(
                        role="user",
                        content_text="My favorite color is yellow.",
                        timestamp=_ts(1),
                        source_path=source_path,
                        source_offset=100,
                        raw_json='{"type":"user","timestamp":"2026-01-01T00:00:01Z"}',
                    ),
                    EventIngest(
                        role="assistant",
                        content_text="Noted.",
                        timestamp=_ts(2),
                        source_path=source_path,
                        source_offset=200,
                        raw_json='{"type":"assistant","timestamp":"2026-01-01T00:00:02Z"}',
                    ),
                    EventIngest(
                        role="system",
                        content_text="Conversation compacted [trigger=auto]",
                        timestamp=_ts(3),
                        source_path=source_path,
                        source_offset=300,
                        raw_json='{"type":"system","subtype":"compact_boundary","timestamp":"2026-01-01T00:00:03Z"}',
                    ),
                    EventIngest(
                        role="user",
                        content_text="Please continue the migration.",
                        timestamp=_ts(4),
                        source_path=source_path,
                        source_offset=400,
                        raw_json='{"type":"user","timestamp":"2026-01-01T00:00:04Z"}',
                    ),
                    EventIngest(
                        role="assistant",
                        content_text="Continuing migration now.",
                        timestamp=_ts(5),
                        source_path=source_path,
                        source_offset=500,
                        raw_json='{"type":"assistant","timestamp":"2026-01-01T00:00:05Z"}',
                    ),
                    # Rewind-like stale row after compaction: newer timestamp, older offset.
                    EventIngest(
                        role="user",
                        content_text="yellow stale rewind branch",
                        timestamp=_ts(6),
                        source_path=source_path,
                        source_offset=150,
                        raw_json='{"type":"user","timestamp":"2026-01-01T00:00:06Z"}',
                    ),
                ],
                source_lines=[],
            )
        )
    finally:
        db.close()


def test_sessions_query_context_mode_filters_pre_compaction_matches(tmp_path):
    factory = _make_db(tmp_path)
    _seed_compacted_session(factory)

    for client in _get_client(factory):
        forensic = client.get(
            "/agents/sessions",
            params={"query": "yellow", "days_back": 90, "context_mode": "forensic"},
        )
        assert forensic.status_code == 200
        forensic_data = forensic.json()
        assert forensic_data["total"] == 1
        assert "yellow" in (forensic_data["sessions"][0]["match_snippet"] or "").lower()

        active = client.get(
            "/agents/sessions",
            params={"query": "yellow", "days_back": 90, "context_mode": "active_context"},
        )
        assert active.status_code == 200
        active_data = active.json()
        assert active_data["total"] == 0


def test_context_mode_validation_on_search_endpoints(tmp_path):
    factory = _make_db(tmp_path)

    for client in _get_client(factory):
        bad_sessions = client.get("/agents/sessions", params={"query": "test", "context_mode": "bad"})
        assert bad_sessions.status_code == 400
        assert "context_mode" in bad_sessions.json()["detail"]

        bad_semantic = client.get("/agents/sessions/semantic", params={"query": "test", "context_mode": "bad"})
        assert bad_semantic.status_code == 400
        assert "context_mode" in bad_semantic.json()["detail"]

        bad_recall = client.get("/agents/recall", params={"query": "test", "context_mode": "bad"})
        assert bad_recall.status_code == 400
        assert "context_mode" in bad_recall.json()["detail"]
