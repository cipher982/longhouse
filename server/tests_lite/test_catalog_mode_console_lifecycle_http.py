from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg import database as database_module
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
        if method == "session.runtime.apply.v2":
            return self.store.apply_session_runtime(
                events=[RuntimeEventIngest.model_validate(event) for event in params["events"]]
            )
        raise AssertionError(f"unexpected catalog RPC: {method}")


class _MachineRegistry:
    def __init__(self) -> None:
        self.start_transport_timeout = False
        self.crash_next_start = False
        self.interrupt_supported = True
        self.commands: list[dict[str, object]] = []

    @staticmethod
    def is_online(*, owner_id, device_id):
        return owner_id == 1 and device_id == "cinder"

    def supports(self, *, owner_id, device_id, capability):
        if owner_id != 1 or device_id != "cinder":
            return False
        if capability == "claude.turn_start":
            return True
        return capability == "claude.turn_interrupt" and self.interrupt_supported

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

    monkeypatch.setattr(database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(runtime_router, "live_catalog_enabled", lambda: True)
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
                json={"provider": "claude", "device_id": "cinder", "cwd": "/tmp/longhouse"},
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
