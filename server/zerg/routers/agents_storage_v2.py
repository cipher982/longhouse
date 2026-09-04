"""Storage-v2 durability boundary for Machine Agent source envelopes."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import unicodedata
from datetime import UTC
from datetime import datetime
from time import monotonic
from typing import Any
from uuid import UUID
from weakref import WeakValueDictionary

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import Response
from fastapi import status

from zerg.auth.caller import caller_principal
from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.client import CatalogUnavailable
from zerg.catalogd.store import storage_projectors_for_provider
from zerg.config import get_settings
from zerg.dependencies.agents_auth import owner_id_from_caller
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_caller
from zerg.models.device_token import DeviceToken
from zerg.services.catalogd_supervisor import get_catalogd_client
from zerg.services.provider_interaction_semantics import INTERACTION_PROVIDER_NOTIFICATION
from zerg.services.provider_interaction_semantics import classify_provider_interaction
from zerg.services.provider_interaction_semantics import claude_task_notification_display
from zerg.services.provider_interaction_semantics import claude_task_notification_summary
from zerg.services.provider_interaction_semantics import seed_provider_interaction_sequence_context
from zerg.services.provider_interaction_semantics import semantic_event_included
from zerg.services.provider_interaction_semantics import semantic_projection_facts
from zerg.services.raw_object_workers import RawObjectWorkerBusy
from zerg.services.raw_object_workers import RawObjectWorkerError
from zerg.services.raw_object_workers import RawObjectWorkerPool
from zerg.services.raw_object_workers import get_raw_object_worker_pool
from zerg.services.raw_object_workers import storage_v2_root
from zerg.services.render_object_workers import RenderObjectWorkerBusy
from zerg.services.render_object_workers import RenderObjectWorkerError
from zerg.services.render_object_workers import RenderObjectWorkerPool
from zerg.services.render_object_workers import get_render_object_worker_pool
from zerg.services.storage_v2_semantics import SemanticRecoveryStats
from zerg.services.storage_v2_semantics import StorageV2SemanticRecoveryError
from zerg.services.storage_v2_semantics import StorageV2SemanticRecoveryPermanentError
from zerg.services.storage_v2_semantics import enrich_render_interaction_kinds
from zerg.services.storage_v2_semantics import recover_render_interaction_kinds
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
from zerg.storage_v2.media_objects import media_object_relative_path
from zerg.storage_v2.raw_objects import MAX_RECORD_BYTES
from zerg.storage_v2.raw_objects import MAX_RECORDS
from zerg.storage_v2.raw_objects import RawObjectCorruptError
from zerg.storage_v2.raw_objects import RawObjectSpec
from zerg.storage_v2.raw_objects import RawObjectValidationError
from zerg.storage_v2.raw_objects import RawRecord
from zerg.storage_v2.raw_objects import validate_raw_object_spec
from zerg.storage_v2.render_objects import SEMANTIC_PROJECTION_VERSION
from zerg.storage_v2.render_objects import RenderObjectCorruptError
from zerg.storage_v2.render_objects import RenderObjectSpec
from zerg.storage_v2.render_objects import RenderObjectValidationError
from zerg.storage_v2.render_objects import RenderRecord
from zerg.storage_v2.render_objects import validate_render_object_spec
from zerg.utils.server_timing import ServerTimingRecorder

_SESSION_DETAIL_CATALOG_TIMEOUT_SECONDS = 4.25
_STORAGE_COMMIT_CATALOG_TIMEOUT_SECONDS = 10.0
_SESSION_DETAIL_WORKER_QUEUE_TIMEOUT_SECONDS = 4.25

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
# `facts` is optional so an engine that predates provider facts keeps
# shipping; when present it is validated as strictly as the rest.
_OPTIONAL_ENVELOPE_FIELDS = {"facts"}
_EXPECTED_PROVIDER_FACT_FIELDS = {"kind", "at", "source_position", "payload"}
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
_OPTIONAL_SESSION_FIELDS = {
    "provider_session_id",
    # Subagent lineage. Optional so an engine that predates it still ships;
    # absent means "not a subagent", which is what an older engine meant too.
    "is_subagent",
    "parent_provider_session_id",
    "parent_tool_call_id",
    "workflow_run_id",
}
_EXPECTED_RENDER_FIELDS = {"generation_id", "parser_revision", "ordering_revision", "records"}
_LEGACY_RENDER_RECORD_FIELDS = {
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
_EXPECTED_RENDER_RECORD_FIELDS = (
    _LEGACY_RENDER_RECORD_FIELDS,
    _LEGACY_RENDER_RECORD_FIELDS | {"interaction_kind"},
    _LEGACY_RENDER_RECORD_FIELDS | {"parent_uuid"},
    _LEGACY_RENDER_RECORD_FIELDS | {"interaction_kind", "parent_uuid"},
)
_RENDER_MANIFEST_LIMIT = 1_000
_RENDER_READ_BATCH = 2
_MAX_MEDIA_REFS = 1_000
_MAX_MEDIA_CLAIMS = 512
_SESSION_DETAIL_READ_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


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


def _raise_historical_storage_backpressure(decision, *, path: str, lane: str) -> None:
    from zerg.metrics import historical_admission_rejections_total

    historical_admission_rejections_total.labels(path=path, reason=decision.reason).inc()
    headers = {
        "Retry-After": str(decision.retry_after_seconds),
        "X-Longhouse-Storage-Lane": lane,
        "X-Longhouse-Storage-Backpressure": "historical_admission",
        "X-Longhouse-Storage-Admission-State": decision.reason,
    }
    raise _http_error(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "historical_admission_paused",
        "Historical storage work is paused; live traffic remains reserved.",
        details={
            "reason": decision.reason,
            "disk_free_bytes": decision.disk_free_bytes,
            "disk_free_ratio": decision.disk_free_ratio,
            "stored_bytes": decision.stored_bytes,
            "stored_ceiling_bytes": decision.stored_ceiling_bytes,
        },
        headers=headers,
    )


async def _admit_historical_storage(*, admitted_bytes: int, path: str, lane: str) -> None:
    from zerg.services.historical_admission import evaluate_historical_admission
    from zerg.services.historical_admission import tenant_stored_bytes_ceiling
    from zerg.services.storage_telemetry_snapshot import get_storage_telemetry_snapshot

    snapshot = get_storage_telemetry_snapshot()
    stored_ceiling_enabled = tenant_stored_bytes_ceiling() > 0
    decision = evaluate_historical_admission(
        root=storage_v2_root(),
        admitted_bytes=admitted_bytes,
        stored_bytes=(
            snapshot.total_stored_bytes if not stored_ceiling_enabled or (snapshot.fresh and snapshot.last_error is None) else None
        ),
    )
    if not decision.admitted:
        _raise_historical_storage_backpressure(decision, path=path, lane=lane)


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
    if (
        not isinstance(value, dict)
        or not _EXPECTED_SESSION_FIELDS.issubset(value)
        or set(value) - _EXPECTED_SESSION_FIELDS - _OPTIONAL_SESSION_FIELDS
    ):
        raise ValueError("session fields do not match protocol v2")
    result = dict(value)
    result.setdefault("provider_session_id", None)
    result["environment"] = _canonical_text(result["environment"], "session.environment", 32)
    for field, maximum in (
        ("project", 255),
        ("cwd", 4_096),
        ("git_repo", 500),
        ("git_branch", 255),
        ("origin_kind", 64),
        ("launch_actor", 32),
        ("launch_surface", 32),
        ("provider_session_id", 255),
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
    result.setdefault("is_subagent", False)
    if type(result["is_subagent"]) is not bool:
        raise ValueError("session.is_subagent must be a boolean")
    for field in ("parent_provider_session_id", "parent_tool_call_id", "workflow_run_id"):
        result.setdefault(field, None)
        if result[field] is not None and not isinstance(result[field], str):
            raise ValueError(f"session.{field} must be a string")
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
    record_payloads: list[dict[str, Any]] = []
    records_by_raw_ordinal: dict[int, list[int]] = {}
    for item in wire_records:
        if not isinstance(item, dict) or set(item) not in _EXPECTED_RENDER_RECORD_FIELDS:
            raise ValueError("each render record has invalid fields")
        for field in ("order_time_us", "source_position", "event_subordinal", "raw_record_ordinal"):
            if type(item[field]) is not int:
                raise ValueError(f"render record {field} must be an integer")
        if not raw_spec.range_start <= item["source_position"] < raw_spec.range_end:
            raise ValueError("render record source_position is outside the raw envelope")
        if not 0 <= item["raw_record_ordinal"] < len(raw_spec.records):
            raise ValueError("render record raw_record_ordinal is outside the raw envelope")
        record_payloads.append(dict(item))
        records_by_raw_ordinal.setdefault(item["raw_record_ordinal"], []).append(len(record_payloads) - 1)

    interaction_sequence_context: dict[str, object] = {}
    raw_values: list[object] = []
    for raw_record in raw_spec.records:
        try:
            raw_values.append(raw_record.data.decode("utf-8"))
        except UnicodeDecodeError:
            raw_values.append(None)
    seed_provider_interaction_sequence_context(raw_spec.provider, raw_values, interaction_sequence_context)
    for raw_ordinal, raw_json in enumerate(raw_values):
        record_indexes = records_by_raw_ordinal.get(raw_ordinal, ())
        if record_indexes:
            for record_index in record_indexes:
                record_payload = record_payloads[record_index]
                notification_text = (
                    claude_task_notification_display(raw_json) if str(raw_spec.provider or "").strip().lower() == "claude" else None
                )
                if notification_text is not None:
                    # The parser revision is allowed to lag the host. Normalize
                    # this provider envelope at the durability boundary so an
                    # old engine cannot leak its XML into a served transcript.
                    record_payload["role"] = "system"
                    record_payload["content_text"] = notification_text
                    record_payload["interaction_kind"] = INTERACTION_PROVIDER_NOTIFICATION
                supplied_kind = record_payload.get("interaction_kind")
                classification = semantic_projection_facts(
                    raw_spec.provider,
                    role=record_payload["role"],
                    content_text=record_payload["content_text"],
                    raw_json=raw_json,
                    interaction_kind=supplied_kind,
                    sequence_context=interaction_sequence_context,
                )
                computed_kind = classification["interaction_kind"]
                # Claude's raw envelope remains authoritative inside
                # semantic_projection_facts. For other providers the parser
                # is the only provider-aware source of normalized control
                # facts, so preserve its explicit fact while still allowing
                # the shared classifier to validate the shape.
                if supplied_kind is not None and supplied_kind != computed_kind:
                    logger.warning(
                        "storage-v2 parser semantic fact changed during normalization: "
                        "provider=%s supplied=%s computed=%s envelope=%s ordinal=%s",
                        raw_spec.provider,
                        supplied_kind,
                        computed_kind,
                        source_envelope_id,
                        raw_ordinal,
                    )
                record_payload["interaction_kind"] = computed_kind
            continue
        raw_role = _raw_record_role(raw_json)
        if raw_role is not None:
            classify_provider_interaction(
                raw_spec.provider,
                role=raw_role,
                content_text=None,
                raw_json=raw_json,
                source_surface="provider_file-raw-only",
                sequence_context=interaction_sequence_context,
            )

    records = [RenderRecord(**record_payload) for record_payload in record_payloads]
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
    # Report a contract violation the way every other parse failure here does,
    # so it reaches the caller as 422 `invalid_envelope`. The engine quarantines
    # a rejected envelope into health where `longhouse shipping discard` can
    # clear it; a 503 would make it retry the same invalid bytes forever.
    try:
        validate_render_object_spec(spec)
    except RenderObjectValidationError as exc:
        raise ValueError(str(exc)) from exc
    return spec


def _raw_record_role(raw_json: object) -> str | None:
    if not isinstance(raw_json, str):
        return None
    try:
        raw_value = json.loads(raw_json)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(raw_value, dict):
        return None
    message = raw_value.get("message")
    if isinstance(message, dict) and isinstance(message.get("role"), str):
        return str(message["role"])
    role = raw_value.get("role")
    if isinstance(role, str):
        return role
    if raw_value.get("type") in {"user", "assistant", "tool", "system"}:
        return str(raw_value["type"])
    return None


def _parse_envelope(
    payload: dict[str, Any],
    *,
    tenant_id: str,
    machine_id: str,
    lane: str,
) -> tuple[RawObjectSpec, dict[str, Any]]:
    if set(payload) - _OPTIONAL_ENVELOPE_FIELDS != _EXPECTED_ENVELOPE_FIELDS:
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
    provider_facts = _parse_provider_facts(
        payload.get("facts"), range_start=range_start, range_end=range_end, session_id=payload.get("session_id")
    )
    return spec, {
        "lane": lane,
        "provider_facts": provider_facts,
        "predecessor_source_epoch": predecessor,
        "opened_at": opened_at,
        "expected_envelope_id": expected_envelope,
        "session_facts": session_facts,
        "render_spec": render_spec,
        "media_refs": media_refs,
    }


def _first_provider_title(facts: list[dict[str, Any]]) -> str | None:
    """The oldest provider title in this batch, sanitized like every anchor."""
    from zerg.services.session_title import sanitize_timeline_title

    titled = sorted(
        (fact for fact in facts if fact["kind"] == "session.title" and isinstance(fact["payload"].get("title"), str)),
        key=lambda fact: int(fact["source_position"]),
    )
    for fact in titled:
        title = sanitize_timeline_title(fact["payload"]["title"], max_words=6)
        if title:
            return title
    return None


def _parse_provider_facts(value: object, *, range_start: int, range_end: int, session_id: object = None) -> list[dict[str, Any]]:
    """Wire facts as catalogd expects them; a malformed fact is dropped, never the envelope.

    Facts are derived from the bytes this envelope carries. Rejecting the
    envelope for one bad fact would park the source cursor behind it and
    stall every later transcript line, so the bad fact is logged and skipped
    and the raw commit proceeds.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        logger.warning("storage_v2 envelope facts ignored for session %s: not a list", session_id)
        return []
    facts: list[dict[str, Any]] = []
    dropped: list[str] = []
    for item in value[:1_000]:
        try:
            if not isinstance(item, dict) or set(item) != _EXPECTED_PROVIDER_FACT_FIELDS:
                raise ValueError("each fact must contain kind, at, source_position and payload")
            kind = item["kind"]
            if not isinstance(kind, str) or not kind:
                raise ValueError("fact kind must be a non-empty string")
            position = item["source_position"]
            if type(position) is not int or not range_start <= position < range_end:
                raise ValueError("fact source_position must fall inside the envelope range")
            at = _aware_datetime(item["at"], "fact at")
            payload = item["payload"]
            if not isinstance(payload, dict):
                raise ValueError("fact payload must be an object")
        except ValueError as exc:
            dropped.append(str(exc))
            continue
        facts.append({"kind": kind, "at": at.isoformat(), "source_position": position, "payload": payload})
    if len(value) > 1_000:
        dropped.append(f"facts truncated at 1000 items ({len(value)} sent)")
    if dropped:
        logger.warning(
            "storage_v2 envelope for session %s dropped %d malformed fact(s): %s",
            session_id,
            len(dropped),
            "; ".join(dropped[:5]),
        )
    return facts


