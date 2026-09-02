from __future__ import annotations

import asyncio
import hashlib
import json
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
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-1234")
os.environ.setdefault("INTERNAL_API_SECRET", "test-internal-secret-1234")
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-google-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-google-client-secret")

import pytest

from tests_lite._kernel_test_helpers import seed_managed_kernel_rows
from tests_lite.agents_fixture import SessionFixtureStore
from tests_lite.live_catalog_harness import LiveCatalog
from tests_lite.live_catalog_harness import live_catalog  # noqa: F401
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: F401
from zerg.database import get_db
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.browser_route_auth import get_current_browser_route_user
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionInput
from zerg.models.agents import SessionInputAttachment
from zerg.models.agents import SessionInputDeliveryAttempt
from zerg.models.agents import SessionTurn
from zerg.models.enums import UserRole
from zerg.models.models import Runner
from zerg.models.user import User
from zerg.routers.session_chat import _live_queued_summary
from zerg.routers.session_chat import _project_live_input_to_archive
from zerg.services.agents import EventIngest
from zerg.services.agents import SessionIngest
from zerg.services.live_session_inputs import LiveInputReceiptSnapshot
from zerg.services.machine_control_channel import get_machine_control_channel_registry
from zerg.services.runner_connection_manager import get_runner_connection_manager
from zerg.services.session_inputs import INPUT_STATUS_CANCELLED
from zerg.services.session_inputs import INPUT_STATUS_DELIVERED
from zerg.services.session_inputs import INPUT_STATUS_DELIVERING
from zerg.services.session_inputs import INPUT_STATUS_FAILED
from zerg.services.session_inputs import INPUT_STATUS_QUEUED
from zerg.services.session_inputs import create_session_input
from zerg.services.session_locks import session_lock_manager
from zerg.services.session_runtime import phase_freshness_ms
from zerg.services.session_runtime import runtime_key_for_session


@pytest.fixture(autouse=True)
def _restore_api_app_dependency_overrides():
    """An override installed here must not outlive this test.

    ``api_app`` is a process-global, so an override left behind keeps
    answering for every later test in the run. This file used to leave
    ``verify_agents_token`` returning device ``usage-stats``, and an unrelated
    storage-v2 test several hundred tests later failed with
    ``identity_mismatch``. Nothing catches that until an edit elsewhere
    reorders the suite, so each test puts back what it found.
    """

    from zerg.main import api_app

    saved = dict(api_app.dependency_overrides)
    try:
        yield
    finally:
        api_app.dependency_overrides.clear()
        api_app.dependency_overrides.update(saved)


def _make_db(tmp_path):
    db_path = tmp_path / "test_session_inputs.db"
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)
    return make_sessionmaker(engine)


# ---------------------------------------------------------------------------
# The live catalog: the input path a Runtime Host actually takes
# ---------------------------------------------------------------------------
#
# ``/sessions/{id}/input`` has one implementation. A Runtime Host resolves the
# session, the control grant and every input receipt through catalogd, and the
# archive ``session_inputs`` table is a projection of what the catalog already
# decided -- never the thing a route reads to answer. Tests that reach the
# route therefore declare the ``live_catalog`` fixtures and run against real
# catalogd and searchd daemons. Tests that reach a service directly, like the
# archive queue drainer below, still build their own SQLite database.

LIVE_CATALOG_DEVICE_ID = "cinder"
LIVE_CATALOG_PROVIDER = "claude"


def _machine_heartbeat(*, device_id: str, now: datetime, raw_json: str | None = None) -> dict:
    """The heartbeat stamp the Machine Agent ships on every tick."""

    return {
        "device_id": device_id,
        "received_at": now.isoformat(),
        "version": "test-engine",
        "last_ship_at": now.isoformat(),
        "last_ship_attempt_at": now.isoformat(),
        "last_ship_result": "ok",
        "last_ship_latency_ms": 5,
        "last_ship_http_status": 200,
        "spool_pending": 0,
        "spool_dead": 0,
        "parse_errors_1h": 0,
        "consecutive_failures": 0,
        "ship_attempts_1h": 1,
        "ship_successes_1h": 1,
        "ship_rate_limited_1h": 0,
        "ship_server_errors_1h": 0,
        "ship_payload_rejections_1h": 0,
        "ship_payload_too_large_1h": 0,
        "ship_retryable_client_errors_1h": 0,
        "ship_connect_errors_1h": 0,
        "ship_latency_p50_ms_1h": 5,
        "ship_latency_p95_ms_1h": 5,
        "disk_free_bytes": 1_000_000,
        "is_offline": 0,
        "raw_json": raw_json,
        "sessions_digest": None,
        "sessions_sequence": None,
    }


def _machine_evidence_json(*, provider: str, session_id: str, run_id: str, now: datetime) -> str:
    """The typed facts the provider adapter reports through the heartbeat.

    The control fact binds an adapter connection identity to the catalog
    connection; without it ``control.command.prepare.v2`` refuses every command
    with ``identity_unbound``, so a session can be attached and still
    uncontrollable. The activity fact is what makes the session quiescent, and
    a queue drain will not claim a receipt for a session that is mid-turn.
    """

    from zerg.machine_evidence import canonical_evidence_hash

    connection_id = str(uuid4())
    lease_generation = str(uuid4())
    activity = {
        "authority_class": "provider_runtime",
        "provider": provider,
        "session_id": session_id,
        "run_id": run_id,
        "kind": "idle",
        "raw_kind": "idle",
        "tool_name": None,
        "source": "provider_runtime",
        "observed_at": now.isoformat(),
        "valid_until": (now + timedelta(minutes=5)).isoformat(),
    }
    control = {
        "authority_class": "provider_control",
        "provider": provider,
        "session_id": session_id,
        "run_id": run_id,
        "connection_id": connection_id,
        "lease_generation": lease_generation,
        "granted_operations": ["interrupt", "send_input"],
        "ownership": "managed",
        "state": "attached",
        "lease_ttl_ms": 300_000,
        "source": "provider_control",
        "observed_at": now.isoformat(),
    }
    return json.dumps(
        {
            "machine_evidence": {
                "schema_version": 3,
                "activity": [activity],
                "control": [control],
                "identities": [
                    {
                        "fact_family": "activity",
                        "fact_index": 0,
                        "subject_key": f"run:{run_id}",
                        "source": "provider_runtime",
                        "source_epoch": run_id,
                        "source_seq": 1,
                        "sequenced": True,
                        "dedupe_key": hashlib.sha256(f"{run_id}:activity:1".encode()).hexdigest(),
                        "evidence_hash": canonical_evidence_hash(activity),
                    },
                    {
                        "fact_family": "control",
                        "fact_index": 0,
                        "subject_key": f"connection:{connection_id}:{lease_generation}",
                        "source": "provider_control",
                        "source_epoch": lease_generation,
                        "source_seq": None,
                        "sequenced": False,
                        "dedupe_key": hashlib.sha256(f"{connection_id}:{lease_generation}".encode()).hexdigest(),
                        "evidence_hash": canonical_evidence_hash(control),
                    },
                ],
            }
        }
    )


def _seed_live_catalog_session(
    live: LiveCatalog,
    *,
    owner_id: int,
    provider: str = LIVE_CATALOG_PROVIDER,
    device_id: str = LIVE_CATALOG_DEVICE_ID,
) -> str:
    """Launch one Helm session in the live catalog and bring its control online.

    The production sequence, unabridged: the launch RPC creates the session,
    thread, run and control connection; the launch outcome adopts it; and one
    Machine Agent heartbeat carries the control lease that attaches the
    connection plus the typed facts that bind its adapter identity and report
    the session idle. Every capability the input routes check is derived from
    those rows, not seeded directly.
    """

    from zerg.services.managed_provider_contracts import contract_for_provider

    contract = contract_for_provider(provider)
    assert contract is not None
    session_id = str(uuid4())
    now = datetime.now(timezone.utc).replace(microsecond=0)
    created = live.rpc(
        "session.launch.local.create.v2",
        {
            "launch": {
                "owner_id": owner_id,
                "git_repo": "cipher982/longhouse",
                "git_branch": "main",
                "started_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=5)).isoformat(),
                "plan": {
                    "session_id": session_id,
                    "provider": provider,
                    "provider_session_id": str(uuid4()),
                    "source_name": device_id,
                    "source_runner_id": None,
                    "cwd": "/workspace/longhouse",
                    "project": "session-input-api",
                    "display_name": "Session input api",
                    "managed_session_name": f"{provider}-session-input-api",
                    "permission_mode": "bypass",
                    "launch_actor": "user",
                    "launch_surface": "cli",
                    "environment": "test",
                    "origin_kind": None,
                    "hidden_from_default_timeline": 0,
                    "managed_transport": contract.managed_transport.value,
                    "attach_command": "",
                    "provider_config": {},
                },
            }
        },
    )
    run_id = str(created["run_id"])
    live.rpc(
        "session.launch.local.finish.v2",
        {
            "outcome": {
                "session_id": session_id,
                "run_id": run_id,
                "owner_id": owner_id,
                "device_id": device_id,
                "state": "adopted",
                "error_code": None,
                "error_message": None,
                "observed_at": now.isoformat(),
            }
        },
    )
    live.rpc(
        "machine.heartbeat.apply.v2",
        {
            "heartbeat": _machine_heartbeat(
                device_id=device_id,
                now=now,
                raw_json=_machine_evidence_json(provider=provider, session_id=session_id, run_id=run_id, now=now),
            ),
            "managed_leases": [
                {
                    "session_id": session_id,
                    "provider": provider,
                    "machine_id": device_id,
                    "sequence": 1,
                    "state": "attached",
                    "bridge_status": "ready",
                    "thread_subscription_status": "subscribed",
                    "observed_at": now.isoformat(),
                    "lease_ttl_ms": 300_000,
                }
            ],
            "managed_leases_present": True,
            "owner_id": owner_id,
        },
    )
    return session_id


