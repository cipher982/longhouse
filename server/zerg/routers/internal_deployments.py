"""Authenticated Runtime Host cutover controls.

These routes expose process-local admission state only. Durable attempt receipts
remain control-plane-owned; a process restart creates a new runtime epoch.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from datetime import timezone
from typing import Any

from fastapi import APIRouter
from fastapi import Header
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel
from pydantic import Field

from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.client import CatalogUnavailable
from zerg.catalogd.client import call_catalogd_sync
from zerg.catalogd.schema import catalogd_ping_is_compatible
from zerg.config import get_settings
from zerg.services.catalogd_supervisor import catalogd_paths
from zerg.services.runtime_admission import runtime_admission

logger = logging.getLogger(__name__)

_request_timing: ContextVar[dict[str, Any] | None] = ContextVar("deployment_request_timing", default=None)


@contextmanager
def _timed_stage(name: str):
    """Accumulate one handler stage into this request's timing line."""
    started = time.monotonic()
    try:
        yield
    finally:
        timing = _request_timing.get()
        if timing is not None:
            stages = timing["stages"]
            stages[name] = round(stages.get(name, 0.0) + (time.monotonic() - started) * 1000, 1)


def _record_catalog_call(method: str, started: float, outcome: str) -> None:
    timing = _request_timing.get()
    if timing is not None:
        timing["catalog_calls"].append([method, round((time.monotonic() - started) * 1000, 1), outcome])


class _TimedDeploymentResponse:
    """Send the route's response, then log handler and send time as one line."""

    def __init__(self, response, *, route: str, timing: dict[str, Any], started: float, handler_ms: float) -> None:
        self.response = response
        self.route = route
        self.timing = timing
        self.started = started
        self.handler_ms = handler_ms
        self.status_code = getattr(response, "status_code", None)
        self.background = getattr(response, "background", None)

    async def __call__(self, scope, receive, send) -> None:
        send_started = time.monotonic()
        try:
            await self.response(scope, receive, send)
        finally:
            from zerg.services.event_loop_lag import deploy_window_loop_lag

            event = {
                "event": "deployment_request_timing",
                "route": self.route,
                "status_code": self.status_code,
                "handler_ms": round(self.handler_ms, 1),
                "send_ms": round((time.monotonic() - send_started) * 1000, 1),
                "total_ms": round((time.monotonic() - self.started) * 1000, 1),
                "stages": self.timing["stages"],
                "catalog_calls": self.timing["catalog_calls"],
                "loop_lag": deploy_window_loop_lag(),
            }
            logger.info("deployment_request_timing %s", json.dumps(event, separators=(",", ":")))


class DeploymentTimingRoute(APIRoute):
    """Time every cutover control request from handler entry to its last byte.

    The deployer measures these phases from outside, through the edge. This is
    the inside view: per-stage handler time, each catalog call with its outcome,
    the time to send the response, and event-loop lag in the deploy window.
    These requests are a handful per cutover, so the line is always logged.
    """

    def get_route_handler(self):
        handler = super().get_route_handler()
        route = self.path.rsplit("/", 1)[-1]

        async def timed_handler(request: Request):
            timing: dict[str, Any] = {"stages": {}, "catalog_calls": []}
            token = _request_timing.set(timing)
            started = time.monotonic()
            try:
                response = await handler(request)
            finally:
                _request_timing.reset(token)
            return _TimedDeploymentResponse(
                response,
                route=route,
                timing=timing,
                started=started,
                handler_ms=(time.monotonic() - started) * 1000,
            )

        return timed_handler


router = APIRouter(prefix="/internal/deployments", tags=["internal-deployments"], route_class=DeploymentTimingRoute)


class DeploymentFenceRequest(BaseModel):
    request_id: str = Field(..., min_length=1, max_length=128)
    deployment_id: str = Field(..., min_length=1, max_length=255)
    target_id: str = Field(..., min_length=1, max_length=255)
    generation: str = Field(..., min_length=1, max_length=255)
    deadline_utc: str = Field(..., min_length=1, max_length=80)
    grace_seconds: float = Field(..., ge=0, le=300)
    expected_back_by: str | None = Field(None, max_length=80)
    claim_deadline: str | None = Field(None, max_length=80)
    claim_cutoff: str | None = Field(None, max_length=80)
    runtime_epoch: str | None = Field(None, max_length=128)


