from __future__ import annotations

import json
import os
import time

import pytest
import zstandard as zstd

from zerg.services.archive_store import ArchiveCorruptionError
from zerg.services.archive_store import ArchiveRecord
from zerg.services.archive_store import FilesystemArchiveStore


def _record(seq: int, raw: bytes | None = None) -> ArchiveRecord:
    return ArchiveRecord(
        tenant_id="tenant-a",
        session_id="session-a",
        stream="source_lines",
        source_seq=seq,
        legacy_ref={"table": "source_lines", "rowid": seq},
        provider="codex",
        source_path="/tmp/session.jsonl",
        source_offset=seq * 10,
        received_at="2026-06-05T12:00:00Z",
        raw_bytes=raw if raw is not None else f'{{"seq":{seq}}}'.encode(),
    )


def _decompress(path) -> bytes:
    return zstd.ZstdDecompressor().decompress(path.read_bytes())


def _recompress(path, payload: bytes) -> None:
    path.write_bytes(zstd.ZstdCompressor(level=3).compress(payload))


def test_filesystem_archive_store_roundtrips_exact_raw_bytes(tmp_path):
    store = FilesystemArchiveStore(tmp_path)
    raw = b'\x00exact bytes\n{"unicode":"\xe2\x9c\x93"}\n'

    chunk = store.write_chunk([_record(1, raw)])
    records = store.read_chunk(chunk.relative_path)

    assert chunk.relative_path.startswith("tenants/tenant-a/sessions/session-a/chunks/source_lines-")
    assert records == (_record(1, raw),)
    verified = store.verify_chunk(chunk.relative_path)
    assert verified.valid is True
    assert verified.chunk is not None
    assert verified.chunk.file_sha256 == chunk.file_sha256


def test_filesystem_archive_store_streams_verified_records(tmp_path):
    store = FilesystemArchiveStore(tmp_path)
    expected = [_record(index, b"x" * 1024) for index in range(1, 20)]
    chunk = store.write_chunk(expected)

    assert list(store.iter_chunk_records(chunk.relative_path)) == expected


def test_filesystem_archive_store_stream_rejects_corrupt_payload_before_yield(tmp_path):
    store = FilesystemArchiveStore(tmp_path)
    chunk = store.write_chunk([_record(1), _record(2)])
    payload = _decompress(chunk.path)
    _recompress(chunk.path, payload.replace(b'"raw_sha256":"', b'"raw_sha256":"0', 1))

    with pytest.raises(ArchiveCorruptionError, match="raw_sha256 mismatch|payload sha"):
        next(store.iter_chunk_records(chunk.relative_path))


def test_filesystem_archive_store_duplicate_write_is_idempotent(tmp_path):
    store = FilesystemArchiveStore(tmp_path)
    records = [_record(1), _record(2)]

    first = store.write_chunk(records)
    second = store.write_chunk(records)

    assert second.relative_path == first.relative_path
    assert second.file_sha256 == first.file_sha256
    assert len(list((tmp_path / "tenants" / "tenant-a" / "sessions" / "session-a" / "chunks").glob("*.jsonl.zst"))) == 1


def test_filesystem_archive_store_scopes_chunks_by_tenant(tmp_path):
    store = FilesystemArchiveStore(tmp_path)
    session_id = "shared-session"
    tenant_a = ArchiveRecord(
        tenant_id="tenant-a",
        session_id=session_id,
        stream="source_lines",
        source_seq=1,
        raw_bytes=b"tenant-a",
    )
    tenant_b = ArchiveRecord(
        tenant_id="tenant-b",
        session_id=session_id,
        stream="source_lines",
        source_seq=1,
        raw_bytes=b"tenant-b",
    )

    store.write_chunk([tenant_a])
    store.write_chunk([tenant_b])

    assert [chunk.tenant_id for chunk in store.list_chunks(tenant_id="tenant-a", session_id=session_id)] == ["tenant-a"]
    assert [chunk.tenant_id for chunk in store.list_chunks(tenant_id="tenant-b", session_id=session_id)] == ["tenant-b"]
    assert sorted(chunk.tenant_id for chunk in store.list_chunks(session_id=session_id)) == ["tenant-a", "tenant-b"]


