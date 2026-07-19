from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

import zerg.database as database_module
from zerg.database import initialize_live_database
from zerg.database import make_live_engine
from zerg.database import make_sessionmaker
from zerg.models.live_store import LiveRuntimeState
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionInputReceipt
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.routers.session_chat import PauseRequestResponseRequest
from zerg.routers.session_chat import SessionInputRequest
from zerg.routers.session_chat import _create_session_input_response
from zerg.routers.session_chat import _respond_to_live_pause_request
from zerg.services.live_control_catalog import live_control_capability_available
from zerg.services.live_control_catalog import live_session_input_block_reason
from zerg.services.live_control_catalog import load_live_control_session
from zerg.services.live_control_catalog import wake_next_live_catalog_input
from zerg.services.live_session_inputs import upsert_live_input_receipt
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
from zerg.services.managed_control_dispatcher import ManagedControlDispatchResult


def _seed_live_control(db):
    now = datetime.now(timezone.utc)
    session_id = uuid4()
    thread_id = uuid4()
    run_id = uuid4()
    db.add(
        LiveSessionCatalog(
            session_id=str(session_id),
            provider="codex",
            environment="production",
            project="longhouse",
            device_id="cinder",
            cwd="/workspace/longhouse",
            started_at=now,
            last_activity_at=now,
            primary_thread_id=str(thread_id),
            created_at=now,
            updated_at=now,
        )
    )
    db.add(
        LiveSessionThread(
            id=str(thread_id),
            session_id=str(session_id),
            provider="codex",
            branch_kind="root",
            is_primary=1,
            created_at=now,
            updated_at=now,
        )
    )
    db.add(
        LiveSessionRun(
            id=str(run_id),
            thread_id=str(thread_id),
            provider="codex",
            host_id="cinder",
            cwd="/workspace/longhouse",
            launch_origin="longhouse_spawned",
            started_at=now,
        )
    )
    db.add(
        LiveSessionConnection(
            run_id=str(run_id),
            control_plane="codex_bridge",
            acquisition_kind="spawned_control",
            state="attached",
            device_id="cinder",
            can_send_input=1,
            can_interrupt=1,
            can_terminate=1,
            acquired_at=now,
            last_health_at=now,
        )
    )
    db.commit()
    return session_id


def test_live_control_projection_never_needs_archive_models(tmp_path):
    engine = make_live_engine(f"sqlite:///{tmp_path / 'live.db'}")
    initialize_live_database(engine)
    factory = make_sessionmaker(engine)
    with factory() as db:
        session_id = _seed_live_control(db)
        session = load_live_control_session(db, session_id)
        assert session is not None
        assert session.id == session_id
        assert session.device_id == "cinder"
        assert live_control_capability_available(db, session_id=session_id, capability="send") is True
        assert live_control_capability_available(db, session_id=session_id, capability="interrupt") is True
        connection = db.query(LiveSessionConnection).one()
        connection.state = "degraded"
        db.commit()
        assert live_control_capability_available(db, session_id=session_id, capability="send") is False
        assert live_control_capability_available(db, session_id=session_id, capability="interrupt") is False
        assert live_control_capability_available(db, session_id=session_id, capability="terminate") is False

        # Missing renewal evidence is fail-closed during rollout. Creation now
        # stamps health, but legacy rows stay read-only until the next renewal.
        connection.state = "attached"
        connection.last_health_at = None
        db.commit()
        assert live_control_capability_available(db, session_id=session_id, capability="send") is False


def test_live_control_grant_never_uses_stale_or_observe_only_run(tmp_path):
    engine = make_live_engine(f"sqlite:///{tmp_path / 'live-run-isolation.db'}")
    initialize_live_database(engine)
    factory = make_sessionmaker(engine)
    with factory() as db:
        session_id = _seed_live_control(db)
        thread = db.query(LiveSessionThread).one()
        old_run = db.query(LiveSessionRun).one()
        assert live_control_capability_available(db, session_id=session_id, capability="send") is True

        # A newer open run is authoritative even before its connection arrives;
        # the prior run must not authorize a command for it.
        now = datetime.now(timezone.utc)
        newer_run = LiveSessionRun(
            id=str(uuid4()),
            thread_id=thread.id,
            provider="codex",
            host_id="cinder",
            cwd="/workspace/longhouse",
            launch_origin="longhouse_continued",
            started_at=now,
        )
        db.add(newer_run)
        db.commit()
        assert live_control_capability_available(db, session_id=session_id, capability="send") is False
        db.add(
            LiveSessionConnection(
                run_id=newer_run.id,
                control_plane="log_tail",
                acquisition_kind="observe_only",
                state="attached",
                device_id="cinder",
                can_send_input=1,
                acquired_at=now,
                last_health_at=now,
            )
        )
        old_run.ended_at = now
        db.commit()
        assert live_control_capability_available(db, session_id=session_id, capability="send") is False