def _live_catalog_receipt(live: LiveCatalog, *, owner_id: int, session_id: str, client_request_id: str) -> dict:
    """Read one input receipt back through catalogd, the way production reads it."""

    result = live.rpc(
        "session.input.receipt.read.v2",
        {"owner_id": owner_id, "session_id": session_id, "client_request_id": client_request_id},
    )
    assert result["found"] is True, result
    return result["receipt"]


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


def test_live_failed_input_summary_preserves_typed_error():
    summary = _live_queued_summary(
        LiveInputReceiptSnapshot(
            id="live-failed-1",
            owner_id=1,
            session_id=str(uuid4()),
            provider="claude",
            text="start work",
            intent="auto",
            status=INPUT_STATUS_FAILED,
            client_request_id="request-failed-1",
            archive_session_input_id=None,
            error_json=('{"code":"claude_lifecycle_hook_missing","message":"run `longhouse claude configure`"}'),
        )
    )

    assert summary.last_error == ("claude_lifecycle_hook_missing: run `longhouse claude configure`")


def _seed_live_runtime_state(db, session, *, phase: str = "idle") -> None:
    from zerg.models.agents import SessionRuntimeState

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


def _seed_live_session(session_local, *, owner_id: int | None = None):
    """Seed one live session. Pass ``owner_id`` to put it under an existing user.

    Session control is owner-scoped, so a second session seeded under a fresh
    user is not reachable by the first user's client. Tests about a *different
    session* (not a different owner) must share the owner.
    """
    session_id = uuid4()
    provider_session_id = f"session-input-{uuid4().hex[:8]}"
    with session_local() as db:
        if owner_id is None:
            user = User(email=f"input-{uuid4().hex[:6]}@test.local", role=UserRole.USER.value)
            db.add(user)
            db.commit()
            db.refresh(user)
        else:
            user = db.query(User).filter(User.id == int(owner_id)).one()

        store = SessionFixtureStore(db)
        started_at = datetime.now(timezone.utc)
        store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="claude",
                environment="Cinder",
                project="session-input-api",
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
                        content_text="seed",
                        timestamp=started_at,
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                    )
                ],
            )
        )
        session = store.get_session(session_id)
        assert session is not None
        seed_managed_kernel_rows(db, session, control_plane="claude_channel_bridge")
        runner = Runner(
            id=1,
            owner_id=user.id,
            name="cinder",
            status="online",
            auth_secret_hash="test",
        )
        db.merge(runner)
        db.commit()
        get_runner_connection_manager().register(user.id, 1, SimpleNamespace())
        _seed_live_runtime_state(db, session)
        user_id = user.id

    return session_id, user_id


def _stub_dispatch(monkeypatch):
    """Accept every managed-control dispatch, recording what was sent.

    ``_dispatch_managed_local_text`` is the one seam every input lane leaves
    the process through, and behind it a send is a Machine Agent control
    command against a live control grant. The queue tests below are about the
    drain state machine -- claim, lease, deliver, retry -- so they fake the
    seam instead of standing up a machine, exactly the way the retry and
    failure tests in this file already do.
    """
    from fastapi.responses import JSONResponse

    calls: list[dict] = []

    async def fake_dispatch(
        *,
        source_session,
        owner_id,
        message,
        request_id,
        lock_scope_id,
        db,
        session_input_id=None,
        attachments=None,
    ):
        calls.append({"session_id": str(source_session.id), "text": message, "request_id": request_id})
        return JSONResponse(
            content={
                "accepted": True,
                "session_id": str(source_session.id),
                "request_id": request_id,
            }
        )

    monkeypatch.setattr("zerg.services.session_chat_impl._dispatch_managed_local_text", fake_dispatch)
    return calls


class _AutoCompletingMachineWebSocket:
    def __init__(self):
        self.sent: list[dict[str, object]] = []

    async def send_json(self, message):
        self.sent.append(message)
        await get_machine_control_channel_registry().complete_command(
            {
                "type": "command_result",
                "command_id": message["command_id"],
                "ok": True,
                "result": {
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                    "turn_id": "machine-control-turn-1",
                },
            }
        )


class _TurnEndedMachineWebSocket:
    """Fake Machine Agent whose adapter reports the turn already ended.

    This is the shape the engine returns when a steer lands after the provider
    finished its turn: the command completes, but with the adapter's typed
    turn-ended exit rather than success.
    """

    def __init__(self):
        self.sent: list[dict[str, object]] = []

    async def send_json(self, message):
        self.sent.append(message)
        await get_machine_control_channel_registry().complete_command(
            {
                "type": "command_result",
                "command_id": message["command_id"],
                "ok": True,
                "result": {
                    "exit_code": 2,
                    "stdout": "",
                    "stderr": "error_code: turn_ended",
                },
            }
        )


async def _clear_machine_control_registry() -> None:
    await get_machine_control_channel_registry().clear_for_tests()


async def _register_fake_machine_control(
    *,
    owner_id: int,
    supports: list[str],
    device_id: str = "cinder",
) -> _AutoCompletingMachineWebSocket:
    websocket = _AutoCompletingMachineWebSocket()
    await get_machine_control_channel_registry().register(
        owner_id=owner_id,
        device_id=device_id,
        machine_name=device_id,
        engine_build="test-engine",
        supports=supports,
        websocket=websocket,
    )
    return websocket


def test_session_input_api_schema_exposes_typed_lifecycle_contract():
    from zerg.routers.session_chat import QueuedInputSummary
    from zerg.routers.session_chat import SessionInputRequest
    from zerg.routers.session_chat import SessionInputResponse

    request_schema = SessionInputRequest.model_json_schema()
    response_schema = SessionInputResponse.model_json_schema()
    queued_schema = QueuedInputSummary.model_json_schema()

    assert request_schema["properties"]["intent"]["enum"] == ["auto", "queue", "steer"]
    assert response_schema["properties"]["outcome"]["enum"] == ["sent", "queued"]
    assert response_schema["properties"]["intent"]["enum"] == ["auto", "queue", "steer"]
    turn_schema = response_schema["properties"]["turn"]
    assert "ConsoleTurnReceiptResponse" in str(turn_schema)
    assert queued_schema["properties"]["intent"]["enum"] == ["auto", "queue", "steer"]
    assert queued_schema["properties"]["status"]["enum"] == [
        "queued",
        "delivering",
        "delivered",
        "cancelled",
        "failed",
    ]


def test_json_input_rejects_empty_text_by_contract(tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        resp = client.post(
            f"/api/sessions/{session_id}/input",
            json={"text": "", "intent": "auto", "client_request_id": "empty-json-1"},
        )
        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert any(item["loc"] == ["body", "text"] for item in detail)
    finally:
        api_app_ref.dependency_overrides = {}


def test_intent_auto_sends_now_and_acks_from_the_live_receipt(live_catalog, live_catalog_client):  # noqa: F811
    email = "live-auto@test.local"
    owner_id = live_catalog.create_user(email)
    cookies = {"longhouse_session": live_catalog.browser_cookie(owner_id=owner_id, email=email)}
    session_id = _seed_live_catalog_session(live_catalog, owner_id=owner_id)
    websocket = asyncio.run(_register_fake_machine_control(owner_id=owner_id, supports=["claude.send"], device_id=LIVE_CATALOG_DEVICE_ID))

    try:
        resp = live_catalog_client.post(
            f"/sessions/{session_id}/input",
            json={"text": "hello", "intent": "auto", "client_request_id": "live-auto-1"},
            cookies=cookies,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["outcome"] == "sent"
        assert body["intent"] == "auto"
        assert body["input_id"] is None
        assert body["live_input_id"]

        assert len(websocket.sent) == 1
        frame = websocket.sent[0]
        assert frame["command_type"] == "session.send_text"
        assert frame["payload"]["text"] == "hello"

        receipt = _live_catalog_receipt(
            live_catalog,
            owner_id=owner_id,
            session_id=session_id,
            client_request_id="live-auto-1",
        )
        assert receipt["id"] == body["live_input_id"]
        assert receipt["status"] == INPUT_STATUS_DELIVERED
        assert receipt["delivery_request_id"]
        # An auto send is delivered from the catalog receipt alone; nothing is
        # projected into an archive session_inputs row on the way.
        assert receipt["archive_session_input_id"] is None
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))
        asyncio.run(_clear_machine_control_registry())


def test_auto_input_dedupes_on_the_live_receipt(live_catalog, live_catalog_client):  # noqa: F811
    """A repeated client_request_id acks the first receipt without sending twice."""

    email = "live-dedupe@test.local"
    owner_id = live_catalog.create_user(email)
    cookies = {"longhouse_session": live_catalog.browser_cookie(owner_id=owner_id, email=email)}
    session_id = _seed_live_catalog_session(live_catalog, owner_id=owner_id)
    websocket = asyncio.run(_register_fake_machine_control(owner_id=owner_id, supports=["claude.send"], device_id=LIVE_CATALOG_DEVICE_ID))

    try:
        payload = {"text": "already sent", "intent": "auto", "client_request_id": "live-repeat-1"}
        first = live_catalog_client.post(f"/sessions/{session_id}/input", json=payload, cookies=cookies)
        assert first.status_code == 200, first.text
        asyncio.run(session_lock_manager.release(str(session_id)))
        second = live_catalog_client.post(f"/sessions/{session_id}/input", json=payload, cookies=cookies)
        assert second.status_code == 200, second.text

        assert second.json()["outcome"] == "sent"
        assert second.json()["live_input_id"] == first.json()["live_input_id"]
        assert second.json()["input_id"] is None
        # The second post is answered from the receipt; the machine sees one send.
        assert len(websocket.sent) == 1
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))
        asyncio.run(_clear_machine_control_registry())


