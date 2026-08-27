from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from uuid import UUID
from uuid import uuid4

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from tests_lite.live_catalog_harness import LiveCatalog
from tests_lite.live_catalog_harness import live_catalog  # noqa: F401
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: F401
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionRuntimeState
from zerg.models.agents import SessionTurn
from zerg.models.enums import UserRole
from zerg.models.models import Runner
from zerg.models.user import User
from zerg.services import session_chat_impl
from zerg.services.machine_control_channel import get_machine_control_channel_registry
from zerg.services.managed_local_control import ManagedLocalPhaseUpdate
from zerg.services.managed_local_control import ManagedLocalTerminalResult
from zerg.services.managed_local_event_polling import managed_local_events_include_expected_turn
from zerg.services.runner_connection_manager import get_runner_connection_manager
from zerg.services.session_locks import session_lock_manager
from zerg.services.session_runtime import phase_freshness_ms
from zerg.services.session_runtime import runtime_key_for_session
from zerg.services.session_turns import create_session_turn
from zerg.services.session_turns import mark_session_turn_send_accepted


def _make_db(tmp_path):
    db_path = tmp_path / "test_managed_local_session_chat.db"
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)
    return make_sessionmaker(engine)


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
    get_runner_connection_manager().register(user.id, runner.id, SimpleNamespace())
    return user, runner


def _seed_live_runtime_state(db, session: AgentSession, *, phase: str = "idle") -> None:
    now = datetime.now(timezone.utc)
    freshness_ms = phase_freshness_ms(phase) or int(timedelta(minutes=5).total_seconds() * 1000)
    db.add(
        SessionRuntimeState(
            runtime_key=runtime_key_for_session(str(session.provider or "claude"), str(session.id)),
            session_id=session.id,
            provider=str(session.provider or "claude"),
            device_id=session.device_id,
            phase=phase,
            phase_source="semantic",
            phase_started_at=now,
            last_runtime_signal_at=now,
            last_progress_at=now,
            last_live_at=now,
            timeline_anchor_at=now,
            freshness_expires_at=now + timedelta(milliseconds=freshness_ms),
            terminal_state=None,
            terminal_at=None,
            runtime_version=1,
        )
    )
    db.commit()


def _seed_managed_local_session(db, *, runner: Runner, provider: str = "claude") -> AgentSession:
    session_id = uuid4()
    session = AgentSession(
        id=session_id,
        provider=provider,
        environment="development",
        project="hiring",
        device_id=runner.name,
        cwd="/Users/example/git/acme/hiring",
        git_repo="git@github.com:cipher982/longhouse.git",
        git_branch="main",
        started_at=datetime.now(timezone.utc),
                                        user_messages=1,
        assistant_messages=1,
        tool_calls=0,
                        loop_mode="assist",
                                            )
    db.add(session)
    db.commit()
    db.refresh(session)
    from tests_lite._kernel_test_helpers import seed_managed_kernel_rows

    if provider == "codex":
        kernel_plane = "codex_bridge"
    elif provider == "opencode":
        kernel_plane = "opencode_process"
    else:
        kernel_plane = "claude_channel_bridge"
    seed_managed_kernel_rows(db, session, control_plane=kernel_plane)
    db.commit()
    _seed_live_runtime_state(db, session)
    return session


# ---------------------------------------------------------------------------
# Live catalog: a Helm session whose control path is real
#
# The multipart route is catalog-only now -- a receipt through catalogd, blob
# metadata through catalogd, and a dispatch over the machine control channel.
# Nothing below is seeded directly: the launch RPCs create the session, thread,
# run and connection, and one Machine Agent heartbeat carries the control lease
# and the typed facts that bind the adapter identity and report the session
# idle. The capability gates the route checks are derived from those rows.
# ---------------------------------------------------------------------------