def test_filesystem_archive_store_rejects_out_of_order_or_duplicate_source_seq(tmp_path):
    store = FilesystemArchiveStore(tmp_path)

    with pytest.raises(ValueError, match="strictly increasing"):
        store.write_chunk([_record(2), _record(1)])

    with pytest.raises(ValueError, match="strictly increasing"):
        store.write_chunk([_record(1), _record(1)])


def test_filesystem_archive_store_detects_record_sha_mismatch(tmp_path):
    store = FilesystemArchiveStore(tmp_path)
    chunk = store.write_chunk([_record(1)])
    payload = _decompress(chunk.path)
    obj = json.loads(payload.splitlines()[0])
    obj["raw_sha256"] = "0" * 64
    _recompress(chunk.path, json.dumps(obj, sort_keys=True, separators=(",", ":")).encode() + b"\n")

    result = store.verify_chunk(chunk.relative_path)

    assert result.valid is False
    assert "raw_sha256 mismatch" in "; ".join(result.errors)


def test_filesystem_archive_store_detects_file_sha_mismatch(tmp_path):
    store = FilesystemArchiveStore(tmp_path)
    chunk = store.write_chunk([_record(1)])
    chunk.path.write_bytes(chunk.path.read_bytes() + b"trailing corruption")

    result = store.verify_chunk(chunk.relative_path, expected_file_sha256=chunk.file_sha256)

    assert result.valid is False
    assert "file sha does not match manifest" in result.errors


def test_filesystem_archive_store_reports_malformed_jsonl_record(tmp_path):
    store = FilesystemArchiveStore(tmp_path)
    chunk = store.write_chunk([_record(1)])
    _recompress(chunk.path, _decompress(chunk.path) + b"not-json\n")

    result = store.verify_chunk(chunk.relative_path)

    assert result.valid is False
    assert any("malformed JSON" in error for error in result.errors)


def test_filesystem_archive_store_reports_missing_identity_fields(tmp_path):
    store = FilesystemArchiveStore(tmp_path)
    chunk = store.write_chunk([_record(1)])
    payload = _decompress(chunk.path)
    obj = json.loads(payload.splitlines()[0])
    del obj["tenant_id"]
    _recompress(chunk.path, json.dumps(obj, sort_keys=True, separators=(",", ":")).encode() + b"\n")

    result = store.verify_chunk(chunk.relative_path)

    assert result.valid is False
    assert any("missing tenant_id" in error for error in result.errors)


def test_filesystem_archive_store_splits_on_chunk_boundary(tmp_path):
    store = FilesystemArchiveStore(tmp_path)
    records = [_record(seq, b"x" * 128) for seq in range(5)]

    chunks = store.write_record_chunks(records, target_uncompressed_bytes=350)

    assert len(chunks) > 1
    assert [record.source_seq for chunk in chunks for record in store.read_chunk(chunk.relative_path)] == list(range(5))


def test_filesystem_archive_store_recovers_temp_and_reports_unmanifested_chunks(tmp_path):
    store = FilesystemArchiveStore(tmp_path)
    chunk = store.write_chunk([_record(1)])
    tmp_dir = tmp_path / "tenants" / "tenant-a" / "sessions" / "session-a" / "chunks"
    tmp_file = tmp_dir / "source_lines-000000000002-000000000002-deadbeef.jsonl.zst.999.tmp"
    tmp_file.write_bytes(b"partial")
    old = time.time() - 600
    os_times = (old, old)
    tmp_file.touch()

    os.utime(tmp_file, os_times)

    result = store.recover_orphans(known_chunk_paths=set())

    assert result.moved_temp_files == (f"tenants/tenant-a/sessions/session-a/chunks/{tmp_file.name}",)
    assert result.untracked_chunks == (chunk.relative_path,)
    assert not tmp_file.exists()
    assert (tmp_path / "orphans" / "tmp").exists()


def test_filesystem_archive_store_skips_fresh_temp_files_during_recovery(tmp_path):
    store = FilesystemArchiveStore(tmp_path)
    tmp_dir = tmp_path / "tenants" / "tenant-a" / "sessions" / "session-a" / "chunks"
    tmp_dir.mkdir(parents=True)
    tmp_file = tmp_dir / "source_lines-000000000001-000000000001-deadbeef.jsonl.zst.999.tmp"
    tmp_file.write_bytes(b"partial")

    result = store.recover_orphans(known_chunk_paths=set())

    assert result.moved_temp_files == ()
    assert tmp_file.exists()
