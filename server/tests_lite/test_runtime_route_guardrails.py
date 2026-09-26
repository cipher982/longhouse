from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi import Response

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.services.session_runtime import RuntimeEventBatchIngest  # noqa: E402


def test_catalog_runtime_batch_uses_one_rpc_without_opening_sqlite(monkeypatch):
    import zerg.routers.runtime as runtime_router

    async def run_test():
        calls = []

        class CatalogClient:
            async def call(self, method, params, *, timeout_seconds):
                calls.append((method, params, timeout_seconds))
                return {
                    "accepted": 1,
                    "duplicates": 0,
                    "updated_runtime_keys": ["codex:catalog-runtime"],
                    "commit_seq": "12",
                }

        monkeypatch.setattr(runtime_router, "get_catalogd_client", lambda: CatalogClient())

        payload = RuntimeEventBatchIngest(
            events=[
                {
                    "runtime_key": "codex:catalog-runtime",
                    "provider": "codex",
                    "device_id": "cinder",
                    "source": "codex_bridge",
                    "kind": "phase_signal",
                    "phase": "running",
                    "tool_name": "Shell",
                    "occurred_at": "2026-07-12T07:00:00Z",
                    "freshness_ms": 60_000,
                    "dedupe_key": "catalog-runtime-1",
                    "payload": {},
                }
            ]
        )
        response = Response()
        # ``db=None`` is what ``no_request_db`` yields on a Runtime Host: the
        # route has to answer without a SQLAlchemy session of any kind.
        result = await runtime_router.ingest_runtime_observation_batch(
            payload,
            response,
            None,
            SimpleNamespace(device_id="cinder", id="token-1", owner_id=1),
            None,
        )

        assert result.accepted == 1
        assert result.updated_runtime_keys == ["codex:catalog-runtime"]
        assert response.headers["X-Catalog-Commit-Seq"] == "12"
        assert response.headers["X-Runtime-Label"] == "catalogd-runtime-state"
        assert len(calls) == 1
        method, params, timeout = calls[0]
        assert method == "session.runtime.apply.v2"
        assert timeout == runtime_router._HOT_RUNTIME_QUEUE_TIMEOUT_SECONDS
        assert params["events"][0]["runtime_key"] == "codex:catalog-runtime"
        assert params["events"][0]["occurred_at"] == "2026-07-12T07:00:00Z"

    asyncio.run(run_test())


def test_presence_live_store_delegates_to_runtime_batch_without_archive_wait(monkeypatch):
    import zerg.routers.presence as presence_router
    import zerg.routers.runtime as runtime_router

    async def run_test():
        calls = {}

        async def fake_runtime_batch(payload, response, db, token, single):
            calls["payload"] = payload
            calls["db"] = db
            calls["token"] = token
            calls["single"] = single
            response.headers["X-Runtime-Label"] = "presence-live-state"

        def fail_archive_serializer():  # pragma: no cover - regression guard
            raise AssertionError("live-configured presence must not wait on archive serializer")

        monkeypatch.setattr(presence_router, "live_store_configured", lambda: True)
        monkeypatch.setattr(presence_router, "get_write_serializer", fail_archive_serializer)
        monkeypatch.setattr(runtime_router, "ingest_runtime_observation_batch", fake_runtime_batch)

        token = SimpleNamespace(device_id="cinder", id="token-1", owner_id=1)
        request_db = SimpleNamespace()
        response = await presence_router.upsert_presence(
            presence_router.PresenceIn(
                session_id="019f3e77-2532-77d0-b9ba-2f24b1ca1cea",
                state="running",
                tool_name="Shell",
                provider="codex",
                occurred_at="2026-01-01T00:00:00Z",
                dedupe_key="presence-live-route-fixture",
            ),
            SimpleNamespace(),
            request_db,
            token,
        )

        assert response.status_code == 204
        assert response.headers["X-Runtime-Label"] == "presence-live-state"
        assert calls["db"] is request_db
        assert calls["token"] is token
        assert calls["single"] is None
        [event] = calls["payload"].events
        assert event.runtime_key == "codex:019f3e77-2532-77d0-b9ba-2f24b1ca1cea"
        assert event.phase == "running"
        assert event.tool_name == "Shell"
        assert event.dedupe_key == "presence-live-route-fixture"

    asyncio.run(run_test())


def test_runtime_batch_dispatches_exactly_the_console_turns_catalogd_claimed(monkeypatch):
    """catalogd settles the Console turn; the route only dispatches its claims."""

    import zerg.routers.runtime as runtime_router
    import zerg.services.console_turns as console_turns

    claimed_turn = {"turn_id": "turn-2", "run_id": "run-2", "state": "starting"}

    async def run_test(console_next_turns):
        dispatched = []

        class CatalogClient:
            async def call(self, method, params, *, timeout_seconds):
                assert method == "session.runtime.apply.v2"
                return {
                    "accepted": 1,
                    "duplicates": 0,
                    "updated_runtime_keys": ["omp:session-1"],
                    "console_next_turns": console_next_turns,
                    "commit_seq": "7",
                }

        catalog = CatalogClient()

        async def fake_dispatch(**kwargs):
            dispatched.append(kwargs)

        monkeypatch.setattr(runtime_router, "get_catalogd_client", lambda: catalog)
        monkeypatch.setattr(console_turns, "dispatch_catalog_claimed_turn", fake_dispatch)
        payload = RuntimeEventBatchIngest(
            events=[
                {
                    "runtime_key": "omp:session-1",
                    "provider": "omp",
                    "device_id": "cinder",
                    "source": "omp_print",
                    "kind": "terminal_signal",
                    "occurred_at": "2026-07-12T07:00:00Z",
                    "dedupe_key": "omp-print:terminal",
                    "payload": {"terminal_state": "run_completed"},
                }
            ]
        )
        result = await runtime_router.ingest_runtime_observation_batch(
            payload,
            Response(),
            None,
            SimpleNamespace(device_id="cinder", id="token-1", owner_id=1),
            None,
        )
        assert result.accepted == 1
        return dispatched, catalog

    dispatched, catalog = asyncio.run(run_test([{"owner_id": 3, "turn": claimed_turn}]))
    assert dispatched == [{"owner_id": 3, "turn": claimed_turn, "client": catalog}]

    dispatched, _catalog = asyncio.run(run_test([]))
    assert dispatched == []
