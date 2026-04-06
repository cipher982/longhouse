from __future__ import annotations

import asyncio
import os
from datetime import datetime
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
from zerg.dependencies.oikos_auth import get_current_oikos_user
from zerg.models.agents import AgentSession
from zerg.models.agents import ManagedLocalTurn
from zerg.models.enums import UserRole
from zerg.models.models import Runner
from zerg.models.user import User
from zerg.routers import session_chat
from zerg.services.session_continuity import session_lock_manager


def _make_db(tmp_path):
    db_path = tmp_path / "test_managed_local_session_chat.db"
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)
    return make_sessionmaker(engine)


def _make_client(db_session, current_user):
    from zerg.main import api_app
    from zerg.main import app

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    def override_current_user():
        return current_user

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[get_current_oikos_user] = override_current_user
    return TestClient(app, backend="asyncio"), api_app


def _seed_user_and_runner(db):
    user = User(email="managed-local-chat@test.local", role=UserRole.USER.value)
    db.add(user)
    db.commit()
    db.refresh(user)

    runner = Runner(
        owner_id=user.id,
        name="cinder",
        availability_policy="always_on",
        capabilities=["exec.full"],
        status="online",
        auth_secret_hash="secret-hash",
        runner_metadata={"install_mode": "desktop"},
    )
    db.add(runner)
    db.commit()
    db.refresh(runner)
    return user, runner