class ReadConsistencyResponse(BaseModel):
    attempt_id: str
    runtime_epoch: str
    outcome: str
    snapshot_id: str | None = None
    catalog_revision: str | None = None
    served_session_revision: str | None = None
    machine_read_revision: str | None = None
    build_identity: dict[str, Any] | None = None
    observed_epoch: str
    checked_at: str
    receipt_id: str | None = None
    detail: str | None = None


def _require_internal_token(token: str | None) -> None:
    expected = str(get_settings().internal_api_secret or "")
    if not token or not expected or not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="internal authentication required")


async def _signal_runtime_lifecycle(
    payload: dict[str, Any],
    *,
    lifecycle: dict[str, Any] | None = None,
    drain_complete: bool = False,
) -> None:
    """Publish one lifecycle update on the control, timeline, and system channels."""
    from zerg.services.machine_control_channel import get_machine_control_channel_registry
    from zerg.services.runner_connection_manager import get_runner_connection_manager
    from zerg.services.session_pubsub import TOPIC_HOST_LIFECYCLE
    from zerg.services.session_pubsub import TOPIC_TIMELINE
    from zerg.services.session_pubsub import get_pubsub
    from zerg.websocket.manager import topic_manager

    runtime = runtime_admission()
    host_lifecycle = lifecycle or runtime.host_lifecycle()
    event = {"kind": "runtime_lifecycle", **payload, "host_lifecycle": host_lifecycle}
    if drain_complete:
        event["drain_complete"] = True
    pubsub = get_pubsub()
    pubsub.publish(TOPIC_TIMELINE, event)
    pubsub.publish(TOPIC_HOST_LIFECYCLE, event)
    await get_machine_control_channel_registry().broadcast_host_lifecycle(host_lifecycle, close_after=drain_complete)
    if drain_complete:
        await get_runner_connection_manager().close_all_for_host(code=1012, reason="host.lifecycle")
    try:
        from zerg.generated.ws_messages import Envelope

        await topic_manager.broadcast_to_topic(
            "system",
            Envelope.create(message_type="runtime_lifecycle", topic="system", data=event).model_dump(),
        )
    except Exception:
        logger.exception("Could not publish runtime_lifecycle system event")
    if drain_complete:
        try:
            await topic_manager.close_all_for_host(code=1012, reason="host.lifecycle")
        except Exception:
            logger.exception("Could not close system WebSockets after host lifecycle")


def _fence_response(payload: dict[str, Any]) -> JSONResponse:
    state = str(payload.get("state") or "unknown")
    if state in {"conflict", "unknown"}:
        status_code = 409
    elif state == "draining":
        status_code = 202
    else:
        status_code = 200

    return JSONResponse(status_code=status_code, content=payload)


async def _catalog_admission_probe(operation: str) -> dict[str, Any]:
    """Close/open the catalog writer gate or observe its truthful state."""
    method = {
        "close": "writer.admission.close.v2",
        "open": "writer.admission.open.v2",
        "status": "ping.v2",
    }.get(operation)
    if method is None:
        raise ValueError(f"unsupported catalog admission operation: {operation}")
    try:
        _database_path, catalog_socket = catalogd_paths()
        payload = await asyncio.to_thread(
            call_catalogd_sync,
            catalog_socket,
            method,
            # Closing the writer gate is a deployment control operation, not a
            # hot-path observation: the local catalogd answers it in about a
            # millisecond, but a 50 ms budget made the drain abort on ordinary
            # scheduling hiccups. A drained attempt that aborts here leaves its
            # fence in place, and every later deployment is then refused with
            # `different_active_fence` (release-canary-a, 2026-09-17).
            timeout_seconds=2.0 if operation == "close" else 0.75,
        )
    except Exception as exc:
        return {
            "available": False,
            "state": "unknown",
            "depth": None,
            "accepting": None,
            "detail": str(exc) or "catalog writer admission unavailable",
        }
    if not isinstance(payload, dict) or payload.get("ready") is not True or not isinstance(payload.get("writer_admission"), dict):
        return {
            "available": False,
            "state": "unknown",
            "depth": None,
            "accepting": None,
            "detail": "catalog writer admission is not reported",
        }
    writer = payload["writer_admission"]
    depth = writer.get("depth")
    accepting = writer.get("accepting")
    if type(depth) is not int or depth < 0 or type(accepting) is not bool:
        return {
            "available": False,
            "state": "unknown",
            "depth": None,
            "accepting": None,
            "detail": "catalog writer admission is malformed",
        }
    result = {
        "available": True,
        "state": "open" if accepting else "closed",
        "depth": depth,
        "accepting": accepting,
        "active_label": writer.get("active_label"),
        "active_age_ms": writer.get("active_age_ms"),
        "max_depth": writer.get("max_depth"),
        "detail": None,
    }
    if operation == "status":
        result["activation"] = payload.get("deployment_activation")
    return result


