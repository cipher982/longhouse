"""Byte-exact, versioned identities for the storage-v2 durability boundary."""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import struct
import unicodedata
from dataclasses import dataclass
from uuid import UUID

_ENVELOPE_DOMAIN = b"longhouse-envelope-v2\0"
_PROVIDER_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}\Z")
_RANGE_KINDS = {"byte_offset": 1, "record_ordinal": 2}
_CURSOR_MAGIC = b"LHC2"
_SHA256_HEX_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_BASE64URL_PATTERN = re.compile(r"[A-Za-z0-9_-]+\Z")


def _canonical_utf8(value: str, *, field: str) -> bytes:
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{field} must already be NFC-normalized")
    return value.encode("utf-8", errors="strict")


def _length_prefixed(value: bytes, *, width: int) -> bytes:
    maximum = (1 << (width * 8)) - 1
    if len(value) > maximum:
        raise ValueError(f"field exceeds {maximum} bytes")
    return len(value).to_bytes(width, "big") + value


@dataclass(frozen=True)
class EnvelopeIdentity:
    tenant_id: str
    machine_id: str
    provider: str
    opaque_source_id: str
    source_epoch: UUID
    range_kind: str
    range_start: int
    range_end: int
    record_hashes: tuple[bytes, ...]


def hash_records(records: tuple[bytes, ...]) -> tuple[bytes, ...]:
    return tuple(hashlib.sha256(record).digest() for record in records)


def encode_envelope_preimage(identity: EnvelopeIdentity) -> bytes:
    provider = identity.provider.encode("ascii", errors="strict")
    if not _PROVIDER_PATTERN.fullmatch(identity.provider):
        raise ValueError("provider must be canonical lowercase ASCII")
    range_kind = _RANGE_KINDS.get(identity.range_kind)
    if range_kind is None:
        raise ValueError("unsupported range_kind")
    if not 0 <= identity.range_start <= identity.range_end < 1 << 64:
        raise ValueError("range must be an unsigned [start, end) interval")
    if len(identity.record_hashes) >= 1 << 32:
        raise ValueError("record count exceeds u32")
    if identity.range_start == identity.range_end and identity.record_hashes:
        raise ValueError("an empty range cannot contain records")
    if identity.range_start < identity.range_end and not identity.record_hashes:
        raise ValueError("a non-empty range must contain records")
    if any(len(record_hash) != 32 for record_hash in identity.record_hashes):
        raise ValueError("record hashes must be SHA-256 digests")

    preimage = bytearray(_ENVELOPE_DOMAIN)
    preimage.extend(_length_prefixed(_canonical_utf8(identity.tenant_id, field="tenant_id"), width=4))
    preimage.extend(_length_prefixed(_canonical_utf8(identity.machine_id, field="machine_id"), width=4))
    preimage.extend(_length_prefixed(provider, width=4))
    preimage.extend(_length_prefixed(_canonical_utf8(identity.opaque_source_id, field="opaque_source_id"), width=4))
    preimage.extend(identity.source_epoch.bytes)
    preimage.extend(struct.pack(">BQQI", range_kind, identity.range_start, identity.range_end, len(identity.record_hashes)))
    preimage.extend(b"".join(identity.record_hashes))
    return bytes(preimage)


def envelope_id(identity: EnvelopeIdentity) -> str:
    return hashlib.sha256(encode_envelope_preimage(identity)).hexdigest()


@dataclass(frozen=True)
class DurableReceipt:
    envelope_id: str
    object_hash: str
    commit_seq: int
    render_state: str
    media_state: str
    missing_media_hashes: tuple[str, ...] = ()

    def as_wire(self) -> dict[str, object]:
        if not _SHA256_HEX_PATTERN.fullmatch(self.envelope_id):
            raise ValueError("envelope_id must be lowercase SHA-256 hex")
        if not _SHA256_HEX_PATTERN.fullmatch(self.object_hash):
            raise ValueError("object_hash must be lowercase SHA-256 hex")
        if not 0 <= self.commit_seq < 1 << 64:
            raise ValueError("commit_seq exceeds u64")
        if self.render_state not in {"ready", "pending", "failed"}:
            raise ValueError("unsupported render_state")
        if self.media_state not in {"complete", "pending", "missing"}:
            raise ValueError("unsupported media_state")
        hashes = list(self.missing_media_hashes)
        if hashes != sorted(set(hashes)) or any(not _SHA256_HEX_PATTERN.fullmatch(value) for value in hashes):
            raise ValueError("missing media hashes must be unique, sorted lowercase SHA-256 hex")
        if self.media_state == "complete" and hashes:
            raise ValueError("complete media cannot have missing hashes")
        if self.media_state == "missing" and not hashes:
            raise ValueError("missing media must name at least one hash")
        return {
            "v": 2,
            "envelope_id": self.envelope_id,
            "object_hash": self.object_hash,
            "commit_seq": str(self.commit_seq),
            "raw_state": "durable",
            "render_state": self.render_state,
            "media_state": self.media_state,
            "missing_media_hashes": hashes,
        }


