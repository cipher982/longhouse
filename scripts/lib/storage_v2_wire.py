#!/usr/bin/env python3
"""Dependency-free storage-v2 wire helpers for standalone scripts.

This module mirrors the protocol-v2 envelope identity contract in
``server/zerg/storage_v2/contracts.py`` without importing server packages.
Parity tests pin the byte-exact ``expected_envelope_id`` against the
authoritative server implementation.
"""

from __future__ import annotations

import base64
import hashlib
import re
import struct
import unicodedata
import uuid
from typing import Any

_ENVELOPE_DOMAIN = b"longhouse-envelope-v2\0"
_PROVIDER_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}\Z")
_RANGE_KINDS = {"byte_offset": 1, "record_ordinal": 2}

# Stable canary bootstrap facts. Restart replay must hash-identical.
CANARY_PROVIDER = "canary"
CANARY_OPAQUE_SOURCE_ID = "canary-bootstrap.jsonl"
CANARY_RAW_BYTES = b"longhouse-canary-bootstrap-v1\n"
CANARY_PARSER_REVISION = "canary-parser-v1"
CANARY_ORDERING_REVISION = "canary-order-v1"
CANARY_EPOCH_OPENED_AT = "2024-01-01T00:00:00+00:00"
CANARY_SESSION_STARTED_AT = "2024-01-01T00:00:00+00:00"
CANARY_SESSION_LAST_ACTIVITY_AT = "2024-01-01T00:00:00+00:00"
CANARY_RENDER_ORDER_TIME_US = 1_704_067_200_000_000
_CANARY_NAMESPACE = uuid.NAMESPACE_URL


def _canonical_utf8(value: str, *, field: str) -> bytes:
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{field} must already be NFC-normalized")
    return value.encode("utf-8", errors="strict")


def _length_prefixed(value: bytes, *, width: int) -> bytes:
    maximum = (1 << (width * 8)) - 1
    if len(value) > maximum:
        raise ValueError(f"field exceeds {maximum} bytes")
    return len(value).to_bytes(width, "big") + value


def hash_records(records: tuple[bytes, ...]) -> tuple[bytes, ...]:
    return tuple(hashlib.sha256(record).digest() for record in records)


def encode_envelope_preimage(
    *,
    tenant_id: str,
    machine_id: str,
    provider: str,
    opaque_source_id: str,
    source_epoch: uuid.UUID,
    range_kind: str,
    range_start: int,
    range_end: int,
    record_hashes: tuple[bytes, ...],
) -> bytes:
    provider_bytes = provider.encode("ascii", errors="strict")
    if not _PROVIDER_PATTERN.fullmatch(provider):
        raise ValueError("provider must be canonical lowercase ASCII")
    range_kind_code = _RANGE_KINDS.get(range_kind)
    if range_kind_code is None:
        raise ValueError("unsupported range_kind")
    if not 0 <= range_start <= range_end < 1 << 64:
        raise ValueError("range must be an unsigned [start, end) interval")
    if len(record_hashes) >= 1 << 32:
        raise ValueError("record count exceeds u32")
    if range_start == range_end and record_hashes:
        raise ValueError("an empty range cannot contain records")
    if range_start < range_end and not record_hashes:
        raise ValueError("a non-empty range must contain records")
    if any(len(record_hash) != 32 for record_hash in record_hashes):
        raise ValueError("record hashes must be SHA-256 digests")

    preimage = bytearray(_ENVELOPE_DOMAIN)
    preimage.extend(_length_prefixed(_canonical_utf8(tenant_id, field="tenant_id"), width=4))
    preimage.extend(_length_prefixed(_canonical_utf8(machine_id, field="machine_id"), width=4))
    preimage.extend(_length_prefixed(provider_bytes, width=4))
    preimage.extend(_length_prefixed(_canonical_utf8(opaque_source_id, field="opaque_source_id"), width=4))
    preimage.extend(source_epoch.bytes)
    preimage.extend(
        struct.pack(">BQQI", range_kind_code, range_start, range_end, len(record_hashes))
    )
    preimage.extend(b"".join(record_hashes))
    return bytes(preimage)


