from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi import Response
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.database import Base
from zerg.database import get_db
from zerg.database import initialize_live_database
from zerg.database import make_engine
from zerg.database import make_live_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.main import api_app
from zerg.models.agents import SessionRuntimeState
from zerg.models.live_store import LiveArchiveOutbox
from zerg.models.live_store import LiveRuntimeState
from zerg.services.live_archive_outbox import RUNTIME_EVENT_KIND
from zerg.services.session_runtime import RuntimeEventBatchIngest


def test_runtime_batch_releases_request_db_before_serialized_write(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path}/runtime_release.db", pool_size=1, max_overflow=0)
    Base.metadata.create_all(bind=engine)
    factory = make_sessionmaker(engine)
    observations: dict[str, int] = {}

    class ReleaseCheckingSerializer:
        is_configured = True

        async def execute_after_closing_request_session(self, fn, fallback_db, **_kwargs):
            observations["before_close"] = engine.pool.checkedout()
            fallback_db.close()
            observations["after_close"] = engine.pool.checkedout()
            with factory() as write_db:
                result = fn(write_db)
                write_db.commit()
                return result

        async def execute_or_direct(self, *_args, **_kwargs):  # pragma: no cover - regression guard
            raise AssertionError("runtime batch must release the request DB before waiting on serialized writes")

    def override_db():
        db = factory()
        try:
            db.execute(text("SELECT 1"))
            yield db
        finally:
            db.close()

    def override_verify_agents_token():
        return SimpleNamespace(device_id="runtime-release", id="token-1", owner_id=1)

    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.setattr("zerg.routers.runtime.get_write_serializer", lambda: ReleaseCheckingSerializer())
    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    try:
        with TestClient(api_app) as client:
            response = client.post(
                "/agents/runtime/events/batch",
                json={
                    "events": [
                        {
                            "runtime_key": "codex:runtime-release",
                            "provider": "codex",
                            "device_id": "runtime-release",
                            "source": "codex_bridge",
                            "kind": "phase_signal",
                            "phase": "idle",
                            "occurred_at": "2026-01-01T00:00:00Z",
                            "freshness_ms": 60000,
                            "dedupe_key": "runtime-release-1",
                            "payload": {},
                        }
                    ]
                },
                headers={"X-Agents-Token": "dev"},
            )
        assert response.status_code == 200, response.text
    finally:
        api_app.dependency_overrides.clear()
        engine.dispose()

    assert observations == {"before_close": 1, "after_close": 0}


def test_runtime_batch_uses_live_store_and_outbox_without_archive_observations(tmp_path, monkeypatch):
    import zerg.routers.runtime as runtime_router

    async def run_test():
        archive_engine = make_engine(f"sqlite:///{tmp_path}/archive.db")
        Base.metadata.create_all(bind=archive_engine)
        ArchiveSession = sessionmaker(bind=archive_engine)

        live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
        initialize_live_database(live_engine)
        LiveSession = sessionmaker(bind=live_engine)

        class LiveSerializer:
            is_configured = True

            async def execute(self, fn, **kwargs):
                assert kwargs["label"] == "runtime-live-state"
                with LiveSession() as live_db:
                    result = fn(live_db)
                    live_db.commit()
                    return result

        class ArchiveSerializer:
            is_configured = True

            async def execute(self, *_args, **_kwargs):  # pragma: no cover - regression guard
                raise AssertionError("live-configured runtime ingest must not archive observations inline")

            async def execute_after_closing_request_session(self, *_args, **_kwargs):  # pragma: no cover - regression guard
                raise AssertionError("live-configured runtime ingest must not wait on archive serializer")

            async def execute_or_direct(self, *_args, **_kwargs):  # pragma: no cover - regression guard
                raise AssertionError("live transcript runtime ingest should not need archive followups")

        monkeypatch.setattr(runtime_router, "live_store_configured", lambda: True)
        monkeypatch.setattr(runtime_router, "get_live_write_serializer", lambda: LiveSerializer())
        monkeypatch.setattr(runtime_router, "get_write_serializer", lambda: ArchiveSerializer())

        payload = RuntimeEventBatchIngest(
            events=[
                {
                    "runtime_key": "codex:runtime-live-route",
                    "provider": "codex",
                    "device_id": "cinder",
                    "source": "codex_bridge",
                    "kind": "phase_signal",
                    "phase": "running",
                    "tool_name": "Shell",
                    "occurred_at": "2026-01-01T00:00:00Z",
                    "freshness_ms": 60000,
                    "dedupe_key": "runtime-live-route-1",
                    "payload": {},
                }
            ]
        )

        request_db = ArchiveSession()
        try:
            result = await asyncio.wait_for(
                runtime_router.ingest_runtime_observation_batch(
                    payload,
                    Response(),
                    request_db,
                    SimpleNamespace(device_id="cinder", id="token-1", owner_id=1),
                    None,
                ),
                timeout=0.5,
            )
            assert result.accepted == 1
            assert result.updated_runtime_keys == ["codex:runtime-live-route"]

            with LiveSession() as live_db:
                live_state = live_db.query(LiveRuntimeState).filter(LiveRuntimeState.runtime_key == "codex:runtime-live-route").one()
                assert live_state.phase == "running"
                assert live_state.active_tool == "Shell"
                outbox = live_db.query(LiveArchiveOutbox).filter(LiveArchiveOutbox.kind == RUNTIME_EVENT_KIND).one()
                assert outbox.drained_at is None
                assert "runtime-live-route-1" in outbox.idempotency_key
            with ArchiveSession() as archive_db:
                assert archive_db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == "codex:runtime-live-route").count() == 0
        finally:
            request_db.close()
            archive_engine.dispose()
            live_engine.dispose()

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


