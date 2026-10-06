from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from starlette.websockets import WebSocketDisconnect

from zerg.middleware.runtime_write_admission import RuntimeWriteAdmissionMiddleware
from zerg.services.runtime_admission import RuntimeAdmission


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _candidate_environment(monkeypatch, *, deadline: datetime | None = None, cutoff: datetime | None = None) -> None:
    monkeypatch.setenv("LONGHOUSE_DEPLOYMENT_PENDING", "1")
    monkeypatch.setenv("LONGHOUSE_DEPLOYMENT_ATTEMPT_ID", "attempt-1")
    monkeypatch.setenv("LONGHOUSE_DEPLOYMENT_GENERATION", "7")
    monkeypatch.setenv("LONGHOUSE_IMAGE_DIGEST", "sha256:" + "a" * 64)
    if deadline is not None:
        monkeypatch.setenv("LONGHOUSE_CLAIM_DEADLINE", _stamp(deadline))
    else:
        monkeypatch.delenv("LONGHOUSE_CLAIM_DEADLINE", raising=False)
    if cutoff is not None:
        monkeypatch.setenv("LONGHOUSE_CLAIM_CUTOFF", _stamp(cutoff))
    else:
        monkeypatch.delenv("LONGHOUSE_CLAIM_CUTOFF", raising=False)
    monkeypatch.delenv("LONGHOUSE_CLAIM_EXPECTED_BACK_BY", raising=False)


def _reopen_payload(runtime: RuntimeAdmission) -> dict[str, object]:
    return {
        "request_id": "reopen-request",
        "deployment_id": "deployment-1",
        "target_id": "target-1",
        "generation": "7",
        "deadline_utc": _stamp(datetime.now(UTC) + timedelta(seconds=30)),
        "grace_seconds": 1,
        "runtime_epoch": runtime.runtime_epoch,
    }


async def _reopen_candidate(runtime: RuntimeAdmission) -> None:
    runtime.observe_candidate(attempt_id="attempt-1", generation="7")
    runtime.mark_candidate_ready(attempt_id="attempt-1")
    runtime.mark_candidate_consistent(attempt_id="attempt-1")

    async def activation_probe(_operation: str, params: dict[str, object]) -> dict[str, object]:
        return {
            "available": True,
            "state": "open",
            "depth": 0,
            "accepting": True,
            "active_label": None,
            "activation": {**params, "activated_at": _stamp(datetime.now(UTC))},
        }

    await runtime.reopen(
        _reopen_payload(runtime),
        attempt_id="attempt-1",
        activation_probe=activation_probe,
    )


@pytest.mark.asyncio
async def test_candidate_pending_write_waits_for_reopen_then_admits(monkeypatch) -> None:
    _candidate_environment(monkeypatch)
    runtime = RuntimeAdmission()
    await _reopen_candidate_prepare(runtime)
    waiter = asyncio.create_task(runtime.try_admit(path="/api/agents/storage/v2/envelopes"))
    await asyncio.sleep(0.01)
    assert not waiter.done()

    await _reopen_candidate(runtime)

    admitted, snapshot = await waiter
    assert admitted is True
    assert snapshot["admission"] == "open"
    await runtime.release()


@pytest.mark.asyncio
async def test_candidate_pending_writer_rejects_at_claim_deadline(monkeypatch) -> None:
    now = datetime.now(UTC)
    _candidate_environment(monkeypatch, deadline=now + timedelta(seconds=0.04), cutoff=now + timedelta(seconds=2))
    runtime = RuntimeAdmission()

    started = time.monotonic()
    admitted, details = await runtime.try_admit(path="/write")
    elapsed = time.monotonic() - started

    assert admitted is False
    assert elapsed < 0.5
    assert details["code"] == "runtime_restarting"
    assert details["admission"] == "pending"
    assert details["claim"]["runtime_epoch"] == runtime.runtime_epoch