async def _catalog_activation_probe(operation: str, params: dict[str, Any]) -> dict[str, Any]:
    method = {
        "read": "writer.admission.activation.read.v2",
        "record": "writer.admission.activation.record.v2",
    }.get(operation)
    if method is None:
        raise ValueError(f"unsupported catalog activation operation: {operation}")
    try:
        _database_path, catalog_socket = catalogd_paths()
        payload = await asyncio.to_thread(
            call_catalogd_sync,
            catalog_socket,
            method,
            params=params,
            timeout_seconds=0.75,
        )
    except Exception as exc:
        return {
            "available": False,
            "activation": None,
            "detail": str(exc) or "catalog activation authority unavailable",
        }
    if not isinstance(payload, dict) or payload.get("ready") is not True:
        return {
            "available": False,
            "activation": None,
            "detail": "catalog activation authority is not ready",
        }
    result: dict[str, Any] = {
        "available": True,
        "activation": payload.get("activation"),
        "detail": None,
    }
    writer = payload.get("writer_admission")
    if isinstance(writer, dict):
        depth = writer.get("depth")
        accepting = writer.get("accepting")
        if type(depth) is not int or depth < 0 or type(accepting) is not bool:
            return {
                "available": False,
                "activation": result["activation"],
                "detail": "catalog writer admission is malformed",
            }
        result.update(
            {
                "state": "open" if accepting else "closed",
                "depth": depth,
                "accepting": accepting,
                "active_label": writer.get("active_label"),
                "active_age_ms": writer.get("active_age_ms"),
                "max_depth": writer.get("max_depth"),
            }
        )
    return result


async def recover_runtime_startup() -> dict[str, Any]:
    """Recover a pending runtime from an exact catalog activation receipt."""
    return await runtime_admission().recover_startup(_catalog_activation_probe, _catalog_admission_probe)


@router.post("/{attempt_id}/drain")
async def drain_runtime(
    attempt_id: str,
    body: DeploymentFenceRequest,
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
):
    _require_internal_token(x_internal_token)
    runtime = runtime_admission()

    async def publish_drain_start(lifecycle: dict[str, Any]) -> None:
        await _signal_runtime_lifecycle(
            {"state": "draining", "attempt_id": attempt_id},
            lifecycle=lifecycle,
        )

    result = await runtime.drain(
        body.model_dump(mode="json"),
        attempt_id=attempt_id,
        catalog_probe=_catalog_admission_probe,
        on_drain_start=publish_drain_start,
    )
    if result.get("state") == "drained":
        await _signal_runtime_lifecycle(result, drain_complete=True)
    elif result.get("state") == "draining":
        # Writers in flight usually finish within milliseconds. Answer when they
        # have, instead of making the deployer sleep and poll again across the
        # edge: each poll is a full round trip inside the closed-writes window.
        # Bounded short, so a stuck writer still gets a prompt "draining".
        deadline = time.monotonic() + _DRAIN_ANSWER_WAIT_SECONDS
        while result.get("state") == "draining" and time.monotonic() < deadline:
            await asyncio.sleep(_DRAIN_ANSWER_POLL_SECONDS)
            result = await _drain_status(attempt_id, request_id=body.request_id, runtime_epoch=None)
        return _fence_response(result)
    return _fence_response(result)


_DRAIN_ANSWER_WAIT_SECONDS = 0.3
_DRAIN_ANSWER_POLL_SECONDS = 0.02


