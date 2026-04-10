from __future__ import annotations

import asyncio
import os
from datetime import datetime
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.database import get_db
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.dependencies.oikos_auth import get_current_oikos_user
from zerg.models.device_token import DeviceToken
from zerg.models.enums import UserRole
from zerg.models.user import User
from zerg.routers import session_chat
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest


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
    api_app.dependency_overrides[get_current_oikos_user] = override_current_user
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


def test_managed_local_launch_response_requires_managed_local_execution_home():
    result = SimpleNamespace(
        session=SimpleNamespace(
            id=uuid4(),
            provider="claude",
            provider_session_id="provider-session",
            execution_home="legacy",
            managed_transport="claude_channel_bridge",
            loop_mode="manual",
            source_runner_id=1,
            source_runner_name="cinder",
            managed_session_name="lh-test",
        ),
        attach_command="longhouse claude-channel attach --session-id session-123",
    )

    with pytest.raises(RuntimeError, match="managed_local session"):
        session_chat._managed_local_launch_response(result)


def test_managed_local_launch_response_requires_managed_transport():
    result = SimpleNamespace(
        session=SimpleNamespace(
            id=uuid4(),
            provider="claude",
            provider_session_id="provider-session",
            execution_home="managed_local",
            managed_transport=None,
            loop_mode="manual",
            source_runner_id=1,
            source_runner_name="cinder",
            managed_session_name="lh-test",
        ),
        attach_command="longhouse claude-channel attach --session-id session-123",
    )

    with pytest.raises(RuntimeError, match="managed transport metadata"):
        session_chat._managed_local_launch_response(result)


@pytest.mark.skip(reason="Cloud branch frozen for launch (P1)")
def test_fake_cloud_continuation_persists_turn_for_follow_up_requests(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    project = "resume-send-test"
    source_session_id = uuid4()
    provider_session_id = f"resume-send-{uuid4().hex[:8]}"

    with session_local() as db:
        user = User(email="session-chat@test.local", role=UserRole.USER.value)
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
                project=project,
                device_id="e2e-device",
                cwd="/tmp",
                git_repo=None,
                git_branch=None,
                provider_session_id=provider_session_id,
                started_at=started_at,
                ended_at=started_at,
                events=[
                    EventIngest(
                        role="user",
                        content_text="Started on laptop before cloud branching",
                        timestamp=started_at,
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                    )
                ],
            )
        )
        db.commit()
        user_id = user.id

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="session-chat@test.local", role=UserRole.USER.value),
    )
    monkeypatch.setenv("E2E_FAKE_SESSION_CHAT", "1")

    async def fake_resolve(*, original_cwd, git_repo, git_branch, session_id):
        return SimpleNamespace(path=Path("/tmp"), is_temp=False, error=None)

    monkeypatch.setattr(session_chat.workspace_resolver, "resolve", fake_resolve)

    try:
        response = client.post(
            f"/api/sessions/{source_session_id}/branch-cloud",
            json={"message": "anything else?"},
        )
        assert response.status_code == 200, response.text
        assert '"persisted_events": 2' in response.text
        assert '"created_branch": true' in response.text

        with session_local() as db:
            store = AgentsStore(db)
            source_session = store.get_session(source_session_id)
            assert source_session is not None
            target_session = store.get_thread_head(source_session)
            assert target_session is not None
            assert target_session.id != source_session_id
            assert target_session.project == project
            assert target_session.user_messages == 1
            assert target_session.assistant_messages == 1

            total, rows = store.list_timeline_thread_page(
                project=project,
                since=started_at.replace(hour=0, minute=0, second=0, microsecond=0),
                limit=20,
                offset=0,
                hide_autonomous=True,
            )
            assert total == 1
            assert len(rows) == 1
            assert rows[0][0] == str(source_session_id)
            assert rows[0][1] == str(target_session.id)
    finally:
        api_app_ref.dependency_overrides = {}


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


@pytest.mark.skip(reason="Cloud branch frozen for launch (P1)")
def test_managed_local_claude_cloud_chat_can_continue_when_live_control_is_gone(monkeypatch, tmp_path):
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
        db.commit()
        user_id = user.id

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="session-chat-managed-local@test.local", role=UserRole.USER.value),
    )
    monkeypatch.setenv("E2E_FAKE_SESSION_CHAT", "1")

    async def fake_resolve(*, original_cwd, git_repo, git_branch, session_id):
        return SimpleNamespace(path=Path("/tmp"), is_temp=False, error=None)

    monkeypatch.setattr(session_chat.workspace_resolver, "resolve", fake_resolve)

    try:
        response = client.post(
            f"/api/sessions/{source_session_id}/branch-cloud",
            json={"message": "continue from Longhouse"},
        )
        assert response.status_code == 200, response.text
        assert '"created_branch": true' in response.text
        assert '"persisted_events": 2' in response.text
    finally:
        api_app_ref.dependency_overrides = {}


