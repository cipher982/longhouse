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

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from tests_lite.live_catalog_harness import LiveCatalog  # noqa: E402
from tests_lite.live_catalog_harness import live_catalog  # noqa: E402,F401
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: E402,F401
from zerg.database import initialize_database  # noqa: E402
from zerg.database import make_engine  # noqa: E402
from zerg.database import make_sessionmaker  # noqa: E402
from zerg.models.enums import UserRole  # noqa: E402
from zerg.models.user import User  # noqa: E402
from zerg.routers import session_chat  # noqa: E402
from zerg.services.machine_control_channel import get_machine_control_channel_registry  # noqa: E402
from zerg.services.session_chat_impl import _managed_local_launch_response  # noqa: E402

# Every route below is one a Runtime Host serves, so each of them declares a
# live catalog. The archive-only helpers left here belong to the two launch
# response tests, which never touch a route.


def _make_db(tmp_path):
    db_path = tmp_path / "test_session_chat.db"
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)
    return make_sessionmaker(engine)


def test_remote_helm_continue_routes_are_not_registered():
    from zerg.main import api_app

    routes = {(route.path, method) for route in api_app.routes for method in getattr(route, "methods", set())}

    assert ("/api/sessions/{session_id}/continue", "POST") not in routes
    assert ("/api/agents/sessions/{session_id}/continue", "POST") not in routes


# ---------------------------------------------------------------------------
# One managed-local session, launched the way a Runtime Host launches it
# ---------------------------------------------------------------------------


class _AutoCompletingMachineWebSocket:
    """A Machine Agent control channel that answers every command with success."""

    def __init__(self, *, exit_code: int = 0, stderr: str = ""):
        self.sent: list[dict[str, object]] = []
        self._exit_code = exit_code
        self._stderr = stderr

    async def send_json(self, message):
        self.sent.append(message)
        await get_machine_control_channel_registry().complete_command(
            {
                "type": "command_result",
                "command_id": message["command_id"],
                "ok": True,
                "result": {
                    "exit_code": self._exit_code,
                    "stdout": "",
                    "stderr": self._stderr,
                    "turn_id": "machine-control-turn-1",
                },
            }
        )


async def _clear_machine_control_registry() -> None:
    await get_machine_control_channel_registry().clear_for_tests()


async def _register_fake_machine_control(
    *,
    owner_id: int,
    device_id: str,
    supports: list[str],
    exit_code: int = 0,
    stderr: str = "",
) -> _AutoCompletingMachineWebSocket:
    websocket = _AutoCompletingMachineWebSocket(exit_code=exit_code, stderr=stderr)
    await get_machine_control_channel_registry().register(
        owner_id=owner_id,
        device_id=device_id,
        machine_name=device_id,
        engine_build="test-engine",
        supports=supports,
        websocket=websocket,
    )
    return websocket


def _machine_heartbeat(*, device_id: str, now: datetime, raw_json: str | None) -> dict:
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
    connection and carries the operations the adapter actually grants; without
    it every capability check answers "no live control channel", because the
    connection a launch creates is born detached for every lease-observed
    provider. The activity fact is what reports the session idle.
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
        "granted_operations": ["interrupt", "resume", "send_input", "tail_output", "terminate"],
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


