from __future__ import annotations

import asyncio
import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.database import get_db
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionMessage
from zerg.models.agents import SessionRuntimeState
from zerg.models.models import Runner
from zerg.models.user import User
from zerg.services.machine_control_channel import get_machine_control_channel_registry
from zerg.services.runner_connection_manager import get_runner_connection_manager
from zerg.services.session_runtime import phase_freshness_ms
from zerg.services.session_runtime import runtime_key_for_session


def _make_db(tmp_path):
    db_path = tmp_path / "test_session_messages.db"
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)
    return make_sessionmaker(engine)


def _make_client(session_factory, *, token_device_id: str = "shipper-laptop"):
    from zerg.main import api_app
    from zerg.main import app

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    def override_verify_agents_token():
        return SimpleNamespace(device_id=token_device_id, id="token-1", owner_id=1)

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    return TestClient(app, backend="asyncio"), api_app


def _seed_session(
    db,
    *,
    provider: str = "claude",
    execution_home: str = "unmanaged_local",
    managed_transport: str | None = None,
    source_runner_id: int | None = None,
    source_runner_name: str | None = None,
    device_id: str = "shipper-laptop",
    device_name: str | None = "laptop",
):
    session_id = uuid4()
    session = AgentSession(
        id=session_id,
        provider=provider,
        environment="development",
        project="zerg",
        device_id=device_id,
        device_name=device_name,
        cwd="/Users/davidrose/git/zerg",
        git_repo="git@github.com:cipher982/longhouse.git",
        git_branch="main",
        started_at=datetime.now(timezone.utc),
        provider_session_id=str(session_id),
        thread_root_session_id=session_id,
        continuation_kind="local",
        origin_label=device_id,
        user_messages=1,
        assistant_messages=1,
        tool_calls=0,
        is_writable_head=1,
        is_sidechain=0,
        loop_mode="assist",
        execution_home=execution_home,
        managed_transport=managed_transport,
        source_runner_id=source_runner_id,
        source_runner_name=source_runner_name,
        managed_session_name=f"lh-{session_id.hex[:8]}",
    )
    db.add(session)
    if execution_home == "managed_local" and source_runner_id is not None:
        db.merge(User(id=1, email="test-owner@example.com"))
        runner = Runner(
            id=int(source_runner_id),
            owner_id=1,
            name=source_runner_name or f"runner-{source_runner_id}",
            status="online",
            auth_secret_hash="test",
        )
        db.merge(runner)
        get_runner_connection_manager().register(1, int(source_runner_id), SimpleNamespace())
    db.flush()
    db.refresh(session)
    if execution_home == "managed_local":
        from tests_lite._kernel_test_helpers import seed_managed_kernel_rows

        if managed_transport == "codex_app_server":
            kernel_plane = "codex_bridge"
        elif managed_transport == "opencode_process":
            kernel_plane = "opencode_process"
        else:
            kernel_plane = "claude_channel_bridge"
        seed_managed_kernel_rows(db, session, control_plane=kernel_plane)
    db.commit()
    db.refresh(session)
    return session


def _upsert_runtime_state(db, session: AgentSession, phase: str, *, phase_source: str = "semantic"):
    now = datetime.now(timezone.utc)
    runtime_key = runtime_key_for_session(str(session.provider or "claude"), str(session.id))
    state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).first()
    freshness_ms = phase_freshness_ms(phase) or int(timedelta(minutes=5).total_seconds() * 1000)
    freshness_expires_at = now + timedelta(milliseconds=freshness_ms)
    if state is None:
        state = SessionRuntimeState(
            runtime_key=runtime_key,
            session_id=session.id,
            provider=str(session.provider or "claude"),
            device_id=session.device_id,
            phase=phase,
            phase_source=phase_source,
            active_tool=None,
            phase_started_at=now,
            last_runtime_signal_at=now,
            last_progress_at=now,
            last_live_at=now,
            timeline_anchor_at=now,
            freshness_expires_at=freshness_expires_at,
            terminal_state=None,
            terminal_at=None,
            runtime_version=1,
        )
        db.add(state)
    else:
        state.phase = phase
        state.phase_source = phase_source
        state.active_tool = None
        state.phase_started_at = now
        state.last_runtime_signal_at = now
        state.last_progress_at = now
        state.last_live_at = now
        state.timeline_anchor_at = now
        state.freshness_expires_at = freshness_expires_at
        state.terminal_state = None
        state.terminal_at = None
        state.runtime_version = int(getattr(state, "runtime_version", 0) or 0) + 1
    db.commit()
    db.refresh(state)
    return state