def _seed_managed_local_session(db, *, runner: Runner, provider: str = "claude") -> AgentSession:
    session_id = uuid4()
    session = AgentSession(
        id=session_id,
        provider=provider,
        environment="development",
        project="hiring",
        device_id=runner.name,
        cwd="/Users/davidrose/git/zeta/hiring",
        git_repo="git@github.com:cipher982/longhouse.git",
        git_branch="main",
        started_at=datetime.now(timezone.utc),
        provider_session_id=str(uuid4()),
        thread_root_session_id=session_id,
        continuation_kind="local",
        origin_label=runner.name,
        user_messages=1,
        assistant_messages=1,
        tool_calls=0,
        is_writable_head=1,
        is_sidechain=0,
        loop_mode="assist",
        execution_home="managed_local",
        managed_transport="tmux",
        source_runner_id=runner.id,
        source_runner_name=runner.name,
        managed_session_name="lh-hiring-chat-1234",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


# ---------------------------------------------------------------------------
# Unit tests for helper functions
# ---------------------------------------------------------------------------


def test_managed_local_events_include_expected_turn_requires_current_prompt_and_reply():
    prompt = "continue"

    assert session_chat._managed_local_events_include_expected_turn(
        events=[
            SimpleNamespace(role="system", content_text="snapshot", tool_name=None),
            SimpleNamespace(role="user", content_text=prompt, tool_name=None),
            SimpleNamespace(role="assistant", content_text="done", tool_name=None),
        ],
        expected_user_message=prompt,
    )

    assert not session_chat._managed_local_events_include_expected_turn(
        events=[
            SimpleNamespace(role="system", content_text="snapshot", tool_name=None),
            SimpleNamespace(role="assistant", content_text="done", tool_name=None),
        ],
        expected_user_message=prompt,
    )

    assert not session_chat._managed_local_events_include_expected_turn(
        events=[
            SimpleNamespace(role="assistant", content_text="older reply", tool_name=None),
            SimpleNamespace(role="user", content_text=prompt, tool_name=None),
        ],
        expected_user_message=prompt,
    )

    assert not session_chat._managed_local_events_include_expected_turn(
        events=[
            SimpleNamespace(role="user", content_text=prompt, tool_name=None),
            SimpleNamespace(role="system", content_text="snapshot", tool_name=None),
        ],
        expected_user_message=prompt,
    )


def test_managed_local_events_include_expected_turn_accepts_native_claude_channel_wrapper():
    prompt = "continue"

    assert session_chat._managed_local_events_include_expected_turn(
        events=[
            SimpleNamespace(
                role="user",
                content_text=(
                    "<channel source=\"longhouse-channel\" injected_by=\"longhouse\">\n"
                    "continue\n"
                    "</channel>"
                ),
                tool_name=None,
            ),
            SimpleNamespace(role="assistant", content_text="done", tool_name=None),
        ],
        expected_user_message=prompt,
    )


# ---------------------------------------------------------------------------
# JSON dispatch tests (managed-local chat returns fast ack, not SSE stream)
# ---------------------------------------------------------------------------


def test_managed_local_claude_dispatch_returns_json_ack(monkeypatch, tmp_path):
    """Managed-local Claude chat returns JSON {accepted: true} instead of SSE stream."""
    session_local = _make_db(tmp_path)
    calls: list[dict[str, object]] = []
    lock_release_calls: list[dict[str, object]] = []

    with session_local() as db:
        user, runner = _seed_user_and_runner(db)
        source_session = _seed_managed_local_session(db, runner=runner, provider="claude")
        client, api_app_ref = _make_client(db, user)

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
            calls.append({
                "owner_id": owner_id,
                "session_id": str(session.id),
                "runner_id": session.source_runner_id,
                "text": text,
                "verify_turn_started": verify_turn_started,
                "verification_timeout_secs": verification_timeout_secs,
            })
            return SimpleNamespace(ok=True, exit_code=0, error=None)

        def fake_schedule_lock_release(**kwargs):
            lock_release_calls.append(kwargs)

        monkeypatch.setattr("zerg.services.live_session_dispatch.send_text_to_live_session", fake_send_text)
        monkeypatch.setattr("zerg.routers.session_chat._schedule_managed_local_lock_release", fake_schedule_lock_release)

        try:
            response = client.post(
                f"/api/sessions/{source_session.id}/send-live",
                json={"message": "continue"},
            )
            assert response.status_code == 200, response.text
            data = response.json()
            assert data["accepted"] is True
            assert data["session_id"] == str(source_session.id)
            assert "request_id" in data
            assert "dispatch_ms" in data

            # Verify turn was created in the ledger
            turn_rows = (
                db.query(ManagedLocalTurn)
                .filter(ManagedLocalTurn.session_id == source_session.id)
                .all()
            )
            assert len(turn_rows) == 1
            assert turn_rows[0].send_accepted_at is not None

            # Verify send was called with correct params
            assert len(calls) == 1
            assert calls[0]["runner_id"] == runner.id
            assert calls[0]["owner_id"] == user.id
            assert calls[0]["text"] == "continue"
            assert calls[0]["verify_turn_started"] is True
            assert calls[0]["verification_timeout_secs"] == 15.0
            assert len(lock_release_calls) == 1
            assert lock_release_calls[0]["lock_scope_id"] == str(source_session.id)
        finally:
            asyncio.run(session_lock_manager.release(str(source_session.id)))
            api_app_ref.dependency_overrides = {}


def test_managed_local_codex_dispatch_returns_json_ack(monkeypatch, tmp_path):
    """Managed-local Codex chat also returns JSON ack."""
    session_local = _make_db(tmp_path)

    with session_local() as db:
        user, runner = _seed_user_and_runner(db)
        source_session = _seed_managed_local_session(db, runner=runner, provider="codex")
        client, api_app_ref = _make_client(db, user)

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
            return SimpleNamespace(ok=True, exit_code=0, error=None)

        monkeypatch.setattr("zerg.services.live_session_dispatch.send_text_to_live_session", fake_send_text)
        monkeypatch.setattr("zerg.routers.session_chat._schedule_managed_local_lock_release", lambda **_kwargs: None)

        try:
            response = client.post(
                f"/api/sessions/{source_session.id}/send-live",
                json={"message": "what about germany"},
            )
            assert response.status_code == 200
            data = response.json()
            assert data["accepted"] is True
            assert data["session_id"] == str(source_session.id)
        finally:
            asyncio.run(session_lock_manager.release(str(source_session.id)))
            api_app_ref.dependency_overrides = {}


def test_managed_local_dispatch_send_failure_returns_502(monkeypatch, tmp_path):
    """When live-session dispatch fails, returns {accepted: false} with 502."""
    session_local = _make_db(tmp_path)

    with session_local() as db:
        user, runner = _seed_user_and_runner(db)
        source_session = _seed_managed_local_session(db, runner=runner, provider="claude")
        client, api_app_ref = _make_client(db, user)

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
            return SimpleNamespace(ok=False, exit_code=None, error="Runner send failed")

        monkeypatch.setattr("zerg.services.live_session_dispatch.send_text_to_live_session", fake_send_text)

        try:
            response = client.post(
                f"/api/sessions/{source_session.id}/send-live",
                json={"message": "continue"},
            )
            assert response.status_code == 502
            data = response.json()
            assert data["accepted"] is False
            assert "Runner send failed" in data["error"]

            # Verify turn was marked as failed in the ledger
            turn_rows = (
                db.query(ManagedLocalTurn)
                .filter(ManagedLocalTurn.session_id == source_session.id)
                .all()
            )
            assert len(turn_rows) == 1
            assert turn_rows[0].error_code == "send_failed"
        finally:
            api_app_ref.dependency_overrides = {}


def test_managed_local_dispatch_send_failure_releases_lock_for_retry(monkeypatch, tmp_path):
    """Failed dispatches should release the lock so the next send can retry immediately."""
    session_local = _make_db(tmp_path)
    send_calls = 0

    with session_local() as db:
        user, runner = _seed_user_and_runner(db)
        source_session = _seed_managed_local_session(db, runner=runner, provider="claude")
        client, api_app_ref = _make_client(db, user)

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
            nonlocal send_calls
            send_calls += 1
            return SimpleNamespace(ok=False, exit_code=None, error="Runner send failed")

        monkeypatch.setattr("zerg.services.live_session_dispatch.send_text_to_live_session", fake_send_text)

        try:
            first = client.post(
                f"/api/sessions/{source_session.id}/send-live",
                json={"message": "continue"},
            )
            assert first.status_code == 502

            second = client.post(
                f"/api/sessions/{source_session.id}/send-live",
                json={"message": "retry"},
            )
            assert second.status_code == 502
            assert send_calls == 2

            turn_rows = (
                db.query(ManagedLocalTurn)
                .filter(ManagedLocalTurn.session_id == source_session.id)
                .order_by(ManagedLocalTurn.id.asc())
                .all()
            )
            assert len(turn_rows) == 2
            assert [row.error_code for row in turn_rows] == ["send_failed", "send_failed"]
        finally:
            api_app_ref.dependency_overrides = {}


def test_managed_local_dispatch_does_not_create_cloud_continuation(monkeypatch, tmp_path):
    """Managed-local chat must not create cloud branch sessions."""
    session_local = _make_db(tmp_path)

    with session_local() as db:
        user, runner = _seed_user_and_runner(db)
        source_session = _seed_managed_local_session(db, runner=runner, provider="claude")
        client, api_app_ref = _make_client(db, user)

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
            return SimpleNamespace(ok=True, exit_code=0, error=None)

        def fail_cloud_target(*_args, **_kwargs):
            raise AssertionError("managed_local should not create cloud branches")

        monkeypatch.setattr("zerg.services.live_session_dispatch.send_text_to_live_session", fake_send_text)
        monkeypatch.setattr("zerg.routers.session_chat._schedule_managed_local_lock_release", lambda **_kwargs: None)
        monkeypatch.setattr(
            session_chat.AgentsStore,
            "ensure_cloud_continuation_target",
            fail_cloud_target,
        )

        try:
            response = client.post(
                f"/api/sessions/{source_session.id}/send-live",
                json={"message": "continue"},
            )
            # If this passes, cloud branching was never attempted
            assert response.status_code == 200
            assert response.json()["accepted"] is True
        finally:
            asyncio.run(session_lock_manager.release(str(source_session.id)))
            api_app_ref.dependency_overrides = {}


def test_managed_local_dispatch_keeps_lock_until_terminal(monkeypatch, tmp_path):
    """Successful managed-local dispatch should keep the thread lock until terminal state."""
    session_local = _make_db(tmp_path)

    with session_local() as db:
        user, runner = _seed_user_and_runner(db)
        source_session = _seed_managed_local_session(db, runner=runner, provider="claude")
        client, api_app_ref = _make_client(db, user)

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
            return SimpleNamespace(ok=True, exit_code=0, error=None)

        monkeypatch.setattr("zerg.services.live_session_dispatch.send_text_to_live_session", fake_send_text)
        monkeypatch.setattr("zerg.routers.session_chat._schedule_managed_local_lock_release", lambda **_kwargs: None)

        try:
            first = client.post(
                f"/api/sessions/{source_session.id}/send-live",
                json={"message": "continue"},
            )
            assert first.status_code == 200

            second = client.post(
                f"/api/sessions/{source_session.id}/send-live",
                json={"message": "continue again"},
            )
            assert second.status_code == 409
            assert second.json()["detail"]["code"] == "SESSION_LOCKED"
        finally:
            asyncio.run(session_lock_manager.release(str(source_session.id)))
            api_app_ref.dependency_overrides = {}


def test_managed_local_dispatch_updates_lock_endpoint_until_terminal(monkeypatch, tmp_path):
    """Successful dispatch should surface the held lock via the lock-status endpoint."""
    session_local = _make_db(tmp_path)

    with session_local() as db:
        user, runner = _seed_user_and_runner(db)
        source_session = _seed_managed_local_session(db, runner=runner, provider="claude")
        client, api_app_ref = _make_client(db, user)

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
            return SimpleNamespace(ok=True, exit_code=0, error=None)

        monkeypatch.setattr("zerg.services.live_session_dispatch.send_text_to_live_session", fake_send_text)
        monkeypatch.setattr("zerg.routers.session_chat._schedule_managed_local_lock_release", lambda **_kwargs: None)

        try:
            response = client.post(
                f"/api/sessions/{source_session.id}/send-live",
                json={"message": "continue"},
            )
            assert response.status_code == 200

            lock_response = client.get(f"/api/sessions/{source_session.id}/lock")
            assert lock_response.status_code == 200
            assert lock_response.json()["locked"] is True
            assert lock_response.json()["fork_available"] is True
        finally:
            asyncio.run(session_lock_manager.release(str(source_session.id)))
            api_app_ref.dependency_overrides = {}