def test_managed_local_claude_cloud_chat_requires_live_send_when_live_control_exists(tmp_path):
    session_local = _make_db(tmp_path)
    source_session_id = uuid4()
    provider_session_id = f"resume-send-{uuid4().hex[:8]}"

    with session_local() as db:
        user = User(email="session-chat-managed-local-live@test.local", role=UserRole.USER.value)
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
                project="managed-local-live",
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
                        content_text="Started on laptop with a live Longhouse control channel",
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
        source_session.source_runner_id = 42
        source_session.source_runner_name = "cinder"
        source_session.managed_session_name = "lh-claude-live"
        db.commit()
        user_id = user.id

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="session-chat-managed-local-live@test.local", role=UserRole.USER.value),
    )

    try:
        response = client.post(
            f"/api/sessions/{source_session_id}/branch-cloud",
            json={"message": "continue from Longhouse"},
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == "This session currently has live Longhouse control. Use live send instead of starting a cloud branch."
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


def test_synced_codex_session_cloud_chat_is_not_available(tmp_path):
    session_local = _make_db(tmp_path)
    source_session_id = uuid4()
    provider_session_id = f"codex-import-{uuid4().hex[:8]}"

    with session_local() as db:
        user = User(email="session-chat-codex-cloud@test.local", role=UserRole.USER.value)
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
                project="codex-history-only",
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
                        content_text="Imported Codex session",
                        timestamp=started_at,
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                    )
                ],
            )
        )
        db.commit()
        user_id = user.id

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="session-chat-codex-cloud@test.local", role=UserRole.USER.value),
    )

    try:
        response = client.post(
            f"/api/sessions/{source_session_id}/branch-cloud",
            json={"message": "continue from Longhouse"},
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == "Codex sessions are not yet available for cloud branching from Longhouse."
    finally:
        api_app_ref.dependency_overrides = {}


@pytest.mark.skip(reason="Cloud branch frozen for launch (P1)")
def test_agents_continue_route_supports_fake_cloud_continuation(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    project = "agents-continue-test"
    source_session_id = uuid4()
    provider_session_id = f"resume-send-{uuid4().hex[:8]}"

    with session_local() as db:
        user = User(email="agents-continue@test.local", role=UserRole.USER.value)
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
                project=project,
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
                        content_text="Started on agent-device before API continuation",
                        timestamp=started_at,
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                    )
                ],
            )
        )
        db.commit()
        token = DeviceToken(owner_id=user.id, device_id="agent-device", token_hash="test")

    client, api_app_ref = _make_machine_client(session_local, token)
    monkeypatch.setenv("E2E_FAKE_SESSION_CHAT", "1")

    async def fake_resolve(*, original_cwd, git_repo, git_branch, session_id):
        return SimpleNamespace(path=Path("/tmp"), is_temp=False, error=None)

    monkeypatch.setattr(session_chat.workspace_resolver, "resolve", fake_resolve)

    try:
        response = client.post(
            f"/api/agents/sessions/{source_session_id}/branch-cloud",
            json={"message": "continue from the API"},
        )
        assert response.status_code == 200, response.text
        assert '"created_branch": true' in response.text
        assert '"persisted_events": 2' in response.text

        with session_local() as db:
            store = AgentsStore(db)
            source_session = store.get_session(source_session_id)
            assert source_session is not None
            target_session = store.get_thread_head(source_session)
            assert target_session is not None
            assert target_session.id != source_session_id
            assert target_session.project == project
    finally:
        api_app_ref.dependency_overrides = {}


