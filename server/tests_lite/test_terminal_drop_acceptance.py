"""Acceptance: a terminal lost in transit must make the Console check go red.

Every other test here proves a fix works. This one proves the *detection* works,
which is the thing the ten-hour wedge actually lacked -- the archive was correct,
the claim read `terminal`, and nothing anywhere turned red for ten hours.

The turn runs through the real routes, the real catalog store, and the real
session-state projector. The terminal signal is then simply never delivered,
which is what "dropped in transit" looks like from the server's side: the run
finished on the machine and the event never arrived.

The predicate is imported from the live harness rather than restated, so the two
cannot drift. A copy here that slowly diverged from what
`console-served-state-e2e` actually asserts would reproduce the exact defect
class this epic exists to close.
"""

from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg import database as database_module  # noqa: E402
from zerg.catalogd.schema import create_catalog_engine  # noqa: E402
from zerg.catalogd.schema import initialize_catalog_schema  # noqa: E402
from zerg.catalogd.store import CatalogStore  # noqa: E402
from zerg.dependencies.agents_auth import require_single_tenant  # noqa: E402
from zerg.dependencies.agents_auth import verify_agents_token  # noqa: E402
from zerg.dependencies.browser_auth import get_current_browser_user  # noqa: E402
from zerg.dependencies.browser_route_auth import get_current_browser_route_user  # noqa: E402
from zerg.main import api_app  # noqa: E402
from zerg.models.live_store import LiveUser  # noqa: E402
from zerg.services.session_runtime import RuntimeEventIngest  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_live_harness():
    """Import the shipped predicates so this asserts on the real ones.

    These live in the core module that both the hand-run harness and the factory
    producer `zerg.qa.console_served_state` import, so this test, the script and
    the factory cannot drift into asserting different things.
    """
    from zerg.qa import console_served_state_core

    return console_served_state_core


HARNESS = _load_live_harness()
CONSOLE_PROVIDERS = HARNESS.console_providers()


def _decode_time(value: object) -> datetime:
    assert isinstance(value, str)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class _CatalogClient:
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
            return self.store.apply_session_runtime(events=[RuntimeEventIngest.model_validate(event) for event in params["events"]])
        raise AssertionError(f"unexpected catalog RPC: {method}")


class _MachineRegistry:
    """Advertises turn_start for whichever provider is under test."""

    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.commands: list[dict[str, object]] = []

    @staticmethod
    def is_online(*, owner_id, device_id):
        return owner_id == 1 and device_id == "cinder"

    def supports(self, *, owner_id, device_id, capability):
        if owner_id != 1 or device_id != "cinder":
            return False
        return capability == f"{self.provider}.turn_start"

    async def send_command(self, **kwargs):
        self.commands.append(kwargs)
        return SimpleNamespace(transport_ok=True, message={"ok": True, "result": {}}, error=None)


def _settled(client: TestClient, session_id: str, run_id: str) -> tuple[bool, dict]:
    response = client.get(f"/timeline/sessions/{session_id}")
    assert response.status_code == 200, response.text
    # The harness reads a workspace envelope; the served session is the same object.
    return HARNESS.settlement_state({"session": response.json()}, run_id)


def test_the_console_provider_set_is_derived_and_non_empty():
    """Per-provider proof belongs in the live harness, not here.

    This ran once per provider until two reviews pointed out the obvious: the
    projector does not branch on provider, so six runs of a provider-agnostic
    projection prove nothing six times. Assert the derivation instead, and let
    `console-served-state-e2e --provider all` carry the real per-provider proof
    where actual adapters run.
    """
    assert CONSOLE_PROVIDERS, "the schema must yield at least one Console provider"
    assert "codex" in CONSOLE_PROVIDERS


def test_a_terminal_lost_in_transit_keeps_the_console_check_red(tmp_path, monkeypatch):
    provider = "codex"
    from zerg.routers import agents_sessions
    from zerg.routers import runtime as runtime_router
    from zerg.routers import session_chat
    from zerg.services import catalogd_supervisor
    from zerg.services import console_sessions
    from zerg.services import live_catalog_timeline
    from zerg.services import machine_control_channel

    engine = create_catalog_engine(tmp_path / f"terminal-drop-{provider}.db")
    initialize_catalog_schema(engine)
    with Session(engine) as db:
        db.add(LiveUser(id=1, email="owner@example.com", is_active=True))
        db.commit()
    store = CatalogStore(engine)
    catalog = _CatalogClient(store)
    registry = _MachineRegistry(provider)

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

    api_app.dependency_overrides[verify_agents_token] = lambda: SimpleNamespace(owner_id=1, device_id="cinder", id="token-1")
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    api_app.dependency_overrides[get_current_browser_route_user] = lambda: SimpleNamespace(id=1)
    api_app.dependency_overrides[get_current_browser_user] = lambda: SimpleNamespace(id=1)
    api_app.dependency_overrides[agents_sessions.session_detail_db_dependency] = lambda: None
    api_app.dependency_overrides[session_chat._catalog_control_db_dependency] = lambda: None
    try:
        with TestClient(api_app, raise_server_exceptions=False) as client:
            created = client.post(
                "/agents/sessions",
                json={"provider": provider, "device_id": "cinder", "cwd": "/tmp/longhouse"},
                headers={"X-Agents-Token": "dev"},
            )
            assert created.status_code == 201, created.text
            session_id = created.json()["session_id"]
            thread_id = created.json()["thread_id"]

            started = client.post(
                f"/agents/sessions/{session_id}/turns",
                json={"message": "acceptance", "client_request_id": f"drop-{uuid4()}"},
                headers={"X-Agents-Token": "dev"},
            )
            assert started.status_code in {200, 202}, started.text
            run_id = started.json()["run_id"]
            assert run_id, started.text

            terminal = {
                "runtime_key": f"{provider}:{session_id}",
                "session_id": session_id,
                "thread_id": thread_id,
                "run_id": run_id,
                "provider": provider,
                "device_id": "cinder",
                "source": f"{provider}_console",
                "kind": "terminal_signal",
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "dedupe_key": f"terminal:{run_id}",
                "payload": {"terminal_state": "run_completed", "exit_code": 0},
            }

            # The drop. The turn finished on the machine; the event never arrives.
            settled, observed = _settled(client, session_id, run_id)

            # Pin the reproduction, not just the verdict. This is the wedge as a
            # viewer saw it for ten hours, and asserting the literal label is what
            # makes this a regression test for the incident rather than a test of
            # whatever the predicate happens to check today.
            assert observed["display_phase"] == "Working", observed
            assert observed["run_lifecycle"] == "running", observed
            assert observed["working_set"] == "open", observed
            assert observed["presentation_key"] == "executing", observed

            assert not settled, (
                f"{provider}: a lost terminal must leave the check red, but the served state already reads settled: {observed}"
            )

            # And the same predicate must go green once the terminal is delivered,
            # or the assertion above would pass for any reason at all.
            applied = client.post(
                "/agents/runtime/events/batch",
                json={"events": [terminal]},
                headers={"X-Agents-Token": "dev"},
            )
            assert applied.status_code == 200, applied.text

            settled, observed = _settled(client, session_id, run_id)
            assert observed["display_phase"] == "Ended", observed
            assert observed["working_set"] == "history", observed
            assert settled, f"{provider}: a delivered terminal must settle the served state, got {observed}"
    finally:
        api_app.dependency_overrides.clear()
        engine.dispose()