async def _apply_provider_title(catalogd: Any, session_id: UUID, title: str) -> None:
    """Freeze or promote the provider's own session name.

    The store decides: an empty anchor takes the name, a Longhouse LLM title
    is promoted to it once, and a provider anchor is never rewritten. This
    runs on every path that stores a title fact, including exact replays,
    so the outcome does not depend on which batch the title arrived in.
    """
    await catalogd.call(
        "storage.session.title.complete.v2",
        {
            "session_id": str(session_id),
            "title": title,
            "completed_at": datetime.now(UTC).isoformat(),
            "source": "provider",
        },
        timeout_seconds=_STORAGE_COMMIT_CATALOG_TIMEOUT_SECONDS,
    )


def _conversation_resets(render_spec: RenderObjectSpec | None) -> list[dict[str, str | None]]:
    """Extract native-id rotations from conversation_reset boundary records.

    The engine emits a ``branch_kind="conversation_reset"`` render record when a
    provider rotates its native session id inside the same transcript (raw
    ``claude --resume`` outside Longhouse). Both ids ride in the record's
    ``tool_input_json``; the catalog commit uses them to alias the new native id
    back to this session so it resolves on the group-A read path.
    """

    if render_spec is None:
        return []
    resets: list[dict[str, str | None]] = []
    seen: set[tuple[str | None, str]] = set()
    for record in render_spec.records:
        if record.branch_kind != "conversation_reset" or not isinstance(record.tool_input_json, dict):
            continue
        new_id = str(record.tool_input_json.get("provider_session_id") or "").strip()
        previous_id = str(record.tool_input_json.get("previous_provider_session_id") or "").strip() or None
        if not new_id:
            continue
        key = (previous_id, new_id)
        if key in seen:
            continue
        seen.add(key)
        resets.append({"previous_provider_session_id": previous_id, "provider_session_id": new_id})
        if len(resets) == 32:  # matches the RPC bound; one rotation per epoch in practice
            break
    return resets


