"""Tests for POST /api/sessions/launch and the launch_remote_session service."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from datetime import timezone
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import zerg.services.remote_session_launch as remote_launch_module  # noqa: E402
from zerg.database import Base  # noqa: E402
from zerg.database import get_db  # noqa: E402
from zerg.database import initialize_live_database  # noqa: E402
from zerg.database import make_engine  # noqa: E402
from zerg.database import make_live_engine  # noqa: E402
from zerg.dependencies.agents_auth import require_single_tenant  # noqa: E402
from zerg.dependencies.agents_auth import verify_agents_token  # noqa: E402
from zerg.dependencies.browser_route_auth import get_current_browser_route_user  # noqa: E402
from zerg.models import User  # noqa: E402
from zerg.models.agents import AgentSession  # noqa: E402
from zerg.models.agents import AgentSourceLine  # noqa: E402
from zerg.models.agents import SessionConnection  # noqa: E402
from zerg.models.agents import SessionLaunchAttempt  # noqa: E402
from zerg.models.agents import SessionRun  # noqa: E402
from zerg.models.agents import SessionThreadAlias  # noqa: E402
from zerg.models.device_token import DeviceToken  # noqa: E402
from zerg.models.live_store import LiveArchiveOutbox  # noqa: E402
from zerg.models.live_store import LiveLaunchReadiness  # noqa: E402
from zerg.services.agents import AgentsStore  # noqa: E402
from zerg.services.agents.kernel_capabilities import project_session_capabilities  # noqa: E402
from zerg.services.agents.kernel_writes import ensure_primary_thread  # noqa: E402
from zerg.services.agents.kernel_writes import record_run  # noqa: E402
from zerg.services.agents.kernel_writes import record_thread_alias  # noqa: E402
from zerg.services.agents.kernel_writes import upsert_connection_for_run  # noqa: E402
from zerg.services.live_archive_outbox import drain_live_archive_outbox  # noqa: E402
from zerg.services.live_launch_readiness import upsert_live_launch_readiness  # noqa: E402
from zerg.services.live_session_dispatch import supports_live_text_dispatch_metadata  # noqa: E402
from zerg.services.machine_control_channel import MachineControlChannelRegistry  # noqa: E402
from zerg.services.machine_control_channel import MachineControlCommandResponse  # noqa: E402
from zerg.services.machine_control_channel import get_machine_control_channel_registry  # noqa: E402
from zerg.services.remote_session_launch import RemoteContinueParams  # noqa: E402
from zerg.services.remote_session_launch import RemoteLaunchError  # noqa: E402
from zerg.services.remote_session_launch import RemoteLaunchParams  # noqa: E402
from zerg.services.remote_session_launch import _project_for  # noqa: E402
from zerg.services.remote_session_launch import continue_remote_session  # noqa: E402
from zerg.services.remote_session_launch import launch_remote_session  # noqa: E402
from zerg.services.remote_session_launch import reap_orphaned_launches  # noqa: E402
from zerg.services.remote_session_launch import reconcile_launch_from_command_result  # noqa: E402
from zerg.services.session_kernel_projection import project_session_control_fields  # noqa: E402
from zerg.services.session_response_projection import build_session_response_list  # noqa: E402
from zerg.services.session_runtime import RuntimeEventIngest  # noqa: E402
from zerg.services.session_runtime import ingest_runtime_events  # noqa: E402
from zerg.services.session_workspace import build_session_mobile_tail  # noqa: E402
from zerg.services.session_workspace import build_session_workspace  # noqa: E402
from zerg.services.write_serializer import WriteSerializer  # noqa: E402

OWNER_ID = 77


def _latest_attempt(db, session_id):
    return (
        db.query(SessionLaunchAttempt)
        .filter(SessionLaunchAttempt.session_id == session_id)
        .order_by(SessionLaunchAttempt.created_at.desc(), SessionLaunchAttempt.id.desc())
        .one()
    )


def _project_control(db, session):
    capabilities = project_session_capabilities(db, session_id=session.id)
    return capabilities, project_session_control_fields(db, session, capabilities=capabilities)


def test_remote_launch_derived_project_ignores_generic_workspace():
    assert _project_for("/private/tmp/longhouse/workspace", None) == "managed-local"
    assert _project_for("/private/tmp/longhouse/workspace", "explicit") == "explicit"


def _make_db(tmp_path):
    db_path = tmp_path / "remote_launch.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _seed_user_and_device(SessionLocal, *, owner_id: int = OWNER_ID, device_id: str = "cinder"):
    with SessionLocal() as db:
        existing = db.query(User).filter(User.id == owner_id).first()
        if existing is None:
            db.add(User(id=owner_id, email=f"u{owner_id}@ex.com", role="ADMIN"))
            db.commit()
    with SessionLocal() as db:
        db.add(
            DeviceToken(
                owner_id=owner_id,
                device_id=device_id,
                token_hash=f"hash-{device_id}-{owner_id}",
            )
        )
        db.commit()


class _FakeWebSocket:
    async def send_json(self, message):  # pragma: no cover — tests short-circuit registry
        pass


def _register_online(
    registry: MachineControlChannelRegistry,
    *,
    owner_id: int,
    device_id: str,
    supports: tuple[str, ...] = ("codex.launch",),
):
    asyncio.run(
        registry.register(
            owner_id=owner_id,
            device_id=device_id,
            machine_name=device_id,
            engine_build="test",
            supports=list(supports),
            websocket=_FakeWebSocket(),
        )
    )


def _seed_continuable_codex_session(
    db,
    *,
    session_id=None,
    device_id: str | None = "cinder",
    provider_thread_id: str = "thread-abc",
    thread_path: str | None = "/Users/me/.codex/sessions/thread-abc.jsonl",
    session_path: str | None = None,
    ended: bool = True,
):
    now = datetime.now(timezone.utc)
    sid = session_id or uuid4()
    session = AgentSession(
        id=sid,
        provider="codex",
        environment="development",
        project="repo",
        device_id=device_id,
        device_name=device_id,
        cwd="/Users/me/repo",
        git_repo="git@example.test/repo.git",
        git_branch="main",
        started_at=now,
        ended_at=now if ended else None,
        last_activity_at=now,
                                        user_messages=1,
        assistant_messages=1,
        tool_calls=0,
                    )
    db.add(session)
    db.flush()
    thread = ensure_primary_thread(db, session)
    record_thread_alias(
        db,
        thread=thread,
        provider="codex",
        alias_kind="provider_session_id",
        alias_value=provider_thread_id,
    )
    if thread_path is not None:
        db.add(
            AgentSourceLine(
                session_id=session.id,
                thread_id=thread.id,
                source_path=thread_path,
                source_offset=0,
                branch_id=0,
                raw_json='{"type":"message"}',
                line_hash=f"hash-thread-{sid}",
            )
        )
    if session_path is not None:
        db.add(
            AgentSourceLine(
                session_id=session.id,
                thread_id=None,
                source_path=session_path,
                source_offset=1,
                branch_id=0,
                raw_json='{"type":"message"}',
                line_hash=f"hash-session-{sid}",
            )
        )
    db.commit()
    return session.id


def _seed_continuable_claude_session(
    db,
    *,
    session_id=None,
    provider_thread_id: str | None = None,
    device_id: str | None = "cinder",
    ended: bool = True,
):
    """Seed a closed managed claude session.

    The provider session id is distinct from the Longhouse id and there is NO
    transcript source_path alias — the resume target is the provider id alone.
    """

    now = datetime.now(timezone.utc)
    sid = session_id or uuid4()
    session = AgentSession(
        id=sid,
        provider="claude",
        environment="development",
        project="repo",
        device_id=device_id,
        device_name=device_id,
        cwd="/Users/me/repo",
        git_repo="git@example.test/repo.git",
        git_branch="main",
        started_at=now,
        ended_at=now if ended else None,
        last_activity_at=now,
                                        user_messages=1,
        assistant_messages=1,
        tool_calls=0,
                    )
    db.add(session)
    db.flush()
    thread = ensure_primary_thread(db, session)
    provider_thread_id = provider_thread_id or f"claude-provider-{sid}"
    record_thread_alias(
        db,
        thread=thread,
        provider="claude",
        alias_kind="provider_session_id",
        alias_value=provider_thread_id,
    )
    # ...and a control-acquisition connection — the sound managed fingerprint.
    # The session is closed, so the connection is released (as it would be after
    # the user exits the claude TUI).
    run = record_run(db, thread=thread, provider="claude", host_id=device_id or "cinder", cwd="/Users/me/repo")
    upsert_connection_for_run(
        db,
        run=run,
        control_plane="claude_channel_bridge",
        acquisition_kind="spawned_control",
        state="released",
        external_name=device_id or "cinder",
        can_send_input=0,
        can_interrupt=0,
        can_terminate=0,
        can_tail_output=0,
        can_resume=1,
    )
    db.commit()
    return session.id


def _seed_imported_claude_session(
    db,
    *,
    session_id=None,
    device_id: str | None = "cinder",
    provider_session_alias: str | None = None,
    observe_only_connection: bool = False,
    source_path: str | None = None,
    ended: bool = True,
):
    """Seed an imported/unmanaged bare-CLI claude session.

    Unmanaged claude was NOT launched with `claude --session-id <our-uuid>`; its
    provider session id is its OWN id (recorded as a provider_session_id alias).
    Such a session is continuable as ``adopt_unmanaged`` IFF it has both that
    alias AND a local transcript (source_path) — the user can explicitly adopt
    it. Missing either → not continuable.

    ``source_path`` seeds an AgentSourceLine so the transcript-evidence gate is
    satisfied. ``observe_only_connection`` simulates kernel backfill attaching an
    observe_only (NOT control) connection.
    """

    now = datetime.now(timezone.utc)
    sid = session_id or uuid4()
    session = AgentSession(
        id=sid,
        provider="claude",
        environment="development",
        project="repo",
        device_id=device_id,
        device_name=device_id,
        cwd="/Users/me/repo",
        started_at=now,
        ended_at=now if ended else None,
        last_activity_at=now,
                                        user_messages=1,
        assistant_messages=1,
        tool_calls=0,
                    )
    db.add(session)
    db.flush()
    thread = ensure_primary_thread(db, session)
    if provider_session_alias is not None:
        record_thread_alias(
            db,
            thread=thread,
            provider="claude",
            alias_kind="provider_session_id",
            alias_value=provider_session_alias,
        )
    if observe_only_connection:
        run = record_run(db, thread=thread, provider="claude", host_id=device_id or "cinder", cwd="/Users/me/repo")
        upsert_connection_for_run(
            db,
            run=run,
            control_plane="claude_channel_bridge",
            acquisition_kind="observe_only",
            state="released",
            external_name=device_id or "cinder",
            can_send_input=0,
            can_interrupt=0,
            can_terminate=0,
            can_tail_output=1,
            can_resume=0,
        )
    if source_path is not None:
        db.add(
            AgentSourceLine(
                session_id=session.id,
                thread_id=thread.id,
                source_path=source_path,
                source_offset=0,
                branch_id=0,
                raw_json='{"type":"message"}',
                line_hash=f"hash-imported-{sid}",
            )
        )
    db.commit()
    return session.id


class _StubRegistry(MachineControlChannelRegistry):
    """Registry with scripted ``send_command`` responses per session_id."""

    def __init__(self):
        super().__init__()
        self._scripted: dict[str, MachineControlCommandResponse] = {}
        self.sent: list[dict] = []

    def script(self, session_id: str, response: MachineControlCommandResponse):
        self._scripted[session_id] = response

    async def send_command(self, **kwargs):  # type: ignore[override]
        self.sent.append(kwargs)
        session_id = kwargs.get("session_id", "")
        if session_id in self._scripted:
            return self._scripted[session_id]
        # Default: transport ok, ok=True
        return MachineControlCommandResponse(
            transport_ok=True,
            message={"type": "command_result", "ok": True, "result": {"session_id": session_id}},
        )

    async def send_command_nowait(self, **kwargs):  # type: ignore[override]
        self.sent.append(kwargs)
        session_id = kwargs.get("session_id", "")
        if session_id in self._scripted:
            return self._scripted[session_id]
        return MachineControlCommandResponse(
            transport_ok=True,
            message={
                "type": "command",
                "command_id": kwargs.get("command_id"),
                "command_type": kwargs.get("command_type"),
                "session_id": session_id,
                "payload": kwargs.get("payload") or {},
            },
        )


class _LiveReadinessSerializer:
    is_configured = True

    def __init__(self, live_session_factory):
        self._live_session_factory = live_session_factory
        self.labels: list[str] = []

    async def execute(self, fn, *, label="", **_kwargs):
        self.labels.append(label)
        with self._live_session_factory() as live_db:
            result = fn(live_db)
            live_db.commit()
            return result


def test_happy_path_inserts_live_session(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder")

    with SessionLocal() as db:
        result = asyncio.run(
            launch_remote_session(
                db,
                RemoteLaunchParams(
                    owner_id=OWNER_ID,
                    device_id="cinder",
                    provider="codex",
                    cwd="/Users/me/repo",
                ),
                registry=registry,
            )
        )

    assert result.launch_state == "live"
    assert result.execution_lifetime == "live_control"
    with SessionLocal() as db:
        row = db.get(AgentSession, result.session_id)
        assert row is not None
        attempt = _latest_attempt(db, result.session_id)
        assert attempt.state == "adopted"
        assert attempt.execution_lifetime == "live_control"
        assert attempt.error_code is None
        assert attempt.expires_at is None
        assert attempt.run_id is not None
        assert row.provider == "codex"
        assert row.cwd == "/Users/me/repo"
        assert row.device_id == "cinder"
        capabilities, control = _project_control(db, row)
        assert capabilities.managed_transport.value == "codex_app_server"
        assert control.source_runner_id is None

    # verify we dispatched a session.launch with the pre-allocated id
    assert len(registry.sent) == 1
    sent = registry.sent[0]
    assert sent["command_type"] == "session.launch"
    assert sent["session_id"] == str(result.session_id)
    assert sent["payload"]["provider"] == "codex"
    assert sent["payload"]["execution_lifetime"] == "live_control"


def test_launch_remote_session_writes_live_launch_readiness(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder")

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)

    live_serializer = _LiveReadinessSerializer(LiveSession)
    monkeypatch.setattr(remote_launch_module.database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(remote_launch_module, "get_live_write_serializer", lambda: live_serializer)

    try:
        with SessionLocal() as db:
            result = asyncio.run(
                launch_remote_session(
                    db,
                    RemoteLaunchParams(
                        owner_id=OWNER_ID,
                        device_id="cinder",
                        provider="codex",
                        cwd="/Users/me/repo",
                        project="repo",
                        client_request_id="tap-live-readiness",
                    ),
                    registry=registry,
                )
        )

        assert result.launch_state == "launching_unknown"
        assert live_serializer.labels == ["launch-readiness", "launch-readiness"]
        sent = registry.sent[0]
        with SessionLocal() as db:
            assert db.get(AgentSession, result.session_id) is not None
            assert _latest_attempt(db, result.session_id).state == "dispatched"
            assert db.query(SessionConnection).count() == 0
        with LiveSession() as live_db:
            row = live_db.get(LiveLaunchReadiness, str(result.session_id))
            assert row is not None
            assert row.state == "dispatched"
            assert row.expires_at is not None
            assert row.owner_id == str(OWNER_ID)
            assert row.device_id == "cinder"
            assert row.machine_id == "cinder"
            assert row.provider == "codex"
            assert row.execution_lifetime == "live_control"
            assert row.project == "repo"
            assert row.client_request_id == "tap-live-readiness"
            assert row.command_id == sent["command_id"]
            assert row.error_code is None
            assert row.error_message is None
            outbox = live_db.query(LiveArchiveOutbox).order_by(LiveArchiveOutbox.id.asc()).all()
            assert [row.kind for row in outbox] == ["remote_launch.v1", "remote_launch_outcome.v1"]
            with SessionLocal() as archive_db:
                drained = drain_live_archive_outbox(live_db, archive_db)
            assert drained.drained == 2
        with SessionLocal() as db:
            session = db.get(AgentSession, result.session_id)
            assert session is not None
            assert _latest_attempt(db, result.session_id).state == "dispatched"
            assert db.query(SessionConnection).count() == 0
    finally:
        live_engine.dispose()


def test_launch_client_request_id_replay_skips_live_launch_readiness_write(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder")

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)

    live_serializer = _LiveReadinessSerializer(LiveSession)
    monkeypatch.setattr(remote_launch_module.database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(remote_launch_module, "get_live_write_serializer", lambda: live_serializer)

    params = RemoteLaunchParams(
        owner_id=OWNER_ID,
        device_id="cinder",
        provider="codex",
        cwd="/Users/me/repo",
        client_request_id="tap-live-readiness-replay",
    )

    try:
        with SessionLocal() as db:
            first = asyncio.run(launch_remote_session(db, params, registry=registry))
        with SessionLocal() as db:
            second = asyncio.run(launch_remote_session(db, params, registry=registry))

        assert first.session_id == second.session_id
        assert len(registry.sent) == 1
        assert live_serializer.labels == ["launch-readiness", "launch-readiness"]
        with LiveSession() as live_db:
            rows = live_db.query(LiveLaunchReadiness).all()
            assert len(rows) == 1
            assert rows[0].session_id == str(first.session_id)
            assert rows[0].command_id == registry.sent[0]["command_id"]
            assert rows[0].client_request_id == "tap-live-readiness-replay"
            assert rows[0].state == "dispatched"
            assert live_db.query(LiveArchiveOutbox).count() == 2
    finally:
        live_engine.dispose()


def test_launch_client_request_id_replay_uses_durable_attempt_when_live_store_is_configured(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder")

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live-replay-pending-archive.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)

    live_serializer = _LiveReadinessSerializer(LiveSession)
    monkeypatch.setattr(remote_launch_module.database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(remote_launch_module, "get_live_write_serializer", lambda: live_serializer)

    params = RemoteLaunchParams(
        owner_id=OWNER_ID,
        device_id="cinder",
        provider="codex",
        cwd="/Users/me/repo",
        client_request_id="tap-live-over-archive",
    )

    try:
        with SessionLocal() as db:
            first = asyncio.run(launch_remote_session(db, params, registry=registry))
        with LiveSession() as live_db, SessionLocal() as archive_db:
            drained = drain_live_archive_outbox(live_db, archive_db, limit=1)
            assert drained.drained == 1
        with SessionLocal() as db:
            assert _latest_attempt(db, first.session_id).state == "dispatched"

        with SessionLocal() as db:
            second = asyncio.run(launch_remote_session(db, params, registry=registry))

        assert second.session_id == first.session_id
        assert second.launch_state == "launching_unknown"
        assert len(registry.sent) == 1
    finally:
        live_engine.dispose()


def test_launch_remote_session_does_not_require_live_store_mirror_write(tmp_path, monkeypatch, caplog):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder")

    class _FailingLiveSerializer:
        is_configured = True

        async def execute(self, _fn, *, label="", **_kwargs):
            assert label == "launch-readiness"
            raise RuntimeError("live store unavailable")

    monkeypatch.setattr(remote_launch_module.database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(remote_launch_module, "get_live_write_serializer", lambda: _FailingLiveSerializer())

    with caplog.at_level(logging.WARNING, logger="zerg.services.remote_session_launch"):
        with SessionLocal() as db:
            result = asyncio.run(
                launch_remote_session(
                    db,
                    RemoteLaunchParams(
                        owner_id=OWNER_ID,
                        device_id="cinder",
                        provider="codex",
                        cwd="/Users/me/repo",
                    ),
                    registry=registry,
                )
            )

    assert result.launch_state == "launching_unknown"
    assert len(registry.sent) == 1
    assert "Failed to write live launch readiness" in caplog.text


def test_one_shot_launch_requires_initial_prompt_before_provider_support(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.run_once",))

    with SessionLocal() as db:
        with pytest.raises(RemoteLaunchError) as excinfo:
            asyncio.run(
                launch_remote_session(
                    db,
                    RemoteLaunchParams(
                        owner_id=OWNER_ID,
                        device_id="cinder",
                        provider="codex",
                        cwd="/Users/me/repo",
                        execution_lifetime="one_shot",
                    ),
                    registry=registry,
                )
            )

    assert excinfo.value.code == "invalid_request"
    assert len(registry.sent) == 0
    with SessionLocal() as db:
        assert db.query(AgentSession).count() == 0


def test_one_shot_launch_requires_machine_support_before_dispatch(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.launch",))

    with SessionLocal() as db:
        with pytest.raises(RemoteLaunchError) as excinfo:
            asyncio.run(
                launch_remote_session(
                    db,
                    RemoteLaunchParams(
                        owner_id=OWNER_ID,
                        device_id="cinder",
                        provider="codex",
                        cwd="/Users/me/repo",
                        initial_prompt="Do one bounded turn",
                        execution_lifetime="one_shot",
                    ),
                    registry=registry,
                )
            )

    assert excinfo.value.code == "provider_unsupported"
    assert "codex.run_once" in excinfo.value.detail
    assert len(registry.sent) == 0
    with SessionLocal() as db:
        assert db.query(AgentSession).count() == 0


def test_one_shot_happy_path_creates_codex_exec_run(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.run_once",))

    with SessionLocal() as db:
        result = asyncio.run(
            launch_remote_session(
                db,
                RemoteLaunchParams(
                    owner_id=OWNER_ID,
                    device_id="cinder",
                    provider="codex",
                    cwd="/Users/me/repo",
                    initial_prompt="Do one bounded turn",
                    execution_lifetime="one_shot",
                ),
                registry=registry,
            )
        )

    assert result.launch_state == "live"
    assert result.execution_lifetime == "one_shot"
    assert len(registry.sent) == 1
    sent = registry.sent[0]
    assert sent["command_type"] == "session.run_once"
    assert sent["payload"]["provider"] == "codex"
    assert sent["payload"]["initial_prompt"] == "Do one bounded turn"
    assert sent["payload"]["execution_lifetime"] == "one_shot"
    assert sent["payload"]["run_id"]

    with SessionLocal() as db:
        row = db.get(AgentSession, result.session_id)
        assert row is not None
        attempt = _latest_attempt(db, result.session_id)
        run = db.get(SessionRun, attempt.run_id)
        connection = db.query(SessionConnection).filter(SessionConnection.run_id == run.id).one()
        assert attempt.state == "adopted"
        assert attempt.execution_lifetime == "one_shot"
        assert str(run.id) == sent["payload"]["run_id"]
        assert run.launch_origin == "longhouse_spawned"
        assert run.ended_at is None
        assert connection.control_plane == "codex_exec"
        assert connection.state == "attached"
        assert connection.can_send_input == 0
        assert connection.can_interrupt == 0
        assert connection.can_resume == 0
        assert row.ended_at is None


def test_one_shot_timeout_late_success_adopts_reserved_run(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)

    class _TimeoutRegistry(_StubRegistry):
        async def send_command(self, **kwargs):
            self.sent.append(kwargs)
            return MachineControlCommandResponse(transport_ok=False, error="timed out")

    registry = _TimeoutRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.run_once",))

    with SessionLocal() as db:
        result = asyncio.run(
            launch_remote_session(
                db,
                RemoteLaunchParams(
                    owner_id=OWNER_ID,
                    device_id="cinder",
                    provider="codex",
                    cwd="/Users/me/repo",
                    initial_prompt="Do one bounded turn",
                    execution_lifetime="one_shot",
                ),
                registry=registry,
            )
        )

    assert result.launch_state == "launching_unknown"
    command_id = registry.sent[-1]["command_id"]
    run_id = registry.sent[-1]["payload"]["run_id"]
    with SessionLocal() as db:
        attempt = _latest_attempt(db, result.session_id)
        run = db.get(SessionRun, attempt.run_id)
        assert attempt.state == "dispatched"
        assert str(attempt.run_id) == run_id
        assert run.ended_at is None
        assert db.query(SessionConnection).count() == 0

    with SessionLocal() as db:
        reconciled = reconcile_launch_from_command_result(
            db,
            {
                "type": "command_result",
                "command_id": command_id,
                "ok": True,
                "result": {
                    "session_id": str(result.session_id),
                    "pid": 4242,
                    "argv": ["codex", "exec", "--json", "Do one bounded turn"],
                },
            },
        )

    assert reconciled is True
    with SessionLocal() as db:
        attempt = _latest_attempt(db, result.session_id)
        run = db.get(SessionRun, attempt.run_id)
        connection = db.query(SessionConnection).filter(SessionConnection.run_id == run.id).one()
        assert attempt.state == "adopted"
        assert str(run.id) == run_id
        assert run.pid == 4242
        assert run.argv_redacted_json == ["codex", "exec", "--json", "Do one bounded turn"]
        assert connection.control_plane == "codex_exec"
        assert connection.state == "attached"


def test_one_shot_late_success_preserves_terminal_closed_run(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)

    class _TimeoutRegistry(_StubRegistry):
        async def send_command(self, **kwargs):
            self.sent.append(kwargs)
            return MachineControlCommandResponse(transport_ok=False, error="timed out")

    registry = _TimeoutRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.run_once",))

    with SessionLocal() as db:
        result = asyncio.run(
            launch_remote_session(
                db,
                RemoteLaunchParams(
                    owner_id=OWNER_ID,
                    device_id="cinder",
                    provider="codex",
                    cwd="/Users/me/repo",
                    initial_prompt="Do one bounded turn",
                    execution_lifetime="one_shot",
                ),
                registry=registry,
            )
        )

    command_id = registry.sent[-1]["command_id"]
    with SessionLocal() as db:
        attempt = _latest_attempt(db, result.session_id)
        run = db.get(SessionRun, attempt.run_id)
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=f"codex:{result.session_id}",
                    session_id=result.session_id,
                    thread_id=attempt.thread_id,
                    run_id=run.id,
                    provider="codex",
                    device_id="cinder",
                    source="codex_exec",
                    kind="terminal_signal",
                    occurred_at=datetime.now(timezone.utc),
                    dedupe_key=f"codex-exec:{run.id}:terminal",
                    payload={"terminal_state": "run_completed", "exit_code": 0},
                )
            ],
        )
        db.commit()
        db.refresh(run)
        ended_at = run.ended_at
        assert run.exit_status == "exit_0"
        assert db.query(SessionConnection).count() == 0

    with SessionLocal() as db:
        reconciled = reconcile_launch_from_command_result(
            db,
            {
                "type": "command_result",
                "command_id": command_id,
                "ok": True,
                "result": {
                    "session_id": str(result.session_id),
                    "pid": 4242,
                    "argv": ["codex", "exec", "--json", "Do one bounded turn"],
                },
            },
        )

    assert reconciled is True
    with SessionLocal() as db:
        attempt = _latest_attempt(db, result.session_id)
        run = db.get(SessionRun, attempt.run_id)
        connection = db.query(SessionConnection).filter(SessionConnection.run_id == run.id).one()
        assert attempt.state == "adopted"
        assert run.ended_at == ended_at
        assert run.exit_status == "exit_0"
        assert connection.control_plane == "codex_exec"
        assert connection.state == "ended"
        assert connection.released_at == ended_at


def test_one_shot_late_failure_preserves_terminal_exit_status(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)

    class _TimeoutRegistry(_StubRegistry):
        async def send_command(self, **kwargs):
            self.sent.append(kwargs)
            return MachineControlCommandResponse(transport_ok=False, error="timed out")

    registry = _TimeoutRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.run_once",))

    with SessionLocal() as db:
        result = asyncio.run(
            launch_remote_session(
                db,
                RemoteLaunchParams(
                    owner_id=OWNER_ID,
                    device_id="cinder",
                    provider="codex",
                    cwd="/Users/me/repo",
                    initial_prompt="Do one bounded turn",
                    execution_lifetime="one_shot",
                ),
                registry=registry,
            )
        )

    command_id = registry.sent[-1]["command_id"]
    with SessionLocal() as db:
        attempt = _latest_attempt(db, result.session_id)
        run = db.get(SessionRun, attempt.run_id)
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=f"codex:{result.session_id}",
                    session_id=result.session_id,
                    thread_id=attempt.thread_id,
                    run_id=run.id,
                    provider="codex",
                    device_id="cinder",
                    source="codex_exec",
                    kind="terminal_signal",
                    occurred_at=datetime.now(timezone.utc),
                    dedupe_key=f"codex-exec:{run.id}:terminal",
                    payload={"terminal_state": "run_completed", "exit_code": 0},
                )
            ],
        )
        db.commit()
        db.refresh(run)
        ended_at = run.ended_at
        assert run.exit_status == "exit_0"

    with SessionLocal() as db:
        reconciled = reconcile_launch_from_command_result(
            db,
            {
                "type": "command_result",
                "command_id": command_id,
                "ok": False,
                "error": {"code": "provider_launch_failed", "message": "provider failed after exit"},
            },
        )

    assert reconciled is True
    with SessionLocal() as db:
        attempt = _latest_attempt(db, result.session_id)
        run = db.get(SessionRun, attempt.run_id)
        assert attempt.state == "failed"
        assert run.ended_at == ended_at
        assert run.exit_status == "exit_0"


def test_one_shot_run_terminal_after_session_ended_closes_run_connection(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)

    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.run_once",))

    with SessionLocal() as db:
        result = asyncio.run(
            launch_remote_session(
                db,
                RemoteLaunchParams(
                    owner_id=OWNER_ID,
                    device_id="cinder",
                    provider="codex",
                    cwd="/Users/me/repo",
                    initial_prompt="Do one bounded turn",
                    execution_lifetime="one_shot",
                ),
                registry=registry,
            )
        )

    with SessionLocal() as db:
        attempt = _latest_attempt(db, result.session_id)
        run = db.get(SessionRun, attempt.run_id)
        connection = db.query(SessionConnection).filter(SessionConnection.run_id == run.id).one()
        assert connection.state == "attached"

        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=f"codex:{result.session_id}",
                    session_id=result.session_id,
                    thread_id=attempt.thread_id,
                    provider="codex",
                    device_id="cinder",
                    source="codex_bridge",
                    kind="terminal_signal",
                    occurred_at=datetime.now(timezone.utc),
                    dedupe_key=f"codex:{result.session_id}:session-ended",
                    payload={"terminal_state": "session_ended"},
                )
            ],
        )
        db.commit()

    with SessionLocal() as db:
        attempt = _latest_attempt(db, result.session_id)
        run = db.get(SessionRun, attempt.run_id)
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=f"codex:{result.session_id}",
                    session_id=result.session_id,
                    thread_id=attempt.thread_id,
                    run_id=run.id,
                    provider="codex",
                    device_id="cinder",
                    source="codex_exec",
                    kind="terminal_signal",
                    occurred_at=datetime.now(timezone.utc),
                    dedupe_key=f"codex-exec:{run.id}:terminal",
                    payload={"terminal_state": "run_completed", "exit_code": 0},
                )
            ],
        )
        db.commit()

    with SessionLocal() as db:
        row = db.get(AgentSession, result.session_id)
        attempt = _latest_attempt(db, result.session_id)
        run = db.get(SessionRun, attempt.run_id)
        connection = db.query(SessionConnection).filter(SessionConnection.run_id == run.id).one()
        assert row.ended_at is not None
        assert run.ended_at is not None
        assert run.exit_status == "exit_0"
        assert connection.state == "ended"
        assert connection.released_at == run.ended_at


def test_one_shot_reaper_closes_reserved_run_without_connection(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)

    class _TimeoutRegistry(_StubRegistry):
        async def send_command(self, **kwargs):
            self.sent.append(kwargs)
            return MachineControlCommandResponse(transport_ok=False, error="timed out")

    registry = _TimeoutRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.run_once",))

    with SessionLocal() as db:
        result = asyncio.run(
            launch_remote_session(
                db,
                RemoteLaunchParams(
                    owner_id=OWNER_ID,
                    device_id="cinder",
                    provider="codex",
                    cwd="/Users/me/repo",
                    initial_prompt="Do one bounded turn",
                    execution_lifetime="one_shot",
                ),
                registry=registry,
            )
        )

    with SessionLocal() as db:
        attempt = _latest_attempt(db, result.session_id)
        reaped = reap_orphaned_launches(db, now=attempt.expires_at)

    assert reaped == 1
    with SessionLocal() as db:
        row = db.get(AgentSession, result.session_id)
        attempt = _latest_attempt(db, result.session_id)
        run = db.get(SessionRun, attempt.run_id)
        assert attempt.state == "abandoned"
        assert attempt.error_code == "launch_timeout"
        assert row.ended_at is not None
        assert run.ended_at is not None
        assert run.exit_status == "launch_timeout"
        assert db.query(SessionConnection).count() == 0


def test_remote_launch_does_not_wait_on_write_serializer_when_writer_saturated(tmp_path, monkeypatch):
    class SaturatedWriter:
        is_configured = True
        writer_active = True
        active_label = "ingest-replay"
        active_age_ms = 30_000.0
        queue_depth = 999

        async def execute(self, *_args, **_kwargs):  # pragma: no cover - regression guard
            raise AssertionError("remote launch should not enter the serialized writer lane")

        async def execute_or_direct(self, *_args, **_kwargs):  # pragma: no cover - regression guard
            raise AssertionError("remote launch should not enter the serialized writer lane")

        async def execute_after_closing_request_session(self, *_args, **_kwargs):  # pragma: no cover - regression guard
            raise AssertionError("remote launch should not enter the serialized writer lane")

    monkeypatch.setattr("zerg.services.write_serializer.get_write_serializer", lambda: SaturatedWriter())

    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder")

    with SessionLocal() as db:
        result = asyncio.run(
            launch_remote_session(
                db,
                RemoteLaunchParams(
                    owner_id=OWNER_ID,
                    device_id="cinder",
                    provider="codex",
                    cwd="/Users/me/repo",
                ),
                registry=registry,
            )
        )

    assert result.launch_state == "live"
    assert len(registry.sent) == 1


def test_remote_launch_materializes_archive_shell_before_dispatch(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)

    class HotDispatchRegistry(_StubRegistry):
        async def send_command_nowait(self, **kwargs):  # type: ignore[override]
            with SessionLocal() as db:
                assert db.get(AgentSession, kwargs["session_id"]) is not None
            return await super().send_command_nowait(**kwargs)

    registry = HotDispatchRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder")

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live-hot-dispatch.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)
    live_serializer = _LiveReadinessSerializer(LiveSession)
    monkeypatch.setattr(remote_launch_module.database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(remote_launch_module, "get_live_write_serializer", lambda: live_serializer)

    try:
        with SessionLocal() as db:
            result = asyncio.run(
                launch_remote_session(
                    db,
                    RemoteLaunchParams(
                        owner_id=OWNER_ID,
                        device_id="cinder",
                        provider="codex",
                        cwd="/Users/me/repo",
                    ),
                    registry=registry,
                )
            )
        assert result.launch_state == "launching_unknown"
        assert len(registry.sent) == 1
        with SessionLocal() as db:
            assert db.get(AgentSession, result.session_id) is not None
            assert _latest_attempt(db, result.session_id).state == "dispatched"
    finally:
        live_engine.dispose()


def test_live_launch_detail_uses_durable_shell_before_archive_drain(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder")

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live-detail-placeholder.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)
    live_serializer = _LiveReadinessSerializer(LiveSession)
    monkeypatch.setattr(remote_launch_module.database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(remote_launch_module.database_module, "get_live_session_factory", lambda: LiveSession)
    monkeypatch.setattr(remote_launch_module, "get_live_write_serializer", lambda: live_serializer)

    try:
        with SessionLocal() as db:
            result = asyncio.run(
                launch_remote_session(
                    db,
                    RemoteLaunchParams(
                        owner_id=OWNER_ID,
                        device_id="cinder",
                        provider="codex",
                        cwd="/Users/me/repo",
                        project="repo",
                    ),
                    registry=registry,
                )
            )
            assert db.get(AgentSession, result.session_id) is not None

        client, api_app = _make_agents_client(SessionLocal)
        try:
            resp = client.get(f"/api/agents/sessions/{result.session_id}", headers={"X-Agents-Token": "dev"})
        finally:
            api_app.dependency_overrides.clear()

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == str(result.session_id)
        assert body["provider"] == "codex"
        assert body["project"] == "repo"
        assert body["launch_state"] == "launching_unknown"
        assert body["runtime_source"] != "live_launch_readiness"
        assert body["runtime_display"]["detail"] != "Archive is catching up."

        with SessionLocal() as db:
            session = db.get(AgentSession, result.session_id)
            assert session is not None
            [list_row] = build_session_response_list(db=db, store=AgentsStore(db), sessions=[session])
        assert list_row.launch_state == "launching_unknown"
        assert list_row.runtime_source != "live_launch_readiness"
    finally:
        live_engine.dispose()


def test_live_launch_detail_fallback_requires_owner_match(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live-detail-owner.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)
    monkeypatch.setattr(remote_launch_module.database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(remote_launch_module.database_module, "get_live_session_factory", lambda: LiveSession)

    try:
        session_id = uuid4()
        with LiveSession() as live_db:
            upsert_live_launch_readiness(
                live_db,
                session_id=session_id,
                owner_id=OWNER_ID,
                device_id="cinder",
                provider="codex",
                execution_lifetime="live_control",
                state="adopted",
                command_id=f"launch-{session_id}",
                client_request_id=None,
                machine_id="cinder",
                project="repo",
                expires_at=None,
            )
            live_db.commit()

        monkeypatch.setattr("zerg.routers.agents_sessions._owner_id_from_agents_auth", lambda _db, _auth: OWNER_ID + 1)
        client, api_app = _make_agents_client(SessionLocal, owner_id=OWNER_ID + 1)
        try:
            resp = client.get(f"/api/agents/sessions/{session_id}", headers={"X-Agents-Token": "dev"})
        finally:
            api_app.dependency_overrides.clear()

        assert resp.status_code == 404
    finally:
        live_engine.dispose()


def test_failed_live_launch_detail_uses_durable_shell(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder")

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live-detail-failed.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)
    live_serializer = _LiveReadinessSerializer(LiveSession)
    monkeypatch.setattr(remote_launch_module.database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(remote_launch_module.database_module, "get_live_session_factory", lambda: LiveSession)
    monkeypatch.setattr(remote_launch_module, "get_live_write_serializer", lambda: live_serializer)

    try:
        with SessionLocal() as db:
            result = asyncio.run(
                launch_remote_session(
                    db,
                    RemoteLaunchParams(
                        owner_id=OWNER_ID,
                        device_id="cinder",
                        provider="codex",
                        cwd="/Users/me/missing",
                        project="repo",
                    ),
                    registry=registry,
                )
            )
            assert db.get(AgentSession, result.session_id) is not None
            assert result.launch_state == "launching_unknown"

        command_id = registry.sent[-1]["command_id"]
        with SessionLocal() as db:
            reconciled = reconcile_launch_from_command_result(
                db,
                {
                    "type": "command_result",
                    "command_id": command_id,
                    "ok": False,
                    "error": {"code": "cwd_not_found", "message": "missing cwd"},
                },
            )
            assert reconciled is True

        client, api_app = _make_agents_client(SessionLocal)
        try:
            resp = client.get(f"/api/agents/sessions/{result.session_id}", headers={"X-Agents-Token": "dev"})
        finally:
            api_app.dependency_overrides.clear()

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["launch_state"] == "launch_failed"
        assert body["ended_at"] is not None
        assert body["confidence"] is None
        assert body["user_state"] == "active"
        assert body["capabilities"]["display_label"] == "Launch failed"
        assert body["capabilities"]["composer_disabled_reason"] == "Launch failed."
    finally:
        live_engine.dispose()


def test_live_launch_workspace_uses_placeholder_while_shell_is_catching_up(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder")

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live-workspace-placeholder.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)
    live_serializer = _LiveReadinessSerializer(LiveSession)
    monkeypatch.setattr(remote_launch_module.database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(remote_launch_module.database_module, "get_live_session_factory", lambda: LiveSession)
    monkeypatch.setattr(remote_launch_module, "get_live_write_serializer", lambda: live_serializer)

    try:
        with SessionLocal() as db:
            result = asyncio.run(
                launch_remote_session(
                    db,
                    RemoteLaunchParams(
                        owner_id=OWNER_ID,
                        device_id="cinder",
                        provider="codex",
                        cwd="/Users/me/repo",
                        project="repo",
                    ),
                    registry=registry,
                )
            )
            workspace = build_session_workspace(
                db=db,
                session_id=result.session_id,
                owner_id=OWNER_ID,
            )

        assert workspace.session.id == str(result.session_id)
        assert workspace.session.launch_state == "launching_unknown"
        assert workspace.session.capabilities.display_label == "Launching"
        assert workspace.session.capabilities.staleness_reason == "archive_catching_up"
        assert workspace.thread.root_session_id == str(result.session_id)
        assert workspace.projection.items == []
        assert workspace.projection.total == 0
        assert workspace.workspace_revision.fingerprint.startswith("live-launch:")
    finally:
        live_engine.dispose()


def test_live_launch_mobile_tail_uses_placeholder_while_shell_is_catching_up(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder")

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live-mobile-tail-placeholder.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)
    live_serializer = _LiveReadinessSerializer(LiveSession)
    monkeypatch.setattr(remote_launch_module.database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(remote_launch_module.database_module, "get_live_session_factory", lambda: LiveSession)
    monkeypatch.setattr(remote_launch_module, "get_live_write_serializer", lambda: live_serializer)

    try:
        with SessionLocal() as db:
            result = asyncio.run(
                launch_remote_session(
                    db,
                    RemoteLaunchParams(
                        owner_id=OWNER_ID,
                        device_id="cinder",
                        provider="codex",
                        cwd="/Users/me/repo",
                        project="repo",
                    ),
                    registry=registry,
                )
            )
            mobile_tail = build_session_mobile_tail(
                db=db,
                session_id=result.session_id,
                owner_id=OWNER_ID,
            )

        assert mobile_tail.session.id == str(result.session_id)
        assert mobile_tail.session.launch_state == "launching_unknown"
        assert mobile_tail.session.capabilities.display_label == "Launching"
        assert mobile_tail.session.capabilities.staleness_reason == "archive_catching_up"
        assert mobile_tail.projection.items == []
        assert mobile_tail.projection.total == 0
        assert mobile_tail.snapshot_event_id is None
        assert mobile_tail.workspace_revision.fingerprint.startswith("live-launch:")
    finally:
        live_engine.dispose()


def test_late_launch_result_reconciles_durable_attempt(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)

    class TimeoutRegistry(_StubRegistry):
        async def send_command(self, **kwargs):  # type: ignore[override]
            self.sent.append(kwargs)
            return MachineControlCommandResponse(transport_ok=False, error="timed out")

    registry = TimeoutRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder")

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live-late-result.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)
    live_serializer = _LiveReadinessSerializer(LiveSession)
    monkeypatch.setattr(remote_launch_module.database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(remote_launch_module.database_module, "get_live_write_session_factory", lambda: LiveSession)
    monkeypatch.setattr(remote_launch_module, "get_live_write_serializer", lambda: live_serializer)

    try:
        with SessionLocal() as db:
            result = asyncio.run(
                launch_remote_session(
                    db,
                    RemoteLaunchParams(
                        owner_id=OWNER_ID,
                        device_id="cinder",
                        provider="codex",
                        cwd="/Users/me/repo",
                    ),
                    registry=registry,
                )
            )

        assert result.launch_state == "launching_unknown"
        command_id = registry.sent[-1]["command_id"]
        with SessionLocal() as db:
            assert db.query(SessionLaunchAttempt).count() == 1
            attempt = _latest_attempt(db, result.session_id)
            assert attempt.state == "dispatched"
            reconciled = reconcile_launch_from_command_result(
                db,
                {
                    "type": "command_result",
                    "command_id": command_id,
                    "ok": True,
                    "result": {"session_id": str(result.session_id), "thread_id": "codex-thread-late"},
                },
            )
        assert reconciled is True

        with LiveSession() as live_db:
            readiness = live_db.get(LiveLaunchReadiness, str(result.session_id))
            assert readiness.state == "dispatched"
            assert live_db.query(LiveArchiveOutbox).count() == 2
        with SessionLocal() as db:
            attempt = _latest_attempt(db, result.session_id)
            assert attempt.state == "adopted"
            assert db.query(SessionConnection).count() == 1
    finally:
        live_engine.dispose()


def test_hot_launch_nowait_transport_failure_marks_durable_attempt_failed(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)

    class FailingNowaitRegistry(_StubRegistry):
        async def send_command_nowait(self, **kwargs):  # type: ignore[override]
            self.sent.append(kwargs)
            return MachineControlCommandResponse(transport_ok=False, error="socket closed")

    registry = FailingNowaitRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder")

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live-nowait-failed.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)
    live_serializer = _LiveReadinessSerializer(LiveSession)
    monkeypatch.setattr(remote_launch_module.database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(remote_launch_module, "get_live_write_serializer", lambda: live_serializer)

    try:
        with SessionLocal() as db:
            result = asyncio.run(
                launch_remote_session(
                    db,
                    RemoteLaunchParams(
                        owner_id=OWNER_ID,
                        device_id="cinder",
                        provider="codex",
                        cwd="/Users/me/repo",
                    ),
                    registry=registry,
                )
            )

        assert result.launch_state == "launch_failed"
        assert result.launch_error_code == "machine_offline"
        with SessionLocal() as db:
            session = db.get(AgentSession, result.session_id)
            assert session is not None
            assert session.ended_at is not None
            attempt = _latest_attempt(db, result.session_id)
            assert attempt.state == "failed"
            assert attempt.error_code == "machine_offline"
        with LiveSession() as live_db:
            readiness = live_db.get(LiveLaunchReadiness, str(result.session_id))
            assert readiness.state == "failed"
            assert readiness.error_code == "machine_offline"
    finally:
        live_engine.dispose()


def test_hot_one_shot_launch_dispatches_reserved_run_then_late_adopts(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.run_once",))

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live-one-shot-nowait.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)
    live_serializer = _LiveReadinessSerializer(LiveSession)
    monkeypatch.setattr(remote_launch_module.database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(remote_launch_module, "get_live_write_serializer", lambda: live_serializer)

    try:
        with SessionLocal() as db:
            result = asyncio.run(
                launch_remote_session(
                    db,
                    RemoteLaunchParams(
                        owner_id=OWNER_ID,
                        device_id="cinder",
                        provider="codex",
                        cwd="/Users/me/repo",
                        initial_prompt="Do one bounded turn",
                        execution_lifetime="one_shot",
                    ),
                    registry=registry,
                )
            )

        assert result.launch_state == "launching_unknown"
        sent = registry.sent[-1]
        assert sent["command_type"] == "session.run_once"
        assert sent["payload"]["run_id"]
        with SessionLocal() as db:
            attempt = _latest_attempt(db, result.session_id)
            assert attempt.state == "dispatched"
            assert str(attempt.run_id) == sent["payload"]["run_id"]
            assert db.query(SessionConnection).count() == 0
            reconciled = reconcile_launch_from_command_result(
                db,
                {
                    "type": "command_result",
                    "command_id": sent["command_id"],
                    "ok": True,
                    "result": {
                        "session_id": str(result.session_id),
                        "pid": 4242,
                        "argv": ["codex", "exec", "--json", "Do one bounded turn"],
                    },
                },
            )
            assert reconciled is True
        with SessionLocal() as db:
            attempt = _latest_attempt(db, result.session_id)
            run = db.get(SessionRun, attempt.run_id)
            assert attempt.state == "adopted"
            assert run.pid == 4242
            assert db.query(SessionConnection).filter(SessionConnection.run_id == run.id).one()
    finally:
        live_engine.dispose()


def test_happy_path_inserts_live_claude_channel_session(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("claude.launch",))

    with SessionLocal() as db:
        result = asyncio.run(
            launch_remote_session(
                db,
                RemoteLaunchParams(
                    owner_id=OWNER_ID,
                    device_id="cinder",
                    provider="claude",
                    cwd="/Users/me/repo",
                ),
                registry=registry,
            )
        )

    assert result.launch_state == "live"
    with SessionLocal() as db:
        row = db.get(AgentSession, result.session_id)
        assert row is not None
        connection = db.query(SessionConnection).one()
        assert row.provider == "claude"
        capabilities, control = _project_control(db, row)
        assert capabilities.managed_transport.value == "claude_channel_bridge"
        assert control.source_runner_id is None
        assert connection.control_plane == "claude_channel_bridge"
        assert connection.can_send_input == 1
        assert connection.can_interrupt == 1
        # Managed claude is now resumable (manifest can_resume=true).
        assert connection.can_resume == 1

    assert len(registry.sent) == 1
    sent = registry.sent[0]
    assert sent["command_type"] == "session.launch"
    assert sent["session_id"] == str(result.session_id)
    assert sent["payload"]["provider"] == "claude"


def test_happy_path_inserts_live_opencode_server_bridge_session(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("opencode.launch",))

    with SessionLocal() as db:
        result = asyncio.run(
            launch_remote_session(
                db,
                RemoteLaunchParams(
                    owner_id=OWNER_ID,
                    device_id="cinder",
                    provider="opencode",
                    cwd="/Users/me/repo",
                ),
                registry=registry,
            )
        )

    assert result.launch_state == "live"
    with SessionLocal() as db:
        row = db.get(AgentSession, result.session_id)
        assert row is not None
        connection = db.query(SessionConnection).one()
        assert row.provider == "opencode"
        capabilities, control = _project_control(db, row)
        assert capabilities.managed_transport.value == "opencode_server_bridge"
        assert control.source_runner_id is None
        assert connection.control_plane == "opencode_server_bridge"
        assert connection.can_send_input == 1
        assert connection.can_interrupt == 1
        assert connection.can_terminate == 1
        assert connection.can_tail_output == 1
        # Kernel can_resume is host reattach, not provider continue.
        assert connection.can_resume == 1
        assert capabilities.host_reattach_available is True
        assert capabilities.can_resume is True

    assert len(registry.sent) == 1
    sent = registry.sent[0]
    assert sent["command_type"] == "session.launch"
    assert sent["session_id"] == str(result.session_id)
    assert sent["payload"]["provider"] == "opencode"


def test_offline_machine_returns_409_no_row(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    # Never register — machine offline

    with SessionLocal() as db:
        with pytest.raises(RemoteLaunchError) as excinfo:
            asyncio.run(
                launch_remote_session(
                    db,
                    RemoteLaunchParams(
                        owner_id=OWNER_ID,
                        device_id="cinder",
                        provider="codex",
                        cwd="/Users/me/repo",
                    ),
                    registry=registry,
                )
            )
    assert excinfo.value.code == "machine_offline"
    assert excinfo.value.status_code == 409

    with SessionLocal() as db:
        assert db.query(AgentSession).count() == 0


def test_provider_without_remote_launch_contract_rejected(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.launch",))

    with SessionLocal() as db:
        with pytest.raises(RemoteLaunchError) as excinfo:
            asyncio.run(
                launch_remote_session(
                    db,
                    RemoteLaunchParams(
                    owner_id=OWNER_ID,
                    device_id="cinder",
                    provider="antigravity",
                    cwd="/Users/me/repo",
                ),
                    registry=registry,
                )
            )
    assert excinfo.value.code == "provider_unsupported"


def test_provider_missing_machine_launch_support_rejected(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.launch",))

    with SessionLocal() as db:
        with pytest.raises(RemoteLaunchError) as excinfo:
            asyncio.run(
                launch_remote_session(
                    db,
                    RemoteLaunchParams(
                        owner_id=OWNER_ID,
                        device_id="cinder",
                        provider="claude",
                        cwd="/Users/me/repo",
                    ),
                    registry=registry,
                )
            )
    assert excinfo.value.code == "provider_unsupported"


def test_device_ownership_required(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal, owner_id=OWNER_ID)
    _seed_user_and_device(SessionLocal, owner_id=OWNER_ID + 1, device_id="not-mine")
    registry = _StubRegistry()
    # Register the other user's machine — shouldn't be launchable by OWNER_ID
    _register_online(registry, owner_id=OWNER_ID + 1, device_id="not-mine")

    with SessionLocal() as db:
        with pytest.raises(RemoteLaunchError) as excinfo:
            asyncio.run(
                launch_remote_session(
                    db,
                    RemoteLaunchParams(
                        owner_id=OWNER_ID,
                        device_id="not-mine",
                        provider="codex",
                        cwd="/Users/me/repo",
                    ),
                    registry=registry,
                )
            )
    assert excinfo.value.code == "device_not_enrolled"
    assert excinfo.value.status_code == 404


def test_engine_error_maps_to_launch_failed(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder")

    # First call will get a typed cwd_not_found error — use a wildcard match
    class _EngineErrorRegistry(_StubRegistry):
        async def send_command(self, **kwargs):
            self.sent.append(kwargs)
            return MachineControlCommandResponse(
                transport_ok=True,
                message={
                    "type": "command_result",
                    "ok": False,
                    "error": {"code": "cwd_not_found", "message": "nope"},
                },
            )

    err_registry = _EngineErrorRegistry()
    _register_online(err_registry, owner_id=OWNER_ID, device_id="cinder")

    with SessionLocal() as db:
        result = asyncio.run(
            launch_remote_session(
                db,
                RemoteLaunchParams(
                    owner_id=OWNER_ID,
                    device_id="cinder",
                    provider="codex",
                    cwd="/Users/me/repo",
                ),
                registry=err_registry,
            )
        )
    assert result.launch_state == "launch_failed"
    assert result.launch_error_code == "cwd_not_found"
    with SessionLocal() as db:
        row = db.get(AgentSession, result.session_id)
        attempt = _latest_attempt(db, result.session_id)
        assert attempt.state == "failed"
        assert attempt.error_code == "cwd_not_found"
        assert row.ended_at is not None


def test_one_shot_engine_error_closes_reserved_run(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)

    class _EngineErrorRegistry(_StubRegistry):
        async def send_command(self, **kwargs):
            self.sent.append(kwargs)
            return MachineControlCommandResponse(
                transport_ok=True,
                message={
                    "type": "command_result",
                    "ok": False,
                    "error": {"code": "cwd_not_found", "message": "nope"},
                },
            )

    registry = _EngineErrorRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.run_once",))

    with SessionLocal() as db:
        result = asyncio.run(
            launch_remote_session(
                db,
                RemoteLaunchParams(
                    owner_id=OWNER_ID,
                    device_id="cinder",
                    provider="codex",
                    cwd="/Users/me/missing-repo",
                    initial_prompt="Do one bounded turn",
                    execution_lifetime="one_shot",
                ),
                registry=registry,
            )
        )

    assert result.launch_state == "launch_failed"
    assert result.launch_error_code == "cwd_not_found"
    assert len(registry.sent) == 1
    assert registry.sent[0]["command_type"] == "session.run_once"
    with SessionLocal() as db:
        row = db.get(AgentSession, result.session_id)
        attempt = _latest_attempt(db, result.session_id)
        run = db.get(SessionRun, attempt.run_id)
        assert attempt.state == "failed"
        assert attempt.error_code == "cwd_not_found"
        assert attempt.execution_lifetime == "one_shot"
        assert row.ended_at is not None
        assert run is not None
        assert run.ended_at is not None
        assert run.exit_status == "cwd_not_found"
        assert db.query(SessionConnection).filter(SessionConnection.run_id == run.id).count() == 0


def test_transport_timeout_leaves_unknown(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)

    class _TimeoutRegistry(_StubRegistry):
        async def send_command(self, **kwargs):
            self.sent.append(kwargs)
            return MachineControlCommandResponse(
                transport_ok=False,
                error="command timed out after 30 seconds",
            )

    registry = _TimeoutRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder")

    with SessionLocal() as db:
        result = asyncio.run(
            launch_remote_session(
                db,
                RemoteLaunchParams(
                    owner_id=OWNER_ID,
                    device_id="cinder",
                    provider="codex",
                    cwd="/Users/me/repo",
                ),
                registry=registry,
            )
        )
    assert result.launch_state == "launching_unknown"
    with SessionLocal() as db:
        row = db.get(AgentSession, result.session_id)
        attempt = _latest_attempt(db, result.session_id)
        assert attempt.state == "dispatched"
        assert attempt.expires_at is not None
        assert row.ended_at is None


def test_cwd_relative_rejected_server_side(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder")

    with SessionLocal() as db:
        with pytest.raises(RemoteLaunchError) as excinfo:
            asyncio.run(
                launch_remote_session(
                    db,
                    RemoteLaunchParams(
                        owner_id=OWNER_ID,
                        device_id="cinder",
                        provider="codex",
                        cwd="not/absolute",
                    ),
                    registry=registry,
                )
            )
    assert excinfo.value.code == "cwd_not_allowed"


# -------- HTTP endpoint ---------------------------------------------------


def _make_browser_client(SessionLocal, *, owner_id: int = OWNER_ID):
    from zerg.main import api_app
    from zerg.main import app

    def override_db():
        with SessionLocal() as db:
            yield db

    def override_user():
        return SimpleNamespace(id=owner_id, email=f"u{owner_id}@ex.com", role="ADMIN")

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[get_current_browser_route_user] = override_user
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    return TestClient(app, backend="asyncio"), api_app


def _make_agents_client(SessionLocal, *, owner_id: int = OWNER_ID, device_id: str = "cinder"):
    from zerg.main import api_app
    from zerg.main import app

    def override_db():
        with SessionLocal() as db:
            yield db

    def override_verify_agents_token():
        return SimpleNamespace(owner_id=owner_id, device_id=device_id)

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    return TestClient(app, backend="asyncio"), api_app


def _patch_registry(registry):
    import zerg.services.remote_session_launch as module

    original = module.get_machine_control_channel_registry
    module.get_machine_control_channel_registry = lambda: registry
    return original, module


def test_http_endpoint_happy_path(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.run_once",))

    original, module = _patch_registry(registry)
    try:
        client, api_app = _make_browser_client(SessionLocal)
        try:
            resp = client.post(
                "/api/sessions/launch",
                json={
                    "device_id": "cinder",
                    "provider": "codex",
                    "cwd": "/Users/me/repo",
                    "initial_prompt": "Check the repo and report status",
                },
            )
        finally:
            api_app.dependency_overrides.clear()
    finally:
        module.get_machine_control_channel_registry = original

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["launch_state"] == "live"
    assert body["execution_lifetime"] == "one_shot"
    assert body["session_id"]
    assert registry.sent[0]["command_type"] == "session.run_once"
    assert registry.sent[0]["payload"]["execution_lifetime"] == "one_shot"


def test_http_endpoint_omitted_lifetime_without_prompt_rejects(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.run_once",))

    original, module = _patch_registry(registry)
    try:
        client, api_app = _make_browser_client(SessionLocal)
        try:
            resp = client.post(
                "/api/sessions/launch",
                json={
                    "device_id": "cinder",
                    "provider": "codex",
                    "cwd": "/Users/me/repo",
                },
            )
        finally:
            api_app.dependency_overrides.clear()
    finally:
        module.get_machine_control_channel_registry = original

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "invalid_request"
    assert registry.sent == []


def test_http_endpoint_explicit_live_control_survives_one_shot_default(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.launch",))

    original, module = _patch_registry(registry)
    try:
        client, api_app = _make_browser_client(SessionLocal)
        try:
            resp = client.post(
                "/api/sessions/launch",
                json={
                    "device_id": "cinder",
                    "provider": "codex",
                    "cwd": "/Users/me/repo",
                    "execution_lifetime": "live_control",
                },
            )
        finally:
            api_app.dependency_overrides.clear()
    finally:
        module.get_machine_control_channel_registry = original

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["execution_lifetime"] == "live_control"
    assert registry.sent[0]["command_type"] == "session.launch"
    assert registry.sent[0]["payload"]["execution_lifetime"] == "live_control"
    assert "initial_prompt" not in registry.sent[0]["payload"]


def test_http_continue_endpoint_happy_path(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.continue",))
    with SessionLocal() as db:
        session_id = _seed_continuable_codex_session(db)

    original, module = _patch_registry(registry)
    try:
        client, api_app = _make_browser_client(SessionLocal)
        try:
            resp = client.post(
                f"/api/sessions/{session_id}/continue",
                json={"client_request_id": "tap-continue"},
            )
        finally:
            api_app.dependency_overrides.clear()
    finally:
        module.get_machine_control_channel_registry = original

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session_id"] == str(session_id)
    assert body["launch_state"] == "live"
    assert body["execution_lifetime"] == "live_control"
    assert registry.sent[0]["payload"]["mode"] == "continue"


def test_http_continue_endpoint_message_defaults_to_one_shot(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.resume_run_once",))
    with SessionLocal() as db:
        session_id = _seed_continuable_codex_session(db)

    original, module = _patch_registry(registry)
    try:
        client, api_app = _make_browser_client(SessionLocal)
        try:
            resp = client.post(
                f"/api/sessions/{session_id}/continue",
                json={
                    "client_request_id": "tap-continue-with-message",
                    "message": "Please continue with a bounded follow-up.",
                },
            )
        finally:
            api_app.dependency_overrides.clear()
    finally:
        module.get_machine_control_channel_registry = original

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session_id"] == str(session_id)
    assert body["launch_state"] == "live"
    assert body["execution_lifetime"] == "one_shot"
    assert len(registry.sent) == 1
    sent = registry.sent[0]
    assert sent["command_type"] == "session.run_once"
    assert sent["payload"]["mode"] == "continue"
    assert sent["payload"]["resume"] == {
        "thread_id": "thread-abc",
        "thread_path": "/Users/me/.codex/sessions/thread-abc.jsonl",
    }
    assert sent["payload"]["initial_prompt"] == "Please continue with a bounded follow-up."
    assert sent["payload"]["execution_lifetime"] == "one_shot"
    assert sent["payload"]["run_id"]

    with SessionLocal() as db:
        attempt = _latest_attempt(db, session_id)
        assert attempt.execution_lifetime == "one_shot"
        assert attempt.run_id is not None
        run = db.get(SessionRun, attempt.run_id)
        assert run is not None
        assert run.launch_origin == "longhouse_continued"
        conn = (
            db.query(SessionConnection)
            .filter(SessionConnection.run_id == run.id)
            .one()
        )
        assert conn.control_plane == "codex_exec"
        assert conn.can_send_input == 0
        assert conn.can_resume == 0


def test_http_continue_endpoint_message_requires_bounded_resume_support(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    # Older engines advertise codex.run_once but cannot safely resume through it.
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.run_once", "codex.continue"))
    with SessionLocal() as db:
        session_id = _seed_continuable_codex_session(db)

    original, module = _patch_registry(registry)
    try:
        client, api_app = _make_browser_client(SessionLocal)
        try:
            resp = client.post(
                f"/api/sessions/{session_id}/continue",
                json={
                    "client_request_id": "tap-continue-old-engine",
                    "message": "Do not silently resume fresh.",
                },
            )
        finally:
            api_app.dependency_overrides.clear()
    finally:
        module.get_machine_control_channel_registry = original

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "provider_unsupported"
    assert "codex.resume_run_once" in resp.json()["detail"]["message"]
    assert registry.sent == []


def test_agents_continue_endpoint_happy_path(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.continue",))
    with SessionLocal() as db:
        session_id = _seed_continuable_codex_session(db)

    original, module = _patch_registry(registry)
    try:
        client, api_app = _make_agents_client(SessionLocal)
        try:
            resp = client.post(
                f"/api/agents/sessions/{session_id}/continue",
                json={"client_request_id": "agent-continue"},
                headers={"X-Agents-Token": "dev"},
            )
        finally:
            api_app.dependency_overrides.clear()
    finally:
        module.get_machine_control_channel_registry = original

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session_id"] == str(session_id)
    assert body["launch_state"] == "live"
    assert registry.sent[0]["payload"]["mode"] == "continue"


def test_client_request_id_is_idempotent(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder")

    params = RemoteLaunchParams(
        owner_id=OWNER_ID,
        device_id="cinder",
        provider="codex",
        cwd="/Users/me/repo",
        client_request_id="tap-1",
    )
    with SessionLocal() as db:
        first = asyncio.run(launch_remote_session(db, params, registry=registry))
    with SessionLocal() as db:
        second = asyncio.run(launch_remote_session(db, params, registry=registry))

    assert first.session_id == second.session_id
    assert len(registry.sent) == 1  # second call short-circuits


def test_client_request_id_is_owner_scoped(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal, owner_id=OWNER_ID, device_id="cinder")
    _seed_user_and_device(SessionLocal, owner_id=OWNER_ID + 1, device_id="cinder")
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder")
    _register_online(registry, owner_id=OWNER_ID + 1, device_id="cinder")

    first_params = RemoteLaunchParams(
        owner_id=OWNER_ID,
        device_id="cinder",
        provider="codex",
        cwd="/Users/me/repo",
        client_request_id="same-tap",
    )
    second_params = RemoteLaunchParams(
        owner_id=OWNER_ID + 1,
        device_id="cinder",
        provider="codex",
        cwd="/Users/other/repo",
        client_request_id="same-tap",
    )
    with SessionLocal() as db:
        first = asyncio.run(launch_remote_session(db, first_params, registry=registry))
    with SessionLocal() as db:
        second = asyncio.run(launch_remote_session(db, second_params, registry=registry))

    assert first.session_id != second.session_id
    assert len(registry.sent) == 2


def test_launched_codex_workspace_exposes_live_engine_control(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    launch_registry = _StubRegistry()
    _register_online(launch_registry, owner_id=OWNER_ID, device_id="cinder")

    global_registry = get_machine_control_channel_registry()
    asyncio.run(global_registry.clear_for_tests())
    _register_online(
        global_registry,
        owner_id=OWNER_ID,
        device_id="cinder",
        supports=("codex.launch", "codex.send", "codex.interrupt", "codex.steer"),
    )

    try:
        with SessionLocal() as db:
            result = asyncio.run(
                launch_remote_session(
                    db,
                    RemoteLaunchParams(
                        owner_id=OWNER_ID,
                        device_id="cinder",
                        provider="codex",
                        cwd="/Users/me/repo",
                    ),
                    registry=launch_registry,
                )
            )
            ingest_runtime_events(
                db,
                [
                    RuntimeEventIngest(
                        runtime_key=f"codex:{result.session_id}",
                        session_id=result.session_id,
                        provider="codex",
                        device_id="cinder",
                        source="codex_bridge",
                        kind="phase_signal",
                        phase="idle",
                        tool_name=None,
                        occurred_at=datetime.now(timezone.utc),
                        freshness_ms=60_000,
                        dedupe_key=f"test-launch-ready:{result.session_id}",
                        payload={"managed_transport": "codex_app_server", "thread_id": "thread-1"},
                    )
                ],
            )
            workspace = build_session_workspace(db=db, session_id=result.session_id, owner_id=OWNER_ID)
            launched = db.get(AgentSession, result.session_id)
            capabilities = project_session_capabilities(db, session_id=launched.id)
            assert capabilities.execution_home.value == "managed_local"
            assert capabilities.managed_transport.value == "codex_app_server"
            assert supports_live_text_dispatch_metadata(launched, db=db, owner_id=OWNER_ID) is True
    finally:
        asyncio.run(global_registry.clear_for_tests())

    assert workspace.session.launch_state == "live"
    assert workspace.session.capabilities.live_control_available is True
    assert workspace.session.capabilities.can_queue_next_input is True
    assert workspace.session.capabilities.can_steer_active_turn is True


def test_continue_session_dispatches_resume_payload_and_attaches_new_run(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.launch", "codex.continue"))

    with SessionLocal() as db:
        session_id = _seed_continuable_codex_session(db)
        thread = ensure_primary_thread(db, db.get(AgentSession, session_id))
        existing_run = record_run(db, thread=thread, provider="codex", host_id="cinder", cwd="/Users/me/repo")
        existing_connection = upsert_connection_for_run(
            db,
            run=existing_run,
            control_plane="codex_bridge",
            acquisition_kind="spawned_control",
            state="attached",
            external_name="cinder",
            can_send_input=0,
            can_interrupt=1,
            can_terminate=1,
            can_tail_output=1,
            can_resume=1,
        )
        degraded_run = record_run(db, thread=thread, provider="codex", host_id="cinder", cwd="/Users/me/repo")
        degraded_connection = upsert_connection_for_run(
            db,
            run=degraded_run,
            control_plane="codex_bridge",
            acquisition_kind="spawned_control",
            state="degraded",
            external_name="cinder",
            can_send_input=0,
            can_interrupt=1,
            can_terminate=1,
            can_tail_output=1,
            can_resume=1,
        )
        existing_run_id = existing_run.id
        existing_connection_id = existing_connection.id
        degraded_run_id = degraded_run.id
        degraded_connection_id = degraded_connection.id
        db.commit()

    with SessionLocal() as db:
        result = asyncio.run(
            continue_remote_session(
                db,
                RemoteContinueParams(
                    owner_id=OWNER_ID,
                    session_id=session_id,
                    client_request_id="continue-1",
                ),
                registry=registry,
            )
        )

    assert result.session_id == session_id
    assert result.launch_state == "live"
    assert len(registry.sent) == 1
    sent = registry.sent[0]
    assert sent["command_type"] == "session.launch"
    assert sent["session_id"] == str(session_id)
    assert sent["command_id"].startswith("continue-")
    assert sent["payload"]["mode"] == "continue"
    assert sent["payload"]["resume"] == {
        "thread_id": "thread-abc",
        "thread_path": "/Users/me/.codex/sessions/thread-abc.jsonl",
    }

    with SessionLocal() as db:
        session = db.get(AgentSession, session_id)
        assert session is not None
        assert session.ended_at is None
        assert db.query(AgentSession).count() == 1
        attempt = _latest_attempt(db, session_id)
        assert attempt.state == "adopted"
        assert attempt.run_id is not None
        assert attempt.run_id != existing_run_id
        assert attempt.run_id != degraded_run_id
        assert db.query(SessionRun).count() == 3
        assert db.get(SessionRun, attempt.run_id).launch_origin == "longhouse_continued"
        released_run = db.get(SessionRun, existing_run_id)
        assert released_run.ended_at is not None
        released_connection = db.get(SessionConnection, existing_connection_id)
        assert released_connection.state == "released"
        assert released_connection.can_send_input == 0
        assert released_connection.can_interrupt == 0
        assert released_connection.released_at is not None
        released_degraded_run = db.get(SessionRun, degraded_run_id)
        assert released_degraded_run.ended_at is not None
        released_degraded_connection = db.get(SessionConnection, degraded_connection_id)
        assert released_degraded_connection.state == "released"
        assert released_degraded_connection.can_interrupt == 0
        assert released_degraded_connection.released_at is not None
        live_connection = (
            db.query(SessionConnection)
            .join(SessionRun, SessionConnection.run_id == SessionRun.id)
            .filter(SessionRun.thread_id == attempt.thread_id)
            .filter(SessionConnection.state == "attached")
            .one()
        )
        assert live_connection.can_send_input == 1
        workspace = build_session_workspace(db=db, session_id=session_id, owner_id=OWNER_ID)
        assert workspace.session.capabilities.can_continue is True
        assert workspace.session.capabilities.continue_targets[0].carry_context == "native"


def test_continue_session_uses_hot_outbox_when_archive_writer_is_stalled(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.continue",))

    with SessionLocal() as db:
        session_id = _seed_continuable_codex_session(db)
        thread = ensure_primary_thread(db, db.get(AgentSession, session_id))
        existing_run = record_run(db, thread=thread, provider="codex", host_id="cinder", cwd="/Users/me/repo")
        existing_connection = upsert_connection_for_run(
            db,
            run=existing_run,
            control_plane="codex_bridge",
            acquisition_kind="spawned_control",
            state="attached",
            external_name="cinder",
            can_send_input=0,
            can_interrupt=1,
            can_terminate=1,
            can_tail_output=1,
            can_resume=1,
        )
        existing_run_id = existing_run.id
        existing_connection_id = existing_connection.id
        db.commit()

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/continue-live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)
    live_serializer = _LiveReadinessSerializer(LiveSession)
    monkeypatch.setattr(remote_launch_module.database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(remote_launch_module, "get_live_write_serializer", lambda: live_serializer)

    serializer = WriteSerializer()
    serializer.configure(SessionLocal)
    writer_entered = Event()
    release_writer = Event()

    def _block_writer(db):
        db.execute(text("SELECT 1"))
        writer_entered.set()
        assert release_writer.wait(5), "blocked archive writer was not released"

    async def _run_continue_while_blocked():
        blocker = asyncio.create_task(serializer.execute(_block_writer, label="ingest-replay"))
        try:
            assert await asyncio.to_thread(writer_entered.wait, 1)
            with SessionLocal() as db:
                result = await asyncio.wait_for(
                    continue_remote_session(
                        db,
                        RemoteContinueParams(
                            owner_id=OWNER_ID,
                            session_id=session_id,
                            client_request_id="continue-hot-stall",
                        ),
                        registry=registry,
                    ),
                    timeout=2,
                )
            assert result.session_id == session_id
            assert result.launch_state == "live"
            with SessionLocal() as db:
                assert db.query(SessionLaunchAttempt).filter(SessionLaunchAttempt.session_id == session_id).count() == 0
                session = db.get(AgentSession, session_id)
                assert session.device_id == "cinder"
                assert session.ended_at is not None
            with LiveSession() as live_db:
                readiness = live_db.get(LiveLaunchReadiness, str(session_id))
                assert readiness is not None
                assert readiness.state == "adopted"
                outbox = live_db.query(LiveArchiveOutbox).order_by(LiveArchiveOutbox.id.asc()).all()
                assert [row.kind for row in outbox] == ["remote_launch.v1", "remote_launch_outcome.v1"]
                assert all(row.drained_at is None for row in outbox)
        finally:
            release_writer.set()
            await asyncio.wait_for(blocker, timeout=2)

    try:
        asyncio.run(_run_continue_while_blocked())

        assert len(registry.sent) == 1
        sent = registry.sent[0]
        assert sent["command_type"] == "session.launch"
        assert sent["session_id"] == str(session_id)
        assert sent["payload"]["mode"] == "continue"
        assert sent["payload"]["resume"] == {
            "thread_id": "thread-abc",
            "thread_path": "/Users/me/.codex/sessions/thread-abc.jsonl",
        }

        with LiveSession() as live_db, SessionLocal() as archive_db:
            drained = drain_live_archive_outbox(live_db, archive_db)
            drain_rows = [
                (row.kind, row.last_error, row.payload_json)
                for row in live_db.query(LiveArchiveOutbox).order_by(LiveArchiveOutbox.id.asc()).all()
            ]
        assert drained.drained == 2, (drained.as_dict(), drain_rows)

        with SessionLocal() as db:
            attempt = _latest_attempt(db, session_id)
            assert attempt.state == "adopted"
            assert attempt.run_id is not None
            assert attempt.run_id != existing_run_id
            assert db.get(SessionRun, attempt.run_id).launch_origin == "longhouse_continued"
            released_run = db.get(SessionRun, existing_run_id)
            assert released_run.ended_at is not None
            released_connection = db.get(SessionConnection, existing_connection_id)
            assert released_connection.state == "released"
            live_connection = (
                db.query(SessionConnection)
                .join(SessionRun, SessionConnection.run_id == SessionRun.id)
                .filter(SessionRun.thread_id == attempt.thread_id)
                .filter(SessionConnection.state == "attached")
                .one()
            )
            assert live_connection.can_send_input == 1
    finally:
        release_writer.set()
        live_engine.dispose()


def test_one_shot_continue_hot_outbox_releases_existing_runs(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.resume_run_once",))

    with SessionLocal() as db:
        session_id = _seed_continuable_codex_session(db)
        thread = ensure_primary_thread(db, db.get(AgentSession, session_id))
        existing_run = record_run(db, thread=thread, provider="codex", host_id="cinder", cwd="/Users/me/repo")
        existing_connection = upsert_connection_for_run(
            db,
            run=existing_run,
            control_plane="codex_bridge",
            acquisition_kind="spawned_control",
            state="attached",
            external_name="cinder",
            can_send_input=0,
            can_interrupt=1,
            can_terminate=1,
            can_tail_output=1,
            can_resume=1,
        )
        existing_run_id = existing_run.id
        existing_connection_id = existing_connection.id
        db.commit()

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/one-shot-continue-live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)
    live_serializer = _LiveReadinessSerializer(LiveSession)
    monkeypatch.setattr(remote_launch_module.database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(remote_launch_module, "get_live_write_serializer", lambda: live_serializer)

    try:
        with SessionLocal() as db:
            result = asyncio.run(
                continue_remote_session(
                    db,
                    RemoteContinueParams(
                        owner_id=OWNER_ID,
                        session_id=session_id,
                        client_request_id="continue-hot-one-shot",
                        message="Please continue with one bounded step.",
                        execution_lifetime="one_shot",
                    ),
                    registry=registry,
                )
            )

        assert result.session_id == session_id
        assert result.launch_state == "live"
        assert result.execution_lifetime == "one_shot"
        assert len(registry.sent) == 1
        sent = registry.sent[0]
        assert sent["command_type"] == "session.run_once"
        assert sent["payload"]["mode"] == "continue"
        assert sent["payload"]["execution_lifetime"] == "one_shot"
        assert sent["payload"]["initial_prompt"] == "Please continue with one bounded step."
        assert sent["payload"]["run_id"]

        with LiveSession() as live_db, SessionLocal() as archive_db:
            drained = drain_live_archive_outbox(live_db, archive_db)
        assert drained.drained == 2

        with SessionLocal() as db:
            attempt = _latest_attempt(db, session_id)
            assert attempt.execution_lifetime == "one_shot"
            assert attempt.state == "adopted"
            assert attempt.run_id is not None
            new_run = db.get(SessionRun, attempt.run_id)
            assert new_run is not None
            assert new_run.launch_origin == "longhouse_continued"
            assert new_run.id != existing_run_id
            released_run = db.get(SessionRun, existing_run_id)
            assert released_run.ended_at is not None
            released_connection = db.get(SessionConnection, existing_connection_id)
            assert released_connection.state == "released"
            one_shot_connection = (
                db.query(SessionConnection)
                .filter(SessionConnection.run_id == new_run.id)
                .one()
            )
            assert one_shot_connection.control_plane == "codex_exec"
            assert one_shot_connection.can_send_input == 0
            assert one_shot_connection.can_resume == 0
    finally:
        live_engine.dispose()


def test_hot_continue_timeout_late_result_reconciles_through_live_outbox(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)

    class TimeoutRegistry(_StubRegistry):
        async def send_command(self, **kwargs):  # type: ignore[override]
            self.sent.append(kwargs)
            return MachineControlCommandResponse(transport_ok=False, error="timed out")

    registry = TimeoutRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.launch", "codex.continue"))

    with SessionLocal() as db:
        session_id = _seed_continuable_codex_session(db)

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/continue-late-live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)
    live_serializer = _LiveReadinessSerializer(LiveSession)
    monkeypatch.setattr(remote_launch_module.database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(remote_launch_module.database_module, "get_live_write_session_factory", lambda: LiveSession)
    monkeypatch.setattr(remote_launch_module, "get_live_write_serializer", lambda: live_serializer)

    try:
        with SessionLocal() as db:
            result = asyncio.run(
                continue_remote_session(
                    db,
                    RemoteContinueParams(
                        owner_id=OWNER_ID,
                        session_id=session_id,
                        client_request_id="continue-hot-timeout",
                    ),
                    registry=registry,
                )
            )

        assert result.launch_state == "launching_unknown"
        command_id = registry.sent[-1]["command_id"]
        assert command_id.startswith("continue-")
        with SessionLocal() as db:
            assert db.query(SessionLaunchAttempt).count() == 0
            reconciled = reconcile_launch_from_command_result(
                db,
                {
                    "type": "command_result",
                    "command_id": command_id,
                    "ok": True,
                    "result": {"session_id": str(session_id), "thread_id": "thread-abc"},
                },
            )
            assert reconciled is True

        with LiveSession() as live_db:
            readiness = live_db.get(LiveLaunchReadiness, str(session_id))
            assert readiness.state == "adopted"
            with SessionLocal() as archive_db:
                drained = drain_live_archive_outbox(live_db, archive_db)
            assert drained.drained == 3
        with SessionLocal() as db:
            attempt = _latest_attempt(db, session_id)
            assert attempt.state == "adopted"
            assert attempt.command_id == command_id
            assert db.query(SessionConnection).count() == 1
    finally:
        live_engine.dispose()


def test_continue_claude_session_resumes_by_id_with_null_thread_path(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(
        registry,
        owner_id=OWNER_ID,
        device_id="cinder",
        supports=("claude.launch", "claude.continue"),
    )

    with SessionLocal() as db:
        session_id = _seed_continuable_claude_session(db)
        provider_id = f"claude-provider-{session_id}"

    with SessionLocal() as db:
        result = asyncio.run(
            continue_remote_session(
                db,
                RemoteContinueParams(
                    owner_id=OWNER_ID,
                    session_id=session_id,
                    client_request_id="claude-continue-1",
                ),
                registry=registry,
            )
        )

    assert result.session_id == session_id
    assert result.launch_state == "live"
    assert len(registry.sent) == 1
    sent = registry.sent[0]
    assert sent["command_type"] == "session.launch"
    assert sent["payload"]["provider"] == "claude"
    assert sent["payload"]["mode"] == "continue"
    # Claude resumes by provider id; there is no transcript path.
    assert sent["payload"]["resume"] == {
        "thread_id": provider_id,
        "thread_path": None,
    }

    with SessionLocal() as db:
        session = db.get(AgentSession, session_id)
        assert session.ended_at is None
        assert db.query(AgentSession).count() == 1
        attempt = _latest_attempt(db, session_id)
        assert attempt.state == "adopted"
        assert attempt.run_id is not None
        assert db.get(SessionRun, attempt.run_id).launch_origin == "longhouse_continued"
        workspace = build_session_workspace(db=db, session_id=session_id, owner_id=OWNER_ID)
        assert workspace.session.capabilities.can_continue is True
        assert workspace.session.capabilities.continue_targets[0].carry_context == "native"


def test_unmanaged_claude_with_alias_and_transcript_is_adoptable(tmp_path):
    """An imported/raw claude session with a provider_session_id alias AND a
    local transcript is continuable as adopt_unmanaged — the user can explicitly
    bring it under management. The resume id is the provider's OWN id (alias),
    not the longhouse id."""

    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    provider_id = str(uuid4())

    with SessionLocal() as db:
        sid = _seed_imported_claude_session(
            db,
            provider_session_alias=provider_id,
            source_path="/Users/me/.claude/projects/-x/raw.jsonl",
        )
        workspace = build_session_workspace(db=db, session_id=sid, owner_id=OWNER_ID)
        caps = workspace.session.capabilities
        assert caps.can_continue is True
        assert caps.continue_targets[0].adoption_mode == "adopt_unmanaged"


def test_live_unmanaged_claude_is_not_adoptable(tmp_path):
    """A still-LIVE raw claude session (ended_at is None) must NOT be adoptable.

    Launching a fresh managed resume of a transcript a live process is still
    writing = two owners contending for one transcript. The closed-state gate
    prevents that. Once it closes, it becomes adoptable."""

    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)

    with SessionLocal() as db:
        live_id = _seed_imported_claude_session(
            db,
            provider_session_alias=str(uuid4()),
            source_path="/Users/me/.claude/projects/-x/raw.jsonl",
            ended=False,
        )
        workspace = build_session_workspace(db=db, session_id=live_id, owner_id=OWNER_ID)
        assert workspace.session.capabilities.can_continue is False
        assert workspace.session.capabilities.continue_targets == []


def test_unmanaged_claude_not_continuable_without_alias_or_transcript(tmp_path):
    """adopt_unmanaged requires BOTH a provider_session_id alias and transcript
    evidence. Missing either → no Continue (we'd have nothing to resume)."""

    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)

    with SessionLocal() as db:
        # alias but NO transcript
        alias_only = _seed_imported_claude_session(db, provider_session_alias=str(uuid4()))
        workspace = build_session_workspace(db=db, session_id=alias_only, owner_id=OWNER_ID)
        assert workspace.session.capabilities.can_continue is False
        assert workspace.session.capabilities.continue_targets == []

    with SessionLocal() as db:
        # transcript but NO alias
        path_only = _seed_imported_claude_session(
            db, provider_session_alias=None, source_path="/Users/me/.claude/projects/-x/raw.jsonl"
        )
        workspace = build_session_workspace(db=db, session_id=path_only, owner_id=OWNER_ID)
        assert workspace.session.capabilities.can_continue is False
        assert workspace.session.capabilities.continue_targets == []


