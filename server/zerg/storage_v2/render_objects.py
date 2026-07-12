"""Deterministic immutable render objects for storage-v2 session detail."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import zstandard

FORMAT_VERSION = 2
MAX_RENDER_BYTES = 4 * 1024 * 1024
MAX_RENDER_EVENTS = 10_000
MAX_RENDER_COMPRESSED_BYTES = 8 * 1024 * 1024
_ROLES = {"user", "assistant", "tool", "system"}


class RenderObjectError(RuntimeError):
    pass


class RenderObjectValidationError(RenderObjectError):
    pass


class RenderObjectCorruptError(RenderObjectError):
    pass


@dataclass(frozen=True, slots=True)
class RenderRecord:
    event_id: str
    order_time_us: int
    source_position: int
    event_subordinal: int
    role: str
    content_text: str | None = None
    tool_name: str | None = None
    tool_input_json: object | None = None
    tool_output_text: str | None = None
    tool_call_id: str | None = None
    thread_id: str | None = None
    branch_kind: str | None = None
    raw_record_ordinal: int = 0


@dataclass(frozen=True, slots=True)
class RenderObjectSpec:
    session_id: UUID
    render_generation: UUID
    parser_revision: str
    ordering_revision: str
    machine_id: str
    provider: str
    opaque_source_id: str
    source_epoch: UUID
    source_envelope_id: str
    records: tuple[RenderRecord, ...]


@dataclass(frozen=True, slots=True)
class SealedRenderObject:
    object_id: str
    object_hash: str
    payload_hash: str
    object_path: str
    uncompressed_size: int
    compressed_size: int
    event_count: int
    first_order_key: str | None
    last_order_key: str | None
    user_messages: int
    assistant_messages: int
    tool_calls: int
    first_user_message_preview: str | None
    last_visible_text_preview: str | None
    reused: bool


@dataclass(frozen=True, slots=True)
class DecodedRenderObject:
    spec: RenderObjectSpec
    object_hash: str
    payload_hash: str


def seal_render_object(root: Path, spec: RenderObjectSpec) -> SealedRenderObject:
    payload = encode_render_object(spec)
    payload_hash = hashlib.sha256(payload).hexdigest()
    compressed = zstandard.ZstdCompressor(level=3, write_checksum=True, write_content_size=True).compress(payload)
    if len(compressed) > MAX_RENDER_COMPRESSED_BYTES:
        raise RenderObjectValidationError("compressed render object exceeds 8 MiB")
    object_hash = hashlib.sha256(compressed).hexdigest()
    relative_path = Path("render") / "v2" / object_hash[:2] / f"{object_hash}.zst"
    final_path = _safe_path(root, relative_path)
    final_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    if final_path.exists():
        existing = final_path.read_bytes()
        if existing != compressed or hashlib.sha256(existing).hexdigest() != object_hash:
            raise RenderObjectCorruptError(f"existing content-addressed render object is corrupt: {relative_path}")
        reused = True
    else:
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{object_hash}.tmp-",
                dir=final_path.parent,
                delete=False,
            ) as handle:
                temporary_name = handle.name
                os.chmod(temporary_name, 0o600)
                handle.write(compressed)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, final_path)
            temporary_name = None
            _fsync_directory(final_path.parent)
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)
        reused = False

    aggregate = _aggregate(spec)
    return SealedRenderObject(
        object_id=object_hash,
        object_hash=object_hash,
        payload_hash=payload_hash,
        object_path=relative_path.as_posix(),
        uncompressed_size=len(payload),
        compressed_size=len(compressed),
        event_count=len(spec.records),
        reused=reused,
        **aggregate,
    )


def encode_render_object(spec: RenderObjectSpec) -> bytes:
    _validate_spec(spec)
    payload = {
        "format_version": FORMAT_VERSION,
        "machine_id": spec.machine_id,
        "opaque_source_id": spec.opaque_source_id,
        "ordering_revision": spec.ordering_revision,
        "parser_revision": spec.parser_revision,
        "provider": spec.provider,
        "records": [asdict(record) for record in spec.records],
        "render_generation": str(spec.render_generation),
        "session_id": str(spec.session_id),
        "source_envelope_id": spec.source_envelope_id,
        "source_epoch": str(spec.source_epoch),
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(encoded) > MAX_RENDER_BYTES:
        raise RenderObjectValidationError("render object exceeds 4 MiB")
    return encoded


def validate_render_object_spec(spec: RenderObjectSpec) -> None:
    _validate_spec(spec)


def read_render_object(root: Path, object_path: str, *, expected_object_hash: str) -> DecodedRenderObject:
    if not _is_hash(expected_object_hash):
        raise RenderObjectValidationError("expected_object_hash must be lowercase SHA-256 hex")
    relative_path = Path(object_path)
    path = _safe_path(root, relative_path)
    if expected_object_hash not in relative_path.name:
        raise RenderObjectValidationError("render object path is not content-addressed")
    try:
        compressed = path.read_bytes()
    except OSError as exc:
        raise RenderObjectCorruptError(f"render object is unreadable: {relative_path}") from exc
    if len(compressed) > MAX_RENDER_COMPRESSED_BYTES or hashlib.sha256(compressed).hexdigest() != expected_object_hash:
        raise RenderObjectCorruptError("render object compressed hash mismatch")
    try:
        payload = zstandard.ZstdDecompressor().decompress(compressed, max_output_size=MAX_RENDER_BYTES)
    except zstandard.ZstdError as exc:
        raise RenderObjectCorruptError("render object zstd payload is corrupt") from exc
    spec = decode_render_object(payload)
    return DecodedRenderObject(
        spec=spec,
        object_hash=expected_object_hash,
        payload_hash=hashlib.sha256(payload).hexdigest(),
    )


def decode_render_object(payload: bytes) -> RenderObjectSpec:
    if len(payload) > MAX_RENDER_BYTES:
        raise RenderObjectCorruptError("render object exceeds 4 MiB")
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RenderObjectCorruptError("render object JSON is invalid") from exc
    expected = {
        "format_version",
        "machine_id",
        "opaque_source_id",
        "ordering_revision",
        "parser_revision",
        "provider",
        "records",
        "render_generation",
        "session_id",
        "source_envelope_id",
        "source_epoch",
    }
    if not isinstance(decoded, dict) or set(decoded) != expected or decoded["format_version"] != FORMAT_VERSION:
        raise RenderObjectCorruptError("render object shape/version is invalid")
    try:
        records = tuple(RenderRecord(**record) for record in decoded["records"])
        spec = RenderObjectSpec(
            session_id=UUID(decoded["session_id"]),
            render_generation=UUID(decoded["render_generation"]),
            parser_revision=decoded["parser_revision"],
            ordering_revision=decoded["ordering_revision"],
            machine_id=decoded["machine_id"],
            provider=decoded["provider"],
            opaque_source_id=decoded["opaque_source_id"],
            source_epoch=UUID(decoded["source_epoch"]),
            source_envelope_id=decoded["source_envelope_id"],
            records=records,
        )
        _validate_spec(spec)
    except (TypeError, ValueError, RenderObjectValidationError) as exc:
        raise RenderObjectCorruptError("render object facts violate the v2 contract") from exc
    return spec


def _validate_spec(spec: RenderObjectSpec) -> None:
    if len(spec.records) > MAX_RENDER_EVENTS:
        raise RenderObjectValidationError("render object exceeds 10000 events")
    if not spec.parser_revision or len(spec.parser_revision.encode()) > 128:
        raise RenderObjectValidationError("parser_revision must contain 1 to 128 UTF-8 bytes")
    if not spec.ordering_revision or len(spec.ordering_revision.encode()) > 128:
        raise RenderObjectValidationError("ordering_revision must contain 1 to 128 UTF-8 bytes")
    if not _is_hash(spec.source_envelope_id):
        raise RenderObjectValidationError("source_envelope_id must be lowercase SHA-256 hex")
    previous: tuple[int, str, str, str, str, int, int] | None = None
    for record in spec.records:
        if not record.event_id or len(record.event_id.encode()) > 255:
            raise RenderObjectValidationError("render event_id must contain 1 to 255 UTF-8 bytes")
        if not -(1 << 63) <= record.order_time_us < 1 << 63:
            raise RenderObjectValidationError("render order_time_us exceeds i64")
        if not 0 <= record.source_position < 1 << 64 or not 0 <= record.event_subordinal < 1 << 32:
            raise RenderObjectValidationError("render source position/subordinal is out of range")
        if record.role not in _ROLES or not 0 <= record.raw_record_ordinal < MAX_RENDER_EVENTS:
            raise RenderObjectValidationError("render role or raw locator is invalid")
        for field, maximum in (
            ("content_text", 2 * 1024 * 1024),
            ("tool_name", 255),
            ("tool_output_text", 2 * 1024 * 1024),
            ("tool_call_id", 255),
            ("thread_id", 255),
            ("branch_kind", 64),
        ):
            value = getattr(record, field)
            if value is not None and (not isinstance(value, str) or len(value.encode("utf-8")) > maximum):
                raise RenderObjectValidationError(f"render {field} is invalid or exceeds its bound")
        if record.tool_input_json is not None:
            try:
                tool_json = json.dumps(record.tool_input_json, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            except (TypeError, ValueError) as exc:
                raise RenderObjectValidationError("render tool_input_json is not JSON-serializable") from exc
            if len(tool_json.encode("utf-8")) > 2 * 1024 * 1024:
                raise RenderObjectValidationError("render tool_input_json exceeds its bound")
        key = _order_key_tuple(spec, record)
        if previous is not None and key <= previous:
            raise RenderObjectValidationError("render events must be strictly ordered")
        previous = key


def _order_key_tuple(spec: RenderObjectSpec, record: RenderRecord) -> tuple[int, str, str, str, str, int, int]:
    return (
        record.order_time_us,
        spec.machine_id,
        spec.provider,
        spec.opaque_source_id,
        str(spec.source_epoch),
        record.source_position,
        record.event_subordinal,
    )


def _order_key(spec: RenderObjectSpec, record: RenderRecord) -> str:
    values = _order_key_tuple(spec, record)
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def _aggregate(spec: RenderObjectSpec) -> dict[str, object]:
    records = spec.records
    first_user = next((record.content_text for record in records if record.role == "user" and record.content_text), None)
    last_visible = next(
        (record.content_text or record.tool_output_text for record in reversed(records) if record.content_text or record.tool_output_text),
        None,
    )
    return {
        "first_order_key": _order_key(spec, records[0]) if records else None,
        "last_order_key": _order_key(spec, records[-1]) if records else None,
        "user_messages": sum(record.role == "user" for record in records),
        "assistant_messages": sum(record.role == "assistant" and record.tool_name is None for record in records),
        "tool_calls": sum(record.tool_name is not None for record in records),
        "first_user_message_preview": _preview(first_user),
        "last_visible_text_preview": _preview(last_visible),
    }


def _preview(value: str | None) -> str | None:
    if value is None:
        return None
    return value[:500]


def _safe_path(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise RenderObjectValidationError("render object path must be safe and relative")
    resolved_root = root.expanduser().resolve()
    resolved = (resolved_root / relative).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise RenderObjectValidationError("render object path escapes storage root")
    return resolved


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _is_hash(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


__all__ = [
    "DecodedRenderObject",
    "RenderObjectCorruptError",
    "RenderObjectError",
    "RenderObjectSpec",
    "RenderObjectValidationError",
    "RenderRecord",
    "SealedRenderObject",
    "decode_render_object",
    "encode_render_object",
    "read_render_object",
    "seal_render_object",
    "validate_render_object_spec",
]
