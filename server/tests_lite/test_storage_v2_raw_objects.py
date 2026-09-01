from __future__ import annotations

import tracemalloc
from dataclasses import replace
from uuid import UUID

import pytest

from zerg.storage_v2.raw_objects import MAX_RECORD_BYTES
from zerg.storage_v2.raw_objects import RawObjectCorruptError
from zerg.storage_v2.raw_objects import RawObjectSpec
from zerg.storage_v2.raw_objects import RawObjectValidationError
from zerg.storage_v2.raw_objects import RawRecord
from zerg.storage_v2.raw_objects import read_raw_object
from zerg.storage_v2.raw_objects import seal_raw_object
from zerg.storage_v2.raw_objects import validate_raw_object_spec


def _spec(*, records: tuple[RawRecord, ...] | None = None) -> RawObjectSpec:
    records = records or (
        RawRecord(source_position=0, data=b'{"role":"user"}\r\n'),
        RawRecord(source_position=17, data=b'{"role":"assistant"}\n'),
    )
    return RawObjectSpec(
        tenant_id="tenant-a",
        machine_id="cinder",
        session_id=UUID("018f0c3a-7b2d-7f10-8a11-123456789abc"),
        provider="codex",
        opaque_source_id="history.jsonl",
        source_epoch=UUID("550e8400-e29b-41d4-a716-446655440000"),
        range_kind="byte_offset",
        range_start=0,
        range_end=sum(len(record.data) for record in records),
        records=records,
    )


def test_seal_is_deterministic_idempotent_and_exactly_readable(tmp_path):
    spec = _spec()
    first = seal_raw_object(tmp_path, spec)
    second = seal_raw_object(tmp_path, spec)

    assert first.envelope_id == second.envelope_id
    assert first.object_hash == second.object_hash
    assert first.payload_hash == second.payload_hash
    assert first.reused is False
    assert second.reused is True
    assert first.object_path == f"raw/v2/{first.object_hash[:2]}/{first.object_hash}.zst"

    decoded = read_raw_object(tmp_path, first.object_path, expected_object_hash=first.object_hash)
    assert decoded.spec == spec
    assert decoded.envelope_id == first.envelope_id
    assert decoded.payload_hash == first.payload_hash
    assert decoded.object_hash == first.object_hash


def test_sealed_bytes_and_hashes_are_stable_across_roots(tmp_path):
    spec = _spec()
    left = seal_raw_object(tmp_path / "left", spec)
    right = seal_raw_object(tmp_path / "right", spec)
    assert left.object_hash == right.object_hash
    assert left.payload_hash == right.payload_hash
    assert (tmp_path / "left" / left.object_path).read_bytes() == (tmp_path / "right" / right.object_path).read_bytes()


def test_reader_attributes_compressed_and_payload_corruption(tmp_path):
    sealed = seal_raw_object(tmp_path, _spec())
    path = tmp_path / sealed.object_path
    damaged = bytearray(path.read_bytes())
    damaged[-1] ^= 0x01
    path.write_bytes(damaged)

    with pytest.raises(RawObjectCorruptError, match="hash mismatch"):
        read_raw_object(tmp_path, sealed.object_path, expected_object_hash=sealed.object_hash)
    with pytest.raises(RawObjectCorruptError, match="existing content-addressed"):
        seal_raw_object(tmp_path, _spec())


def test_validation_rejects_gaps_oversize_and_path_escape(tmp_path):
    with pytest.raises(RawObjectValidationError, match="contiguous"):
        seal_raw_object(
            tmp_path,
            _spec(
                records=(
                    RawRecord(source_position=0, data=b"a\n"),
                    RawRecord(source_position=3, data=b"b\n"),
                )
            ),
        )

    oversized = RawRecord(source_position=0, data=b"x" * (MAX_RECORD_BYTES + 1))
    with pytest.raises(RawObjectValidationError, match="32 MiB"):
        seal_raw_object(tmp_path, _spec(records=(oversized,)))

    sealed = seal_raw_object(tmp_path, _spec())
    with pytest.raises(RawObjectValidationError, match="safe and relative"):
        read_raw_object(tmp_path, f"../{sealed.object_hash}.zst", expected_object_hash=sealed.object_hash)


def test_record_ordinal_round_trip_and_identity_excludes_session_membership(tmp_path):
    records = (
        RawRecord(source_position=41, data=b"db-row-one"),
        RawRecord(source_position=42, data=b"db-row-two"),
    )
    spec = replace(
        _spec(),
        range_kind="record_ordinal",
        range_start=41,
        range_end=43,
        records=records,
    )
    first = seal_raw_object(tmp_path / "first", spec)
    relinked = seal_raw_object(
        tmp_path / "relinked",
        replace(spec, session_id=UUID("018f0c3a-7b2d-7f10-8a11-000000000001")),
    )

    assert first.envelope_id == relinked.envelope_id
    assert first.payload_hash != relinked.payload_hash
    assert read_raw_object(tmp_path / "first", first.object_path, expected_object_hash=first.object_hash).spec == spec


def test_legacy_source_line_provenance_round_trips(tmp_path):
    spec = replace(_spec(), provenance_kind="legacy_source_lines")
    sealed = seal_raw_object(tmp_path, spec)
    decoded = read_raw_object(tmp_path, sealed.object_path, expected_object_hash=sealed.object_hash)
    assert decoded.spec.provenance_kind == "legacy_source_lines"


def test_legacy_normalized_event_provenance_round_trips(tmp_path):
    spec = replace(_spec(), provenance_kind="legacy_normalized_event")
    sealed = seal_raw_object(tmp_path, spec)
    decoded = read_raw_object(tmp_path, sealed.object_path, expected_object_hash=sealed.object_hash)
    assert decoded.spec.provenance_kind == "legacy_normalized_event"


def test_record_ordinal_span_is_bounded_by_the_record_count_not_by_u64():
    """A declared ordinal span must never be materialized.

    `range_end` is only bounded by u64, so an envelope declaring a huge span
    with one record used to allocate `list(range(start, end))` before it could
    notice the mismatch: 439 MB at a span of 10 million, and an out-of-memory
    kill at the u64 ceiling. Validation runs before the envelope-id integrity
    check, so any caller holding a device token could repeat it.
    """

    spec = replace(
        _spec(),
        range_kind="record_ordinal",
        range_start=0,
        range_end=10_000_000,
        records=(RawRecord(source_position=0, data=b"one-row"),),
    )

    tracemalloc.start()
    try:
        with pytest.raises(RawObjectValidationError, match="cover the declared range"):
            validate_raw_object_spec(spec)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 1024 * 1024


def test_record_ordinal_positions_must_match_the_declared_range_exactly():
    base = replace(_spec(), range_kind="record_ordinal", range_start=41, range_end=43)

    with pytest.raises(RawObjectValidationError, match="cover the declared range"):
        validate_raw_object_spec(
            replace(
                base,
                records=(
                    RawRecord(source_position=41, data=b"a"),
                    RawRecord(source_position=99, data=b"b"),
                ),
            )
        )
    with pytest.raises(RawObjectValidationError, match="cover the declared range"):
        validate_raw_object_spec(replace(base, records=(RawRecord(source_position=41, data=b"a"),)))
    validate_raw_object_spec(
        replace(
            base,
            records=(
                RawRecord(source_position=41, data=b"a"),
                RawRecord(source_position=42, data=b"b"),
            ),
        )
    )