def test_continue_unmanaged_claude_resumes_by_provider_alias(tmp_path):
    """Executing continue on an adoptable unmanaged claude session dispatches
    resume.thread_id = the PROVIDER alias id (not session.id)."""

    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("claude.continue",))
    provider_id = str(uuid4())

    with SessionLocal() as db:
        session_id = _seed_imported_claude_session(
            db,
            provider_session_alias=provider_id,
            source_path="/Users/me/.claude/projects/-x/raw.jsonl",
        )

    with SessionLocal() as db:
        result = asyncio.run(
            continue_remote_session(
                db,
                RemoteContinueParams(
                    owner_id=OWNER_ID,
                    session_id=session_id,
                    client_request_id="adopt-unmanaged",
                ),
                registry=registry,
            )
        )

    assert result.launch_state == "live"
    sent = registry.sent[0]
    assert sent["payload"]["mode"] == "continue"
    # Resume id is the provider's own id, NOT the longhouse session id.
    assert sent["payload"]["resume"]["thread_id"] == provider_id
    assert sent["payload"]["resume"]["thread_id"] != str(session_id)


def test_continue_rejects_unmanaged_claude_without_alias_or_transcript(tmp_path):
    """Execution gate rejects claude sessions with no resolvable resume identity
    (no alias, or alias without transcript evidence)."""

    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("claude.continue",))

    with SessionLocal() as db:
        no_alias_id = _seed_imported_claude_session(db, provider_session_alias=None)
        with pytest.raises(RemoteLaunchError) as excinfo:
            asyncio.run(
                continue_remote_session(
                    db,
                    RemoteContinueParams(
                        owner_id=OWNER_ID,
                        session_id=no_alias_id,
                        client_request_id="unmanaged-no-alias",
                    ),
                    registry=registry,
                )
            )
        assert excinfo.value.code == "invalid_request"
        assert excinfo.value.status_code == 409

    with SessionLocal() as db:
        spoof_id = uuid4()
        _seed_imported_claude_session(
            db,
            session_id=spoof_id,
            provider_session_alias=str(spoof_id),
            observe_only_connection=True,
        )
        with pytest.raises(RemoteLaunchError) as excinfo:
            asyncio.run(
                continue_remote_session(
                    db,
                    RemoteContinueParams(
                        owner_id=OWNER_ID,
                        session_id=spoof_id,
                        client_request_id="unmanaged-spoof",
                    ),
                    registry=registry,
                )
            )
        assert excinfo.value.code == "invalid_request"
        assert excinfo.value.status_code == 409

    assert registry.sent == []