def _authenticated_machine_id(auth_token: DeviceToken | object | None, payload: dict[str, Any]) -> str:
    auth_token = caller_principal(auth_token)
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
    auth: DeviceToken | object | None = Depends(verify_agents_caller),
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
        result = await catalogd.call(
            "storage.media.exists.batch.v2",
            {"media_hashes": hashes, "owner_id": str(auth.owner_id)},
        )
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
    auth: DeviceToken | object | None = Depends(verify_agents_caller),
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
        body_hash = await asyncio.to_thread(lambda: hashlib.sha256(data).hexdigest())
        if body_hash != canonical_hash:
            raise MediaObjectValidationError("media bytes do not match media_hash")
        catalogd = get_catalogd_client()
        if catalogd is None:
            raise CatalogUnavailable("catalogd is not supervised")
        if lane == "repair":
            try:
                decoded = await workers.read_media(media_object_relative_path(canonical_hash).as_posix(), canonical_hash)
            except (RawObjectWorkerError, MediaObjectCorruptError):
                decoded = None
            if decoded is not None and decoded.data == data:
                replay = await catalogd.call(
                    "storage.media.commit.v2",
                    {
                        "media_hash": canonical_hash,
                        "state": "present",
                        "mime_type": mime_type,
                        "byte_size": len(data),
                        "object_path": media_object_relative_path(canonical_hash).as_posix(),
                        "session_refs": [],
                        "observed_at": datetime.now(UTC).isoformat(),
                    },
                    timeout_seconds=_STORAGE_COMMIT_CATALOG_TIMEOUT_SECONDS,
                )
                return {
                    "v": 2,
                    "sha256": canonical_hash,
                    "mime_type": replay["media"]["mime_type"] or mime_type,
                    "byte_size": len(data),
                    "created": False,
                    "commit_seq": replay.get("commit_seq"),
                }
        await _admit_historical_storage(admitted_bytes=len(data), path="storage_v2_media", lane=lane)
        async with workers.admission(lane):
            sealed = await workers.seal_media(
                MediaObjectSpec(media_hash=canonical_hash, mime_type=mime_type, data=data),
                lane=lane,
            )
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
            timeout_seconds=_STORAGE_COMMIT_CATALOG_TIMEOUT_SECONDS,
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