async def _reopen_candidate_prepare(runtime: RuntimeAdmission) -> None:
    runtime.observe_candidate(attempt_id="attempt-1", generation="7")
    runtime.mark_candidate_ready(attempt_id="attempt-1")
    runtime.mark_candidate_consistent(attempt_id="attempt-1")


@pytest.mark.asyncio
async def test_candidate_websocket_upgrade_holds_until_reopen(monkeypatch) -> None:
    _candidate_environment(monkeypatch)
    runtime = RuntimeAdmission()
    await _reopen_candidate_prepare(runtime)
    from zerg.services import runtime_admission as admission_module

    monkeypatch.setattr(admission_module, "runtime_admission", lambda: runtime)
    accepted = False
    sent: list[dict] = []

    async def app(scope, receive, send):
        nonlocal accepted
        accepted = True
        await send({"type": "websocket.accept"})

    async def receive():
        raise AssertionError("the upgrade hold must not read WebSocket messages")

    async def send(message):
        sent.append(message)

    middleware = RuntimeWriteAdmissionMiddleware(app)
    scope = {
        "type": "websocket",
        "path": "/api/runners/ws",
        "extensions": {"websocket.http.response": {}},
    }
    task = asyncio.create_task(middleware(scope, receive, send))
    await asyncio.sleep(0.01)
    assert not accepted

    await _reopen_candidate(runtime)
    await task

    assert accepted
    assert sent == [{"type": "websocket.accept"}]


@pytest.mark.asyncio
async def test_candidate_environment_defaults_and_renewal_are_capped_at_cutoff(monkeypatch) -> None:
    now = datetime.now(UTC)
    _candidate_environment(monkeypatch)
    runtime = RuntimeAdmission()
    initial = runtime.host_lifecycle()
    assert 14 <= (datetime.fromisoformat(initial["expected_back_by"].replace("Z", "+00:00")) - now).total_seconds() <= 16
    assert 299 <= (datetime.fromisoformat(initial["deadline"].replace("Z", "+00:00")) - now).total_seconds() <= 301
    assert 959 <= (datetime.fromisoformat(initial["cutoff"].replace("Z", "+00:00")) - now).total_seconds() <= 961

    await runtime.renew_claim(
        claim_deadline=_stamp(now + timedelta(days=2)),
        attempt_id="attempt-1",
        phase="probe",
    )

    renewed = runtime.host_lifecycle()
    assert renewed["phase"] == "probe"
    assert renewed["deadline"] == initial["cutoff"]


@pytest.mark.asyncio
async def test_drain_claim_fields_parse_and_default_horizons() -> None:
    runtime = RuntimeAdmission()

    async def catalog_probe(operation: str) -> dict[str, object]:
        assert operation == "close"
        return {"available": True, "state": "closed", "depth": 0, "accepting": False, "active_label": None}

    now = datetime.now(UTC)
    expected = now + timedelta(seconds=40)
    deadline = now + timedelta(seconds=90)
    cutoff = now + timedelta(seconds=110)
    payload: dict[str, object] = {
        "request_id": "drain-request",
        "deployment_id": "deployment-1",
        "target_id": "target-1",
        "generation": "7",
        "deadline_utc": _stamp(now + timedelta(seconds=30)),
        "grace_seconds": 0,
        "runtime_epoch": runtime.runtime_epoch,
        "expected_back_by": _stamp(expected),
        "claim_deadline": _stamp(deadline),
        "claim_cutoff": _stamp(cutoff),
    }
    result = await runtime.drain(payload, attempt_id="attempt-1", catalog_probe=catalog_probe)
    claim = result["claim"]
    assert claim["state"] == "updating"
    assert datetime.fromisoformat(claim["expected_back_by"].replace("Z", "+00:00")) == expected
    assert datetime.fromisoformat(claim["deadline"].replace("Z", "+00:00")) == deadline
    assert datetime.fromisoformat(claim["cutoff"].replace("Z", "+00:00")) == cutoff


