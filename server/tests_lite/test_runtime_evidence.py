"""Schema discovery must not authorize a candidate or invent catalog health."""

import asyncio
import json
import logging
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace

import pytest


@pytest.fixture
def evidence_runtime(monkeypatch, tmp_path):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'archive.db'}")
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("AUTH_DISABLED", "1")
    monkeypatch.setenv("FERNET_SECRET", Fernet.generate_key().decode())
    monkeypatch.setenv("JWT_SECRET", "runtime-evidence-test-only")
    monkeypatch.setenv("LONGHOUSE_DEPLOYMENT_PENDING", "1")
    monkeypatch.setenv("LONGHOUSE_DEPLOYMENT_GENERATION", "7")
    monkeypatch.setenv("LONGHOUSE_IMAGE_DIGEST", "sha256:" + "a" * 64)

    from fastapi.testclient import TestClient

    from zerg import build_info
    from zerg.catalogd.schema import CATALOG_SCHEMA_GENERATION
    from zerg.catalogd.schema import CATALOG_SCHEMA_VERSION
    from zerg.main import api_app
    from zerg.routers import internal_deployments
    from zerg.services.runtime_admission import RuntimeAdmission

    runtime = RuntimeAdmission()
    runtime.observe_candidate(attempt_id="owned-attempt", generation="7")
    monkeypatch.setattr(internal_deployments, "runtime_admission", lambda: runtime)
    monkeypatch.setattr(internal_deployments, "get_settings", lambda: SimpleNamespace(internal_api_secret="evidence-test-only"))
    monkeypatch.setattr(
        build_info,
        "load",
        lambda: build_info.BuildIdentity("1.0.0", "a" * 40, "a" * 8, False, "2026-09-16T00:00:00Z", "dev"),
    )
    ping = {
        "ready": True,
        "schema_version": CATALOG_SCHEMA_VERSION,
        "schema_generation": CATALOG_SCHEMA_GENERATION,
        "writer_admission": {"depth": 0, "max_depth": 32, "active_label": None},
    }
    monkeypatch.setattr(internal_deployments, "catalogd_paths", lambda: (tmp_path / "catalog.db", tmp_path / "catalog.sock"))
    monkeypatch.setattr(internal_deployments, "call_catalogd_sync", lambda *args, **kwargs: ping)
    with TestClient(api_app) as client:
        yield client, runtime, ping


def test_schema_observation_does_not_authorize_candidate(evidence_runtime):
    client, runtime, ping = evidence_runtime
    assert client.get("/internal/deployments/evidence").status_code == 401
    response = client.get("/internal/deployments/evidence", headers={"X-Internal-Token": "evidence-test-only"})
    assert response.status_code == 200
    assert response.json()["schema_version"] == ping["schema_version"]
    assert response.json()["admission"]["state"] == "closed"
    with pytest.raises(ValueError):
        runtime.mark_candidate_consistent(attempt_id="owned-attempt")

    readiness = client.get(
        "/internal/deployments/owned-attempt/readiness",
        params={"expected_generation": "7", "expected_schema_version": str(ping["schema_version"])},
        headers={"X-Internal-Token": "evidence-test-only"},
    )
    assert readiness.status_code == 200
    runtime.mark_candidate_consistent(attempt_id="owned-attempt")


def test_health_exposes_runtime_epoch_and_admission_to_untrusted_callers(evidence_runtime, monkeypatch):
    client, runtime, _ping = evidence_runtime
    import zerg.services.runtime_admission as admission_module

    monkeypatch.setattr(admission_module, "runtime_admission", lambda: runtime)
    response = client.get("/health")

    assert response.json()["runtime"] == {"epoch": runtime.runtime_epoch, "admission": "pending"}


def test_non_ascii_internal_token_is_401_not_a_server_error(evidence_runtime):
    client, _runtime, _ping = evidence_runtime
    # A non-ASCII header value reaches the app as latin-1 text; compare_digest on str raises TypeError for it.
    response = client.get("/internal/deployments/evidence", headers={"X-Internal-Token": "caf\u00e9".encode("latin-1")})
    assert response.status_code == 401