LIVE_DEVICE_ID = "cinder"


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
    connection; without it every command is refused with ``identity_unbound``.
    The activity fact is what makes the session quiescent.
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
    provider: str = "codex",
    device_id: str = LIVE_DEVICE_ID,
) -> str:
    """Launch one Helm session in the live catalog and bring its control online."""

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
                    "project": "hiring",
                    "display_name": "Hiring",
                    "managed_session_name": f"{provider}-managed-local-chat",
                    "loop_mode": "assist",
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


class _AutoCompletingMachineWebSocket:
    """A Machine Agent control channel that accepts every command."""

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


class _RefusingMachineWebSocket:
    """A Machine Agent control channel whose provider refuses the command."""

    def __init__(self):
        self.sent: list[dict[str, object]] = []

    async def send_json(self, message):
        self.sent.append(message)
        await get_machine_control_channel_registry().complete_command(
            {
                "type": "command_result",
                "command_id": message["command_id"],
                "ok": False,
                "error": "Runner send failed",
            }
        )


async def _clear_machine_control_registry() -> None:
    await get_machine_control_channel_registry().clear_for_tests()


async def _register_fake_machine_control(
    *,
    owner_id: int,
    supports: list[str],
    device_id: str = LIVE_DEVICE_ID,
    websocket=None,
):
    websocket = websocket or _AutoCompletingMachineWebSocket()
    await get_machine_control_channel_registry().register(
        owner_id=owner_id,
        device_id=device_id,
        machine_name=device_id,
        engine_build="test-engine",
        supports=supports,
        websocket=websocket,
    )
    return websocket


# ---------------------------------------------------------------------------
# Unit tests for helper functions
# ---------------------------------------------------------------------------


def test_managed_local_events_include_expected_turn_requires_current_prompt_and_reply():
    prompt = "continue"

    assert managed_local_events_include_expected_turn(
        events=[
            SimpleNamespace(role="system", content_text="snapshot", tool_name=None),
            SimpleNamespace(role="user", content_text=prompt, tool_name=None),
            SimpleNamespace(role="assistant", content_text="done", tool_name=None),
        ],
        expected_user_message=prompt,
    )

    assert not managed_local_events_include_expected_turn(
        events=[
            SimpleNamespace(role="system", content_text="snapshot", tool_name=None),
            SimpleNamespace(role="assistant", content_text="done", tool_name=None),
        ],
        expected_user_message=prompt,
    )

    assert not managed_local_events_include_expected_turn(
        events=[
            SimpleNamespace(role="assistant", content_text="older reply", tool_name=None),
            SimpleNamespace(role="user", content_text=prompt, tool_name=None),
        ],
        expected_user_message=prompt,
    )

    assert not managed_local_events_include_expected_turn(
        events=[
            SimpleNamespace(role="user", content_text=prompt, tool_name=None),
            SimpleNamespace(role="system", content_text="snapshot", tool_name=None),
        ],
        expected_user_message=prompt,
    )


def test_managed_local_events_include_expected_turn_accepts_native_claude_channel_wrapper():
    prompt = "continue"

    assert managed_local_events_include_expected_turn(
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


def _seed_owner(live_catalog: LiveCatalog, email: str) -> tuple[int, dict[str, str]]:
    owner_id = live_catalog.create_user(email)
    return owner_id, {"longhouse_session": live_catalog.browser_cookie(owner_id=owner_id, email=email)}


def test_managed_local_claude_dispatch_returns_json_ack(live_catalog, live_catalog_client, monkeypatch):
    """Managed-local Claude chat returns a JSON ack the moment control acknowledges."""

    owner_id, cookies = _seed_owner(live_catalog, "managed-local-claude@test.local")
    session_id = _seed_live_catalog_session(live_catalog, owner_id=owner_id, provider="claude")
    websocket = asyncio.run(_register_fake_machine_control(owner_id=owner_id, supports=["claude.send"]))
    # The lock is released by a watcher that waits for terminal catalog facts;
    # this test is about what the request itself returns.
    monkeypatch.setattr(session_chat_impl, "_schedule_catalog_lock_release", lambda **_kwargs: None)

    try:
        response = live_catalog_client.post(
            f"/sessions/{session_id}/send-live",
            json={"message": "continue"},
            cookies=cookies,
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["accepted"] is True
        assert data["session_id"] == session_id
        assert data["request_id"]
        # The control ack is the acceptance proof; there is no archive turn to
        # verify against and no second round trip to wait for.
        assert data["verification"] == "live_control_ack"

        assert len(websocket.sent) == 1
        frame = websocket.sent[0]
        assert frame["command_type"] == "session.send_text"
        assert frame["session_id"] == session_id
        assert frame["payload"]["provider"] == "claude"
        assert frame["payload"]["text"] == "continue"
        # The command carries the adapter identity the catalog granted, which is
        # what the Machine Agent checks before touching the provider.
        assert frame["payload"]["longhouse_control_grant"]["identity_source"] == "adapter_bound"
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))
        asyncio.run(_clear_machine_control_registry())