def test_live_input_projection_creates_archive_row_and_links_turn(tmp_path):
    from zerg.services.session_turns import create_session_turn

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)

    with session_local() as db:
        create_session_turn(db, session_id=session_id, request_id="req-live-project")

        input_id = _project_live_input_to_archive(
            db,
            source_session_id=session_id,
            owner_id=user_id,
            text="project me later",
            intent="auto",
            client_request_id="ios-live-project",
            delivery_request_id="req-live-project",
        )

        row = db.query(SessionInput).filter(SessionInput.id == input_id).one()
        assert row.status == INPUT_STATUS_DELIVERED
        assert row.client_request_id == "ios-live-project"
        assert row.delivery_request_id == "req-live-project"

        turn = db.query(SessionTurn).filter(SessionTurn.session_id == session_id, SessionTurn.request_id == "req-live-project").one()
        assert turn.session_input_id == input_id


def _assert_provider_auto_input_routes_through_machine_control(
    live: LiveCatalog,
    client,
    *,
    provider: str,
    support: str,
) -> None:
    email = f"live-{provider}-send@test.local"
    owner_id = live.create_user(email)
    cookies = {"longhouse_session": live.browser_cookie(owner_id=owner_id, email=email)}
    device_id = f"{provider}-machine-control"
    session_id = _seed_live_catalog_session(live, owner_id=owner_id, provider=provider, device_id=device_id)
    websocket = asyncio.run(_register_fake_machine_control(owner_id=owner_id, supports=[support], device_id=device_id))

    try:
        resp = client.post(
            f"/sessions/{session_id}/input",
            json={"text": f"ship through {provider}", "intent": "auto", "client_request_id": f"{provider}-send-1"},
            cookies=cookies,
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["outcome"] == "sent"
        assert body["intent"] == "auto"
        assert len(websocket.sent) == 1
        frame = websocket.sent[0]
        assert frame["command_type"] == "session.send_text"
        assert frame["session_id"] == session_id
        assert str(frame["command_id"]).startswith(f"managed-control:{session_id}:session.send_text:")
        assert frame["payload"]["provider"] == provider
        assert frame["payload"]["text"] == f"ship through {provider}"
        # Authorization binds the adapter identity the Helm launch seeded, so
        # the engine is handed a control grant rather than a bare session id.
        assert frame["payload"]["longhouse_control_grant"]["run_id"]

        receipt = _live_catalog_receipt(
            live,
            owner_id=owner_id,
            session_id=session_id,
            client_request_id=f"{provider}-send-1",
        )
        assert receipt["id"] == body["live_input_id"]
        assert receipt["status"] == INPUT_STATUS_DELIVERED
        assert receipt["archive_session_input_id"] is None
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))
        asyncio.run(_clear_machine_control_registry())


def test_claude_auto_input_routes_through_machine_control(live_catalog, live_catalog_client):  # noqa: F811
    _assert_provider_auto_input_routes_through_machine_control(
        live_catalog,
        live_catalog_client,
        provider="claude",
        support="claude.send",
    )


def test_opencode_auto_input_routes_through_machine_control(live_catalog, live_catalog_client):  # noqa: F811
    _assert_provider_auto_input_routes_through_machine_control(
        live_catalog,
        live_catalog_client,
        provider="opencode",
        support="opencode.send",
    )


def test_codex_auto_input_routes_through_machine_control(live_catalog, live_catalog_client):  # noqa: F811
    _assert_provider_auto_input_routes_through_machine_control(
        live_catalog,
        live_catalog_client,
        provider="codex",
        support="codex.send",
    )


def test_antigravity_auto_input_is_routed_through_the_hook_inbox(live_catalog, live_catalog_client):  # noqa: F811
    # The hook-inbox send path is routed and the Helm launcher seeds the
    # control identity authorization binds against, so the input is accepted
    # and reaches the machine.
    _assert_provider_auto_input_routes_through_machine_control(
        live_catalog,
        live_catalog_client,
        provider="antigravity",
        support="antigravity.send",
    )


class _DisconnectOnSendMachineWebSocket:
    """Fake Machine Agent that drops its control channel as the command goes out.

    Mimics the most plausible "no babysitting" steer-loop failure: the engine's
    control WebSocket disconnects while a send_text is in flight. The frame is
    recorded, then the connection unregisters itself, which fails the pending
    command via the registry's disconnect path.
    """

    def __init__(self, *, owner_id: int, device_id: str):
        self.sent: list[dict[str, object]] = []
        self._owner_id = owner_id
        self._device_id = device_id

    async def send_json(self, message):
        self.sent.append(message)
        await get_machine_control_channel_registry().unregister(
            owner_id=self._owner_id,
            device_id=self._device_id,
            websocket=self,
        )


def _assert_provider_inflight_disconnect_fails_cleanly(
    live: LiveCatalog,
    client,
    *,
    provider: str,
    support: str,
) -> None:
    email = f"live-{provider}-disconnect@test.local"
    owner_id = live.create_user(email)
    cookies = {"longhouse_session": live.browser_cookie(owner_id=owner_id, email=email)}
    device_id = f"{provider}-machine-control"
    session_id = _seed_live_catalog_session(live, owner_id=owner_id, provider=provider, device_id=device_id)
    websocket = _DisconnectOnSendMachineWebSocket(owner_id=owner_id, device_id=device_id)
    asyncio.run(
        get_machine_control_channel_registry().register(
            owner_id=owner_id,
            device_id=device_id,
            machine_name=device_id,
            engine_build="test-engine",
            supports=[support],
            websocket=websocket,
        )
    )

    try:
        resp = client.post(
            f"/sessions/{session_id}/input",
            json={
                "text": f"steer through {provider}",
                "intent": "auto",
                "client_request_id": f"{provider}-disconnect-1",
            },
            cookies=cookies,
        )

        # The engine dropped mid-command: the client must see a clean gateway
        # error, never a false "sent".
        assert resp.status_code == 502, resp.text
        assert resp.json()["detail"]["error_code"] == "send_failed"
        assert len(websocket.sent) == 1
        assert websocket.sent[0]["command_type"] == "session.send_text"

        receipt = _live_catalog_receipt(
            live,
            owner_id=owner_id,
            session_id=session_id,
            client_request_id=f"{provider}-disconnect-1",
        )
        # The crucial "no babysitting" guarantee: a dropped send is NOT
        # silently marked delivered.
        assert receipt["status"] == INPUT_STATUS_FAILED
        assert receipt["status"] != INPUT_STATUS_DELIVERED
        assert receipt["error_json"]
        # Lock must be released so the next steer attempt is not wedged.
        assert asyncio.run(session_lock_manager.is_locked(str(session_id))) is False
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))
        asyncio.run(_clear_machine_control_registry())


def test_claude_inflight_disconnect_fails_cleanly(live_catalog, live_catalog_client):  # noqa: F811
    _assert_provider_inflight_disconnect_fails_cleanly(
        live_catalog,
        live_catalog_client,
        provider="claude",
        support="claude.send",
    )


def test_codex_inflight_disconnect_fails_cleanly(live_catalog, live_catalog_client):  # noqa: F811
    _assert_provider_inflight_disconnect_fails_cleanly(
        live_catalog,
        live_catalog_client,
        provider="codex",
        support="codex.send",
    )


def test_queue_input_acks_from_live_receipt_without_archive_row(live_catalog, live_catalog_client):  # noqa: F811
    email = "live-queue@test.local"
    owner_id = live_catalog.create_user(email)
    cookies = {"longhouse_session": live_catalog.browser_cookie(owner_id=owner_id, email=email)}
    session_id = _seed_live_catalog_session(live_catalog, owner_id=owner_id)
    # A control channel that would happily accept a send, so "nothing was
    # dispatched" below is a fact about queue intent, not about the machine.
    websocket = asyncio.run(_register_fake_machine_control(owner_id=owner_id, supports=["claude.send"], device_id=LIVE_CATALOG_DEVICE_ID))

    try:
        resp = live_catalog_client.post(
            f"/sessions/{session_id}/input",
            json={"text": "queued hot", "intent": "queue", "client_request_id": "live-queue-1"},
            cookies=cookies,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["outcome"] == "queued"
        assert body["input_id"] is None
        assert body["live_input_id"]
        assert body["queued"] == [
            {
                "id": None,
                "live_input_id": body["live_input_id"],
                "text": "queued hot",
                "intent": "queue",
                "status": "queued",
                "last_error": None,
                "created_at": body["queued"][0]["created_at"],
            }
        ]
        assert websocket.sent == []

        receipt = _live_catalog_receipt(
            live_catalog,
            owner_id=owner_id,
            session_id=session_id,
            client_request_id="live-queue-1",
        )
        assert receipt["id"] == body["live_input_id"]
        assert receipt["status"] == INPUT_STATUS_QUEUED
        assert receipt["client_request_id"] == "live-queue-1"
        # The catalog receipt is the whole record. Queuing projects nothing into
        # an archive session_inputs row, so nothing binds it to one.
        assert receipt["archive_session_input_id"] is None
    finally:
        asyncio.run(_clear_machine_control_registry())


def test_cancel_live_queued_input_uses_live_receipt(live_catalog, live_catalog_client):  # noqa: F811
    email = "live-cancel@test.local"
    owner_id = live_catalog.create_user(email)
    cookies = {"longhouse_session": live_catalog.browser_cookie(owner_id=owner_id, email=email)}
    session_id = _seed_live_catalog_session(live_catalog, owner_id=owner_id)

    queued = live_catalog_client.post(
        f"/sessions/{session_id}/input",
        json={"text": "cancel hot", "intent": "queue", "client_request_id": "live-cancel-1"},
        cookies=cookies,
    )
    assert queued.status_code == 200, queued.text
    live_input_id = queued.json()["live_input_id"]

    resp = live_catalog_client.delete(f"/sessions/{session_id}/inputs/live/{live_input_id}", cookies=cookies)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"cancelled": True, "live_input_id": live_input_id, "input_id": None}

    listed = live_catalog_client.get(f"/sessions/{session_id}/inputs", cookies=cookies)
    assert listed.status_code == 200, listed.text
    assert listed.json() == []
    receipt = _live_catalog_receipt(
        live_catalog,
        owner_id=owner_id,
        session_id=session_id,
        client_request_id="live-cancel-1",
    )
    assert receipt["id"] == live_input_id
    assert receipt["status"] == INPUT_STATUS_CANCELLED