def test_adopted_control_claude_session_is_continuable(tmp_path):
    """A claude session Longhouse ADOPTED (adopted_control) is managed and
    continuable — the connection proves ownership and the provider alias
    supplies the native resume id."""

    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)

    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        sid = uuid4()
        session = AgentSession(
            id=sid,
            provider="claude",
            environment="development",
            project="repo",
            device_id="cinder",
            device_name="cinder",
            cwd="/Users/me/repo",
            started_at=now,
            ended_at=now,
            last_activity_at=now,
                                                user_messages=1,
            assistant_messages=1,
            tool_calls=0,
                                )
        db.add(session)
        db.flush()
        thread = ensure_primary_thread(db, session)
        provider_id = f"claude-provider-{sid}"
        record_thread_alias(
            db, thread=thread, provider="claude", alias_kind="provider_session_id", alias_value=provider_id
        )
        run = record_run(db, thread=thread, provider="claude", host_id="cinder", cwd="/Users/me/repo")
        upsert_connection_for_run(
            db,
            run=run,
            control_plane="claude_channel_bridge",
            acquisition_kind="adopted_control",
            state="released",
            external_name="cinder",
            can_send_input=0,
            can_interrupt=0,
            can_terminate=0,
            can_tail_output=0,
            can_resume=1,
        )
        db.commit()
        workspace = build_session_workspace(db=db, session_id=sid, owner_id=OWNER_ID)
        assert workspace.session.capabilities.can_continue is True
        assert workspace.session.capabilities.continue_targets[0].carry_context == "native"