def _launch_managed_local_session(
    live: LiveCatalog,
    *,
    owner_id: int,
    provider: str = "claude",
    device_id: str = "cinder",
    project: str = "session-chat",
    attach: bool = True,
) -> tuple[str, str]:
    """Launch one Helm session in the live catalog; return ``(session_id, run_id)``.

    The production sequence: the launch RPC creates the session, thread, run
    and control connection; the launch outcome adopts it; and one Machine Agent
    heartbeat carries the control lease that attaches the connection. Every
    capability the control routes check is derived from those rows.

    ``attach=False`` stops after the launch outcome, which is the reattachable
    session the UI shows as needing host attach: a real connection row in state
    ``detached``, granting nothing.
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
                    "project": project,
                    "display_name": project,
                    "managed_session_name": f"lh-{provider}-{project}",
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
    if attach:
        live.rpc(
            "machine.heartbeat.apply.v2",
            {
                "heartbeat": _machine_heartbeat(
                    device_id=device_id,
                    now=now,
                    raw_json=_machine_evidence_json(
                        provider=provider,
                        session_id=session_id,
                        run_id=run_id,
                        now=now,
                    ),
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
    return session_id, run_id


def _apply_terminal_signal(
    live: LiveCatalog,
    *,
    session_id: str,
    run_id: str,
    provider: str,
    terminal_state: str,
    device_id: str = "cinder",
) -> None:
    """Report the run's end the way the provider adapter reports it."""

    now = datetime.now(timezone.utc).replace(microsecond=0)
    live.rpc(
        "session.runtime.apply.v2",
        {
            "events": [
                {
                    "runtime_key": f"{provider}:{session_id}",
                    "session_id": session_id,
                    "run_id": run_id,
                    "provider": provider,
                    "device_id": device_id,
                    "source": f"{provider}_bridge",
                    "kind": "terminal_signal",
                    "occurred_at": now.isoformat(),
                    "dedupe_key": f"{session_id}:{terminal_state}",
                    "payload": {"terminal_state": terminal_state},
                }
            ]
        },
    )


# ---------------------------------------------------------------------------
# Run disposition: which terminal states close a run to new input
# ---------------------------------------------------------------------------
#
# ``session_input_block_reason`` is the gate the queue drainer consults before
# it delivers a queued message. It used to read the archive
# ``session_runtime_state`` table, and these tests used to seed one; it reads
# catalogd runtime facts now, so the terminal state has to arrive the way the
# provider adapter reports it.


@pytest.mark.parametrize("terminal_state", ["finished", "host_expired"])
def test_turn_or_unverified_terminal_state_does_not_block_session_input(live_catalog, terminal_state):  # noqa: F811
    from zerg.services.session_runtime import session_input_block_reason

    owner_id = live_catalog.create_user(f"block-open-{terminal_state}@test.local")
    session_id, run_id = _launch_managed_local_session(live_catalog, owner_id=owner_id, provider="claude")
    _apply_terminal_signal(
        live_catalog,
        session_id=session_id,
        run_id=run_id,
        provider="claude",
        terminal_state=terminal_state,
    )

    # A finished turn and an unverified host both leave the run addressable.
    assert session_input_block_reason(None, session_id) is None


@pytest.mark.parametrize(
    ("terminal_state", "reason"),
    [
        ("session_ended", "run_ended"),
        ("process_gone", "run_ended"),
        ("user_closed", "session_closed"),
    ],
)
def test_irreversible_terminal_state_blocks_session_input(live_catalog, terminal_state, reason):  # noqa: F811
    from zerg.services.session_runtime import session_input_block_reason

    owner_id = live_catalog.create_user(f"block-closed-{terminal_state}@test.local")
    session_id, run_id = _launch_managed_local_session(live_catalog, owner_id=owner_id, provider="claude")
    _apply_terminal_signal(
        live_catalog,
        session_id=session_id,
        run_id=run_id,
        provider="claude",
        terminal_state=terminal_state,
    )

    # Disposition and run end are separate answers: only explicit closure
    # closes the durable session, and provider exit only ends the run.
    assert session_input_block_reason(None, session_id) == reason


# ---------------------------------------------------------------------------
# Managed-local launch response (no route, archive session)
# ---------------------------------------------------------------------------