@dataclass(frozen=True)
class RenderDetailCursor:
    session_id: UUID
    render_generation: UUID
    order_time_us: int
    machine_id: str
    provider: str
    opaque_source_id: str
    source_epoch: UUID
    source_position: int
    event_subordinal: int


@dataclass(frozen=True)
class RawExportCursor:
    session_id: UUID
    machine_id: str
    provider: str
    opaque_source_id: str
    source_epoch: UUID
    range_start: int
    envelope_id: str


def encode_render_detail_cursor(cursor: RenderDetailCursor) -> bytes:
    provider = cursor.provider.encode("ascii", errors="strict")
    if not _PROVIDER_PATTERN.fullmatch(cursor.provider):
        raise ValueError("provider must be canonical lowercase ASCII")
    if not -(1 << 63) <= cursor.order_time_us < 1 << 63:
        raise ValueError("order_time_us exceeds i64")
    if not 0 <= cursor.source_position < 1 << 64:
        raise ValueError("source_position exceeds u64")
    if not 0 <= cursor.event_subordinal < 1 << 32:
        raise ValueError("event_subordinal exceeds u32")

    encoded = bytearray(_CURSOR_MAGIC)
    encoded.extend(struct.pack(">BBH", 1, 1, 0))
    encoded.extend(cursor.session_id.bytes)
    encoded.extend(cursor.render_generation.bytes)
    encoded.extend(struct.pack(">q", cursor.order_time_us))
    encoded.extend(_length_prefixed(_canonical_utf8(cursor.machine_id, field="machine_id"), width=2))
    encoded.extend(_length_prefixed(provider, width=2))
    encoded.extend(_length_prefixed(_canonical_utf8(cursor.opaque_source_id, field="opaque_source_id"), width=4))
    encoded.extend(cursor.source_epoch.bytes)
    encoded.extend(struct.pack(">QI", cursor.source_position, cursor.event_subordinal))
    return bytes(encoded)


def render_detail_cursor_token(cursor: RenderDetailCursor) -> str:
    return base64.urlsafe_b64encode(encode_render_detail_cursor(cursor)).rstrip(b"=").decode("ascii")