def test_catalog_input_block_reason_uses_disposition_and_latest_run_not_legacy_runtime():
    session = SimpleNamespace(
        closed_at=None,
        ended_at=datetime.now(timezone.utc),
        catalog_facts={
            "runtime": {"terminal_state": "session_ended"},
            "latest_run": {"ended_at": None},
        },
    )

    for legacy_terminal in ("session_ended", "process_gone", "host_expired"):
        session.catalog_facts["runtime"]["terminal_state"] = legacy_terminal
        assert live_session_input_block_reason(None, session) is None

    session.catalog_facts["latest_run"]["ended_at"] = datetime.now(timezone.utc).isoformat()
    assert live_session_input_block_reason(None, session) == "run_ended"

    session.catalog_facts["runtime"]["terminal_state"] = "user_closed"
    assert live_session_input_block_reason(None, session) == "session_closed"


@pytest.mark.asyncio
async def test_hot_pause_request_is_answerable_before_archive_convergence(tmp_path, monkeypatch):
    from zerg.catalogd.schema import initialize_catalog_schema
    from zerg.catalogd.store import CatalogStore

    engine = make_live_engine(f"sqlite:///{tmp_path / 'live-pause.db'}")
    initialize_live_database(engine)
    initialize_catalog_schema(engine)
    factory = make_sessionmaker(engine)
    catalog_store = CatalogStore(engine)

    class CatalogClient:
        async def call(self, method, params, **_kwargs):
            if method == "interaction.list.v2":
                return catalog_store.list_interactions(**params)
            if method == "interaction.resolve.v2":
                payload = {**params, "resolved_at": datetime.fromisoformat(params["resolved_at"])}
                return catalog_store.resolve_interaction(**payload)
            raise AssertionError(method)

    monkeypatch.setattr("zerg.services.catalogd_supervisor.get_catalogd_client", lambda: CatalogClient())
    pause_id = str(uuid4())
    request_key = "codex:runtime:request-1"
    with factory() as db:
        session_id = _seed_live_control(db)
        session = load_live_control_session(db, session_id)
        assert session is not None
        db.add(
            LiveRuntimeState(
                runtime_key=f"codex:{session_id}",
                session_id=session_id,
                provider="codex",
                device_id="cinder",
                phase="blocked",
                phase_source="codex_bridge",
                timeline_anchor_at=datetime.now(timezone.utc),
                runtime_version=1,
                pending_interaction_id=request_key,
                pending_interaction_kind="structured_question",
                pending_interaction_opened_at=datetime.now(timezone.utc),
                pending_interaction_updated_at=datetime.now(timezone.utc),
                pending_interaction_can_respond=1,
                pending_interaction_projection_json={
                    "id": pause_id,
                    "request_key": request_key,
                    "session_id": str(session_id),
                    "runtime_key": f"codex:{session_id}",
                    "kind": "structured_question",
                    "status": "pending",
                    "provider": "codex",
                    "can_respond": True,
                    "questions": [{"id": "choice", "question": "Choose", "options": []}],
                },
            )
        )
        db.commit()

        async def fake_answer(**kwargs):
            assert kwargs["request_key"] == request_key
            return SimpleNamespace(ok=True, response_data={"status": "resolved"}, error=None, exit_code=0)

        monkeypatch.setattr("zerg.routers.session_chat.answer_pause_request_on_managed_local_session", fake_answer)
        response = await _respond_to_live_pause_request(
            source_session=session,
            owner_id=7,
            pause_request_id=pause_id,
            body=PauseRequestResponseRequest(decision="answer", answers={"choice": "fast"}),
            db=db,
        )
        assert response.status == "resolved"
        db.expire_all()
        state = db.query(LiveRuntimeState).one()
        assert state.pending_interaction_id is None
        assert state.pending_interaction_projection_json is None


