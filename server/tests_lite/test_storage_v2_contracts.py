from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

from zerg.storage_v2.contracts import DurableReceipt
from zerg.storage_v2.contracts import EnvelopeIdentity
from zerg.storage_v2.contracts import RenderDetailCursor
from zerg.storage_v2.contracts import decode_render_detail_cursor_token
from zerg.storage_v2.contracts import encode_envelope_preimage
from zerg.storage_v2.contracts import encode_render_detail_cursor
from zerg.storage_v2.contracts import envelope_id
from zerg.storage_v2.contracts import hash_records
from zerg.storage_v2.contracts import render_detail_cursor_token

_VECTORS = Path(__file__).parents[2] / "schemas" / "storage-v2-contract-vectors.json"


def _fixture() -> dict:
    return json.loads(_VECTORS.read_text(encoding="utf-8"))


@pytest.mark.parametrize("vector", _fixture()["envelope_identity"]["vectors"], ids=lambda item: item["name"])
def test_envelope_identity_matches_frozen_vectors(vector):
    record_hashes = hash_records(tuple(bytes.fromhex(value) for value in vector["record_bytes_hex"]))
    assert [value.hex() for value in record_hashes] == vector["record_hashes"]
    identity = EnvelopeIdentity(
        tenant_id=vector["tenant_id"],
        machine_id=vector["machine_id"],
        provider=vector["provider"],
        opaque_source_id=vector["opaque_source_id"],
        source_epoch=UUID(vector["source_epoch"]),
        range_kind=vector["range_kind"],
        range_start=vector["range_start"],
        range_end=vector["range_end"],
        record_hashes=record_hashes,
    )
    assert encode_envelope_preimage(identity).hex() == vector["preimage_hex"]
    assert envelope_id(identity) == vector["envelope_id"]


def test_render_detail_cursor_matches_frozen_vector():
    vector = _fixture()["render_detail_cursor"]
    cursor = RenderDetailCursor(
        session_id=UUID(vector["session_id"]),
        render_generation=UUID(vector["render_generation"]),
        order_time_us=vector["order_time_us"],
        machine_id=vector["machine_id"],
        provider=vector["provider"],
        opaque_source_id=vector["opaque_source_id"],
        source_epoch=UUID(vector["source_epoch"]),
        source_position=vector["source_position"],
        event_subordinal=vector["event_subordinal"],
    )
    assert encode_render_detail_cursor(cursor).hex() == vector["bytes_hex"]
    assert render_detail_cursor_token(cursor) == vector["base64url"]
    assert decode_render_detail_cursor_token(vector["base64url"]) == cursor
    with pytest.raises(ValueError):
        decode_render_detail_cursor_token(vector["base64url"] + "=")


@pytest.mark.parametrize("token", ("!", "TEhDMg", "TEhDMg==", "A" * 8_193))
def test_render_detail_cursor_rejects_noncanonical_or_truncated_tokens(token):
    with pytest.raises(ValueError):
        decode_render_detail_cursor_token(token)


def test_durable_receipt_matches_frozen_wire_example():
    vector = _fixture()["receipt"]
    receipt = DurableReceipt(
        envelope_id=vector["envelope_id"],
        object_hash=vector["object_hash"],
        commit_seq=int(vector["commit_seq"]),
        render_state=vector["render_state"],
        media_state=vector["media_state"],
        missing_media_hashes=tuple(vector["missing_media_hashes"]),
    )
    assert receipt.as_wire() == vector


@pytest.mark.parametrize(
    ("media_state", "missing_hashes"),
    (("complete", ("0" * 64,)), ("missing", ())),
)
def test_durable_receipt_rejects_contradictory_media_state(media_state, missing_hashes):
    receipt = DurableReceipt(
        envelope_id="1" * 64,
        object_hash="2" * 64,
        commit_seq=1,
        render_state="ready",
        media_state=media_state,
        missing_media_hashes=missing_hashes,
    )
    with pytest.raises(ValueError):
        receipt.as_wire()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("provider", "Codex"),
        ("provider", "_codex"),
        ("provider", "a" * 33),
        ("machine_id", "e\u0301"),
        ("opaque_source_id", "e\u0301.jsonl"),
    ),
)
def test_identity_rejects_noncanonical_text(field, value):
    values = {
        "tenant_id": "tenant",
        "machine_id": "cinder",
        "provider": "codex",
        "opaque_source_id": "history.jsonl",
        field: value,
    }
    identity = EnvelopeIdentity(
        **values,
        source_epoch=UUID("550e8400-e29b-41d4-a716-446655440000"),
        range_kind="byte_offset",
        range_start=0,
        range_end=0,
        record_hashes=(),
    )
    with pytest.raises((UnicodeEncodeError, ValueError)):
        encode_envelope_preimage(identity)


def test_identity_rejects_nonempty_range_without_records():
    identity = EnvelopeIdentity(
        tenant_id="tenant",
        machine_id="cinder",
        provider="codex",
        opaque_source_id="history.jsonl",
        source_epoch=UUID("550e8400-e29b-41d4-a716-446655440000"),
        range_kind="record_ordinal",
        range_start=1,
        range_end=2,
        record_hashes=(),
    )
    with pytest.raises(ValueError, match="non-empty range"):
        encode_envelope_preimage(identity)