def test_runtime_batch_skips_bridge_live_transcript_delta_outbox(tmp_path, monkeypatch):
    import zerg.routers.runtime as runtime_router

    async def run_test():
        archive_engine = make_engine(f"sqlite:///{tmp_path}/archive.db")
        Base.metadata.create_all(bind=archive_engine)
        ArchiveSession = sessionmaker(bind=archive_engine)

        live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
        initialize_live_database(live_engine)
        LiveSession = sessionmaker(bind=live_engine)

        class LiveSerializer:
            is_configured = True

            async def execute(self, fn, **kwargs):
                assert kwargs["label"] == "runtime-live-state"
                with LiveSession() as live_db:
                    result = fn(live_db)
                    live_db.commit()
                    return result

        class ArchiveSerializer:
            is_configured = True

            async def execute(self, *_args, **_kwargs):  # pragma: no cover - regression guard
                raise AssertionError("live transcript deltas must not archive observations inline")

            async def execute_after_closing_request_session(self, *_args, **_kwargs):  # pragma: no cover - regression guard
                raise AssertionError("live transcript deltas must not wait on archive serializer")

            async def execute_or_direct(self, *_args, **_kwargs):  # pragma: no cover - regression guard
                raise AssertionError("live transcript deltas should not need archive followups")

        monkeypatch.setattr(runtime_router, "live_store_configured", lambda: True)
        monkeypatch.setattr(runtime_router, "get_live_write_serializer", lambda: LiveSerializer())
        monkeypatch.setattr(runtime_router, "get_write_serializer", lambda: ArchiveSerializer())

        payload = RuntimeEventBatchIngest(
            events=[
                {
                    "runtime_key": "codex:runtime-live-transcript",
                    "provider": "codex",
                    "device_id": "cinder",
                    "source": "codex_bridge_live",
                    "kind": "progress_signal",
                    "occurred_at": "2026-01-01T00:00:00Z",
                    "freshness_ms": 60000,
                    "dedupe_key": "runtime-live-transcript-1",
                    "payload": {"progress_kind": "bridge_live_transcript_delta"},
                }
            ]
        )

        request_db = ArchiveSession()
        try:
            result = await asyncio.wait_for(
                runtime_router.ingest_runtime_observation_batch(
                    payload,
                    Response(),
                    request_db,
                    SimpleNamespace(device_id="cinder", id="token-1", owner_id=1),
                    None,
                ),
                timeout=0.5,
            )
            assert result.accepted == 1
            assert result.updated_runtime_keys == ["codex:runtime-live-transcript"]

            with LiveSession() as live_db:
                live_state = live_db.query(LiveRuntimeState).filter(LiveRuntimeState.runtime_key == "codex:runtime-live-transcript").one()
                assert live_state.last_progress_at is not None
                assert live_db.query(LiveArchiveOutbox).count() == 0
        finally:
            request_db.close()
            archive_engine.dispose()
            live_engine.dispose()

    asyncio.run(run_test())


def test_runtime_batch_live_store_requires_configured_live_serializer(tmp_path, monkeypatch):
    import zerg.routers.runtime as runtime_router

    async def run_test():
        archive_engine = make_engine(f"sqlite:///{tmp_path}/archive.db")
        Base.metadata.create_all(bind=archive_engine)
        ArchiveSession = sessionmaker(bind=archive_engine)

        class UnconfiguredLiveSerializer:
            is_configured = False

        class ArchiveSerializer:
            is_configured = True

        monkeypatch.setattr(runtime_router, "live_store_configured", lambda: True)
        monkeypatch.setattr(runtime_router, "get_live_write_serializer", lambda: UnconfiguredLiveSerializer())
        monkeypatch.setattr(runtime_router, "get_write_serializer", lambda: ArchiveSerializer())

        payload = RuntimeEventBatchIngest(
            events=[
                {
                    "runtime_key": "codex:runtime-live-unconfigured",
                    "provider": "codex",
                    "device_id": "cinder",
                    "source": "codex_bridge",
                    "kind": "phase_signal",
                    "phase": "idle",
                    "occurred_at": "2026-01-01T00:00:00Z",
                    "freshness_ms": 60000,
                    "dedupe_key": "runtime-live-unconfigured-1",
                    "payload": {},
                }
            ]
        )

        request_db = ArchiveSession()
        try:
            try:
                await runtime_router.ingest_runtime_observation_batch(
                    payload,
                    Response(),
                    request_db,
                    SimpleNamespace(device_id="cinder", id="token-1", owner_id=1),
                    None,
                )
            except runtime_router.HTTPException as exc:
                assert exc.status_code == 503
                assert "Live Store write serializer is not configured" in str(exc.detail)
            else:  # pragma: no cover - regression guard
                raise AssertionError("expected runtime live store misconfiguration to return 503")
        finally:
            request_db.close()
            archive_engine.dispose()

    asyncio.run(run_test())
