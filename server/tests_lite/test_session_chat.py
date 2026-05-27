from __future__ import annotations

import asyncio
import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.database import get_db
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.dependencies.browser_route_auth import get_current_browser_route_user
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionInput
from zerg.models.agents import SessionRuntimeState
from zerg.models.device_token import DeviceToken
from zerg.models.enums import UserRole
from zerg.models.models import Runner
from zerg.models.user import User
from zerg.routers import session_chat
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest
from zerg.services.runner_connection_manager import get_runner_connection_manager
from zerg.services.session_chat_impl import _session_is_closed_for_input
from zerg.services.session_runtime import phase_freshness_ms
from zerg.services.session_runtime import runtime_key_for_session


def _make_db(tmp_path):
    db_path = tmp_path / "test_session_chat.db"
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)
    return make_sessionmaker(engine)


def _make_client(session_local, current_user):
    from zerg.main import api_app
    from zerg.main import app

    def override_get_db():
        db = session_local()
        try:
            yield db
        finally:
            db.close()

    def override_current_user():
        return current_user

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[get_current_browser_route_user] = override_current_user
    return TestClient(app, backend="asyncio"), api_app


def _make_machine_client(session_local, device_token):
    from zerg.main import api_app
    from zerg.main import app

    def override_get_db():
        db = session_local()
        try:
            yield db
        finally:
            db.close()

    def override_verify():
        return device_token

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[verify_agents_token] = override_verify
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    return TestClient(app, backend="asyncio"), api_app


def _mark_session_live(db, session, *, owner_id: int, phase: str = "idle") -> None:
    from tests_lite._kernel_test_helpers import seed_managed_kernel_rows

    runner_id = int(session.source_runner_id)
    runner = Runner(
        id=runner_id,
        owner_id=owner_id,
        name=session.source_runner_name or f"runner-{runner_id}",
        status="online",
        auth_secret_hash="test",
    )
    db.merge(runner)
    get_runner_connection_manager().register(owner_id, runner_id, SimpleNamespace())
    provider = (session.provider or "claude").strip().lower()
    if provider == "codex":
        plane = "codex_bridge"
    elif provider == "opencode":
        plane = "opencode_process"
    else:
        plane = "claude_channel_bridge"
    seed_managed_kernel_rows(db, session, control_plane=plane)
    db.commit()

    now = datetime.now(timezone.utc)
    freshness_ms = phase_freshness_ms(phase) or int(timedelta(minutes=5).total_seconds() * 1000)
    key = runtime_key_for_session(str(session.provider or "claude"), str(session.id))
    state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == key).first()
    if state is None:
        state = SessionRuntimeState(
            runtime_key=key,
            session_id=session.id,
            provider=str(session.provider or "claude"),
            device_id=session.device_id,
        )
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


