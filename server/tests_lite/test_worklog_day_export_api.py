from __future__ import annotations

import os
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.main import api_app
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionThread
from zerg.routers.agents_sessions import _session_detail_db
from zerg.services.worklog_day_export import WORKLOG_DAY_MESSAGE_SQL


def _make_client(tmp_path):
    db_path = tmp_path / "worklog_day.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    factory = make_sessionmaker(engine)

    def override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    def override_verify_agents_token():
        return SimpleNamespace(device_id="worklog-day", id="token-1", owner_id=1)

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[_session_detail_db] = override_db
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    return TestClient(api_app), factory


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _seed_session(
    factory,
    *,
    session_id: str,
    started_at: str,
    environment: str = "production",
    branch_kind: str = "root",
    project: str = "longhouse",
    event_specs: list[tuple[str, str | None, str]] | None = None,
) -> None:
    db = factory()
    try:
        session = AgentSession(
            id=session_id,
            provider="codex",
            environment=environment,
            project=project,
            device_id="test-device",
            cwd=f"/tmp/{project}",
            git_repo=f"https://github.com/cipher982/{project}.git",
            started_at=_dt(started_at),
            user_messages=1,
            assistant_messages=1,
            tool_calls=1,
        )
        db.add(session)
        db.flush()
        thread = SessionThread(
            id=uuid4(),
            session_id=session.id,
            provider=session.provider,
            branch_kind=branch_kind,
            is_primary=1,
        )
        db.add(thread)
        session.primary_thread_id = thread.id
        db.flush()
        for index, (role, content_text, timestamp) in enumerate(event_specs or []):
            db.add(
                AgentEvent(
                    session_id=session.id,
                    role=role,
                    content_text=content_text,
                    timestamp=_dt(timestamp),
                    source_path=f"/tmp/{session_id}.jsonl",
                    source_offset=index,
                    event_hash=f"{session_id}-{index}",
                )
            )
        db.commit()
    finally:
        db.close()


def test_worklog_day_export_returns_window_sessions_and_messages(tmp_path):
    client, factory = _make_client(tmp_path)
    try:
        _seed_session(
            factory,
            session_id="11111111-1111-4111-8111-111111111111",
            started_at="2026-07-07T12:00:00Z",
            event_specs=[
                ("user", "start the work", "2026-07-07T12:00:00Z"),
                ("assistant", "made progress", "2026-07-07T12:01:00Z"),
                ("tool", None, "2026-07-07T12:02:00Z"),
                ("user", "outside", "2026-07-08T05:00:00Z"),
            ],
        )

        response = client.get(
            "/agents/worklog/day?date=2026-07-07&timezone=America/New_York",
            headers={"X-Agents-Token": "dev"},
        )

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["source"] == "longhouse-worklog-api-v1"
        assert payload["stats"] == {"session_count": 1, "message_count": 2, "event_count": 3}
        assert payload["sessions"][0]["message_count"] == 2
        assert payload["sessions"][0]["event_count"] == 3
        assert [event["content_text"] for event in payload["events"]] == ["start the work", "made progress"]
    finally:
        api_app.dependency_overrides.clear()


def test_worklog_day_export_empty_day_and_dst_window(tmp_path):
    client, _factory = _make_client(tmp_path)
    try:
        response = client.get(
            "/agents/worklog/day?date=2026-03-08&timezone=America/New_York",
            headers={"X-Agents-Token": "dev"},
        )

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["sessions"] == []
        assert payload["events"] == []
        assert payload["window_start"] == "2026-03-08T00:00:00-05:00"
        assert payload["window_end"] == "2026-03-09T00:00:00-04:00"
    finally:
        api_app.dependency_overrides.clear()


def test_worklog_day_export_rejects_invalid_timezone(tmp_path):
    client, _factory = _make_client(tmp_path)
    try:
        response = client.get(
            "/agents/worklog/day?date=2026-07-07&timezone=Nope/Nowhere",
            headers={"X-Agents-Token": "dev"},
        )

        assert response.status_code == 400
        assert "Unknown timezone" in response.json()["detail"]
    finally:
        api_app.dependency_overrides.clear()


def test_worklog_day_export_drops_test_sessions_by_default(tmp_path):
    client, factory = _make_client(tmp_path)
    try:
        _seed_session(
            factory,
            session_id="22222222-2222-4222-8222-222222222222",
            started_at="2026-07-07T12:00:00Z",
            environment="test",
            event_specs=[("user", "test noise", "2026-07-07T12:00:00Z")],
        )

        default_response = client.get(
            "/agents/worklog/day?date=2026-07-07&timezone=America/New_York",
            headers={"X-Agents-Token": "dev"},
        )
        include_response = client.get(
            "/agents/worklog/day?date=2026-07-07&timezone=America/New_York&include_test=true",
            headers={"X-Agents-Token": "dev"},
        )

        assert default_response.status_code == 200, default_response.text
        assert default_response.json()["stats"]["session_count"] == 0
        assert include_response.status_code == 200, include_response.text
        assert include_response.json()["stats"]["session_count"] == 1
    finally:
        api_app.dependency_overrides.clear()


