from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

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
from zerg.models.agents import SessionRuntimeState
from zerg.models.enums import UserRole
from zerg.models.models import Runner
from zerg.models.user import User
from zerg.routers import session_chat


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


def test_await_managed_local_turn_events_retries_on_pool_timeout(monkeypatch):
    session_id = uuid4()
    calls = {"latest": 0}
    expected_events = [
        SimpleNamespace(
            id=11,
            role="user",
            content_text="continue",
            tool_name=None,
        ),
        SimpleNamespace(
            id=12,
            role="assistant",
            content_text="done",
            tool_name=None,
        ),
    ]

    def fake_latest_event_id(**_kwargs):
        calls["latest"] += 1
        if calls["latest"] == 1:
            raise SQLAlchemyTimeoutError("QueuePool busy")
        return 12

    monkeypatch.setattr(
        "zerg.routers.session_chat._get_managed_local_latest_event_id",
        fake_latest_event_id,
    )
    monkeypatch.setattr(
        "zerg.routers.session_chat._fetch_managed_local_events_since",
        lambda **_kwargs: expected_events,
    )

    events = asyncio.run(
        session_chat._await_managed_local_turn_events(
            db_bind=object(),
            session_id=session_id,
            after_event_id=10,
            expected_user_message="continue",
            timeout_secs=0.5,
            poll_interval_secs=0.0,
        )
    )

    assert events == expected_events
    assert calls["latest"] >= 3


def test_chat_with_session_routes_claude_managed_local_without_cloud_continuation(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    calls: list[dict[str, object]] = []

    with session_local() as db:
        user, runner = _seed_user_and_runner(db)
        source_session = _seed_managed_local_session(db, runner=runner, provider="claude")
        client, api_app_ref = _make_client(db, user)

        async def fake_wait_for_events(**_kwargs):
            user_event = session_chat.AgentEvent(
                session_id=source_session.id,
                role="user",
                content_text="continue",
                timestamp=datetime.now(timezone.utc),
            )
            assistant_event = session_chat.AgentEvent(
                session_id=source_session.id,
                role="assistant",
                content_text="Local tmux reply",
                tool_name=None,
                tool_call_id=None,
                timestamp=datetime.now(timezone.utc),
            )
            db.add_all([user_event, assistant_event])
            db.commit()
            return [user_event, assistant_event]

        def fail_cloud_target(*_args, **_kwargs):
            raise AssertionError("managed_local chat should not create cloud continuations")

        async def fake_send_text(*, db, owner_id, session, text, commis_id=None, timeout_secs=15):
            calls.append(
                {
                    "owner_id": owner_id,
                    "session_id": str(session.id),
                    "runner_id": session.source_runner_id,
                    "text": text,
                    "commis_id": commis_id,
                    "timeout_secs": timeout_secs,
                }
            )
            return SimpleNamespace(ok=True, exit_code=0, error=None)

        async def fake_wait_for_terminal(**_kwargs):
            return SimpleNamespace(
                phase="idle",
                control_status="completed",
                runtime_event_id=44,
                occurred_at=datetime.now(timezone.utc),
            )

        monkeypatch.setattr("zerg.routers.session_chat.send_text_to_managed_local_session", fake_send_text)
        monkeypatch.setattr(
            "zerg.routers.session_chat._await_managed_local_turn_events",
            fake_wait_for_events,
        )
        monkeypatch.setattr("zerg.routers.session_chat.await_managed_local_turn_terminal", fake_wait_for_terminal)
        monkeypatch.setattr(
            session_chat.AgentsStore,
            "ensure_cloud_continuation_target",
            fail_cloud_target,
        )

        try:
            response = client.post(
                f"/api/sessions/{source_session.id}/chat",
                json={"message": "continue"},
            )
            assert response.status_code == 200, response.text
            body = response.text
            assert "event: system" in body
            assert "event: assistant_delta" in body
            assert "event: done" in body
            assert "Local tmux reply" in body
            assert '"persisted_events": 2' in body
            assert '"persistence_error": null' in body
            assert '"sync_status": "complete"' in body
            assert '"control_status": "completed"' in body
            assert '"created_continuation": false' in body
            assert f'"session_id": "{source_session.id}"' in body
            assert f'"shipped_session_id": "{source_session.id}"' in body
            runtime_state_rows = (
                db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == source_session.id).all()
            )
            assert runtime_state_rows == []
            turn_rows = (
                db.query(ManagedLocalTurn)
                .filter(ManagedLocalTurn.session_id == source_session.id)
                .order_by(ManagedLocalTurn.id.asc())
                .all()
            )
            assert len(turn_rows) == 1
            assert turn_rows[0].request_id
            assert turn_rows[0].send_accepted_at is not None
            assert turn_rows[0].terminal_phase == "idle"
            assert turn_rows[0].terminal_runtime_event_id == 44
            assert turn_rows[0].durable_at is not None
            assert turn_rows[0].durable_user_event_id is not None
            assert turn_rows[0].durable_assistant_event_id is not None
            assert len(calls) == 1
            assert calls[0]["runner_id"] == runner.id
            assert calls[0]["owner_id"] == user.id
            assert calls[0]["session_id"] == str(source_session.id)
            assert calls[0]["text"] == "continue"
        finally:
            api_app_ref.dependency_overrides = {}