async def _storage_v2_media_manifest(media_hash: str, *, owner_id: int) -> tuple[str, dict[str, object]]:
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
            {"media_hash": canonical_hash, "session_id": None, "owner_id": str(owner_id), "limit": 1},
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
    auth: DeviceToken | object | None = Depends(verify_agents_caller),
    _single: None = Depends(require_single_tenant),
) -> Response:
    canonical_hash, media = await _storage_v2_media_manifest(media_hash, owner_id=auth.owner_id)
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
    auth: DeviceToken | object | None = Depends(verify_agents_caller),
    _single: None = Depends(require_single_tenant),
) -> Response:
    canonical_hash, media = await _storage_v2_media_manifest(media_hash, owner_id=auth.owner_id)
    return Response(
        status_code=status.HTTP_200_OK,
        media_type=str(media["mime_type"]),
        headers={"Content-Length": str(media["byte_size"]), "X-Media-Sha256": canonical_hash},
    )


@router.get("/capabilities")
async def storage_v2_capabilities(
    request: Request,
    auth_token: DeviceToken | object | None = Depends(verify_agents_caller),
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
    auth_token: DeviceToken | object | None = Depends(verify_agents_caller),
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
    # Both branches below answer the client identically on purpose: `machine_id`
    # is bound to the authenticated device token, so a distinguishable "wrong
    # machine" reply would tell a caller that an epoch exists on someone else's
    # machine. The distinction is real and worth having, so it goes to the
    # operator's logs instead of the wire — without it, a shipper blocked on a
    # machine rename is indistinguishable from one blocked on a genuinely
    # missing epoch, which is what made the 2026-08-04 incident unprovable.
    if result.get("found") is not True or not isinstance(epoch, dict):
        logger.info(
            "storage-v2 manifest miss: no epoch row source_epoch=%s machine_id=%s",
            source_epoch,
            machine_id,
        )
        raise _http_error(status.HTTP_404_NOT_FOUND, "source_epoch_not_found", "Source epoch was not found.")
    if epoch.get("machine_id") != machine_id:
        logger.warning(
            "storage-v2 manifest miss: epoch belongs to another machine source_epoch=%s requested_by=%s owned_by=%s",
            source_epoch,
            machine_id,
            epoch.get("machine_id"),
        )
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
        # Bound body decode separately from catalog work. A slow catalog commit
        # must not retain scarce live admission and make unrelated live tips
        # look like storage-worker saturation.
        async with raw_workers.admission(lane):
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
            if parsed["provider_facts"]:
                # The bytes are already durable; facts a pre-facts engine never
                # shipped (or a backfill re-sends) still need their rows, and
                # a provider title among them still names the session.
                await catalogd.call(
                    "session.provider_facts.insert.v2",
                    {
                        "session_id": str(spec.session_id),
                        "source_epoch": str(spec.source_epoch),
                        "provider_facts": parsed["provider_facts"],
                    },
                    timeout_seconds=_STORAGE_COMMIT_CATALOG_TIMEOUT_SECONDS,
                )
                replayed_title = _first_provider_title(parsed["provider_facts"])
                if replayed_title is not None:
                    await _apply_provider_title(catalogd, spec.session_id, replayed_title)
            return _validated_receipt(objects[0]["receipt"])

        owner_value = getattr(auth_token, "owner_id", None)
        render_spec = parsed["render_spec"]
        if render_spec is not None:
            parsed["render_spec"] = await enrich_render_interaction_kinds(
                catalog=catalogd,
                raw_workers=raw_workers,
                session_id=str(spec.session_id),
                owner_id=str(owner_value) if owner_value is not None else None,
                raw_spec=spec,
                render_spec=render_spec,
                manifest_cache={},
            )

        # Admission guards the shared archive filesystem every tenant writes
        # to, so it runs for both lanes: a client that labels its own traffic
        # `live` must not be able to keep storing bytes past the disk floor.
        await _admit_historical_storage(
            admitted_bytes=sum(len(record.data) for record in spec.records),
            path="storage_v2",
            lane=lane,
        )

        async with raw_workers.admission(lane):
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
                # A failed render seal must fail the whole commit. Committing
                # the raw object alone would hand the engine a durable receipt
                # for events that reach no timeline, detail, search or
                # embedding read — renders are per-batch deltas, no later
                # envelope backfills them, and a re-send short-circuits on
                # dedup. Failing here keeps the raw bytes uncommitted so the
                # engine retries the same envelope.
                sealed_render = await render_task
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
                # Claude local-command evidence can arrive in a later raw
                # envelope. Keep its catalog projection explicitly pending so
                # timeline/search/embedding projectors replay the complete raw
                # stream before treating the first user row as durable.
                "semantic_projection_version": (0 if render_spec.provider.strip().lower() == "claude" else SEMANTIC_PROJECTION_VERSION),
            }
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
                # Register every projector that can do work for this provider.
                # Search and dense embeddings consume every render; semantic-v2
                # is Claude-only sequence repair and must not acquire a global
                # no-op backlog that delays real Claude debt.
                "projectors": list(storage_projectors_for_provider(spec.provider)) if render_manifest is not None else ["render-v2"],
                "render_manifest": render_manifest,
                "session_facts": parsed["session_facts"],
                "conversation_resets": _conversation_resets(render_spec),
                "provider_facts": parsed["provider_facts"],
                "sealed_at": datetime.now(UTC).isoformat(),
            },
            timeout_seconds=_STORAGE_COMMIT_CATALOG_TIMEOUT_SECONDS,
        )
        provider_title = _first_provider_title(parsed["provider_facts"])
        if provider_title is not None:
            # The provider named this session. Whether that freezes an empty
            # anchor, promotes a Longhouse LLM title that won the race, or is
            # a no-op against an earlier provider name is the store's call.
            await _apply_provider_title(catalogd, spec.session_id, provider_title)
        elif (
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
                    "canonical_title_eligible": True,
                }
            )
        if committed.get("created") is True and render_manifest is not None:
            from zerg.services.session_input_links import link_ingested_user_inputs

            # Provenance for sends: the user events in this batch are the
            # only chance to say which Longhouse receipt each one became.
            await link_ingested_user_inputs(catalogd, spec.session_id, list(render_spec.records))
        # Wake clients whenever the commit changed what the read paths serve,
        # not when a message counter moved. An envelope of only system rows or
        # only excluded local-control rows still advances the session, and a
        # raw-only envelope still moves its session facts; gating on
        # user/assistant/tool counts left those changes invisible until some
        # unrelated commit happened to wake the page. An exact replay changes
        # nothing, so it stays silent.
        if committed.get("created") is True:
            from zerg.services.session_pubsub import TOPIC_TIMELINE
            from zerg.services.session_pubsub import get_pubsub
            from zerg.services.session_pubsub import topic_session

            payload = {
                "session_id": str(spec.session_id),
                "kind": "ingest",
                "events_inserted": int(render_manifest["event_count"] or 0) if render_manifest is not None else 0,
                "provider": spec.provider,
                "source": "storage_v2",
                "server_fanout_at_ms": int(datetime.now(UTC).timestamp() * 1000),
            }
            bus = get_pubsub()
            # Storage-v2 is the durable archive path for managed providers. It
            # must wake the focused workspace as well as the global timeline;
            # otherwise a detail page attached to an empty shell never
            # refetches the render events that just became durable.
            bus.publish(topic_session(str(spec.session_id)), payload)
            bus.publish(TOPIC_TIMELINE, payload)
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
            headers={
                "X-Longhouse-Storage-Backpressure": "storage_lane_busy",
                "X-Longhouse-Storage-Lane": lane,
                "Retry-After": "5",
            },
        ) from exc
    except RenderObjectWorkerBusy as exc:
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "storage_lane_busy",
            "Storage-v2 render lane is full; retry the same envelope.",
            headers={
                "X-Longhouse-Storage-Backpressure": "storage_lane_busy",
                "X-Longhouse-Storage-Lane": lane,
                "Retry-After": "5",
            },
        ) from exc
    except RawObjectValidationError as exc:
        raise _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_envelope", str(exc)) from exc
    except StorageV2SemanticRecoveryPermanentError as exc:
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "semantic_recovery_permanent",
            "Provider interaction evidence exceeds the safe replay bound; manual repair is required.",
        ) from exc
    except StorageV2SemanticRecoveryError as exc:
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "semantic_recovery_pending",
            "Provider interaction semantics are pending immutable raw history; retry the envelope.",
            headers={"Retry-After": "60"},
        ) from exc
    except RawObjectWorkerError as exc:
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "storage_worker_unavailable",
            "Storage-v2 worker failed; retry the same envelope.",
        ) from exc
    except RenderObjectValidationError as exc:
        # The envelope itself is unacceptable -- the 4 MiB render bound is
        # reachable in normal operation, because the engine ships 4 MiB raw
        # batches and the render re-serialization is larger. Retrying identical
        # bytes can never succeed, so answer 422 like the raw-side bound above:
        # the engine quarantines a 422 visibly and `longhouse shipping discard`
        # clears it, where a 503 would spin forever.
        raise _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_envelope", str(exc)) from exc
    except RenderObjectWorkerError as exc:
        # The worker failed, not the envelope. Retrying the same bytes is the
        # right move -- but never commit an envelope whose events nothing will
        # ever serve.
        logger.warning(
            "Storage-v2 render seal failed; envelope rejected",
            extra={"lane": lane, "error": str(exc)},
        )
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "storage_worker_unavailable",
            "Storage-v2 render worker failed; retry the same envelope.",
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