def test_continue_claude_capability_required(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    # Machine online but only advertises claude.launch, not claude.continue.
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("claude.launch",))

    with SessionLocal() as db:
        session_id = _seed_continuable_claude_session(db)
        with pytest.raises(RemoteLaunchError) as excinfo:
            asyncio.run(
                continue_remote_session(
                    db,
                    RemoteContinueParams(
                        owner_id=OWNER_ID,
                        session_id=session_id,
                        client_request_id="claude-continue-cap",
                    ),
                    registry=registry,
                )
            )

    assert excinfo.value.code == "provider_unsupported"
    assert excinfo.value.status_code == 409
    assert registry.sent == []


def test_late_continue_claude_reconciliation_attaches_new_run(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)

    class _TimeoutRegistry(_StubRegistry):
        async def send_command(self, **kwargs):
            self.sent.append(kwargs)
            return MachineControlCommandResponse(transport_ok=False, error="timed out")

    registry = _TimeoutRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("claude.continue",))
    with SessionLocal() as db:
        session_id = _seed_continuable_claude_session(db)
        provider_id = f"claude-provider-{session_id}"
        result = asyncio.run(
            continue_remote_session(
                db,
                RemoteContinueParams(
                    owner_id=OWNER_ID,
                    session_id=session_id,
                    client_request_id="claude-continue-timeout",
                ),
                registry=registry,
            )
        )
    assert result.launch_state == "launching_unknown"
    command_id = registry.sent[-1]["command_id"]
    assert command_id.startswith("continue-")

    # Late success: the engine echoes the claude provider id.
    with SessionLocal() as db:
        reconciled = reconcile_launch_from_command_result(
            db,
            {
                "type": "command_result",
                "command_id": command_id,
                "ok": True,
                "result": {
                    "session_id": str(session_id),
                    "thread_id": provider_id,
                },
            },
        )

    assert reconciled is True
    with SessionLocal() as db:
        session = db.get(AgentSession, session_id)
        attempt = _latest_attempt(db, session_id)
        assert session.ended_at is None
        assert attempt.state == "adopted"
        assert attempt.run_id is not None
        assert db.query(AgentSession).count() == 1
        # Original managed run/connection (now released) + the new continued run.
        assert db.query(SessionRun).count() == 2
        assert db.query(SessionConnection).count() == 2
        new_connection = (
            db.query(SessionConnection)
            .filter(SessionConnection.run_id == attempt.run_id)
            .one()
        )
        assert new_connection.state == "attached"


