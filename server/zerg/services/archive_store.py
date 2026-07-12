"""Filesystem archive store for raw session source records.

Phase 2 keeps this store independent from ingest and database manifests. It is
the byte-preserving archive primitive that later phases will wire into shadow
ingest, projectors, and legacy export.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import time
from abc import ABC
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Collection
from typing import Iterable
from typing import Iterator
from typing import Mapping

import zstandard as zstd

ARCHIVE_CHUNK_VERSION = 1
ARCHIVE_CHUNK_SUFFIX = ".jsonl.zst"


class ArchiveStoreError(RuntimeError):
    """Base class for archive-store failures."""


class ArchiveCorruptionError(ArchiveStoreError):
    """Raised when a chunk fails verification or cannot be decoded."""


@dataclass(frozen=True)
class ArchiveRecord:
    tenant_id: str
    session_id: str
    stream: str
    source_seq: int
    raw_bytes: bytes
    legacy_ref: Mapping[str, Any] | None = None
    provider: str | None = None
    source_path: str | None = None
    source_offset: int | None = None
    received_at: str | None = None


@dataclass(frozen=True)
class ArchiveChunkRef:
    tenant_id: str
    session_id: str
    stream: str
    first_source_seq: int
    last_source_seq: int
    record_count: int
    uncompressed_bytes: int
    compressed_bytes: int
    payload_sha256: str
    file_sha256: str
    relative_path: str
    path: Path


@dataclass(frozen=True)
class ArchiveVerifyResult:
    valid: bool
    chunk: ArchiveChunkRef | None
    errors: tuple[str, ...]
    records: tuple[ArchiveRecord, ...] = ()


@dataclass(frozen=True)
class ArchiveRecoveryResult:
    moved_temp_files: tuple[str, ...]
    untracked_chunks: tuple[str, ...]


class ArchiveStore(ABC):
    @abstractmethod
    def write_chunk(self, records: Iterable[ArchiveRecord]) -> ArchiveChunkRef:
        """Write one sealed archive chunk."""

    @abstractmethod
    def write_record_chunks(
        self,
        records: Iterable[ArchiveRecord],
        *,
        target_uncompressed_bytes: int,
    ) -> list[ArchiveChunkRef]:
        """Write records split into bounded chunks."""

    @abstractmethod
    def read_chunk(self, relative_path: str) -> tuple[ArchiveRecord, ...]:
        """Read and verify one chunk, returning exact raw-byte records."""

    @abstractmethod
    def list_chunks(
        self,
        *,
        tenant_id: str | None = None,
        session_id: str | None = None,
        stream: str | None = None,
    ) -> list[ArchiveChunkRef]:
        """List sealed chunk files visible in the store."""

    @abstractmethod
    def verify_chunk(
        self,
        relative_path: str,
        *,
        expected_file_sha256: str | None = None,
        expected_payload_sha256: str | None = None,
    ) -> ArchiveVerifyResult:
        """Verify a sealed chunk."""

    @abstractmethod
    def recover_orphans(
        self,
        *,
        known_chunk_paths: Collection[str] | None = None,
        min_temp_age_seconds: float = 300,
    ) -> ArchiveRecoveryResult:
        """Recover temp files and report sealed chunks missing from a manifest."""


class FilesystemArchiveStore(ArchiveStore):
    def __init__(self, root: str | Path, *, compression_level: int = 3) -> None:
        self.root = Path(root)
        self.compression_level = compression_level

    def write_chunk(self, records: Iterable[ArchiveRecord]) -> ArchiveChunkRef:
        batch = tuple(records)
        if not batch:
            raise ValueError("cannot write an empty archive chunk")
        _validate_record_batch(batch)

        payload = _encode_records(batch)
        payload_sha = _sha256(payload)
        compressed = zstd.ZstdCompressor(level=self.compression_level).compress(payload)
        file_sha = _sha256(compressed)

        first_seq = min(record.source_seq for record in batch)
        last_seq = max(record.source_seq for record in batch)
        first = batch[0]
        chunks_dir = self._chunks_dir(tenant_id=first.tenant_id, session_id=first.session_id)
        final_path = chunks_dir / _chunk_filename(first.stream, first_seq, last_seq, payload_sha)
        relative_path = _relative(final_path, self.root)

        chunks_dir.mkdir(parents=True, exist_ok=True)
        if final_path.exists():
            existing_sha = _sha256(final_path.read_bytes())
            if existing_sha != file_sha:
                raise ArchiveStoreError(f"archive chunk already exists with different bytes: {relative_path}")
            return ArchiveChunkRef(
                tenant_id=first.tenant_id,
                session_id=first.session_id,
                stream=first.stream,
                first_source_seq=first_seq,
                last_source_seq=last_seq,
                record_count=len(batch),
                uncompressed_bytes=len(payload),
                compressed_bytes=len(compressed),
                payload_sha256=payload_sha,
                file_sha256=file_sha,
                relative_path=relative_path,
                path=final_path,
            )

        tmp_path = final_path.with_name(f"{final_path.name}.{os.getpid()}.tmp")
        try:
            with tmp_path.open("wb") as handle:
                handle.write(compressed)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, final_path)
            _fsync_dir(chunks_dir)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

        return ArchiveChunkRef(
            tenant_id=first.tenant_id,
            session_id=first.session_id,
            stream=first.stream,
            first_source_seq=first_seq,
            last_source_seq=last_seq,
            record_count=len(batch),
            uncompressed_bytes=len(payload),
            compressed_bytes=len(compressed),
            payload_sha256=payload_sha,
            file_sha256=file_sha,
            relative_path=relative_path,
            path=final_path,
        )

    def write_record_chunks(
        self,
        records: Iterable[ArchiveRecord],
        *,
        target_uncompressed_bytes: int,
    ) -> list[ArchiveChunkRef]:
        if target_uncompressed_bytes <= 0:
            raise ValueError("target_uncompressed_bytes must be positive")

        chunks: list[ArchiveChunkRef] = []
        pending: list[ArchiveRecord] = []
        pending_bytes = 0
        for record in records:
            encoded_size = len(_encode_record(record)) + 1
            if pending and pending_bytes + encoded_size > target_uncompressed_bytes:
                chunks.append(self.write_chunk(pending))
                pending = []
                pending_bytes = 0
            pending.append(record)
            pending_bytes += encoded_size
        if pending:
            chunks.append(self.write_chunk(pending))
        return chunks

    def read_chunk(self, relative_path: str) -> tuple[ArchiveRecord, ...]:
        result = self.verify_chunk(relative_path)
        if not result.valid:
            raise ArchiveCorruptionError("; ".join(result.errors))
        return result.records

    def iter_chunk_records(self, relative_path: str) -> Iterator[ArchiveRecord]:
        """Verify then replay a chunk without materializing its full payload."""

        path = self._resolve_relative(relative_path)
        parsed = _parse_chunk_filename(path.name)
        if parsed is None:
            raise ArchiveCorruptionError("invalid chunk filename")
        stream, first_seq, last_seq, expected_payload_sha = parsed
        if not path.exists():
            raise ArchiveCorruptionError("chunk file is missing")

        errors: list[str] = []
        payload_digest = hashlib.sha256()
        observed_first: int | None = None
        observed_last: int | None = None
        record_count = 0
        try:
            for ordinal, line in enumerate(_iter_zstd_lines(path), start=1):
                payload_digest.update(line)
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as exc:
                    errors.append(f"line {ordinal}: malformed JSON: {exc.msg}")
                    continue
                record = _decode_record_obj(obj, ordinal=ordinal, errors=errors)
                if record is None:
                    continue
                if record.stream != stream:
                    errors.append(f"line {ordinal}: stream does not match chunk filename")
                if observed_last is not None and record.source_seq <= observed_last:
                    errors.append("source_seq values must be strictly increasing")
                observed_first = record.source_seq if observed_first is None else observed_first
                observed_last = record.source_seq
                record_count += 1
        except zstd.ZstdError as exc:
            raise ArchiveCorruptionError(f"zstd decompression failed: {exc}") from exc

        if payload_digest.hexdigest() != expected_payload_sha:
            errors.append("payload sha does not match chunk filename")
        if record_count == 0:
            errors.append("chunk contains no records")
        else:
            if observed_first != first_seq:
                errors.append("first source_seq does not match chunk filename")
            if observed_last != last_seq:
                errors.append("last source_seq does not match chunk filename")
        if errors:
            raise ArchiveCorruptionError("; ".join(errors))

        # Decode a second time only after the complete artifact is proven. This
        # keeps memory bounded without allowing a corrupt prefix to be committed.
        for ordinal, line in enumerate(_iter_zstd_lines(path), start=1):
            if not line.strip():
                continue
            decode_errors: list[str] = []
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ArchiveCorruptionError(f"line {ordinal}: malformed JSON: {exc.msg}") from exc
            record = _decode_record_obj(obj, ordinal=ordinal, errors=decode_errors)
            if record is None or decode_errors:
                raise ArchiveCorruptionError("; ".join(decode_errors) or f"line {ordinal}: invalid record")
            yield record

    def list_chunks(
        self,
        *,
        tenant_id: str | None = None,
        session_id: str | None = None,
        stream: str | None = None,
    ) -> list[ArchiveChunkRef]:
        # Recovery/fixture helper: production list paths should use manifest rows
        # rather than filesystem scans and decompression.
        if tenant_id and session_id:
            base = self._chunks_dir(tenant_id=tenant_id, session_id=session_id)
        elif tenant_id:
            base = self.root / "tenants" / _safe_component(tenant_id) / "sessions"
        elif session_id:
            base = self.root / "tenants"
        else:
            base = self.root / "tenants"
        if not base.exists():
            return []
        refs: list[ArchiveChunkRef] = []
        for path in sorted(base.rglob(f"*{ARCHIVE_CHUNK_SUFFIX}")):
            if _is_in_orphans(path, self.root):
                continue
            parsed = _parse_chunk_filename(path.name)
            if parsed is None:
                continue
            path_stream, _first_seq, _last_seq, _payload_hash = parsed
            if stream is not None and path_stream != stream:
                continue
            result = self.verify_chunk(_relative(path, self.root))
            if result.valid and result.chunk is not None:
                if tenant_id is not None and result.chunk.tenant_id != tenant_id:
                    continue
                if session_id is not None and result.chunk.session_id != session_id:
                    continue
                refs.append(result.chunk)
        return refs

    def verify_chunk(
        self,
        relative_path: str,
        *,
        expected_file_sha256: str | None = None,
        expected_payload_sha256: str | None = None,
    ) -> ArchiveVerifyResult:
        path = self._resolve_relative(relative_path)
        errors: list[str] = []
        parsed = _parse_chunk_filename(path.name)
        if parsed is None:
            return ArchiveVerifyResult(valid=False, chunk=None, errors=("invalid chunk filename",))
        stream, first_seq, last_seq, expected_payload_sha = parsed
        if not path.exists():
            return ArchiveVerifyResult(valid=False, chunk=None, errors=("chunk file is missing",))

        compressed = path.read_bytes()
        file_sha = _sha256(compressed)
        if expected_file_sha256 is not None and file_sha != expected_file_sha256:
            errors.append("file sha does not match manifest")
        try:
            payload = zstd.ZstdDecompressor().decompress(compressed)
        except Exception as exc:
            return ArchiveVerifyResult(valid=False, chunk=None, errors=(f"zstd decompression failed: {exc}",))

        payload_sha = _sha256(payload)
        if expected_payload_sha256 is not None and payload_sha != expected_payload_sha256:
            errors.append("payload sha does not match manifest")
        if payload_sha != expected_payload_sha:
            errors.append("payload sha does not match chunk filename")

        records: list[ArchiveRecord] = []
        for ordinal, line in enumerate(payload.splitlines(), start=1):
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"line {ordinal}: malformed JSON: {exc.msg}")
                continue
            record = _decode_record_obj(obj, ordinal=ordinal, errors=errors)
            if record is None:
                continue
            if record.stream != stream:
                errors.append(f"line {ordinal}: stream does not match chunk filename")
            records.append(record)

        if records:
            tenant_id = records[0].tenant_id
            session_id = records[0].session_id
            for previous, current in zip(records, records[1:], strict=False):
                if current.source_seq <= previous.source_seq:
                    errors.append("source_seq values must be strictly increasing")
                    break
            if min(record.source_seq for record in records) != first_seq:
                errors.append("first source_seq does not match chunk filename")
            if max(record.source_seq for record in records) != last_seq:
                errors.append("last source_seq does not match chunk filename")
        else:
            tenant_id = ""
            session_id = _session_id_from_path(path, self.root) or ""
            errors.append("chunk contains no records")

        chunk = ArchiveChunkRef(
            tenant_id=tenant_id,
            session_id=session_id,
            stream=stream,
            first_source_seq=first_seq,
            last_source_seq=last_seq,
            record_count=len(records),
            uncompressed_bytes=len(payload),
            compressed_bytes=len(compressed),
            payload_sha256=payload_sha,
            file_sha256=file_sha,
            relative_path=_relative(path, self.root),
            path=path,
        )
        return ArchiveVerifyResult(valid=not errors, chunk=chunk, errors=tuple(errors), records=tuple(records))

    def recover_orphans(
        self,
        *,
        known_chunk_paths: Collection[str] | None = None,
        min_temp_age_seconds: float = 300,
    ) -> ArchiveRecoveryResult:
        self.root.mkdir(parents=True, exist_ok=True)
        orphan_root = self.root / "orphans" / "tmp"
        moved: list[str] = []
        now = time.time()
        for tmp_path in sorted(self.root.rglob("*.tmp")):
            if _is_in_orphans(tmp_path, self.root):
                continue
            try:
                age_seconds = now - tmp_path.stat().st_mtime
            except OSError:
                continue
            if age_seconds < min_temp_age_seconds:
                continue
            relative = _relative(tmp_path, self.root)
            target = orphan_root / _safe_component(relative.replace("/", "__"))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(tmp_path), target)
            moved.append(relative)

        known = {_normalize_relative(path) for path in known_chunk_paths} if known_chunk_paths is not None else None
        untracked: list[str] = []
        if known is not None:
            for chunk_path in sorted(self.root.rglob(f"*{ARCHIVE_CHUNK_SUFFIX}")):
                if _is_in_orphans(chunk_path, self.root):
                    continue
                relative = _relative(chunk_path, self.root)
                if relative not in known:
                    untracked.append(relative)
        return ArchiveRecoveryResult(moved_temp_files=tuple(moved), untracked_chunks=tuple(untracked))

    def _chunks_dir(self, *, tenant_id: str, session_id: str) -> Path:
        return self.root / "tenants" / _safe_component(tenant_id) / "sessions" / _safe_component(session_id) / "chunks"

    def _resolve_relative(self, relative_path: str) -> Path:
        normalized = _normalize_relative(relative_path)
        path = (self.root / normalized).resolve()
        root = self.root.resolve()
        if root != path and root not in path.parents:
            raise ArchiveStoreError(f"archive path escapes root: {relative_path}")
        return path


def _validate_record_batch(records: tuple[ArchiveRecord, ...]) -> None:
    tenant_id = records[0].tenant_id
    session_id = records[0].session_id
    stream = records[0].stream
    last_source_seq: int | None = None
    for record in records:
        if not record.tenant_id:
            raise ValueError("archive chunk records must have tenant_id")
        if not record.session_id:
            raise ValueError("archive chunk records must have session_id")
        if not record.stream:
            raise ValueError("archive chunk records must have stream")
        if record.tenant_id != tenant_id:
            raise ValueError("archive chunk records must share tenant_id")
        if record.session_id != session_id:
            raise ValueError("archive chunk records must share session_id")
        if record.stream != stream:
            raise ValueError("archive chunk records must share stream")
        if record.source_seq < 0:
            raise ValueError("source_seq must be non-negative")
        if last_source_seq is not None and record.source_seq <= last_source_seq:
            raise ValueError("archive chunk source_seq values must be strictly increasing")
        last_source_seq = record.source_seq


def _record_obj(record: ArchiveRecord) -> dict[str, Any]:
    raw_sha = _sha256(record.raw_bytes)
    return {
        "v": ARCHIVE_CHUNK_VERSION,
        "tenant_id": record.tenant_id,
        "session_id": record.session_id,
        "stream": record.stream,
        "source_seq": record.source_seq,
        "legacy_ref": dict(record.legacy_ref) if record.legacy_ref is not None else None,
        "provider": record.provider,
        "source_path": record.source_path,
        "source_offset": record.source_offset,
        "received_at": record.received_at,
        "raw_sha256": raw_sha,
        "raw_b64": base64.b64encode(record.raw_bytes).decode("ascii"),
    }


def _encode_record(record: ArchiveRecord) -> bytes:
    return json.dumps(_record_obj(record), sort_keys=True, separators=(",", ":")).encode("utf-8")


def _encode_records(records: Iterable[ArchiveRecord]) -> bytes:
    return b"".join(_encode_record(record) + b"\n" for record in records)


def _iter_zstd_lines(path: Path) -> Iterator[bytes]:
    with path.open("rb") as compressed:
        with zstd.ZstdDecompressor().stream_reader(compressed) as reader:
            with io.BufferedReader(reader) as buffered:
                while line := buffered.readline():
                    yield line


def _decode_record_obj(obj: object, *, ordinal: int, errors: list[str]) -> ArchiveRecord | None:
    if not isinstance(obj, dict):
        errors.append(f"line {ordinal}: record is not an object")
        return None
    if obj.get("v") != ARCHIVE_CHUNK_VERSION:
        errors.append(f"line {ordinal}: unsupported archive record version")
        return None
    try:
        raw = base64.b64decode(str(obj["raw_b64"]).encode("ascii"), validate=True)
    except Exception:
        errors.append(f"line {ordinal}: invalid raw_b64")
        return None
    raw_sha = str(obj.get("raw_sha256") or "")
    if _sha256(raw) != raw_sha:
        errors.append(f"line {ordinal}: raw_sha256 mismatch")

    try:
        source_seq = int(obj["source_seq"])
    except Exception:
        errors.append(f"line {ordinal}: invalid source_seq")
        return None

    tenant_id = str(obj.get("tenant_id") or "")
    session_id = str(obj.get("session_id") or "")
    stream = str(obj.get("stream") or "")
    if not tenant_id:
        errors.append(f"line {ordinal}: missing tenant_id")
    if not session_id:
        errors.append(f"line {ordinal}: missing session_id")
    if not stream:
        errors.append(f"line {ordinal}: missing stream")

    legacy_ref = obj.get("legacy_ref")
    if legacy_ref is not None and not isinstance(legacy_ref, dict):
        errors.append(f"line {ordinal}: legacy_ref must be an object or null")
        legacy_ref = None

    return ArchiveRecord(
        tenant_id=tenant_id,
        session_id=session_id,
        stream=stream,
        source_seq=source_seq,
        raw_bytes=raw,
        legacy_ref=legacy_ref,
        provider=_optional_str(obj.get("provider")),
        source_path=_optional_str(obj.get("source_path")),
        source_offset=_optional_int(obj.get("source_offset")),
        received_at=_optional_str(obj.get("received_at")),
    )


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def _chunk_filename(stream: str, first_seq: int, last_seq: int, payload_sha: str) -> str:
    return f"{_safe_component(stream)}-{first_seq:012d}-{last_seq:012d}-{payload_sha}{ARCHIVE_CHUNK_SUFFIX}"


def _parse_chunk_filename(name: str) -> tuple[str, int, int, str] | None:
    if not name.endswith(ARCHIVE_CHUNK_SUFFIX):
        return None
    stem = name[: -len(ARCHIVE_CHUNK_SUFFIX)]
    parts = stem.rsplit("-", 3)
    if len(parts) != 4:
        return None
    stream, first, last, payload_hash = parts
    try:
        return stream, int(first), int(last), payload_hash
    except ValueError:
        return None


def _safe_component(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in str(value)).strip("._")
    return cleaned or "unknown"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _normalize_relative(path: str) -> str:
    return Path(path).as_posix().lstrip("/")


def _fsync_dir(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _is_in_orphans(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to((root / "orphans").resolve())
        return True
    except ValueError:
        return False


def _session_id_from_path(path: Path, root: Path) -> str | None:
    try:
        parts = path.resolve().relative_to(root.resolve()).parts
    except ValueError:
        return None
    if len(parts) >= 5 and parts[0] == "tenants" and parts[2] == "sessions":
        return parts[3]
    return None
