"""Parse and verify one storage-v2 envelope into the specs the Runtime Host seals.

A pure function of the decoded request body and the authenticated identity:
no I/O, no catalog. It runs in the raw object worker processes, not on
serve's event loop (14 ms of interpreter time per 1.7 MB envelope), so this
module imports only what parsing needs; the API router pulls in FastAPI and
the catalog client, which would cost each worker 70 MB and 1.5 s to start.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from typing import Any
from uuid import UUID

from zerg.services.claude_channel_text import strip_claude_channel_wrapper
from zerg.services.provider_interaction_semantics import INTERACTION_PROVIDER_NOTIFICATION
from zerg.services.provider_interaction_semantics import INTERACTION_PROVIDER_REASONING
from zerg.services.provider_interaction_semantics import INTERACTION_PROVIDER_SYSTEM
from zerg.services.provider_interaction_semantics import classify_provider_interaction
from zerg.services.provider_interaction_semantics import claude_task_notification_display
from zerg.services.provider_interaction_semantics import seed_provider_interaction_sequence_context
from zerg.services.provider_interaction_semantics import semantic_projection_facts
from zerg.storage_v2.contracts import EnvelopeIdentity
from zerg.storage_v2.contracts import envelope_id
from zerg.storage_v2.contracts import hash_records
from zerg.storage_v2.fields import canonical_text
from zerg.storage_v2.fields import parse_session_facts
from zerg.storage_v2.fields import utc_timestamp
from zerg.storage_v2.raw_objects import MAX_RECORD_BYTES
from zerg.storage_v2.raw_objects import MAX_RECORDS
from zerg.storage_v2.raw_objects import RawObjectSpec
from zerg.storage_v2.raw_objects import RawRecord
from zerg.storage_v2.raw_objects import validate_raw_object_spec
from zerg.storage_v2.render_objects import RenderObjectSpec
from zerg.storage_v2.render_objects import RenderObjectValidationError
from zerg.storage_v2.render_objects import RenderRecord
from zerg.storage_v2.render_objects import validate_render_object_spec
from zerg.utils import fast_json

logger = logging.getLogger(__name__)


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


# A record carries its exact source bytes either as a UTF-8 string (`data`)
# or, for bytes that are not valid UTF-8, as base64 (`data_b64`). Transcripts
# are JSON lines, so nearly every record is text; base64 inside the zstd
# envelope halved its compression (5.2x to 2.6x on captured engine envelopes).
_RECORD_FIELDS_BASE64 = frozenset({"source_position", "data_b64"})


_RECORD_FIELDS_TEXT = frozenset({"source_position", "data"})


_EXPECTED_MEDIA_REF_FIELDS = {"sha256", "source_position", "ref_key", "availability"}


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


# Fields a parser sends only when it has something to assert.
_OPTIONAL_RENDER_RECORD_FIELDS = frozenset({"interaction_kind", "parent_uuid", "item"})


_MAX_MEDIA_REFS = 1_000


def canonical_uuid(value: object, field: str) -> UUID:
    try:
        parsed = UUID(value) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if parsed is None or str(parsed) != value:
        raise ValueError(f"{field} must be a canonical UUID")
    return parsed


def lower_hash(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{field} must be lowercase SHA-256 hex")
    return value


def _parse_media_refs(value: object, *, range_start: int, range_end: int) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) > _MAX_MEDIA_REFS:
        raise ValueError(f"media must contain at most {_MAX_MEDIA_REFS} references")
    refs: list[dict[str, object]] = []
    seen: set[tuple[str, int, str]] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != _EXPECTED_MEDIA_REF_FIELDS:
            raise ValueError("each media reference has invalid fields")
        media_hash = lower_hash(item["sha256"], "media.sha256")
        position = item["source_position"]
        if type(position) is not int or not range_start <= position < range_end:
            raise ValueError("media.source_position is outside the raw envelope")
        ref_key = canonical_text(item["ref_key"], "media.ref_key", 255)
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


def parse_render_spec(
    value: object,
    *,
    raw_spec: RawObjectSpec,
    source_envelope_id: str,
) -> RenderObjectSpec | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _EXPECTED_RENDER_FIELDS:
        raise ValueError("render fields do not match protocol v2")
    generation_id = canonical_uuid(value["generation_id"], "render.generation_id")
    parser_revision = canonical_text(value["parser_revision"], "render.parser_revision", 128)
    ordering_revision = canonical_text(value["ordering_revision"], "render.ordering_revision", 128)
    wire_records = value["records"]
    if not isinstance(wire_records, list) or len(wire_records) > MAX_RECORDS:
        raise ValueError(f"render.records must contain at most {MAX_RECORDS} items")
    record_payloads: list[dict[str, Any]] = []
    records_by_raw_ordinal: dict[int, list[int]] = {}
    for item in wire_records:
        if (
            not isinstance(item, dict)
            or not _LEGACY_RENDER_RECORD_FIELDS <= set(item) <= _LEGACY_RENDER_RECORD_FIELDS | _OPTIONAL_RENDER_RECORD_FIELDS
        ):
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
            raw_value = fast_json.loads(raw_record.data.decode("utf-8"))
            raw_values.append(raw_value if isinstance(raw_value, dict) else None)
        except (UnicodeDecodeError, json.JSONDecodeError):
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
                elif (
                    str(raw_spec.provider or "").strip().lower() == "claude"
                    and record_payload["role"] == "user"
                    and isinstance(record_payload["content_text"], str)
                ):
                    # Channel framing is execution metadata, not authored
                    # transcript text. Keep the raw provider envelope intact;
                    # serve and correlate only its user-authored body.
                    record_payload["content_text"] = strip_claude_channel_wrapper(record_payload["content_text"])
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
                # Native Claude and Codex envelopes remain authoritative inside
                # semantic_projection_facts. For providers without a raw
                # semantic contract, preserve the parser's explicit fact while
                # still allowing the shared classifier to validate the shape.
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
                # The parser asserts reasoning and turn ends from the raw
                # record it parsed. A turn end can share its raw line with a
                # thinking part (Pi, OMP), which the shared classifier would
                # read as reasoning.
                item_kind = (record_payload.get("item") or {}).get("kind")
                if item_kind == "reasoning":
                    record_payload["interaction_kind"] = INTERACTION_PROVIDER_REASONING
                elif item_kind == "turn_end":
                    record_payload["interaction_kind"] = INTERACTION_PROVIDER_SYSTEM
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


def _raw_record_role(raw_value: object) -> str | None:
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


def parse_envelope(
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
    provider = canonical_text(payload["provider"], "provider", 32)
    opaque_source_id = canonical_text(payload["opaque_source_id"], "opaque_source_id", 4_096)
    session_id = canonical_uuid(payload["session_id"], "session_id")
    source_epoch = canonical_uuid(payload["source_epoch"], "source_epoch")
    predecessor_value = payload["predecessor_source_epoch"]
    predecessor = canonical_uuid(predecessor_value, "predecessor_source_epoch") if predecessor_value is not None else None
    opened_at = utc_timestamp(payload["epoch_opened_at"], "epoch_opened_at")
    range_kind = payload["range_kind"]
    if range_kind not in {"byte_offset", "record_ordinal"}:
        raise ValueError("range_kind must be byte_offset or record_ordinal")
    range_start = payload["range_start"]
    range_end = payload["range_end"]
    if type(range_start) is not int or type(range_end) is not int:
        raise ValueError("source range must use integers")
    expected_envelope = lower_hash(payload["expected_envelope_id"], "expected_envelope_id")
    session_facts = parse_session_facts(payload["session"])
    media_refs = _parse_media_refs(payload["media"], range_start=range_start, range_end=range_end)

    wire_records = payload["records"]
    if not isinstance(wire_records, list) or len(wire_records) > MAX_RECORDS:
        raise ValueError(f"records must contain at most {MAX_RECORDS} items")
    records: list[RawRecord] = []
    raw_bytes = 0
    for item in wire_records:
        fields = set(item) if isinstance(item, dict) else None
        if fields != _RECORD_FIELDS_TEXT and fields != _RECORD_FIELDS_BASE64:
            raise ValueError("each record must contain source_position and one of data or data_b64")
        position = item["source_position"]
        if type(position) is not int or not 0 <= position < 1 << 64:
            raise ValueError("record source_position must be an unsigned 64-bit integer")
        if fields == _RECORD_FIELDS_TEXT:
            text = item["data"]
            if not isinstance(text, str):
                raise ValueError("record data must be a string")
            try:
                # A JSON string can spell a lone surrogate, which no UTF-8 byte
                # sequence encodes; such a record could not be the source bytes.
                data = text.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError("record data is not valid UTF-8 text") from exc
        else:
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
    render_spec = parse_render_spec(payload["render"], raw_spec=spec, source_envelope_id=expected_envelope)
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
            at = utc_timestamp(item["at"], "fact at")
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