def test_client_request_id_unique_constraint_blocks_duplicate_rows(tmp_path):
    from sqlalchemy.exc import IntegrityError

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)

    with session_local() as db:
        create_session_input(
            db,
            session_id=session_id,
            text="once",
            owner_id=user_id,
            intent="queue",
            status=INPUT_STATUS_QUEUED,
            client_request_id="ios-unique-1",
        )
        try:
            create_session_input(
                db,
                session_id=session_id,
                text="twice",
                owner_id=user_id,
                intent="queue",
                status=INPUT_STATUS_QUEUED,
                client_request_id="ios-unique-1",
            )
        except IntegrityError:
            db.rollback()
        else:
            raise AssertionError("duplicate client request id inserted")

        rows = db.query(SessionInput).filter(SessionInput.session_id == session_id).all()
        assert len(rows) == 1
        assert rows[0].body == "once"


def test_client_request_id_same_key_different_owner_creates_separate_inputs(tmp_path):
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)

    with session_local() as db:
        second_user = User(email="second-owner@test.local", role=UserRole.USER.value)
        db.add(second_user)
        db.flush()
        first = create_session_input(
            db,
            session_id=session_id,
            text="same owner scoped id",
            owner_id=user_id,
            intent="queue",
            status=INPUT_STATUS_QUEUED,
            client_request_id="shared-client-key",
        )
        second = create_session_input(
            db,
            session_id=session_id,
            text="same owner scoped id",
            owner_id=second_user.id,
            intent="queue",
            status=INPUT_STATUS_QUEUED,
            client_request_id="shared-client-key",
        )
        db.commit()

        assert first.id != second.id
        rows = (
            db.query(SessionInput)
            .filter(SessionInput.session_id == session_id, SessionInput.client_request_id == "shared-client-key")
            .all()
        )
        assert {row.owner_id for row in rows} == {user_id, second_user.id}


def test_duplicate_integrity_retry_path_reuses_failed_input(tmp_path):
    from zerg.routers.session_chat import SessionInputRequest
    from zerg.routers.session_chat import _create_session_input_or_existing

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    with session_local() as db:
        session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        failed = create_session_input(
            db,
            session_id=session_id,
            text="retry after failed race",
            owner_id=user_id,
            intent="auto",
            status=INPUT_STATUS_FAILED,
            client_request_id="race-client-key",
            delivery_request_id="old-delivery",
        )
        db.commit()
        failed_id = int(failed.id)

        row = _create_session_input_or_existing(
            db=db,
            source_session=session,
            owner_id=user_id,
            body=SessionInputRequest(
                text="retry after failed race",
                intent="auto",
                client_request_id="race-client-key",
            ),
            intent="auto",
            status_value=INPUT_STATUS_DELIVERING,
            client_request_id="race-client-key",
            delivery_request_id="new-delivery",
        )
        db.commit()

        assert isinstance(row, SessionInput)
        assert int(row.id) == failed_id
        assert row.status == INPUT_STATUS_DELIVERING
        assert row.delivery_request_id == "new-delivery"


def test_queue_drain_preserves_client_request_id(tmp_path):
    from zerg.services.session_inputs import claim_next_queued

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)

    with session_local() as db:
        row = create_session_input(
            db,
            session_id=session_id,
            text="drain me",
            owner_id=user_id,
            intent="queue",
            status=INPUT_STATUS_QUEUED,
            client_request_id="ios-drain-1",
        )

        claimed = claim_next_queued(db, session_id, delivery_request_id="drain-delivery-1")

        assert claimed is not None
        assert claimed.id == row.id
        assert claimed.client_request_id == "ios-drain-1"
        assert claimed.delivery_request_id == "drain-delivery-1"


def test_live_queue_drain_dispatches_catalog_receipt_without_archive_projection(live_catalog, live_catalog_client):  # noqa: F811
    """A queued catalog receipt drains through the catalog drainer, not the archive one.

    ``wake_session_input_queue`` is the archive-lane drainer and reads
    ``session_inputs``. A Runtime Host has no archive row to read: after a
    terminal turn it calls ``wake_next_live_catalog_input``, which claims the
    receipt through catalogd, dispatches it over the machine control channel
    and finishes it back in the catalog.
    """

    from zerg.services.live_control_catalog import wake_next_live_catalog_input

    email = "live-drain@test.local"
    owner_id = live_catalog.create_user(email)
    cookies = {"longhouse_session": live_catalog.browser_cookie(owner_id=owner_id, email=email)}
    session_id = _seed_live_catalog_session(live_catalog, owner_id=owner_id)
    websocket = asyncio.run(_register_fake_machine_control(owner_id=owner_id, supports=["claude.send"], device_id=LIVE_CATALOG_DEVICE_ID))

    try:
        queued = live_catalog_client.post(
            f"/sessions/{session_id}/input",
            json={"text": "drain hot receipt", "intent": "queue", "client_request_id": "live-drain-1"},
            cookies=cookies,
        )
        assert queued.status_code == 200, queued.text
        live_input_id = queued.json()["live_input_id"]

        assert asyncio.run(wake_next_live_catalog_input(session_id)) is True

        assert len(websocket.sent) == 1, "the drained receipt must reach the machine control channel"
        frame = websocket.sent[0]
        assert frame["command_type"] == "session.send_text"
        assert frame["session_id"] == session_id
        assert frame["payload"]["text"] == "drain hot receipt"

        receipt = _live_catalog_receipt(
            live_catalog,
            owner_id=owner_id,
            session_id=session_id,
            client_request_id="live-drain-1",
        )
        assert receipt["id"] == live_input_id
        assert receipt["status"] == INPUT_STATUS_DELIVERED
        assert receipt["delivery_request_id"]
        # Delivered from the catalog alone: no archive row was created for it,
        # so nothing was projected and nothing links back.
        assert receipt["archive_session_input_id"] is None
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))
        asyncio.run(_clear_machine_control_registry())


def test_queue_wake_defers_behind_active_turn(monkeypatch, tmp_path):
    from zerg.services.session_input_queue import wake_session_input_queue
    from zerg.services.session_inputs import create_session_input
    from zerg.services.session_turns import create_session_turn
    from zerg.services.session_turns import mark_session_turn_send_accepted

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    dispatch_calls = _stub_dispatch(monkeypatch)

    with session_local() as db:
        create_session_turn(db, session_id=session_id, request_id="req-active-prior")
        mark_session_turn_send_accepted(db, session_id=session_id, request_id="req-active-prior")
        row = create_session_input(
            db,
            session_id=session_id,
            text="wait behind active turn",
            owner_id=user_id,
            intent="queue",
            status=INPUT_STATUS_QUEUED,
            client_request_id="ios-active-gate-1",
        )
        input_id = int(row.id)
        db_bind = db.get_bind()
        db.commit()

    result = asyncio.run(
        wake_session_input_queue(
            db_bind=db_bind,
            session_id=session_id,
            reason="test_active_turn",
            lock_scope_id=str(session_id),
        )
    )

    with session_local() as db:
        row = db.query(SessionInput).filter(SessionInput.id == input_id).one()
        assert row.status == INPUT_STATUS_QUEUED
    assert result.dispatched is False
    assert result.reason == "active_turn"
    assert dispatch_calls == []


def test_queue_wake_drains_after_prior_turn_terminal(monkeypatch, tmp_path):
    from zerg.services.session_input_queue import wake_session_input_queue
    from zerg.services.session_inputs import create_session_input
    from zerg.services.session_turns import create_session_turn
    from zerg.services.session_turns import mark_session_turn_send_accepted
    from zerg.services.session_turns import mark_session_turn_terminal

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    _stub_dispatch(monkeypatch)

    with session_local() as db:
        create_session_turn(db, session_id=session_id, request_id="req-terminal-prior")
        mark_session_turn_send_accepted(db, session_id=session_id, request_id="req-terminal-prior")
        mark_session_turn_terminal(db, session_id=session_id, request_id="req-terminal-prior", phase="idle")
        row = create_session_input(
            db,
            session_id=session_id,
            text="drain after terminal prior turn",
            owner_id=user_id,
            intent="queue",
            status=INPUT_STATUS_QUEUED,
            client_request_id="ios-terminal-gate-1",
        )
        input_id = int(row.id)
        db_bind = db.get_bind()
        db.commit()

    try:
        result = asyncio.run(
            wake_session_input_queue(
                db_bind=db_bind,
                session_id=session_id,
                reason="test_terminal_turn",
                lock_scope_id=str(session_id),
            )
        )

        with session_local() as db:
            row = db.query(SessionInput).filter(SessionInput.id == input_id).one()
            assert row.status == INPUT_STATUS_DELIVERED
            assert row.delivery_request_id
        assert result.dispatched is True
        assert result.input_id == input_id
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))