def test_readiness_claim_deadline_renewal_is_capped_by_cutoff(evidence_runtime):
    client, runtime, ping = evidence_runtime
    cutoff = runtime.host_lifecycle()["cutoff"]
    requested_deadline = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()

    response = client.get(
        "/internal/deployments/owned-attempt/readiness",
        params={
            "expected_generation": "7",
            "expected_schema_version": str(ping["schema_version"]),
            "claim_deadline": requested_deadline,
        },
        headers={"X-Internal-Token": "evidence-test-only"},
    )

    assert response.status_code == 200
    assert runtime.host_lifecycle()["deadline"] == cutoff


def test_unknown_catalog_schema_is_not_ready(evidence_runtime):
    client, runtime, ping = evidence_runtime
    ping.pop("schema_version")
    response = client.get("/internal/deployments/evidence", headers={"X-Internal-Token": "evidence-test-only"})
    assert response.status_code == 503
    assert response.json()["outcome"] == "not_ready"
    assert response.json()["schema_version"] is None
    assert runtime.state == "closed"


def _drain_payload(runtime) -> dict[str, object]:
    return {
        "request_id": "drain-request",
        "deployment_id": "deployment",
        "target_id": "target",
        "generation": "7",
        "deadline_utc": (datetime.now(timezone.utc) + timedelta(seconds=0.2)).isoformat(),
        "grace_seconds": 0.15,
        "runtime_epoch": runtime.runtime_epoch,
    }


@pytest.mark.asyncio
async def test_drain_waits_for_runtime_and_catalog_quiescence() -> None:
    from zerg.services.runtime_admission import RuntimeAdmission

    runtime = RuntimeAdmission()
    admitted, _ = await runtime.try_admit(path="/test")
    assert admitted is True
    probes: list[str] = []

    async def probe(operation: str) -> dict[str, object]:
        probes.append(operation)
        return {"available": True, "state": "closed", "depth": 0, "accepting": False}

    task = asyncio.create_task(runtime.drain(_drain_payload(runtime), attempt_id="attempt", catalog_probe=probe))
    await asyncio.sleep(0.02)
    assert task.done() is False
    await runtime.release()
    result = await task

    assert result["state"] == "drained"
    assert result["active_writers"] == 0
    assert probes and all(operation == "close" for operation in probes)


@pytest.mark.asyncio
async def test_nonblocking_drain_poll_reaches_drained_after_writer_release(monkeypatch) -> None:
    from zerg.routers import internal_deployments
    from zerg.services.runtime_admission import RuntimeAdmission

    runtime = RuntimeAdmission()
    admitted, _ = await runtime.try_admit(path="/test")
    assert admitted is True

    async def probe(operation: str) -> dict[str, object]:
        assert operation == "close"
        return {"available": True, "state": "closed", "depth": 0, "accepting": False, "active_label": None}

    monkeypatch.setattr(
        internal_deployments,
        "get_settings",
        lambda: SimpleNamespace(internal_api_secret="secret"),
    )
    monkeypatch.setattr(internal_deployments, "runtime_admission", lambda: runtime)
    monkeypatch.setattr(internal_deployments, "_catalog_admission_probe", probe)

    payload = _drain_payload(runtime)
    payload["grace_seconds"] = 0
    payload["deadline_utc"] = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()
    initial_response = await asyncio.wait_for(
        internal_deployments.drain_runtime(
            "attempt",
            body=internal_deployments.DeploymentFenceRequest(**payload),
            x_internal_token="secret",
        ),
        timeout=0.5,
    )
    assert initial_response.status_code == 202
    initial = json.loads(initial_response.body)
    assert initial["state"] == "draining"
    fence = runtime.fence
    assert fence is not None
    assert fence.request_id == payload["request_id"]

    await runtime.release()
    polled = await internal_deployments.get_runtime_drain(
        "attempt",
        request_id=str(payload["request_id"]),
        runtime_epoch=runtime.runtime_epoch,
        x_internal_token="secret",
    )

    assert polled.status_code == 200
    body = json.loads(polled.body)
    assert body["state"] == "drained"
    assert runtime.fence == fence


