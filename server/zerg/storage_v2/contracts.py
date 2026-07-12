"""Byte-exact, versioned identities for the storage-v2 durability boundary."""

from __future__ import annotations

import base64
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