def test_worklog_day_export_derives_sidechain_from_session_thread(tmp_path):
    client, factory = _make_client(tmp_path)
    try:
        _seed_session(
            factory,
            session_id="33333333-3333-4333-8333-333333333333",
            started_at="2026-07-07T12:00:00Z",
            branch_kind="subagent",
            event_specs=[("user", "subagent work", "2026-07-07T12:00:00Z")],
        )

        response = client.get(
            "/agents/worklog/day?date=2026-07-07&timezone=America/New_York",
            headers={"X-Agents-Token": "dev"},
        )

        assert response.status_code == 200, response.text
        assert response.json()["sessions"][0]["is_sidechain"] is True
    finally:
        api_app.dependency_overrides.clear()


def test_worklog_day_live_catalog_uses_search_projection_without_cold_fallback(tmp_path, monkeypatch):
    client, _factory = _make_client(tmp_path)
    api_app.dependency_overrides.pop(_session_detail_db)

    class FakeCatalog:
        async def call(self, method, params):
            assert method == "projector.state.list_lag.v2"
            assert params["projector"] == "search-v2"
            return {"lag_count": 1, "indexed_through": "8", "commit_seq": "10", "states": [{}]}

    class FakeSearch:
        def __init__(self):
            self.calls = []
            self.snapshot_id = "55555555-5555-4555-8555-555555555555"

        async def call(self, method, params):
            if method == "worklog.snapshot.release.v2":
                assert params == {"snapshot_id": self.snapshot_id, "owner_id": "1"}
                return {"released": True}
            assert method == "worklog.day.v2"
            self.calls.append(params)
            assert params["offset"] == 0
            if params["section"] == "sessions":
                assert params["snapshot_id"] is None
                return {
                    "items": [
                        {
                            "session_id": "44444444-4444-4444-8444-444444444444",
                            "project": "longhouse",
                            "provider": "codex",
                            "cwd": "/workspace/longhouse",
                            "git_repo": "cipher982/longhouse",
                            "started_at": "2026-07-07T12:00:00+00:00",
                            "user_messages": 3,
                            "assistant_messages": 2,
                            "tool_calls": 1,
                            "is_sidechain": 0,
                            "first_event_us": 1_783_426_400_000_000,
                            "last_event_us": 1_783_426_460_000_000,
                            "first_message_us": 1_783_426_400_000_000,
                            "message_count": 2,
                            "day_event_count": 3,
                        }
                    ],
                    "has_more": False,
                    "next_offset": None,
                    "snapshot_id": self.snapshot_id,
                }
            assert params["snapshot_id"] == self.snapshot_id
            return {
                "items": [
                    {
                        "session_id": "44444444-4444-4444-8444-444444444444",
                        "role": "user",
                        "content_text": "search projection only",
                        "order_time_us": 1_783_426_400_000_000,
                    },
                    {
                        "session_id": "44444444-4444-4444-8444-444444444444",
                        "role": "assistant",
                        "content_text": "no archive fallback",
                        "order_time_us": 1_783_426_460_000_000,
                    },
                ],
                "has_more": False,
                "next_offset": None,
                "snapshot_id": self.snapshot_id,
            }

    search = FakeSearch()
    import zerg.routers.agents_sessions as route_module

    monkeypatch.setattr(route_module.database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(
        route_module.database_module,
        "get_session_factory",
        lambda: (_ for _ in ()).throw(AssertionError("cold database factory opened")),
    )
    monkeypatch.setattr(route_module, "get_catalogd_client", lambda: FakeCatalog())
    monkeypatch.setattr(route_module, "get_searchd_client", lambda: search)
    monkeypatch.setattr(
        route_module,
        "build_worklog_day_export",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("cold worklog fallback opened")),
    )
    try:
        response = client.get(
            "/agents/worklog/day?date=2026-07-07&timezone=America/New_York",
            headers={"X-Agents-Token": "dev"},
        )

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["source"] == "longhouse-worklog-search-v2"
        assert payload["projection_lag"] is True
        assert payload["indexed_through"] == "8"
        assert payload["desired_through"] == "10"
        assert payload["stats"] == {"session_count": 1, "message_count": 2, "event_count": 3}
        assert [call["section"] for call in search.calls] == ["sessions", "events"]
    finally:
        api_app.dependency_overrides.clear()


def test_worklog_day_message_query_uses_timestamp_index(tmp_path):
    _client, factory = _make_client(tmp_path)
    db = factory()
    try:
        plan_rows = db.execute(
            text("EXPLAIN QUERY PLAN " + WORKLOG_DAY_MESSAGE_SQL),
            {
                "window_start_utc": "2026-07-07 04:00:00.000000",
                "window_end_utc": "2026-07-08 04:00:00.000000",
                "include_test": 0,
            },
        ).fetchall()
        plan = "\n".join(str(row) for row in plan_rows)

        assert "ix_events_timestamp" in plan
    finally:
        db.close()
        api_app.dependency_overrides.clear()