def test_queue_wake_drains_needs_user_phase(monkeypatch, tmp_path):
    from zerg.services.session_input_queue import wake_session_input_queue
    from zerg.services.session_inputs import create_session_input

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    _stub_dispatch(monkeypatch)

    with session_local() as db:
        session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        _seed_live_runtime_state(db, session, phase="needs_user")
        row = create_session_input(
            db,
            session_id=session_id,
            text="answer needs user",
            owner_id=user_id,
            intent="queue",
            status=INPUT_STATUS_QUEUED,
            client_request_id="ios-needs-user-gate-1",
        )
        input_id = int(row.id)
        db_bind = db.get_bind()
        db.commit()

    try:
        result = asyncio.run(
            wake_session_input_queue(
                db_bind=db_bind,
                session_id=session_id,
                reason="test_needs_user",
                lock_scope_id=str(session_id),
            )
        )

        with session_local() as db:
            row = db.query(SessionInput).filter(SessionInput.id == input_id).one()
            assert row.status == INPUT_STATUS_DELIVERED
        assert result.dispatched is True
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))


def test_concurrent_queue_wakes_dispatch_at_most_one_input(monkeypatch, tmp_path):
    from zerg.services.session_input_queue import wake_session_input_queue
    from zerg.services.session_inputs import create_session_input

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    dispatch_calls = _stub_dispatch(monkeypatch)

    with session_local() as db:
        first = create_session_input(
            db,
            session_id=session_id,
            text="first concurrent",
            owner_id=user_id,
            intent="queue",
            status=INPUT_STATUS_QUEUED,
            client_request_id="ios-concurrent-1",
        )
        second = create_session_input(
            db,
            session_id=session_id,
            text="second concurrent",
            owner_id=user_id,
            intent="queue",
            status=INPUT_STATUS_QUEUED,
            client_request_id="ios-concurrent-2",
        )
        input_ids = [int(first.id), int(second.id)]
        db_bind = db.get_bind()

    async def run_wakes():
        return await asyncio.gather(
            wake_session_input_queue(
                db_bind=db_bind,
                session_id=session_id,
                reason="test_concurrent_1",
                lock_scope_id=str(session_id),
            ),
            wake_session_input_queue(
                db_bind=db_bind,
                session_id=session_id,
                reason="test_concurrent_2",
                lock_scope_id=str(session_id),
            ),
        )

    try:
        results = asyncio.run(run_wakes())

        with session_local() as db:
            rows = db.query(SessionInput).filter(SessionInput.id.in_(input_ids)).order_by(SessionInput.id.asc()).all()
            statuses = [row.status for row in rows]
            assert statuses.count(INPUT_STATUS_DELIVERED) == 1
            assert statuses.count(INPUT_STATUS_QUEUED) == 1
        assert sum(1 for result in results if result.dispatched) == 1
        assert len(dispatch_calls) == 1
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))


def test_active_attempt_blocks_queue_readiness(tmp_path):
    from zerg.services.session_input_queue import evaluate_session_input_queue_readiness
    from zerg.services.session_inputs import create_session_input

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)

    with session_local() as db:
        session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        row = create_session_input(
            db,
            session_id=session_id,
            text="held by active lease",
            owner_id=user_id,
            intent="queue",
            status=INPUT_STATUS_QUEUED,
            client_request_id="ios-active-lease-1",
        )
        db.add(
            SessionInputDeliveryAttempt(
                session_input_id=int(row.id),
                session_id=session_id,
                thread_id=row.thread_id,
                owner_id=user_id,
                request_id="active-attempt-1",
                attempt_number=1,
                status="acquired",
                lease_owner="test",
                lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
        )
        db.commit()

        readiness = evaluate_session_input_queue_readiness(db, session=session, owner_id=user_id)

    assert readiness.ready is False
    assert readiness.reason == "lease_active"


def test_concurrent_queue_wakes_different_lock_scopes_create_one_attempt(monkeypatch, tmp_path):
    from zerg.services.session_input_queue import wake_session_input_queue
    from zerg.services.session_inputs import create_session_input

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    dispatch_calls = _stub_dispatch(monkeypatch)

    with session_local() as db:
        first = create_session_input(
            db,
            session_id=session_id,
            text="first durable lease",
            owner_id=user_id,
            intent="queue",
            status=INPUT_STATUS_QUEUED,
            client_request_id="ios-durable-concurrent-1",
        )
        second = create_session_input(
            db,
            session_id=session_id,
            text="second durable lease",
            owner_id=user_id,
            intent="queue",
            status=INPUT_STATUS_QUEUED,
            client_request_id="ios-durable-concurrent-2",
        )
        input_ids = [int(first.id), int(second.id)]
        db_bind = db.get_bind()

    async def run_wakes():
        return await asyncio.gather(
            wake_session_input_queue(
                db_bind=db_bind,
                session_id=session_id,
                reason="test_durable_concurrent_1",
                lock_scope_id=f"scope-a-{uuid4().hex}",
            ),
            wake_session_input_queue(
                db_bind=db_bind,
                session_id=session_id,
                reason="test_durable_concurrent_2",
                lock_scope_id=f"scope-b-{uuid4().hex}",
            ),
        )

    results = asyncio.run(run_wakes())

    with session_local() as db:
        rows = db.query(SessionInput).filter(SessionInput.id.in_(input_ids)).order_by(SessionInput.id.asc()).all()
        statuses = [row.status for row in rows]
        attempts = db.query(SessionInputDeliveryAttempt).filter(SessionInputDeliveryAttempt.session_id == session_id).all()
        assert statuses.count(INPUT_STATUS_DELIVERED) == 1
        assert statuses.count(INPUT_STATUS_QUEUED) == 1
        assert len(attempts) == 1
        assert attempts[0].status == "accepted"
    assert sum(1 for result in results if result.dispatched) == 1
    assert len(dispatch_calls) == 1


def test_expired_attempt_allows_retry(monkeypatch, tmp_path):
    from zerg.services.session_input_queue import wake_session_input_queue
    from zerg.services.session_inputs import create_session_input

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)
    _stub_dispatch(monkeypatch)

    with session_local() as db:
        row = create_session_input(
            db,
            session_id=session_id,
            text="retry expired lease",
            owner_id=user_id,
            intent="queue",
            status=INPUT_STATUS_DELIVERING,
            client_request_id="ios-expired-attempt-1",
            delivery_request_id="expired-attempt",
        )
        expired = SessionInputDeliveryAttempt(
            session_input_id=int(row.id),
            session_id=session_id,
            thread_id=row.thread_id,
            owner_id=user_id,
            request_id="expired-attempt",
            attempt_number=1,
            status="acquired",
            lease_owner="expired",
            lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        db.add(expired)
        db.commit()
        input_id = int(row.id)
        db_bind = db.get_bind()

    try:
        result = asyncio.run(
            wake_session_input_queue(
                db_bind=db_bind,
                session_id=session_id,
                reason="test_expired_attempt",
                lock_scope_id=str(session_id),
            )
        )

        with session_local() as db:
            row = db.query(SessionInput).filter(SessionInput.id == input_id).one()
            attempts = (
                db.query(SessionInputDeliveryAttempt)
                .filter(SessionInputDeliveryAttempt.session_input_id == input_id)
                .order_by(SessionInputDeliveryAttempt.id.asc())
                .all()
            )
            assert row.status == INPUT_STATUS_DELIVERED
            assert [attempt.status for attempt in attempts] == ["expired", "accepted"]
        assert result.dispatched is True
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))


