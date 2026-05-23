"""Scripted stress tests for POST /api/sessions/{id}/input.

These exercise real asyncio concurrency through the ASGI stack — not the
synchronous TestClient wrapper — so lock-manager races, queue-cap
enforcement, and drain-mid-cancel races can actually collide.

What is and isn't covered here:

- Covered: the session lock manager, SessionInput queue cap, row-level
  claim atomicity, cancel-vs-drain races, reconciliation intent split at
  scale.
- NOT covered: provider-side behavior (Codex turn/steer, Claude channel
  injection). Those need a live managed session and live provider and
  are deferred to real dogfood on a real machine.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet
import httpx
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.database import get_db
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.browser_route_auth import get_current_browser_route_user
from zerg.models.agents import SessionInput
from zerg.models.agents import SessionRuntimeState
from zerg.models.enums import UserRole
from zerg.models.models import Runner
from zerg.models.user import User
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest
from zerg.services.runner_connection_manager import get_runner_connection_manager
from zerg.services.session_continuity import session_lock_manager
from zerg.services.session_runtime import phase_freshness_ms
from zerg.services.session_runtime import runtime_key_for_session


def _make_db(tmp_path, name):
    db_path = tmp_path / f"{name}.db"
    # Size pool + overflow for the concurrency the stress tests drive.
    # Default QueuePool is 5+10=15; 40-way parallel fanout exhausts it and
    # blocks on pool.get. That's a real production concern the stress test
    # is meant to keep in mind, but here we're exercising the lock manager,
    # not the pool — so give it headroom.
    engine = make_engine(f"sqlite:///{db_path}", pool_size=50, max_overflow=50)
    initialize_database(engine)
    return make_sessionmaker(engine)


def _seed_live_runtime_state(db, session, *, phase: str = "idle") -> None:
    now = datetime.now(timezone.utc)
    freshness_ms = phase_freshness_ms(phase) or int(timedelta(minutes=5).total_seconds() * 1000)
    key = runtime_key_for_session(str(session.provider or "codex"), str(session.id))
    state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == key).first()
    if state is None:
        state = SessionRuntimeState(runtime_key=key, session_id=session.id, provider=str(session.provider or "codex"), device_id=session.device_id)
        db.add(state)
    state.phase = phase
    state.phase_source = "semantic"
    state.phase_started_at = now
    state.last_runtime_signal_at = now
    state.last_progress_at = now
    state.last_live_at = now
    state.timeline_anchor_at = now
    state.freshness_expires_at = now + timedelta(milliseconds=freshness_ms)
    state.terminal_state = None
    state.terminal_at = None
    state.runtime_version = int(getattr(state, "runtime_version", 0) or 0) + 1
    db.commit()


def _seed_managed_session(session_local, *, transport: str = "codex_app_server") -> tuple:
    session_id = uuid4()
    with session_local() as db:
        user = User(email=f"stress-{uuid4().hex[:6]}@test.local", role=UserRole.USER.value)
        db.add(user)
        db.commit()
        db.refresh(user)
        store = AgentsStore(db)
        started_at = datetime.now(timezone.utc)
        store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="codex",
                environment="Cinder",
                project="stress",
                device_id="cinder",
                cwd="/tmp",
                git_repo=None,
                git_branch=None,
                provider_session_id=f"stress-{uuid4().hex[:8]}",
                started_at=started_at,
                ended_at=started_at,
                events=[
                    EventIngest(
                        role="user",
                        content_text="seed",
                        timestamp=started_at,
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                    )
                ],
            )
        )
        sess = store.get_session(session_id)
        assert sess is not None
        sess.execution_home = "managed_local"
        sess.managed_transport = transport
        sess.source_runner_id = 1
        sess.source_runner_name = "cinder"
        sess.managed_session_name = "lh-stress"
        runner = Runner(
            id=1,
            owner_id=user.id,
            name="cinder",
            status="online",
            auth_secret_hash="test",
        )
        db.merge(runner)
        from tests_lite._kernel_test_helpers import seed_managed_kernel_rows

        if transport == "codex_app_server":
            kernel_plane = "codex_bridge"
        elif transport == "opencode_process":
            kernel_plane = "opencode_process"
        else:
            kernel_plane = "claude_channel_bridge"
        seed_managed_kernel_rows(db, sess, control_plane=kernel_plane)
        db.commit()
        get_runner_connection_manager().register(user.id, 1, SimpleNamespace())
        _seed_live_runtime_state(db, sess)
        user_id = user.id
    return session_id, user_id


def _configure_app(session_local, user):
    """Wire dependency overrides on the live ASGI app for async calls."""
    from zerg.main import api_app
    from zerg.main import app

    def override_get_db():
        db = session_local()
        try:
            yield db
        finally:
            db.close()

    def override_current_user():
        return user

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[get_current_browser_route_user] = override_current_user
    return app, api_app


def _install_dispatch_stubs(monkeypatch, *, send_latency_secs: float = 0.0):
    """Stub live_session_dispatch so we exercise Longhouse's own lock/queue
    semantics without requiring a managed runtime."""

    async def fake_send_text(
        *,
        db,
        owner_id,
        session,
        text,
        commis_id=None,
        timeout_secs=15,
        verify_turn_started=False,
        verification_timeout_secs=None,
        attachments=None,
    ):
        if send_latency_secs > 0:
            await asyncio.sleep(send_latency_secs)
        return SimpleNamespace(ok=True, exit_code=0, error=None, verified_turn_started=True)

    monkeypatch.setattr("zerg.services.live_session_dispatch.send_text_to_live_session", fake_send_text)
    monkeypatch.setattr("zerg.services.session_chat_impl._schedule_managed_local_lock_release", lambda **_: None)
    monkeypatch.setattr(
        "zerg.services.session_chat_impl._schedule_managed_local_active_phase_observation", lambda **_: None
    )


@pytest.mark.asyncio
async def test_stress_concurrent_auto_serializes_through_lock(monkeypatch, tmp_path):
    """With the session lock already held, 10 concurrent intent=auto posts
    must land exactly one queued row each (all queue) and never exceed the
    session's configured cap — even under asyncio interleaving."""

    session_local = _make_db(tmp_path, "stress_lock")
    session_id, user_id = _seed_managed_session(session_local, transport="claude_channel_bridge")
    _install_dispatch_stubs(monkeypatch)

    # Pre-acquire the lock as a different holder; every /input auto in this
    # test should see it held and queue.
    lock_scope_id = str(session_id)
    acquired = await session_lock_manager.acquire(
        session_id=lock_scope_id,
        holder="stress-external-holder",
        ttl_seconds=300,
    )
    assert acquired

    app, api_app = _configure_app(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://stress",
        ) as client:
            async def fire(i: int) -> httpx.Response:
                return await client.post(
                    f"/api/sessions/{session_id}/input",
                    json={"text": f"msg {i}", "intent": "auto"},
                )

            responses = await asyncio.gather(*[fire(i) for i in range(10)])

        # 5 succeed (cap), 5 return 409 queue-full. Server-side cap is the
        # invariant; no silent truncation, no extra rows.
        assert sum(r.status_code == 200 for r in responses) == 5, [r.status_code for r in responses]
        assert sum(r.status_code == 409 for r in responses) == 5, [r.status_code for r in responses]

        with session_local() as db:
            rows = db.query(SessionInput).filter(SessionInput.session_id == session_id).all()
            assert len(rows) == 5, f"expected 5 queued rows, got {len(rows)}"
            assert all(r.status == "queued" for r in rows)
            assert all(r.intent == "auto" for r in rows)
    finally:
        await session_lock_manager.release(lock_scope_id, "stress-external-holder")
        api_app.dependency_overrides = {}