def _claude_abandoned_event_ids(
    records: list[tuple[tuple[int, str, str, str, str, int, int], RenderRecord]],
) -> set[str]:
    siblings: dict[str, list[tuple[tuple[int, str, str, str, str, int, int], RenderRecord]]] = {}
    for key, record in records:
        if record.role == "user" and record.parent_uuid:
            siblings.setdefault(record.parent_uuid, []).append((key, record))

    abandoned: set[str] = set()
    for group in siblings.values():
        group.sort(key=lambda item: (item[1].source_position, item[1].event_subordinal, item[0]))
        abandoned.update(record.event_id for _, record in group[:-1])
    return abandoned


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


def _render_event_wire(
    session_id: UUID,
    generation_id: UUID,
    decoded,
    record: RenderRecord,
    *,
    interaction_kind: str | None = None,
) -> dict[str, object]:
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
    effective_interaction_kind = interaction_kind or record.interaction_kind
    role = record.role
    content_text = record.content_text
    if effective_interaction_kind == INTERACTION_PROVIDER_NOTIFICATION:
        role = "system"
        content_text = claude_task_notification_summary(content_text) or content_text
    return {
        "event_id": record.event_id,
        "cursor": render_detail_cursor_token(cursor),
        "timestamp": timestamp,
        "role": role,
        "content_text": content_text,
        "interaction_kind": effective_interaction_kind,
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
    _auth: DeviceToken | object | None = Depends(verify_agents_caller),
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
    owner_value = owner_id_from_caller(_auth)
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
    response: Response,
    cursor: str | None = Query(None, description="Exclusive source-ordered raw-object cursor"),
    _auth: DeviceToken | object | None = Depends(verify_agents_caller),
    _single: None = Depends(require_single_tenant),
) -> dict[str, object]:
    timing = ServerTimingRecorder(surface="raw_export")
    owner_value = owner_id_from_caller(_auth)
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
        with timing.span("raw_manifest"):
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
        from zerg.metrics import product_read_bytes
        from zerg.metrics import product_read_objects

        product_read_objects.labels("raw_export", "raw").observe(0)
        product_read_bytes.labels("raw_export", "raw").observe(0)
        result = {
            "v": 2,
            "session_id": str(session_id),
            "object": None,
            "records": [],
            "next_cursor": None,
            "has_more": False,
        }
        timing.apply(response)
        return result
    item = objects[0]
    if not isinstance(item, dict):
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "raw_manifest_invalid",
            "The catalog returned an invalid raw-object row.",
        )
    workers = get_raw_object_worker_pool()
    try:
        with timing.span("raw_object_read"):
            decoded = await workers.read(str(item["object_path"]), str(item["object_hash"]), str(item["tenant_id"]))
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
    from zerg.metrics import product_read_bytes
    from zerg.metrics import product_read_objects

    product_read_objects.labels("raw_export", "raw").observe(1)
    product_read_bytes.labels("raw_export", "raw").observe(int(item.get("compressed_size") or 0))
    result = {
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
    timing.apply(response)
    return result


def _session_detail_read_lock(session_id: UUID) -> asyncio.Lock:
    """Return one process-local user-read lane for a session.

    Creating the lock contains no await, so lookup/install is atomic on the
    Runtime Host event loop.  Weak storage drops idle session identities while
    active and waiting tasks retain the lock they use.
    """

    key = str(session_id)
    lock = _SESSION_DETAIL_READ_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _SESSION_DETAIL_READ_LOCKS[key] = lock
    return lock


async def read_storage_v2_session_events_page(
    *,
    session_id: UUID,
    owner_id: str,
    cursor: str | None,
    anchor: str,
    limit: int,
    branch_mode: str = "all",
    timing: ServerTimingRecorder | None = None,
) -> dict[str, object]:
    """Serialize immutable object reads for one session within this host."""

    timing = timing or ServerTimingRecorder()
    lock = _session_detail_read_lock(session_id)
    admission_started_at = monotonic()
    await lock.acquire()
    timing.record("read_admission", (monotonic() - admission_started_at) * 1000.0)
    try:
        return await _read_storage_v2_session_events_page_admitted(
            session_id=session_id,
            owner_id=owner_id,
            cursor=cursor,
            anchor=anchor,
            limit=limit,
            branch_mode=branch_mode,
            timing=timing,
        )
    finally:
        lock.release()


async def _read_storage_v2_session_events_page_admitted(
    *,
    session_id: UUID,
    owner_id: str,
    cursor: str | None,
    anchor: str,
    limit: int,
    branch_mode: str = "all",
    timing: ServerTimingRecorder,
) -> dict[str, object]:
    """Read one verified render page for a known owner.

    Browser and machine routes share this physical read so the canonical
    product surfaces cannot drift back toward the cold monolith.
    """

    if anchor not in {"start", "tail"}:
        raise _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_anchor", "anchor must be start or tail")
    if branch_mode not in {"head", "all"}:
        raise _http_error(status.HTTP_400_BAD_REQUEST, "invalid_branch_mode", "branch_mode must be one of: head, all")
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
    retain_product_metrics = timing is not None and timing.product_surface is not None
    try:
        with timing.span("render_manifest"):
            manifest = await catalogd.call(
                "storage.session.render_manifest.v2",
                {
                    "session_id": str(session_id),
                    "owner_id": owner_id,
                    "generation_id": str(decoded_cursor.render_generation) if decoded_cursor is not None else None,
                    "anchor": anchor,
                    "after_order_key": cursor_order_key if anchor == "start" else None,
                    "before_order_key": cursor_order_key if anchor == "tail" else None,
                    "limit": _RENDER_MANIFEST_LIMIT,
                },
                timeout_seconds=_SESSION_DETAIL_CATALOG_TIMEOUT_SECONDS,
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
    raw_workers = get_raw_object_worker_pool()
    raw_manifest_cache: dict[str, dict[str, dict[str, object]]] = {}
    sequence_context_cache: dict[tuple[str, ...], dict[str, object]] = {}
    ordered_events: list[tuple[tuple[int, str, str, str, str, int, int], dict[str, object]]] = []
    claude_generation = False
    claude_branch_records: list[tuple[tuple[int, str, str, str, str, int, int], RenderRecord]] = []
    claude_semantic_event_ids: set[str] = set()
    next_object_index = 0
    cursor_key = _cursor_order_key(decoded_cursor) if decoded_cursor is not None else None
    object_read_duration_ms = 0.0
    semantic_recovery_duration_ms = 0.0
    semantic_recovery_stats = SemanticRecoveryStats()
    try:
        while next_object_index < len(objects):
            batch_manifests = objects[next_object_index : next_object_index + _RENDER_READ_BATCH]
            if any(not isinstance(item, dict) for item in batch_manifests):
                raise ValueError("render object manifest is invalid")
            object_read_started = monotonic()
            try:
                decoded_batch = await asyncio.gather(
                    *(
                        workers.read(
                            str(item["object_path"]),
                            str(item["object_hash"]),
                            lane="user",
                            queue_timeout_seconds=_SESSION_DETAIL_WORKER_QUEUE_TIMEOUT_SECONDS,
                        )
                        for item in batch_manifests
                    )
                )
            finally:
                object_read_duration_ms += (monotonic() - object_read_started) * 1000.0
            for item, decoded in zip(batch_manifests, decoded_batch, strict=True):
                spec = decoded.spec
                claude_generation = claude_generation or spec.provider.strip().lower() == "claude"
                if (
                    spec.session_id != session_id
                    or spec.render_generation != generation_id
                    or spec.source_envelope_id != item.get("source_envelope_id")
                    or decoded.object_hash != item.get("object_hash")
                ):
                    raise ValueError("render object does not match its catalog manifest")
                semantic_recovery_started_at = monotonic()
                try:
                    recovered_kinds = await recover_render_interaction_kinds(
                        catalog=catalogd,
                        raw_workers=raw_workers,
                        session_id=str(session_id),
                        owner_id=owner_id,
                        provider=spec.provider,
                        records=spec.records,
                        source_envelope_id=spec.source_envelope_id,
                        manifest_cache=raw_manifest_cache,
                        sequence_context_cache=sequence_context_cache,
                        reclassify_sequence_controls=spec.provider.strip().lower() == "claude",
                        stats=semantic_recovery_stats,
                    )
                finally:
                    semantic_recovery_duration_ms += (monotonic() - semantic_recovery_started_at) * 1000.0
                for ordinal, record in enumerate(spec.records):
                    key = _render_record_order_key(decoded, record)
                    if claude_generation:
                        claude_branch_records.append((key, record))
                    recovered_kind = recovered_kinds.get(ordinal)
                    # A legacy render object may have persisted the ambiguous
                    # command as durable before a later Claude caveat arrived.
                    # Raw replay is authoritative for the current projection;
                    # only fall back to the stored fact when replay has no
                    # answer because evidence is unavailable.
                    interaction_kind = recovered_kind if recovered_kind is not None else getattr(record, "interaction_kind", None)
                    is_provider_notification = interaction_kind == INTERACTION_PROVIDER_NOTIFICATION
                    if not is_provider_notification and not semantic_event_included(
                        spec.provider,
                        role=record.role,
                        content_text=record.content_text,
                        interaction_kind=interaction_kind,
                    ):
                        continue
                    if claude_generation:
                        claude_semantic_event_ids.add(record.event_id)
                    key = _render_record_order_key(decoded, record)
                    if (anchor == "start" and (cursor_key is None or key > cursor_key)) or (
                        anchor == "tail" and (cursor_key is None or key < cursor_key)
                    ):
                        try:
                            wire = _render_event_wire(
                                session_id,
                                generation_id,
                                decoded,
                                record,
                                interaction_kind=interaction_kind,
                            )
                        except ValueError:
                            # One unserializable row must not cost the reader
                            # the whole page. Sealing now rejects timestamps
                            # the read cannot express, so only pre-bound
                            # objects can land here and a parser-revision
                            # re-mint repairs them from durable raw.
                            logger.warning(
                                "storage-v2 render event skipped: unrepresentable timestamp "
                                "session_id=%s generation_id=%s event_id=%s order_time_us=%s",
                                session_id,
                                generation_id,
                                record.event_id,
                                record.order_time_us,
                            )
                            continue
                        ordered_events.append((key, wire))
            next_object_index += len(batch_manifests)
            ordered_events.sort(key=lambda item: item[0])
            if claude_generation and anchor == "tail":
                visible_events = ordered_events
                if branch_mode == "head":
                    seen_abandoned_ids = _claude_abandoned_event_ids(claude_branch_records)
                    visible_events = [(key, wire) for key, wire in ordered_events if wire.get("event_id") not in seen_abandoned_ids]
                if len(visible_events) >= limit:
                    cutoff = visible_events[-limit][0]
                    if next_object_index >= len(objects) or _manifest_last_key(objects[next_object_index]) < cutoff:
                        break
            elif not claude_generation and len(ordered_events) > limit:
                cutoff = ordered_events[limit][0] if anchor == "start" else ordered_events[-limit - 1][0]
                if (
                    next_object_index >= len(objects)
                    or (anchor == "start" and _manifest_first_key(objects[next_object_index]) > cutoff)
                    or (anchor == "tail" and _manifest_last_key(objects[next_object_index]) < cutoff)
                ):
                    break
    except RenderObjectWorkerBusy as exc:
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "render_read_busy",
            "Session history is temporarily busy; retry shortly.",
            headers={"Retry-After": "1"},
        ) from exc
    except StorageV2SemanticRecoveryPermanentError as exc:
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "semantic_recovery_permanent",
            "Provider interaction evidence exceeds the safe replay bound; manual repair is required.",
        ) from exc
    except StorageV2SemanticRecoveryError as exc:
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "semantic_recovery_pending",
            "Provider interaction semantics are pending immutable raw history; retry shortly.",
            headers={"Retry-After": "60"},
        ) from exc
    except (
        KeyError,
        TypeError,
        ValueError,
        RenderObjectCorruptError,
        RenderObjectWorkerError,
    ) as exc:
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "render_read_failed",
            "The immutable render generation could not be verified.",
        ) from exc
    finally:
        timing.record("render_object_read", object_read_duration_ms)
        timing.record("semantic_recover", semantic_recovery_duration_ms)
        if semantic_recovery_stats.raw_manifest_duration_ms > 0:
            timing.record("raw_manifest_read", semantic_recovery_stats.raw_manifest_duration_ms)
        if semantic_recovery_stats.raw_companion_duration_ms > 0:
            timing.record("raw_companion_read", semantic_recovery_stats.raw_companion_duration_ms)
        if semantic_recovery_stats.sequence_context_duration_ms > 0:
            timing.record("sequence_context_seed", semantic_recovery_stats.sequence_context_duration_ms)

    abandoned_ids = _claude_abandoned_event_ids(claude_branch_records) if claude_generation else set()
    abandoned_events = len(abandoned_ids & claude_semantic_event_ids)
    for _, wire in ordered_events:
        if wire.get("event_id") in abandoned_ids:
            wire["branch_kind"] = "abandoned"
    if branch_mode == "head":
        ordered_events = [(key, wire) for key, wire in ordered_events if wire.get("event_id") not in abandoned_ids]

    page = ordered_events[:limit] if anchor == "start" else ordered_events[-limit:]
    has_more = len(ordered_events) > limit or next_object_index < len(objects) or manifest.get("objects_truncated") is True
    logger.info(
        "storage-v2 session detail read session_id=%s generation_id=%s anchor=%s branch_mode=%s "
        "objects_read=%s events_kept=%s semantic_candidates=%s raw_companions=%s has_more=%s",
        session_id,
        generation_id,
        anchor,
        branch_mode,
        next_object_index,
        len(page),
        semantic_recovery_stats.selected_records,
        semantic_recovery_stats.raw_companions_read,
        has_more,
    )
    if retain_product_metrics:
        from zerg.metrics import product_read_bytes
        from zerg.metrics import product_read_objects

        read_objects = objects[:next_object_index]
        compressed_bytes = sum(int(item.get("compressed_size") or 0) for item in read_objects if isinstance(item, dict))
        product_read_objects.labels("session_detail", "render").observe(next_object_index)
        product_read_bytes.labels("session_detail", "render").observe(compressed_bytes)
    return {
        "v": 2,
        "session_id": str(session_id),
        "generation_id": str(generation_id),
        "events": [event for _, event in page],
        "next_cursor": (page[-1][1]["cursor"] if anchor == "start" else page[0][1]["cursor"]) if page and has_more else None,
        "has_more": has_more,
        "total": total,
        "abandoned_events": abandoned_events,
    }


