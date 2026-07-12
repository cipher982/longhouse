"""Storage-v2 durability boundary for Machine Agent source envelopes."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import unicodedata
from datetime import UTC
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status

from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.client import CatalogUnavailable
from zerg.config import get_settings
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.models.device_token import DeviceToken
from zerg.services.catalogd_supervisor import get_catalogd_client
from zerg.services.raw_object_workers import RawObjectWorkerBusy
from zerg.services.raw_object_workers import RawObjectWorkerError
from zerg.services.raw_object_workers import RawObjectWorkerPool
from zerg.services.raw_object_workers import get_raw_object_worker_pool
from zerg.storage_v2.contracts import DurableReceipt
from zerg.storage_v2.contracts import EnvelopeIdentity
from zerg.storage_v2.contracts import envelope_id
from zerg.storage_v2.contracts import hash_records
from zerg.storage_v2.raw_objects import MAX_RECORD_BYTES
from zerg.storage_v2.raw_objects import MAX_RECORDS
from zerg.storage_v2.raw_objects import RawObjectSpec
from zerg.storage_v2.raw_objects import RawObjectValidationError
from zerg.storage_v2.raw_objects import RawRecord
from zerg.storage_v2.raw_objects import validate_raw_object_spec

router = APIRouter(prefix="/agents/storage/v2", tags=["agents"])

MAX_WIRE_BODY_BYTES = 6 * 1024 * 1024
PROJECTORS = ("render-v2", "search-v2", "worklog-v2")
_EXPECTED_ENVELOPE_FIELDS = {
    "protocol_version",
    "tenant_id",
    "machine_id",
    "session_id",
    "provider",
    "opaque_source_id",
    "source_epoch",
    "predecessor_source_epoch",
    "epoch_opened_at",
    "range_kind",
    "range_start",
    "range_end",
    "session",
    "records",
    "expected_envelope_id",
}
_EXPECTED_RECORD_FIELDS = {"source_position", "data_b64"}
_EXPECTED_SESSION_FIELDS = {
    "environment",
    "project",
    "cwd",
    "git_repo",
    "git_branch",
    "started_at",
    "last_activity_at",
    "ended_at",
    "origin_kind",
    "hidden_from_default_timeline",
    "launch_actor",
    "launch_surface",
}


def _http_error(status_code: int, code: str, message: str, *, details: dict[str, Any] | None = None) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "details": details or {}},
    )


def _canonical_text(value: object, field: str, maximum_bytes: int) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{field} must already be NFC-normalized")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{field} exceeds {maximum_bytes} UTF-8 bytes")
    return value


def _canonical_uuid(value: object, field: str) -> UUID:
    try:
        parsed = UUID(value) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if parsed is None or str(parsed) != value:
        raise ValueError(f"{field} must be a canonical UUID")
    return parsed


def _aware_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


def _lower_hash(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{field} must be lowercase SHA-256 hex")
    return value


def _parse_session_facts(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _EXPECTED_SESSION_FIELDS:
        raise ValueError("session fields do not match protocol v2")
    result = dict(value)
    result["environment"] = _canonical_text(result["environment"], "session.environment", 32)
    for field, maximum in (
        ("project", 255),
        ("cwd", 4_096),
        ("git_repo", 500),
        ("git_branch", 255),
        ("origin_kind", 64),
        ("launch_actor", 32),
        ("launch_surface", 32),
    ):
        raw = result[field]
        if raw is not None:
            result[field] = _canonical_text(raw, f"session.{field}", maximum)
    started_at = _aware_datetime(result["started_at"], "session.started_at")
    last_activity_at = _aware_datetime(result["last_activity_at"], "session.last_activity_at")
    ended_at = _aware_datetime(result["ended_at"], "session.ended_at") if result["ended_at"] is not None else None
    if last_activity_at < started_at:
        raise ValueError("session.last_activity_at cannot precede session.started_at")
    if ended_at is not None and ended_at < started_at:
        raise ValueError("session.ended_at cannot precede session.started_at")
    result["started_at"] = started_at.isoformat()
    result["last_activity_at"] = last_activity_at.isoformat()
    result["ended_at"] = ended_at.isoformat() if ended_at is not None else None
    if type(result["hidden_from_default_timeline"]) is not bool:
        raise ValueError("session.hidden_from_default_timeline must be a boolean")
    return result


async def _read_bounded_json(request: Request) -> dict[str, Any]:
    content_encoding = request.headers.get("content-encoding", "identity").strip().lower()
    if content_encoding not in {"", "identity"}:
        raise _http_error(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "unsupported_content_encoding",
            "Storage v2 accepts identity-encoded JSON only.",
        )
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError as exc:
            raise _http_error(status.HTTP_400_BAD_REQUEST, "invalid_content_length", "Content-Length is invalid.") from exc
        if declared < 0 or declared > MAX_WIRE_BODY_BYTES:
            raise _http_error(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                "storage_envelope_too_large",
                f"Storage-v2 wire body exceeds {MAX_WIRE_BODY_BYTES} bytes.",
            )

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_WIRE_BODY_BYTES:
            raise _http_error(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                "storage_envelope_too_large",
                f"Storage-v2 wire body exceeds {MAX_WIRE_BODY_BYTES} bytes.",
            )
        body.extend(chunk)
    try:
        decoded = await asyncio.to_thread(json.loads, body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _http_error(status.HTTP_400_BAD_REQUEST, "invalid_json", "Storage-v2 body is not valid JSON.") from exc
    if not isinstance(decoded, dict):
        raise _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_envelope", "Storage-v2 body must be an object.")
    return decoded


def _parse_envelope(
    payload: dict[str, Any],
    *,
    tenant_id: str,
    machine_id: str,
    lane: str,
) -> tuple[RawObjectSpec, dict[str, Any]]:
    if set(payload) != _EXPECTED_ENVELOPE_FIELDS:
        raise ValueError("storage-v2 envelope fields do not match protocol v2")
    if payload["protocol_version"] != 2:
        raise ValueError("protocol_version must be 2")
    if payload["tenant_id"] != tenant_id:
        raise PermissionError("tenant_id does not match the authenticated Runtime Host")
    if payload["machine_id"] != machine_id:
        raise PermissionError("machine_id does not match the authenticated device token")
    provider = _canonical_text(payload["provider"], "provider", 32)
    opaque_source_id = _canonical_text(payload["opaque_source_id"], "opaque_source_id", 4_096)
    session_id = _canonical_uuid(payload["session_id"], "session_id")
    source_epoch = _canonical_uuid(payload["source_epoch"], "source_epoch")
    predecessor_value = payload["predecessor_source_epoch"]
    predecessor = _canonical_uuid(predecessor_value, "predecessor_source_epoch") if predecessor_value is not None else None
    opened_at = _aware_datetime(payload["epoch_opened_at"], "epoch_opened_at")
    range_kind = payload["range_kind"]
    if range_kind not in {"byte_offset", "record_ordinal"}:
        raise ValueError("range_kind must be byte_offset or record_ordinal")
    range_start = payload["range_start"]
    range_end = payload["range_end"]
    if type(range_start) is not int or type(range_end) is not int:
        raise ValueError("source range must use integers")
    expected_envelope = _lower_hash(payload["expected_envelope_id"], "expected_envelope_id")
    session_facts = _parse_session_facts(payload["session"])

    wire_records = payload["records"]
    if not isinstance(wire_records, list) or len(wire_records) > MAX_RECORDS:
        raise ValueError(f"records must contain at most {MAX_RECORDS} items")
    records: list[RawRecord] = []
    raw_bytes = 0
    for item in wire_records:
        if not isinstance(item, dict) or set(item) != _EXPECTED_RECORD_FIELDS:
            raise ValueError("each record must contain source_position and data_b64")
        position = item["source_position"]
        if type(position) is not int or not 0 <= position < 1 << 64:
            raise ValueError("record source_position must be an unsigned 64-bit integer")
        encoded = item["data_b64"]
        if not isinstance(encoded, str):
            raise ValueError("record data_b64 must be a string")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("record data_b64 is invalid base64") from exc
        raw_bytes += len(data)
        if raw_bytes > MAX_RECORD_BYTES:
            raise ValueError(f"raw record bytes exceed {MAX_RECORD_BYTES}")
        records.append(RawRecord(source_position=position, data=data))

    spec = RawObjectSpec(
        tenant_id=tenant_id,
        machine_id=machine_id,
        session_id=session_id,
        provider=provider,
        opaque_source_id=opaque_source_id,
        source_epoch=source_epoch,
        range_kind=range_kind,
        range_start=range_start,
        range_end=range_end,
        records=tuple(records),
    )
    validate_raw_object_spec(spec)
    identity = EnvelopeIdentity(
        tenant_id=tenant_id,
        machine_id=machine_id,
        provider=provider,
        opaque_source_id=opaque_source_id,
        source_epoch=source_epoch,
        range_kind=range_kind,
        range_start=range_start,
        range_end=range_end,
        record_hashes=hash_records(tuple(record.data for record in records)),
    )
    if envelope_id(identity) != expected_envelope:
        raise ValueError("expected_envelope_id does not match the exact source bytes")
    return spec, {
        "lane": lane,
        "predecessor_source_epoch": predecessor,
        "opened_at": opened_at,
        "expected_envelope_id": expected_envelope,
        "session_facts": session_facts,
    }


def _authenticated_machine_id(auth_token: DeviceToken | object | None, payload: dict[str, Any]) -> str:
    if auth_token is not None:
        machine_id = getattr(auth_token, "device_id", None)
    else:
        machine_id = payload.get("machine_id")
    return _canonical_text(machine_id, "machine_id", 255)


def _validated_receipt(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or value.get("raw_state") != "durable":
        raise CatalogUnavailable("catalog returned an invalid durable receipt")
    try:
        receipt = DurableReceipt(
            envelope_id=value["envelope_id"],
            object_hash=value["object_hash"],
            commit_seq=int(value["commit_seq"]),
            render_state=value["render_state"],
            media_state=value["media_state"],
            missing_media_hashes=tuple(value["missing_media_hashes"]),
        ).as_wire()
    except (KeyError, TypeError, ValueError) as exc:
        raise CatalogUnavailable("catalog returned an invalid durable receipt") from exc
    if receipt != value:
        raise CatalogUnavailable("catalog durable receipt is not canonical")
    return receipt


def _raise_catalog_error(exc: CatalogRemoteError) -> None:
    status_code = {
        "invalid_request": status.HTTP_422_UNPROCESSABLE_ENTITY,
        "source_epoch_conflict": status.HTTP_409_CONFLICT,
        "session_deleted": status.HTTP_410_GONE,
    }.get(exc.code, status.HTTP_503_SERVICE_UNAVAILABLE)
    raise _http_error(status_code, exc.code, str(exc), details=exc.details) from exc


@router.get("/capabilities")
async def storage_v2_capabilities(
    request: Request,
    auth_token: DeviceToken | object | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> dict[str, object]:
    settings = get_settings()
    payload_machine = request.query_params.get("machine_id")
    try:
        machine_id = _authenticated_machine_id(auth_token, {"machine_id": payload_machine})
    except ValueError as exc:
        raise _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_machine", str(exc)) from exc
    return {
        "protocol_version": 2,
        "tenant_id": settings.archive_primary_tenant_id,
        "machine_id": machine_id,
        "ingest_path": "/api/agents/storage/v2/envelopes",
        "max_wire_body_bytes": MAX_WIRE_BODY_BYTES,
        "max_raw_record_bytes": MAX_RECORD_BYTES,
        "max_records": MAX_RECORDS,
        "range_kinds": ["byte_offset", "record_ordinal"],
        "lanes": ["live", "repair"],
        "lane_header": "X-Longhouse-Storage-Lane",
    }


async def _commit_admitted_envelope(
    request: Request,
    auth_token: DeviceToken | object | None,
    *,
    lane: str,
    workers: RawObjectWorkerPool,
) -> dict[str, object]:
    settings = get_settings()
    tenant_id = _canonical_text(settings.archive_primary_tenant_id, "tenant_id", 255)
    try:
        payload = await _read_bounded_json(request)
        machine_id = _authenticated_machine_id(auth_token, payload)
        spec, parsed = await asyncio.to_thread(
            _parse_envelope,
            payload,
            tenant_id=tenant_id,
            machine_id=machine_id,
            lane=lane,
        )
        catalogd = get_catalogd_client()
        if catalogd is None:
            raise CatalogUnavailable("catalogd is not supervised")
        existing = await catalogd.call(
            "storage.raw_object.exists.batch.v2",
            {"envelope_ids": [parsed["expected_envelope_id"]]},
        )
        objects = existing.get("objects")
        if not isinstance(objects, list) or len(objects) != 1 or not isinstance(objects[0], dict):
            raise CatalogUnavailable("catalog returned an invalid raw-object existence result")
        if objects[0].get("receipt") is not None:
            return _validated_receipt(objects[0]["receipt"])

        await catalogd.call(
            "storage.source_epoch.open.v2",
            {
                "tenant_id": tenant_id,
                "machine_id": machine_id,
                "provider": spec.provider,
                "opaque_source_id": spec.opaque_source_id,
                "source_epoch": str(spec.source_epoch),
                "range_kind": spec.range_kind,
                "predecessor_source_epoch": (
                    str(parsed["predecessor_source_epoch"]) if parsed["predecessor_source_epoch"] is not None else None
                ),
                "opened_at": parsed["opened_at"].isoformat(),
            },
        )
        sealed = await workers.seal(spec, lane=parsed["lane"])
        if sealed.envelope_id != parsed["expected_envelope_id"]:
            raise RawObjectWorkerError("sealed raw object identity changed after admission")
        owner_value = getattr(auth_token, "owner_id", None)
        committed = await catalogd.call(
            "storage.raw_object.commit.v2",
            {
                "protocol_version": 2,
                "tenant_id": tenant_id,
                "owner_id": str(owner_value) if owner_value is not None else None,
                "session_id": str(spec.session_id),
                "machine_id": machine_id,
                "provider": spec.provider,
                "opaque_source_id": spec.opaque_source_id,
                "source_epoch": str(spec.source_epoch),
                "range_kind": spec.range_kind,
                "range_start": spec.range_start,
                "range_end": spec.range_end,
                "record_hashes": list(sealed.record_hashes),
                "envelope_id": sealed.envelope_id,
                "object_hash": sealed.object_hash,
                "payload_hash": sealed.payload_hash,
                "compressed_hash": sealed.compressed_hash,
                "object_path": sealed.object_path,
                "uncompressed_size": sealed.uncompressed_size,
                "compressed_size": sealed.compressed_size,
                "provenance_kind": spec.provenance_kind,
                "render_state": "pending",
                "media_state": "complete",
                "missing_media_hashes": [],
                "projectors": list(PROJECTORS),
                "session_facts": parsed["session_facts"],
                "sealed_at": datetime.now(UTC).isoformat(),
            },
            timeout_seconds=2.0,
        )
        return _validated_receipt(committed.get("receipt"))
    except CatalogRemoteError as exc:
        _raise_catalog_error(exc)
    except CatalogUnavailable as exc:
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "catalog_unavailable",
            "Storage-v2 catalog is temporarily unavailable.",
        ) from exc
    except RawObjectWorkerBusy as exc:
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "storage_lane_busy",
            "Storage-v2 worker lane is full; retry the same envelope.",
        ) from exc
    except RawObjectValidationError as exc:
        raise _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_envelope", str(exc)) from exc
    except RawObjectWorkerError as exc:
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "storage_worker_unavailable",
            "Storage-v2 worker failed; retry the same envelope.",
        ) from exc
    except PermissionError as exc:
        raise _http_error(status.HTTP_403_FORBIDDEN, "identity_mismatch", str(exc)) from exc
    except ValueError as exc:
        raise _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_envelope", str(exc)) from exc


@router.post("/envelopes")
async def commit_storage_v2_envelope(
    request: Request,
    auth_token: DeviceToken | object | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> dict[str, object]:
    lane = request.headers.get("X-Longhouse-Storage-Lane", "").strip().lower()
    if lane not in {"live", "repair"}:
        raise _http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_lane",
            "X-Longhouse-Storage-Lane must be live or repair.",
        )
    workers = get_raw_object_worker_pool()
    try:
        async with workers.admission(lane):
            return await _commit_admitted_envelope(request, auth_token, lane=lane, workers=workers)
    except RawObjectWorkerBusy as exc:
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "storage_lane_busy",
            "Storage-v2 worker lane is full; retry the same envelope.",
        ) from exc


__all__ = ["MAX_WIRE_BODY_BYTES", "commit_storage_v2_envelope", "router", "storage_v2_capabilities"]