def test_continue_session_prefers_thread_source_path_over_session_fallback(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.continue",))

    with SessionLocal() as db:
        session_id = _seed_continuable_codex_session(
            db,
            thread_path="/Users/me/.codex/sessions/thread-abc.jsonl",
            session_path="/Users/me/.codex/sessions/session-fallback.jsonl",
        )

    with SessionLocal() as db:
        result = asyncio.run(
            continue_remote_session(
                db,
                RemoteContinueParams(
                    owner_id=OWNER_ID,
                    session_id=session_id,
                    client_request_id="continue-thread-path",
                ),
                registry=registry,
            )
        )

    assert result.launch_state == "live"
    assert registry.sent[0]["payload"]["resume"] == {
        "thread_id": "thread-abc",
        "thread_path": "/Users/me/.codex/sessions/thread-abc.jsonl",
    }


def test_continue_session_uses_session_bounded_source_path_without_thread_path(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.continue",))

    with SessionLocal() as db:
        session_id = _seed_continuable_codex_session(
            db,
            thread_path=None,
            session_path="/Users/me/.codex/sessions/session-fallback.jsonl",
        )

    with SessionLocal() as db:
        result = asyncio.run(
            continue_remote_session(
                db,
                RemoteContinueParams(
                    owner_id=OWNER_ID,
                    session_id=session_id,
                    client_request_id="continue-session-path",
                ),
                registry=registry,
            )
        )

    assert result.launch_state == "live"
    assert registry.sent[0]["payload"]["resume"] == {
        "thread_id": "thread-abc",
        "thread_path": "/Users/me/.codex/sessions/session-fallback.jsonl",
    }


def test_continue_session_is_idempotent_by_client_request_id(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.continue",))

    with SessionLocal() as db:
        session_id = _seed_continuable_codex_session(db)

    params = RemoteContinueParams(owner_id=OWNER_ID, session_id=session_id, client_request_id="continue-same")
    with SessionLocal() as db:
        first = asyncio.run(continue_remote_session(db, params, registry=registry))
    with SessionLocal() as db:
        second = asyncio.run(continue_remote_session(db, params, registry=registry))

    assert first.session_id == second.session_id
    assert len(registry.sent) == 1


def test_continue_requires_client_request_id(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.continue",))

    with SessionLocal() as db:
        session_id = _seed_continuable_codex_session(db)
        with pytest.raises(RemoteLaunchError) as excinfo:
            asyncio.run(
                continue_remote_session(
                    db,
                    RemoteContinueParams(owner_id=OWNER_ID, session_id=session_id, client_request_id=""),
                    registry=registry,
                )
            )

    assert excinfo.value.code == "invalid_request"
    assert excinfo.value.status_code == 400
    assert registry.sent == []


def test_continue_requires_source_session_device_owned_by_user(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal, owner_id=OWNER_ID, device_id="cinder")
    _seed_user_and_device(SessionLocal, owner_id=OWNER_ID + 1, device_id="not-mine")
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.continue",))

    with SessionLocal() as db:
        session_id = _seed_continuable_codex_session(db, device_id="not-mine")
        with pytest.raises(RemoteLaunchError) as excinfo:
            asyncio.run(
                continue_remote_session(
                    db,
                    RemoteContinueParams(
                        owner_id=OWNER_ID,
                        session_id=session_id,
                        device_id="cinder",
                        client_request_id="continue-owned-source",
                    ),
                    registry=registry,
                )
            )

    assert excinfo.value.code == "device_not_enrolled"
    assert excinfo.value.status_code == 404
    assert registry.sent == []


def test_continue_rejects_session_without_recorded_source_host(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal, owner_id=OWNER_ID, device_id="cinder")
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.continue",))

    with SessionLocal() as db:
        session_id = _seed_continuable_codex_session(db, device_id=None)
        with pytest.raises(RemoteLaunchError) as excinfo:
            asyncio.run(
                continue_remote_session(
                    db,
                    RemoteContinueParams(
                        owner_id=OWNER_ID,
                        session_id=session_id,
                        device_id="cinder",
                        client_request_id="continue-null-source-host",
                    ),
                    registry=registry,
                )
            )

    assert excinfo.value.code == "invalid_request"
    assert excinfo.value.status_code == 409
    assert registry.sent == []


def test_continue_requires_continue_capability(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.launch",))

    with SessionLocal() as db:
        session_id = _seed_continuable_codex_session(db)
        with pytest.raises(RemoteLaunchError) as excinfo:
            asyncio.run(
                continue_remote_session(
                    db,
                    RemoteContinueParams(
                        owner_id=OWNER_ID,
                        session_id=session_id,
                        client_request_id="continue-capability",
                    ),
                    registry=registry,
                )
            )

    assert excinfo.value.code == "provider_unsupported"
    assert excinfo.value.status_code == 409
    assert registry.sent == []


def test_continue_rejects_missing_resume_identity(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.continue",))

    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        sid = uuid4()
        db.add(
            AgentSession(
                id=sid,
                provider="codex",
                environment="development",
                project="repo",
                device_id="cinder",
                cwd="/Users/me/repo",
                started_at=now,
                ended_at=now,
                                                                                user_messages=0,
                assistant_messages=0,
                tool_calls=0,
                                            )
        )
        db.commit()
        with pytest.raises(RemoteLaunchError) as excinfo:
            asyncio.run(
                continue_remote_session(
                    db,
                    RemoteContinueParams(
                        owner_id=OWNER_ID,
                        session_id=sid,
                        client_request_id="continue-missing-identity",
                    ),
                    registry=registry,
                )
            )

    assert excinfo.value.code == "invalid_request"
    assert excinfo.value.status_code == 409
    assert registry.sent == []


def test_continue_rejects_legacy_session_id_as_provider_thread_id(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.continue",))

    with SessionLocal() as db:
        sid = uuid4()
        session_id = _seed_continuable_codex_session(db, session_id=sid, provider_thread_id=str(sid))
        with pytest.raises(RemoteLaunchError) as excinfo:
            asyncio.run(
                continue_remote_session(
                    db,
                    RemoteContinueParams(
                        owner_id=OWNER_ID,
                        session_id=session_id,
                        client_request_id="continue-legacy-thread-id",
                    ),
                    registry=registry,
                )
            )

    assert excinfo.value.code == "invalid_request"
    assert excinfo.value.status_code == 409
    assert registry.sent == []


def test_late_result_reconciliation_moves_unknown_to_live(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)

    class _TimeoutRegistry(_StubRegistry):
        async def send_command(self, **kwargs):
            self.sent.append(kwargs)
            return MachineControlCommandResponse(transport_ok=False, error="timed out")

    registry = _TimeoutRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder")

    with SessionLocal() as db:
        result = asyncio.run(
            launch_remote_session(
                db,
                RemoteLaunchParams(
                    owner_id=OWNER_ID,
                    device_id="cinder",
                    provider="codex",
                    cwd="/Users/me/repo",
                ),
                registry=registry,
            )
        )
    assert result.launch_state == "launching_unknown"

    command_id = registry.sent[-1]["command_id"]
    # Simulate late success
    with SessionLocal() as db:
        reconciled = reconcile_launch_from_command_result(
            db,
            {
                "type": "command_result",
                "command_id": command_id,
                "ok": True,
                "result": {"session_id": str(result.session_id)},
            },
        )
    assert reconciled is True
    with SessionLocal() as db:
        attempt = _latest_attempt(db, result.session_id)
        assert attempt.state == "adopted"
        assert attempt.run_id is not None
        assert attempt.expires_at is None
        assert db.query(SessionRun).count() == 1
        assert db.query(SessionConnection).count() == 1

    with SessionLocal() as db:
        duplicate = reconcile_launch_from_command_result(
            db,
            {
                "type": "command_result",
                "command_id": command_id,
                "ok": True,
                "result": {"session_id": str(result.session_id)},
            },
        )
    assert duplicate is True
    with SessionLocal() as db:
        assert db.query(SessionRun).count() == 1
        assert db.query(SessionConnection).count() == 1


def test_late_continue_result_reconciliation_keeps_existing_session_live(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)

    class _TimeoutRegistry(_StubRegistry):
        async def send_command(self, **kwargs):
            self.sent.append(kwargs)
            return MachineControlCommandResponse(transport_ok=False, error="timed out")

    registry = _TimeoutRegistry()
    _register_online(registry, owner_id=OWNER_ID, device_id="cinder", supports=("codex.continue",))
    with SessionLocal() as db:
        session_id = _seed_continuable_codex_session(db)
        result = asyncio.run(
            continue_remote_session(
                db,
                RemoteContinueParams(
                    owner_id=OWNER_ID,
                    session_id=session_id,
                    client_request_id="continue-timeout",
                ),
                registry=registry,
            )
        )
    assert result.launch_state == "launching_unknown"
    command_id = registry.sent[-1]["command_id"]
    assert command_id.startswith("continue-")
    with SessionLocal() as db:
        assert (
            db.query(SessionThreadAlias)
            .filter(SessionThreadAlias.alias_kind == "source_path")
            .filter(SessionThreadAlias.alias_value == "/Users/me/.codex/sessions/thread-abc.jsonl")
            .count()
        ) == 1

    with SessionLocal() as db:
        reconciled = reconcile_launch_from_command_result(
            db,
            {
                "type": "command_result",
                "command_id": command_id,
                "ok": True,
                "result": {
                    "session_id": str(session_id),
                    "thread_id": "thread-abc",
                    "thread_path": "/Users/me/.codex/sessions/thread-abc.jsonl",
                },
            },
        )

    assert reconciled is True
    with SessionLocal() as db:
        session = db.get(AgentSession, session_id)
        attempt = _latest_attempt(db, session_id)
        assert session.ended_at is None
        assert attempt.state == "adopted"
        assert attempt.run_id is not None
        assert db.query(AgentSession).count() == 1
        assert db.query(SessionRun).count() == 1
        assert db.query(SessionConnection).count() == 1


def test_late_result_reconciliation_ignores_unknown_command(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    with SessionLocal() as db:
        reconciled = reconcile_launch_from_command_result(
            db,
            {
                "type": "command_result",
                "command_id": "launch-00000000-0000-0000-0000-000000000000",
                "ok": True,
            },
        )
    assert reconciled is False


def test_reap_orphaned_launches_expires_stale_rows(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)

    past = datetime.now(timezone.utc).replace(tzinfo=timezone.utc)
    with SessionLocal() as db:
        sid = uuid4()
        session = AgentSession(
            id=sid,
            provider="codex",
            environment="development",
            project="repo",
            device_id="cinder",
            cwd="/Users/me/repo",
            started_at=past,
                                                            user_messages=0,
            assistant_messages=0,
            tool_calls=0,
                                )
        db.add(session)
        db.flush()
        db.add(
            SessionLaunchAttempt(
                session_id=sid,
                provider="codex",
                host_id="cinder",
                command_id=f"launch-{sid}",
                state="dispatched",
                expires_at=past.replace(year=past.year - 1),  # way in the past
            )
        )
        db.commit()
        reaped = reap_orphaned_launches(db)
    assert reaped == 1
    with SessionLocal() as db:
        row = db.query(AgentSession).first()
        attempt = _latest_attempt(db, row.id)
        assert attempt.state == "abandoned"
        assert attempt.expires_at is None
        assert attempt.error_code == "launch_timeout"
        assert row.ended_at is not None


def _make_admin_client(SessionLocal, *, owner_id: int = OWNER_ID):
    from zerg.dependencies.auth import get_current_user
    from zerg.dependencies.auth import require_admin
    from zerg.main import api_app
    from zerg.main import app

    def override_get_db():
        with SessionLocal() as db:
            yield db

    def override_user():
        return SimpleNamespace(id=owner_id, email="admin@example.com", role="ADMIN")

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[get_current_user] = override_user
    api_app.dependency_overrides[require_admin] = override_user
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    return TestClient(app, backend="asyncio"), api_app


def test_admin_launch_debug_lists_non_live_rows(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)

    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        for launch_state, attempt_state in (
            ("launching_unknown", "dispatched"),
            ("launch_failed", "failed"),
            ("launch_orphaned", "abandoned"),
            ("live", "dispatched"),
        ):
            sid = uuid4()
            session = AgentSession(
                id=sid,
                provider="codex",
                environment="development",
                project="repo",
                device_id="cinder",
                cwd="/Users/me/repo",
                started_at=now,
                                                                                user_messages=0,
                assistant_messages=0,
                tool_calls=0,
                                            )
            db.add(session)
            db.flush()
            thread = ensure_primary_thread(db, session)
            run = None
            if launch_state == "live":
                run = record_run(db, thread=thread, provider="codex", host_id="cinder", cwd="/Users/me/repo")
            db.add(
                SessionLaunchAttempt(
                    session_id=sid,
                    thread_id=thread.id,
                    run_id=run.id if run is not None else None,
                    provider="codex",
                    host_id="cinder",
                    state=attempt_state,
                    error_code="boom" if attempt_state in {"failed", "abandoned"} else None,
                    error_message="boom" if attempt_state in {"failed", "abandoned"} else None,
                )
            )
        db.commit()
        test_sid = uuid4()
        db.add(
            AgentSession(
                id=test_sid,
                provider="codex",
                environment="test",
                project="probe",
                device_id="cinder",
                cwd="/Users/me/repo",
                started_at=now,
                                                                                user_messages=0,
                assistant_messages=0,
                tool_calls=0,
                                            )
        )
        db.flush()
        db.add(
            SessionLaunchAttempt(
                session_id=test_sid,
                provider="codex",
                host_id="cinder",
                state="failed",
                error_code="probe",
            )
        )
        db.commit()

    client, api_app = _make_admin_client(SessionLocal)
    try:
        resp = client.get("/api/admin/launches/debug")
    finally:
        api_app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    body = resp.json()
    states = sorted(e["launch_state"] for e in body["entries"])
    assert states == ["launch_failed", "launch_orphaned", "launching_unknown"]
    assert all(e["launch_error_code"] != "probe" for e in body["entries"])

    client, api_app = _make_admin_client(SessionLocal)
    try:
        resp_all = client.get("/api/admin/launches/debug?include_live=true")
    finally:
        api_app.dependency_overrides.clear()
    assert resp_all.status_code == 200
    assert len(resp_all.json()["entries"]) == 4

    client, api_app = _make_admin_client(SessionLocal)
    try:
        resp_with_test = client.get("/api/admin/launches/debug?include_test=true")
    finally:
        api_app.dependency_overrides.clear()
    assert resp_with_test.status_code == 200
    assert any(e["launch_error_code"] == "probe" for e in resp_with_test.json()["entries"])


def test_http_endpoint_offline_machine_is_409(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user_and_device(SessionLocal)
    registry = _StubRegistry()

    original, module = _patch_registry(registry)
    try:
        client, api_app = _make_browser_client(SessionLocal)
        try:
            resp = client.post(
                "/api/sessions/launch",
                json={
                    "device_id": "cinder",
                    "provider": "codex",
                    "cwd": "/Users/me/repo",
                    "initial_prompt": "Check whether this machine is online",
                },
            )
        finally:
            api_app.dependency_overrides.clear()
    finally:
        module.get_machine_control_channel_registry = original

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "machine_offline"
