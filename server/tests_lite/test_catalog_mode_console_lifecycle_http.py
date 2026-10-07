from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.store import CatalogStore
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.dependencies.browser_auth import get_current_browser_user
from zerg.dependencies.browser_route_auth import get_current_browser_route_user
from zerg.main import api_app
from zerg.models.live_store import LiveUser
from zerg.services.session_runtime import RuntimeEventIngest


def _decode_time(value: object) -> datetime:
    assert isinstance(value, str)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class _CatalogClient:
    """Hosted-shape RPC facade backed by a real catalog store."""

    def __init__(self, store: CatalogStore) -> None:
        self.store = store

    async def call(self, method, params, *, timeout_seconds=None):
        del timeout_seconds
        if method == "session.console.create.v2":
            data = dict(params["session"])
            data["started_at"] = _decode_time(data["started_at"])
            return self.store.create_console_session(data=data)
        if method == "session.console.turn.enqueue.v2":
            data = dict(params["turn"])
            data["created_at"] = _decode_time(data["created_at"])
            return self.store.enqueue_console_turn(data=data)
        if method == "session.console.turn.update.v2":
            data = dict(params["turn"])
            data["updated_at"] = _decode_time(data["updated_at"])
            return self.store.update_console_turn(data=data)
        if method == "session.console.turn.current.v2":
            return self.store.read_current_console_turn(
                session_id=params["session_id"],
                owner_id=int(params["owner_id"]),
            )
        if method == "session.input.recent.list.v2":
            return self.store.list_recent_input_receipts(session_id=params["session_id"])
        if method == "session.input.receipts.list.v2":
            return self.store.list_session_input_receipts(session_id=params["session_id"])
        if method == "session.runtime.apply.v2":
            return self.store.apply_session_runtime(events=[RuntimeEventIngest.model_validate(event) for event in params["events"]])
        raise AssertionError(f"unexpected catalog RPC: {method}")


class _MachineRegistry:
    def __init__(self) -> None:
        self.start_transport_timeout = False
        self.crash_next_start = False
        self.interrupt_supported = True
        self.invocation_close_supported = True
        self.commands: list[dict[str, object]] = []

    @staticmethod
    def is_online(*, owner_id, device_id):
        return owner_id == 1 and device_id == "cinder"

    def supports(self, *, owner_id, device_id, capability):
        if owner_id != 1 or device_id != "cinder":
            return False
        if capability == "claude.turn_start":
            return True
        if capability == "claude.turn_interrupt":
            return self.interrupt_supported
        return capability == "claude.invocation_close" and self.invocation_close_supported

    @staticmethod
    def provider_readiness_state(*, owner_id, device_id, provider):
        return None

    async def send_command(self, **kwargs):
        self.commands.append(kwargs)
        if kwargs["command_type"] == "session.turn.start" and self.crash_next_start:
            self.crash_next_start = False
            raise RuntimeError("simulated Runtime Host crash after durable FIFO claim")
        if kwargs["command_type"] == "session.turn.start" and self.start_transport_timeout:
            return SimpleNamespace(transport_ok=False, message={}, error="control response timed out")
        return SimpleNamespace(transport_ok=True, message={"ok": True, "result": {}}, error=None)