def _seed_kernel_session(session_local, *, provider: str, with_kernel_rows: bool, control_plane: str | None = None):
    """Create a real AgentSession with optional kernel rows.

    The kernel projection — not raw ``execution_home`` columns — drives the
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
            user_messages=0,
            assistant_messages=0,
            tool_calls=0,
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
            _managed_local_launch_response(db, result)


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
            _managed_local_launch_response(db, result)


# ---------------------------------------------------------------------------
# Live send
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_live_send_refuses_a_session_whose_control_is_not_attached(live_catalog, live_catalog_client, provider):  # noqa: F811
    """A launched-but-detached session cannot be continued from Longhouse.

    This used to assert a second, softer message for the reattachable case --
    "needs host attach before Longhouse can continue it" -- chosen from
    ``host_reattach_available``. That branch is gone: a capability is granted
    by an attached connection or it is not, and the answer is one message.
    """

    email = f"detached-{provider}@test.local"
    owner_id = live_catalog.create_user(email)
    token = live_catalog.create_device_token(owner_id=owner_id, device_id="cinder")
    session_id, _run_id = _launch_managed_local_session(
        live_catalog,
        owner_id=owner_id,
        provider=provider,
        project="managed-local-detached",
        attach=False,
    )

    response = live_catalog_client.post(
        f"/agents/sessions/{session_id}/send-live",
        json={"message": "continue from Longhouse"},
        headers={"X-Agents-Token": token},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "This session does not have a live Longhouse control channel."


def test_agents_send_live_route_ignores_device_mismatch_and_dispatches(live_catalog, live_catalog_client):  # noqa: F811
    """The token names the owner, not the machine the session runs on.

    A device token minted on one laptop must still be able to steer a session
    running on another machine the same owner enrolled, so the send routes on
    the session's own device and not on the token's label.
    """

    owner_id = live_catalog.create_user("agents-send-live@test.local")
    token = live_catalog.create_device_token(owner_id=owner_id, device_id="different-machine-label")
    session_id, _run_id = _launch_managed_local_session(
        live_catalog,
        owner_id=owner_id,
        device_id="agent-device",
        project="agents-send-live",
    )
    websocket = asyncio.run(
        _register_fake_machine_control(
            owner_id=owner_id,
            device_id="agent-device",
            supports=["claude.send"],
        )
    )

    try:
        response = live_catalog_client.post(
            f"/agents/sessions/{session_id}/send-live",
            json={"message": "continue locally from the API"},
            headers={"X-Agents-Token": token},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["accepted"] is True
        assert payload["session_id"] == session_id
        assert payload["verification"] == "live_control_ack"

        assert len(websocket.sent) == 1
        frame = websocket.sent[0]
        assert frame["command_type"] == "session.send_text"
        assert frame["session_id"] == session_id
        assert frame["payload"]["text"] == "continue locally from the API"
        assert frame["payload"]["provider"] == "claude"
        # The grant is minted from the attached connection, not from the token.
        assert frame["payload"]["longhouse_control_grant"]["identity_source"] == "adapter_bound"
    finally:
        asyncio.run(session_chat.session_lock_manager.release(str(session_id)))
        asyncio.run(_clear_machine_control_registry())


@pytest.mark.parametrize(
    ("terminal_state", "error_code", "message"),
    [
        ("session_ended", "run_ended", "This run has ended."),
        ("process_gone", "run_ended", "This run has ended."),
        ("user_closed", "session_closed", "This session is closed."),
    ],
)
def test_agents_send_live_rejects_ended_runtime_run(
    live_catalog,  # noqa: F811
    live_catalog_client,  # noqa: F811
    terminal_state,
    error_code,
    message,
):
    """A run that ended refuses input even while its control channel is up.

    The third parameter used to be ``provider_disconnected``, a terminal state
    no adapter emits: it existed to reach the archive predicate's catch-all
    ``run_ended``. The catalog answer is derived from the run's own end and
    from explicit closure, so the states worth naming here are the ones a
    provider actually reports.
    """

    owner_id = live_catalog.create_user(f"agents-send-closed-{terminal_state}@test.local")
    token = live_catalog.create_device_token(owner_id=owner_id, device_id="agent-device")
    session_id, run_id = _launch_managed_local_session(
        live_catalog,
        owner_id=owner_id,
        provider="codex",
        device_id="agent-device",
        project="agents-send-closed",
    )
    _apply_terminal_signal(
        live_catalog,
        session_id=session_id,
        run_id=run_id,
        provider="codex",
        terminal_state=terminal_state,
        device_id="agent-device",
    )
    websocket = asyncio.run(
        _register_fake_machine_control(
            owner_id=owner_id,
            device_id="agent-device",
            supports=["codex.send"],
        )
    )

    try:
        response = live_catalog_client.post(
            f"/agents/sessions/{session_id}/send-live",
            json={"message": "this should not send"},
            headers={"X-Agents-Token": token},
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == {"error_code": error_code, "message": message}
        assert websocket.sent == []
    finally:
        asyncio.run(_clear_machine_control_registry())


# ---------------------------------------------------------------------------
# Interrupt and terminate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["claude", "opencode", "codex"])
def test_browser_interrupt_live_route_uses_machine_control(live_catalog, live_catalog_client, provider):  # noqa: F811
    email = f"{provider}-interrupt@test.local"
    owner_id = live_catalog.create_user(email)
    cookies = {"longhouse_session": live_catalog.browser_cookie(owner_id=owner_id, email=email)}
    device_id = f"{provider}-interrupt-machine"
    session_id, _run_id = _launch_managed_local_session(
        live_catalog,
        owner_id=owner_id,
        provider=provider,
        device_id=device_id,
        project=f"{provider}-interrupt-live",
    )
    websocket = asyncio.run(
        _register_fake_machine_control(
            owner_id=owner_id,
            device_id=device_id,
            supports=[f"{provider}.interrupt"],
        )
    )
    asyncio.run(session_chat.session_lock_manager.acquire(str(session_id), holder="stalled-turn"))

    try:
        response = live_catalog_client.post(f"/sessions/{session_id}/interrupt-live", cookies=cookies)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["interrupt_dispatched"] is True
        assert payload["confirmed_stopped"] is False
        assert payload["session_id"] == session_id
        assert payload["released_lock"] is True
        assert payload["exit_code"] == 0

        assert len(websocket.sent) == 1
        frame = websocket.sent[0]
        assert frame["type"] == "command"
        assert frame["command_type"] == "session.interrupt"
        assert frame["session_id"] == session_id
        assert str(frame["command_id"]).startswith(f"managed-control:{session_id}:session.interrupt:")
        assert frame["payload"]["provider"] == provider
    finally:
        asyncio.run(session_chat.session_lock_manager.release(str(session_id)))
        asyncio.run(_clear_machine_control_registry())


def test_agents_interrupt_live_route_dispatches_and_releases_lock(live_catalog, live_catalog_client):  # noqa: F811
    owner_id = live_catalog.create_user("agents-interrupt-live@test.local")
    token = live_catalog.create_device_token(owner_id=owner_id, device_id="different-machine-label")
    session_id, _run_id = _launch_managed_local_session(
        live_catalog,
        owner_id=owner_id,
        device_id="agent-device",
        project="agents-interrupt-live",
    )
    websocket = asyncio.run(
        _register_fake_machine_control(
            owner_id=owner_id,
            device_id="agent-device",
            supports=["claude.interrupt"],
        )
    )
    asyncio.run(session_chat.session_lock_manager.acquire(str(session_id), holder="stalled-turn"))

    try:
        response = live_catalog_client.post(
            f"/agents/sessions/{session_id}/interrupt-live",
            headers={"X-Agents-Token": token},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["interrupt_dispatched"] is True
        assert payload["confirmed_stopped"] is False
        assert payload["session_id"] == session_id
        assert payload["released_lock"] is True
        assert [frame["command_type"] for frame in websocket.sent] == ["session.interrupt"]
    finally:
        asyncio.run(session_chat.session_lock_manager.release(str(session_id)))
        asyncio.run(_clear_machine_control_registry())


def test_agents_interrupt_live_route_releases_lock_on_dispatch_failure(live_catalog, live_catalog_client):  # noqa: F811
    owner_id = live_catalog.create_user("agents-interrupt-fail@test.local")
    token = live_catalog.create_device_token(owner_id=owner_id, device_id="different-machine-label")
    session_id, _run_id = _launch_managed_local_session(
        live_catalog,
        owner_id=owner_id,
        device_id="agent-device",
        project="agents-interrupt-fail",
    )
    asyncio.run(
        _register_fake_machine_control(
            owner_id=owner_id,
            device_id="agent-device",
            supports=["claude.interrupt"],
            exit_code=7,
            stderr="interrupt failed",
        )
    )
    asyncio.run(session_chat.session_lock_manager.acquire(str(session_id), holder="stalled-turn"))

    try:
        response = live_catalog_client.post(
            f"/agents/sessions/{session_id}/interrupt-live",
            headers={"X-Agents-Token": token},
        )
        assert response.status_code == 502, response.text
        detail = response.json()["detail"]
        assert detail["error_code"] == "interrupt_failed"
        assert detail["exit_code"] == 7
        assert detail["released_lock"] is True
        assert detail["confirmed_stopped"] is False
        # A failed interrupt still leaves the session unlocked, or the stalled
        # turn stays wedged behind a lock nobody holds.
        assert asyncio.run(session_chat.session_lock_manager.get_lock_info(str(session_id))) is None
    finally:
        asyncio.run(session_chat.session_lock_manager.release(str(session_id)))
        asyncio.run(_clear_machine_control_registry())


def test_browser_terminate_live_route_dispatches_and_releases_lock(live_catalog, live_catalog_client):  # noqa: F811
    """Terminate runs against OpenCode because it is the provider that grants it.

    This used to run against Claude with the dispatch function replaced, which
    hid the fact that ``can_terminate`` is derived from the machine-control
    supports a provider actually advertises. Claude and Codex declare the
    contract operation and carry no ``<provider>.terminate`` support, so a
    Claude terminate is refused before the engine is contacted.
    """

    email = "browser-terminate-live@test.local"
    owner_id = live_catalog.create_user(email)
    cookies = {"longhouse_session": live_catalog.browser_cookie(owner_id=owner_id, email=email)}
    session_id, _run_id = _launch_managed_local_session(
        live_catalog,
        owner_id=owner_id,
        provider="opencode",
        device_id="agent-device",
        project="browser-terminate-live",
    )
    websocket = asyncio.run(
        _register_fake_machine_control(
            owner_id=owner_id,
            device_id="agent-device",
            supports=["opencode.terminate"],
        )
    )
    asyncio.run(session_chat.session_lock_manager.acquire(str(session_id), holder="stalled-turn"))

    try:
        response = live_catalog_client.post(f"/sessions/{session_id}/terminate-live", cookies=cookies)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["terminate_dispatched"] is True
        assert payload["session_id"] == session_id
        assert payload["released_lock"] is True
        assert [frame["command_type"] for frame in websocket.sent] == ["session.terminate"]
    finally:
        asyncio.run(session_chat.session_lock_manager.release(str(session_id)))
        asyncio.run(_clear_machine_control_registry())


def test_agents_terminate_live_route_releases_lock_on_dispatch_failure(live_catalog, live_catalog_client):  # noqa: F811
    owner_id = live_catalog.create_user("agents-terminate-fail@test.local")
    token = live_catalog.create_device_token(owner_id=owner_id, device_id="different-machine-label")
    session_id, _run_id = _launch_managed_local_session(
        live_catalog,
        owner_id=owner_id,
        provider="opencode",
        device_id="agent-device",
        project="agents-terminate-fail",
    )
    asyncio.run(
        _register_fake_machine_control(
            owner_id=owner_id,
            device_id="agent-device",
            supports=["opencode.terminate"],
            exit_code=7,
            stderr="terminate failed",
        )
    )
    asyncio.run(session_chat.session_lock_manager.acquire(str(session_id), holder="stalled-turn"))

    try:
        response = live_catalog_client.post(
            f"/agents/sessions/{session_id}/terminate-live",
            headers={"X-Agents-Token": token},
        )
        assert response.status_code == 502, response.text
        detail = response.json()["detail"]
        assert detail["error_code"] == "terminate_failed"
        assert detail["exit_code"] == 7
        assert detail["released_lock"] is True
        assert asyncio.run(session_chat.session_lock_manager.get_lock_info(str(session_id))) is None
    finally:
        asyncio.run(session_chat.session_lock_manager.release(str(session_id)))
        asyncio.run(_clear_machine_control_registry())