@router.get("/{attempt_id}/drain")
async def get_runtime_drain(
    attempt_id: str,
    request_id: str = Query(..., min_length=1, max_length=128),
    runtime_epoch: str | None = Query(None, max_length=128),
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
):
    _require_internal_token(x_internal_token)
    return _fence_response(await _drain_status(attempt_id, request_id=request_id, runtime_epoch=runtime_epoch))


async def _drain_status(attempt_id: str, *, request_id: str, runtime_epoch: str | None) -> dict[str, Any]:
    runtime = runtime_admission()
    fence = runtime.fence
    snapshot = await runtime.snapshot()
    if isinstance(runtime_epoch, str) and runtime_epoch and runtime_epoch != runtime.runtime_epoch:
        return {
            **snapshot,
            "state": "unknown",
            "code": "runtime_epoch_mismatch",
            "message": "request belongs to a different runtime process epoch",
        }
    if fence is None or fence.attempt_id != attempt_id or fence.request_id != request_id:
        return {**snapshot, "state": "unknown", "code": "drain_fence_unknown", "message": "runtime has no matching drain fence"}
    if runtime.state not in {"draining", "drained"}:
        return {
            **snapshot,
            "state": runtime.state,
            "attempt_id": fence.attempt_id,
            "request_id": fence.request_id,
            "deployment_id": fence.deployment_id,
            "target_id": fence.target_id,
            "generation": fence.generation,
            "deadline_utc": fence.deadline_utc,
            "grace_seconds": fence.grace_seconds,
        }
    catalog = await _catalog_admission_probe("close")
    snapshot = await runtime.snapshot(catalog_admission=catalog)
    catalog_quiescent = (
        snapshot.get("catalog_admission", {}).get("available") is True
        and snapshot.get("catalog_admission", {}).get("state") == "closed"
        and snapshot.get("catalog_admission", {}).get("depth") == 0
        and snapshot.get("catalog_admission", {}).get("active_label") is None
    )
    state = runtime.state
    if state == "drained" and not catalog_quiescent:
        state = "draining"
    result = {
        **snapshot,
        "state": state,
        "attempt_id": fence.attempt_id,
        "request_id": fence.request_id,
        "deployment_id": fence.deployment_id,
        "target_id": fence.target_id,
        "generation": fence.generation,
        "deadline_utc": fence.deadline_utc,
        "grace_seconds": fence.grace_seconds,
    }
    if state == "drained":
        await _signal_runtime_lifecycle(result, drain_complete=True)
    return result


@router.post("/{attempt_id}/reopen")
async def reopen_runtime(
    attempt_id: str,
    body: DeploymentFenceRequest,
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
):
    _require_internal_token(x_internal_token)
    runtime = runtime_admission()
    with _timed_stage("catalog_status"):
        catalog = await _catalog_admission_probe("status")
    await runtime.update_catalog_admission(catalog)
    with _timed_stage("reopen"):
        result = await runtime.reopen(
            body.model_dump(mode="json"),
            attempt_id=attempt_id,
            catalog_probe=_catalog_admission_probe,
            activation_probe=_catalog_activation_probe,
        )
    if result.get("state") == "reopened":
        with _timed_stage("signal_lifecycle"):
            await _signal_runtime_lifecycle(result)
    return _fence_response(result)


def _runtime_evidence() -> dict[str, Any]:
    """Read build/catalog evidence without changing admission or candidate state."""
    try:
        from zerg.build_info import load as load_build_identity

        build = load_build_identity().as_dict()
    except Exception as exc:
        build = {"status": "missing", "error": str(exc)}
    try:
        _database_path, catalog_socket = catalogd_paths()
        catalog = call_catalogd_sync(catalog_socket, "ping.v2", timeout_seconds=0.5)
    except Exception as exc:
        catalog = {"status": "unavailable", "error": str(exc)}
    schema_version = catalog.get("schema_version") if isinstance(catalog, dict) else None
    writer_admission = catalog.get("writer_admission") if isinstance(catalog, dict) else None
    writer_state = "unknown"
    writer_ready = False
    if isinstance(writer_admission, dict):
        depth = writer_admission.get("depth")
        max_depth = writer_admission.get("max_depth")
        active_label = writer_admission.get("active_label")
        if type(depth) is int and type(max_depth) is int and max_depth > 0 and depth >= 0:
            writer_state = "active" if active_label else "idle"
            if depth > max_depth:
                writer_state = "saturated"
            writer_ready = depth <= max_depth
    catalog_ok = bool(catalog.get("ready")) and catalogd_ping_is_compatible(catalog) if isinstance(catalog, dict) else False
    build_ok = build.get("status") not in {"missing", "error"} and "error" not in build
    return {
        "image_digest": os.getenv("LONGHOUSE_IMAGE_DIGEST"),
        "generation": os.getenv("LONGHOUSE_DEPLOYMENT_GENERATION"),
        "source_sha": build.get("commit"),
        "build_identity": build,
        "schema_version": schema_version,
        "writer_state": writer_state,
        "writer_admission": writer_admission,
        "outcome": "ready" if schema_version is not None and catalog_ok and writer_ready and build_ok else "not_ready",
    }


