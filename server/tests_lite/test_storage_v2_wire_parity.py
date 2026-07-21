"""Parity tests for the dependency-free storage-v2 wire helper."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import UUID

from zerg.routers.agents_storage_v2 import _parse_envelope
from zerg.storage_v2.contracts import EnvelopeIdentity
from zerg.storage_v2.contracts import encode_envelope_preimage
from zerg.storage_v2.contracts import envelope_id
from zerg.storage_v2.contracts import hash_records


def _load_wire():
    path = Path(__file__).resolve().parents[2] / "scripts" / "lib" / "storage_v2_wire.py"
    spec = importlib.util.spec_from_file_location("storage_v2_wire_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_storage_v2_wire_envelope_id_matches_server_contract():
    wire = _load_wire()
    records = (b"alpha\n", b"beta\n")
    record_hashes = hash_records(records)
    identity = EnvelopeIdentity(
        tenant_id="tenant-a",
        machine_id="cinder",
        provider="canary",
        opaque_source_id="canary-bootstrap.jsonl",
        source_epoch=UUID("018f0c3a-7b2d-7f10-8a11-223456789abc"),
        range_kind="byte_offset",
        range_start=0,
        range_end=sum(len(item) for item in records),
        record_hashes=record_hashes,
    )
    assert wire.hash_records(records) == record_hashes
    assert wire.encode_envelope_preimage(
        tenant_id=identity.tenant_id,
        machine_id=identity.machine_id,
        provider=identity.provider,
        opaque_source_id=identity.opaque_source_id,
        source_epoch=identity.source_epoch,
        range_kind=identity.range_kind,
        range_start=identity.range_start,
        range_end=identity.range_end,
        record_hashes=identity.record_hashes,
    ) == encode_envelope_preimage(identity)
    assert (
        wire.expected_envelope_id(
            tenant_id=identity.tenant_id,
            machine_id=identity.machine_id,
            provider=identity.provider,
            opaque_source_id=identity.opaque_source_id,
            source_epoch=identity.source_epoch,
            range_kind=identity.range_kind,
            range_start=identity.range_start,
            range_end=identity.range_end,
            record_hashes=identity.record_hashes,
        )
        == envelope_id(identity)
    )


def test_canary_bootstrap_envelope_is_deterministic_and_idempotent():
    wire = _load_wire()
    session_id = "a776f692-7fb8-44a7-9574-e347fa29b88e"
    first = wire.build_canary_bootstrap_envelope(
        tenant_id="tenant-a",
        machine_id="canary-host",
        session_id=session_id,
    )
    second = wire.build_canary_bootstrap_envelope(
        tenant_id="tenant-a",
        machine_id="canary-host",
        session_id=session_id,
    )
    assert first == second
    assert first["provider"] == "canary"
    assert first["session"]["environment"] == "test"
    assert first["session"]["project"] == "canary"
    assert first["session"]["origin_kind"] == "test_or_canary"
    assert first["session"]["hidden_from_default_timeline"] is True
    assert first["opaque_source_id"] == "canary-bootstrap.jsonl"
    assert first["source_epoch"] == str(wire.canary_source_epoch(session_id))
    assert first["render"]["generation_id"] == str(wire.canary_render_generation(session_id))

    identity = EnvelopeIdentity(
        tenant_id="tenant-a",
        machine_id="canary-host",
        provider="canary",
        opaque_source_id="canary-bootstrap.jsonl",
        source_epoch=UUID(first["source_epoch"]),
        range_kind="byte_offset",
        range_start=0,
        range_end=len(wire.CANARY_RAW_BYTES),
        record_hashes=hash_records((wire.CANARY_RAW_BYTES,)),
    )
    assert first["expected_envelope_id"] == envelope_id(identity)

    raw_spec, parsed = _parse_envelope(
        first,
        tenant_id="tenant-a",
        machine_id="canary-host",
        lane="live",
    )
    assert str(raw_spec.session_id) == session_id
    assert parsed["expected_envelope_id"] == first["expected_envelope_id"]
    assert parsed["render_spec"] is not None
    assert parsed["session_facts"]["hidden_from_default_timeline"] is True