def test_managed_local_codex_dispatch_returns_json_ack(live_catalog, live_catalog_client, monkeypatch):
    """Managed-local Codex chat also returns JSON ack."""

    owner_id, cookies = _seed_owner(live_catalog, "managed-local-codex@test.local")
    session_id = _seed_live_catalog_session(live_catalog, owner_id=owner_id, provider="codex")
    websocket = asyncio.run(_register_fake_machine_control(owner_id=owner_id, supports=["codex.send"]))
    monkeypatch.setattr(session_chat_impl, "_schedule_catalog_lock_release", lambda **_kwargs: None)

    try:
        response = live_catalog_client.post(
            f"/sessions/{session_id}/send-live",
            json={"message": "what about germany"},
            cookies=cookies,
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["accepted"] is True
        assert data["session_id"] == session_id
        assert websocket.sent[0]["payload"] == {
            "provider": "codex",
            "text": "what about germany",
            "longhouse_control_grant": websocket.sent[0]["payload"]["longhouse_control_grant"],
        }
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))
        asyncio.run(_clear_machine_control_registry())


def test_managed_local_draft_reply_returns_prefill(live_catalog, live_catalog_client, monkeypatch):
    """Draft reply generates a composer prefill without dispatching to the live session."""

    llm_calls: list[dict[str, object]] = []

    class FakeCompletions:
        async def create(self, **kwargs):
            llm_calls.append(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="Please run the focused iOS tests and report the result.")
                    )
                ]
            )

    class FakeClient:
        def __init__(self):
            self.chat = SimpleNamespace(completions=FakeCompletions())

        async def close(self):
            return None

    owner_id, cookies = _seed_owner(live_catalog, "managed-local-draft@test.local")
    session_id = _seed_live_catalog_session(live_catalog, owner_id=owner_id, provider="codex")
    live_catalog.commit_session(
        owner_id=owner_id,
        session_id=UUID(session_id),
        device_id=LIVE_DEVICE_ID,
        texts=("Let's add iOS steering.", "I added the endpoint and need to run tests."),
    )

    monkeypatch.setattr(
        session_chat_impl,
        "get_llm_client_for_use_case",
        lambda use_case: (FakeClient(), "test-draft-model", "openai"),
    )

    try:
        response = live_catalog_client.post(
            f"/sessions/{session_id}/draft-reply",
            json={"max_chars": 500},
            cookies=cookies,
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["draft_text"] == "Please run the focused iOS tests and report the result."
        assert data["model"] == "test-draft-model"
        assert len(llm_calls) == 1
        assert llm_calls[0]["model"] == "test-draft-model"
        assert "max_tokens" not in llm_calls[0]
        prompt = llm_calls[0]["messages"][1]["content"]
        assert "Let's add iOS steering." in prompt
        assert "need to run tests" in prompt
        # Drafting reads the transcript; it never takes the dispatch lock.
        assert asyncio.run(session_lock_manager.get_lock_info(session_id)) is None
    finally:
        asyncio.run(session_lock_manager.release(session_id))


def test_managed_local_draft_reply_requires_live_control(live_catalog, live_catalog_client):
    """Draft reply is not exposed for imported/unmanaged sessions."""

    owner_id, cookies = _seed_owner(live_catalog, "managed-local-nodraft@test.local")
    # A shipped transcript with no launch and no control lease: observable, not
    # steerable, so the capability projection collapses to observe-only.
    seeded = live_catalog.commit_session(owner_id=owner_id, texts=("imported transcript",))

    response = live_catalog_client.post(
        f"/sessions/{seeded.session_id}/draft-reply",
        json={"max_chars": 500},
        cookies=cookies,
    )
    assert response.status_code == 409, response.text


def test_managed_local_dispatch_send_failure_returns_502(live_catalog, live_catalog_client):
    """When live-session dispatch fails, returns {accepted: false} with 502."""

    owner_id, cookies = _seed_owner(live_catalog, "managed-local-502@test.local")
    session_id = _seed_live_catalog_session(live_catalog, owner_id=owner_id, provider="claude")
    asyncio.run(
        _register_fake_machine_control(
            owner_id=owner_id,
            supports=["claude.send"],
            websocket=_RefusingMachineWebSocket(),
        )
    )

    try:
        response = live_catalog_client.post(
            f"/sessions/{session_id}/send-live",
            json={"message": "continue"},
            cookies=cookies,
        )
        assert response.status_code == 502, response.text
        data = response.json()
        assert data["accepted"] is False
        assert "Runner send failed" in data["error"]
        assert data["error_code"] == "send_failed"
    finally:
        asyncio.run(_clear_machine_control_registry())


def test_managed_local_dispatch_send_failure_releases_lock_for_retry(live_catalog, live_catalog_client):
    """Failed dispatches should release the lock so the next send can retry immediately."""

    owner_id, cookies = _seed_owner(live_catalog, "managed-local-retry@test.local")
    session_id = _seed_live_catalog_session(live_catalog, owner_id=owner_id, provider="claude")
    websocket = asyncio.run(
        _register_fake_machine_control(
            owner_id=owner_id,
            supports=["claude.send"],
            websocket=_RefusingMachineWebSocket(),
        )
    )

    try:
        first = live_catalog_client.post(
            f"/sessions/{session_id}/send-live",
            json={"message": "continue"},
            cookies=cookies,
        )
        assert first.status_code == 502, first.text

        second = live_catalog_client.post(
            f"/sessions/{session_id}/send-live",
            json={"message": "retry"},
            cookies=cookies,
        )
        # A retained lock would answer 409 here without ever reaching the machine.
        assert second.status_code == 502, second.text
        assert [frame["payload"]["text"] for frame in websocket.sent] == ["continue", "retry"]
    finally:
        asyncio.run(_clear_machine_control_registry())


def test_managed_local_dispatch_keeps_lock_until_terminal(live_catalog, live_catalog_client, monkeypatch):
    """Successful managed-local dispatch should keep the thread lock until terminal state."""

    owner_id, cookies = _seed_owner(live_catalog, "managed-local-lock@test.local")
    session_id = _seed_live_catalog_session(live_catalog, owner_id=owner_id, provider="claude")
    asyncio.run(_register_fake_machine_control(owner_id=owner_id, supports=["claude.send"]))
    monkeypatch.setattr(session_chat_impl, "_schedule_catalog_lock_release", lambda **_kwargs: None)

    try:
        first = live_catalog_client.post(
            f"/sessions/{session_id}/send-live",
            json={"message": "continue"},
            cookies=cookies,
        )
        assert first.status_code == 200, first.text

        second = live_catalog_client.post(
            f"/sessions/{session_id}/send-live",
            json={"message": "continue again"},
            cookies=cookies,
        )
        assert second.status_code == 409
        assert second.json()["detail"]["code"] == "SESSION_LOCKED"
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))
        asyncio.run(_clear_machine_control_registry())