def test_expired_steer_attempt_is_not_silently_requeued(tmp_path):
    from zerg.services.session_input_queue import wake_session_input_queue
    from zerg.services.session_inputs import create_session_input

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)

    with session_local() as db:
        row = create_session_input(
            db,
            session_id=session_id,
            text="stale steer",
            owner_id=user_id,
            intent="steer",
            status=INPUT_STATUS_DELIVERING,
            client_request_id="ios-stale-steer-1",
            delivery_request_id="expired-steer-attempt",
        )
        db.add(
            SessionInputDeliveryAttempt(
                session_input_id=int(row.id),
                session_id=session_id,
                thread_id=row.thread_id,
                owner_id=user_id,
                request_id="expired-steer-attempt",
                attempt_number=1,
                status="acquired",
                lease_owner="expired",
                lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
        )
        db.commit()
        input_id = int(row.id)
        db_bind = db.get_bind()

    result = asyncio.run(
        wake_session_input_queue(
            db_bind=db_bind,
            session_id=session_id,
            reason="test_expired_steer",
            lock_scope_id=str(session_id),
        )
    )

    with session_local() as db:
        row = db.query(SessionInput).filter(SessionInput.id == input_id).one()
        attempt = db.query(SessionInputDeliveryAttempt).filter(SessionInputDeliveryAttempt.session_input_id == input_id).one()
        assert row.status == INPUT_STATUS_FAILED
        assert row.last_error == "steer delivery interrupted before accepted attempt"
        assert row.delivery_request_id == "expired-steer-attempt"
        assert attempt.status == "expired"
    assert result.dispatched is False
    assert result.reason == "no_queued_input"


def test_expired_attachment_attempt_is_failed_not_requeued(tmp_path):
    from zerg.services.session_input_queue import wake_session_input_queue
    from zerg.services.session_inputs import create_session_input

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)

    with session_local() as db:
        row = create_session_input(
            db,
            session_id=session_id,
            text="stale attachment",
            owner_id=user_id,
            intent="queue",
            status=INPUT_STATUS_DELIVERING,
            client_request_id="ios-stale-attachment-1",
            delivery_request_id="expired-attachment-attempt",
        )
        db.add(
            SessionInputAttachment(
                session_input_id=int(row.id),
                session_id=session_id,
                mime_type="image/png",
                byte_size=12,
                sha256="a" * 64,
                blob_path="/tmp/missing-attachment.png",
            )
        )
        db.add(
            SessionInputDeliveryAttempt(
                session_input_id=int(row.id),
                session_id=session_id,
                thread_id=row.thread_id,
                owner_id=user_id,
                request_id="expired-attachment-attempt",
                attempt_number=1,
                status="submitted",
                lease_owner="expired",
                lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
        )
        db.commit()
        input_id = int(row.id)
        db_bind = db.get_bind()

    result = asyncio.run(
        wake_session_input_queue(
            db_bind=db_bind,
            session_id=session_id,
            reason="test_expired_attachment",
            lock_scope_id=str(session_id),
        )
    )

    with session_local() as db:
        row = db.query(SessionInput).filter(SessionInput.id == input_id).one()
        attempt = db.query(SessionInputDeliveryAttempt).filter(SessionInputDeliveryAttempt.session_input_id == input_id).one()
        assert row.status == INPUT_STATUS_FAILED
        assert row.last_error == "attachment delivery interrupted before accepted attempt"
        assert row.delivery_request_id == "expired-attachment-attempt"
        assert attempt.status == "expired"
    assert result.dispatched is False
    assert result.reason == "no_queued_input"


def test_queue_drain_requeues_transient_machine_control_unavailable(monkeypatch, tmp_path):
    from fastapi.responses import JSONResponse

    from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_UNAVAILABLE_ERROR
    from zerg.services.session_input_queue import wake_session_input_queue
    from zerg.services.session_inputs import create_session_input
    from zerg.services.session_turns import SESSION_TURN_ERROR_SEND_FAILED

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)

    with session_local() as db:
        row = create_session_input(
            db,
            session_id=session_id,
            text="wait for control reconnect",
            owner_id=user_id,
            intent="queue",
            status=INPUT_STATUS_QUEUED,
            client_request_id="ios-drain-requeue-1",
        )
        input_id = int(row.id)
        db_bind = db.get_bind()

    async def fake_dispatch(**_kwargs):
        return JSONResponse(
            status_code=502,
            content={
                "accepted": False,
                "error": MANAGED_CONTROL_UNAVAILABLE_ERROR,
                "error_code": SESSION_TURN_ERROR_SEND_FAILED,
            },
        )

    monkeypatch.setattr("zerg.services.session_chat_impl._dispatch_managed_local_text", fake_dispatch)

    result = asyncio.run(
        wake_session_input_queue(
            db_bind=db_bind,
            session_id=session_id,
            reason="test_transient_failure",
            lock_scope_id=str(session_id),
        )
    )

    with session_local() as db:
        row = db.query(SessionInput).filter(SessionInput.id == input_id).one()
        assert row.status == INPUT_STATUS_QUEUED
        assert row.delivery_request_id is None
        assert row.last_error == MANAGED_CONTROL_UNAVAILABLE_ERROR
        assert row.attempt_count == 1
        assert row.next_attempt_at is not None
        attempt = db.query(SessionInputDeliveryAttempt).filter(SessionInputDeliveryAttempt.session_input_id == input_id).one()
        assert attempt.status == "released"
        assert attempt.error_code == SESSION_TURN_ERROR_SEND_FAILED
    probe = asyncio.run(session_lock_manager.acquire(session_id=str(session_id), holder="probe", ttl_seconds=1))
    assert probe is not None
    asyncio.run(session_lock_manager.release(str(session_id), "probe"))
    assert result.dispatched is False
    assert result.reason == "transient_dispatch_failure"


def test_next_attempt_at_is_respected_after_transient_failure(monkeypatch, tmp_path):
    from fastapi.responses import JSONResponse

    from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_UNAVAILABLE_ERROR
    from zerg.services.session_input_queue import wake_session_input_queue
    from zerg.services.session_inputs import create_session_input
    from zerg.services.session_turns import SESSION_TURN_ERROR_SEND_FAILED

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)

    with session_local() as db:
        row = create_session_input(
            db,
            session_id=session_id,
            text="respect retry time",
            owner_id=user_id,
            intent="queue",
            status=INPUT_STATUS_QUEUED,
            client_request_id="ios-next-attempt-1",
        )
        input_id = int(row.id)
        db_bind = db.get_bind()

    async def fake_dispatch(*, lock_scope_id, request_id, **_kwargs):
        await session_lock_manager.release(lock_scope_id, request_id)
        return JSONResponse(
            status_code=502,
            content={
                "accepted": False,
                "error": MANAGED_CONTROL_UNAVAILABLE_ERROR,
                "error_code": SESSION_TURN_ERROR_SEND_FAILED,
            },
        )

    monkeypatch.setattr("zerg.services.session_chat_impl._dispatch_managed_local_text", fake_dispatch)

    first = asyncio.run(
        wake_session_input_queue(
            db_bind=db_bind,
            session_id=session_id,
            reason="test_next_attempt_first",
            lock_scope_id=str(session_id),
        )
    )
    second = asyncio.run(
        wake_session_input_queue(
            db_bind=db_bind,
            session_id=session_id,
            reason="test_next_attempt_second",
            lock_scope_id=str(session_id),
        )
    )

    with session_local() as db:
        row = db.query(SessionInput).filter(SessionInput.id == input_id).one()
        attempts = db.query(SessionInputDeliveryAttempt).filter(SessionInputDeliveryAttempt.session_input_id == input_id).all()
        assert row.status == INPUT_STATUS_QUEUED
        assert row.attempt_count == 1
        assert row.next_attempt_at is not None
        assert len(attempts) == 1
    assert first.reason == "transient_dispatch_failure"
    assert second.reason == "next_attempt_pending"


def test_attempt_count_increments_on_retry(monkeypatch, tmp_path):
    from fastapi.responses import JSONResponse

    from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_UNAVAILABLE_ERROR
    from zerg.services.session_input_queue import wake_session_input_queue
    from zerg.services.session_inputs import create_session_input
    from zerg.services.session_turns import SESSION_TURN_ERROR_SEND_FAILED

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)

    with session_local() as db:
        row = create_session_input(
            db,
            session_id=session_id,
            text="retry then succeed",
            owner_id=user_id,
            intent="queue",
            status=INPUT_STATUS_QUEUED,
            client_request_id="ios-retry-count-1",
        )
        input_id = int(row.id)
        db_bind = db.get_bind()

    async def fake_transient(*, lock_scope_id, request_id, **_kwargs):
        await session_lock_manager.release(lock_scope_id, request_id)
        return JSONResponse(
            status_code=502,
            content={
                "accepted": False,
                "error": MANAGED_CONTROL_UNAVAILABLE_ERROR,
                "error_code": SESSION_TURN_ERROR_SEND_FAILED,
            },
        )

    monkeypatch.setattr("zerg.services.session_chat_impl._dispatch_managed_local_text", fake_transient)
    asyncio.run(
        wake_session_input_queue(
            db_bind=db_bind,
            session_id=session_id,
            reason="test_retry_count_first",
            lock_scope_id=str(session_id),
        )
    )

    with session_local() as db:
        db.query(SessionInput).filter(SessionInput.id == input_id).update(
            {"next_attempt_at": datetime.now(timezone.utc) - timedelta(seconds=1)},
            synchronize_session=False,
        )
        db.commit()

    async def fake_success(**_kwargs):
        return JSONResponse(
            status_code=200,
            content={
                "accepted": True,
                "session_id": str(session_id),
                "request_id": "retry-success",
            },
        )

    monkeypatch.setattr("zerg.services.session_chat_impl._dispatch_managed_local_text", fake_success)
    try:
        second = asyncio.run(
            wake_session_input_queue(
                db_bind=db_bind,
                session_id=session_id,
                reason="test_retry_count_second",
                lock_scope_id=str(session_id),
            )
        )

        with session_local() as db:
            row = db.query(SessionInput).filter(SessionInput.id == input_id).one()
            attempts = (
                db.query(SessionInputDeliveryAttempt)
                .filter(SessionInputDeliveryAttempt.session_input_id == input_id)
                .order_by(SessionInputDeliveryAttempt.id.asc())
                .all()
            )
            assert row.status == INPUT_STATUS_DELIVERED
            assert row.attempt_count == 2
            assert [attempt.status for attempt in attempts] == ["released", "accepted"]
        assert second.dispatched is True
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))


def test_permanent_dispatch_failure_marks_input_and_attempt_failed(monkeypatch, tmp_path):
    from fastapi.responses import JSONResponse

    from zerg.services.session_input_queue import wake_session_input_queue
    from zerg.services.session_inputs import create_session_input

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)

    with session_local() as db:
        row = create_session_input(
            db,
            session_id=session_id,
            text="permanent failure",
            owner_id=user_id,
            intent="queue",
            status=INPUT_STATUS_QUEUED,
            client_request_id="ios-permanent-failure-1",
        )
        input_id = int(row.id)
        db_bind = db.get_bind()

    async def fake_permanent(*, lock_scope_id, request_id, **_kwargs):
        await session_lock_manager.release(lock_scope_id, request_id)
        return JSONResponse(
            status_code=502,
            content={
                "accepted": False,
                "error": "session is closed",
                "error_code": "session_closed",
            },
        )

    monkeypatch.setattr("zerg.services.session_chat_impl._dispatch_managed_local_text", fake_permanent)
    result = asyncio.run(
        wake_session_input_queue(
            db_bind=db_bind,
            session_id=session_id,
            reason="test_permanent_failure",
            lock_scope_id=str(session_id),
        )
    )

    with session_local() as db:
        row = db.query(SessionInput).filter(SessionInput.id == input_id).one()
        attempt = db.query(SessionInputDeliveryAttempt).filter(SessionInputDeliveryAttempt.session_input_id == input_id).one()
        assert row.status == INPUT_STATUS_FAILED
        assert row.last_error == "session is closed"
        assert attempt.status == "failed"
        assert attempt.error_code == "session_closed"
    assert result.dispatched is False
    assert result.reason == "dispatch_failed"