def test_catalog_mode_http_console_lifecycle_survives_ambiguous_start_and_crash_replay(tmp_path, monkeypatch):
    from zerg.routers import agents_sessions
    from zerg.routers import runtime as runtime_router
    from zerg.routers import session_chat
    from zerg.services import catalogd_supervisor
    from zerg.services import console_sessions
    from zerg.services import live_catalog_timeline
    from zerg.services import machine_control_channel

    engine = create_catalog_engine(tmp_path / "hosted-console.db")
    initialize_catalog_schema(engine)
    with Session(engine) as db:
        db.add(LiveUser(id=1, email="owner@example.com", is_active=True))
        db.commit()
    store = CatalogStore(engine)
    catalog = _CatalogClient(store)
    registry = _MachineRegistry()

    monkeypatch.setattr(catalogd_supervisor, "get_catalogd_client", lambda: catalog)
    monkeypatch.setattr(console_sessions, "get_catalogd_client", lambda: catalog)
    monkeypatch.setattr(runtime_router, "get_catalogd_client", lambda: catalog)
    monkeypatch.setattr(machine_control_channel, "get_machine_control_channel_registry", lambda: registry)
    monkeypatch.setattr(agents_sessions, "get_machine_control_channel_registry", lambda: registry)
    monkeypatch.setattr(live_catalog_timeline, "get_machine_control_channel_registry", lambda: registry)
    monkeypatch.setattr(
        live_catalog_timeline,
        "shadow_session_state_snapshot",
        lambda session_id, owner_id: store.read_shadow_session_state(session_id=session_id, owner_id=owner_id),
    )

    api_app.dependency_overrides[verify_agents_token] = lambda: SimpleNamespace(
        owner_id=1,
        device_id="cinder",
        id="token-1",
    )
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    api_app.dependency_overrides[get_current_browser_route_user] = lambda: SimpleNamespace(id=1)
    api_app.dependency_overrides[get_current_browser_user] = lambda: SimpleNamespace(id=1)
    api_app.dependency_overrides[agents_sessions.session_detail_db_dependency] = lambda: None
    api_app.dependency_overrides[session_chat._catalog_control_db_dependency] = lambda: None
    try:
        with TestClient(api_app, raise_server_exceptions=False) as client:
            created = client.post(
                "/agents/sessions",
                json={
                    "provider": "claude",
                    "device_id": "cinder",
                    "cwd": "/tmp/longhouse",
                    "model": "claude-haiku-4-5-20251001",
                },
                headers={"X-Agents-Token": "dev"},
            )
            assert created.status_code == 201, created.text
            session_id = created.json()["session_id"]
            thread_id = created.json()["thread_id"]

            registry.start_transport_timeout = True
            uncertain = client.post(
                f"/agents/sessions/{session_id}/turns",
                json={"message": "first", "client_request_id": "http-turn-1"},
                headers={"X-Agents-Token": "dev"},
            )
            assert uncertain.status_code == 502, uncertain.text
            starting = client.get(f"/timeline/sessions/{session_id}")
            assert starting.status_code == 200, starting.text
            starting_state = starting.json()["session_state"]
            assert starting_state["run"]["lifecycle"] == "starting"
            assert starting_state["working_set"] == "open"
            assert starting_state["presentation"]["primary"]["label"] == "Starting"

            registry.start_transport_timeout = False
            first = client.post(
                f"/agents/sessions/{session_id}/turns",
                json={"message": "first", "client_request_id": "http-turn-1"},
                headers={"X-Agents-Token": "dev"},
            )
            assert first.status_code == 202, first.text
            assert first.json()["state"] == "active"
            assert registry.commands[-1]["payload"]["model"] == "claude-haiku-4-5-20251001"
            first_run_id = first.json()["run_id"]
            working = client.get(f"/timeline/sessions/{session_id}")
            assert working.status_code == 200, working.text
            working_state = working.json()["session_state"]
            assert working_state["run"]["lifecycle"] == "running"
            assert working_state["working_set"] == "open"
            assert working_state["presentation"]["primary"]["label"] == "Working"
            assert working_state["control"]["actions"]["interrupt"]["state"] == "available"

            supported_interrupt = client.post(f"/sessions/{session_id}/turns/current/interrupt")
            assert supported_interrupt.status_code == 200, supported_interrupt.text
            registry.interrupt_supported = False
            unsupported_interrupt = client.post(f"/sessions/{session_id}/turns/current/interrupt")
            assert unsupported_interrupt.status_code == 409, unsupported_interrupt.text
            assert unsupported_interrupt.json()["detail"]["code"] == "adapter_unavailable"
            registry.interrupt_supported = True

            second = client.post(
                f"/agents/sessions/{session_id}/turns",
                json={"message": "second", "client_request_id": "http-turn-2"},
                headers={"X-Agents-Token": "dev"},
            )
            assert second.status_code == 202, second.text
            assert second.json()["state"] == "queued"
            assert second.json()["run_id"] is None

            terminal_event = {
                "events": [
                    {
                        "runtime_key": f"claude:{session_id}",
                        "session_id": session_id,
                        "thread_id": thread_id,
                        "run_id": first_run_id,
                        "provider": "claude",
                        "device_id": "cinder",
                        "source": "claude_hook",
                        "kind": "terminal_signal",
                        "occurred_at": datetime.now(timezone.utc).isoformat(),
                        "dedupe_key": f"terminal:{first_run_id}:completed",
                        "payload": {"terminal_state": "run_completed"},
                    }
                ]
            }
            registry.crash_next_start = True
            crashed = client.post(
                "/agents/runtime/events/batch",
                json=terminal_event,
                headers={"X-Agents-Token": "dev"},
            )
            assert crashed.status_code == 500, crashed.text
            crashed_command_id = registry.commands[-1]["command_id"]

            replayed = client.post(
                "/agents/runtime/events/batch",
                json=terminal_event,
                headers={"X-Agents-Token": "dev"},
            )
            assert replayed.status_code == 200, replayed.text
            assert registry.commands[-1]["command_id"] == crashed_command_id

            recovered = client.get(f"/timeline/sessions/{session_id}")
            assert recovered.status_code == 200, recovered.text
            recovered_state = recovered.json()["session_state"]
            assert recovered_state["run"]["lifecycle"] == "running"
            assert recovered_state["working_set"] == "open"
            assert recovered_state["presentation"]["primary"]["label"] == "Working"
    finally:
        api_app.dependency_overrides.clear()
        engine.dispose()