@router.get("/evidence")
async def runtime_evidence(
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
):
    _require_internal_token(x_internal_token)
    snapshot = await runtime_admission().snapshot()
    from zerg.services.catalog_handoff import catalog_handoff_pending

    if catalog_handoff_pending() is not None:
        # The shared catalog socket may still name the predecessor's catalogd.
        return JSONResponse(
            status_code=503,
            content={
                "runtime_epoch": snapshot["runtime_epoch"],
                "admission": snapshot,
                "outcome": "not_ready",
                "detail": "catalog handoff has not taken the catalog yet",
            },
        )
    with _timed_stage("evidence"):
        evidence = await asyncio.to_thread(_runtime_evidence)
    payload = {**evidence, "runtime_epoch": snapshot["runtime_epoch"], "admission": snapshot}
    return JSONResponse(status_code=200 if evidence["outcome"] == "ready" else 503, content=payload)


# A warm candidate opens its catalog only after the predecessor releases it.
# Readiness waits briefly for that instead of answering "not ready" and making
# the deployer poll again; it never pings the catalog socket before then, because
# the shared socket path could still name the predecessor's catalogd.
_HANDOFF_READINESS_WAIT_SECONDS = 2.0


async def _catalog_handoff_readiness(attempt_id: str, runtime_epoch: str) -> JSONResponse | None:
    from zerg.services.catalog_handoff import catalog_handoff

    handoff = catalog_handoff()
    if handoff is None or handoff.ready.is_set():
        return None
    if handoff.failed is None:
        with _timed_stage("catalog_handoff_wait"):
            try:
                await asyncio.wait_for(handoff.ready.wait(), timeout=_HANDOFF_READINESS_WAIT_SECONDS)
            except TimeoutError:
                pass
    if handoff.ready.is_set():
        return None
    base = {"attempt_id": attempt_id, "runtime_epoch": runtime_epoch, "handoff": dict(handoff.timings)}
    if handoff.failed is not None:
        return JSONResponse(
            status_code=409,
            content={**base, "outcome": "conflict", "detail": f"catalog handoff failed: {handoff.failed}"},
        )
    return JSONResponse(
        status_code=503,
        content={**base, "outcome": "not_ready", "detail": "catalog handoff has not taken the catalog yet"},
    )