@pytest.mark.asyncio
async def test_drain_observes_late_catalog_depth_before_drained() -> None:
    from zerg.services.runtime_admission import RuntimeAdmission

    runtime = RuntimeAdmission()
    depths = iter((1, 0))

    async def probe(operation: str) -> dict[str, object]:
        assert operation == "close"
        return {"available": True, "state": "closed", "depth": next(depths), "accepting": False}

    result = await runtime.drain(_drain_payload(runtime), attempt_id="attempt", catalog_probe=probe)

    assert result["state"] == "drained"
    assert result["catalog_admission"]["depth"] == 0


@pytest.mark.asyncio
async def test_drain_catalog_unavailable_stays_unknown() -> None:
    from zerg.services.runtime_admission import RuntimeAdmission

    runtime = RuntimeAdmission()

    async def probe(_operation: str) -> dict[str, object]:
        return {"available": False, "state": "unknown", "depth": None, "accepting": None}

    result = await runtime.drain(_drain_payload(runtime), attempt_id="attempt", catalog_probe=probe)

    assert result["state"] == "draining"
    assert result["active_writers"] is None
    assert result["queued_side_effects"] is None


@pytest.mark.asyncio
async def test_reopened_runtime_accepts_next_drain_without_reusing_fence(monkeypatch) -> None:
    from zerg.services.runtime_admission import RuntimeAdmission

    monkeypatch.setenv("LONGHOUSE_DEPLOYMENT_PENDING", "0")
    runtime = RuntimeAdmission()
    operations: list[str] = []

    async def probe(operation: str) -> dict[str, object]:
        operations.append(operation)
        if operation == "open":
            return {"available": True, "state": "open", "depth": 0, "accepting": True, "active_label": None}
        return {"available": True, "state": "closed", "depth": 0, "accepting": False, "active_label": None}

    first = _drain_payload(runtime)
    first.update({"request_id": "first-drain", "grace_seconds": 0})
    drained = await runtime.drain(first, attempt_id="first-attempt", catalog_probe=probe)
    assert drained["state"] == "drained"

    reopened = await runtime.reopen(first, attempt_id="first-attempt", catalog_probe=probe)
    assert reopened["state"] == "reopened"

    replayed = await runtime.drain(first, attempt_id="first-attempt", catalog_probe=probe)
    assert replayed["state"] == "reopened"
    assert operations == ["close", "open"]

    next_request = {**first, "request_id": "next-drain", "deployment_id": "next-deployment"}
    next_drained = await runtime.drain(next_request, attempt_id="next-attempt", catalog_probe=probe)
    assert next_drained["state"] == "drained"
    assert operations == ["close", "open", "close"]


@pytest.mark.asyncio
async def test_get_drain_promotes_when_catalog_gate_and_runtime_are_quiescent(monkeypatch) -> None:
    from zerg.routers import internal_deployments
    from zerg.services.runtime_admission import RuntimeAdmission
    from zerg.services.runtime_admission import RuntimeFence

    runtime = RuntimeAdmission()
    runtime._state = "draining"
    runtime._fence = RuntimeFence(
        attempt_id="attempt",
        request_id="drain-request",
        deployment_id="deployment",
        target_id="target",
        generation="7",
        deadline_utc=_drain_payload(runtime)["deadline_utc"],
        grace_seconds=0.15,
        runtime_epoch=runtime.runtime_epoch,
        fingerprint="fingerprint",
    )
    monkeypatch.setattr(
        internal_deployments,
        "get_settings",
        lambda: SimpleNamespace(internal_api_secret="secret"),
    )

    async def probe(_operation: str) -> dict[str, object]:
        return {"available": True, "state": "closed", "depth": 0, "accepting": False}

    monkeypatch.setattr(internal_deployments, "_catalog_admission_probe", probe)
    monkeypatch.setattr(internal_deployments, "runtime_admission", lambda: runtime)
    response = await internal_deployments.get_runtime_drain(
        "attempt",
        request_id="drain-request",
        x_internal_token="secret",
    )

    assert response.status_code == 200
    assert json.loads(response.body)["state"] == "drained"
    assert runtime.state == "drained"