def decode_render_detail_cursor_token(token: str) -> RenderDetailCursor:
    if not isinstance(token, str) or not token or len(token) > 8_192 or not _BASE64URL_PATTERN.fullmatch(token):
        raise ValueError("render cursor must be a bounded base64url token")
    try:
        padding = "=" * (-len(token) % 4)
        encoded = base64.b64decode(token + padding, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("render cursor is not valid base64url") from exc
    minimum = 4 + 4 + 16 + 16 + 8 + 2 + 2 + 4 + 16 + 8 + 4
    if len(encoded) < minimum or encoded[:4] != _CURSOR_MAGIC:
        raise ValueError("render cursor has an invalid header")
    kind, version, reserved = struct.unpack_from(">BBH", encoded, 4)
    if (kind, version, reserved) != (1, 1, 0):
        raise ValueError("render cursor version is unsupported")
    offset = 8
    session_id = UUID(bytes=encoded[offset : offset + 16])
    offset += 16
    render_generation = UUID(bytes=encoded[offset : offset + 16])
    offset += 16
    (order_time_us,) = struct.unpack_from(">q", encoded, offset)
    offset += 8

    def read_text(width: int, field: str) -> str:
        nonlocal offset
        if offset + width > len(encoded):
            raise ValueError("render cursor is truncated")
        length = int.from_bytes(encoded[offset : offset + width], "big")
        offset += width
        if offset + length > len(encoded):
            raise ValueError("render cursor is truncated")
        try:
            value = encoded[offset : offset + length].decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError(f"render cursor {field} is not UTF-8") from exc
        offset += length
        if not value or unicodedata.normalize("NFC", value) != value:
            raise ValueError(f"render cursor {field} is not canonical")
        return value

    machine_id = read_text(2, "machine_id")
    provider = read_text(2, "provider")
    if not _PROVIDER_PATTERN.fullmatch(provider):
        raise ValueError("render cursor provider is not canonical")
    opaque_source_id = read_text(4, "opaque_source_id")
    if offset + 28 != len(encoded):
        raise ValueError("render cursor has trailing or truncated fields")
    source_epoch = UUID(bytes=encoded[offset : offset + 16])
    offset += 16
    source_position, event_subordinal = struct.unpack_from(">QI", encoded, offset)
    cursor = RenderDetailCursor(
        session_id=session_id,
        render_generation=render_generation,
        order_time_us=order_time_us,
        machine_id=machine_id,
        provider=provider,
        opaque_source_id=opaque_source_id,
        source_epoch=source_epoch,
        source_position=source_position,
        event_subordinal=event_subordinal,
    )
    if encode_render_detail_cursor(cursor) != encoded:
        raise ValueError("render cursor is not canonical")
    return cursor


def encode_raw_export_cursor(cursor: RawExportCursor) -> bytes:
    provider = cursor.provider.encode("ascii", errors="strict")
    if not _PROVIDER_PATTERN.fullmatch(cursor.provider):
        raise ValueError("provider must be canonical lowercase ASCII")
    if not 0 <= cursor.range_start < 1 << 64:
        raise ValueError("range_start exceeds u64")
    if not _SHA256_HEX_PATTERN.fullmatch(cursor.envelope_id):
        raise ValueError("envelope_id must be lowercase SHA-256 hex")
    encoded = bytearray(_CURSOR_MAGIC)
    encoded.extend(struct.pack(">BBH", 2, 1, 0))
    encoded.extend(cursor.session_id.bytes)
    encoded.extend(_length_prefixed(_canonical_utf8(cursor.machine_id, field="machine_id"), width=2))
    encoded.extend(_length_prefixed(provider, width=2))
    encoded.extend(_length_prefixed(_canonical_utf8(cursor.opaque_source_id, field="opaque_source_id"), width=4))
    encoded.extend(cursor.source_epoch.bytes)
    encoded.extend(struct.pack(">Q", cursor.range_start))
    encoded.extend(bytes.fromhex(cursor.envelope_id))
    return bytes(encoded)


def raw_export_cursor_token(cursor: RawExportCursor) -> str:
    return base64.urlsafe_b64encode(encode_raw_export_cursor(cursor)).rstrip(b"=").decode("ascii")


def decode_raw_export_cursor_token(token: str) -> RawExportCursor:
    if not isinstance(token, str) or not token or len(token) > 8_192 or not _BASE64URL_PATTERN.fullmatch(token):
        raise ValueError("raw cursor must be a bounded base64url token")
    try:
        padding = "=" * (-len(token) % 4)
        encoded = base64.b64decode(token + padding, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("raw cursor is not valid base64url") from exc
    minimum = 4 + 4 + 16 + 2 + 2 + 4 + 16 + 8 + 32
    if len(encoded) < minimum or encoded[:4] != _CURSOR_MAGIC:
        raise ValueError("raw cursor has an invalid header")
    kind, version, reserved = struct.unpack_from(">BBH", encoded, 4)
    if (kind, version, reserved) != (2, 1, 0):
        raise ValueError("raw cursor version is unsupported")
    offset = 8
    session_id = UUID(bytes=encoded[offset : offset + 16])
    offset += 16

    def read_text(width: int, field: str) -> str:
        nonlocal offset
        if offset + width > len(encoded):
            raise ValueError("raw cursor is truncated")
        length = int.from_bytes(encoded[offset : offset + width], "big")
        offset += width
        if offset + length > len(encoded):
            raise ValueError("raw cursor is truncated")
        try:
            value = encoded[offset : offset + length].decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError(f"raw cursor {field} is not UTF-8") from exc
        offset += length
        if not value or unicodedata.normalize("NFC", value) != value:
            raise ValueError(f"raw cursor {field} is not canonical")
        return value

    machine_id = read_text(2, "machine_id")
    provider = read_text(2, "provider")
    if not _PROVIDER_PATTERN.fullmatch(provider):
        raise ValueError("raw cursor provider is not canonical")
    opaque_source_id = read_text(4, "opaque_source_id")
    if offset + 56 != len(encoded):
        raise ValueError("raw cursor has trailing or truncated fields")
    source_epoch = UUID(bytes=encoded[offset : offset + 16])
    offset += 16
    (range_start,) = struct.unpack_from(">Q", encoded, offset)
    offset += 8
    envelope_id = encoded[offset : offset + 32].hex()
    cursor = RawExportCursor(
        session_id=session_id,
        machine_id=machine_id,
        provider=provider,
        opaque_source_id=opaque_source_id,
        source_epoch=source_epoch,
        range_start=range_start,
        envelope_id=envelope_id,
    )
    if encode_raw_export_cursor(cursor) != encoded:
        raise ValueError("raw cursor is not canonical")
    return cursor