def test_create_message_delivers_immediately_for_safe_managed_local(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    send_calls: list[dict[str, object]] = []

    with session_local() as db:
        from_session = _seed_session(db, execution_home="unmanaged_local")
        to_session = _seed_session(
            db,
            execution_home="managed_local",
            managed_transport="claude_channel_bridge",
            source_runner_id=7,
            source_runner_name="cinder",
            device_id="cinder",
            device_name="cinder",
        )
        _upsert_runtime_state(db, to_session, "idle")

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
        send_calls.append(
            {
                "owner_id": owner_id,
                "session_id": str(session.id),
                "text": text,
                "verify_turn_started": verify_turn_started,
            }
        )
        return SimpleNamespace(ok=True, error=None)

    monkeypatch.setattr("zerg.services.live_session_dispatch.send_text_to_live_session", fake_send_text)

    client, api_app_ref = _make_client(session_local)
    try:
        response = client.post(
            "/api/agents/messages",
            json={
                "from_session_id": str(from_session.id),
                "to_session_id": str(to_session.id),
                "text": "Heads up: auth is broken.",
            },
        )
        assert response.status_code == 201, response.text
        data = response.json()
        assert data["delivery_status"] == "delivered"
        assert len(send_calls) == 1
        assert send_calls[0]["owner_id"] == 1
        assert send_calls[0]["session_id"] == str(to_session.id)
        assert "Heads up: auth is broken." in str(send_calls[0]["text"])

        with session_local() as verify_db:
            message = verify_db.query(SessionMessage).one()
            assert message.delivery_status == "delivered"
            assert message.delivered_at is not None
    finally:
        api_app_ref.dependency_overrides = {}


def test_create_message_queues_when_target_is_running(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        from_session = _seed_session(db, execution_home="unmanaged_local")
        to_session = _seed_session(
            db,
            execution_home="managed_local",
            managed_transport="claude_channel_bridge",
            source_runner_id=7,
            source_runner_name="cinder",
            device_id="cinder",
            device_name="cinder",
        )
        _upsert_runtime_state(db, to_session, "running")

    async def fail_if_called(**_kwargs):
        raise AssertionError("send_text_to_managed_local_session should not be called while running")

    monkeypatch.setattr("zerg.services.live_session_dispatch.send_text_to_live_session", fail_if_called)

    client, api_app_ref = _make_client(session_local)
    try:
        response = client.post(
            "/api/agents/messages",
            json={
                "from_session_id": str(from_session.id),
                "to_session_id": str(to_session.id),
                "text": "Queue this until the current turn ends.",
            },
        )
        assert response.status_code == 201, response.text
        data = response.json()
        assert data["delivery_status"] == "queued"

        with session_local() as verify_db:
            message = verify_db.query(SessionMessage).one()
            assert message.delivery_status == "queued"
            assert message.delivered_at is None
    finally:
        api_app_ref.dependency_overrides = {}


def test_create_message_uses_runtime_state_when_presence_missing(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    send_calls: list[dict[str, object]] = []

    with session_local() as db:
        from_session = _seed_session(db, execution_home="unmanaged_local")
        to_session = _seed_session(
            db,
            provider="codex",
            execution_home="managed_local",
            managed_transport="codex_app_server",
            source_runner_id=7,
            source_runner_name="cinder",
            device_id="cinder",
            device_name="cinder",
        )
        _upsert_runtime_state(db, to_session, "needs_user")

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
        send_calls.append(
            {
                "owner_id": owner_id,
                "session_id": str(session.id),
                "text": text,
            }
        )
        return SimpleNamespace(ok=True, error=None)

    monkeypatch.setattr("zerg.services.live_session_dispatch.send_text_to_live_session", fake_send_text)

    client, api_app_ref = _make_client(session_local)
    try:
        response = client.post(
            "/api/agents/messages",
            json={
                "from_session_id": str(from_session.id),
                "to_session_id": str(to_session.id),
                "text": "Codex should receive this without a presence row.",
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["delivery_status"] == "delivered"
        assert len(send_calls) == 1
        assert send_calls[0]["session_id"] == str(to_session.id)
    finally:
        api_app_ref.dependency_overrides = {}


def test_create_message_delivers_to_engine_controlled_codex_without_runner(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    send_calls: list[dict[str, object]] = []
    registry = get_machine_control_channel_registry()
    asyncio.run(registry.clear_for_tests())

    with session_local() as db:
        from_session = _seed_session(db, execution_home="unmanaged_local")
        to_session = _seed_session(
            db,
            provider="codex",
            execution_home="managed_local",
            managed_transport="codex_app_server",
            source_runner_id=None,
            source_runner_name="cinder",
            device_id="cinder",
            device_name="cinder",
        )
        _upsert_runtime_state(db, to_session, "needs_user", phase_source="codex_bridge")

    asyncio.run(
        registry.register(
            owner_id=1,
            device_id="cinder",
            machine_name="cinder",
            engine_build="abc123",
            supports=["codex.send"],
            websocket=SimpleNamespace(),
        )
    )

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
        send_calls.append(
            {
                "owner_id": owner_id,
                "session_id": str(session.id),
                "text": text,
            }
        )
        return SimpleNamespace(ok=True, error=None)

    monkeypatch.setattr("zerg.services.live_session_dispatch.send_text_to_live_session", fake_send_text)

    client, api_app_ref = _make_client(session_local)
    try:
        response = client.post(
            "/api/agents/messages",
            json={
                "from_session_id": str(from_session.id),
                "to_session_id": str(to_session.id),
                "text": "Engine-channel Codex should receive this.",
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["delivery_status"] == "delivered"
        assert len(send_calls) == 1
        assert send_calls[0]["owner_id"] == 1
        assert send_calls[0]["session_id"] == str(to_session.id)
    finally:
        api_app_ref.dependency_overrides = {}
        asyncio.run(registry.clear_for_tests())


def test_presence_safe_transition_delivers_oldest_queued_message(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    send_calls: list[str] = []

    with session_local() as db:
        from_session = _seed_session(db, execution_home="unmanaged_local")
        to_session = _seed_session(
            db,
            execution_home="managed_local",
            managed_transport="claude_channel_bridge",
            source_runner_id=7,
            source_runner_name="cinder",
            device_id="cinder",
            device_name="cinder",
        )
        db.add(
            SessionMessage(
                from_session_id=from_session.id,
                to_session_id=to_session.id,
                body="Deliver this on the next safe boundary.",
                delivery_status="queued",
            )
        )
        db.commit()

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
        send_calls.append(text)
        return SimpleNamespace(ok=True, error=None)

    monkeypatch.setattr("zerg.services.live_session_dispatch.send_text_to_live_session", fake_send_text)

    client, api_app_ref = _make_client(session_local)
    try:
        response = client.post(
            "/api/agents/presence",
            json={
                "session_id": str(to_session.id),
                "state": "idle",
                "cwd": "/Users/davidrose/git/zerg",
                "provider": "claude",
            },
        )
        assert response.status_code == 204, response.text
        assert len(send_calls) == 1

        with session_local() as verify_db:
            message = verify_db.query(SessionMessage).one()
            assert message.delivery_status == "delivered"
            assert message.delivered_at is not None
    finally:
        api_app_ref.dependency_overrides = {}


def test_runtime_safe_transition_delivers_queued_message_without_presence(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    send_calls: list[str] = []

    with session_local() as db:
        from_session = _seed_session(db, execution_home="unmanaged_local")
        to_session = _seed_session(
            db,
            provider="codex",
            execution_home="managed_local",
            managed_transport="codex_app_server",
            source_runner_id=7,
            source_runner_name="cinder",
            device_id="cinder",
            device_name="cinder",
        )
        db.add(
            SessionMessage(
                from_session_id=from_session.id,
                to_session_id=to_session.id,
                body="Deliver this after the Codex bridge reports idle.",
                delivery_status="queued",
            )
        )
        db.commit()

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
        send_calls.append(text)
        return SimpleNamespace(ok=True, error=None)

    monkeypatch.setattr("zerg.services.live_session_dispatch.send_text_to_live_session", fake_send_text)

    client, api_app_ref = _make_client(session_local)
    try:
        response = client.post(
            "/api/agents/runtime/events/batch",
            json={
                "events": [
                    {
                        "runtime_key": runtime_key_for_session("codex", str(to_session.id)),
                        "session_id": str(to_session.id),
                        "provider": "codex",
                        "device_id": "cinder",
                        "source": "codex_bridge",
                        "kind": "phase_signal",
                        "phase": "idle",
                        "occurred_at": datetime.now(timezone.utc).isoformat(),
                        "freshness_ms": phase_freshness_ms("idle"),
                        "dedupe_key": "codex-idle-1",
                        "payload": {},
                    }
                ]
            },
        )
        assert response.status_code == 200, response.text
        assert len(send_calls) == 1

        with session_local() as verify_db:
            message = verify_db.query(SessionMessage).one()
            assert message.delivery_status == "delivered"
            assert message.delivered_at is not None
    finally:
        api_app_ref.dependency_overrides = {}


def test_presence_safe_transition_drains_multiple_queued_messages(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    send_calls: list[str] = []

    with session_local() as db:
        from_session = _seed_session(db, execution_home="unmanaged_local")
        to_session = _seed_session(
            db,
            execution_home="managed_local",
            managed_transport="claude_channel_bridge",
            source_runner_id=7,
            source_runner_name="cinder",
            device_id="cinder",
            device_name="cinder",
        )
        db.add_all(
            [
                SessionMessage(
                    from_session_id=from_session.id,
                    to_session_id=to_session.id,
                    body="First queued message.",
                    delivery_status="queued",
                ),
                SessionMessage(
                    from_session_id=from_session.id,
                    to_session_id=to_session.id,
                    body="Second queued message.",
                    delivery_status="queued",
                ),
                SessionMessage(
                    from_session_id=from_session.id,
                    to_session_id=to_session.id,
                    body="Third queued message.",
                    delivery_status="queued",
                ),
            ]
        )
        db.commit()

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
        send_calls.append(text)
        return SimpleNamespace(ok=True, error=None)

    monkeypatch.setattr("zerg.services.live_session_dispatch.send_text_to_live_session", fake_send_text)

    client, api_app_ref = _make_client(session_local)
    try:
        response = client.post(
            "/api/agents/presence",
            json={
                "session_id": str(to_session.id),
                "state": "idle",
                "cwd": "/Users/davidrose/git/zerg",
                "provider": "claude",
            },
        )
        assert response.status_code == 204, response.text
        assert len(send_calls) == 3

        with session_local() as verify_db:
            messages = verify_db.query(SessionMessage).order_by(SessionMessage.id.asc()).all()
            assert [message.delivery_status for message in messages] == ["delivered", "delivered", "delivered"]
    finally:
        api_app_ref.dependency_overrides = {}


def test_presence_safe_transition_stops_drain_when_session_leaves_safe_boundary(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    send_calls: list[str] = []

    with session_local() as db:
        from_session = _seed_session(db, execution_home="unmanaged_local")
        to_session = _seed_session(
            db,
            execution_home="managed_local",
            managed_transport="claude_channel_bridge",
            source_runner_id=7,
            source_runner_name="cinder",
            device_id="cinder",
            device_name="cinder",
        )
        db.add_all(
            [
                SessionMessage(
                    from_session_id=from_session.id,
                    to_session_id=to_session.id,
                    body="Deliver once, then stop.",
                    delivery_status="queued",
                ),
                SessionMessage(
                    from_session_id=from_session.id,
                    to_session_id=to_session.id,
                    body="Stay queued because the session is busy.",
                    delivery_status="queued",
                ),
            ]
        )
        db.commit()

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
        send_calls.append(text)
        _upsert_runtime_state(db, session, "running")
        return SimpleNamespace(ok=True, error=None)

    monkeypatch.setattr("zerg.services.live_session_dispatch.send_text_to_live_session", fake_send_text)

    client, api_app_ref = _make_client(session_local)
    try:
        response = client.post(
            "/api/agents/presence",
            json={
                "session_id": str(to_session.id),
                "state": "idle",
                "cwd": "/Users/davidrose/git/zerg",
                "provider": "claude",
            },
        )
        assert response.status_code == 204, response.text
        assert len(send_calls) == 1

        with session_local() as verify_db:
            messages = verify_db.query(SessionMessage).order_by(SessionMessage.id.asc()).all()
            assert [message.delivery_status for message in messages] == ["delivered", "queued"]
    finally:
        api_app_ref.dependency_overrides = {}


def test_presence_stale_safe_payload_does_not_deliver_when_canonical_state_is_busy(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        from_session = _seed_session(db, execution_home="unmanaged_local")
        to_session = _seed_session(
            db,
            execution_home="managed_local",
            managed_transport="claude_channel_bridge",
            source_runner_id=7,
            source_runner_name="cinder",
            device_id="cinder",
            device_name="cinder",
        )
        db.add(
            SessionMessage(
                from_session_id=from_session.id,
                to_session_id=to_session.id,
                body="Do not deliver from stale idle.",
                delivery_status="queued",
            )
        )
        db.commit()

    async def fail_if_called(**_kwargs):
        raise AssertionError("stale safe payload should not deliver when canonical state stays blocked")

    monkeypatch.setattr("zerg.services.live_session_dispatch.send_text_to_live_session", fail_if_called)

    client, api_app_ref = _make_client(session_local)
    now = datetime.now(timezone.utc)
    try:
        blocked_response = client.post(
            "/api/agents/presence",
            json={
                "session_id": str(to_session.id),
                "state": "blocked",
                "tool_name": "Bash",
                "cwd": "/Users/davidrose/git/zerg",
                "provider": "claude",
                "occurred_at": now.isoformat(),
                "dedupe_key": "blocked-new",
            },
        )
        assert blocked_response.status_code == 204, blocked_response.text

        stale_idle_response = client.post(
            "/api/agents/presence",
            json={
                "session_id": str(to_session.id),
                "state": "idle",
                "cwd": "/Users/davidrose/git/zerg",
                "provider": "claude",
                "occurred_at": (now - timedelta(seconds=30)).isoformat(),
                "dedupe_key": "idle-old",
            },
        )
        assert stale_idle_response.status_code == 204, stale_idle_response.text

        with session_local() as verify_db:
            message = verify_db.query(SessionMessage).one()
            assert message.delivery_status == "queued"
            state = (
                verify_db.query(SessionRuntimeState)
                .filter(SessionRuntimeState.session_id == to_session.id)
                .one()
            )
            assert state.phase == "blocked"
    finally:
        api_app_ref.dependency_overrides = {}


def test_create_message_stored_only_for_unmanaged_target(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        from_session = _seed_session(db, execution_home="unmanaged_local")
        to_session = _seed_session(db, execution_home="unmanaged_local", device_id="shipper-cube", device_name="cube")

    async def fail_if_called(**_kwargs):
        raise AssertionError("send_text_to_managed_local_session should not be called for unmanaged sessions")

    monkeypatch.setattr("zerg.services.live_session_dispatch.send_text_to_live_session", fail_if_called)

    client, api_app_ref = _make_client(session_local)
    try:
        response = client.post(
            "/api/agents/messages",
            json={
                "from_session_id": str(from_session.id),
                "to_session_id": str(to_session.id),
                "text": "This should store without push delivery.",
            },
        )
        assert response.status_code == 201, response.text
        data = response.json()
        assert data["delivery_status"] == "stored_only"

        with session_local() as verify_db:
            message = verify_db.query(SessionMessage).one()
            assert message.delivery_status == "stored_only"
            assert message.delivered_at is None
    finally:
        api_app_ref.dependency_overrides = {}


def test_list_messages_returns_inbound_rows_without_mutation(tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        from_session = _seed_session(db, execution_home="unmanaged_local")
        to_session = _seed_session(db, execution_home="unmanaged_local", device_id="shipper-cube", device_name="cube")
        db.add(
            SessionMessage(
                from_session_id=from_session.id,
                to_session_id=to_session.id,
                body="Stored message",
                delivery_status="stored_only",
            )
        )
        db.commit()

    client, api_app_ref = _make_client(session_local, token_device_id="shipper-cube")
    try:
        response = client.get("/api/agents/messages", params={"session_id": str(to_session.id)})
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["total"] == 1
        assert data["messages"][0]["delivery_status"] == "stored_only"

        with session_local() as verify_db:
            message = verify_db.query(SessionMessage).one()
            assert message.delivery_status == "stored_only"
            assert message.acknowledged_at is None
    finally:
        api_app_ref.dependency_overrides = {}


def test_create_message_uses_current_session_header_when_body_omitted(tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        from_session = _seed_session(db, execution_home="unmanaged_local")
        to_session = _seed_session(db, execution_home="unmanaged_local", device_id="shipper-cube", device_name="cube")

    client, api_app_ref = _make_client(session_local)
    try:
        response = client.post(
            "/api/agents/messages",
            headers={"X-Longhouse-Session-Id": str(from_session.id)},
            json={
                "to_session_id": str(to_session.id),
                "text": "header-derived sender",
            },
        )
        assert response.status_code == 201, response.text
        data = response.json()
        assert data["from_session_id"] == str(from_session.id)
        assert data["delivery_status"] == "stored_only"
    finally:
        api_app_ref.dependency_overrides = {}


def test_create_message_rejects_header_body_mismatch(tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        from_session = _seed_session(db, execution_home="unmanaged_local")
        other_session = _seed_session(db, execution_home="unmanaged_local")
        to_session = _seed_session(db, execution_home="unmanaged_local", device_id="shipper-cube", device_name="cube")

    client, api_app_ref = _make_client(session_local)
    try:
        response = client.post(
            "/api/agents/messages",
            headers={"X-Longhouse-Session-Id": str(from_session.id)},
            json={
                "from_session_id": str(other_session.id),
                "to_session_id": str(to_session.id),
                "text": "should fail",
            },
        )
        assert response.status_code == 403, response.text
        assert "does not match" in response.text
    finally:
        api_app_ref.dependency_overrides = {}


def test_list_messages_rejects_device_session_mismatch(tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        from_session = _seed_session(db, execution_home="unmanaged_local")
        to_session = _seed_session(db, execution_home="unmanaged_local", device_id="shipper-cube", device_name="cube")
        db.add(
            SessionMessage(
                from_session_id=from_session.id,
                to_session_id=to_session.id,
                body="Stored message",
                delivery_status="stored_only",
            )
        )
        db.commit()

    client, api_app_ref = _make_client(session_local, token_device_id="shipper-laptop")
    try:
        response = client.get("/api/agents/messages", params={"session_id": str(to_session.id)})
        assert response.status_code == 403, response.text
        assert "Authenticated device cannot act" in response.text
    finally:
        api_app_ref.dependency_overrides = {}


def test_acknowledge_message_sets_acknowledged_at_and_filters_unacknowledged(tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        from_session = _seed_session(db, execution_home="unmanaged_local")
        to_session = _seed_session(db, execution_home="unmanaged_local", device_id="shipper-cube", device_name="cube")
        db.add(
            SessionMessage(
                from_session_id=from_session.id,
                to_session_id=to_session.id,
                body="Stored message",
                delivery_status="stored_only",
            )
        )
        db.commit()
        message = db.query(SessionMessage).one()
        message_id = message.id

    client, api_app_ref = _make_client(session_local, token_device_id="shipper-cube")
    try:
        ack_response = client.post(
            f"/api/agents/messages/{message_id}/ack",
            headers={"X-Longhouse-Session-Id": str(to_session.id)},
        )
        assert ack_response.status_code == 200, ack_response.text
        assert ack_response.json()["acknowledged_at"] is not None

        unacked_response = client.get(
            "/api/agents/messages",
            headers={"X-Longhouse-Session-Id": str(to_session.id)},
            params={"direction": "inbound", "unacknowledged_only": True},
        )
        assert unacked_response.status_code == 200, unacked_response.text
        assert unacked_response.json()["total"] == 0
    finally:
        api_app_ref.dependency_overrides = {}


def test_acknowledge_message_rejects_queued_delivery(tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        from_session = _seed_session(db, execution_home="unmanaged_local")
        to_session = _seed_session(db, execution_home="managed_local", device_id="shipper-cube", device_name="cube")
        db.add(
            SessionMessage(
                from_session_id=from_session.id,
                to_session_id=to_session.id,
                body="Queued message",
                delivery_status="queued",
            )
        )
        db.commit()
        message = db.query(SessionMessage).one()
        message_id = message.id

    client, api_app_ref = _make_client(session_local, token_device_id="shipper-cube")
    try:
        response = client.post(
            f"/api/agents/messages/{message_id}/ack",
            headers={"X-Longhouse-Session-Id": str(to_session.id)},
        )
        assert response.status_code == 409, response.text
        assert "has not been delivered" in response.text
    finally:
        api_app_ref.dependency_overrides = {}