@router.get("/sessions/{session_id}/events")
async def read_storage_v2_session_events(
    session_id: UUID,
    response: Response,
    cursor: str | None = Query(None, description="Exclusive generation-qualified render cursor"),
    anchor: str = Query("start", description="Page from the beginning or latest tail: start|tail"),
    limit: int = Query(100, ge=1, le=500),
    _auth: DeviceToken | object | None = Depends(verify_agents_caller),
    _single: None = Depends(require_single_tenant),
) -> dict[str, object]:
    owner_value = owner_id_from_caller(_auth)
    timing = ServerTimingRecorder(surface="session_detail")
    result = await read_storage_v2_session_events_page(
        session_id=session_id,
        owner_id=str(owner_value),
        cursor=cursor,
        anchor=anchor,
        limit=limit,
        timing=timing,
    )
    timing.apply(response)
    return result


@router.post("/envelopes")
async def commit_storage_v2_envelope(
    request: Request,
    auth_token: DeviceToken | object | None = Depends(verify_agents_caller),
    _single: None = Depends(require_single_tenant),
) -> dict[str, object]:
    lane = request.headers.get("X-Longhouse-Storage-Lane", "").strip().lower()
    if lane not in {"live", "repair"}:
        raise _http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_lane",
            "X-Longhouse-Storage-Lane must be live or repair.",
        )
    return await _commit_admitted_envelope(
        request,
        auth_token,
        lane=lane,
        raw_workers=get_raw_object_worker_pool(),
        render_workers=get_render_object_worker_pool(),
    )


__all__ = ["MAX_WIRE_BODY_BYTES", "commit_storage_v2_envelope", "router", "storage_v2_capabilities"]
