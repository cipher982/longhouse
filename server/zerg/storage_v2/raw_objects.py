"""Deterministic immutable raw-object encoding and filesystem durability."""

from __future__ import annotations

import hashlib
import json
import os
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import zstandard

from zerg.storage_v2.contracts import EnvelopeIdentity
from zerg.storage_v2.contracts import envelope_id
from zerg.storage_v2.contracts import hash_records

MAGIC = b"LHRAW2\x00\x00"
FORMAT_VERSION = 2
MAX_RECORD_BYTES = 4 * 1024 * 1024
MAX_RECORDS = 10_000
MAX_HEADER_BYTES = 16 * 1024
MAX_ENCODED_BYTES = MAX_RECORD_BYTES + MAX_HEADER_BYTES + (MAX_RECORDS * 12)
MAX_COMPRESSED_BYTES = 8 * 1024 * 1024


class RawObjectError(RuntimeError):
    """Base error for the immutable raw-object boundary."""


class RawObjectValidationError(RawObjectError):
    pass


class RawObjectCorruptError(RawObjectError):
    pass


@dataclass(frozen=True, slots=True)
class RawRecord:
    source_position: int
    data: bytes


@dataclass(frozen=True, slots=True)
class RawObjectSpec:
    tenant_id: str
    machine_id: str
    session_id: UUID
    provider: str
    opaque_source_id: str
    source_epoch: UUID
    range_kind: str
    range_start: int
    range_end: int
    records: tuple[RawRecord, ...]
    provenance_kind: str = "native"


@dataclass(frozen=True, slots=True)
class SealedRawObject:
    envelope_id: str
    object_hash: str
    payload_hash: str
    compressed_hash: str
    object_path: str
    uncompressed_size: int
    compressed_size: int
    record_hashes: tuple[str, ...]
    reused: bool


@dataclass(frozen=True, slots=True)
class DecodedRawObject:
    spec: RawObjectSpec
    envelope_id: str
    payload_hash: str
    object_hash: str


def seal_raw_object(root: Path, spec: RawObjectSpec) -> SealedRawObject:
    payload, identity, record_hash_values = encode_raw_object(spec)
    payload_hash = hashlib.sha256(payload).hexdigest()
    compressed = zstandard.ZstdCompressor(level=3, write_checksum=True, write_content_size=True).compress(payload)
    if len(compressed) > MAX_COMPRESSED_BYTES:
        raise RawObjectValidationError("compressed raw object exceeds 8 MiB")
    compressed_hash = hashlib.sha256(compressed).hexdigest()
    relative_path = Path("raw") / "v2" / compressed_hash[:2] / f"{compressed_hash}.zst"
    final_path = _safe_path(root, relative_path)
    final_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    if final_path.exists():
        existing = final_path.read_bytes()
        if hashlib.sha256(existing).hexdigest() != compressed_hash or existing != compressed:
            raise RawObjectCorruptError(f"existing content-addressed raw object is corrupt: {relative_path}")
        reused = True
    else:
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{compressed_hash}.tmp-",
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

    return SealedRawObject(
        envelope_id=envelope_id(identity),
        object_hash=compressed_hash,
        payload_hash=payload_hash,
        compressed_hash=compressed_hash,
        object_path=relative_path.as_posix(),
        uncompressed_size=len(payload),
        compressed_size=len(compressed),
        record_hashes=tuple(value.hex() for value in record_hash_values),
        reused=reused,
    )


def read_raw_object(root: Path, object_path: str, *, expected_object_hash: str) -> DecodedRawObject:
    if not _is_hash(expected_object_hash):
        raise RawObjectValidationError("expected_object_hash must be lowercase SHA-256 hex")
    relative_path = Path(object_path)
    path = _safe_path(root, relative_path)
    if expected_object_hash not in relative_path.name:
        raise RawObjectValidationError("object path is not addressed by expected hash")
    try:
        compressed = path.read_bytes()
    except OSError as exc:
        raise RawObjectCorruptError(f"raw object is unreadable: {relative_path}") from exc
    if len(compressed) > MAX_COMPRESSED_BYTES:
        raise RawObjectCorruptError("compressed raw object exceeds its bound")
    object_hash = hashlib.sha256(compressed).hexdigest()
    if object_hash != expected_object_hash:
        raise RawObjectCorruptError("compressed raw object hash mismatch")
    try:
        payload = zstandard.ZstdDecompressor().decompress(compressed, max_output_size=MAX_ENCODED_BYTES)
    except zstandard.ZstdError as exc:
        raise RawObjectCorruptError("raw object zstd payload is corrupt") from exc
    if len(payload) > MAX_ENCODED_BYTES:
        raise RawObjectCorruptError("raw object payload exceeds its bound")
    spec, stored_envelope = decode_raw_object(payload)
    identity = _identity(spec)
    computed_envelope = envelope_id(identity)
    if stored_envelope != computed_envelope:
        raise RawObjectCorruptError("raw object envelope identity mismatch")
    return DecodedRawObject(
        spec=spec,
        envelope_id=computed_envelope,
        payload_hash=hashlib.sha256(payload).hexdigest(),
        object_hash=object_hash,
    )