def test_read_consistency_reports_catalog_failure_as_503_not_conflict(evidence_runtime, monkeypatch):
    client, runtime, _ping = evidence_runtime
    conflict = client.get(
        "/internal/deployments/owned-attempt/read-consistency",
        params={"runtime_epoch": "old-epoch"},
        headers={"X-Internal-Token": "evidence-test-only"},
    )
    assert conflict.status_code == 409

    from zerg.catalogd.client import CatalogUnavailable
    from zerg.routers import internal_deployments

    monkeypatch.setattr(internal_deployments, "_READ_CONSISTENCY_READY_BUDGET_SECONDS", 0.0)
    monkeypatch.setattr(
        internal_deployments,
        "call_catalogd_sync",
        lambda *args, **kwargs: (_ for _ in ()).throw(CatalogUnavailable("catalog down")),
    )
    unavailable = client.get(
        "/internal/deployments/owned-attempt/read-consistency",
        params={"runtime_epoch": runtime.runtime_epoch},
        headers={"X-Internal-Token": "evidence-test-only"},
    )
    assert unavailable.status_code == 503
    assert unavailable.json()["outcome"] == "unknown"


def _consistency_snapshot() -> dict[str, object]:
    from zerg.catalogd.schema import CATALOG_SCHEMA_GENERATION
    from zerg.catalogd.schema import CATALOG_SCHEMA_VERSION

    return {
        "ready": True,
        "catalog_id": "11111111-1111-4111-8111-111111111111",
        "schema_version": CATALOG_SCHEMA_VERSION,
        "schema_generation": CATALOG_SCHEMA_GENERATION,
        "commit_seq": "5",
        "active_session_ids": [],
        "queued_session_ids": [],
    }


