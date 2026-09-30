"""Tester funnel facts: what the tenant records, what it never does, and how it is read.

Covers services/funnel_facts.py (classification, the side file), the one catalogd
read RPC behind machines and sessions per provider, and the internal route the
control plane calls. See docs/specs/first-users-gtm.md, Phase 0.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from zerg.services import funnel_facts
from zerg.services.funnel_facts import FunnelFactsStore

IOS_UA = "Longhouse/12339 CFNetwork/3860.100.1 Darwin/25.0.0"
CHROME_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"


def _scope(
    method="GET",
    path="/api/timeline/sessions",
    *,
    principal="user:1",
    user_agent=IOS_UA,
    authorization="Bearer runtime.jwt.value",
    cookie=None,
    query="",
    scope_type="http",
):
    headers = [(b"user-agent", user_agent.encode())]
    if authorization:
        headers.append((b"authorization", authorization.encode()))
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    return {
        "type": scope_type,
        "method": method,
        "path": path,
        "query_string": query.encode(),
        "headers": headers,
        "state": {"principal": principal} if principal else {},
    }


# ---------------------------------------------------------------------------
# Classification: only a signed-in person's successful request counts
# ---------------------------------------------------------------------------


class TestClassify:
    def test_the_iphone_app_is_a_phone_view(self):
        assert funnel_facts.classify(_scope(), 200) == ("ios", ["phone_view"])

    def test_the_explicit_ios_agent_string_counts_too(self):
        assert funnel_facts.classify(_scope(user_agent="Longhouse-iOS"), 200)[0] == "ios"

    def test_a_browser_with_a_session_cookie_is_a_web_view(self):
        scope = _scope(user_agent=CHROME_UA, authorization=None, cookie="longhouse_session=abc")
        assert funnel_facts.classify(scope, 200) == ("web", ["web_view"])

    @pytest.mark.parametrize(
        "principal",
        ["device:machine-1", "session:abc", "unattributed", None],
    )
    def test_machines_agents_and_anonymous_callers_are_not_people(self, principal):
        assert funnel_facts.classify(_scope(principal=principal), 200) == (None, [])

    def test_the_widget_and_scripts_are_not_the_app(self):
        widget = _scope(user_agent="LonghouseWidget/12339 CFNetwork/3860.100.1 Darwin/25.0.0")
        script = _scope(user_agent="python-httpx/0.27")
        assert funnel_facts.classify(widget, 200) == (None, [])
        assert funnel_facts.classify(script, 200) == (None, [])

    @pytest.mark.parametrize("status", [199, 301, 401, 404, 500])
    def test_only_a_successful_response_counts(self, status):
        assert funnel_facts.classify(_scope(), status) == (None, [])

    def test_background_refresh_and_streams_are_not_a_return(self):
        assert funnel_facts.classify(_scope(path="/api/auth/refresh-native"), 200) == (None, [])
        assert funnel_facts.classify(_scope(path="/api/timeline/sessions/stream"), 200) == (None, [])

    def test_websockets_are_ignored(self):
        assert funnel_facts.classify(_scope(scope_type="websocket"), 200) == (None, [])

    def test_a_search_needs_query_text_on_a_search_route(self):
        assert funnel_facts.classify(_scope(query="query=refresh+token"), 200)[1] == ["phone_view", "search"]
        assert funnel_facts.classify(_scope(path="/api/timeline/recall", query="query=x"), 200)[1] == [
            "phone_view",
            "search",
        ]
        assert funnel_facts.classify(_scope(query=""), 200)[1] == ["phone_view"]  # just opening the timeline
        assert funnel_facts.classify(_scope(query="query=%20"), 200)[1] == ["phone_view"]
        assert funnel_facts.classify(_scope(query="query=x", method="POST"), 200)[1] == ["phone_view"]

    @pytest.mark.parametrize("route", ["input", "inputs-multipart", "send-live"])
    def test_sending_an_instruction_is_a_steer(self, route):
        scope = _scope(method="POST", path=f"/api/sessions/abc123/{route}")
        assert funnel_facts.classify(scope, 200)[1] == ["phone_view", "steer"]

    def test_reading_or_interrupting_is_not_a_steer(self):
        assert funnel_facts.classify(_scope(method="GET", path="/api/sessions/abc123/inputs"), 200)[1] == ["phone_view"]
        assert funnel_facts.classify(_scope(method="POST", path="/api/sessions/abc123/interrupt-live"), 200)[1] == ["phone_view"]


# ---------------------------------------------------------------------------
# The side file
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    return FunnelFactsStore(tmp_path / "funnel-facts.sqlite3")


def _at(day, hour=9):
    return datetime(2026, 10, day, hour, 0, tzinfo=UTC)


class TestStore:
    def test_a_milestone_keeps_only_the_time_it_first_happened(self, store):
        assert store.note_milestone("search", at=_at(1, 9)) is True
        assert store.note_milestone("search", at=_at(3, 9)) is False

        assert store.snapshot()["milestones"]["search"] == {"first_at": "2026-10-01T09:00:00Z"}

    def test_a_restart_never_moves_a_milestone(self, store):
        store.note_milestone("search", at=_at(1, 9))

        after_restart = FunnelFactsStore(store.path)
        assert after_restart.note_milestone("search", at=_at(5, 9)) is True  # written again, ignored by the file

        assert after_restart.snapshot()["milestones"]["search"] == {"first_at": "2026-10-01T09:00:00Z"}

    def test_repeats_do_not_write(self, store):
        assert store.note_milestone("steer", at=_at(1, 9)) is True
        assert store.note_milestone("steer", at=_at(1, 9)) is False
        assert store.note_active("ios", at=_at(1, 9)) is True
        assert store.note_active("ios", at=_at(1, 20)) is False  # same UTC day
        assert store.note_active("ios", at=_at(2, 1)) is True

    def test_active_days_are_per_surface_and_sorted(self, store):
        store.note_active("ios", at=_at(8))
        store.note_active("web", at=_at(1))
        store.note_active("ios", at=_at(1))

        assert store.snapshot()["active_days"] == {"web": ["2026-10-01"], "ios": ["2026-10-01", "2026-10-08"]}

    def test_the_vocabulary_is_closed(self, store):
        with pytest.raises(ValueError):
            store.note_milestone("query_text")
        with pytest.raises(ValueError):
            store.note_active("desktop")

    def test_the_file_holds_only_names_and_dates(self, store):
        store.note_milestone("search", at=_at(1))
        store.note_active("web", at=_at(1))
        with sqlite3.connect(store.path) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            columns = {(table, row[1]) for table in tables for row in connection.execute(f"PRAGMA table_info({table})")}
        assert tables == {"milestones", "active_days"}
        assert columns == {
            ("milestones", "name"),
            ("milestones", "first_at"),
            ("active_days", "day"),
            ("active_days", "surface"),
        }

    def test_an_empty_store_snapshot_needs_no_file(self, tmp_path):
        snapshot = FunnelFactsStore(tmp_path / "missing.sqlite3").snapshot()
        assert snapshot == {"milestones": {}, "active_days": {"web": [], "ios": []}}


# ---------------------------------------------------------------------------
# The seam: off by default, never in the way
# ---------------------------------------------------------------------------


@pytest.fixture
def facts_runtime(tmp_path, monkeypatch):
    """A runtime whose live catalog is in tmp_path, with the flag as asked (the real env var)."""
    funnel_facts.reset_store_for_tests()

    def configure(enabled: bool):
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'longhouse.db'}")
        monkeypatch.setenv("LONGHOUSE_FUNNEL_FACTS", "1" if enabled else "")
        return tmp_path / "funnel-facts.sqlite3"

    yield configure
    funnel_facts.reset_store_for_tests()


async def _drain_executor():
    await asyncio.sleep(0.2)


@pytest.mark.asyncio
async def test_disabled_records_nothing_and_creates_no_file(facts_runtime):
    path = facts_runtime(False)

    funnel_facts.observe_request(_scope(), 200)
    await _drain_executor()

    assert not path.exists()


@pytest.mark.asyncio
async def test_enabled_records_the_observation_beside_the_live_catalog(facts_runtime):
    path = facts_runtime(True)

    funnel_facts.observe_request(_scope(method="POST", path="/api/sessions/abc/input"), 200)
    await _drain_executor()

    snapshot = FunnelFactsStore(path).snapshot()
    assert set(snapshot["milestones"]) == {"phone_view", "steer"}
    assert len(snapshot["active_days"]["ios"]) == 1


@pytest.mark.asyncio
async def test_a_machine_agent_shipping_leaves_no_trace(facts_runtime):
    path = facts_runtime(True)

    funnel_facts.observe_request(_scope(principal="device:laptop", authorization="X"), 200)
    await _drain_executor()

    assert not path.exists()


@pytest.mark.asyncio
async def test_a_broken_side_file_never_reaches_the_request(facts_runtime, tmp_path):
    path = facts_runtime(True)
    path.mkdir()  # a directory where the file should be: every open fails

    funnel_facts.observe_request(_scope(), 200)  # must not raise
    await _drain_executor()


def test_observing_outside_an_event_loop_is_harmless(facts_runtime):
    facts_runtime(True)
    funnel_facts.observe_request(_scope(), 200)  # no running loop: swallowed, not raised


def test_the_document_merges_catalog_and_side_facts():
    document = funnel_facts.build_document(
        {
            "providers": [
                {"provider": "claude", "sessions": 40, "first_shipped_at": "2026-10-01T09:05:00+00:00"},
                {"provider": "codex", "sessions": 2, "first_shipped_at": "2026-10-01T09:06:00+00:00"},
            ],
            "devices": {"count": 2, "first_created_at": "2026-10-01T09:00:00+00:00", "last_used_at": None},
        },
        {"milestones": {"search": {"first_at": "a"}}, "active_days": {"ios": ["2026-10-01"]}},
    )

    assert document["schema"] == "longhouse.tenant-funnel.v1"
    assert document["machines"] == {"first_connected_at": "2026-10-01T09:00:00+00:00"}
    assert document["providers"]["claude"] == {"sessions": 40, "first_shipped_at": "2026-10-01T09:05:00+00:00"}
    assert document["milestones"]["search"]["first_at"] == "a"
    assert document["active_days"] == {"ios": ["2026-10-01"]}


# ---------------------------------------------------------------------------
# The internal route
# ---------------------------------------------------------------------------


class _Catalog:
    def __init__(self):
        self.calls = []

    async def call(self, method, params=None):
        self.calls.append((method, params))
        if method == "auth.owner.get.v2":
            return {"found": True, "owner_id": 7}
        assert method == "tenant.funnel.facts.read.v2"
        return {
            "providers": [{"provider": "claude", "sessions": 3, "first_shipped_at": "2026-10-01T09:05:00+00:00"}],
            "devices": {"count": 1, "first_created_at": "2026-10-01T09:00:00+00:00", "last_used_at": None},
        }


@pytest.fixture
def funnel_route(monkeypatch, tmp_path):
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'archive.db'}")
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("AUTH_DISABLED", "1")
    monkeypatch.setenv("FERNET_SECRET", Fernet.generate_key().decode())
    monkeypatch.setenv("JWT_SECRET", "funnel-facts-test-only")

    from zerg.main import api_app
    from zerg.routers import internal_funnel

    funnel_facts.reset_store_for_tests()
    catalog = _Catalog()

    def configure(enabled: bool):
        monkeypatch.setenv("LONGHOUSE_FUNNEL_FACTS", "1" if enabled else "")
        monkeypatch.setattr(internal_funnel, "get_settings", lambda: SimpleNamespace(internal_api_secret="funnel-test-only"))
        monkeypatch.setattr(internal_funnel, "get_catalogd_client", lambda: catalog)
        return TestClient(api_app), catalog

    yield configure
    funnel_facts.reset_store_for_tests()


def test_the_route_wants_the_tenants_own_secret(funnel_route):
    client, catalog = funnel_route(True)

    assert client.get("/internal/funnel").status_code == 401
    assert client.get("/internal/funnel", headers={"X-Internal-Token": "wrong"}).status_code == 401
    assert catalog.calls == []


def test_a_runtime_not_launched_for_a_tester_answers_404(funnel_route):
    client, catalog = funnel_route(False)

    response = client.get("/internal/funnel", headers={"X-Internal-Token": "funnel-test-only"})

    assert response.status_code == 404
    assert catalog.calls == []


def test_the_route_answers_the_document(funnel_route, tmp_path):
    client, catalog = funnel_route(True)
    FunnelFactsStore(tmp_path / "funnel-facts.sqlite3").note_active("ios", at=_at(1))

    response = client.get("/internal/funnel", headers={"X-Internal-Token": "funnel-test-only"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema"] == "longhouse.tenant-funnel.v1"
    assert body["providers"]["claude"]["sessions"] == 3
    assert body["machines"]["first_connected_at"] == "2026-10-01T09:00:00+00:00"
    assert body["active_days"]["ios"] == ["2026-10-01"]
    assert catalog.calls[1] == ("tenant.funnel.facts.read.v2", {"owner_id": "7"})


# ---------------------------------------------------------------------------
# The catalog read RPC
# ---------------------------------------------------------------------------


@pytest.fixture
def daemon_paths():
    root = Path("/tmp") / f"lhcd-funnel-{uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    yield root / "live.db", root / "catalogd.sock"
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


@pytest.mark.asyncio
async def test_the_catalog_reports_machines_and_sessions_per_provider(daemon_paths):
    from tests_lite.test_catalogd_storage_v2 import _raw_params
    from zerg.catalogd.client import CatalogClient
    from zerg.catalogd.client import CatalogRemoteError
    from zerg.catalogd.schema import create_catalog_engine
    from zerg.catalogd.schema import initialize_catalog_schema
    from zerg.catalogd.server import CatalogDaemon
    from zerg.models.live_store import LiveDeviceToken

    database_path, socket_path = daemon_paths
    first, later = _at(1, 9), _at(2, 9)
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    with engine.begin() as connection:
        for token_id, created, used in (("t1", first, later), ("t2", later, None)):
            connection.execute(
                LiveDeviceToken.__table__.insert().values(
                    id=token_id,
                    owner_id=42,
                    device_id=f"machine-{token_id}",
                    token_hash=token_id * 32,
                    created_at=created,
                    last_used_at=used,
                )
            )
        # A machine the tester has since disconnected is history, not a connected machine.
        connection.execute(
            LiveDeviceToken.__table__.insert().values(
                id="t3",
                owner_id=42,
                device_id="machine-t3",
                token_hash="3" * 64,
                created_at=_at(1, 5),
                last_used_at=_at(1, 6),
                revoked_at=_at(1, 7),
            )
        )
        # Another owner's machine must not leak into this owner's facts.
        connection.execute(
            LiveDeviceToken.__table__.insert().values(id="other", owner_id=7, device_id="theirs", token_hash="o" * 64, created_at=_at(1, 1))
        )
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        for provider, when in (("claude", first), ("claude", later), ("codex", later)):
            await client.call(
                "storage.raw_object.commit.v2",
                _raw_params(
                    epoch=uuid4(),
                    session_id=uuid4(),
                    start=0,
                    end=6,
                    records=(b"hello\n",),
                    sealed_at=when,
                    provider=provider,
                    opaque_source_id=f"{uuid4()}.jsonl",
                ),
            )

        facts = await client.call("tenant.funnel.facts.read.v2", {"owner_id": "42"})

        providers = {row["provider"]: row for row in facts["providers"]}
        assert providers["claude"]["sessions"] == 2 and providers["codex"]["sessions"] == 1
        assert facts["devices"]["count"] == 2  # the revoked one is not connected now
        assert facts["devices"]["first_created_at"] == _at(1, 5).isoformat()  # but it did connect first
        assert facts["devices"]["last_used_at"] == later.isoformat()

        nobody = await client.call("tenant.funnel.facts.read.v2", {"owner_id": "9"})
        assert nobody["providers"] == [] and nobody["devices"]["count"] == 0

        for bad in ("not-a-number", "9" * 19, ""):
            with pytest.raises(CatalogRemoteError):
                await client.call("tenant.funnel.facts.read.v2", {"owner_id": bad})
    finally:
        await client.close()
        await daemon.close()


# ---------------------------------------------------------------------------
# Through the real middleware
# ---------------------------------------------------------------------------


async def _run_through_access_log(scope, *, principal="user:1", status=200):
    from zerg.middleware.access_log import AccessLogMiddleware

    async def app(scope, receive, send):
        scope["state"] = {"principal": principal}
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    await AccessLogMiddleware(app)(scope, receive, send)
    await _drain_executor()


@pytest.mark.asyncio
async def test_the_access_log_seam_feeds_the_facts_after_the_handler_resolved_the_caller(facts_runtime):
    path = facts_runtime(True)
    scope = _scope(path="/api/timeline/sessions", query="query=deploy", principal=None)

    await _run_through_access_log(scope)

    snapshot = FunnelFactsStore(path).snapshot()
    assert set(snapshot["milestones"]) == {"phone_view", "search"}


@pytest.mark.asyncio
async def test_polling_noise_never_reaches_the_facts(facts_runtime):
    path = facts_runtime(True)

    await _run_through_access_log(_scope(path="/api/users/me/client-presence", principal=None))
    await _run_through_access_log(_scope(path="/api/agents/heartbeat", principal=None))

    assert not path.exists()
