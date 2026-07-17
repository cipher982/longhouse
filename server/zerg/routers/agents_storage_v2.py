"""Storage-v2 durability boundary for Machine Agent source envelopes."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import unicodedata
from datetime import UTC
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import Response
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
from zerg.services.render_object_workers import RenderObjectWorkerBusy
from zerg.services.render_object_workers import RenderObjectWorkerError
from zerg.services.render_object_workers import RenderObjectWorkerPool
from zerg.services.render_object_workers import get_render_object_worker_pool
from zerg.storage_v2.contracts import DurableReceipt
from zerg.storage_v2.contracts import EnvelopeIdentity
from zerg.storage_v2.contracts import RawExportCursor
from zerg.storage_v2.contracts import RenderDetailCursor
from zerg.storage_v2.contracts import decode_raw_export_cursor_token
from zerg.storage_v2.contracts import decode_render_detail_cursor_token
from zerg.storage_v2.contracts import envelope_id
from zerg.storage_v2.contracts import hash_records
from zerg.storage_v2.contracts import raw_export_cursor_token
from zerg.storage_v2.contracts import render_detail_cursor_token
from zerg.storage_v2.cutover import STORAGE_V2_CUTOVER
from zerg.storage_v2.media_objects import MAX_MEDIA_BYTES
from zerg.storage_v2.media_objects import MediaObjectCorruptError
from zerg.storage_v2.media_objects import MediaObjectSpec
from zerg.storage_v2.media_objects import MediaObjectValidationError
from zerg.storage_v2.raw_objects import MAX_RECORD_BYTES
from zerg.storage_v2.raw_objects import MAX_RECORDS
from zerg.storage_v2.raw_objects import RawObjectCorruptError
from zerg.storage_v2.raw_objects import RawObjectSpec
from zerg.storage_v2.raw_objects import RawObjectValidationError
from zerg.storage_v2.raw_objects import RawRecord
from zerg.storage_v2.raw_objects import validate_raw_object_spec
from zerg.storage_v2.render_objects import RenderObjectCorruptError
from zerg.storage_v2.render_objects import RenderObjectSpec
from zerg.storage_v2.render_objects import RenderObjectValidationError
from zerg.storage_v2.render_objects import RenderRecord
from zerg.storage_v2.render_objects import validate_render_object_spec

router = APIRouter(prefix="/agents/storage/v2", tags=["agents"])
logger = logging.getLogger(__name__)

MAX_WIRE_BODY_BYTES = 48 * 1024 * 1024
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
    "render",
    "media",
    "session",
    "records",
    "expected_envelope_id",
}
_EXPECTED_RECORD_FIELDS = {"source_position", "data_b64"}
_EXPECTED_MEDIA_REF_FIELDS = {"sha256", "source_position", "ref_key", "availability"}
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
_EXPECTED_RENDER_FIELDS = {"generation_id", "parser_revision", "ordering_revision", "records"}
_EXPECTED_RENDER_RECORD_FIELDS = {
    "event_id",
    "order_time_us",
    "source_position",
    "event_subordinal",
    "role",
    "content_text",
    "tool_name",
    "tool_input_json",
    "tool_output_text",
    "tool_call_id",
    "thread_id",
    "branch_kind",
    "raw_record_ordinal",
}
_RENDER_MANIFEST_LIMIT = 1_000
_RENDER_READ_BATCH = 2
_MAX_MEDIA_REFS = 1_000
_MAX_MEDIA_CLAIMS = 512


def _http_error(
    status_code: int,
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "details": details or {}},
        headers=headers,
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


async def _read_bounded_bytes(request: Request, *, maximum: int) -> bytes:
    content_encoding = request.headers.get("content-encoding", "identity").strip().lower()
    if content_encoding not in {"", "identity"}:
        raise _http_error(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "unsupported_content_encoding",
            "Storage v2 media accepts identity encoding only.",
        )
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            size = int(declared)
        except ValueError as exc:
            raise _http_error(status.HTTP_400_BAD_REQUEST, "invalid_content_length", "Content-Length is invalid.") from exc
        if not 0 < size <= maximum:
            raise _http_error(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                "media_object_too_large",
                f"Storage-v2 media object must contain 1 through {maximum} bytes.",
            )
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum:
            raise _http_error(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                "media_object_too_large",
                f"Storage-v2 media object exceeds {maximum} bytes.",
            )
        body.extend(chunk)
    if not body:
        raise _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "empty_media_object", "Media object cannot be empty.")
    return bytes(body)


def _parse_media_refs(value: object, *, range_start: int, range_end: int) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) > _MAX_MEDIA_REFS:
        raise ValueError(f"media must contain at most {_MAX_MEDIA_REFS} references")
    refs: list[dict[str, object]] = []
    seen: set[tuple[str, int, str]] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != _EXPECTED_MEDIA_REF_FIELDS:
            raise ValueError("each media reference has invalid fields")
        media_hash = _lower_hash(item["sha256"], "media.sha256")
        position = item["source_position"]
        if type(position) is not int or not range_start <= position < range_end:
            raise ValueError("media.source_position is outside the raw envelope")
        ref_key = _canonical_text(item["ref_key"], "media.ref_key", 255)
        availability = item["availability"]
        if availability not in {"available", "missing"}:
            raise ValueError("media.availability must be available or missing")
        key = (media_hash, position, ref_key)
        if key in seen:
            raise ValueError("media references must not contain duplicates")
        seen.add(key)
        refs.append(
            {
                "media_hash": media_hash,
                "source_position": position,
                "ref_key": ref_key,
                "availability": availability,
            }
        )
    return refs


def _parse_render_spec(
    value: object,
    *,
    raw_spec: RawObjectSpec,
    source_envelope_id: str,
) -> RenderObjectSpec | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _EXPECTED_RENDER_FIELDS:
        raise ValueError("render fields do not match protocol v2")
    generation_id = _canonical_uuid(value["generation_id"], "render.generation_id")
    parser_revision = _canonical_text(value["parser_revision"], "render.parser_revision", 128)
    ordering_revision = _canonical_text(value["ordering_revision"], "render.ordering_revision", 128)
    wire_records = value["records"]
    if not isinstance(wire_records, list) or len(wire_records) > MAX_RECORDS:
        raise ValueError(f"render.records must contain at most {MAX_RECORDS} items")
    records: list[RenderRecord] = []
    for item in wire_records:
        if not isinstance(item, dict) or set(item) != _EXPECTED_RENDER_RECORD_FIELDS:
            raise ValueError("each render record has invalid fields")
        for field in ("order_time_us", "source_position", "event_subordinal", "raw_record_ordinal"):
            if type(item[field]) is not int:
                raise ValueError(f"render record {field} must be an integer")
        if not raw_spec.range_start <= item["source_position"] < raw_spec.range_end:
            raise ValueError("render record source_position is outside the raw envelope")
        if not 0 <= item["raw_record_ordinal"] < len(raw_spec.records):
            raise ValueError("render record raw_record_ordinal is outside the raw envelope")
        records.append(RenderRecord(**item))
    spec = RenderObjectSpec(
        session_id=raw_spec.session_id,
        render_generation=generation_id,
        parser_revision=parser_revision,
        ordering_revision=ordering_revision,
        machine_id=raw_spec.machine_id,
        provider=raw_spec.provider,
        opaque_source_id=raw_spec.opaque_source_id,
        source_epoch=raw_spec.source_epoch,
        source_envelope_id=source_envelope_id,
        records=tuple(records),
    )
    validate_render_object_spec(spec)
    return spec


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
    media_refs = _parse_media_refs(payload["media"], range_start=range_start, range_end=range_end)

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
    render_spec = _parse_render_spec(payload["render"], raw_spec=spec, source_envelope_id=expected_envelope)
    return spec, {
        "lane": lane,
        "predecessor_source_epoch": predecessor,
        "opened_at": opened_at,
        "expected_envelope_id": expected_envelope,
        "session_facts": session_facts,
        "render_spec": render_spec,
        "media_refs": media_refs,
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
        "media_unavailable": status.HTTP_409_CONFLICT,
        "session_deleted": status.HTTP_410_GONE,
    }.get(exc.code, status.HTTP_503_SERVICE_UNAVAILABLE)
    raise _http_error(status_code, exc.code, str(exc), details=exc.details) from exc


def _media_content_type(request: Request) -> str:
    value = (request.headers.get("content-type") or "application/octet-stream").split(";", 1)[0].strip().lower()
    return _canonical_text(value, "Content-Type", 255)


@router.post("/media/claims")
async def claim_storage_v2_media(
    request: Request,
    _auth: DeviceToken | object | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> dict[str, object]:
    """Return the exact media hashes that still need verified immutable bytes."""

    payload = await _read_bounded_json(request)
    if set(payload) != {"items"} or not isinstance(payload["items"], list):
        raise _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_media_claim", "Media claim fields are invalid.")
    items = payload["items"]
    if len(items) > _MAX_MEDIA_CLAIMS:
        raise _http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_media_claim",
            f"Media claims contain more than {_MAX_MEDIA_CLAIMS} objects.",
        )
    metadata: dict[str, tuple[str, int]] = {}
    rejected: list[dict[str, str]] = []
    for item in items:
        try:
            if not isinstance(item, dict) or set(item) != {"sha256", "mime_type", "byte_size"}:
                raise ValueError("invalid_fields")
            media_hash = _lower_hash(item["sha256"], "sha256")
            mime_type = _canonical_text(item["mime_type"], "mime_type", 255)
            byte_size = item["byte_size"]
            if type(byte_size) is not int or not 0 < byte_size <= MAX_MEDIA_BYTES:
                raise ValueError("unsupported_byte_size")
            prior = metadata.get(media_hash)
            if prior is not None and prior != (mime_type, byte_size):
                raise ValueError("conflicting_metadata")
            metadata[media_hash] = (mime_type, byte_size)
        except ValueError as exc:
            raw_hash = item.get("sha256", "") if isinstance(item, dict) else ""
            rejected.append({"sha256": str(raw_hash), "reason": str(exc)})
    if rejected:
        return {"needed": [], "present": [], "rejected": rejected}
    catalogd = get_catalogd_client()
    if catalogd is None:
        raise _http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "catalog_unavailable", "Storage-v2 catalog is unavailable.")
    hashes = sorted(metadata)
    try:
        result = await catalogd.call("storage.media.exists.batch.v2", {"media_hashes": hashes})
    except CatalogRemoteError as exc:
        _raise_catalog_error(exc)
    except CatalogUnavailable as exc:
        raise _http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "catalog_unavailable", "Storage-v2 catalog is unavailable.") from exc
    rows = result.get("objects")
    if not isinstance(rows, list) or len(rows) != len(hashes):
        raise _http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "catalog_unavailable", "Media manifest result is invalid.")
    needed: list[str] = []
    present: list[str] = []
    for media_hash, row in zip(hashes, rows, strict=True):
        if not isinstance(row, dict) or row.get("media_hash") != media_hash:
            raise _http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "catalog_unavailable", "Media manifest result is invalid.")
        state_value = row.get("state")
        if state_value == "present" and row.get("byte_size") == metadata[media_hash][1]:
            present.append(media_hash)
        elif state_value == "deleted":
            rejected.append({"sha256": media_hash, "reason": "deleted"})
        else:
            needed.append(media_hash)
    return {"needed": needed, "present": present, "rejected": rejected}


@router.put("/media/{media_hash}")
async def put_storage_v2_media(
    media_hash: str,
    request: Request,
    _auth: DeviceToken | object | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> dict[str, object]:
    """Hash-verify, fsync, rename, then publish one immutable media manifest."""

    try:
        canonical_hash = _lower_hash(media_hash, "media_hash")
        mime_type = _media_content_type(request)
    except ValueError as exc:
        raise _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_media_object", str(exc)) from exc
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
            data = await _read_bounded_bytes(request, maximum=MAX_MEDIA_BYTES)
            sealed = await workers.seal_media(
                MediaObjectSpec(media_hash=canonical_hash, mime_type=mime_type, data=data),
                lane=lane,
            )
        catalogd = get_catalogd_client()
        if catalogd is None:
            raise CatalogUnavailable("catalogd is not supervised")
        result = await catalogd.call(
            "storage.media.commit.v2",
            {
                "media_hash": sealed.media_hash,
                "state": "present",
                "mime_type": sealed.mime_type,
                "byte_size": sealed.byte_size,
                "object_path": sealed.object_path,
                "session_refs": [],
                "observed_at": datetime.now(UTC).isoformat(),
            },
            timeout_seconds=2.0,
        )
    except CatalogRemoteError as exc:
        _raise_catalog_error(exc)
    except CatalogUnavailable as exc:
        raise _http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "catalog_unavailable", "Storage-v2 catalog is unavailable.") from exc
    except RawObjectWorkerBusy as exc:
        raise _http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "storage_lane_busy", "Media storage lane is full.") from exc
    except (RawObjectWorkerError, MediaObjectCorruptError) as exc:
        raise _http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "storage_worker_unavailable", "Media seal failed.") from exc
    except MediaObjectValidationError as exc:
        raise _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_media_object", str(exc)) from exc
    media = result.get("media")
    if not isinstance(media, dict) or media.get("state") != "present" or media.get("media_hash") != canonical_hash:
        raise _http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "catalog_unavailable", "Media manifest commit is invalid.")
    return {
        "v": 2,
        "sha256": canonical_hash,
        "mime_type": media["mime_type"],
        "byte_size": media["byte_size"],
        "created": result.get("created") is True,
        "commit_seq": result.get("commit_seq"),
    }


async def _storage_v2_media_manifest(media_hash: str) -> tuple[str, dict[str, object]]:
    try:
        canonical_hash = _lower_hash(media_hash, "media_hash")
    except ValueError as exc:
        raise _http_error(status.HTTP_404_NOT_FOUND, "media_not_found", "Media object was not found.") from exc
    catalogd = get_catalogd_client()
    if catalogd is None:
        raise _http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "catalog_unavailable", "Storage-v2 catalog is unavailable.")
    try:
        result = await catalogd.call(
            "storage.media.read.v2",
            {"media_hash": canonical_hash, "session_id": None, "limit": 1},
        )
        media = result.get("media")
        if result.get("found") is not True or not isinstance(media, dict) or media.get("state") != "present":
            raise _http_error(status.HTTP_404_NOT_FOUND, "media_not_found", "Media object was not found.")
    except CatalogRemoteError as exc:
        _raise_catalog_error(exc)
    except CatalogUnavailable as exc:
        raise _http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "catalog_unavailable", "Storage-v2 catalog is unavailable.") from exc
    return canonical_hash, media


@router.get("/media/{media_hash}/blob")
async def get_storage_v2_media(
    media_hash: str,
    _auth: DeviceToken | object | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> Response:
    canonical_hash, media = await _storage_v2_media_manifest(media_hash)
    try:
        decoded = await get_raw_object_worker_pool().read_media(str(media["object_path"]), canonical_hash)
    except RawObjectWorkerBusy as exc:
        raise _http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "storage_lane_busy", "Media read lane is full.") from exc
    except (KeyError, RawObjectWorkerError, MediaObjectCorruptError, MediaObjectValidationError) as exc:
        raise _http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "media_read_failed", "Media object failed verification.") from exc
    data = decoded.data
    return Response(
        content=data,
        media_type=str(media["mime_type"]),
        headers={"Content-Length": str(len(data)), "X-Media-Sha256": canonical_hash},
    )


@router.head("/media/{media_hash}")
async def head_storage_v2_media(
    media_hash: str,
    _auth: DeviceToken | object | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> Response:
    canonical_hash, media = await _storage_v2_media_manifest(media_hash)
    return Response(
        status_code=status.HTTP_200_OK,
        media_type=str(media["mime_type"]),
        headers={"Content-Length": str(media["byte_size"]), "X-Media-Sha256": canonical_hash},
    )


@router.get("/capabilities")
async def storage_v2_capabilities(
    request: Request,
    auth_token: DeviceToken | object | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> dict[str, object]:
    settings = get_settings()
    payload_machine = request.headers.get("X-Longhouse-Machine-Id") or request.query_params.get("machine_id")
    try:
        machine_id = _authenticated_machine_id(auth_token, {"machine_id": payload_machine})
    except ValueError as exc:
        raise _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_machine", str(exc)) from exc
    return {
        "protocol_version": 2,
        "cutover": STORAGE_V2_CUTOVER,
        "tenant_id": settings.archive_primary_tenant_id,
        "machine_id": machine_id,
        "ingest_path": "/api/agents/storage/v2/envelopes",
        "max_wire_body_bytes": MAX_WIRE_BODY_BYTES,
        "max_raw_record_bytes": MAX_RECORD_BYTES,
        "max_records": MAX_RECORDS,
        "media_claim_path": "/api/agents/storage/v2/media/claims",
        "media_upload_path_template": "/api/agents/storage/v2/media/{sha256}",
        "max_media_bytes": MAX_MEDIA_BYTES,
        "max_media_claims": _MAX_MEDIA_CLAIMS,
        "range_kinds": ["byte_offset", "record_ordinal"],
        "lanes": ["live", "repair"],
        "lane_header": "X-Longhouse-Storage-Lane",
    }


@router.get("/source-epochs/{source_epoch}/manifest")
async def storage_v2_source_epoch_manifest(
    source_epoch: UUID,
    after_position: int | None = Query(None, ge=0),
    limit: int = Query(1000, ge=1, le=1000),
    auth_token: DeviceToken | object | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> dict[str, object]:
    """Return bounded per-range proof for one authenticated machine source."""

    machine_id = _authenticated_machine_id(auth_token, {})
    catalogd = get_catalogd_client()
    if catalogd is None:
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "catalog_unavailable",
            "Storage-v2 catalog is temporarily unavailable.",
        )
    try:
        result = await catalogd.call(
            "storage.source_epoch.manifest.v2",
            {
                "source_epoch": str(source_epoch),
                "after_position": after_position,
                "limit": limit,
            },
        )
    except CatalogRemoteError as exc:
        _raise_catalog_error(exc)
    except CatalogUnavailable as exc:
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "catalog_unavailable",
            "Storage-v2 catalog is temporarily unavailable.",
        ) from exc
    epoch = result.get("source_epoch")
    objects = result.get("objects")
    if result.get("found") is not True or not isinstance(epoch, dict):
        raise _http_error(status.HTTP_404_NOT_FOUND, "source_epoch_not_found", "Source epoch was not found.")
    if epoch.get("machine_id") != machine_id:
        raise _http_error(status.HTTP_404_NOT_FOUND, "source_epoch_not_found", "Source epoch was not found.")
    if not isinstance(objects, list) or any(
        not isinstance(item, dict) or item.get("machine_id") != machine_id or item.get("source_epoch") != str(source_epoch)
        for item in objects
    ):
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "catalog_unavailable",
            "Source epoch manifest is invalid.",
        )
    return {
        "v": 2,
        "source_epoch": epoch,
        "objects": objects,
        "commit_seq": result.get("commit_seq"),
        "observed_at": result.get("observed_at"),
    }


async def _commit_admitted_envelope(
    request: Request,
    auth_token: DeviceToken | object | None,
    *,
    lane: str,
    raw_workers: RawObjectWorkerPool,
    render_workers: RenderObjectWorkerPool,
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

        raw_task = asyncio.create_task(raw_workers.seal(spec, lane=parsed["lane"]))
        render_spec = parsed["render_spec"]
        render_task = asyncio.create_task(render_workers.seal(render_spec, lane=parsed["lane"])) if render_spec is not None else None
        try:
            sealed = await raw_task
        except BaseException:
            if render_task is not None:
                await asyncio.gather(render_task, return_exceptions=True)
            raise
        if sealed.envelope_id != parsed["expected_envelope_id"]:
            raise RawObjectWorkerError("sealed raw object identity changed after admission")
        sealed_render = None
        if render_task is not None:
            try:
                sealed_render = await render_task
            except (RenderObjectWorkerBusy, RenderObjectWorkerError, RenderObjectValidationError) as exc:
                logger.warning(
                    "Render object deferred after raw seal",
                    extra={"envelope_id": sealed.envelope_id, "lane": lane, "error": str(exc)},
                )
        render_manifest = None
        if sealed_render is not None and render_spec is not None:
            render_manifest = {
                "generation_id": str(render_spec.render_generation),
                "parser_revision": render_spec.parser_revision,
                "ordering_revision": render_spec.ordering_revision,
                "object_id": sealed_render.object_id,
                "object_hash": sealed_render.object_hash,
                "payload_hash": sealed_render.payload_hash,
                "object_path": sealed_render.object_path,
                "uncompressed_size": sealed_render.uncompressed_size,
                "compressed_size": sealed_render.compressed_size,
                "event_count": sealed_render.event_count,
                "first_order_key": sealed_render.first_order_key,
                "last_order_key": sealed_render.last_order_key,
                "user_messages": sealed_render.user_messages,
                "assistant_messages": sealed_render.assistant_messages,
                "tool_calls": sealed_render.tool_calls,
                "first_user_message_preview": sealed_render.first_user_message_preview,
                "last_visible_text_preview": sealed_render.last_visible_text_preview,
            }
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
                "predecessor_source_epoch": (
                    str(parsed["predecessor_source_epoch"]) if parsed["predecessor_source_epoch"] is not None else None
                ),
                "epoch_opened_at": parsed["opened_at"].isoformat(),
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
                "render_state": "ready" if render_manifest is not None else "pending",
                "media_refs": parsed["media_refs"],
                "projectors": ["search-v2"] if render_manifest is not None else ["render-v2"],
                "render_manifest": render_manifest,
                "session_facts": parsed["session_facts"],
                "sealed_at": datetime.now(UTC).isoformat(),
            },
            timeout_seconds=2.0,
        )
        if (
            committed.get("title_generation_required") is True
            and render_manifest is not None
            and str(render_manifest.get("first_user_message_preview") or "").strip()
        ):
            from zerg.services.storage_session_titles import schedule_storage_session_title

            schedule_storage_session_title(
                {
                    "session_id": str(spec.session_id),
                    "first_user_message": render_manifest["first_user_message_preview"],
                    "provider": spec.provider,
                    "project": parsed["session_facts"].get("project"),
                    "git_branch": parsed["session_facts"].get("git_branch"),
                }
            )
        if render_manifest is not None and any(
            int(render_manifest[field] or 0) > 0 for field in ("user_messages", "assistant_messages", "tool_calls")
        ):
            from zerg.services.session_pubsub import TOPIC_TIMELINE
            from zerg.services.session_pubsub import get_pubsub

            get_pubsub().publish(TOPIC_TIMELINE, {"session_id": str(spec.session_id), "kind": "durable_content"})
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


def _cursor_order_key(cursor: RenderDetailCursor) -> tuple[int, str, str, str, str, int, int]:
    return (
        cursor.order_time_us,
        cursor.machine_id,
        cursor.provider,
        cursor.opaque_source_id,
        str(cursor.source_epoch),
        cursor.source_position,
        cursor.event_subordinal,
    )


def _render_record_order_key(decoded, record: RenderRecord) -> tuple[int, str, str, str, str, int, int]:
    spec = decoded.spec
    return (
        record.order_time_us,
        spec.machine_id,
        spec.provider,
        spec.opaque_source_id,
        str(spec.source_epoch),
        record.source_position,
        record.event_subordinal,
    )


def _manifest_first_key(manifest: dict[str, object]) -> tuple[int, str, str, str, str, int, int]:
    raw = manifest.get("first_order_key")
    if not isinstance(raw, str):
        raise ValueError("render manifest is missing its first order key")
    decoded = json.loads(raw)
    if not isinstance(decoded, list) or len(decoded) != 7:
        raise ValueError("render manifest first order key is invalid")
    return tuple(decoded)  # type: ignore[return-value]


def _manifest_last_key(manifest: dict[str, object]) -> tuple[int, str, str, str, str, int, int]:
    raw = manifest.get("last_order_key")
    if not isinstance(raw, str):
        raise ValueError("render manifest is missing its last order key")
    decoded = json.loads(raw)
    if not isinstance(decoded, list) or len(decoded) != 7:
        raise ValueError("render manifest last order key is invalid")
    return tuple(decoded)  # type: ignore[return-value]


def _render_event_wire(session_id: UUID, generation_id: UUID, decoded, record: RenderRecord) -> dict[str, object]:
    spec = decoded.spec
    try:
        seconds, microseconds = divmod(record.order_time_us, 1_000_000)
        timestamp = datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=microseconds).isoformat()
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("render event timestamp is outside the supported range") from exc
    cursor = RenderDetailCursor(
        session_id=session_id,
        render_generation=generation_id,
        order_time_us=record.order_time_us,
        machine_id=spec.machine_id,
        provider=spec.provider,
        opaque_source_id=spec.opaque_source_id,
        source_epoch=spec.source_epoch,
        source_position=record.source_position,
        event_subordinal=record.event_subordinal,
    )
    return {
        "event_id": record.event_id,
        "cursor": render_detail_cursor_token(cursor),
        "timestamp": timestamp,
        "role": record.role,
        "content_text": record.content_text,
        "tool_name": record.tool_name,
        "tool_input_json": record.tool_input_json,
        "tool_output_text": record.tool_output_text,
        "tool_call_id": record.tool_call_id,
        "thread_id": record.thread_id,
        "branch_kind": record.branch_kind,
        "raw_locator": {
            "source_envelope_id": spec.source_envelope_id,
            "raw_record_ordinal": record.raw_record_ordinal,
        },
    }


@router.get("/sessions")
async def list_storage_v2_sessions(
    before_last_activity_at: datetime | None = Query(None),
    before_session_id: UUID | None = Query(None),
    project: str | None = Query(None, min_length=1, max_length=255),
    provider: str | None = Query(None, min_length=1, max_length=32),
    include_test: bool = Query(False),
    limit: int = Query(50, ge=1, le=100),
    _auth: DeviceToken | object | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> dict[str, object]:
    if (before_last_activity_at is None) != (before_session_id is None):
        raise _http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_cursor",
            "Timeline cursor fields must both be omitted or both be supplied.",
        )
    if before_last_activity_at is not None and before_last_activity_at.utcoffset() is None:
        raise _http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_cursor",
            "Timeline cursor timestamp must include a UTC offset.",
        )
    owner_value = getattr(_auth, "owner_id", None)
    if owner_value is None:
        raise _http_error(status.HTTP_403_FORBIDDEN, "owner_required", "Storage-v2 reads require owner identity.")
    catalogd = get_catalogd_client()
    if catalogd is None:
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "catalog_unavailable",
            "The session catalog is temporarily unavailable.",
        )
    try:
        result = await catalogd.call(
            "storage.session.timeline.list.v2",
            {
                "owner_id": str(owner_value),
                "before_last_activity_at": (
                    before_last_activity_at.astimezone(UTC).isoformat() if before_last_activity_at is not None else None
                ),
                "before_session_id": str(before_session_id) if before_session_id is not None else None,
                "project": project,
                "provider": provider,
                "include_test": include_test,
                "limit": limit,
            },
        )
    except (CatalogUnavailable, CatalogRemoteError) as exc:
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "catalog_unavailable",
            "The session catalog is temporarily unavailable.",
        ) from exc
    sessions = result.get("sessions")
    if not isinstance(sessions, list):
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "catalog_response_invalid",
            "The session catalog returned an invalid timeline page.",
        )
    next_cursor = None
    if result.get("has_more") is True and sessions:
        last = sessions[-1]
        if not isinstance(last, dict):
            raise _http_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "catalog_response_invalid",
                "The session catalog returned an invalid timeline row.",
            )
        next_cursor = {
            "before_last_activity_at": last.get("last_activity_at"),
            "before_session_id": last.get("session_id"),
        }
    return {
        "v": 2,
        "sessions": sessions,
        "next_cursor": next_cursor,
        "has_more": result.get("has_more") is True,
        "commit_seq": result.get("commit_seq"),
        "observed_at": result.get("observed_at"),
    }


@router.get("/sessions/{session_id}/raw")
async def read_storage_v2_session_raw(
    session_id: UUID,
    cursor: str | None = Query(None, description="Exclusive source-ordered raw-object cursor"),
    _auth: DeviceToken | object | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> dict[str, object]:
    owner_value = getattr(_auth, "owner_id", None)
    if owner_value is None:
        raise _http_error(status.HTTP_403_FORBIDDEN, "owner_required", "Storage-v2 reads require owner identity.")
    after = None
    if cursor is not None:
        try:
            after = decode_raw_export_cursor_token(cursor)
        except ValueError as exc:
            raise _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_cursor", str(exc)) from exc
        if after.session_id != session_id:
            raise _http_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "invalid_cursor",
                "Raw cursor belongs to a different session.",
            )
    catalogd = get_catalogd_client()
    if catalogd is None:
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "catalog_unavailable",
            "The session catalog is temporarily unavailable.",
        )
    after_source_key = None
    if after is not None:
        after_source_key = json.dumps(
            [
                after.machine_id,
                after.provider,
                after.opaque_source_id,
                str(after.source_epoch),
                f"{after.range_start:020d}",
                after.envelope_id,
            ],
            separators=(",", ":"),
        )
    try:
        manifest = await catalogd.call(
            "storage.session.raw_manifest.v2",
            {
                "session_id": str(session_id),
                "owner_id": str(owner_value),
                "after_source_key": after_source_key,
                "limit": 1,
            },
        )
    except (CatalogUnavailable, CatalogRemoteError) as exc:
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "catalog_unavailable",
            "The session catalog is temporarily unavailable.",
        ) from exc
    if manifest.get("deleted") is True or manifest.get("found") is not True:
        raise _http_error(status.HTTP_404_NOT_FOUND, "session_not_found", "Session was not found.")
    objects = manifest.get("objects")
    if not isinstance(objects, list):
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "raw_manifest_invalid",
            "The catalog returned an invalid raw manifest.",
        )
    if not objects:
        return {
            "v": 2,
            "session_id": str(session_id),
            "object": None,
            "records": [],
            "next_cursor": None,
            "has_more": False,
        }
    item = objects[0]
    if not isinstance(item, dict):
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "raw_manifest_invalid",
            "The catalog returned an invalid raw-object row.",
        )
    workers = get_raw_object_worker_pool()
    try:
        decoded = await workers.read(str(item["object_path"]), str(item["object_hash"]))
        spec = decoded.spec
        if (
            spec.session_id != session_id
            or decoded.envelope_id != item.get("envelope_id")
            or decoded.object_hash != item.get("object_hash")
        ):
            raise ValueError("raw object does not match its catalog manifest")
        object_cursor = RawExportCursor(
            session_id=session_id,
            machine_id=spec.machine_id,
            provider=spec.provider,
            opaque_source_id=spec.opaque_source_id,
            source_epoch=spec.source_epoch,
            range_start=spec.range_start,
            envelope_id=decoded.envelope_id,
        )
    except (KeyError, TypeError, ValueError, RawObjectCorruptError, RawObjectWorkerError) as exc:
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "raw_read_failed",
            "The immutable raw object could not be verified.",
        ) from exc
    has_more = manifest.get("objects_truncated") is True
    return {
        "v": 2,
        "session_id": str(session_id),
        "object": {
            "envelope_id": decoded.envelope_id,
            "machine_id": spec.machine_id,
            "provider": spec.provider,
            "opaque_source_id": spec.opaque_source_id,
            "source_epoch": str(spec.source_epoch),
            "range_kind": spec.range_kind,
            "range_start": spec.range_start,
            "range_end": spec.range_end,
            "provenance_kind": spec.provenance_kind,
        },
        "records": [
            {"source_position": record.source_position, "data_b64": base64.b64encode(record.data).decode("ascii")}
            for record in spec.records
        ],
        "next_cursor": raw_export_cursor_token(object_cursor) if has_more else None,
        "has_more": has_more,
    }


async def read_storage_v2_session_events_page(
    *,
    session_id: UUID,
    owner_id: str,
    cursor: str | None,
    anchor: str,
    limit: int,
) -> dict[str, object]:
    """Read one verified render page for a known owner.

    Browser and machine routes share this physical read so the canonical
    product surfaces cannot drift back toward the cold monolith.
    """

    if anchor not in {"start", "tail"}:
        raise _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_anchor", "anchor must be start or tail")
    decoded_cursor = None
    if cursor is not None:
        try:
            decoded_cursor = decode_render_detail_cursor_token(cursor)
        except ValueError as exc:
            raise _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_cursor", str(exc)) from exc
        if decoded_cursor.session_id != session_id:
            raise _http_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "invalid_cursor",
                "Render cursor belongs to a different session.",
            )

    catalogd = get_catalogd_client()
    if catalogd is None:
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "catalog_unavailable",
            "The session catalog is temporarily unavailable.",
        )
    cursor_order_key = json.dumps(_cursor_order_key(decoded_cursor), separators=(",", ":")) if decoded_cursor is not None else None
    try:
        manifest = await catalogd.call(
            "storage.session.render_manifest.v2",
            {
                "session_id": str(session_id),
                "owner_id": owner_id,
                "generation_id": str(decoded_cursor.render_generation) if decoded_cursor is not None else None,
                "after_order_key": cursor_order_key if anchor == "start" else None,
                "before_order_key": cursor_order_key if anchor == "tail" else None,
                "limit": _RENDER_MANIFEST_LIMIT,
            },
        )
    except (CatalogUnavailable, CatalogRemoteError) as exc:
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "catalog_unavailable",
            "The session catalog is temporarily unavailable.",
        ) from exc
    if manifest.get("deleted") is True or manifest.get("found") is not True:
        raise _http_error(status.HTTP_404_NOT_FOUND, "session_not_found", "Session was not found.")
    if manifest.get("stale_generation") is True:
        raise _http_error(
            status.HTTP_409_CONFLICT,
            "stale_generation",
            "The render generation changed; restart pagination from the current generation.",
            details={"current_generation_id": manifest.get("current_generation_id")},
        )
    generation = manifest.get("generation")
    objects = manifest.get("objects")
    if manifest.get("current_generation_id") is None:
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "render_not_ready",
            "Raw history is durable but its render generation is not ready.",
        )
    if not isinstance(generation, dict) or not isinstance(objects, list):
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "render_manifest_invalid",
            "The catalog returned an invalid render manifest.",
        )
    try:
        generation_id = UUID(str(generation["generation_id"]))
        total = int(generation["event_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "render_manifest_invalid",
            "The catalog returned an invalid render generation.",
        ) from exc

    workers = get_render_object_worker_pool()
    ordered_events: list[tuple[tuple[int, str, str, str, str, int, int], dict[str, object]]] = []
    next_object_index = 0
    cursor_key = _cursor_order_key(decoded_cursor) if decoded_cursor is not None else None
    try:
        while next_object_index < len(objects):
            batch_manifests = objects[next_object_index : next_object_index + _RENDER_READ_BATCH]
            if any(not isinstance(item, dict) for item in batch_manifests):
                raise ValueError("render object manifest is invalid")
            decoded_batch = await asyncio.gather(
                *(workers.read(str(item["object_path"]), str(item["object_hash"]), lane="user") for item in batch_manifests)
            )
            for item, decoded in zip(batch_manifests, decoded_batch, strict=True):
                spec = decoded.spec
                if (
                    spec.session_id != session_id
                    or spec.render_generation != generation_id
                    or spec.source_envelope_id != item.get("source_envelope_id")
                    or decoded.object_hash != item.get("object_hash")
                ):
                    raise ValueError("render object does not match its catalog manifest")
                for record in spec.records:
                    key = _render_record_order_key(decoded, record)
                    if (anchor == "start" and (cursor_key is None or key > cursor_key)) or (
                        anchor == "tail" and (cursor_key is None or key < cursor_key)
                    ):
                        ordered_events.append((key, _render_event_wire(session_id, generation_id, decoded, record)))
            next_object_index += len(batch_manifests)
            ordered_events.sort(key=lambda item: item[0])
            if len(ordered_events) > limit:
                cutoff = ordered_events[limit][0] if anchor == "start" else ordered_events[-limit - 1][0]
                if (
                    next_object_index >= len(objects)
                    or (anchor == "start" and _manifest_first_key(objects[next_object_index]) > cutoff)
                    or (anchor == "tail" and _manifest_last_key(objects[next_object_index]) < cutoff)
                ):
                    break
    except (KeyError, TypeError, ValueError, RenderObjectCorruptError, RenderObjectWorkerError) as exc:
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "render_read_failed",
            "The immutable render generation could not be verified.",
        ) from exc

    page = ordered_events[:limit] if anchor == "start" else ordered_events[-limit:]
    has_more = len(ordered_events) > limit or next_object_index < len(objects) or manifest.get("objects_truncated") is True
    return {
        "v": 2,
        "session_id": str(session_id),
        "generation_id": str(generation_id),
        "events": [event for _, event in page],
        "next_cursor": (page[-1][1]["cursor"] if anchor == "start" else page[0][1]["cursor"]) if page and has_more else None,
        "has_more": has_more,
        "total": total,
    }


@router.get("/sessions/{session_id}/events")
async def read_storage_v2_session_events(
    session_id: UUID,
    cursor: str | None = Query(None, description="Exclusive generation-qualified render cursor"),
    anchor: str = Query("start", description="Page from the beginning or latest tail: start|tail"),
    limit: int = Query(100, ge=1, le=500),
    _auth: DeviceToken | object | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> dict[str, object]:
    owner_value = getattr(_auth, "owner_id", None)
    if owner_value is None:
        raise _http_error(status.HTTP_403_FORBIDDEN, "owner_required", "Storage-v2 reads require owner identity.")
    return await read_storage_v2_session_events_page(
        session_id=session_id,
        owner_id=str(owner_value),
        cursor=cursor,
        anchor=anchor,
        limit=limit,
    )


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
    raw_workers = get_raw_object_worker_pool()
    render_workers = get_render_object_worker_pool()
    try:
        # Raw admission bounds request parsing and raw sealing. Render sealing
        # is optional and already owns a bounded lane inside `seal`; reserving
        # render capacity here made raw-only Cursor envelopes block rendered
        # Claude/Codex traffic without doing any render work.
        async with raw_workers.admission(lane):
            return await _commit_admitted_envelope(
                request,
                auth_token,
                lane=lane,
                raw_workers=raw_workers,
                render_workers=render_workers,
            )
    except (RawObjectWorkerBusy, RenderObjectWorkerBusy) as exc:
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "storage_lane_busy",
            "Storage-v2 worker lane is full; retry the same envelope.",
            headers={
                "X-Longhouse-Storage-Backpressure": "storage_lane_busy",
                "X-Longhouse-Storage-Lane": lane,
                "Retry-After": "5",
            },
        ) from exc


__all__ = ["MAX_WIRE_BODY_BYTES", "commit_storage_v2_envelope", "router", "storage_v2_capabilities"]