@pytest.mark.asyncio
async def test_catalog_input_dispatches_and_projects_live_receipt_only(tmp_path, monkeypatch):
    from zerg.catalogd.schema import initialize_catalog_schema
    from zerg.catalogd.store import CatalogStore

    engine = make_live_engine(f"sqlite:///{tmp_path / 'live.db'}")
    initialize_live_database(engine)
    initialize_catalog_schema(engine)
    factory = make_sessionmaker(engine)
    with factory() as db:
        session_id = _seed_live_control(db)

    monkeypatch.setattr(database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(database_module, "get_live_write_session_factory", lambda: factory)
    catalog_store = CatalogStore(engine)

    class _CatalogClient:
        async def call(self, method, params, **_kwargs):
            if method == "session.input.receipt.read.v2":
                return catalog_store.read_input_receipt(**params)
            if method == "session.input.receipt.upsert.v2":
                receipt = dict(params["receipt"])
                if receipt["expires_at"] is not None:
                    receipt["expires_at"] = datetime.fromisoformat(receipt["expires_at"])
                return catalog_store.upsert_input_receipt(receipt=receipt)
            if method == "session.input.finish.v2":
                return catalog_store.finish_queued_input(**params)
            if method == "session.input.recent.list.v2":
                return catalog_store.list_recent_input_receipts(**params)
            raise AssertionError(method)

    import zerg.services.managed_control_dispatcher as dispatcher
    import zerg.services.session_chat_impl as chat_impl

    async def fake_dispatch(**_kwargs):
        return ManagedControlDispatchResult(
            ok=True,
            transport=MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL,
            data={"exit_code": 0, "turn_id": "turn-1"},
        )

    monkeypatch.setattr(dispatcher, "dispatch_managed_control_command", fake_dispatch)
    monkeypatch.setattr(chat_impl, "_schedule_catalog_lock_release", lambda **_kwargs: None)
    monkeypatch.setattr("zerg.services.catalogd_supervisor.get_catalogd_client", lambda: _CatalogClient())
    monkeypatch.setattr(
        "zerg.services.catalog_read_gateway.session_snapshot",
        lambda value, *, owner_id=None: catalog_store.read_session(session_id=value, owner_id=owner_id),
    )

    from zerg.services.live_control_catalog import load_live_control_session_snapshot

    with factory() as db:
        session = load_live_control_session_snapshot(session_id)
        assert session is not None
        response = await _create_session_input_response(
            source_session=session,
            owner_id=7,
            body=SessionInputRequest(text="keep the hot loop alive", client_request_id="catalog-control-1"),
            db=db,
        )

    assert response.outcome == "sent"
    assert response.input_id is None
    with factory() as db:
        receipt = db.query(LiveSessionInputReceipt).one()
        assert receipt.status == "delivered"
        assert receipt.client_request_id == "catalog-control-1"
        assert receipt.archive_session_input_id is None


@pytest.mark.asyncio
async def test_catalog_terminal_wake_dispatches_next_live_receipt(tmp_path, monkeypatch):
    from zerg.catalogd.schema import initialize_catalog_schema
    from zerg.catalogd.store import CatalogStore

    engine = make_live_engine(f"sqlite:///{tmp_path / 'live.db'}")
    initialize_live_database(engine)
    initialize_catalog_schema(engine)
    factory = make_sessionmaker(engine)
    with factory() as db:
        session_id = _seed_live_control(db)
        now = datetime.now(timezone.utc)
        db.add(
            LiveRuntimeState(
                runtime_key=f"codex:{session_id}",
                session_id=session_id,
                provider="codex",
                device_id="cinder",
                phase="idle",
                phase_source="test",
                timeline_anchor_at=now,
                runtime_version=1,
                updated_at=now,
            )
        )
        upsert_live_input_receipt(
            db,
            owner_id=7,
            session_id=session_id,
            provider="codex",
            text="queued after the current turn",
            intent="auto",
            status="queued",
            client_request_id="queued-1",
        )
        db.commit()

    catalog_store = CatalogStore(engine)

    class _CatalogClient:
        async def call(self, method, params, **_kwargs):
            if method == "session.input.claim.v2":
                return catalog_store.claim_queued_input(**params)
            if method == "session.input.finish.v2":
                return catalog_store.finish_queued_input(**params)
            raise AssertionError(method)

    import zerg.services.managed_control_dispatcher as dispatcher
    import zerg.services.session_chat_impl as chat_impl

    async def fake_dispatch(**_kwargs):
        return ManagedControlDispatchResult(
            ok=True,
            transport=MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL,
            data={"exit_code": 0, "turn_id": "turn-queued"},
        )

    monkeypatch.setattr(dispatcher, "dispatch_managed_control_command", fake_dispatch)
    monkeypatch.setattr("zerg.services.catalogd_supervisor.get_catalogd_client", lambda: _CatalogClient())
    monkeypatch.setattr(chat_impl, "_schedule_catalog_lock_release", lambda **_kwargs: None)

    assert await wake_next_live_catalog_input(session_id) is True
    with factory() as db:
        receipt = db.query(LiveSessionInputReceipt).one()
        assert receipt.status == "delivered"
        assert receipt.delivery_request_id


@pytest.mark.asyncio
async def test_catalog_lock_timeout_releases_and_attempts_queue_recovery(tmp_path, monkeypatch):
    engine = make_live_engine(f"sqlite:///{tmp_path / 'live.db'}")
    initialize_live_database(engine)
    factory = make_sessionmaker(engine)
    with factory() as db:
        session_id = _seed_live_control(db)

    import zerg.services.live_control_catalog as live_control
    import zerg.services.session_chat_impl as chat_impl
    from zerg.services.session_kernel_projection import session_lock_scope_id
    from zerg.services.session_locks import session_lock_manager

    request_id = "timeout-holder"
    scope = session_lock_scope_id(session_id)
    assert await session_lock_manager.acquire(session_id=scope, holder=request_id, ttl_seconds=300)
    wakes: list[str] = []

    async def fake_wake(value):
        wakes.append(str(value))
        return False

    monkeypatch.setattr("zerg.services.catalogd_supervisor.get_catalogd_client", lambda: object())
    monkeypatch.setattr(chat_impl, "MANAGED_LOCAL_LOCK_RELEASE_TIMEOUT_SECS", 0)
    monkeypatch.setattr(live_control, "wake_next_live_catalog_input", fake_wake)

    await chat_impl._release_catalog_lock_after_terminal(
        session_id=session_id,
        lock_scope_id=scope,
        request_id=request_id,
        dispatched_at=datetime.now(timezone.utc),
    )

    assert await session_lock_manager.get_lock_info(scope) is None
    assert wakes == [str(session_id)]