@pytest.mark.asyncio
async def test_stress_concurrent_cancel_races_never_double_cancel(monkeypatch, tmp_path):
    """Fire 10 concurrent DELETE /inputs/{id} against the same queued row.
    Exactly one should return {cancelled: true}; the rest should 409 or
    404 (no double-transitions, no successful second cancel)."""

    session_local = _make_db(tmp_path, "stress_cancel")
    session_id, user_id = _seed_managed_session(session_local)
    _install_dispatch_stubs(monkeypatch)

    # Seed one queued row we'll race to cancel.
    from zerg.services.session_inputs import create_session_input

    with session_local() as db:
        row = create_session_input(
            db,
            session_id=session_id,
            text="cancel me",
            owner_id=user_id,
            intent="queue",
            status="queued",
        )
        input_id = int(row.id)

    app, api_app = _configure_app(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://stress",
        ) as client:
            async def cancel() -> httpx.Response:
                return await client.delete(f"/api/sessions/{session_id}/inputs/{input_id}")

            responses = await asyncio.gather(*[cancel() for _ in range(10)])

        # Exactly one 200 cancelled=true; the rest either 409 (no longer queued)
        # or 404 (already cancelled / race resolved).
        success = [r for r in responses if r.status_code == 200]
        rejects = [r for r in responses if r.status_code in (404, 409)]
        assert len(success) == 1, [r.status_code for r in responses]
        assert len(success) + len(rejects) == 10

        with session_local() as db:
            refreshed = db.query(SessionInput).filter(SessionInput.id == input_id).one()
            assert refreshed.status == "cancelled"
    finally:
        api_app.dependency_overrides = {}