def encode_raw_object(spec: RawObjectSpec) -> tuple[bytes, EnvelopeIdentity, tuple[bytes, ...]]:
    _validate_spec(spec)
    identity = _identity(spec)
    record_hash_values = identity.record_hashes
    computed_envelope = envelope_id(identity)
    header = {
        "envelope_id": computed_envelope,
        "format_version": FORMAT_VERSION,
        "machine_id": spec.machine_id,
        "opaque_source_id": spec.opaque_source_id,
        "provenance_kind": spec.provenance_kind,
        "provider": spec.provider,
        "range_end": str(spec.range_end),
        "range_kind": spec.range_kind,
        "range_start": str(spec.range_start),
        "record_count": len(spec.records),
        "session_id": str(spec.session_id),
        "source_epoch": str(spec.source_epoch),
        "tenant_id": spec.tenant_id,
    }
    header_bytes = json.dumps(header, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(header_bytes) > MAX_HEADER_BYTES:
        raise RawObjectValidationError("raw object header exceeds 16 KiB")
    encoded = bytearray(MAGIC)
    encoded.extend(struct.pack(">HHI", FORMAT_VERSION, 0, len(header_bytes)))
    encoded.extend(header_bytes)
    for record in spec.records:
        encoded.extend(struct.pack(">QI", record.source_position, len(record.data)))
        encoded.extend(record.data)
    if len(encoded) > MAX_ENCODED_BYTES:
        raise RawObjectValidationError("encoded raw object exceeds its bound")
    return bytes(encoded), identity, record_hash_values


def validate_raw_object_spec(spec: RawObjectSpec) -> None:
    """Validate a wire-derived spec without allocating its encoded object."""

    _validate_spec(spec)


def decode_raw_object(payload: bytes) -> tuple[RawObjectSpec, str]:
    view = memoryview(payload)
    prefix_size = len(MAGIC) + 8
    if len(view) < prefix_size or bytes(view[: len(MAGIC)]) != MAGIC:
        raise RawObjectCorruptError("raw object magic is invalid")
    version, reserved, header_length = struct.unpack(">HHI", view[len(MAGIC) : prefix_size])
    if version != FORMAT_VERSION or reserved != 0 or header_length > MAX_HEADER_BYTES:
        raise RawObjectCorruptError("raw object version/header is invalid")
    cursor = prefix_size
    header_end = cursor + header_length
    if header_end > len(view):
        raise RawObjectCorruptError("raw object header is truncated")
    try:
        header = json.loads(bytes(view[cursor:header_end]).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RawObjectCorruptError("raw object header JSON is invalid") from exc
    expected_fields = {
        "envelope_id",
        "format_version",
        "machine_id",
        "opaque_source_id",
        "provenance_kind",
        "provider",
        "range_end",
        "range_kind",
        "range_start",
        "record_count",
        "session_id",
        "source_epoch",
        "tenant_id",
    }
    if not isinstance(header, dict) or set(header) != expected_fields or header["format_version"] != FORMAT_VERSION:
        raise RawObjectCorruptError("raw object header shape is invalid")
    try:
        record_count = int(header["record_count"])
        range_start = int(header["range_start"])
        range_end = int(header["range_end"])
        session_id = UUID(header["session_id"])
        source_epoch = UUID(header["source_epoch"])
    except (TypeError, ValueError) as exc:
        raise RawObjectCorruptError("raw object header values are invalid") from exc
    if not 0 <= record_count <= MAX_RECORDS:
        raise RawObjectCorruptError("raw object record count exceeds its bound")
    cursor = header_end
    records: list[RawRecord] = []
    raw_bytes = 0
    for _ in range(record_count):
        if cursor + 12 > len(view):
            raise RawObjectCorruptError("raw object record table is truncated")
        position, length = struct.unpack(">QI", view[cursor : cursor + 12])
        cursor += 12
        end = cursor + length
        if end > len(view):
            raise RawObjectCorruptError("raw object record payload is truncated")
        raw_bytes += length
        if raw_bytes > MAX_RECORD_BYTES:
            raise RawObjectCorruptError("raw object record bytes exceed their bound")
        records.append(RawRecord(source_position=position, data=bytes(view[cursor:end])))
        cursor = end
    if cursor != len(view):
        raise RawObjectCorruptError("raw object has trailing bytes")
    spec = RawObjectSpec(
        tenant_id=header["tenant_id"],
        machine_id=header["machine_id"],
        session_id=session_id,
        provider=header["provider"],
        opaque_source_id=header["opaque_source_id"],
        source_epoch=source_epoch,
        range_kind=header["range_kind"],
        range_start=range_start,
        range_end=range_end,
        records=tuple(records),
        provenance_kind=header["provenance_kind"],
    )
    try:
        _validate_spec(spec)
    except (RawObjectValidationError, UnicodeEncodeError, ValueError) as exc:
        raise RawObjectCorruptError("raw object facts violate the v2 contract") from exc
    stored_envelope = header["envelope_id"]
    if not _is_hash(stored_envelope):
        raise RawObjectCorruptError("raw object envelope hash is invalid")
    return spec, stored_envelope


def _identity(spec: RawObjectSpec) -> EnvelopeIdentity:
    return EnvelopeIdentity(
        tenant_id=spec.tenant_id,
        machine_id=spec.machine_id,
        provider=spec.provider,
        opaque_source_id=spec.opaque_source_id,
        source_epoch=spec.source_epoch,
        range_kind=spec.range_kind,
        range_start=spec.range_start,
        range_end=spec.range_end,
        record_hashes=hash_records(tuple(record.data for record in spec.records)),
    )


def _validate_spec(spec: RawObjectSpec) -> None:
    if spec.provenance_kind not in {"native", "legacy_source_lines", "legacy_fallback"}:
        raise RawObjectValidationError("unsupported provenance_kind")
    if len(spec.records) > MAX_RECORDS:
        raise RawObjectValidationError("raw object exceeds 10000 records")
    total_bytes = sum(len(record.data) for record in spec.records)
    if total_bytes > MAX_RECORD_BYTES:
        raise RawObjectValidationError("raw record bytes exceed 4 MiB")
    if not 0 <= spec.range_start <= spec.range_end < 1 << 64:
        raise RawObjectValidationError("raw source range exceeds u64")
    if any(not 0 <= record.source_position < 1 << 64 for record in spec.records):
        raise RawObjectValidationError("raw record position exceeds u64")
    if spec.range_start == spec.range_end and spec.records:
        raise RawObjectValidationError("empty range cannot contain records")
    if spec.range_start < spec.range_end and not spec.records:
        raise RawObjectValidationError("non-empty range must contain records")
    if spec.range_kind == "byte_offset":
        position = spec.range_start
        for record in spec.records:
            if record.source_position != position or not record.data:
                raise RawObjectValidationError("byte-offset records must be non-empty and contiguous")
            position += len(record.data)
        if position != spec.range_end:
            raise RawObjectValidationError("byte-offset records do not cover the declared range")
    elif spec.range_kind == "record_ordinal":
        expected = list(range(spec.range_start, spec.range_end))
        if [record.source_position for record in spec.records] != expected:
            raise RawObjectValidationError("record-ordinal records must cover the declared range")
    else:
        raise RawObjectValidationError("unsupported range_kind")
    # Frozen identity encoding performs NFC/provider/hash validation.
    envelope_id(_identity(spec))


def _safe_path(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise RawObjectValidationError("raw object path must be safe and relative")
    resolved_root = root.expanduser().resolve()
    resolved = (resolved_root / relative).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise RawObjectValidationError("raw object path escapes storage root")
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
    "DecodedRawObject",
    "RawObjectCorruptError",
    "RawObjectError",
    "RawObjectSpec",
    "RawObjectValidationError",
    "RawRecord",
    "SealedRawObject",
    "decode_raw_object",
    "encode_raw_object",
    "read_raw_object",
    "seal_raw_object",
    "validate_raw_object_spec",
]