@pytest.mark.asyncio
async def test_drain_uses_default_claim_horizons_when_request_omits_them() -> None:
    runtime = RuntimeAdmission()
    before = datetime.now(UTC)

    async def catalog_probe(_operation: str) -> dict[str, object]:
        return {"available": True, "state": "closed", "depth": 0, "accepting": False, "active_label": None}

    payload = {
        "request_id": "drain-request",
        "deployment_id": "deployment-1",
        "target_id": "target-1",
        "generation": "7",
        "deadline_utc": _stamp(before + timedelta(seconds=30)),
        "grace_seconds": 0,
        "runtime_epoch": runtime.runtime_epoch,
    }
    result = await runtime.drain(payload, attempt_id="attempt-1", catalog_probe=catalog_probe)
    claim = result["claim"]
    after = datetime.now(UTC)
    for name, seconds in (("expected_back_by", 30), ("deadline", 360), ("cutoff", 960)):
        value = datetime.fromisoformat(claim[name].replace("Z", "+00:00"))
        assert before + timedelta(seconds=seconds - 1) < value < after + timedelta(seconds=seconds + 1)


@pytest.mark.asyncio
async def test_drain_route_publishes_start_and_completion_lifecycle(monkeypatch) -> None:
    from zerg.routers import internal_deployments

    runtime = RuntimeAdmission()
    monkeypatch.setattr(internal_deployments, "runtime_admission", lambda: runtime)
    monkeypatch.setattr(
        internal_deployments,
        "get_settings",
        lambda: SimpleNamespace(internal_api_secret="internal-secret"),
    )
    publisher = AsyncMock()
    monkeypatch.setattr(internal_deployments, "_signal_runtime_lifecycle", publisher)

    async def catalog_probe(_operation: str) -> dict[str, object]:
        return {"available": True, "state": "closed", "depth": 0, "accepting": False, "active_label": None}

    monkeypatch.setattr(internal_deployments, "_catalog_admission_probe", catalog_probe)
    body = internal_deployments.DeploymentFenceRequest(
        request_id="drain-request",
        deployment_id="deployment-1",
        target_id="target-1",
        generation="7",
        deadline_utc=_stamp(datetime.now(UTC) + timedelta(seconds=30)),
        grace_seconds=0,
    )

    response = await internal_deployments.drain_runtime("attempt-1", body, "internal-secret")

    assert response.status_code == 200
    assert publisher.await_count == 2
    start_call, completion_call = publisher.await_args_list
    assert start_call.kwargs["lifecycle"]["phase"] == "drain"
    assert start_call.kwargs["lifecycle"]["state"] == "updating"
    assert completion_call.kwargs["drain_complete"] is True


@pytest.mark.asyncio
async def test_reopen_route_publishes_serving_lifecycle(monkeypatch) -> None:
    from zerg.routers import internal_deployments

    _candidate_environment(monkeypatch)
    runtime = RuntimeAdmission()
    await _reopen_candidate_prepare(runtime)
    monkeypatch.setattr(internal_deployments, "runtime_admission", lambda: runtime)
    monkeypatch.setattr(
        internal_deployments,
        "get_settings",
        lambda: SimpleNamespace(internal_api_secret="internal-secret"),
    )
    monkeypatch.setattr(
        internal_deployments,
        "_catalog_admission_probe",
        AsyncMock(return_value={"available": True, "state": "closed", "depth": 0, "accepting": False, "active_label": None}),
    )
    monkeypatch.setattr(
        internal_deployments,
        "_catalog_activation_probe",
        AsyncMock(
            return_value={
                "available": True,
                "state": "open",
                "depth": 0,
                "accepting": True,
                "active_label": None,
                "activation": {
                    "image_digest": "sha256:" + "a" * 64,
                    "generation": "7",
                    "activated_at": _stamp(datetime.now(UTC)),
                },
            }
        ),
    )
    publisher = AsyncMock()
    monkeypatch.setattr(internal_deployments, "_signal_runtime_lifecycle", publisher)
    body = internal_deployments.DeploymentFenceRequest(**_reopen_payload(runtime))

    response = await internal_deployments.reopen_runtime("attempt-1", body, "internal-secret")

    assert response.status_code == 200
    publisher.assert_awaited_once()
    lifecycle = runtime.host_lifecycle()
    assert lifecycle["state"] == "serving"
    assert lifecycle["expected_back_by"] is None
    assert lifecycle["deadline"] is None
    assert lifecycle["cutoff"] is None


