"""Query-less recent-sessions listing on GET /agents/sessions.

The MCP search_sessions tool forwards here. Omitting the query (or sending a
blank one) must list recent sessions ordered by last activity, honoring the
project/provider/days_back/limit filters, with no match snippet or score.
A real query must keep the existing content-search behavior.
"""

from __future__ import annotations

import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.database import get_db
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.main import api_app
from zerg.services.agents import AgentsStore
from zerg.services.agents import EventIngest
from zerg.services.agents import SessionIngest


def _make_db(tmp_path, name: str):
    db_path = tmp_path / f"{name}.db"
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)
    return make_sessionmaker(engine)


def _ingest(
    store: AgentsStore,
    *,
    provider: str = "claude",
    project: str = "zerg",
    content: str = "hello there",
    active_at: datetime,
) -> str:
    result = store.ingest_session(
        SessionIngest(
            provider=provider,
            environment="development",
            project=project,
            device_id="cinder",
            cwd=f"/tmp/{project}",
            git_repo=None,
            git_branch=None,
            started_at=active_at - timedelta(minutes=5),
            events=[
                EventIngest(
                    role="user",
                    content_text=content,
                    timestamp=active_at,
                    source_path=f"/tmp/{project}/session.jsonl",
                    source_offset=0,
                )
            ],
        )
    )
    return str(result.session_id)


def _make_client(db_session) -> TestClient:
    def override_db():
        yield db_session

    api_app.dependency_overrides[get_db] = override_db
    return TestClient(api_app)


def test_queryless_listing_returns_recent_sessions_ordered_by_activity(tmp_path):
    session_local = _make_db(tmp_path, "queryless_order")
    now = datetime.now(timezone.utc)

    with session_local() as db:
        store = AgentsStore(db)
        older = _ingest(store, active_at=now - timedelta(days=2))
        newest = _ingest(store, active_at=now - timedelta(hours=1))
        middle = _ingest(store, active_at=now - timedelta(days=1))
        client = _make_client(db)

        try:
            response = client.get("/agents/sessions")
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["total"] == 3
            assert [s["id"] for s in payload["sessions"]] == [newest, middle, older]
            for session in payload["sessions"]:
                assert session["match_snippet"] is None
                assert session["match_score"] is None
        finally:
            api_app.dependency_overrides.clear()


def test_blank_query_is_treated_as_absent(tmp_path):
    session_local = _make_db(tmp_path, "queryless_blank")
    now = datetime.now(timezone.utc)

    with session_local() as db:
        store = AgentsStore(db)
        older = _ingest(store, active_at=now - timedelta(days=1))
        newer = _ingest(store, active_at=now - timedelta(hours=1))
        client = _make_client(db)

        try:
            for blank in ("", "   "):
                response = client.get("/agents/sessions", params={"query": blank})
                assert response.status_code == 200, response.text
                payload = response.json()
                # An FTS search for "" would return zero sessions; the listing
                # path returns the full recent-ordered corpus.
                assert payload["total"] == 2
                assert [s["id"] for s in payload["sessions"]] == [newer, older]
                assert all(s["match_snippet"] is None for s in payload["sessions"])
        finally:
            api_app.dependency_overrides.clear()


def test_queryless_listing_honors_filters(tmp_path):
    session_local = _make_db(tmp_path, "queryless_filters")
    now = datetime.now(timezone.utc)

    with session_local() as db:
        store = AgentsStore(db)
        zerg_claude = _ingest(store, project="zerg", provider="claude", active_at=now - timedelta(hours=1))
        zerg_codex = _ingest(store, project="zerg", provider="codex", active_at=now - timedelta(hours=2))
        other_project = _ingest(store, project="g55", provider="claude", active_at=now - timedelta(hours=3))
        stale = _ingest(store, project="zerg", provider="claude", active_at=now - timedelta(days=30))
        client = _make_client(db)

        try:
            response = client.get("/agents/sessions", params={"project": "zerg"})
            assert response.status_code == 200, response.text
            ids = [s["id"] for s in response.json()["sessions"]]
            assert ids == [zerg_claude, zerg_codex]
            assert other_project not in ids
            assert stale not in ids  # outside default days_back=14

            response = client.get("/agents/sessions", params={"provider": "codex"})
            assert [s["id"] for s in response.json()["sessions"]] == [zerg_codex]

            response = client.get("/agents/sessions", params={"days_back": 60, "project": "zerg"})
            assert [s["id"] for s in response.json()["sessions"]] == [zerg_claude, zerg_codex, stale]

            response = client.get("/agents/sessions", params={"limit": 2})
            payload = response.json()
            assert [s["id"] for s in payload["sessions"]] == [zerg_claude, zerg_codex]
            assert payload["total"] == 3
        finally:
            api_app.dependency_overrides.clear()


def test_query_search_behavior_unchanged(tmp_path):
    session_local = _make_db(tmp_path, "queryless_search")
    now = datetime.now(timezone.utc)

    with session_local() as db:
        store = AgentsStore(db)
        match = _ingest(store, content="rotating refresh tokens shipped", active_at=now - timedelta(days=1))
        _ingest(store, content="unrelated timeline work", active_at=now - timedelta(hours=1))
        client = _make_client(db)

        try:
            response = client.get("/agents/sessions", params={"query": "refresh tokens"})
            assert response.status_code == 200, response.text
            payload = response.json()
            assert [s["id"] for s in payload["sessions"]] == [match]
            assert payload["sessions"][0]["match_snippet"]
            assert "refresh" in payload["sessions"][0]["match_snippet"].lower()
        finally:
            api_app.dependency_overrides.clear()