@router.get("/{attempt_id}/readiness")
async def runtime_readiness(
    attempt_id: str,
    expected_generation: str | None = Query(None, max_length=255),
    expected_schema_version: str | None = Query(None, max_length=255),
    runtime_epoch: str | None = Query(None, max_length=128),
    claim_deadline: str | None = Query(None, max_length=80),
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
):
    _require_internal_token(x_internal_token)
    runtime = runtime_admission()
    snapshot = await runtime.snapshot()
    if runtime_epoch and runtime_epoch != runtime.runtime_epoch:
        return JSONResponse(
            status_code=409,
            content={
                "attempt_id": attempt_id,
                "runtime_epoch": runtime.runtime_epoch,
                "outcome": "unknown",
                "detail": "runtime epoch does not belong to this process",
            },
        )
    if expected_generation:
        try:
            runtime.observe_candidate(
                attempt_id=attempt_id,
                generation=expected_generation,
            )
            snapshot = await runtime.snapshot()
        except ValueError as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "attempt_id": attempt_id,
                    "runtime_epoch": runtime.runtime_epoch,
                    "outcome": "conflict",
                    "detail": str(exc),
                },
            )
    await runtime.renew_claim(claim_deadline=claim_deadline, attempt_id=attempt_id, phase="readiness")
    snapshot = await runtime.snapshot()
    lifecycle = runtime.host_lifecycle()
    if lifecycle.get("attempt_id") == attempt_id:
        with _timed_stage("signal_lifecycle"):
            await _signal_runtime_lifecycle(
                {"state": snapshot.get("state"), "attempt_id": attempt_id},
                lifecycle=lifecycle,
            )
    handoff_response = await _catalog_handoff_readiness(attempt_id, runtime.runtime_epoch)
    if handoff_response is not None:
        return handoff_response
    # The catalogd ping is a blocking socket call; off the event loop it cannot
    # stall the clients that reconnect to this candidate at the same moment.
    with _timed_stage("evidence"):
        evidence = await asyncio.to_thread(_runtime_evidence)
    schema_version = evidence["schema_version"]
    schema_ok = expected_schema_version is None or str(schema_version) == str(expected_schema_version)
    ready = bool(
        schema_ok
        and evidence["outcome"] == "ready"
        and (not snapshot.get("startup_closed") or bool(expected_generation))
        and snapshot["state"] in {"open", "closed", "reopened"}
    )
    if ready:
        try:
            runtime.mark_candidate_ready(attempt_id=attempt_id)
        except ValueError as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "attempt_id": attempt_id,
                    "runtime_epoch": runtime.runtime_epoch,
                    "outcome": "conflict",
                    "detail": str(exc),
                },
            )
    payload = {
        "attempt_id": attempt_id,
        "runtime_epoch": runtime.runtime_epoch,
        **evidence,
        "outcome": "ready" if ready else "not_ready",
        "detail": None if ready else "build, schema, catalog, or writer state is not ready",
        "active_writers": snapshot.get("active_writers"),
        "queued_side_effects": snapshot.get("queued_side_effects"),
        "admission": snapshot,
    }
    return JSONResponse(status_code=200 if ready else 503, content=payload)


# A cutover gate is that the candidate's reads are *consistent*, not that a
# just-started catalogd answers instantly. A catalog that is not answering yet
# (socket not published, "not ready") is retried quickly inside a bounded
# budget; one that answers slowly is waited for, never abandoned and resent.
# The old loop gave each of four reads a 0.75 s timeout and slept 0.25 s after
# a failure, blocking the event loop for each try: every david010 cutover on
# 2026-10-07 lost exactly three such cycles (probe 3.26-3.83 s).
_READ_CONSISTENCY_READY_BUDGET_SECONDS = 6.0
_READ_CONSISTENCY_RETRY_SECONDS = 0.05


def _catalog_failure_name(exc: BaseException) -> str:
    cause = exc.__cause__
    if isinstance(exc, CatalogRemoteError):
        return f"{type(exc).__name__}:{exc.code}"
    return type(exc).__name__ + (f":{type(cause).__name__}" if cause is not None else "")


async def _catalog_consistency_read(catalog_socket) -> dict[str, Any]:
    """Read the probe's evidence from one catalog snapshot on the control lane."""

    deadline = time.monotonic() + _READ_CONSISTENCY_READY_BUDGET_SECONDS
    while True:
        started = time.monotonic()
        try:
            result = await asyncio.to_thread(
                call_catalogd_sync,
                catalog_socket,
                "deployment.read_consistency.v2",
                params={"observed_at": datetime.now(timezone.utc).isoformat()},
                timeout_seconds=max(0.05, deadline - started),
            )
        except (CatalogUnavailable, CatalogRemoteError) as exc:
            _record_catalog_call("deployment.read_consistency.v2", started, _catalog_failure_name(exc))
            # Not answering yet: the socket refused, or catalogd says it is not
            # ready or its control lane is full. A timeout or an expired
            # deadline means this read already had the whole budget.
            if isinstance(exc, CatalogRemoteError):
                not_answering_yet = exc.retryable and exc.code != "deadline_exceeded"
            else:
                not_answering_yet = not isinstance(exc.__cause__, TimeoutError)
            if not not_answering_yet or time.monotonic() + _READ_CONSISTENCY_RETRY_SECONDS >= deadline:
                raise
            await asyncio.sleep(_READ_CONSISTENCY_RETRY_SECONDS)
            continue
        _record_catalog_call("deployment.read_consistency.v2", started, "ok")
        return result


