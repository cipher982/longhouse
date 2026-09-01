"""Day export for machine worklog consumers.

The export is served from the derived search-v2 projection: the route asks
catalogd for projector lag, pages the day out of searchd under one snapshot, and
never opens the archive database. So the tests that seed data provision a real
live catalog, ship transcripts into it, drain the real projector, and read the
day back through the route.
"""

from __future__ import annotations

import os
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from tests_lite.live_catalog_harness import LiveCatalog  # noqa: E402
from tests_lite.live_catalog_harness import live_catalog  # noqa: E402,F401
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: E402,F401
from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.dependencies.request_db import no_request_db
from zerg.main import api_app
from zerg.services.worklog_day_export import WORKLOG_DAY_MESSAGE_SQL

DEVICE_ID = "worklog-day"


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
    api_app.dependency_overrides[no_request_db] = override_db
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    return TestClient(api_app), factory


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _worklog_owner(live: LiveCatalog) -> tuple[int, str]:
    owner_id = live.create_user("owner@worklog.test")
    return owner_id, live.create_device_token(owner_id=owner_id, device_id=DEVICE_ID)


def test_worklog_day_export_returns_window_sessions_and_messages(live_catalog, live_catalog_client):  # noqa: F811
    """The day window bounds the export on both the session and message sides."""
    owner_id, token = _worklog_owner(live_catalog)
    live_catalog.commit_session(
        owner_id=owner_id,
        texts=("start the work", "made progress"),
        now=_dt("2026-07-07T16:00:00Z"),
    )
    # 01:00 the next morning in America/New_York, so past the window's end.
    live_catalog.commit_session(
        owner_id=owner_id,
        texts=("outside",),
        now=_dt("2026-07-08T05:00:00Z"),
    )
    live_catalog.index_search()

    response = live_catalog_client.get(
        "/agents/worklog/day?date=2026-07-07&timezone=America/New_York",
        headers={"X-Agents-Token": token},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["source"] == "longhouse-worklog-search-v2"
    assert payload["stats"] == {"session_count": 1, "message_count": 2, "event_count": 2}
    assert payload["sessions"][0]["message_count"] == 2
    assert payload["sessions"][0]["event_count"] == 2
    assert [event["content_text"] for event in payload["events"]] == ["start the work", "made progress"]


def test_worklog_day_export_empty_day_and_dst_window(live_catalog, live_catalog_client):  # noqa: F811
    """A day with nothing in it still reports the timezone's real boundaries."""
    _owner_id, token = _worklog_owner(live_catalog)

    response = live_catalog_client.get(
        "/agents/worklog/day?date=2026-03-08&timezone=America/New_York",
        headers={"X-Agents-Token": token},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["sessions"] == []
    assert payload["events"] == []
    assert payload["window_start"] == "2026-03-08T00:00:00-05:00"
    assert payload["window_end"] == "2026-03-09T00:00:00-04:00"


def test_worklog_day_export_rejects_invalid_timezone(live_catalog, live_catalog_client):  # noqa: F811
    _owner_id, token = _worklog_owner(live_catalog)

    response = live_catalog_client.get(
        "/agents/worklog/day?date=2026-07-07&timezone=Nope/Nowhere",
        headers={"X-Agents-Token": token},
    )

    assert response.status_code == 400
    assert "Unknown timezone" in response.json()["detail"]


def test_worklog_day_export_drops_test_sessions_by_default(live_catalog, live_catalog_client):  # noqa: F811
    """Environment is carried by the shipped envelope, and it decides visibility.

    The envelope is what a Machine Agent puts on the wire, so setting the
    session's environment on it is how a test-scoped session actually comes to
    exist -- and the search projection is where the include_test predicate runs.
    """
    owner_id, token = _worklog_owner(live_catalog)
    envelope = live_catalog.envelope_body(
        session_id=uuid4(),
        device_id=DEVICE_ID,
        texts=("test noise",),
        now=_dt("2026-07-07T16:00:00Z"),
    )
    envelope["session"]["environment"] = "test"
    shipped = live_catalog_client.post(
        "/agents/storage/v2/envelopes",
        json=envelope,
        headers={"X-Agents-Token": token, "X-Longhouse-Storage-Lane": "live"},
    )
    assert shipped.status_code == 200, shipped.text
    live_catalog.index_search()

    default_response = live_catalog_client.get(
        "/agents/worklog/day?date=2026-07-07&timezone=America/New_York",
        headers={"X-Agents-Token": token},
    )
    include_response = live_catalog_client.get(
        "/agents/worklog/day?date=2026-07-07&timezone=America/New_York&include_test=true",
        headers={"X-Agents-Token": token},
    )

    assert default_response.status_code == 200, default_response.text
    assert default_response.json()["stats"]["session_count"] == 0
    assert include_response.status_code == 200, include_response.text
    assert include_response.json()["stats"]["session_count"] == 1


def test_worklog_day_live_catalog_uses_search_projection_without_cold_fallback(tmp_path, monkeypatch):
    client, _factory = _make_client(tmp_path)
    api_app.dependency_overrides.pop(no_request_db)

    class FakeCatalog:
        async def call(self, method, params):
            assert method == "projector.state.list_lag.v2"
            assert params["projector"] == "search-v2"
            return {"lag_count": 1, "indexed_through": "8", "commit_seq": "10", "states": [{}]}

    class FakeSearch:
        def __init__(self):
            self.calls = []
            self.snapshot_id = "55555555-5555-4555-8555-555555555555"

        async def call(self, method, params, **kwargs):
            if method == "worklog.snapshot.release.v2":
                assert params == {"snapshot_id": self.snapshot_id, "owner_id": "1"}
                return {"released": True}
            assert method == "worklog.day.v2"
            assert kwargs == {"timeout_seconds": 5.0}
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
    import zerg.database as zerg_database
    import zerg.routers.agents_sessions as route_module

    monkeypatch.setattr(
        zerg_database,
        "get_session_factory",
        lambda: (_ for _ in ()).throw(AssertionError("cold database factory opened")),
    )
    monkeypatch.setattr(route_module, "get_catalogd_client", lambda: FakeCatalog())
    monkeypatch.setattr(route_module, "get_searchd_client", lambda: search)
    # There is no cold fallback left to guard against by name: the route no
    # longer imports build_worklog_day_export at all. The archive factory guard
    # above is what still proves nothing reaches for it.
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