def test_read_consistency_waits_out_a_cold_catalogd(evidence_runtime, monkeypatch):
    """A just-started catalogd is not a failed cutover.

    The canary's first probe hit the catalog writer while it was still coming up
    and rolled a healthy candidate back; the same read succeeds a moment later,
    so the gate retries a catalog that is not answering yet inside its budget.
    """
    client, runtime, _ping = evidence_runtime
    # The gate is consistency *of a ready candidate*, so readiness comes first.
    runtime.mark_candidate_ready(attempt_id="owned-attempt")

    from zerg.catalogd.client import CatalogUnavailable
    from zerg.routers import internal_deployments

    attempts = {"count": 0}

    def flaky(socket_path, method, **kwargs):
        assert method == "deployment.read_consistency.v2"
        attempts["count"] += 1
        # The socket is not published yet: a refusal, not a slow answer.
        if attempts["count"] <= 3:
            raise CatalogUnavailable(f"catalogd unavailable for {method}") from ConnectionRefusedError()
        return _consistency_snapshot()

    monkeypatch.setattr(internal_deployments, "call_catalogd_sync", flaky)
    response = client.get(
        "/internal/deployments/owned-attempt/read-consistency",
        params={"runtime_epoch": runtime.runtime_epoch},
        headers={"X-Internal-Token": "evidence-test-only"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "pass"
    assert body["catalog_revision"] == body["served_session_revision"] == body["machine_read_revision"] == "5"
    assert attempts["count"] == 4


def test_read_consistency_waits_for_a_slow_answer_instead_of_resending(evidence_runtime, monkeypatch, caplog):
    """One read gets the whole budget; a slow catalog is never abandoned.

    Each david010 cutover on 2026-10-07 spent three 0.75 s timeouts plus 0.25 s
    sleeps before an answer: every abandoned read was resent behind itself.
    """
    client, runtime, _ping = evidence_runtime
    runtime.mark_candidate_ready(attempt_id="owned-attempt")
    caplog.set_level(logging.INFO, logger="zerg.routers.internal_deployments")

    from zerg.catalogd.client import CatalogUnavailable
    from zerg.routers import internal_deployments

    timeouts: list[float] = []

    def slow(socket_path, method, *, params, timeout_seconds):
        timeouts.append(timeout_seconds)
        if len(timeouts) == 1:
            return _consistency_snapshot()
        raise AssertionError("a read that answered must not be resent")

    monkeypatch.setattr(internal_deployments, "call_catalogd_sync", slow)
    response = client.get(
        "/internal/deployments/owned-attempt/read-consistency",
        params={"runtime_epoch": runtime.runtime_epoch},
        headers={"X-Internal-Token": "evidence-test-only"},
    )
    assert response.json()["outcome"] == "pass"
    assert len(timeouts) == 1 and timeouts[0] > 5.0
    timing = next(
        json.loads(record.getMessage().split(" ", 1)[1])
        for record in caplog.records
        if record.getMessage().startswith("deployment_request_timing")
    )
    assert timing["route"] == "read-consistency"
    assert timing["status_code"] == 200
    assert [call[0] for call in timing["catalog_calls"]] == ["deployment.read_consistency.v2"]
    assert set(timing["stages"]) >= {"renew_claim", "build_identity", "catalog_read"}

    def timed_out(socket_path, method, *, params, timeout_seconds):
        timeouts.append(timeout_seconds)
        raise CatalogUnavailable(f"catalogd unavailable for {method}") from TimeoutError()

    monkeypatch.setattr(internal_deployments, "call_catalogd_sync", timed_out)
    response = client.get(
        "/internal/deployments/owned-attempt/read-consistency",
        params={"runtime_epoch": runtime.runtime_epoch},
        headers={"X-Internal-Token": "evidence-test-only"},
    )
    # The whole budget elapsed inside that one read: report it, do not resend.
    assert response.status_code == 503
    assert len(timeouts) == 2

    from zerg.catalogd.client import CatalogRemoteError
    from zerg.catalogd.protocol import CatalogRpcError

    def expired(socket_path, method, *, params, timeout_seconds):
        timeouts.append(timeout_seconds)
        raise CatalogRemoteError(
            CatalogRpcError(code="deadline_exceeded", message="request deadline exceeded", retryable=True, retry_after_ms=None, details={})
        )

    monkeypatch.setattr(internal_deployments, "call_catalogd_sync", expired)
    response = client.get(
        "/internal/deployments/owned-attempt/read-consistency",
        params={"runtime_epoch": runtime.runtime_epoch},
        headers={"X-Internal-Token": "evidence-test-only"},
    )
    assert response.status_code == 503
    assert len(timeouts) == 3


def _activation_payload(runtime) -> dict[str, object]:
    return {
        "request_id": "reopen-request",
        "deployment_id": "deployment",
        "target_id": "target",
        "generation": "7",
        "deadline_utc": (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(),
        "grace_seconds": 1.0,
        "runtime_epoch": runtime.runtime_epoch,
    }


@pytest.mark.asyncio
async def test_exact_activation_receipt_reopens_same_env_process_restart(monkeypatch) -> None:
    monkeypatch.setenv("LONGHOUSE_DEPLOYMENT_PENDING", "1")
    monkeypatch.setenv("LONGHOUSE_DEPLOYMENT_GENERATION", "7")
    monkeypatch.setenv("LONGHOUSE_IMAGE_DIGEST", "sha256:" + "a" * 64)
    from zerg.services.runtime_admission import RuntimeAdmission

    receipt: dict[str, str] = {}

    async def catalog_probe(operation: str) -> dict[str, object]:
        if operation == "open":
            return {"available": True, "state": "open", "depth": 0, "accepting": True, "active_label": None}
        return {"available": True, "state": "closed", "depth": 0, "accepting": False, "active_label": None}

    async def activation_probe(operation: str, params: dict[str, object]) -> dict[str, object]:
        if operation == "record":
            receipt.update({key: str(value) for key, value in params.items()})
            return {
                "available": True,
                "state": "open",
                "depth": 0,
                "accepting": True,
                "active_label": None,
                "activation": {
                    **receipt,
                    "activated_at": "2026-09-16T00:00:00+00:00",
                },
            }
        return {"available": True, "activation": {**receipt, "activated_at": "2026-09-16T00:00:00+00:00"}}

    first = RuntimeAdmission()
    first.observe_candidate(attempt_id="attempt", generation="7")
    first.mark_candidate_ready(attempt_id="attempt")
    first.mark_candidate_consistent(attempt_id="attempt")
    reopened = await first.reopen(
        _activation_payload(first),
        attempt_id="attempt",
        catalog_probe=catalog_probe,
        activation_probe=activation_probe,
    )
    assert reopened["state"] == "reopened"

    restarted = RuntimeAdmission()
    recovered = await restarted.recover_startup(activation_probe, catalog_probe)
    assert recovered["state"] == "reopened"
    admitted, _ = await restarted.try_admit(path="/api/sessions")
    assert admitted is True


@pytest.mark.asyncio
async def test_activation_receipt_mismatch_stays_closed(monkeypatch) -> None:
    monkeypatch.setenv("LONGHOUSE_DEPLOYMENT_PENDING", "1")
    monkeypatch.setenv("LONGHOUSE_DEPLOYMENT_GENERATION", "8")
    monkeypatch.setenv("LONGHOUSE_IMAGE_DIGEST", "sha256:" + "b" * 64)
    from zerg.services.runtime_admission import RuntimeAdmission

    opened = False

    async def catalog_probe(operation: str) -> dict[str, object]:
        nonlocal opened
        opened = operation == "open"
        return {"available": True, "state": "open", "depth": 0, "accepting": True, "active_label": None}

    async def activation_probe(_operation: str, _params: dict[str, object]) -> dict[str, object]:
        return {
            "available": True,
            "activation": {
                "image_digest": "sha256:" + "a" * 64,
                "generation": "7",
                "activated_at": "2026-09-16T00:00:00+00:00",
            },
        }

    runtime = RuntimeAdmission()
    recovered = await runtime.recover_startup(activation_probe, catalog_probe)
    assert recovered["state"] == "closed"
    assert recovered["code"] == "activation_mismatch"
    assert opened is False
    admitted, _ = await runtime.try_admit(path="/api/sessions")
    assert admitted is False


@pytest.mark.asyncio
async def test_unavailable_activation_evidence_stays_closed(monkeypatch) -> None:
    monkeypatch.setenv("LONGHOUSE_DEPLOYMENT_PENDING", "1")
    monkeypatch.setenv("LONGHOUSE_DEPLOYMENT_GENERATION", "7")
    monkeypatch.setenv("LONGHOUSE_IMAGE_DIGEST", "sha256:" + "a" * 64)
    from zerg.services.runtime_admission import RuntimeAdmission

    async def activation_probe(_operation: str, _params: dict[str, object]) -> dict[str, object]:
        return {"available": False, "activation": None, "detail": "catalog unavailable"}

    async def catalog_probe(_operation: str) -> dict[str, object]:
        raise AssertionError("catalog must not open without activation evidence")

    runtime = RuntimeAdmission()
    recovered = await runtime.recover_startup(activation_probe, catalog_probe)
    assert recovered["state"] == "unknown"
    admitted, _ = await runtime.try_admit(path="/api/sessions")
    assert admitted is False


def test_mounted_control_can_reopen_while_tenant_writes_are_closed(evidence_runtime, monkeypatch):
    from fastapi.testclient import TestClient
    from starlette.routing import Mount

    from zerg.main import api_app
    from zerg.main import app
    from zerg.routers import internal_deployments
    from zerg.services import runtime_admission as admission_module

    _client, runtime, _ping = evidence_runtime
    monkeypatch.setattr(admission_module, "runtime_admission", lambda: runtime)
    monkeypatch.setattr(app.router, "routes", [Mount("/api", app=api_app)])
    activation = {}

    async def catalog_probe(operation):
        return {"available": True, "state": "closed", "depth": 0, "accepting": False, "active_label": None}

    async def activation_probe(operation, params):
        activation.update(params)
        return {
            "available": True,
            "state": "open",
            "depth": 0,
            "accepting": True,
            "active_label": None,
            "activation": {**activation, "activated_at": datetime.now(timezone.utc).isoformat()},
        }

    monkeypatch.setattr(internal_deployments, "_catalog_admission_probe", catalog_probe)
    monkeypatch.setattr(internal_deployments, "_catalog_activation_probe", activation_probe)
    runtime.mark_candidate_ready(attempt_id="owned-attempt")
    runtime.mark_candidate_consistent(attempt_id="owned-attempt")
    client = TestClient(app)
    try:
        assert client.post("/api/sessions").status_code == 503
        path = "/api/internal/deployments/owned-attempt/reopen"
        payload = _activation_payload(runtime)
        assert client.post(path, json=payload).status_code == 401
        response = client.post(path, json=payload, headers={"X-Internal-Token": "evidence-test-only"})
        assert response.status_code == 200
        assert response.json()["state"] == "reopened"
        assert activation["generation"] == "7"
    finally:
        client.close()


@pytest.mark.asyncio
async def test_refused_steer_retry_cannot_drain_another_writer(monkeypatch):
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from fastapi import HTTPException

    from zerg.routers import session_chat
    from zerg.services import runtime_admission as admission_module

    monkeypatch.setenv("LONGHOUSE_DEPLOYMENT_PENDING", "0")
    runtime = admission_module.RuntimeAdmission()
    admitted, _ = await runtime.try_admit(path="/existing-provider-write")
    assert admitted

    async def catalog_probe(_operation):
        return {"available": True, "state": "closed", "depth": 0, "accepting": False}

    payload = {**_drain_payload(runtime), "grace_seconds": 0}
    assert (await runtime.drain(payload, attempt_id="attempt", catalog_probe=catalog_probe))["state"] == "draining"
    receipt = SimpleNamespace(
        id="receipt",
        delivery_request_id="delivery",
        text="keep working",
        intent="steer",
        status="delivering",
        error_json=json.dumps({"code": "runtime_draining"}),
    )
    monkeypatch.setattr(admission_module, "runtime_admission", lambda: runtime)
    monkeypatch.setattr(session_chat, "load_live_input_receipt_by_client_request", AsyncMock(return_value=receipt))
    monkeypatch.setattr(session_chat, "_set_catalog_live_receipt_error", AsyncMock(return_value=True))
    monkeypatch.setattr(
        session_chat,
        "session_lock_manager",
        SimpleNamespace(acquire=AsyncMock(return_value=True), release=AsyncMock()),
    )
    try:
        with pytest.raises(HTTPException) as refused:
            await session_chat._retry_runtime_draining_catalog_input(
                source_session=SimpleNamespace(id=uuid4()),
                owner_id=7,
                body=session_chat.SessionInputRequest(text=receipt.text, intent="steer", client_request_id="original-operation"),
                db=None,
                existing=receipt,
            )
        assert refused.value.status_code == 503
        assert (await runtime.snapshot())["state"] == "draining"
    finally:
        await runtime.release()
    assert (await runtime.snapshot())["state"] == "drained"


@pytest.mark.asyncio
async def test_runtime_restarting_replay_refusal_remains_retryable(monkeypatch):
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from fastapi import HTTPException
    from fastapi.responses import JSONResponse

    from zerg.routers import session_chat

    body = session_chat.SessionInputRequest(
        text="keep working",
        intent="auto",
        client_request_id="restart-retry-1",
    )
    receipt = SimpleNamespace(
        id="receipt",
        delivery_request_id="delivery",
        text=body.text,
        intent=body.intent,
        status="delivering",
        error_json=json.dumps({"code": "runtime_restarting"}),
    )
    update_receipt = AsyncMock(return_value=True)
    lock_manager = SimpleNamespace(acquire=AsyncMock(return_value=True), release=AsyncMock())
    monkeypatch.setattr(session_chat, "load_live_input_receipt_by_client_request", AsyncMock(return_value=receipt))
    monkeypatch.setattr(session_chat, "_set_catalog_live_receipt_error", update_receipt)
    monkeypatch.setattr(
        session_chat,
        "_build_managed_local_chat_response",
        AsyncMock(
            return_value=JSONResponse(
                status_code=503,
                content={"error_code": "runtime_restarting", "error": "Runtime is restarting"},
            )
        ),
    )
    monkeypatch.setattr(session_chat, "session_lock_manager", lock_manager)

    with pytest.raises(HTTPException) as refused:
        await session_chat._retry_runtime_draining_catalog_input(
            source_session=SimpleNamespace(id=uuid4()),
            owner_id=7,
            body=body,
            db=None,
            existing=receipt,
        )

    assert refused.value.status_code == 503
    assert refused.value.detail["error_code"] == "runtime_restarting"
    assert update_receipt.await_args_list[-1].kwargs["error"]["error_code"] == "runtime_restarting"
    lock_manager.release.assert_awaited_once()