def test_managed_local_dispatch_updates_lock_endpoint_until_terminal(live_catalog, live_catalog_client, monkeypatch):
    """Successful dispatch should surface the held lock via the lock-status endpoint."""

    owner_id, cookies = _seed_owner(live_catalog, "managed-local-lockstatus@test.local")
    session_id = _seed_live_catalog_session(live_catalog, owner_id=owner_id, provider="claude")
    asyncio.run(_register_fake_machine_control(owner_id=owner_id, supports=["claude.send"]))
    monkeypatch.setattr(session_chat_impl, "_schedule_catalog_lock_release", lambda **_kwargs: None)

    try:
        response = live_catalog_client.post(
            f"/sessions/{session_id}/send-live",
            json={"message": "continue"},
            cookies=cookies,
        )
        assert response.status_code == 200, response.text

        lock_response = live_catalog_client.get(f"/sessions/{session_id}/lock", cookies=cookies)
        assert lock_response.status_code == 200, lock_response.text
        assert lock_response.json()["locked"] is True
        assert lock_response.json()["fork_available"] is True
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))
        asyncio.run(_clear_machine_control_registry())


def test_managed_local_active_observer_marks_canonical_turn(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        _user, runner = _seed_user_and_runner(db)
        source_session = _seed_managed_local_session(db, runner=runner, provider="claude")
        create_session_turn(
            db,
            session_id=source_session.id,
            request_id="req-active",
        )
        mark_session_turn_send_accepted(db, session_id=source_session.id, request_id="req-active")
        db.commit()
        db_bind = db.get_bind()

    async def fake_wait(**_kwargs):
        return ManagedLocalPhaseUpdate(
            phase="thinking",
            observation_id=12,
            occurred_at=datetime.now(timezone.utc),
            source="claude_hook",
        )

    monkeypatch.setattr("zerg.services.session_chat_impl.await_managed_local_hook_phase_update", fake_wait)

    asyncio.run(
        session_chat_impl._observe_managed_local_turn_active_phase(
            request_id="req-active",
            session_id=source_session.id,
            provider="claude",
            db_bind=db_bind,
            after_observation_id=0,
        )
    )

    with session_local() as verify_db:
        row = (
            verify_db.query(SessionTurn)
            .filter(SessionTurn.session_id == source_session.id, SessionTurn.request_id == "req-active")
            .one()
        )
        assert row.active_phase_observed_at is not None
        assert row.state == "active"


def test_managed_local_terminal_observer_marks_canonical_turn_and_releases_lock(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)
    release_calls: list[tuple[str, str]] = []

    with session_local() as db:
        _user, runner = _seed_user_and_runner(db)
        source_session = _seed_managed_local_session(db, runner=runner, provider="claude")
        create_session_turn(
            db,
            session_id=source_session.id,
            request_id="req-terminal",
        )
        mark_session_turn_send_accepted(db, session_id=source_session.id, request_id="req-terminal")
        db.commit()
        db_bind = db.get_bind()

    async def fake_terminal_wait(**_kwargs):
        return ManagedLocalTerminalResult(
            phase="idle",
            control_status="completed",
            observation_id=0,
            occurred_at=datetime.now(timezone.utc),
        )

    async def fake_release(lock_scope_id, request_id):
        release_calls.append((lock_scope_id, request_id))
        return True

    monkeypatch.setattr("zerg.services.session_chat_impl.await_managed_local_turn_terminal", fake_terminal_wait)
    monkeypatch.setattr(session_chat_impl.session_lock_manager, "release", fake_release)

    asyncio.run(
        session_chat_impl._release_managed_local_lock_after_terminal(
            lock_scope_id=str(source_session.id),
            request_id="req-terminal",
            session_id=source_session.id,
            provider="claude",
            db_bind=db_bind,
            after_observation_id=0,
        )
    )

    with session_local() as verify_db:
        canonical_row = (
            verify_db.query(SessionTurn)
            .filter(SessionTurn.session_id == source_session.id, SessionTurn.request_id == "req-terminal")
            .one()
        )
        assert canonical_row.terminal_at is not None
        assert canonical_row.terminal_phase == "idle"
        assert canonical_row.state == "terminal"

    assert release_calls == [(str(source_session.id), "req-terminal")]


def test_managed_local_active_observer_is_noop_after_terminal_turn(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        _user, runner = _seed_user_and_runner(db)
        source_session = _seed_managed_local_session(db, runner=runner, provider="claude")
        create_session_turn(
            db,
            session_id=source_session.id,
            request_id="req-active-after-terminal",
        )
        mark_session_turn_send_accepted(db, session_id=source_session.id, request_id="req-active-after-terminal")
        db.commit()
        db_bind = db.get_bind()

    async def fake_terminal_wait(**_kwargs):
        return ManagedLocalTerminalResult(
            phase="idle",
            control_status="completed",
            observation_id=0,
            occurred_at=datetime.now(timezone.utc),
        )

    async def fake_active_wait(**_kwargs):
        return ManagedLocalPhaseUpdate(
            phase="thinking",
            observation_id=12,
            occurred_at=datetime.now(timezone.utc),
            source="claude_hook",
        )

    async def fake_release(_lock_scope_id, _request_id):
        return True

    monkeypatch.setattr("zerg.services.session_chat_impl.await_managed_local_turn_terminal", fake_terminal_wait)
    monkeypatch.setattr("zerg.services.session_chat_impl.await_managed_local_hook_phase_update", fake_active_wait)
    monkeypatch.setattr(session_chat_impl.session_lock_manager, "release", fake_release)

    asyncio.run(
        session_chat_impl._release_managed_local_lock_after_terminal(
            lock_scope_id=str(source_session.id),
            request_id="req-active-after-terminal",
            session_id=source_session.id,
            provider="claude",
            db_bind=db_bind,
            after_observation_id=0,
        )
    )
    asyncio.run(
        session_chat_impl._observe_managed_local_turn_active_phase(
            request_id="req-active-after-terminal",
            session_id=source_session.id,
            provider="claude",
            db_bind=db_bind,
            after_observation_id=0,
        )
    )

    with session_local() as verify_db:
        row = (
            verify_db.query(SessionTurn)
            .filter(
                SessionTurn.session_id == source_session.id,
                SessionTurn.request_id == "req-active-after-terminal",
            )
            .one()
        )
        assert row.state == "terminal"
        assert row.terminal_phase == "idle"
        assert row.active_phase_observed_at is None


def test_managed_local_dispatch_send_crash_releases_lock(live_catalog, live_catalog_client, monkeypatch):
    """A crashed dispatch answers 500 and leaves the session free to retry."""

    from zerg.services import managed_control_dispatcher

    owner_id, cookies = _seed_owner(live_catalog, "managed-local-crash@test.local")
    session_id = _seed_live_catalog_session(live_catalog, owner_id=owner_id, provider="claude")
    asyncio.run(_register_fake_machine_control(owner_id=owner_id, supports=["claude.send"]))

    async def crash(**_kwargs):
        raise RuntimeError("dispatch crashed")

    monkeypatch.setattr(managed_control_dispatcher, "dispatch_managed_control_command", crash)

    try:
        response = live_catalog_client.post(
            f"/sessions/{session_id}/send-live",
            json={"message": "continue"},
            cookies=cookies,
        )
        assert response.status_code == 500, response.text

        lock_response = live_catalog_client.get(f"/sessions/{session_id}/lock", cookies=cookies)
        assert lock_response.status_code == 200, lock_response.text
        assert lock_response.json()["locked"] is False
    finally:
        asyncio.run(_clear_machine_control_registry())