def test_agents_continue_route_rejects_codex_cloud_continuation(tmp_path):
    session_local = _make_db(tmp_path)
    source_session_id = uuid4()
    provider_session_id = f"codex-agents-{uuid4().hex[:8]}"

    with session_local() as db:
        user = User(email="agents-continue-codex@test.local", role=UserRole.USER.value)
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
                project="agents-codex-history-only",
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
                        content_text="Imported Codex session on agent-device",
                        timestamp=started_at,
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                    )
                ],
            )
        )
        db.commit()
        token = DeviceToken(owner_id=user.id, device_id="agent-device", token_hash="test")

    client, api_app_ref = _make_machine_client(session_local, token)

    try:
        response = client.post(
            f"/api/agents/sessions/{source_session_id}/branch-cloud",
            json={"message": "continue from the API"},
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == "Codex sessions are not yet available for cloud branching from Longhouse."
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
    monkeypatch.setattr("zerg.routers.session_chat._schedule_managed_local_lock_release", lambda **_kwargs: None)

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


def test_agents_branch_cloud_route_rejects_other_device(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    source_session_id = uuid4()
    provider_session_id = f"resume-send-{uuid4().hex[:8]}"

    with session_local() as db:
        user = User(email="agents-continue-denied@test.local", role=UserRole.USER.value)
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
                project="auth-test",
                device_id="device-a",
                cwd="/tmp",
                git_repo=None,
                git_branch=None,
                provider_session_id=provider_session_id,
                started_at=started_at,
                ended_at=started_at,
                events=[
                    EventIngest(
                        role="user",
                        content_text="Started on device-a",
                        timestamp=started_at,
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                    )
                ],
            )
        )
        db.commit()
        token = DeviceToken(owner_id=user.id, device_id="device-b", token_hash="test")

    client, api_app_ref = _make_machine_client(session_local, token)
    monkeypatch.setenv("E2E_FAKE_SESSION_CHAT", "1")

    try:
        response = client.post(
            f"/api/agents/sessions/{source_session_id}/branch-cloud",
            json={"message": "should be rejected"},
        )
        assert response.status_code == 403
        assert response.json()["detail"] == "Authenticated device cannot start a cloud branch from a session on another device"
    finally:
        api_app_ref.dependency_overrides = {}


@pytest.mark.skip(reason="Cloud branch frozen for launch (P1)")
def test_agents_continue_route_allows_auth_disabled_without_device_token(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    project = "agents-continue-auth-disabled"
    source_session_id = uuid4()
    provider_session_id = f"resume-send-{uuid4().hex[:8]}"

    with session_local() as db:
        user = User(email="agents-continue-auth-disabled@test.local", role=UserRole.USER.value)
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
                project=project,
                device_id="local-device",
                cwd="/tmp",
                git_repo=None,
                git_branch=None,
                provider_session_id=provider_session_id,
                started_at=started_at,
                ended_at=started_at,
                events=[
                    EventIngest(
                        role="user",
                        content_text="Started on a local auth-disabled instance",
                        timestamp=started_at,
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                    )
                ],
            )
        )
        db.commit()

    client, api_app_ref = _make_machine_client(session_local, None)
    monkeypatch.setenv("E2E_FAKE_SESSION_CHAT", "1")
    monkeypatch.setattr(session_chat, "get_settings", lambda: SimpleNamespace(auth_disabled=True))

    async def fake_resolve(*, original_cwd, git_repo, git_branch, session_id):
        return SimpleNamespace(path=Path("/tmp"), is_temp=False, error=None)

    monkeypatch.setattr(session_chat.workspace_resolver, "resolve", fake_resolve)

    try:
        response = client.post(
            f"/api/agents/sessions/{source_session_id}/branch-cloud",
            json={"message": "continue from localhost without a token"},
        )
        assert response.status_code == 200, response.text
        assert '"created_branch": true' in response.text
    finally:
        api_app_ref.dependency_overrides = {}


def test_agents_branch_cloud_auth_disabled_still_rejects_wrong_current_session_header(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    source_session_id = uuid4()
    provider_session_id = f"resume-send-{uuid4().hex[:8]}"

    with session_local() as db:
        user = User(email="agents-continue-auth-disabled-mismatch@test.local", role=UserRole.USER.value)
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
                project="auth-disabled-header-mismatch",
                device_id="local-device",
                cwd="/tmp",
                git_repo=None,
                git_branch=None,
                provider_session_id=provider_session_id,
                started_at=started_at,
                ended_at=started_at,
                events=[
                    EventIngest(
                        role="user",
                        content_text="Started on localhost",
                        timestamp=started_at,
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                    )
                ],
            )
        )
        db.commit()

    client, api_app_ref = _make_machine_client(session_local, None)
    monkeypatch.setattr(session_chat, "get_settings", lambda: SimpleNamespace(auth_disabled=True))

    try:
        response = client.post(
            f"/api/agents/sessions/{source_session_id}/branch-cloud",
            headers={"X-Longhouse-Session-Id": str(uuid4())},
            json={"message": "this header should fail"},
        )
        assert response.status_code == 403
        assert response.json()["detail"] == "Current session header does not match the target session"
    finally:
        api_app_ref.dependency_overrides = {}