class _Socket:
    def __init__(self, messages: list[dict] | None = None) -> None:
        self.headers = {}
        self.query_params = {}
        self.state = SimpleNamespace()
        self.scope = {}
        self.messages = list(messages or [])
        self.sent: list[dict] = []
        self.closed: tuple[int, str] | None = None

    async def accept(self) -> None:
        return None

    async def receive_json(self):
        if self.messages:
            return self.messages.pop(0)
        raise WebSocketDisconnect(code=1000)

    async def send_json(self, value: dict) -> None:
        self.sent.append(value)

    async def close(self, *, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)


@pytest.mark.asyncio
async def test_control_hello_sends_lifecycle_then_k3_ack(monkeypatch) -> None:
    from zerg.routers import agents_control

    runtime = RuntimeAdmission()
    registry = SimpleNamespace(
        register=AsyncMock(),
        unregister=AsyncMock(),
    )
    monkeypatch.setattr(
        agents_control,
        "get_settings",
        lambda: SimpleNamespace(testing=True, single_tenant=True, auth_disabled=True),
    )
    monkeypatch.setattr(agents_control, "get_machine_control_channel_registry", lambda: registry)
    monkeypatch.setattr(agents_control, "_resolve_agents_owner_id", lambda *_args: 7)
    monkeypatch.setattr(agents_control, "runtime_admission", lambda: runtime)
    monkeypatch.setattr(agents_control, "_reconcile_console_turns_after_register", AsyncMock())
    websocket = _Socket([{"type": "hello", "device_id": "machine-1"}])

    await agents_control.machine_control_websocket(websocket)

    assert websocket.sent[0] == runtime.host_lifecycle()
    assert websocket.sent[1] == {"type": "hello_ack", "runtime_epoch": runtime.runtime_epoch, "admission": "open"}


@pytest.mark.asyncio
async def test_lifecycle_broadcast_bounds_stalled_send_lock() -> None:
    from zerg.services.machine_control_channel import MachineControlChannelRegistry

    websocket = _Socket()
    registry = MachineControlChannelRegistry()
    await registry.register(
        owner_id=7,
        device_id="machine-1",
        machine_name="machine-1",
        engine_build=None,
        supports=[],
        websocket=websocket,
    )
    connection = registry._connections[(7, "machine-1")]
    await connection.send_lock.acquire()
    try:
        sent = await asyncio.wait_for(
            registry.broadcast_host_lifecycle({"type": "host.lifecycle", "state": "updating"}, close_after=True),
            timeout=2.5,
        )
    finally:
        connection.send_lock.release()

    assert sent == 0
    assert websocket.sent == []
    assert websocket.closed == (1012, "host.lifecycle")