def _seed_kernel_session(session_local, *, provider: str, with_kernel_rows: bool, control_plane: str | None = None):
    """Create a real AgentSession with optional kernel rows.

    The kernel projection — not legacy ``execution_home`` columns — drives the
    launch-response gate, so these helper-built sessions exercise the same
    branches the SimpleNamespace placeholders used to.
    """

    from tests_lite._kernel_test_helpers import seed_managed_kernel_rows
    from zerg.models.agents import AgentSession

    sid = uuid4()
    with session_local() as db:
        user = User(email=f"launch-resp-{uuid4().hex[:6]}@test.local", role=UserRole.USER.value)
        db.add(user)
        db.commit()
        db.refresh(user)
        session = AgentSession(
            id=sid,
            provider=provider,
            environment="dev",
            project="zerg",
            started_at=datetime.now(timezone.utc),
            provider_session_id="provider-session",
            thread_root_session_id=sid,
            user_messages=0,
            assistant_messages=0,
            tool_calls=0,
            loop_mode="assist",
            source_runner_id=1,
            source_runner_name="cinder",
            managed_session_name="lh-test",
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        if with_kernel_rows:
            seed_managed_kernel_rows(
                db,
                session,
                control_plane=control_plane or ("codex_bridge" if provider == "codex" else "claude_channel_bridge"),
            )
            db.commit()
            db.refresh(session)
    return sid


def _seed_live_input_session(session_local, *, provider: str = "claude", phase: str = "idle"):
    source_session_id = uuid4()
    provider_session_id = f"{provider}-input-{uuid4().hex[:8]}"
    with session_local() as db:
        user = User(email=f"{provider}-input-{uuid4().hex[:6]}@test.local", role=UserRole.USER.value)
        db.add(user)
        db.commit()
        db.refresh(user)

        store = AgentsStore(db)
        started_at = datetime.now(timezone.utc)
        store.ingest_session(
            SessionIngest(
                id=source_session_id,
                provider=provider,
                environment="Cinder",
                project="session-input",
                device_id="agent-device",
                cwd="/tmp",
                git_repo=None,
                git_branch=None,
                provider_session_id=provider_session_id,
                started_at=started_at,
                ended_at=started_at,
                events=[
                    EventIngest(
                        role="user",
                        content_text="Started before Longhouse input test",
                        timestamp=started_at,
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                    )
                ],
            )
        )
        source_session = store.get_session(source_session_id)
        assert source_session is not None
        source_session.execution_home = "managed_local"
        source_session.managed_transport = "claude_channel_bridge" if provider == "claude" else "codex_app_server"
        source_session.source_runner_id = 1
        source_session.source_runner_name = "agent-device"
        source_session.managed_session_name = f"lh-{provider}-input"
        db.commit()
        _mark_session_live(db, source_session, owner_id=user.id, phase=phase)
        return source_session_id, user.id


@pytest.mark.parametrize("terminal_state", ["finished", "host_expired"])
def test_turn_or_unverified_terminal_state_does_not_block_session_input(tmp_path, terminal_state):
    session_local = _make_db(tmp_path)
    now = datetime.now(timezone.utc)

    with session_local() as db:
        source_session = AgentSession(
            provider="claude",
            environment="test",
            project="zerg",
            started_at=now - timedelta(minutes=5),
            user_messages=1,
            assistant_messages=1,
            tool_calls=0,
        )
        db.add(source_session)
        db.flush()
        db.add(
            SessionRuntimeState(
                runtime_key=f"claude:{source_session.id}",
                session_id=source_session.id,
                provider="claude",
                device_id="cinder",
                phase="finished",
                phase_source="semantic",
                phase_started_at=now,
                last_runtime_signal_at=now,
                last_live_at=now,
                timeline_anchor_at=now,
                freshness_expires_at=now,
                terminal_state=terminal_state,
                terminal_at=now,
                runtime_version=1,
            )
        )
        db.commit()

        assert not _session_is_closed_for_input(db, source_session)


@pytest.mark.parametrize("terminal_state", ["session_ended", "process_gone", "user_closed"])
def test_irreversible_terminal_state_blocks_session_input(tmp_path, terminal_state):
    session_local = _make_db(tmp_path)
    now = datetime.now(timezone.utc)

    with session_local() as db:
        source_session = AgentSession(
            provider="claude",
            environment="test",
            project="zerg",
            started_at=now - timedelta(minutes=5),
            user_messages=1,
            assistant_messages=1,
            tool_calls=0,
        )
        db.add(source_session)
        db.flush()
        db.add(
            SessionRuntimeState(
                runtime_key=f"claude:{source_session.id}",
                session_id=source_session.id,
                provider="claude",
                device_id="cinder",
                phase="finished",
                phase_source="semantic",
                phase_started_at=now,
                last_runtime_signal_at=now,
                last_live_at=now,
                timeline_anchor_at=now,
                freshness_expires_at=now,
                terminal_state=terminal_state,
                terminal_at=now,
                runtime_version=1,
            )
        )
        db.commit()

        assert _session_is_closed_for_input(db, source_session)


def test_managed_local_launch_response_requires_managed_local_execution_home(tmp_path):
    session_local = _make_db(tmp_path)
    sid = _seed_kernel_session(session_local, provider="claude", with_kernel_rows=False)

    with session_local() as db:
        from zerg.models.agents import AgentSession

        session = db.query(AgentSession).filter_by(id=sid).one()
        result = SimpleNamespace(
            session=session,
            attach_command="longhouse claude-channel attach --session-id session-123",
        )
        with pytest.raises(RuntimeError, match="kernel-managed session"):
            session_chat._managed_local_launch_response(db, result)


def test_managed_local_launch_response_requires_managed_transport(tmp_path):
    session_local = _make_db(tmp_path)
    sid = _seed_kernel_session(
        session_local,
        provider="claude",
        with_kernel_rows=True,
        control_plane="bogus_plane",  # not in adapter map → managed_transport=None
    )

    with session_local() as db:
        from zerg.models.agents import AgentSession

        session = db.query(AgentSession).filter_by(id=sid).one()
        result = SimpleNamespace(
            session=session,
            attach_command="longhouse claude-channel attach --session-id session-123",
        )
        with pytest.raises(RuntimeError, match="managed transport metadata"):
            session_chat._managed_local_launch_response(db, result)
def test_managed_local_claude_live_send_requires_live_control(tmp_path):
    session_local = _make_db(tmp_path)
    source_session_id = uuid4()
    provider_session_id = f"resume-send-{uuid4().hex[:8]}"

    with session_local() as db:
        user = User(email="session-chat-managed-local@test.local", role=UserRole.USER.value)
        db.add(user)
        db.commit()
        db.refresh(user)

        store = AgentsStore(db)
        started_at = datetime.now(timezone.utc)
        store.ingest_session(
            SessionIngest(
                id=source_session_id,
                provider="claude",
                environment="Cinder",
                project="managed-local-fallback",
                device_id="cinder",
                cwd="/tmp",
                git_repo=None,
                git_branch=None,
                provider_session_id=provider_session_id,
                started_at=started_at,
                ended_at=started_at,
                events=[
                    EventIngest(
                        role="user",
                        content_text="Started on laptop before Longhouse lost the live channel",
                        timestamp=started_at,
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                    )
                ],
            )
        )
        source_session = store.get_session(source_session_id)
        assert source_session is not None
        source_session.execution_home = "managed_local"
        source_session.managed_transport = "claude_channel_bridge"
        source_session.source_runner_id = None
        source_session.source_runner_name = "cinder"
        source_session.managed_session_name = "lh-claude-no-runner"
        from tests_lite._kernel_test_helpers import seed_managed_kernel_rows

        seed_managed_kernel_rows(
            db,
            source_session,
            control_plane="claude_channel_bridge",
            state="detached",  # reattachable bucket: no live control, but host attach is possible
        )
        db.commit()
        user_id = user.id

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="session-chat-managed-local@test.local", role=UserRole.USER.value),
    )

    try:
        response = client.post(
            f"/api/sessions/{source_session_id}/send-live",
            json={"message": "continue from Longhouse"},
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == "This live session needs host attach before Longhouse can continue it."
    finally:
        api_app_ref.dependency_overrides = {}
def test_managed_local_codex_live_send_requires_host_attach(tmp_path):
    session_local = _make_db(tmp_path)
    source_session_id = uuid4()
    provider_session_id = f"codex-managed-local-{uuid4().hex[:8]}"

    with session_local() as db:
        user = User(email="session-chat-managed-local-codex@test.local", role=UserRole.USER.value)
        db.add(user)
        db.commit()
        db.refresh(user)

        store = AgentsStore(db)
        started_at = datetime.now(timezone.utc)
        store.ingest_session(
            SessionIngest(
                id=source_session_id,
                provider="codex",
                environment="Cinder",
                project="managed-local-codex-host-attach",
                device_id="cinder",
                cwd="/tmp",
                git_repo=None,
                git_branch=None,
                provider_session_id=provider_session_id,
                started_at=started_at,
                ended_at=started_at,
                events=[
                    EventIngest(
                        role="user",
                        content_text="Started on laptop before Longhouse lost the Codex channel",
                        timestamp=started_at,
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                    )
                ],
            )
        )
        source_session = store.get_session(source_session_id)
        assert source_session is not None
        source_session.execution_home = "managed_local"
        source_session.managed_transport = "codex_app_server"
        source_session.source_runner_id = None
        source_session.source_runner_name = "cinder"
        source_session.managed_session_name = "lh-codex-no-runner"
        from tests_lite._kernel_test_helpers import seed_managed_kernel_rows

        seed_managed_kernel_rows(
            db, source_session, control_plane="codex_bridge", state="detached"
        )
        db.commit()
        user_id = user.id

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="session-chat-managed-local-codex@test.local", role=UserRole.USER.value),
    )

    try:
        response = client.post(
            f"/api/sessions/{source_session_id}/send-live",
            json={"message": "continue from Longhouse"},
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == "This live session needs host attach before Longhouse can continue it."
    finally:
        api_app_ref.dependency_overrides = {}


def test_explicit_claude_steer_rejects_idle_turn_without_dispatch(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    source_session_id, user_id = _seed_live_input_session(session_local, provider="claude", phase="idle")
    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="claude-steer-idle@test.local", role=UserRole.USER.value),
    )

    async def fail_steer(**_kwargs):
        pytest.fail("idle intent=steer must be rejected before dispatch")

    monkeypatch.setattr("zerg.services.managed_local_control.steer_text_to_managed_local_session", fail_steer)

    try:
        response = client.post(
            f"/api/sessions/{source_session_id}/input",
            json={"text": "correct the active turn", "intent": "steer"},
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["error_code"] == "turn_not_active"
        with session_local() as db:
            assert db.query(SessionInput).filter(SessionInput.session_id == source_session_id).count() == 0
    finally:
        api_app_ref.dependency_overrides = {}


def test_explicit_claude_steer_dispatches_during_active_turn(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    source_session_id, user_id = _seed_live_input_session(session_local, provider="claude", phase="running")
    calls: list[dict[str, object]] = []
    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="claude-steer-active@test.local", role=UserRole.USER.value),
    )

    async def fake_steer(*, db, owner_id, session, text, commis_id=None):
        calls.append(
            {
                "owner_id": owner_id,
                "session_id": str(session.id),
                "text": text,
                "commis_id": commis_id,
            }
        )
        return SimpleNamespace(ok=True, exit_code=0, error=None)

    monkeypatch.setattr("zerg.services.managed_local_control.steer_text_to_managed_local_session", fake_steer)

    try:
        response = client.post(
            f"/api/sessions/{source_session_id}/input",
            json={"text": "correct the active turn", "intent": "steer"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["outcome"] == "sent"
        assert payload["intent"] == "steer"
        assert len(calls) == 1
        assert calls[0]["owner_id"] == user_id
        assert calls[0]["session_id"] == str(source_session_id)
        assert calls[0]["text"] == "correct the active turn"
        assert calls[0]["commis_id"]
    finally:
        api_app_ref.dependency_overrides = {}
def test_agents_send_live_route_ignores_device_mismatch_and_dispatches(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    source_session_id = uuid4()
    provider_session_id = f"managed-live-{uuid4().hex[:8]}"
    calls: list[dict[str, object]] = []

    with session_local() as db:
        user = User(email="agents-send-live@test.local", role=UserRole.USER.value)
        db.add(user)
        db.commit()
        db.refresh(user)

        store = AgentsStore(db)
        started_at = datetime.now(timezone.utc)
        store.ingest_session(
            SessionIngest(
                id=source_session_id,
                provider="claude",
                environment="Cinder",
                project="agents-send-live",
                device_id="agent-device",
                cwd="/tmp",
                git_repo=None,
                git_branch=None,
                provider_session_id=provider_session_id,
                started_at=started_at,
                ended_at=started_at,
                events=[
                    EventIngest(
                        role="user",
                        content_text="Started on agent-device before live send",
                        timestamp=started_at,
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                    )
                ],
            )
        )
        source_session = store.get_session(source_session_id)
        assert source_session is not None
        source_session.execution_home = "managed_local"
        source_session.managed_transport = "claude_channel_bridge"
        source_session.source_runner_id = 1
        source_session.source_runner_name = "agent-device"
        source_session.managed_session_name = "lh-agent-send-live"
        db.commit()
        _mark_session_live(db, source_session, owner_id=user.id)
        token = DeviceToken(owner_id=user.id, device_id="different-machine-label", token_hash="test")

    client, api_app_ref = _make_machine_client(session_local, token)

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
        calls.append(
            {
                "owner_id": owner_id,
                "session_id": str(session.id),
                "text": text,
                "commis_id": commis_id,
                "verify_turn_started": verify_turn_started,
                "verification_timeout_secs": verification_timeout_secs,
            }
        )
        return SimpleNamespace(ok=True, exit_code=0, error=None, verified_turn_started=True)

    monkeypatch.setattr("zerg.services.live_session_dispatch.send_text_to_live_session", fake_send_text)
    monkeypatch.setattr("zerg.services.session_chat_impl._schedule_managed_local_lock_release", lambda **_kwargs: None)

    try:
        response = client.post(
            f"/api/agents/sessions/{source_session_id}/send-live",
            json={"message": "continue locally from the API"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["accepted"] is True
        assert payload["session_id"] == str(source_session_id)
        assert len(calls) == 1
        assert calls[0]["owner_id"] == token.owner_id
        assert calls[0]["text"] == "continue locally from the API"
        assert calls[0]["verify_turn_started"] is True
        assert calls[0]["verification_timeout_secs"] == 15.0
    finally:
        asyncio.run(session_chat.session_lock_manager.release(str(source_session_id)))
        api_app_ref.dependency_overrides = {}


@pytest.mark.parametrize("terminal_state", ["session_ended", "provider_disconnected"])
def test_agents_send_live_rejects_runtime_closed_session(monkeypatch, tmp_path, terminal_state):
    session_local = _make_db(tmp_path)
    source_session_id = uuid4()
    provider_session_id = f"managed-closed-{uuid4().hex[:8]}"

    with session_local() as db:
        user = User(email="agents-send-closed@test.local", role=UserRole.USER.value)
        db.add(user)
        db.commit()
        db.refresh(user)

        store = AgentsStore(db)
        started_at = datetime.now(timezone.utc)
        store.ingest_session(
            SessionIngest(
                id=source_session_id,
                provider="codex",
                environment="Cinder",
                project="agents-send-closed",
                device_id="agent-device",
                cwd="/tmp",
                git_repo=None,
                git_branch=None,
                provider_session_id=provider_session_id,
                started_at=started_at,
                ended_at=started_at,
                events=[],
            )
        )
        source_session = store.get_session(source_session_id)
        assert source_session is not None
        source_session.execution_home = "managed_local"
        source_session.managed_transport = "codex_app_server"
        source_session.source_runner_id = 1
        source_session.source_runner_name = "agent-device"
        db.commit()
        _mark_session_live(db, source_session, owner_id=user.id)
        state = (
            db.query(SessionRuntimeState)
            .filter(SessionRuntimeState.session_id == source_session.id)
            .one()
        )
        state.phase = "finished"
        state.terminal_state = terminal_state
        state.terminal_at = datetime.now(timezone.utc)
        db.commit()
        token = DeviceToken(owner_id=user.id, device_id="agent-device", token_hash="test")

    client, api_app_ref = _make_machine_client(session_local, token)
    monkeypatch.setattr(
        "zerg.services.live_session_dispatch.send_text_to_live_session",
        lambda **_kwargs: pytest.fail("closed session should not dispatch"),
    )

    try:
        response = client.post(
            f"/api/agents/sessions/{source_session_id}/send-live",
            json={"message": "this should not send"},
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == {
            "error_code": "session_closed",
            "message": "This session has ended.",
        }
    finally:
        api_app_ref.dependency_overrides = {}


def test_agents_interrupt_live_route_dispatches_and_releases_lock(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    source_session_id = uuid4()
    provider_session_id = f"managed-interrupt-{uuid4().hex[:8]}"
    calls: list[dict[str, object]] = []

    with session_local() as db:
        user = User(email="agents-interrupt-live@test.local", role=UserRole.USER.value)
        db.add(user)
        db.commit()
        db.refresh(user)

        store = AgentsStore(db)
        started_at = datetime.now(timezone.utc)
        store.ingest_session(
            SessionIngest(
                id=source_session_id,
                provider="claude",
                environment="Cinder",
                project="agents-interrupt-live",
                device_id="agent-device",
                cwd="/tmp",
                git_repo=None,
                git_branch=None,
                provider_session_id=provider_session_id,
                started_at=started_at,
                ended_at=started_at,
                events=[
                    EventIngest(
                        role="user",
                        content_text="Started on agent-device before interrupt",
                        timestamp=started_at,
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                    )
                ],
            )
        )
        source_session = store.get_session(source_session_id)
        assert source_session is not None
        source_session.execution_home = "managed_local"
        source_session.managed_transport = "claude_channel_bridge"
        source_session.source_runner_id = 1
        source_session.source_runner_name = "agent-device"
        source_session.managed_session_name = "lh-agent-interrupt-live"
        db.commit()
        _mark_session_live(db, source_session, owner_id=user.id)
        token = DeviceToken(owner_id=user.id, device_id="different-machine-label", token_hash="test")

    client, api_app_ref = _make_machine_client(session_local, token)

    async def fake_interrupt(*, db, owner_id, session, commis_id=None, timeout_secs=15):
        calls.append(
            {
                "owner_id": owner_id,
                "session_id": str(session.id),
                "commis_id": commis_id,
                "timeout_secs": timeout_secs,
            }
        )
        return SimpleNamespace(ok=True, exit_code=0, error=None)

    monkeypatch.setattr("zerg.services.managed_local_control.interrupt_managed_local_session", fake_interrupt)
    asyncio.run(session_chat.session_lock_manager.acquire(str(source_session_id), holder="stalled-turn"))

    try:
        response = client.post(f"/api/agents/sessions/{source_session_id}/interrupt-live")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["interrupt_dispatched"] is True
        assert payload["confirmed_stopped"] is False
        assert payload["session_id"] == str(source_session_id)
        assert payload["released_lock"] is True
        assert len(calls) == 1
        assert calls[0]["owner_id"] == token.owner_id
        assert calls[0]["session_id"] == str(source_session_id)
    finally:
        asyncio.run(session_chat.session_lock_manager.release(str(source_session_id)))
        api_app_ref.dependency_overrides = {}


def test_browser_interrupt_live_route_dispatches_and_releases_lock(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    source_session_id = uuid4()
    provider_session_id = f"browser-managed-interrupt-{uuid4().hex[:8]}"
    calls: list[dict[str, object]] = []

    with session_local() as db:
        user = User(email="browser-interrupt-live@test.local", role=UserRole.USER.value)
        db.add(user)
        db.commit()
        db.refresh(user)

        store = AgentsStore(db)
        started_at = datetime.now(timezone.utc)
        store.ingest_session(
            SessionIngest(
                id=source_session_id,
                provider="claude",
                environment="Cinder",
                project="browser-interrupt-live",
                device_id="agent-device",
                cwd="/tmp",
                git_repo=None,
                git_branch=None,
                provider_session_id=provider_session_id,
                started_at=started_at,
                ended_at=started_at,
                events=[
                    EventIngest(
                        role="user",
                        content_text="Started on agent-device before browser interrupt",
                        timestamp=started_at,
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                    )
                ],
            )
        )
        source_session = store.get_session(source_session_id)
        assert source_session is not None
        source_session.execution_home = "managed_local"
        source_session.managed_transport = "claude_channel_bridge"
        source_session.source_runner_id = 1
        source_session.source_runner_name = "agent-device"
        source_session.managed_session_name = "lh-browser-interrupt-live"
        db.commit()
        _mark_session_live(db, source_session, owner_id=user.id)
        user_id = user.id

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="browser-interrupt-live@test.local", role=UserRole.USER.value),
    )

    async def fake_interrupt(*, db, owner_id, session, commis_id=None, timeout_secs=15):
        calls.append(
            {
                "owner_id": owner_id,
                "session_id": str(session.id),
                "commis_id": commis_id,
                "timeout_secs": timeout_secs,
            }
        )
        return SimpleNamespace(ok=True, exit_code=0, error=None)

    monkeypatch.setattr("zerg.services.managed_local_control.interrupt_managed_local_session", fake_interrupt)
    asyncio.run(session_chat.session_lock_manager.acquire(str(source_session_id), holder="stalled-turn"))

    try:
        response = client.post(f"/api/sessions/{source_session_id}/interrupt-live")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["interrupt_dispatched"] is True
        assert payload["confirmed_stopped"] is False
        assert payload["session_id"] == str(source_session_id)
        assert payload["released_lock"] is True
        assert len(calls) == 1
        assert calls[0]["owner_id"] == user_id
        assert calls[0]["session_id"] == str(source_session_id)
    finally:
        asyncio.run(session_chat.session_lock_manager.release(str(source_session_id)))
        api_app_ref.dependency_overrides = {}


def test_agents_interrupt_live_route_releases_lock_on_dispatch_failure(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    source_session_id = uuid4()
    provider_session_id = f"managed-interrupt-fail-{uuid4().hex[:8]}"

    with session_local() as db:
        user = User(email="agents-interrupt-fail@test.local", role=UserRole.USER.value)
        db.add(user)
        db.commit()
        db.refresh(user)

        store = AgentsStore(db)
        started_at = datetime.now(timezone.utc)
        store.ingest_session(
            SessionIngest(
                id=source_session_id,
                provider="claude",
                environment="Cinder",
                project="agents-interrupt-fail",
                device_id="agent-device",
                cwd="/tmp",
                git_repo=None,
                git_branch=None,
                provider_session_id=provider_session_id,
                started_at=started_at,
                ended_at=started_at,
                events=[
                    EventIngest(
                        role="user",
                        content_text="Started on agent-device before failed interrupt",
                        timestamp=started_at,
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                    )
                ],
            )
        )
        source_session = store.get_session(source_session_id)
        assert source_session is not None
        source_session.execution_home = "managed_local"
        source_session.managed_transport = "claude_channel_bridge"
        source_session.source_runner_id = 1
        source_session.source_runner_name = "agent-device"
        source_session.managed_session_name = "lh-agent-interrupt-fail"
        db.commit()
        _mark_session_live(db, source_session, owner_id=user.id)
        token = DeviceToken(owner_id=user.id, device_id="different-machine-label", token_hash="test")

    client, api_app_ref = _make_machine_client(session_local, token)

    async def fake_interrupt(*, db, owner_id, session, commis_id=None, timeout_secs=15):
        return SimpleNamespace(ok=False, exit_code=7, error="interrupt failed")

    monkeypatch.setattr("zerg.services.managed_local_control.interrupt_managed_local_session", fake_interrupt)
    asyncio.run(session_chat.session_lock_manager.acquire(str(source_session_id), holder="stalled-turn"))

    try:
        response = client.post(f"/api/agents/sessions/{source_session_id}/interrupt-live")
        assert response.status_code == 502, response.text
        detail = response.json()["detail"]
        assert detail["error_code"] == "interrupt_failed"
        assert detail["exit_code"] == 7
        assert detail["released_lock"] is True
        assert detail["confirmed_stopped"] is False
        assert asyncio.run(session_chat.session_lock_manager.get_lock_info(str(source_session_id))) is None
    finally:
        asyncio.run(session_chat.session_lock_manager.release(str(source_session_id)))
        api_app_ref.dependency_overrides = {}