def test_chat_with_session_reports_claude_managed_local_send_failure(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        user, runner = _seed_user_and_runner(db)
        source_session = _seed_managed_local_session(db, runner=runner, provider="claude")
        client, api_app_ref = _make_client(db, user)

        async def fake_send_text(*, db, owner_id, session, text, commis_id=None, timeout_secs=15):
            return SimpleNamespace(ok=False, exit_code=None, error="Runner send failed")

        monkeypatch.setattr("zerg.routers.session_chat.send_text_to_managed_local_session", fake_send_text)

        try:
            response = client.post(
                f"/api/sessions/{source_session.id}/chat",
                json={"message": "continue"},
            )
            assert response.status_code == 200, response.text
            body = response.text
            assert "event: error" in body
            assert "event: done" in body
            assert "Runner send failed" in body
            assert '"persisted_events": 0' in body
            assert '"created_continuation": false' in body
            turn_rows = db.query(ManagedLocalTurn).filter(ManagedLocalTurn.session_id == source_session.id).all()
            assert len(turn_rows) == 1
            assert turn_rows[0].send_accepted_at is None
            assert turn_rows[0].error_code == "send_failed"
        finally:
            api_app_ref.dependency_overrides = {}


def test_chat_with_session_reports_claude_managed_local_persistence_timeout(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        user, runner = _seed_user_and_runner(db)
        source_session = _seed_managed_local_session(db, runner=runner, provider="claude")
        client, api_app_ref = _make_client(db, user)

        async def fake_send_text(*, db, owner_id, session, text, commis_id=None, timeout_secs=15):
            return SimpleNamespace(ok=True, exit_code=0, error=None)

        async def fake_wait_for_events(**_kwargs):
            return []

        async def fake_wait_for_terminal(**_kwargs):
            return None

        monkeypatch.setattr("zerg.routers.session_chat.send_text_to_managed_local_session", fake_send_text)
        monkeypatch.setattr(
            "zerg.routers.session_chat._await_managed_local_turn_events",
            fake_wait_for_events,
        )
        monkeypatch.setattr("zerg.routers.session_chat.await_managed_local_turn_terminal", fake_wait_for_terminal)

        try:
            response = client.post(
                f"/api/sessions/{source_session.id}/chat",
                json={"message": "continue"},
            )
            assert response.status_code == 200, response.text
            body = response.text
            assert "event: error" in body
            assert "event: done" in body
            assert session_chat._MANAGED_LOCAL_TURN_TIMEOUT_MESSAGE in body
            assert '"persisted_events": 0' in body
            assert '"created_continuation": false' in body
            assert '"sync_status": "failed"' in body
            assert '"control_status": "failed"' in body
            assert (
                '"persistence_error": "Message was sent to the managed local session, but Longhouse did not '
                'observe a completed turn yet."'
            ) in body
            assert '"exit_code": 0' in body
        finally:
            api_app_ref.dependency_overrides = {}


def test_chat_with_session_returns_sync_pending_after_terminal_control_success(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    wait_calls = {"count": 0}
    ship_calls = {"count": 0}

    with session_local() as db:
        user, runner = _seed_user_and_runner(db)
        source_session = _seed_managed_local_session(db, runner=runner, provider="claude")
        client, api_app_ref = _make_client(db, user)

        async def fake_send_text(*, db, owner_id, session, text, commis_id=None, timeout_secs=15):
            return SimpleNamespace(ok=True, exit_code=0, error=None)

        async def fake_wait_for_terminal(**_kwargs):
            return SimpleNamespace(
                phase="needs_user",
                control_status="needs_user",
                runtime_event_id=77,
                occurred_at=datetime.now(timezone.utc),
            )

        async def fake_wait_for_events(**_kwargs):
            wait_calls["count"] += 1
            return []

        async def fake_ship(*, db, owner_id, session, commis_id=None, timeout_secs=20):
            ship_calls["count"] += 1
            return SimpleNamespace(ok=False, exit_code=13, error="no new transcript events")

        monkeypatch.setattr("zerg.routers.session_chat.send_text_to_managed_local_session", fake_send_text)
        monkeypatch.setattr("zerg.routers.session_chat.await_managed_local_turn_terminal", fake_wait_for_terminal)
        monkeypatch.setattr(
            "zerg.routers.session_chat._await_managed_local_turn_events",
            fake_wait_for_events,
        )
        monkeypatch.setattr("zerg.routers.session_chat.ship_managed_local_claude_transcript", fake_ship)
        monkeypatch.setattr(session_chat, "MANAGED_LOCAL_PRE_FORCE_SYNC_GRACE_SECS", 0.01)
        monkeypatch.setattr(session_chat, "MANAGED_LOCAL_POST_TERMINAL_SYNC_GRACE_SECS", 0.01)
        monkeypatch.setattr(session_chat, "MANAGED_LOCAL_POST_FORCE_SYNC_GRACE_SECS", 0.01)

        try:
            response = client.post(
                f"/api/sessions/{source_session.id}/chat",
                json={"message": "continue"},
            )
            assert response.status_code == 200, response.text
            body = response.text
            assert "event: error" not in body
            assert "event: done" in body
            assert '"persisted_events": 0' in body
            assert '"sync_status": "pending"' in body
            assert '"control_status": "needs_user"' in body
            assert f'"shipped_session_id": "{source_session.id}"' in body
            runtime_state_rows = (
                db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == source_session.id).all()
            )
            assert runtime_state_rows == []
            assert wait_calls["count"] >= 1
            assert ship_calls["count"] == 1
        finally:
            api_app_ref.dependency_overrides = {}


def test_chat_with_session_uses_direct_ship_to_upgrade_pending_claude_turn(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    events_ready = asyncio.Event()
    ship_calls: list[dict[str, object]] = []

    with session_local() as db:
        user, runner = _seed_user_and_runner(db)
        source_session = _seed_managed_local_session(db, runner=runner, provider="claude")
        client, api_app_ref = _make_client(db, user)

        async def fake_send_text(*, db, owner_id, session, text, commis_id=None, timeout_secs=15):
            return SimpleNamespace(ok=True, exit_code=0, error=None)

        async def fake_wait_for_terminal(**_kwargs):
            return SimpleNamespace(
                phase="idle",
                control_status="completed",
                runtime_event_id=88,
                occurred_at=datetime.now(timezone.utc),
            )

        async def fake_wait_for_events(**_kwargs):
            assert _kwargs.get("expected_user_message") == "continue"
            await events_ready.wait()
            return [
                SimpleNamespace(
                    id=101,
                    role="user",
                    content_text="continue",
                    tool_name=None,
                    tool_call_id=None,
                ),
                SimpleNamespace(
                    id=102,
                    role="assistant",
                    content_text="Local tmux reply",
                    tool_name=None,
                    tool_call_id=None,
                ),
            ]

        async def fake_ship(*, db, owner_id, session, commis_id=None, timeout_secs=20):
            ship_calls.append(
                {
                    "owner_id": owner_id,
                    "session_id": str(session.id),
                    "commis_id": commis_id,
                    "timeout_secs": timeout_secs,
                }
            )
            events_ready.set()
            return SimpleNamespace(ok=True, exit_code=0, error=None)

        monkeypatch.setattr("zerg.routers.session_chat.send_text_to_managed_local_session", fake_send_text)
        monkeypatch.setattr("zerg.routers.session_chat.await_managed_local_turn_terminal", fake_wait_for_terminal)
        monkeypatch.setattr(
            "zerg.routers.session_chat._await_managed_local_turn_events",
            fake_wait_for_events,
        )
        monkeypatch.setattr("zerg.routers.session_chat.ship_managed_local_claude_transcript", fake_ship)
        monkeypatch.setattr(session_chat, "MANAGED_LOCAL_PRE_FORCE_SYNC_GRACE_SECS", 0.01)
        monkeypatch.setattr(session_chat, "MANAGED_LOCAL_POST_TERMINAL_SYNC_GRACE_SECS", 0.01)
        monkeypatch.setattr(session_chat, "MANAGED_LOCAL_POST_FORCE_SYNC_GRACE_SECS", 0.05)

        try:
            response = client.post(
                f"/api/sessions/{source_session.id}/chat",
                json={"message": "continue"},
            )
            assert response.status_code == 200, response.text
            body = response.text
            assert "Local tmux reply" in body
            assert '"sync_status": "complete"' in body
            assert '"control_status": "completed"' in body
            assert '"persisted_events": 2' in body
            assert len(ship_calls) == 1
            assert ship_calls[0]["owner_id"] == user.id
            assert ship_calls[0]["session_id"] == str(source_session.id)
            assert ship_calls[0]["commis_id"]
            assert ship_calls[0]["timeout_secs"] == 20
        finally:
            api_app_ref.dependency_overrides = {}


def test_chat_with_session_keeps_direct_ship_running_after_pending_response(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    detached = {"count": 0}

    with session_local() as db:
        user, runner = _seed_user_and_runner(db)
        source_session = _seed_managed_local_session(db, runner=runner, provider="claude")
        client, api_app_ref = _make_client(db, user)

        async def fake_send_text(*, db, owner_id, session, text, commis_id=None, timeout_secs=15):
            return SimpleNamespace(ok=True, exit_code=0, error=None)

        async def fake_wait_for_terminal(**_kwargs):
            return SimpleNamespace(
                phase="idle",
                control_status="completed",
                runtime_event_id=88,
                occurred_at=datetime.now(timezone.utc),
            )

        async def fake_wait_for_events(**_kwargs):
            return []

        async def fake_ship(*, db, owner_id, session, commis_id=None, timeout_secs=20):
            await asyncio.sleep(0.05)
            return SimpleNamespace(ok=True, exit_code=0, error=None)

        def fake_detach(task, *, session_id):
            detached["count"] += 1
            assert session_id == source_session.id
            task.cancel()

        monkeypatch.setattr("zerg.routers.session_chat.send_text_to_managed_local_session", fake_send_text)
        monkeypatch.setattr("zerg.routers.session_chat.await_managed_local_turn_terminal", fake_wait_for_terminal)
        monkeypatch.setattr(
            "zerg.routers.session_chat._await_managed_local_turn_events",
            fake_wait_for_events,
        )
        monkeypatch.setattr("zerg.routers.session_chat.ship_managed_local_claude_transcript", fake_ship)
        monkeypatch.setattr("zerg.routers.session_chat._detach_managed_local_ship_task", fake_detach)
        monkeypatch.setattr(session_chat, "MANAGED_LOCAL_PRE_FORCE_SYNC_GRACE_SECS", 0.01)
        monkeypatch.setattr(session_chat, "MANAGED_LOCAL_POST_TERMINAL_SYNC_GRACE_SECS", 0.01)

        try:
            response = client.post(
                f"/api/sessions/{source_session.id}/chat",
                json={"message": "continue"},
            )
            assert response.status_code == 200, response.text
            body = response.text
            assert '"sync_status": "pending"' in body
            assert detached["count"] == 1
        finally:
            api_app_ref.dependency_overrides = {}


def test_chat_with_session_lets_natural_events_win_before_forcing_claude_sync(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    ship_calls = {"count": 0}

    with session_local() as db:
        user, runner = _seed_user_and_runner(db)
        source_session = _seed_managed_local_session(db, runner=runner, provider="claude")
        client, api_app_ref = _make_client(db, user)

        async def fake_send_text(*, db, owner_id, session, text, commis_id=None, timeout_secs=15):
            return SimpleNamespace(ok=True, exit_code=0, error=None)

        async def fake_wait_for_terminal(**_kwargs):
            return SimpleNamespace(
                phase="idle",
                control_status="completed",
                runtime_event_id=89,
                occurred_at=datetime.now(timezone.utc),
            )

        async def fake_wait_for_events(**_kwargs):
            await asyncio.sleep(0.01)
            return [
                SimpleNamespace(
                    id=101,
                    role="user",
                    content_text="continue",
                    tool_name=None,
                    tool_call_id=None,
                ),
                SimpleNamespace(
                    id=102,
                    role="assistant",
                    content_text="Natural tmux reply",
                    tool_name=None,
                    tool_call_id=None,
                ),
            ]

        async def fake_ship(*, db, owner_id, session, commis_id=None, timeout_secs=20):
            ship_calls["count"] += 1
            return SimpleNamespace(ok=True, exit_code=0, error=None)

        monkeypatch.setattr("zerg.routers.session_chat.send_text_to_managed_local_session", fake_send_text)
        monkeypatch.setattr("zerg.routers.session_chat.await_managed_local_turn_terminal", fake_wait_for_terminal)
        monkeypatch.setattr(
            "zerg.routers.session_chat._await_managed_local_turn_events",
            fake_wait_for_events,
        )
        monkeypatch.setattr("zerg.routers.session_chat.ship_managed_local_claude_transcript", fake_ship)
        monkeypatch.setattr(session_chat, "MANAGED_LOCAL_PRE_FORCE_SYNC_GRACE_SECS", 0.05)
        monkeypatch.setattr(session_chat, "MANAGED_LOCAL_POST_TERMINAL_SYNC_GRACE_SECS", 0.05)

        try:
            response = client.post(
                f"/api/sessions/{source_session.id}/chat",
                json={"message": "continue"},
            )
            assert response.status_code == 200, response.text
            body = response.text
            assert "Natural tmux reply" in body
            assert '"sync_status": "complete"' in body
            assert '"persisted_events": 2' in body
            assert ship_calls["count"] == 0
        finally:
            api_app_ref.dependency_overrides = {}


def test_chat_with_session_prefers_natural_events_even_after_force_sync_starts(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    ship_calls = {"count": 0}
    ship_finished = {"value": False}
    ship_cancelled = {"value": False}

    with session_local() as db:
        user, runner = _seed_user_and_runner(db)
        source_session = _seed_managed_local_session(db, runner=runner, provider="claude")
        client, api_app_ref = _make_client(db, user)

        async def fake_send_text(*, db, owner_id, session, text, commis_id=None, timeout_secs=15):
            return SimpleNamespace(ok=True, exit_code=0, error=None)

        async def fake_wait_for_terminal(**_kwargs):
            return SimpleNamespace(
                phase="idle",
                control_status="completed",
                runtime_event_id=90,
                occurred_at=datetime.now(timezone.utc),
            )

        async def fake_wait_for_events(**_kwargs):
            await asyncio.sleep(0.05)
            return [
                SimpleNamespace(
                    id=101,
                    role="user",
                    content_text="continue",
                    tool_name=None,
                    tool_call_id=None,
                ),
                SimpleNamespace(
                    id=102,
                    role="assistant",
                    content_text="Natural tmux reply after force sync start",
                    tool_name=None,
                    tool_call_id=None,
                ),
            ]

        async def fake_ship(*, db, owner_id, session, commis_id=None, timeout_secs=20):
            ship_calls["count"] += 1
            try:
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                ship_cancelled["value"] = True
                raise
            ship_finished["value"] = True
            return SimpleNamespace(ok=True, exit_code=0, error=None)

        monkeypatch.setattr("zerg.routers.session_chat.send_text_to_managed_local_session", fake_send_text)
        monkeypatch.setattr("zerg.routers.session_chat.await_managed_local_turn_terminal", fake_wait_for_terminal)
        monkeypatch.setattr(
            "zerg.routers.session_chat._await_managed_local_turn_events",
            fake_wait_for_events,
        )
        monkeypatch.setattr("zerg.routers.session_chat.ship_managed_local_claude_transcript", fake_ship)
        monkeypatch.setattr(session_chat, "MANAGED_LOCAL_PRE_FORCE_SYNC_GRACE_SECS", 0.01)
        monkeypatch.setattr(session_chat, "MANAGED_LOCAL_POST_TERMINAL_SYNC_GRACE_SECS", 0.1)

        try:
            started_at = time.monotonic()
            response = client.post(
                f"/api/sessions/{source_session.id}/chat",
                json={"message": "continue"},
            )
            elapsed = time.monotonic() - started_at
            assert response.status_code == 200, response.text
            body = response.text
            assert "Natural tmux reply after force sync start" in body
            assert '"sync_status": "complete"' in body
            assert '"persisted_events": 2' in body
            assert ship_calls["count"] == 1
            assert not ship_finished["value"]
            assert ship_cancelled["value"] is True
            assert elapsed < 0.4
        finally:
            api_app_ref.dependency_overrides = {}


def test_chat_with_session_waits_for_codex_events_after_terminal_success(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        user, runner = _seed_user_and_runner(db)
        source_session = _seed_managed_local_session(db, runner=runner, provider="codex")
        client, api_app_ref = _make_client(db, user)

        async def fake_send_text(*, db, owner_id, session, text, commis_id=None, timeout_secs=15):
            return SimpleNamespace(ok=True, exit_code=0, error=None)

        async def fake_wait_for_terminal(**_kwargs):
            return SimpleNamespace(
                phase="idle",
                control_status="completed",
                runtime_event_id=91,
                occurred_at=datetime.now(timezone.utc),
            )

        async def fake_wait_for_events(**_kwargs):
            assert _kwargs.get("expected_user_message") == "continue"
            await asyncio.sleep(0.01)
            return [
                SimpleNamespace(
                    id=101,
                    role="user",
                    content_text="continue",
                    tool_name=None,
                    tool_call_id=None,
                ),
                SimpleNamespace(
                    id=102,
                    role="assistant",
                    content_text="Codex reply after terminal success",
                    tool_name=None,
                    tool_call_id=None,
                ),
            ]

        monkeypatch.setattr("zerg.routers.session_chat.send_text_to_managed_local_session", fake_send_text)
        monkeypatch.setattr("zerg.routers.session_chat.await_managed_local_turn_terminal", fake_wait_for_terminal)
        monkeypatch.setattr(
            "zerg.routers.session_chat._await_managed_local_turn_events",
            fake_wait_for_events,
        )
        monkeypatch.setattr(session_chat, "MANAGED_LOCAL_POST_TERMINAL_SYNC_GRACE_SECS", 0.05)

        try:
            response = client.post(
                f"/api/sessions/{source_session.id}/chat",
                json={"message": "continue"},
            )
            assert response.status_code == 200, response.text
            body = response.text
            assert "Codex reply after terminal success" in body
            assert '"sync_status": "complete"' in body
            assert '"persisted_events": 2' in body
            assert '"control_status": "completed"' in body
        finally:
            api_app_ref.dependency_overrides = {}


def test_chat_with_session_routes_codex_managed_local_without_cloud_continuation(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    calls: list[dict[str, object]] = []

    with session_local() as db:
        user, runner = _seed_user_and_runner(db)
        source_session = _seed_managed_local_session(db, runner=runner, provider="codex")
        client, api_app_ref = _make_client(db, user)

        async def fake_wait_for_events(**_kwargs):
            return [
                SimpleNamespace(
                    id=101,
                    role="assistant",
                    content_text="Local codex tmux reply",
                    tool_name=None,
                    tool_call_id=None,
                )
            ]

        def fail_cloud_target(*_args, **_kwargs):
            raise AssertionError("managed_local chat should not create cloud continuations")

        async def fake_send_text(*, db, owner_id, session, text, commis_id=None, timeout_secs=15):
            calls.append(
                {
                    "owner_id": owner_id,
                    "session_id": str(session.id),
                    "runner_id": session.source_runner_id,
                    "text": text,
                    "commis_id": commis_id,
                    "timeout_secs": timeout_secs,
                }
            )
            return SimpleNamespace(ok=True, exit_code=0, error=None)

        async def fake_wait_for_terminal(**_kwargs):
            return None

        monkeypatch.setattr("zerg.routers.session_chat.send_text_to_managed_local_session", fake_send_text)
        monkeypatch.setattr(
            "zerg.routers.session_chat._await_managed_local_turn_events",
            fake_wait_for_events,
        )
        monkeypatch.setattr("zerg.routers.session_chat.await_managed_local_turn_terminal", fake_wait_for_terminal)
        monkeypatch.setattr(
            session_chat.AgentsStore,
            "ensure_cloud_continuation_target",
            fail_cloud_target,
        )

        try:
            response = client.post(
                f"/api/sessions/{source_session.id}/chat",
                json={"message": "continue"},
            )
            assert response.status_code == 200, response.text
            body = response.text
            assert "Local codex tmux reply" in body
            assert '"sync_status": "complete"' in body
            assert '"control_status": "completed"' in body
            assert '"created_continuation": false' in body
            assert f'"session_id": "{source_session.id}"' in body
            assert f'"shipped_session_id": "{source_session.id}"' in body
            runtime_state_rows = (
                db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == source_session.id).all()
            )
            assert runtime_state_rows == []
            assert len(calls) == 1
            assert calls[0]["runner_id"] == runner.id
            assert calls[0]["owner_id"] == user.id
            assert calls[0]["session_id"] == str(source_session.id)
            assert calls[0]["text"] == "continue"
        finally:
            api_app_ref.dependency_overrides = {}