@pytest.mark.asyncio
async def test_final_lifecycle_publish_closes_control_and_runner_websockets(monkeypatch) -> None:
    from zerg.routers import internal_deployments
    from zerg.services import machine_control_channel
    from zerg.services import runner_connection_manager
    from zerg.services import session_pubsub
    from zerg.services.machine_control_channel import MachineControlChannelRegistry
    from zerg.websocket.manager import topic_manager

    runtime = RuntimeAdmission()
    now = datetime.now(UTC)

    async def catalog_probe(_operation: str) -> dict[str, object]:
        return {"available": True, "state": "closed", "depth": 0, "accepting": False, "active_label": None}

    await runtime.drain(
        {
            "request_id": "drain-request",
            "deployment_id": "deployment-1",
            "target_id": "target-1",
            "generation": "7",
            "deadline_utc": _stamp(now + timedelta(seconds=10)),
            "grace_seconds": 0,
            "runtime_epoch": runtime.runtime_epoch,
        },
        attempt_id="attempt-1",
        catalog_probe=catalog_probe,
    )
    lifecycle = runtime.host_lifecycle()
    control_socket = _Socket()
    registry = MachineControlChannelRegistry()
    await registry.register(
        owner_id=7,
        device_id="machine-1",
        machine_name="machine-1",
        engine_build=None,
        supports=[],
        websocket=control_socket,
    )

    class PubSub:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict]] = []

        def publish(self, topic: str, event: dict) -> None:
            self.events.append((topic, event))

    bus = PubSub()
    runner_close = AsyncMock(return_value=1)
    system_broadcast = AsyncMock()
    monkeypatch.setattr(internal_deployments, "runtime_admission", lambda: runtime)
    monkeypatch.setattr(machine_control_channel, "get_machine_control_channel_registry", lambda: registry)
    monkeypatch.setattr(
        runner_connection_manager, "get_runner_connection_manager", lambda: SimpleNamespace(close_all_for_host=runner_close)
    )
    monkeypatch.setattr(session_pubsub, "get_pubsub", lambda: bus)
    monkeypatch.setattr(topic_manager, "broadcast_to_topic", system_broadcast)

    await internal_deployments._signal_runtime_lifecycle({"state": "drained"}, drain_complete=True)

    assert control_socket.sent == [lifecycle]
    assert control_socket.closed == (1012, "host.lifecycle")
    assert bus.events[0][1]["host_lifecycle"] == lifecycle
    assert bus.events[0][1]["drain_complete"] is True
    runner_close.assert_awaited_once_with(code=1012, reason="host.lifecycle")
    system_broadcast.assert_awaited_once()
    system_event = system_broadcast.await_args.args[1]
    assert system_event["data"]["host_lifecycle"] == lifecycle


@pytest.mark.asyncio
async def test_workspace_sse_emits_final_lifecycle_then_ends(monkeypatch) -> None:
    from zerg.routers import timeline
    from zerg.services import runtime_admission as admission_module
    from zerg.services.session_pubsub import TOPIC_HOST_LIFECYCLE
    from zerg.services.session_pubsub import get_pubsub
    from zerg.services.session_pubsub import reset_pubsub_for_test

    reset_pubsub_for_test()
    initial = {
        "type": "host.lifecycle",
        "state": "serving",
        "runtime_epoch": "runtime-test",
        "attempt_id": None,
        "phase": None,
        "expected_back_by": None,
        "deadline": None,
        "cutoff": None,
    }
    runtime = SimpleNamespace(runtime_epoch="runtime-test", admission="open", host_lifecycle=lambda: initial)
    monkeypatch.setattr(admission_module, "runtime_admission", lambda: runtime)

    class Request:
        async def is_disconnected(self) -> bool:
            return False

    async def run():
        stream = timeline._live_catalog_workspace_stream(
            Request(),
            session_id=uuid4(),
            skip_initial=False,
            last_event_id=None,
            stream_epoch=None,
        )
        connected = await anext(stream)
        host_lifecycle = await anext(stream)
        await anext(stream)  # initial workspace_changed
        waiter = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        final = {**initial, "state": "updating", "attempt_id": "attempt-1", "phase": "drain"}
        get_pubsub().publish(
            TOPIC_HOST_LIFECYCLE,
            {"kind": "runtime_lifecycle", "host_lifecycle": final, "drain_complete": True},
        )
        final_frame = await waiter
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        return connected, host_lifecycle, final_frame, final

    connected, initial_frame, final_frame, expected_final = await run()
    connected_data = json.loads(connected["data"])
    assert connected["event"] == "connected"
    assert connected_data["runtime_epoch"] == "runtime-test"
    assert connected_data["admission"] == "open"
    assert initial_frame["event"] == "host_lifecycle"
    assert final_frame == {"event": "host_lifecycle", "data": json.dumps(expected_final)}