@pytest.mark.asyncio
async def test_stress_cap_exactly_enforced_across_mixed_intents(monkeypatch, tmp_path):
    """Fire 20 queue + 20 auto (under lock) concurrently. Cap is 5 total
    queued rows; any overflow should 409 and no row should land beyond the
    cap. Reported row count must equal min(request_count, cap)."""

    from zerg.services.session_inputs import MAX_QUEUED_PER_SESSION

    session_local = _make_db(tmp_path, "stress_cap")
    session_id, user_id = _seed_managed_session(session_local, transport="claude_channel_bridge")
    _install_dispatch_stubs(monkeypatch)

    lock_scope_id = str(session_id)
    await session_lock_manager.acquire(
        session_id=lock_scope_id,
        holder="stress-cap-holder",
        ttl_seconds=300,
    )

    app, api_app = _configure_app(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://stress",
        ) as client:

            async def fire(i: int, intent: str) -> httpx.Response:
                return await client.post(
                    f"/api/sessions/{session_id}/input",
                    json={"text": f"{intent}-{i}", "intent": intent},
                )

            calls = [fire(i, "queue") for i in range(20)] + [fire(i, "auto") for i in range(20)]
            responses = await asyncio.gather(*calls)

        accepted = sum(1 for r in responses if r.status_code == 200)
        cap_rejections = sum(1 for r in responses if r.status_code == 409)
        assert accepted == MAX_QUEUED_PER_SESSION, f"accepted={accepted} cap={MAX_QUEUED_PER_SESSION}"
        assert accepted + cap_rejections == 40

        with session_local() as db:
            queued_rows = (
                db.query(SessionInput)
                .filter(SessionInput.session_id == session_id, SessionInput.status == "queued")
                .all()
            )
            assert len(queued_rows) == MAX_QUEUED_PER_SESSION
    finally:
        await session_lock_manager.release(lock_scope_id, "stress-cap-holder")
        api_app.dependency_overrides = {}


@pytest.mark.asyncio
async def test_stress_reconciliation_intent_split_at_scale(tmp_path):
    """Seed 30 stuck delivering rows (half auto, half steer) and run the
    boot reconciliation once. Auto rows should requeue; steer rows should
    fail. No row should land in the wrong bucket."""

    from datetime import timedelta

    from zerg.services.session_inputs import create_session_input
    from zerg.services.session_inputs import requeue_stuck_delivering

    session_local = _make_db(tmp_path, "stress_reconcile")
    session_id, user_id = _seed_managed_session(session_local)

    now_stale = datetime.now(timezone.utc) - timedelta(seconds=300)
    auto_ids: list[int] = []
    steer_ids: list[int] = []
    with session_local() as db:
        for i in range(15):
            row = create_session_input(
                db,
                session_id=session_id,
                text=f"auto-{i}",
                owner_id=user_id,
                intent="auto",
                status="delivering",
            )
            row.updated_at = now_stale
            auto_ids.append(int(row.id))
        for i in range(15):
            row = create_session_input(
                db,
                session_id=session_id,
                text=f"steer-{i}",
                owner_id=user_id,
                intent="steer",
                status="delivering",
            )
            row.updated_at = now_stale
            steer_ids.append(int(row.id))
        db.commit()
        requeued = requeue_stuck_delivering(db)
        # Only the 15 auto rows requeue; 15 steer rows flip to failed.
        assert requeued == 15

        db.expire_all()
        auto_rows = db.query(SessionInput).filter(SessionInput.id.in_(auto_ids)).all()
        steer_rows = db.query(SessionInput).filter(SessionInput.id.in_(steer_ids)).all()
        assert all(r.status == "queued" for r in auto_rows)
        assert all(r.status == "failed" for r in steer_rows)
        assert all(r.last_error == "steer interrupted by restart" for r in steer_rows)
