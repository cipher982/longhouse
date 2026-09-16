"""Authenticated Runtime Host cutover controls.

These routes expose process-local admission state only. Durable attempt receipts
remain control-plane-owned; a process restart creates a new runtime epoch.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from datetime import datetime
from datetime import timezone
from typing import Any

from fastapi import APIRouter
from fastapi import Header
from fastapi import HTTPException
from fastapi import Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from pydantic import Field

from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.client import CatalogUnavailable
from zerg.catalogd.client import call_catalogd_sync
from zerg.catalogd.schema import catalogd_ping_is_compatible
from zerg.config import get_settings
from zerg.services.catalogd_supervisor import catalogd_paths
from zerg.services.runtime_admission import runtime_admission

router = APIRouter(prefix="/internal/deployments", tags=["internal-deployments"])


class DeploymentFenceRequest(BaseModel):
    request_id: str = Field(..., min_length=1, max_length=128)
    deployment_id: str = Field(..., min_length=1, max_length=255)
    target_id: str = Field(..., min_length=1, max_length=255)
    generation: str = Field(..., min_length=1, max_length=255)
    deadline_utc: str = Field(..., min_length=1, max_length=80)
    grace_seconds: float = Field(..., ge=0, le=300)
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
    if not token or not expected or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="internal authentication required")


async def _signal_runtime_lifecycle(payload: dict[str, Any]) -> None:
    """Wake SSE and WebSocket clients without pretending this is durable data."""
    from zerg.generated.ws_messages import Envelope
    from zerg.services.session_pubsub import TOPIC_TIMELINE
    from zerg.services.session_pubsub import get_pubsub
    from zerg.websocket.manager import topic_manager

    event = {"kind": "runtime_lifecycle", **payload}
    get_pubsub().publish(TOPIC_TIMELINE, event)
    await topic_manager.broadcast_to_topic(
        "system",
        Envelope.create(message_type="runtime_lifecycle", topic="system", data=event).model_dump(),
    )


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
            timeout_seconds=0.05 if operation == "close" else 0.75,
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
    result = await runtime_admission().drain(
        body.model_dump(mode="json"),
        attempt_id=attempt_id,
        catalog_probe=_catalog_admission_probe,
    )
    await _signal_runtime_lifecycle(result)
    return _fence_response(result)


@router.get("/{attempt_id}/drain")
async def get_runtime_drain(
    attempt_id: str,
    request_id: str = Query(..., min_length=1, max_length=128),
    runtime_epoch: str | None = Query(None, max_length=128),
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
):
    _require_internal_token(x_internal_token)
    runtime = runtime_admission()
    fence = runtime.fence
    snapshot = await runtime.snapshot()
    if isinstance(runtime_epoch, str) and runtime_epoch and runtime_epoch != runtime.runtime_epoch:
        return _fence_response(
            {
                **snapshot,
                "state": "unknown",
                "code": "runtime_epoch_mismatch",
                "message": "request belongs to a different runtime process epoch",
            }
        )
    if fence is None or fence.attempt_id != attempt_id or fence.request_id != request_id:
        return _fence_response(
            {**snapshot, "state": "unknown", "code": "drain_fence_unknown", "message": "runtime has no matching drain fence"}
        )
    if runtime.state not in {"draining", "drained"}:
        return _fence_response(
            {
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
        )
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
    return _fence_response(
        {
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
    )


@router.post("/{attempt_id}/reopen")
async def reopen_runtime(
    attempt_id: str,
    body: DeploymentFenceRequest,
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
):
    _require_internal_token(x_internal_token)
    runtime = runtime_admission()
    catalog = await _catalog_admission_probe("status")
    await runtime.update_catalog_admission(catalog)
    result = await runtime.reopen(
        body.model_dump(mode="json"),
        attempt_id=attempt_id,
        catalog_probe=_catalog_admission_probe,
        activation_probe=_catalog_activation_probe,
    )
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
    evidence = _runtime_evidence()
    payload = {**evidence, "runtime_epoch": snapshot["runtime_epoch"], "admission": snapshot}
    return JSONResponse(status_code=200 if evidence["outcome"] == "ready" else 503, content=payload)


@router.get("/{attempt_id}/readiness")
async def runtime_readiness(
    attempt_id: str,
    expected_generation: str | None = Query(None, max_length=255),
    expected_schema_version: str | None = Query(None, max_length=255),
    runtime_epoch: str | None = Query(None, max_length=128),
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
    evidence = _runtime_evidence()
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


@router.get("/{attempt_id}/read-consistency", response_model=ReadConsistencyResponse)
async def read_consistency(
    attempt_id: str,
    runtime_epoch: str = Query(..., min_length=1, max_length=128),
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
    try:
        from zerg.build_info import load as load_build_identity

        build_identity = load_build_identity().as_dict()
        _database_path, catalog_socket = catalogd_paths()
        schema = call_catalogd_sync(catalog_socket, "schema.v2", timeout_seconds=0.75)
        active = call_catalogd_sync(
            catalog_socket,
            "session.active.list.v2",
            params={
                "limit": 1,
                "days_back": 1,
                "observed_at": datetime.now(timezone.utc).isoformat(),
            },
            timeout_seconds=0.75,
        )
        queued = call_catalogd_sync(
            catalog_socket,
            "session.input.queued.list.v2",
            params={"limit": 1},
            timeout_seconds=0.75,
        )
        ping = call_catalogd_sync(catalog_socket, "ping.v2", timeout_seconds=0.75)
        catalog_revision = str(ping.get("commit_seq") or "")
        served_revision = str(active.get("commit_seq") or "")
        machine_revision = str(queued.get("commit_seq") or "")
        schema_matches = schema.get("schema_generation") == ping.get("schema_generation")
        compatible = catalogd_ping_is_compatible(ping)
        revisions_comparable = all(revision.isdecimal() for revision in (catalog_revision, served_revision, machine_revision))
        snapshot_id = hashlib.sha256(
            json.dumps(
                {
                    "epoch": runtime.runtime_epoch,
                    "catalog_id": ping.get("catalog_id"),
                    "schema_generation": schema.get("schema_generation"),
                    "active_session_ids": active.get("session_ids", []),
                    "queued_session_ids": queued.get("session_ids", []),
                    "catalog_revision": catalog_revision,
                    "served_revision": served_revision,
                    "machine_revision": machine_revision,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        if not schema_matches or not compatible or not revisions_comparable or int(machine_revision) < int(served_revision):
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