@router.get("/{attempt_id}/read-consistency", response_model=ReadConsistencyResponse)
async def read_consistency(
    attempt_id: str,
    runtime_epoch: str = Query(..., min_length=1, max_length=128),
    claim_deadline: str | None = Query(None, max_length=80),
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
):
    """Concrete authenticated catalog read used by cutover verification.

    This is deliberately not a generic health/probe route: it reads the
    catalog's actual metadata and schema through the same Unix-socket gateway
    used by application reads and verifies a comparable commit coordinate.
    """
    _require_internal_token(x_internal_token)
    runtime = runtime_admission()
    checked_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    base: dict[str, Any] = {
        "attempt_id": attempt_id,
        "runtime_epoch": runtime.runtime_epoch,
        "outcome": "unknown",
        "snapshot_id": None,
        "catalog_revision": None,
        "served_session_revision": None,
        "machine_read_revision": None,
        "build_identity": None,
        "observed_epoch": runtime.runtime_epoch,
        "checked_at": checked_at,
        "receipt_id": None,
        "detail": None,
    }
    if runtime_epoch != runtime.runtime_epoch:
        base["detail"] = "runtime epoch is no longer served by this process"
        return JSONResponse(status_code=409, content=base)
    with _timed_stage("renew_claim"):
        await runtime.renew_claim(claim_deadline=claim_deadline, attempt_id=attempt_id, phase="probe")
    lifecycle = runtime.host_lifecycle()
    if lifecycle.get("attempt_id") == attempt_id:
        with _timed_stage("signal_lifecycle"):
            await _signal_runtime_lifecycle(
                {"state": runtime.state, "attempt_id": attempt_id},
                lifecycle=lifecycle,
            )
    try:
        from zerg.build_info import load as load_build_identity

        with _timed_stage("build_identity"):
            build_identity = load_build_identity().as_dict()
            _database_path, catalog_socket = catalogd_paths()
        with _timed_stage("catalog_read"):
            snapshot = await _catalog_consistency_read(catalog_socket)
        # One snapshot answers every read, so the served and machine reads share
        # the catalog's commit coordinate by construction.
        catalog_revision = str(snapshot.get("commit_seq") or "")
        served_revision = catalog_revision
        machine_revision = catalog_revision
        compatible = catalogd_ping_is_compatible(snapshot)
        snapshot_id = hashlib.sha256(
            json.dumps(
                {
                    "epoch": runtime.runtime_epoch,
                    "catalog_id": snapshot.get("catalog_id"),
                    "schema_generation": snapshot.get("schema_generation"),
                    "active_session_ids": snapshot.get("active_session_ids", []),
                    "queued_session_ids": snapshot.get("queued_session_ids", []),
                    "catalog_revision": catalog_revision,
                    "served_revision": served_revision,
                    "machine_revision": machine_revision,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        if not compatible or not catalog_revision.isdecimal():
            base.update(
                {
                    "outcome": "fail",
                    "snapshot_id": snapshot_id,
                    "catalog_revision": catalog_revision,
                    "served_session_revision": served_revision,
                    "machine_read_revision": machine_revision,
                    "build_identity": build_identity,
                    "detail": "catalog schema, compatibility, or read revisions are not consistent",
                }
            )
            return JSONResponse(status_code=503, content=base)
        base.update(
            {
                "outcome": "pass",
                "snapshot_id": snapshot_id,
                "catalog_revision": catalog_revision,
                "served_session_revision": served_revision,
                "machine_read_revision": machine_revision,
                "build_identity": build_identity,
            }
        )
        try:
            runtime.mark_candidate_consistent(attempt_id=attempt_id)
        except ValueError as exc:
            base["outcome"] = "conflict"
            base["detail"] = str(exc) or "runtime consistency fence conflicts with this process"
            return JSONResponse(status_code=409, content=base)
    except (CatalogUnavailable, CatalogRemoteError) as exc:
        base["detail"] = str(exc) or "catalog consistency read unavailable"
    except Exception as exc:
        base["detail"] = str(exc) or "runtime consistency read failed"
    return JSONResponse(status_code=200 if base["outcome"] == "pass" else 503, content=base)