def test_catalog_mode_http_wake_signal_dispatches_once_after_completed_turn(tmp_path, monkeypatch):
    from uuid import uuid4

    from zerg.models.live_store import LiveSessionThreadAlias
    from zerg.routers import agents_sessions
    from zerg.routers import runtime as runtime_router
    from zerg.services import catalogd_supervisor
    from zerg.services import console_sessions
    from zerg.services import machine_control_channel

    engine = create_catalog_engine(tmp_path / "hosted-console-wake.db")
    initialize_catalog_schema(engine)
    with Session(engine) as db:
        db.add(LiveUser(id=1, email="owner@example.com", is_active=True))
        db.commit()
    store = CatalogStore(engine)
    catalog = _CatalogClient(store)
    registry = _MachineRegistry()

    monkeypatch.setattr(catalogd_supervisor, "get_catalogd_client", lambda: catalog)
    monkeypatch.setattr(console_sessions, "get_catalogd_client", lambda: catalog)
    monkeypatch.setattr(runtime_router, "get_catalogd_client", lambda: catalog)
    monkeypatch.setattr(machine_control_channel, "get_machine_control_channel_registry", lambda: registry)
    monkeypatch.setattr(agents_sessions, "get_machine_control_channel_registry", lambda: registry)

    api_app.dependency_overrides[verify_agents_token] = lambda: SimpleNamespace(
        owner_id=1,
        device_id="cinder",
        id="token-1",
    )
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    try:
        with TestClient(api_app, raise_server_exceptions=False) as client:
            created = client.post(
                "/agents/sessions",
                json={"provider": "claude", "device_id": "cinder", "cwd": "/tmp/longhouse"},
                headers={"X-Agents-Token": "dev"},
            )
            assert created.status_code == 201, created.text
            session_id = created.json()["session_id"]
            thread_id = created.json()["thread_id"]

            first_request_id = "wake-route-first"
            first = client.post(
                f"/agents/sessions/{session_id}/turns",
                json={"message": "first response", "client_request_id": first_request_id},
                headers={"X-Agents-Token": "dev"},
            )
            assert first.status_code == 202, first.text

            provider_thread_id = "provider-thread-wake-route"
            now = datetime.now(timezone.utc)
            with Session(engine) as db:
                db.add(
                    LiveSessionThreadAlias(
                        thread_id=thread_id,
                        provider="claude",
                        alias_kind="provider_session_id",
                        alias_value=provider_thread_id,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )
                db.commit()

            invocation_id = str(uuid4())

            def complete_turn(turn, client_request_id: str) -> None:
                run_id = turn.json()["run_id"]
                terminal = {
                    "events": [
                        {
                            "runtime_key": f"claude:{session_id}",
                            "session_id": session_id,
                            "thread_id": thread_id,
                            "run_id": run_id,
                            "provider": "claude",
                            "device_id": "cinder",
                            "source": "claude_print",
                            "kind": "terminal_signal",
                            "occurred_at": datetime.now(timezone.utc).isoformat(),
                            "dedupe_key": f"claude-print:{session_id}:{run_id}:terminal",
                            "payload": {
                                "managed_transport": "claude_print",
                                "execution_lifetime": "persistent",
                                "terminal_state": "run_completed",
                                "terminal_reason": "completed",
                                "terminal_source": "claude_print",
                                "exit_code": 0,
                                "stderr_tail": None,
                                "turn_id": turn.json()["turn_id"],
                                "client_request_id": client_request_id,
                                "provider_thread_id": provider_thread_id,
                                "invocation": {
                                    "id": invocation_id,
                                    "state": "parked",
                                    "pending_count": 1,
                                },
                            },
                        }
                    ]
                }
                response = client.post(
                    "/agents/runtime/events/batch",
                    json=terminal,
                    headers={"X-Agents-Token": "dev"},
                )
                assert response.status_code == 200, response.text

            complete_turn(first, first_request_id)
            resumed_request_id = "wake-route-resume"
            resumed = client.post(
                f"/agents/sessions/{session_id}/turns",
                json={"message": "resume the parked session", "client_request_id": resumed_request_id},
                headers={"X-Agents-Token": "dev"},
            )
            assert resumed.status_code == 202, resumed.text
            user_resume_identity = registry.commands[-1]["payload"]["resume_provider_thread_id"]
            assert user_resume_identity == provider_thread_id
            complete_turn(resumed, resumed_request_id)

            wake_id = f"{invocation_id}:1"
            wake = {
                "events": [
                    {
                        "runtime_key": f"claude:{session_id}",
                        "session_id": session_id,
                        "thread_id": thread_id,
                        "provider": "claude",
                        "device_id": "cinder",
                        "source": "claude_console",
                        "kind": "wake_signal",
                        "occurred_at": datetime.now(timezone.utc).isoformat(),
                        "dedupe_key": f"wake:{wake_id}",
                        "payload": {
                            "invocation_id": invocation_id,
                            "wake_id": wake_id,
                            "provider_thread_id": provider_thread_id,
                            "trigger": {
                                "kind": "monitor_event",
                                "task_ids": ["monitor-1"],
                                "summary": "the branch is ready",
                            },
                        },
                    }
                ]
            }
            wake_response = client.post(
                "/agents/runtime/events/batch",
                json=wake,
                headers={"X-Agents-Token": "dev"},
            )
            assert wake_response.status_code == 200, wake_response.text
            wake_command = registry.commands[-1]
            assert wake_command["command_type"] == "session.turn.start"
            assert wake_command["payload"]["origin"] == "wake"
            assert "launch_actor" not in wake_command["payload"]
            assert wake_command["payload"]["wake_id"] == wake_id
            assert wake_command["payload"]["invocation_id"] == invocation_id
            assert wake_command["payload"]["message"] == ""
            assert wake_command["payload"]["resume_provider_thread_id"] == user_resume_identity

            wake_request_id = f"wake:{wake_id}"
            detail_receipts = store.list_session_input_receipts(session_id=session_id)["receipts"]
            wake_detail = next(row for row in detail_receipts if row["client_request_id"] == wake_request_id)
            assert wake_detail["origin"] == "wake"
            assert wake_detail["text"] == "Monitor event: the branch is ready"
            live_receipts = store.list_recent_input_receipts(session_id=session_id)["receipts"]
            wake_live = next(row for row in live_receipts if row["client_request_id"] == wake_request_id)
            assert wake_live["turn"]["origin"] == "wake"

            dispatched_count = len(registry.commands)
            duplicate = client.post(
                "/agents/runtime/events/batch",
                json=wake,
                headers={"X-Agents-Token": "dev"},
            )
            assert duplicate.status_code == 200, duplicate.text
            assert len(registry.commands) == dispatched_count
    finally:
        api_app.dependency_overrides.clear()
        engine.dispose()


def _catalog_http_stack(tmp_path, monkeypatch, *, name: str):
    from zerg.routers import agents_sessions
    from zerg.routers import runtime as runtime_router
    from zerg.routers import session_chat
    from zerg.services import catalogd_supervisor
    from zerg.services import console_sessions
    from zerg.services import live_catalog_timeline
    from zerg.services import machine_control_channel

    engine = create_catalog_engine(tmp_path / f"{name}.db")
    initialize_catalog_schema(engine)
    with Session(engine) as db:
        db.add(LiveUser(id=1, email="owner@example.com", is_active=True))
        db.commit()
    store = CatalogStore(engine)
    catalog = _CatalogClient(store)
    registry = _MachineRegistry()
    monkeypatch.setattr(catalogd_supervisor, "get_catalogd_client", lambda: catalog)
    monkeypatch.setattr(console_sessions, "get_catalogd_client", lambda: catalog)
    monkeypatch.setattr(runtime_router, "get_catalogd_client", lambda: catalog)
    monkeypatch.setattr(machine_control_channel, "get_machine_control_channel_registry", lambda: registry)
    monkeypatch.setattr(agents_sessions, "get_machine_control_channel_registry", lambda: registry)
    monkeypatch.setattr(live_catalog_timeline, "get_machine_control_channel_registry", lambda: registry)
    monkeypatch.setattr(
        live_catalog_timeline,
        "shadow_session_state_snapshot",
        lambda session_id, owner_id: store.read_shadow_session_state(session_id=session_id, owner_id=owner_id),
    )
    api_app.dependency_overrides[verify_agents_token] = lambda: SimpleNamespace(
        owner_id=1,
        device_id="cinder",
        id="token-1",
    )
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    api_app.dependency_overrides[get_current_browser_route_user] = lambda: SimpleNamespace(id=1)
    api_app.dependency_overrides[get_current_browser_user] = lambda: SimpleNamespace(id=1)
    api_app.dependency_overrides[agents_sessions.session_detail_db_dependency] = lambda: None
    api_app.dependency_overrides[session_chat._catalog_control_db_dependency] = lambda: None
    return engine, store, registry


def _create_parked_console_session(client, provider="claude"):
    headers = {"X-Agents-Token": "dev"}
    created = client.post(
        "/agents/sessions",
        json={"provider": provider, "device_id": "cinder", "cwd": "/tmp/longhouse"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["session_id"]
    thread_id = created.json()["thread_id"]
    turn = client.post(
        f"/agents/sessions/{session_id}/turns",
        json={"message": "start background work", "client_request_id": "parked-stop-turn"},
        headers=headers,
    )
    assert turn.status_code == 202, turn.text
    run_id = turn.json()["run_id"]
    now = datetime.now(timezone.utc)
    for kind, dedupe_key, occurred_at, payload in (
        ("terminal_signal", f"terminal:{run_id}", now, {"terminal_state": "run_completed"}),
        (
            "delegation_signal",
            f"delegation:{run_id}",
            now,
            {
                "delegation": {
                    "count": 2,
                    "kinds": {"monitor": 1, "shell": 1},
                    "items": [
                        {"id": "watch-branch", "kind": "monitor", "status": "running", "description": "watch the branch"},
                        {
                            "id": "integration-tests",
                            "kind": "shell",
                            "status": "running",
                            "description": "run the integration tests",
                        },
                    ],
                    "recent_items": [],
                    "observed_at": now.isoformat(),
                }
            },
        ),
    ):
        response = client.post(
            "/agents/runtime/events/batch",
            json={
                "events": [
                    {
                        "runtime_key": f"{provider}:{session_id}",
                        "session_id": session_id,
                        "thread_id": thread_id,
                        "run_id": run_id,
                        "provider": provider,
                        "device_id": "cinder",
                        "source": "codex_app_server" if provider == "codex" else "claude_console",
                        "kind": kind,
                        "occurred_at": occurred_at.isoformat(),
                        "dedupe_key": dedupe_key,
                        "payload": payload,
                    }
                ]
            },
            headers=headers,
        )
        assert response.status_code == 200, response.text
    return session_id, thread_id, run_id


def test_catalog_mode_http_interrupt_closes_parked_invocation_and_refuses_idle(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    engine, _store, registry = _catalog_http_stack(tmp_path, monkeypatch, name="console-stop")
    try:
        with TestClient(api_app, raise_server_exceptions=False) as client:
            session_id, thread_id, run_id = _create_parked_console_session(client)
            stopped = client.post(f"/sessions/{session_id}/turns/current/interrupt")
            assert stopped.status_code == 200, stopped.text
            command = registry.commands[-1]
            assert command["command_type"] == "session.invocation.close"
            assert command["command_id"] == f"{run_id}:close"
            assert command["payload"] == {
                "provider": "claude",
                "run_id": run_id,
                "thread_id": thread_id,
                "reason": "user_stop",
            }

            empty_at = datetime.now(timezone.utc)
            emptied = client.post(
                "/agents/runtime/events/batch",
                json={
                    "events": [
                        {
                            "runtime_key": f"claude:{session_id}",
                            "session_id": session_id,
                            "thread_id": thread_id,
                            "run_id": run_id,
                            "provider": "claude",
                            "device_id": "cinder",
                            "source": "claude_console",
                            "kind": "delegation_signal",
                            "occurred_at": empty_at.isoformat(),
                            "dedupe_key": f"delegation:{run_id}:empty",
                            "payload": {
                                "delegation": {
                                    "count": 0,
                                    "kinds": {},
                                    "items": [],
                                    "recent_items": [],
                                    "observed_at": empty_at.isoformat(),
                                }
                            },
                        }
                    ]
                },
                headers={"X-Agents-Token": "dev"},
            )
            assert emptied.status_code == 200, emptied.text
            idle = client.post(f"/sessions/{session_id}/turns/current/interrupt")
            assert idle.status_code == 409, idle.text
            assert idle.json()["detail"]["code"] == "no_active_turn"
    finally:
        api_app.dependency_overrides.clear()
        engine.dispose()


@pytest.mark.parametrize(("provider", "reason"), [("claude", "user_stop"), ("codex", "machine_agent_restart")])
def test_catalog_mode_http_invocation_closed_event_records_one_longhouse_notice(tmp_path, monkeypatch, provider, reason):
    from fastapi.testclient import TestClient
    from zerg.models.live_store import LiveConsoleTurn
    from zerg.models.live_store import LiveRuntimeState
    from zerg.models.live_store import LiveSessionCatalog
    from zerg.models.live_store import LiveSessionRun

    engine, store, registry = _catalog_http_stack(tmp_path, monkeypatch, name="console-close-notice")
    try:
        with TestClient(api_app, raise_server_exceptions=False) as client:
            session_id, thread_id, run_id = _create_parked_console_session(client, provider)
            with Session(engine) as db:
                prior_runtime = db.get(LiveRuntimeState, f"{provider}:{session_id}")
                prior_run = db.get(LiveSessionRun, run_id)
                prior_session = db.get(LiveSessionCatalog, session_id)
                original_outcome = (
                    prior_runtime.terminal_state,
                    prior_runtime.terminal_reason,
                    prior_runtime.terminal_source,
                    prior_runtime.terminal_at,
                    prior_run.exit_status,
                    prior_run.ended_at,
                    prior_session.last_console_result_outcome,
                    prior_session.closed_at,
                )
            invocation_id = "invocation-close-http-test"
            occurred_at = datetime.now(timezone.utc).isoformat()
            close_event = {
                "events": [
                    {
                        "runtime_key": f"{provider}:{session_id}",
                        "session_id": session_id,
                        "thread_id": thread_id,
                        "run_id": run_id,
                        "provider": provider,
                        "device_id": "cinder",
                        "source": "codex_app_server" if provider == "codex" else "claude_console",
                        "kind": "invocation_closed",
                        "occurred_at": occurred_at,
                        "dedupe_key": f"close:{invocation_id}",
                        "payload": {
                            "invocation_id": invocation_id,
                            "reason": reason,
                            "stopped": [
                                {"id": "watch-branch", "kind": "monitor", "description": "watch the branch"},
                                {
                                    "id": "integration-tests",
                                    "kind": "shell",
                                    "description": "run the integration tests",
                                },
                            ],
                        },
                    }
                ]
            }
            headers = {"X-Agents-Token": "dev"}
            first = client.post("/agents/runtime/events/batch", json=close_event, headers=headers)
            duplicate = client.post("/agents/runtime/events/batch", json=close_event, headers=headers)
            assert first.status_code == 200, first.text
            assert duplicate.status_code == 200, duplicate.text

            receipts = store.list_recent_input_receipts(session_id=session_id)["receipts"]
            notices = [row for row in receipts if row.get("client_request_id") == f"close:{invocation_id}"]
            assert len(notices) == 1
            assert notices[0]["origin"] == "longhouse"
            assert notices[0]["turn"] is None
            with Session(engine) as db:
                assert db.query(LiveConsoleTurn).filter(LiveConsoleTurn.session_id == session_id).count() == 1
                after_runtime = db.get(LiveRuntimeState, f"{provider}:{session_id}")
                after_run = db.get(LiveSessionRun, run_id)
                after_session = db.get(LiveSessionCatalog, session_id)
                assert (
                    after_runtime.terminal_state,
                    after_runtime.terminal_reason,
                    after_runtime.terminal_source,
                    after_runtime.terminal_at,
                    after_run.exit_status,
                    after_run.ended_at,
                    after_session.last_console_result_outcome,
                    after_session.closed_at,
                ) == original_outcome
            assert len(registry.commands) == 1
    finally:
        api_app.dependency_overrides.clear()
        engine.dispose()
