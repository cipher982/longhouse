from __future__ import annotations

import json

import zstandard as zstd

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
    raw = b"\x00exact bytes\n{\"unicode\":\"\xe2\x9c\x93\"}\n"

    chunk = store.write_chunk([_record(1, raw)])
    records = store.read_chunk(chunk.relative_path)

    assert chunk.relative_path.startswith("sessions/session-a/chunks/source_lines-")
    assert records == (_record(1, raw),)
    verified = store.verify_chunk(chunk.relative_path)
    assert verified.valid is True
    assert verified.chunk is not None
    assert verified.chunk.file_sha256 == chunk.file_sha256


def test_filesystem_archive_store_duplicate_write_is_idempotent(tmp_path):
    store = FilesystemArchiveStore(tmp_path)
    records = [_record(1), _record(2)]

    first = store.write_chunk(records)
    second = store.write_chunk(records)

    assert second.relative_path == first.relative_path
    assert second.file_sha256 == first.file_sha256
    assert len(list((tmp_path / "sessions" / "session-a" / "chunks").glob("*.jsonl.zst"))) == 1


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


def test_filesystem_archive_store_splits_on_chunk_boundary(tmp_path):
    store = FilesystemArchiveStore(tmp_path)
    records = [_record(seq, b"x" * 128) for seq in range(5)]

    chunks = store.write_record_chunks(records, target_uncompressed_bytes=350)

    assert len(chunks) > 1
    assert [record.source_seq for chunk in chunks for record in store.read_chunk(chunk.relative_path)] == list(range(5))


def test_filesystem_archive_store_recovers_temp_and_reports_unmanifested_chunks(tmp_path):
    store = FilesystemArchiveStore(tmp_path)
    chunk = store.write_chunk([_record(1)])
    tmp_dir = tmp_path / "sessions" / "session-a" / "chunks"
    tmp_file = tmp_dir / "source_lines-000000000002-000000000002-deadbeef.jsonl.zst.999.tmp"
    tmp_file.write_bytes(b"partial")

    result = store.recover_orphans(known_chunk_paths=set())

    assert result.moved_temp_files == (f"sessions/session-a/chunks/{tmp_file.name}",)
    assert result.untracked_chunks == (chunk.relative_path,)
    assert not tmp_file.exists()
    assert (tmp_path / "orphans" / "tmp").exists()