def test_client_request_id_different_text_conflicts(live_catalog, live_catalog_client):  # noqa: F811
    email = "live-conflict@test.local"
    owner_id = live_catalog.create_user(email)
    cookies = {"longhouse_session": live_catalog.browser_cookie(owner_id=owner_id, email=email)}
    session_id = _seed_live_catalog_session(live_catalog, owner_id=owner_id)

    first = live_catalog_client.post(
        f"/sessions/{session_id}/input",
        json={"text": "original", "intent": "queue", "client_request_id": "live-conflict-1"},
        cookies=cookies,
    )
    second = live_catalog_client.post(
        f"/sessions/{session_id}/input",
        json={"text": "edited", "intent": "queue", "client_request_id": "live-conflict-1"},
        cookies=cookies,
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 409, second.text
    assert second.json()["detail"] == {
        "error_code": "input_conflict",
        "existing_live_input_id": first.json()["live_input_id"],
        "reason": "different_text",
    }


def test_retry_failed_input_rejects_terminal_rows(tmp_path):
    from zerg.services.session_inputs import retry_failed_input

    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_live_session(session_local)

    with session_local() as db:
        row = create_session_input(
            db,
            session_id=session_id,
            text="already sent",
            owner_id=user_id,
            intent="auto",
            status=INPUT_STATUS_DELIVERED,
            client_request_id="ios-delivered-1",
            delivery_request_id="old-delivery",
        )
        input_id = int(row.id)

        retried = retry_failed_input(
            db,
            input_id,
            intent="auto",
            status=INPUT_STATUS_DELIVERING,
            delivery_request_id="new-delivery",
        )

        db.expire_all()
        refreshed = db.query(SessionInput).filter(SessionInput.id == input_id).one()
        assert retried is None
        assert refreshed.status == INPUT_STATUS_DELIVERED
        assert refreshed.client_request_id == "ios-delivered-1"
        assert refreshed.delivery_request_id == "old-delivery"


def test_intent_auto_locked_queues_the_live_receipt(live_catalog, live_catalog_client):  # noqa: F811
    email = "live-auto-locked@test.local"
    owner_id = live_catalog.create_user(email)
    cookies = {"longhouse_session": live_catalog.browser_cookie(owner_id=owner_id, email=email)}
    session_id = _seed_live_catalog_session(live_catalog, owner_id=owner_id)
    # A control channel that would happily accept a send, so "nothing was
    # dispatched" below is a fact about the held lock, not about the machine.
    websocket = asyncio.run(_register_fake_machine_control(owner_id=owner_id, supports=["claude.send"], device_id=LIVE_CATALOG_DEVICE_ID))

    # Pre-acquire the lock on the session scope.
    lock_scope_id = str(session_id)
    acquired = asyncio.run(session_lock_manager.acquire(session_id=lock_scope_id, holder="other", ttl_seconds=60))
    assert acquired

    try:
        resp = live_catalog_client.post(
            f"/sessions/{session_id}/input",
            json={"text": "send if free", "intent": "auto", "client_request_id": "live-auto-locked-1"},
            cookies=cookies,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["outcome"] == "queued"
        assert body["intent"] == "auto"
        assert len(body["queued"]) == 1
        assert websocket.sent == []

        receipt = _live_catalog_receipt(
            live_catalog,
            owner_id=owner_id,
            session_id=session_id,
            client_request_id="live-auto-locked-1",
        )
        assert receipt["status"] == INPUT_STATUS_QUEUED
        assert receipt["intent"] == "auto"
    finally:
        asyncio.run(session_lock_manager.release(lock_scope_id, "other"))
        asyncio.run(_clear_machine_control_registry())


def test_antigravity_steer_intent_is_rejected_before_machine_control(live_catalog, live_catalog_client):  # noqa: F811
    email = "live-agy-steer@test.local"
    owner_id = live_catalog.create_user(email)
    cookies = {"longhouse_session": live_catalog.browser_cookie(owner_id=owner_id, email=email)}
    session_id = _seed_live_catalog_session(
        live_catalog,
        owner_id=owner_id,
        provider="antigravity",
        device_id="antigravity-machine-control",
    )
    websocket = asyncio.run(
        _register_fake_machine_control(
            owner_id=owner_id,
            supports=["antigravity.send"],
            device_id="antigravity-machine-control",
        )
    )

    try:
        resp = live_catalog_client.post(
            f"/sessions/{session_id}/input",
            json={"text": "mid-turn change", "intent": "steer", "client_request_id": "agy-steer-1"},
            cookies=cookies,
        )
        # The durable invariant is that nothing reaches machine control:
        # `steer_active_turn` is false for antigravity, so the provider
        # contract refuses the intent before a command is ever built.
        assert resp.status_code == 409, resp.text
        assert websocket.sent == []
        receipt = _live_catalog_receipt(
            live_catalog,
            owner_id=owner_id,
            session_id=session_id,
            client_request_id="agy-steer-1",
        )
        assert receipt["status"] == INPUT_STATUS_FAILED
    finally:
        asyncio.run(_clear_machine_control_registry())


def test_intent_steer_acks_from_live_receipt_without_archive_row(live_catalog, live_catalog_client):  # noqa: F811
    email = "live-steer@test.local"
    owner_id = live_catalog.create_user(email)
    cookies = {"longhouse_session": live_catalog.browser_cookie(owner_id=owner_id, email=email)}
    session_id = _seed_live_catalog_session(live_catalog, owner_id=owner_id)
    # Steer routes on the steer capability alone; the send capability is not
    # advertised here so a steer that silently fell back to send would fail.
    websocket = asyncio.run(_register_fake_machine_control(owner_id=owner_id, supports=["claude.steer"], device_id=LIVE_CATALOG_DEVICE_ID))

    try:
        resp = live_catalog_client.post(
            f"/sessions/{session_id}/input",
            json={"text": "redirect hot", "intent": "steer", "client_request_id": "live-steer-1"},
            cookies=cookies,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["outcome"] == "sent"
        assert body["intent"] == "steer"
        assert body["input_id"] is None
        assert body["live_input_id"]

        assert len(websocket.sent) == 1
        frame = websocket.sent[0]
        assert frame["command_type"] == "session.steer_text"
        assert frame["payload"]["text"] == "redirect hot"
        assert frame["payload"]["intent"] == "steer"

        receipt = _live_catalog_receipt(
            live_catalog,
            owner_id=owner_id,
            session_id=session_id,
            client_request_id="live-steer-1",
        )
        assert receipt["id"] == body["live_input_id"]
        assert receipt["status"] == INPUT_STATUS_DELIVERED
        assert receipt["intent"] == "steer"
        assert receipt["client_request_id"] == "live-steer-1"
        # A steer is delivered straight from the catalog receipt; no archive
        # projection is enqueued for it.
        assert receipt["archive_session_input_id"] is None
    finally:
        asyncio.run(_clear_machine_control_registry())


def test_codex_steer_intent_routes_through_machine_control(live_catalog, live_catalog_client):  # noqa: F811
    email = "live-codex-steer@test.local"
    owner_id = live_catalog.create_user(email)
    cookies = {"longhouse_session": live_catalog.browser_cookie(owner_id=owner_id, email=email)}
    session_id = _seed_live_catalog_session(
        live_catalog,
        owner_id=owner_id,
        provider="codex",
        device_id="codex-machine-control",
    )
    websocket = asyncio.run(
        _register_fake_machine_control(
            owner_id=owner_id,
            supports=["codex.steer"],
            device_id="codex-machine-control",
        )
    )

    try:
        resp = live_catalog_client.post(
            f"/sessions/{session_id}/input",
            json={"text": "steer through codex bridge", "intent": "steer", "client_request_id": "codex-steer-1"},
            cookies=cookies,
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["outcome"] == "sent"
        assert body["intent"] == "steer"
        assert len(websocket.sent) == 1
        frame = websocket.sent[0]
        assert frame["command_type"] == "session.steer_text"
        assert frame["session_id"] == session_id
        assert str(frame["command_id"]).startswith(f"managed-control:{session_id}:session.steer_text:")
        assert frame["payload"]["provider"] == "codex"
        assert frame["payload"]["text"] == "steer through codex bridge"
        assert frame["payload"]["intent"] == "steer"

        receipt = _live_catalog_receipt(
            live_catalog,
            owner_id=owner_id,
            session_id=session_id,
            client_request_id="codex-steer-1",
        )
        assert receipt["status"] == INPUT_STATUS_DELIVERED
        assert receipt["intent"] == "steer"
    finally:
        asyncio.run(_clear_machine_control_registry())


def test_intent_steer_turn_ended_returns_structured_409(live_catalog, live_catalog_client):  # noqa: F811
    """A steer that lost the turn race is a typed 409, and the receipt keeps the loss."""

    email = "live-turn-ended@test.local"
    owner_id = live_catalog.create_user(email)
    cookies = {"longhouse_session": live_catalog.browser_cookie(owner_id=owner_id, email=email)}
    session_id = _seed_live_catalog_session(
        live_catalog,
        owner_id=owner_id,
        provider="codex",
        device_id="codex-machine-control",
    )
    # The engine answers the steer command the way it answers one that arrived
    # after the turn already ended.
    websocket = _TurnEndedMachineWebSocket()
    asyncio.run(
        get_machine_control_channel_registry().register(
            owner_id=owner_id,
            device_id="codex-machine-control",
            machine_name="codex-machine-control",
            engine_build="test-engine",
            supports=["codex.steer"],
            websocket=websocket,
        )
    )

    try:
        resp = live_catalog_client.post(
            f"/sessions/{session_id}/input",
            json={"text": "too late", "intent": "steer", "client_request_id": "codex-turn-ended-1"},
            cookies=cookies,
        )
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["error_code"] == "turn_ended"
        # The receipt persists as failed for audit — no silent recovery.
        receipt = _live_catalog_receipt(
            live_catalog,
            owner_id=owner_id,
            session_id=session_id,
            client_request_id="codex-turn-ended-1",
        )
        assert receipt["status"] == INPUT_STATUS_FAILED
        assert "turn_ended" in str(receipt["error_json"])
    finally:
        asyncio.run(_clear_machine_control_registry())


def test_capability_includes_can_queue_next_input():
    from tests_lite._capability_test_helper import build_session_capabilities

    session = SimpleNamespace(
        execution_home="managed_local",
        managed_transport="claude_channel_bridge",
        source_runner_id=1,
        continuation_kind=None,
        origin_label=None,
        environment=None,
    )
    caps = build_session_capabilities(session)
    assert caps.live_control_available is True
    assert caps.can_queue_next_input is True

    session_no_runner = SimpleNamespace(
        execution_home="managed_local",
        managed_transport="claude_channel_bridge",
        source_runner_id=None,
        continuation_kind=None,
        origin_label=None,
        environment=None,
    )
    caps2 = build_session_capabilities(session_no_runner)
    assert caps2.live_control_available is False
    assert caps2.can_queue_next_input is False


def test_queue_cap_rejects_over_limit(live_catalog, live_catalog_client):  # noqa: F811
    from zerg.services.session_inputs import MAX_QUEUED_PER_SESSION

    email = "live-cap@test.local"
    owner_id = live_catalog.create_user(email)
    cookies = {"longhouse_session": live_catalog.browser_cookie(owner_id=owner_id, email=email)}
    session_id = _seed_live_catalog_session(live_catalog, owner_id=owner_id)

    for index in range(MAX_QUEUED_PER_SESSION):
        queued = live_catalog_client.post(
            f"/sessions/{session_id}/input",
            json={"text": f"msg {index}", "intent": "queue", "client_request_id": f"live-cap-{index}"},
            cookies=cookies,
        )
        assert queued.status_code == 200, queued.text
    over = live_catalog_client.post(
        f"/sessions/{session_id}/input",
        json={"text": "one too many", "intent": "queue", "client_request_id": "live-cap-over"},
        cookies=cookies,
    )
    assert over.status_code == 409, over.text


def test_inputs_etag_returns_304_when_unchanged(live_catalog, live_catalog_client):  # noqa: F811
    email = "live-etag@test.local"
    owner_id = live_catalog.create_user(email)
    cookies = {"longhouse_session": live_catalog.browser_cookie(owner_id=owner_id, email=email)}
    session_id = _seed_live_catalog_session(live_catalog, owner_id=owner_id)

    # Seed one queued receipt so the list is non-trivial.
    queued = live_catalog_client.post(
        f"/sessions/{session_id}/input",
        json={"text": "etag test", "intent": "queue", "client_request_id": "live-etag-1"},
        cookies=cookies,
    )
    assert queued.status_code == 200, queued.text

    # First list call: full response + ETag header.
    first = live_catalog_client.get(f"/sessions/{session_id}/inputs", cookies=cookies)
    assert first.status_code == 200
    etag = first.headers.get("etag")
    assert etag, "expected ETag header on /inputs response"

    # Second call with If-None-Match presents the same etag → 304.
    second = live_catalog_client.get(
        f"/sessions/{session_id}/inputs",
        headers={"If-None-Match": etag},
        cookies=cookies,
    )
    assert second.status_code == 304, second.text
    assert second.headers.get("etag") == etag

    # Mutating state (cancel) invalidates the ETag.
    cancel = live_catalog_client.delete(
        f"/sessions/{session_id}/inputs/live/{queued.json()['live_input_id']}",
        cookies=cookies,
    )
    assert cancel.status_code == 200, cancel.text

    third = live_catalog_client.get(
        f"/sessions/{session_id}/inputs",
        headers={"If-None-Match": etag},
        cookies=cookies,
    )
    assert third.status_code == 200, "cancel should bust the ETag"
    assert third.headers.get("etag") != etag


def test_startup_reconciliation_fails_stuck_steer_rows_instead_of_requeuing(tmp_path):
    from datetime import timedelta

    from zerg.services.session_inputs import create_session_input
    from zerg.services.session_inputs import requeue_stuck_delivering

    session_local = _make_db(tmp_path)
    session_id, _ = _seed_live_session(session_local)

    with session_local() as db:
        steer_row = create_session_input(
            db,
            session_id=session_id,
            text="redirect now",
            intent="steer",
            status="delivering",
            client_request_id="crash-steer",
            delivery_request_id="crash-steer-delivery",
        )
        steer_row.updated_at = datetime.now(timezone.utc) - timedelta(seconds=300)
        auto_row = create_session_input(
            db,
            session_id=session_id,
            text="retryable",
            intent="auto",
            status="delivering",
            client_request_id="crash-auto",
            delivery_request_id="crash-auto-delivery",
        )
        auto_row.updated_at = datetime.now(timezone.utc) - timedelta(seconds=300)
        db.commit()

        requeued = requeue_stuck_delivering(db)
        # Only the auto row requeues; the steer row is failed so we do not
        # silently turn a corrective intent into a queued message.
        assert requeued == 1
        db.expire_all()
        steer_refreshed = db.query(SessionInput).filter(SessionInput.id == steer_row.id).one()
        auto_refreshed = db.query(SessionInput).filter(SessionInput.id == auto_row.id).one()
        assert steer_refreshed.status == INPUT_STATUS_FAILED
        assert steer_refreshed.last_error == "steer interrupted by restart"
        assert steer_refreshed.delivery_request_id == "crash-steer-delivery"
        assert auto_refreshed.status == INPUT_STATUS_QUEUED
        assert auto_refreshed.client_request_id == "crash-auto"
        assert auto_refreshed.delivery_request_id is None


def test_startup_reconciliation_rewinds_stuck_delivering(tmp_path):
    from datetime import timedelta

    from zerg.services.session_inputs import create_session_input
    from zerg.services.session_inputs import requeue_stuck_delivering

    session_local = _make_db(tmp_path)
    session_id, _ = _seed_live_session(session_local)

    with session_local() as db:
        row = create_session_input(
            db,
            session_id=session_id,
            text="stuck",
            intent="auto",
            status="delivering",
            client_request_id="old",
            delivery_request_id="old-delivery",
        )
        row.updated_at = datetime.now(timezone.utc) - timedelta(seconds=300)
        db.commit()
        requeued = requeue_stuck_delivering(db)
        assert requeued == 1
        db.expire_all()
        refreshed = db.query(SessionInput).filter(SessionInput.id == row.id).one()
        assert refreshed.status == INPUT_STATUS_QUEUED
        assert refreshed.client_request_id == "old"
        assert refreshed.delivery_request_id is None


def test_startup_reconciliation_returns_queued_sessions_for_boot_drain_idempotently(tmp_path):
    from zerg.services.session_inputs import reconcile_startup_session_inputs

    session_local = _make_db(tmp_path)
    queued_session_id, _ = _seed_live_session(session_local)
    retry_session_id, _ = _seed_live_session(session_local)
    steer_session_id, _ = _seed_live_session(session_local)
    delivered_session_id, _ = _seed_live_session(session_local)
    stale_at = datetime.now(timezone.utc) - timedelta(seconds=300)

    with session_local() as db:
        create_session_input(
            db,
            session_id=queued_session_id,
            text="already queued",
            intent="queue",
            status="queued",
            client_request_id="boot-queued",
        )
        retry_row = create_session_input(
            db,
            session_id=retry_session_id,
            text="retry at boot",
            intent="auto",
            status="delivering",
            client_request_id="boot-auto",
            delivery_request_id="boot-auto-delivery",
        )
        steer_row = create_session_input(
            db,
            session_id=steer_session_id,
            text="too late to steer",
            intent="steer",
            status="delivering",
            client_request_id="boot-steer",
            delivery_request_id="boot-steer-delivery",
        )
        create_session_input(
            db,
            session_id=delivered_session_id,
            text="already delivered",
            intent="auto",
            status="delivered",
            client_request_id="boot-delivered",
        )
        retry_row.updated_at = stale_at
        steer_row.updated_at = stale_at
        db.commit()

        first_boot = reconcile_startup_session_inputs(db)
        second_boot = reconcile_startup_session_inputs(db)

        expected = {str(queued_session_id), str(retry_session_id)}
        assert {str(session_id) for session_id in first_boot} == expected
        assert {str(session_id) for session_id in second_boot} == expected

        db.expire_all()
        retry_refreshed = db.query(SessionInput).filter(SessionInput.id == retry_row.id).one()
        steer_refreshed = db.query(SessionInput).filter(SessionInput.id == steer_row.id).one()
        assert retry_refreshed.status == INPUT_STATUS_QUEUED
        assert retry_refreshed.delivery_request_id is None
        assert steer_refreshed.status == INPUT_STATUS_FAILED
        assert steer_refreshed.last_error == "steer interrupted by restart"