def expected_envelope_id(
    *,
    tenant_id: str,
    machine_id: str,
    provider: str,
    opaque_source_id: str,
    source_epoch: uuid.UUID,
    range_kind: str,
    range_start: int,
    range_end: int,
    record_hashes: tuple[bytes, ...],
) -> str:
    return hashlib.sha256(
        encode_envelope_preimage(
            tenant_id=tenant_id,
            machine_id=machine_id,
            provider=provider,
            opaque_source_id=opaque_source_id,
            source_epoch=source_epoch,
            range_kind=range_kind,
            range_start=range_start,
            range_end=range_end,
            record_hashes=record_hashes,
        )
    ).hexdigest()


def canary_source_epoch(session_id: str) -> uuid.UUID:
    return uuid.uuid5(_CANARY_NAMESPACE, f"longhouse-canary-source-epoch:{session_id}")


def canary_render_generation(session_id: str) -> uuid.UUID:
    return uuid.uuid5(_CANARY_NAMESPACE, f"longhouse-canary-render-generation:{session_id}")


def build_canary_bootstrap_envelope(
    *,
    tenant_id: str,
    machine_id: str,
    session_id: str,
    raw_bytes: bytes = CANARY_RAW_BYTES,
) -> dict[str, Any]:
    """Build one deterministic live-lane storage-v2 bootstrap envelope.

    Identity fields are stable for a given ``session_id`` so producer restarts
    exact-replay instead of opening conflicting epochs/ranges.
    """

    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id must be a non-empty canonical UUID string")
    try:
        parsed_session = uuid.UUID(session_id)
    except ValueError as exc:
        raise ValueError("session_id must be a canonical UUID string") from exc
    if str(parsed_session) != session_id:
        raise ValueError("session_id must be a canonical UUID string")

    source_epoch = canary_source_epoch(session_id)
    generation_id = canary_render_generation(session_id)
    range_start = 0
    range_end = len(raw_bytes)
    record_hashes = hash_records((raw_bytes,))
    envelope = expected_envelope_id(
        tenant_id=tenant_id,
        machine_id=machine_id,
        provider=CANARY_PROVIDER,
        opaque_source_id=CANARY_OPAQUE_SOURCE_ID,
        source_epoch=source_epoch,
        range_kind="byte_offset",
        range_start=range_start,
        range_end=range_end,
        record_hashes=record_hashes,
    )
    return {
        "protocol_version": 2,
        "tenant_id": tenant_id,
        "machine_id": machine_id,
        "session_id": session_id,
        "provider": CANARY_PROVIDER,
        "opaque_source_id": CANARY_OPAQUE_SOURCE_ID,
        "source_epoch": str(source_epoch),
        "predecessor_source_epoch": None,
        "epoch_opened_at": CANARY_EPOCH_OPENED_AT,
        "range_kind": "byte_offset",
        "range_start": range_start,
        "range_end": range_end,
        "render": {
            "generation_id": str(generation_id),
            "parser_revision": CANARY_PARSER_REVISION,
            "ordering_revision": CANARY_ORDERING_REVISION,
            "records": [
                {
                    "event_id": "canary-bootstrap-1",
                    "order_time_us": CANARY_RENDER_ORDER_TIME_US,
                    "source_position": 0,
                    "event_subordinal": 0,
                    "role": "user",
                    "content_text": "longhouse canary bootstrap",
                    "tool_name": None,
                    "tool_input_json": None,
                    "tool_output_text": None,
                    "tool_call_id": None,
                    "thread_id": None,
                    "branch_kind": None,
                    "raw_record_ordinal": 0,
                }
            ],
        },
        "media": [],
        "session": {
            "environment": "test",
            "project": "canary",
            "cwd": None,
            "git_repo": None,
            "git_branch": None,
            "started_at": CANARY_SESSION_STARTED_AT,
            "last_activity_at": CANARY_SESSION_LAST_ACTIVITY_AT,
            "ended_at": None,
            "origin_kind": "test_or_canary",
            "hidden_from_default_timeline": True,
            "launch_actor": None,
            "launch_surface": None,
        },
        "records": [
            {
                "source_position": 0,
                "data_b64": base64.b64encode(raw_bytes).decode("ascii"),
            }
        ],
        "expected_envelope_id": envelope,
    }


__all__ = [
    "CANARY_OPAQUE_SOURCE_ID",
    "CANARY_PROVIDER",
    "CANARY_RAW_BYTES",
    "build_canary_bootstrap_envelope",
    "canary_render_generation",
    "canary_source_epoch",
    "encode_envelope_preimage",
    "expected_envelope_id",
    "hash_records",
]
