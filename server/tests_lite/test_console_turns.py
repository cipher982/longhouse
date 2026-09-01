from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

import zerg.services.console_sessions as console_sessions
from tests_lite.live_catalog_harness import live_catalog  # noqa: F401
from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentSession
from zerg.services.console_sessions import create_empty_console_session
from zerg.services.console_turns import dispatch_catalog_claimed_turn
from zerg.services.console_turns import reconcile_starting_console_turns_for_device
from zerg.services.session_turns import SESSION_TURN_STATE_ACTIVE
from zerg.services.session_turns import SESSION_TURN_STATE_FAILED
from zerg.services.session_turns import SESSION_TURN_STATE_STARTING


def _db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'console-turns.db'}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)()


def _session(db):
    session = AgentSession(
        id=uuid4(),
        provider="codex",
        environment="test",
        project="longhouse",
        started_at=datetime.now(timezone.utc),
        user_messages=0,
        assistant_messages=0,
        tool_calls=0,
    )
    db.add(session)
    db.flush()
    return session


@pytest.mark.asyncio
async def test_create_empty_console_session_has_target_but_no_run(live_catalog):
    owner_id = live_catalog.create_user("console-target@test.local")

    created = await create_empty_console_session(
        None,
        owner_id=owner_id,
        provider="codex",
        device_id="cinder",
        cwd="/tmp/longhouse",
    )

    facts = live_catalog.rpc("session.read.v2", {"session_id": str(created.session_id)})["facts"]
    assert created.created is True
    assert facts["primary_thread"]["id"] == str(created.thread_id)
    assert facts["primary_thread"]["device_id"] == "cinder"
    assert facts["primary_thread"]["cwd"] == "/tmp/longhouse"
    # Identity first: a Console session exists and is addressable before any
    # provider run is started against it.
    assert facts["latest_run"] is None


@pytest.mark.asyncio
async def test_test_surface_console_session_is_automation_hidden(live_catalog):
    owner_id = live_catalog.create_user("console-automation@test.local")

    created = await create_empty_console_session(
        None,
        owner_id=owner_id,
        provider="codex",
        device_id="provider-factory-resume",
        cwd="/tmp/provider-factory",
        launch_surface="test",
    )

    catalog = live_catalog.rpc("session.read.v2", {"session_id": str(created.session_id)})["facts"]["catalog"]
    assert catalog["environment"] == "test"
    assert catalog["origin_kind"] == "console"
    assert catalog["hidden_from_default_timeline"] == 1
    assert catalog["launch_actor"] == "automation"
    assert catalog["launch_surface"] == "test"


@pytest.mark.asyncio
async def test_catalog_console_create_uses_human_facing_write_budget(monkeypatch):
    observed: dict[str, object] = {}

    class Catalog:
        async def call(self, method, params, *, timeout_seconds=None):
            observed.update(method=method, params=params, timeout_seconds=timeout_seconds)
            return {"created": True}

    monkeypatch.setattr(console_sessions, "get_catalogd_client", lambda: Catalog())

    created = await create_empty_console_session(
        None,
        owner_id=1,
        provider="codex",
        device_id="cinder",
        cwd="/tmp/longhouse",
    )

    assert created.created is True
    assert observed["method"] == "session.console.create.v2"
    assert observed["timeout_seconds"] == console_sessions.CONSOLE_CREATE_CATALOG_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_control_reconnect_replays_live_catalog_turn_with_same_run_id(monkeypatch):
    session_id = uuid4()
    thread_id = uuid4()
    turn_id = uuid4()
    run_id = uuid4()
    calls = []
    turn = {
        "turn_id": str(turn_id),
        "session_id": str(session_id),
        "thread_id": str(thread_id),
        "run_id": str(run_id),
        "state": SESSION_TURN_STATE_STARTING,
        "provider": "claude",
        "device_id": "cube",
        "cwd": "/tmp/longhouse",
        "message": "Continue after catalog reconnect",
        "client_request_id": "catalog-reconnect-request",
        "provider_config": {"permission_mode": "bypass"},
        "resume_provider_thread_id": None,
    }

    class Catalog:
        async def call(self, method, params):
            calls.append((method, params))
            if method == "session.console.turn.starting_for_device.v2":
                return {"turns": [turn], "commit_seq": "7"}
            assert method == "session.console.turn.update.v2"
            assert params["turn"]["run_id"] == str(run_id)
            assert params["turn"]["expected_state"] == SESSION_TURN_STATE_STARTING
            return {
                "found": True,
                "applied": True,
                "stale": False,
                "turn": {**turn, "state": SESSION_TURN_STATE_ACTIVE},
                "next_turn": None,
                "commit_seq": "8",
            }

    class Registry:
        command = None

        def supports(self, **_kwargs):
            return True

        async def send_command(self, **kwargs):
            self.command = kwargs
            return SimpleNamespace(transport_ok=True, message={"ok": True, "result": {}}, error=None)

    catalog = Catalog()
    registry = Registry()
    monkeypatch.setattr("zerg.services.catalogd_supervisor.get_catalogd_client", lambda: catalog)

    outcomes = await reconcile_starting_console_turns_for_device(
        None,
        owner_id=1,
        device_id="cube",
        registry=registry,
    )

    assert [outcome.state for outcome in outcomes] == [SESSION_TURN_STATE_ACTIVE]
    assert registry.command["command_id"] == str(run_id)
    assert registry.command["payload"]["run_id"] == str(run_id)
    assert [method for method, _params in calls] == [
        "session.console.turn.starting_for_device.v2",
        "session.console.turn.update.v2",
    ]


@pytest.mark.asyncio
async def test_catalog_dispatch_failure_releases_and_attempts_next_claimed_turn():
    first_run = uuid4()
    second_run = uuid4()
    first_turn = uuid4()
    second_turn = uuid4()
    session_id = uuid4()
    thread_id = uuid4()

    def payload(turn_id, run_id, message):
        return {
            "turn_id": str(turn_id),
            "run_id": str(run_id),
            "session_id": str(session_id),
            "thread_id": str(thread_id),
            "provider": "codex",
            "device_id": "offline",
            "cwd": "/tmp/longhouse",
            "message": message,
            "provider_config": {},
        }

    class Registry:
        def supports(self, **_kwargs):
            return False

    class Catalog:
        calls = []

        async def call(self, _method, params):
            self.calls.append(params["turn"])
            if len(self.calls) == 1:
                return {"next_turn": payload(second_turn, second_run, "second")}
            return {"next_turn": None}

    catalog = Catalog()
    result = await dispatch_catalog_claimed_turn(
        owner_id=1,
        turn=payload(first_turn, first_run, "first"),
        client=catalog,
        registry=Registry(),
    )

    assert result.state == SESSION_TURN_STATE_FAILED
    assert [call["run_id"] for call in catalog.calls] == [str(first_run), str(second_run)]
