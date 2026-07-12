"""Resumable conversion of the frozen legacy SQLite corpus into storage-v2."""

from __future__ import annotations

import asyncio
import ctypes
import functools
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any
from typing import Protocol
from uuid import NAMESPACE_URL
from uuid import UUID
from uuid import uuid4
from uuid import uuid5

from sqlalchemy import and_
from sqlalchemy import or_
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from zerg.data_plane import create_archive_store
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentSessionBranch
from zerg.models.agents import AgentSourceLine
from zerg.models.agents import ArchiveChunk
from zerg.models.agents import MediaObject
from zerg.models.agents import SessionMediaRef
from zerg.services.archive_store import FilesystemArchiveStore
from zerg.services.archive_transcript import archive_owning_session_ids
from zerg.services.archive_transcript import load_session_source_line_bytes
from zerg.services.media_store import absolute_media_path
from zerg.services.raw_json_compression import decode_raw_json
from zerg.storage_v2.media_objects import MediaObjectError
from zerg.storage_v2.media_objects import MediaObjectSpec
from zerg.storage_v2.media_objects import seal_media_object
from zerg.storage_v2.raw_objects import MAX_RECORD_BYTES
from zerg.storage_v2.raw_objects import RawObjectSpec
from zerg.storage_v2.raw_objects import RawRecord
from zerg.storage_v2.raw_objects import seal_raw_object
from zerg.storage_v2.render_objects import RenderObjectSpec
from zerg.storage_v2.render_objects import RenderObjectValidationError
from zerg.storage_v2.render_objects import RenderRecord
from zerg.storage_v2.render_objects import seal_render_object

PARSER_REVISION = "legacy-normalized-v1"
ORDERING_REVISION = "semantic-order-v2"
MIGRATION_LAYOUT_REVISION = "bounded-v2"
INVENTORY_BATCH = 500
STREAMING_SOURCE_THRESHOLD = 10_000
STREAMING_EVENT_PAGE = 50
STREAMING_MATCH_PAGE = 250
STREAMING_EVENT_LAYOUT_REVISION = "bounded-events-v2"


class CatalogCaller(Protocol):
    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class LegacyHighWatermark:
    session_rowid: int
    source_line_id: int
    event_id: int
    media_ref_id: int

    def encode(self) -> str:
        return json.dumps(
            {"e": self.event_id, "m": self.media_ref_id, "s": self.session_rowid, "l": self.source_line_id},
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def decode(cls, value: str) -> LegacyHighWatermark:
        raw = json.loads(value)
        return cls(
            session_rowid=int(raw["s"]),
            source_line_id=int(raw["l"]),
            event_id=int(raw["e"]),
            media_ref_id=int(raw["m"]),
        )


@dataclass(frozen=True, slots=True)
class InventoryRow:
    session_id: UUID
    source_expected: int
    media_expected: int

    def rpc(self) -> dict[str, object]:
        return {
            "session_id": str(self.session_id),
            "source_expected": self.source_expected,
            "media_expected": self.media_expected,
        }


@dataclass(frozen=True, slots=True)
class MigrationResult:
    session_id: UUID
    source_covered: int
    source_missing: int
    media_covered: int
    media_missing: int
    output_proof_hash: str
    parity_proof_hash: str
    parity_matches: bool
    degradation_code: str | None
    degradation_message: str | None
    envelope_ids: tuple[str, ...]
    media_hashes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _SourceRecord:
    data: bytes
    source_path: str
    source_offset: int
    branch_id: int
    provenance_kind: str
    event: AgentEvent | None = None


@dataclass(frozen=True, slots=True)
class _SourceBatch:
    source_path: str
    provenance_kind: str
    range_start: int
    records: tuple[_SourceRecord, ...]


class _IncrementalProof:
    """Length-delimited deterministic proof without retaining proof inputs."""

    def __init__(self, *domain: str) -> None:
        self._digest = hashlib.sha256()
        for value in domain:
            self.update(value)

    def update(self, value: object) -> None:
        if isinstance(value, tuple):
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        else:
            encoded = str(value).encode("utf-8")
        self._digest.update(len(encoded).to_bytes(8, "big"))
        self._digest.update(encoded)

    def hexdigest(self) -> str:
        return self._digest.hexdigest()


class _EventParityProof:
    """Order-independent bounded multiset proof for normalized event tuples."""

    def __init__(self, session_id: UUID) -> None:
        self._session_id = str(session_id)
        self._count = 0
        self._sum = 0

    def update(self, value: tuple[object, ...]) -> None:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._sum = (self._sum + int.from_bytes(hashlib.sha256(encoded).digest(), "big")) % (1 << 256)
        self._count += 1

    def hexdigest(self) -> str:
        return _proof("events", self._session_id, str(self._count), f"{self._sum:064x}")


def freeze_high_watermark(db: Session) -> LegacyHighWatermark:
    """Capture the exact upper bounds used by every query in a run."""

    values = db.execute(
        text(
            "SELECT "
            "COALESCE((SELECT MAX(rowid) FROM sessions), 0), "
            "COALESCE((SELECT MAX(id) FROM source_lines), 0), "
            "COALESCE((SELECT MAX(id) FROM events), 0), "
            "COALESCE((SELECT MAX(id) FROM session_media_refs), 0)"
        )
    ).one()
    return LegacyHighWatermark(*(int(value) for value in values))


def inventory_rows(db: Session, watermark: LegacyHighWatermark) -> list[InventoryRow]:
    session_ids = [
        UUID(str(value))
        for value in db.execute(
            text("SELECT id FROM sessions WHERE rowid <= :high ORDER BY rowid"),
            {"high": watermark.session_rowid},
        ).scalars()
    ]
    rows: list[InventoryRow] = []
    for session_id in session_ids:
        source_count = int(
            db.execute(
                text("SELECT COUNT(*) FROM source_lines WHERE session_id = :sid AND id <= :high"),
                {"sid": str(session_id), "high": watermark.source_line_id},
            ).scalar_one()
        )
        if source_count == 0:
            source_count = int(
                db.execute(
                    text("SELECT COUNT(*) FROM events WHERE session_id = :sid AND id <= :high"),
                    {"sid": str(session_id), "high": watermark.event_id},
                ).scalar_one()
            )
        media_count = int(
            db.execute(
                text("SELECT COUNT(*) FROM session_media_refs WHERE session_id = :sid AND id <= :high"),
                {"sid": str(session_id), "high": watermark.media_ref_id},
            ).scalar_one()
        )
        rows.append(InventoryRow(session_id=session_id, source_expected=source_count, media_expected=media_count))
    return rows


async def create_inventory_run(
    db: Session,
    catalog: CatalogCaller,
    *,
    run_id: UUID | None = None,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    watermark = freeze_high_watermark(db)
    inventory = inventory_rows(db, watermark)
    run_id = run_id or uuid4()
    created_at = created_at or datetime.now(UTC)
    await catalog.call(
        "migration.run.create.v2",
        {
            "run_id": str(run_id),
            "legacy_high_watermark": watermark.encode(),
            "expected_session_count": len(inventory),
            "created_at": created_at.isoformat(),
        },
        timeout_seconds=5.0,
    )
    for offset in range(0, len(inventory), INVENTORY_BATCH):
        await catalog.call(
            "migration.session.register.batch.v2",
            {
                "run_id": str(run_id),
                "sessions": [row.rpc() for row in inventory[offset : offset + INVENTORY_BATCH]],
                "registered_at": created_at.isoformat(),
            },
            timeout_seconds=5.0,
        )
    return {
        "run_id": str(run_id),
        "legacy_high_watermark": watermark.encode(),
        "expected_session_count": len(inventory),
        "source_expected": sum(row.source_expected for row in inventory),
        "media_expected": sum(row.media_expected for row in inventory),
    }


class LegacyCorpusConverter:
    def __init__(
        self,
        *,
        session_factory: sessionmaker,
        catalog: CatalogCaller,
        object_root: Path,
        tenant_id: str,
        archive_store: FilesystemArchiveStore | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.catalog = catalog
        self.object_root = object_root
        self.tenant_id = tenant_id
        self.archive_store = archive_store
        self._cached_owner_id: str | None = None

    async def migrate_run(
        self,
        run_id: UUID,
        *,
        workers: int = 2,
        claim_limit: int = 1,
        worker_prefix: str = "legacy-migration",
    ) -> dict[str, Any]:
        if not 1 <= workers <= 32:
            raise ValueError("workers must be between 1 and 32")
        if claim_limit != 1:
            raise ValueError("migration workers claim exactly one session so leases cannot expire in a local queue")
        run = await self.catalog.call("migration.run.read.v2", {"run_id": str(run_id)}, timeout_seconds=5.0)
        watermark = LegacyHighWatermark.decode(str(run["run"]["legacy_high_watermark"]))

        async def worker(index: int) -> None:
            worker_id = f"{worker_prefix}-{index}"
            while True:
                claim_token = uuid4()
                claim = await self.catalog.call(
                    "migration.session.claim.v2",
                    {
                        "run_id": str(run_id),
                        "worker_id": worker_id,
                        "claim_token": str(claim_token),
                        "now": datetime.now(UTC).isoformat(),
                        "lease_seconds": 3600,
                        "limit": claim_limit,
                    },
                    timeout_seconds=5.0,
                )
                claimed = claim.get("claimed") or []
                if not claimed:
                    return
                for row in claimed:
                    try:
                        with self.session_factory() as db:
                            result = await self.convert_session(
                                db,
                                UUID(str(row["session_id"])),
                                watermark,
                                replace_existing_epochs=int(row["attempts"]) > 1,
                            )
                        await self._complete(run_id, claim_token, result)
                    except Exception as exc:
                        failed_at = datetime.now(UTC)
                        await self.catalog.call(
                            "migration.session.fail.v2",
                            {
                                "run_id": str(run_id),
                                "session_id": str(row["session_id"]),
                                "claim_token": str(claim_token),
                                "error_code": type(exc).__name__[:64],
                                "error_message": str(exc)[:2048] or None,
                                "failed_at": failed_at.isoformat(),
                                "retry_at": (failed_at + timedelta(minutes=5)).isoformat(),
                            },
                            timeout_seconds=5.0,
                        )

        await asyncio.gather(*(worker(index) for index in range(workers)))
        return await self.catalog.call("migration.run.summary.v2", {"run_id": str(run_id)}, timeout_seconds=5.0)

    async def convert_session(
        self,
        db: Session,
        session_id: UUID,
        watermark: LegacyHighWatermark,
        *,
        replace_existing_epochs: bool = False,
    ) -> MigrationResult:
        session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        if self._should_stream_archived_session(db, session_id, watermark):
            return await self._convert_streaming_archived(
                db,
                session,
                watermark,
                replace_existing_epochs=replace_existing_epochs,
            )
        events = (
            db.query(AgentEvent)
            .filter(AgentEvent.session_id == session_id, AgentEvent.id <= watermark.event_id)
            .order_by(AgentEvent.timestamp.asc(), AgentEvent.id.asc())
            .all()
        )
        sources, source_covered, source_missing = self._load_sources(db, session_id, watermark, events)
        event_groups = _events_by_source(events)
        source_keys = {(record.source_path, record.source_offset) for record in sources}
        synthetic_path = f"legacy-unmatched-events:{session_id}"
        for event in events:
            event_key = None
            if event.source_path is not None and event.source_offset is not None:
                event_key = (event.source_path, int(event.source_offset))
            if event_key is not None and event_key in source_keys:
                continue
            normalized = _normalized_event_source((event,))
            if normalized is None:
                raise ValueError(f"legacy event {event.id} cannot be preserved as bounded derived evidence")
            normalized_bytes = normalized.encode("utf-8")
            if len(normalized_bytes) <= MAX_RECORD_BYTES:
                synthetic_key = (synthetic_path, int(event.id))
                sources.append(
                    _SourceRecord(
                        data=normalized_bytes,
                        source_path=synthetic_path,
                        source_offset=int(event.id),
                        branch_id=int(event.branch_id or 0),
                        provenance_kind="legacy_normalized_event",
                        event=event,
                    )
                )
                event_groups[synthetic_key] = [event]
                continue
            chunk_path = f"{synthetic_path}:{event.id}"
            for chunk_index, start in enumerate(range(0, len(normalized_bytes), MAX_RECORD_BYTES)):
                sources.append(
                    _SourceRecord(
                        data=normalized_bytes[start : start + MAX_RECORD_BYTES],
                        source_path=chunk_path,
                        source_offset=chunk_index,
                        branch_id=int(event.branch_id or 0),
                        provenance_kind="legacy_normalized_event",
                        event=event if chunk_index == 0 else None,
                    )
                )
            event_groups[(chunk_path, 0)] = [event]
        batches = _source_batches(sources, event_groups)
        if not batches and not events and source_missing == 0:
            batches = [_SourceBatch("empty", "legacy_source_lines", 0, ())]
        generation = _stable_uuid("render", str(session_id), watermark.encode())
        envelope_ids: list[str] = []
        output_parts: list[str] = []
        head_branch_id = (
            db.query(AgentSessionBranch.id)
            .filter(
                AgentSessionBranch.session_id == session_id,
                AgentSessionBranch.is_head == 1,
            )
            .scalar()
        )
        consumed_event_ids: set[int] = set()
        rendered_records: list[RenderRecord] = []
        render_failures: list[str] = []
        epoch_plans: dict[tuple[str, str], tuple[UUID, UUID | None]] = {}
        owner_id = await self._active_owner_id() if batches else None

        for batch in batches:
            source_path = batch.source_path
            opaque_id = _opaque_source_id(session_id, source_path, batch.provenance_kind)
            epoch_key = (source_path, batch.provenance_kind)
            if epoch_key not in epoch_plans:
                original_epoch = _legacy_source_epoch(session_id, source_path, batch.provenance_kind, watermark)
                source_epoch, predecessor_source_epoch = original_epoch, None
                if replace_existing_epochs:
                    manifest = await self.catalog.call(
                        "storage.source_epoch.manifest.v2",
                        {"source_epoch": str(original_epoch), "after_position": None, "limit": 1},
                        timeout_seconds=5.0,
                    )
                    if manifest.get("found") is True:
                        source_epoch = _stable_uuid(
                            "source-replacement",
                            MIGRATION_LAYOUT_REVISION,
                            str(session_id),
                            source_path,
                            batch.provenance_kind,
                            watermark.encode(),
                        )
                        predecessor_source_epoch = original_epoch
                epoch_plans[epoch_key] = source_epoch, predecessor_source_epoch
            source_epoch, predecessor_source_epoch = epoch_plans[epoch_key]
            raw_records = _raw_records(batch)
            raw_spec = RawObjectSpec(
                tenant_id=self.tenant_id,
                machine_id=session.device_id or "legacy",
                session_id=session_id,
                provider=session.provider,
                opaque_source_id=opaque_id,
                source_epoch=source_epoch,
                range_kind="record_ordinal",
                range_start=batch.range_start,
                range_end=batch.range_start + len(batch.records),
                records=raw_records,
                provenance_kind=batch.provenance_kind,
            )
            sealed_raw = await asyncio.to_thread(seal_raw_object, self.object_root, raw_spec)
            render_records: list[RenderRecord] = []
            for index, item in enumerate(batch.records):
                position = batch.range_start + index
                for event in event_groups.get((item.source_path, item.source_offset), ()):
                    if event.id in consumed_event_ids:
                        continue
                    rendered = _render_record(event, position, index, session_id, head_branch_id=head_branch_id)
                    render_records.append(rendered)
                    consumed_event_ids.add(int(event.id))
            render_records.sort(key=_render_order_key)
            render_spec = RenderObjectSpec(
                session_id=session_id,
                render_generation=generation,
                parser_revision=PARSER_REVISION,
                ordering_revision=ORDERING_REVISION,
                machine_id=session.device_id or "legacy",
                provider=session.provider,
                opaque_source_id=opaque_id,
                source_epoch=source_epoch,
                source_envelope_id=sealed_raw.envelope_id,
                records=tuple(render_records),
            )
            try:
                sealed_render = await asyncio.to_thread(seal_render_object, self.object_root, render_spec)
            except RenderObjectValidationError as exc:
                sealed_render = None
                render_failures.append(str(exc))
            else:
                rendered_records.extend(render_records)
            committed = await self.catalog.call(
                "storage.raw_object.commit.v2",
                _raw_commit(
                    session=session,
                    raw_spec=raw_spec,
                    sealed_raw=sealed_raw,
                    render_spec=render_spec,
                    sealed_render=sealed_render,
                    tenant_id=self.tenant_id,
                    owner_id=owner_id,
                    predecessor_source_epoch=predecessor_source_epoch,
                ),
                timeout_seconds=10.0,
            )
            envelope_ids.append(sealed_raw.envelope_id)
            committed_seq = str(committed["receipt"]["commit_seq"])
            output_parts.extend((sealed_raw.object_hash, committed_seq))
            if sealed_render is not None:
                output_parts.append(sealed_render.object_hash)

        media_covered, media_missing, media_hashes = await self._migrate_media(db, session_id, watermark)
        output_proof = _proof("output", str(session_id), *sorted(output_parts), *sorted(media_hashes))
        expected_tuples = [_event_tuple(event, session_id, head_branch_id=head_branch_id) for event in events]
        expected_event_hash = _event_parity_hash(expected_tuples)
        rendered_event_hash = _event_parity_hash([_render_tuple(record) for record in rendered_records])
        parity_matches = expected_event_hash == rendered_event_hash
        parity_proof = _proof("parity", str(session_id), expected_event_hash, rendered_event_hash)
        return MigrationResult(
            session_id=session_id,
            source_covered=source_covered,
            source_missing=source_missing,
            media_covered=media_covered,
            media_missing=media_missing,
            output_proof_hash=output_proof,
            parity_proof_hash=parity_proof,
            parity_matches=parity_matches,
            degradation_code="render_projection_failed" if render_failures else None,
            degradation_message="; ".join(sorted(set(render_failures)))[:2048] if render_failures else None,
            envelope_ids=tuple(envelope_ids),
            media_hashes=tuple(sorted(media_hashes)),
        )

    def _should_stream_archived_session(
        self,
        db: Session,
        session_id: UUID,
        watermark: LegacyHighWatermark,
    ) -> bool:
        counts = db.execute(
            text(
                "SELECT COUNT(*), "
                "SUM(CASE WHEN raw_json_z IS NOT NULL OR COALESCE(raw_json, '') <> '' THEN 1 ELSE 0 END) "
                "FROM source_lines WHERE session_id = :session_id AND id <= :high"
            ),
            {"session_id": str(session_id), "high": watermark.source_line_id},
        ).one()
        source_count = int(counts[0] or 0)
        rows_with_inline_raw = int(counts[1] or 0)
        return source_count >= STREAMING_SOURCE_THRESHOLD and rows_with_inline_raw == 0

    async def _convert_streaming_archived(
        self,
        db: Session,
        session: AgentSession,
        watermark: LegacyHighWatermark,
        *,
        replace_existing_epochs: bool,
    ) -> MigrationResult:
        """Convert an all-slim giant session one archive/event page at a time."""

        session_id = UUID(str(session.id))
        owner_id = await self._active_owner_id()
        generation = _stable_uuid("render", str(session_id), watermark.encode())
        head_branch_id = _head_branch_id(db, session_id)
        output_proof = _IncrementalProof("output", str(session_id))
        expected_events = _EventParityProof(session_id)
        rendered_events = _EventParityProof(session_id)
        render_failures: list[str] = []

        source_covered = await self._stream_archived_source_lines(
            db,
            session,
            watermark,
            generation=generation,
            owner_id=owner_id,
            output_proof=output_proof,
            replace_existing_epochs=replace_existing_epochs,
        )
        source_expected = int(
            db.execute(
                text("SELECT COUNT(*) FROM source_lines WHERE session_id = :session_id AND id <= :high"),
                {"session_id": str(session_id), "high": watermark.source_line_id},
            ).scalar_one()
        )
        source_missing = max(0, source_expected - source_covered)
        await self._stream_normalized_events(
            db,
            session,
            watermark,
            generation=generation,
            owner_id=owner_id,
            head_branch_id=head_branch_id,
            output_proof=output_proof,
            expected_events=expected_events,
            rendered_events=rendered_events,
            render_failures=render_failures,
            replace_existing_epochs=replace_existing_epochs,
        )

        media_covered, media_missing, media_hashes = await self._migrate_media(db, session_id, watermark)
        for media_hash in sorted(media_hashes):
            output_proof.update(media_hash)
        expected_event_hash = expected_events.hexdigest()
        rendered_event_hash = rendered_events.hexdigest()
        parity_matches = expected_event_hash == rendered_event_hash
        parity_proof = _proof("parity", str(session_id), expected_event_hash, rendered_event_hash)
        return MigrationResult(
            session_id=session_id,
            source_covered=source_covered,
            source_missing=source_missing,
            media_covered=media_covered,
            media_missing=media_missing,
            output_proof_hash=output_proof.hexdigest(),
            parity_proof_hash=parity_proof,
            parity_matches=parity_matches,
            degradation_code="render_projection_failed" if render_failures else None,
            degradation_message="; ".join(sorted(set(render_failures)))[:2048] if render_failures else None,
            envelope_ids=(),
            media_hashes=tuple(sorted(media_hashes)),
        )

    async def _stream_archived_source_lines(
        self,
        db: Session,
        session: AgentSession,
        watermark: LegacyHighWatermark,
        *,
        generation: UUID,
        owner_id: str,
        output_proof: _IncrementalProof,
        replace_existing_epochs: bool,
    ) -> int:
        session_id = UUID(str(session.id))
        archive_store = self.archive_store or create_archive_store()
        connection = db.connection()
        connection.exec_driver_sql("PRAGMA temp_store=FILE")
        connection.exec_driver_sql(
            "CREATE TEMP TABLE IF NOT EXISTS migration_source_counts ("
            "source_path TEXT NOT NULL, source_offset INTEGER NOT NULL, line_hash TEXT NOT NULL, "
            "row_count INTEGER NOT NULL, consumed INTEGER NOT NULL DEFAULT 0, "
            "PRIMARY KEY(source_path, source_offset, line_hash)) WITHOUT ROWID"
        )
        connection.exec_driver_sql("DELETE FROM migration_source_counts")
        connection.execute(
            text(
                "INSERT INTO migration_source_counts(source_path, source_offset, line_hash, row_count) "
                "SELECT source_path, source_offset, line_hash, COUNT(*) FROM source_lines "
                "WHERE session_id = :session_id AND id <= :high GROUP BY source_path, source_offset, line_hash"
            ),
            {"session_id": str(session_id), "high": watermark.source_line_id},
        )
        connection.exec_driver_sql(
            "CREATE TEMP TABLE IF NOT EXISTS migration_archive_page ("
            "source_path TEXT NOT NULL, source_offset INTEGER NOT NULL, line_hash TEXT NOT NULL, "
            "PRIMARY KEY(source_path, source_offset, line_hash)) WITHOUT ROWID"
        )
        owner_ids = [UUID(value) for value in archive_owning_session_ids(db, session_id)]
        manifests = (
            db.query(ArchiveChunk.relative_path)
            .filter(
                ArchiveChunk.session_id.in_(owner_ids),
                ArchiveChunk.stream == "source_lines",
                ArchiveChunk.state == "sealed",
            )
            .order_by(ArchiveChunk.first_source_seq.asc(), ArchiveChunk.id.asc())
            .yield_per(4)
        )
        covered = 0
        source_plans: dict[str, tuple[UUID, UUID | None, int]] = {}
        for (relative_path,) in manifests:
            records = archive_store.read_chunk(relative_path)
            archive_by_key: dict[tuple[str, int, str], object] = {}
            for record in records:
                if record.source_path is None or record.source_offset is None:
                    continue
                raw_hash = hashlib.sha256(record.raw_bytes).hexdigest()
                archive_by_key.setdefault((record.source_path, int(record.source_offset), raw_hash), record)
            keys = list(archive_by_key)
            connection.exec_driver_sql("DELETE FROM migration_archive_page")
            for start in range(0, len(keys), STREAMING_MATCH_PAGE):
                page = keys[start : start + STREAMING_MATCH_PAGE]
                connection.exec_driver_sql(
                    "INSERT OR IGNORE INTO migration_archive_page VALUES (?, ?, ?)",
                    page,
                )
            matched = connection.execute(
                text(
                    "SELECT c.source_path, c.source_offset, c.line_hash, c.row_count "
                    "FROM migration_archive_page p JOIN migration_source_counts c "
                    "USING(source_path, source_offset, line_hash) WHERE c.consumed = 0 "
                    "ORDER BY c.source_path, c.source_offset, c.line_hash"
                ),
            ).all()
            connection.exec_driver_sql(
                "UPDATE migration_source_counts SET consumed = 1 WHERE consumed = 0 AND EXISTS ("
                "SELECT 1 FROM migration_archive_page p WHERE p.source_path = migration_source_counts.source_path "
                "AND p.source_offset = migration_source_counts.source_offset "
                "AND p.line_hash = migration_source_counts.line_hash)"
            )
            matched_records: list[_SourceRecord] = []
            for path, offset, line_hash, row_count in matched:
                record = archive_by_key[(str(path), int(offset), str(line_hash))]
                covered += int(row_count)
                matched_records.append(
                    _SourceRecord(
                        data=record.raw_bytes,
                        source_path=str(path),
                        source_offset=int(offset),
                        branch_id=0,
                        provenance_kind="legacy_source_lines",
                    )
                )
            groups: dict[str, list[_SourceRecord]] = defaultdict(list)
            for record in matched_records:
                groups[record.source_path].append(record)
            for source_path, source_records in sorted(groups.items()):
                batches = _source_batches(
                    source_records,
                    {},
                )
                plan = source_plans.get(source_path)
                if plan is None:
                    epoch, predecessor = await self._stream_epoch_plan(
                        session_id,
                        source_path,
                        "legacy_source_lines",
                        watermark,
                        replace_existing_epochs=replace_existing_epochs,
                    )
                    plan = (epoch, predecessor, 0)
                epoch, predecessor, next_ordinal = plan
                for batch in batches:
                    adjusted = _SourceBatch(
                        source_path=source_path,
                        provenance_kind=batch.provenance_kind,
                        range_start=next_ordinal,
                        records=batch.records,
                    )
                    next_ordinal += len(batch.records)
                    await self._commit_stream_batch(
                        session,
                        adjusted,
                        generation=generation,
                        source_epoch=epoch,
                        predecessor_source_epoch=predecessor,
                        owner_id=owner_id,
                        render_records=(),
                        output_proof=output_proof,
                    )
                source_plans[source_path] = (epoch, predecessor, next_ordinal)
            del records, archive_by_key, keys, matched, matched_records, groups
        connection.exec_driver_sql("DROP TABLE migration_archive_page")
        connection.exec_driver_sql("DROP TABLE migration_source_counts")
        return covered

    async def _stream_normalized_events(
        self,
        db: Session,
        session: AgentSession,
        watermark: LegacyHighWatermark,
        *,
        generation: UUID,
        owner_id: str,
        head_branch_id: int | None,
        output_proof: _IncrementalProof,
        expected_events: _EventParityProof,
        rendered_events: _EventParityProof,
        render_failures: list[str],
        replace_existing_epochs: bool,
    ) -> None:
        session_id = UUID(str(session.id))
        source_path = f"legacy-unmatched-events:{session_id}"
        epoch_plans: dict[str, tuple[UUID, UUID | None, int]] = {}
        last_timestamp: datetime | None = None
        last_id = 0
        while True:
            query = db.query(AgentEvent).filter(
                AgentEvent.session_id == session_id,
                AgentEvent.id <= watermark.event_id,
            )
            if last_timestamp is not None:
                query = query.filter(
                    or_(
                        AgentEvent.timestamp > last_timestamp,
                        and_(AgentEvent.timestamp == last_timestamp, AgentEvent.id > last_id),
                    )
                )
            events = query.order_by(AgentEvent.timestamp.asc(), AgentEvent.id.asc()).limit(STREAMING_EVENT_PAGE).all()
            if not events:
                break
            source_records: list[_SourceRecord] = []
            event_groups: dict[tuple[str, int], list[AgentEvent]] = {}
            for event in events:
                expected_events.update(_event_tuple(event, session_id, head_branch_id=head_branch_id))
                normalized = _normalized_event_source((event,))
                if normalized is None:
                    render_failures.append(f"legacy event {event.id} cannot be normalized")
                    continue
                encoded = normalized.encode("utf-8")
                event_path = source_path
                for chunk_index, start in enumerate(range(0, len(encoded), MAX_RECORD_BYTES)):
                    chunk_path = event_path if len(encoded) <= MAX_RECORD_BYTES else f"{event_path}:event={event.id}"
                    source_records.append(
                        _SourceRecord(
                            data=encoded[start : start + MAX_RECORD_BYTES],
                            source_path=chunk_path,
                            source_offset=int(event.id) if chunk_index == 0 else chunk_index,
                            branch_id=int(event.branch_id or 0),
                            provenance_kind="legacy_normalized_event",
                            event=event if chunk_index == 0 else None,
                        )
                    )
                    if chunk_index == 0:
                        event_groups[(chunk_path, int(event.id))] = [event]
            for batch in _source_batches(source_records, event_groups):
                plan = epoch_plans.get(batch.source_path)
                if plan is None:
                    epoch, predecessor = await self._stream_epoch_plan(
                        session_id,
                        batch.source_path,
                        "legacy_normalized_event",
                        watermark,
                        replace_existing_epochs=replace_existing_epochs,
                        layout_revision=STREAMING_EVENT_LAYOUT_REVISION,
                    )
                    plan = (epoch, predecessor, 0)
                epoch, predecessor, range_start = plan
                adjusted = _SourceBatch(
                    source_path=batch.source_path,
                    provenance_kind=batch.provenance_kind,
                    range_start=range_start,
                    records=batch.records,
                )
                epoch_plans[batch.source_path] = (epoch, predecessor, range_start + len(batch.records))
                render_records = tuple(
                    _render_record(
                        event,
                        adjusted.range_start + index,
                        index,
                        session_id,
                        head_branch_id=head_branch_id,
                    )
                    for index, record in enumerate(adjusted.records)
                    for event in event_groups.get((record.source_path, record.source_offset), ())
                )
                sealed_render = await self._commit_stream_batch(
                    session,
                    adjusted,
                    generation=generation,
                    source_epoch=epoch,
                    predecessor_source_epoch=predecessor,
                    owner_id=owner_id,
                    render_records=render_records,
                    output_proof=output_proof,
                    render_failures=render_failures,
                )
                if sealed_render:
                    for record in sorted(render_records, key=_render_order_key):
                        rendered_events.update(_render_tuple(record))
            last_timestamp = events[-1].timestamp
            last_id = int(events[-1].id)
            for event in events:
                db.expunge(event)
            # This migration can serialize multiple GiB over hundreds of
            # thousands of small pages. Drop every page-owned reference before
            # asking glibc to return its now-free zstd/JSON arenas to the host;
            # otherwise RSS follows total bytes processed instead of page size.
            events.clear()
            source_records.clear()
            event_groups.clear()
            query = event = normalized = encoded = record = batch = adjusted = render_records = None
            _return_free_heap_to_os()

    async def _stream_epoch_plan(
        self,
        session_id: UUID,
        source_path: str,
        provenance_kind: str,
        watermark: LegacyHighWatermark,
        *,
        replace_existing_epochs: bool,
        layout_revision: str = MIGRATION_LAYOUT_REVISION,
    ) -> tuple[UUID, UUID | None]:
        original = _legacy_source_epoch(session_id, source_path, provenance_kind, watermark)
        if not replace_existing_epochs:
            return original, None
        manifest = await self.catalog.call(
            "storage.source_epoch.manifest.v2",
            {"source_epoch": str(original), "after_position": None, "limit": 1},
            timeout_seconds=5.0,
        )
        if manifest.get("found") is not True:
            return original, None
        current = original
        current_facts = manifest.get("source_epoch")
        if not isinstance(current_facts, dict):
            raise RuntimeError("source epoch manifest is missing epoch facts")
        for _ in range(8):
            replacement_value = current_facts.get("replaced_by_source_epoch")
            if current_facts.get("state") == "open" or replacement_value is None:
                break
            current = UUID(str(replacement_value))
            current_manifest = await self.catalog.call(
                "storage.source_epoch.manifest.v2",
                {"source_epoch": str(current), "after_position": None, "limit": 1},
                timeout_seconds=5.0,
            )
            current_facts = current_manifest.get("source_epoch")
            if current_manifest.get("found") is not True or not isinstance(current_facts, dict):
                raise RuntimeError("replacement source epoch manifest is unavailable")
        if current_facts.get("state") != "open":
            raise RuntimeError("source epoch replacement chain has no open head")
        replacement = _stable_uuid(
            "source-replacement",
            layout_revision,
            str(session_id),
            source_path,
            provenance_kind,
            watermark.encode(),
        )
        if current == replacement:
            predecessor = current_facts.get("predecessor_source_epoch")
            return current, UUID(str(predecessor)) if predecessor is not None else None
        return replacement, current

    async def _commit_stream_batch(
        self,
        session: AgentSession,
        batch: _SourceBatch,
        *,
        generation: UUID,
        source_epoch: UUID,
        predecessor_source_epoch: UUID | None,
        owner_id: str,
        render_records: tuple[RenderRecord, ...],
        output_proof: _IncrementalProof,
        render_failures: list[str] | None = None,
    ) -> bool:
        opaque_id = _opaque_source_id(UUID(str(session.id)), batch.source_path, batch.provenance_kind)
        raw_spec = RawObjectSpec(
            tenant_id=self.tenant_id,
            machine_id=session.device_id or "legacy",
            session_id=UUID(str(session.id)),
            provider=session.provider,
            opaque_source_id=opaque_id,
            source_epoch=source_epoch,
            range_kind="record_ordinal",
            range_start=batch.range_start,
            range_end=batch.range_start + len(batch.records),
            records=_raw_records(batch),
            provenance_kind=batch.provenance_kind,
        )
        sealed_raw = await asyncio.to_thread(seal_raw_object, self.object_root, raw_spec)
        render_spec = RenderObjectSpec(
            session_id=UUID(str(session.id)),
            render_generation=generation,
            parser_revision=PARSER_REVISION,
            ordering_revision=ORDERING_REVISION,
            machine_id=session.device_id or "legacy",
            provider=session.provider,
            opaque_source_id=opaque_id,
            source_epoch=source_epoch,
            source_envelope_id=sealed_raw.envelope_id,
            records=tuple(sorted(render_records, key=_render_order_key)),
        )
        try:
            sealed_render = await asyncio.to_thread(seal_render_object, self.object_root, render_spec)
        except RenderObjectValidationError as exc:
            sealed_render = None
            if render_failures is not None:
                render_failures.append(str(exc))
        committed = await self.catalog.call(
            "storage.raw_object.commit.v2",
            _raw_commit(
                session=session,
                raw_spec=raw_spec,
                sealed_raw=sealed_raw,
                render_spec=render_spec,
                sealed_render=sealed_render,
                tenant_id=self.tenant_id,
                owner_id=owner_id,
                predecessor_source_epoch=predecessor_source_epoch,
            ),
            timeout_seconds=10.0,
        )
        output_proof.update(sealed_raw.object_hash)
        output_proof.update(str(committed["receipt"]["commit_seq"]))
        if sealed_render is not None:
            output_proof.update(sealed_render.object_hash)
        return sealed_render is not None

    def _load_sources(
        self,
        db: Session,
        session_id: UUID,
        watermark: LegacyHighWatermark,
        events: list[AgentEvent],
    ) -> tuple[list[_SourceRecord], int, int]:
        rows = (
            db.query(AgentSourceLine)
            .filter(AgentSourceLine.session_id == session_id, AgentSourceLine.id <= watermark.source_line_id)
            .order_by(
                AgentSourceLine.source_path.asc(),
                AgentSourceLine.branch_id.asc(),
                AgentSourceLine.source_offset.asc(),
                AgentSourceLine.revision.asc(),
                AgentSourceLine.id.asc(),
            )
            .all()
        )
        if rows:
            archive: dict[tuple[str, int, str], str] = {}
            if any(decode_raw_json(row) is None for row in rows):
                archive = load_session_source_line_bytes(db, session_id, archive_store=self.archive_store)
            # A slim source-line row may still have an event copy. The source
            # line is authoritative when present; event raw is only its bounded
            # recovery fallback after archive lookup failed.
            events_by_key: dict[tuple[str, int], list[AgentEvent]] = defaultdict(list)
            for event in sorted(events, key=lambda item: item.id):
                if event.source_path is not None and event.source_offset is not None:
                    events_by_key[(event.source_path, int(event.source_offset))].append(event)
            records = []
            physical_keys: set[tuple[str, int, str]] = set()
            covered = 0
            missing = 0
            for row in rows:
                value = decode_raw_json(row)
                provenance_kind = "legacy_source_lines"
                validated_raw_hash: str | None = None
                if value is None:
                    value = archive.get((row.source_path, int(row.source_offset), row.line_hash))
                if value is not None:
                    data = value.encode("utf-8")
                    validated_raw_hash = hashlib.sha256(data).hexdigest()
                    if len(data) > MAX_RECORD_BYTES or validated_raw_hash != row.line_hash:
                        value = None
                        validated_raw_hash = None
                matching_events = events_by_key.get((row.source_path, int(row.source_offset)), ())
                if value is None:
                    value = next(
                        (
                            raw
                            for event in matching_events
                            if (raw := decode_raw_json(event)) is not None
                            and len(raw.encode("utf-8")) <= MAX_RECORD_BYTES
                            and hashlib.sha256(raw.encode("utf-8")).hexdigest() == row.line_hash
                        ),
                        None,
                    )
                    provenance_kind = "legacy_fallback"
                if value is None:
                    value = _normalized_event_source(matching_events)
                    provenance_kind = "legacy_normalized_event"
                    missing += 1
                if value is None:
                    continue
                data = value.encode("utf-8")
                if len(data) > MAX_RECORD_BYTES:
                    continue
                raw_hash = validated_raw_hash or hashlib.sha256(data).hexdigest()
                if provenance_kind != "legacy_normalized_event":
                    if raw_hash != row.line_hash:
                        derived = _normalized_event_source(matching_events)
                        if derived is None or len(derived.encode("utf-8")) > MAX_RECORD_BYTES:
                            missing += 1
                            continue
                        data = derived.encode("utf-8")
                        raw_hash = hashlib.sha256(data).hexdigest()
                        provenance_kind = "legacy_normalized_event"
                        missing += 1
                    else:
                        covered += 1
                physical_key = (row.source_path, int(row.source_offset), raw_hash)
                if physical_key not in physical_keys:
                    physical_keys.add(physical_key)
                    records.append(
                        _SourceRecord(
                            data=data,
                            source_path=row.source_path,
                            source_offset=int(row.source_offset),
                            branch_id=int(row.branch_id),
                            provenance_kind=provenance_kind,
                        )
                    )
            return records, covered, missing

        records = []
        physical_keys: set[tuple[str, int, str]] = set()
        covered = 0
        missing = 0
        for event in events:
            value = decode_raw_json(event)
            provenance_kind = "legacy_fallback"
            if value is None or len(value.encode("utf-8")) > MAX_RECORD_BYTES:
                missing += 1
                value = _normalized_event_source((event,))
                provenance_kind = "legacy_normalized_event"
            if value is None or len(value.encode("utf-8")) > MAX_RECORD_BYTES:
                continue
            data = value.encode("utf-8")
            source_path = event.source_path or f"legacy-event:{event.id}"
            source_offset = int(event.source_offset if event.source_offset is not None else event.id)
            physical_key = (source_path, source_offset, hashlib.sha256(data).hexdigest())
            if provenance_kind != "legacy_normalized_event":
                covered += 1
            if physical_key not in physical_keys:
                physical_keys.add(physical_key)
                records.append(
                    _SourceRecord(
                        data=data,
                        source_path=source_path,
                        source_offset=source_offset,
                        branch_id=int(event.branch_id or 0),
                        provenance_kind=provenance_kind,
                        event=event,
                    )
                )
        return records, covered, missing

    async def _active_owner_id(self) -> str:
        if self._cached_owner_id is not None:
            return self._cached_owner_id
        result = await self.catalog.call("auth.owner.get.v2", {}, timeout_seconds=5.0)
        if result.get("found") is not True or result.get("owner_id") is None:
            raise RuntimeError("storage-v2 migration requires an active catalog owner")
        self._cached_owner_id = str(result["owner_id"])
        return self._cached_owner_id

    async def _migrate_media(
        self,
        db: Session,
        session_id: UUID,
        watermark: LegacyHighWatermark,
    ) -> tuple[int, int, list[str]]:
        rows = (
            db.query(SessionMediaRef, MediaObject)
            .outerjoin(MediaObject, MediaObject.sha256 == SessionMediaRef.media_sha256)
            .filter(SessionMediaRef.session_id == session_id, SessionMediaRef.id <= watermark.media_ref_id)
            .order_by(SessionMediaRef.id.asc())
            .all()
        )
        covered = 0
        missing = 0
        hashes: list[str] = []
        for ref, media in rows:
            if media is None:
                missing += 1
                continue
            try:
                data = absolute_media_path(media).read_bytes()
                sealed = await asyncio.to_thread(
                    seal_media_object,
                    self.object_root,
                    MediaObjectSpec(media_hash=media.sha256, mime_type=media.mime_type, data=data),
                )
                session_ref = {
                    "session_id": str(session_id),
                    "envelope_id": None,
                    "ref_key": f"legacy-ref:{ref.id}",
                }
                await self.catalog.call(
                    "storage.media.commit.v2",
                    {
                        "media_hash": sealed.media_hash,
                        "state": "present",
                        "mime_type": sealed.mime_type,
                        "byte_size": sealed.byte_size,
                        "object_path": sealed.object_path,
                        "session_refs": [session_ref],
                        "observed_at": datetime.now(UTC).isoformat(),
                    },
                    timeout_seconds=10.0,
                )
            except (OSError, ValueError, MediaObjectError):
                missing += 1
                continue
            covered += 1
            hashes.append(sealed.media_hash)
        return covered, missing, hashes

    async def _complete(self, run_id: UUID, claim_token: UUID, result: MigrationResult) -> None:
        if not result.parity_matches:
            if result.degradation_code is not None:
                await self._complete_with_degradation(run_id, claim_token, result)
                return
            failed_at = datetime.now(UTC)
            await self.catalog.call(
                "migration.session.fail.v2",
                {
                    "run_id": str(run_id),
                    "session_id": str(result.session_id),
                    "claim_token": str(claim_token),
                    "error_code": "parity_mismatch",
                    "error_message": "normalized legacy events differ from sealed render records",
                    "failed_at": failed_at.isoformat(),
                    "retry_at": (failed_at + timedelta(minutes=5)).isoformat(),
                },
                timeout_seconds=5.0,
            )
            return
        await self._complete_with_degradation(run_id, claim_token, result)

    async def _complete_with_degradation(self, run_id: UUID, claim_token: UUID, result: MigrationResult) -> None:
        await self.catalog.call(
            "migration.session.complete.v2",
            {
                "run_id": str(run_id),
                "session_id": str(result.session_id),
                "claim_token": str(claim_token),
                "source_covered": result.source_covered,
                "source_missing": result.source_missing,
                "media_covered": result.media_covered,
                "media_missing": result.media_missing,
                "output_proof_hash": result.output_proof_hash,
                "parity_proof_hash": result.parity_proof_hash,
                "degradation_code": result.degradation_code,
                "degradation_message": result.degradation_message,
                "completed_at": datetime.now(UTC).isoformat(),
            },
            timeout_seconds=5.0,
        )


def _source_batches(
    records: list[_SourceRecord],
    event_groups: dict[tuple[str, int], list[AgentEvent]],
) -> list[_SourceBatch]:
    grouped: dict[tuple[str, str], list[_SourceRecord]] = defaultdict(list)
    for record in records:
        grouped[(record.source_path, record.provenance_kind)].append(record)
    batches: list[_SourceBatch] = []
    for (source_path, provenance_kind), source_records in grouped.items():
        current: list[_SourceRecord] = []
        size = 0
        render_events = 0
        render_bytes = 0
        range_start = 0
        for record in source_records:
            related_events = event_groups.get((record.source_path, record.source_offset), ())
            related_render_bytes = sum(_estimated_render_bytes(event) for event in related_events)
            # Leave room for normalized render records. The hard storage
            # contract remains 10k/4MiB; these smaller batches are an operator
            # fairness bound and preserve contiguous ordinals per source epoch.
            if current and (
                len(current) >= 1_000
                or size + len(record.data) > 1024 * 1024
                or render_events + len(related_events) > 2_000
                or render_bytes + related_render_bytes > 1024 * 1024
            ):
                batches.append(_SourceBatch(source_path, provenance_kind, range_start, tuple(current)))
                range_start += len(current)
                current = []
                size = 0
                render_events = 0
                render_bytes = 0
            current.append(record)
            size += len(record.data)
            render_events += len(related_events)
            render_bytes += related_render_bytes
        if current:
            batches.append(_SourceBatch(source_path, provenance_kind, range_start, tuple(current)))
    return batches


def _head_branch_id(db: Session, session_id: UUID) -> int | None:
    query = db.query(AgentSessionBranch.id)
    query = query.filter(AgentSessionBranch.session_id == session_id, AgentSessionBranch.is_head == 1)
    return query.scalar()


def _estimated_render_bytes(event: AgentEvent) -> int:
    """Conservative bound used only to keep legacy render batches small."""

    total = 512
    for value in (
        event.content_text,
        event.tool_name,
        event.tool_output_text,
        event.tool_call_id,
        str(event.thread_id) if event.thread_id is not None else None,
    ):
        if value is not None:
            total += len(str(value).encode("utf-8"))
    if event.tool_input_json is not None:
        encoded_tool_input = json.dumps(
            event.tool_input_json,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        total += len(encoded_tool_input)
    return total


@functools.cache
def _malloc_trim_function() -> Any | None:
    """Resolve glibc's optional heap-release hook once per converter process."""

    try:
        trim = ctypes.CDLL(None).malloc_trim
    except (AttributeError, OSError):
        return None
    trim.argtypes = [ctypes.c_size_t]
    trim.restype = ctypes.c_int
    return trim


def _return_free_heap_to_os() -> None:
    trim = _malloc_trim_function()
    if trim is not None:
        trim(0)


def _events_by_source(events: list[AgentEvent]) -> dict[tuple[str, int], list[AgentEvent]]:
    grouped: dict[tuple[str, int], list[AgentEvent]] = defaultdict(list)
    for event in events:
        if event.source_path is not None and event.source_offset is not None:
            grouped[(event.source_path, int(event.source_offset))].append(event)
    return grouped


def _render_record(
    event: AgentEvent,
    source_position: int,
    raw_record_ordinal: int,
    session_id: UUID,
    *,
    head_branch_id: int | None,
) -> RenderRecord:
    timestamp = event.timestamp
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    thread_id = str(event.thread_id or _stable_uuid("thread", str(session_id), str(event.branch_id or 0)))
    return RenderRecord(
        event_id=f"legacy:{event.id}",
        order_time_us=int(timestamp.timestamp() * 1_000_000),
        source_position=source_position,
        event_subordinal=int(event.id) % (1 << 32),
        role=event.role,
        content_text=event.content_text,
        tool_name=event.tool_name,
        tool_input_json=event.tool_input_json,
        tool_output_text=event.tool_output_text,
        tool_call_id=event.tool_call_id,
        thread_id=thread_id,
        branch_kind="head" if head_branch_id is None or event.branch_id in {None, head_branch_id} else "abandoned",
        raw_record_ordinal=raw_record_ordinal,
    )


def _raw_commit(
    *,
    session,
    raw_spec,
    sealed_raw,
    render_spec,
    sealed_render,
    tenant_id: str,
    owner_id: str,
    predecessor_source_epoch: UUID | None,
) -> dict[str, Any]:
    render_manifest = None
    if sealed_render is not None:
        render_manifest = {
            "generation_id": str(render_spec.render_generation),
            "parser_revision": render_spec.parser_revision,
            "ordering_revision": render_spec.ordering_revision,
            "object_id": sealed_render.object_id,
            "object_hash": sealed_render.object_hash,
            "payload_hash": sealed_render.payload_hash,
            "object_path": sealed_render.object_path,
            "uncompressed_size": sealed_render.uncompressed_size,
            "compressed_size": sealed_render.compressed_size,
            "event_count": sealed_render.event_count,
            "first_order_key": sealed_render.first_order_key,
            "last_order_key": sealed_render.last_order_key,
            "user_messages": sealed_render.user_messages,
            "assistant_messages": sealed_render.assistant_messages,
            "tool_calls": sealed_render.tool_calls,
            "first_user_message_preview": sealed_render.first_user_message_preview,
            "last_visible_text_preview": sealed_render.last_visible_text_preview,
        }
    return {
        "protocol_version": 2,
        "tenant_id": tenant_id,
        "owner_id": owner_id,
        "session_id": str(session.id),
        "machine_id": raw_spec.machine_id,
        "provider": raw_spec.provider,
        "opaque_source_id": raw_spec.opaque_source_id,
        "source_epoch": str(raw_spec.source_epoch),
        "predecessor_source_epoch": str(predecessor_source_epoch) if predecessor_source_epoch is not None else None,
        "epoch_opened_at": _aware(session.started_at).isoformat(),
        "range_kind": raw_spec.range_kind,
        "range_start": raw_spec.range_start,
        "range_end": raw_spec.range_end,
        "record_hashes": list(sealed_raw.record_hashes),
        "envelope_id": sealed_raw.envelope_id,
        "object_hash": sealed_raw.object_hash,
        "payload_hash": sealed_raw.payload_hash,
        "compressed_hash": sealed_raw.compressed_hash,
        "object_path": sealed_raw.object_path,
        "uncompressed_size": sealed_raw.uncompressed_size,
        "compressed_size": sealed_raw.compressed_size,
        "provenance_kind": raw_spec.provenance_kind,
        "render_state": "ready" if sealed_render is not None else "failed",
        "media_refs": [],
        "projectors": ["search-v2"],
        "render_manifest": render_manifest,
        "session_facts": {
            "environment": session.environment,
            "project": _optional_text(session.project),
            "cwd": _optional_text(session.cwd),
            "git_repo": _optional_text(session.git_repo),
            "git_branch": _optional_text(session.git_branch),
            "started_at": _aware(session.started_at).isoformat(),
            "last_activity_at": _aware(session.last_activity_at or session.started_at).isoformat(),
            "ended_at": _aware(session.ended_at).isoformat() if session.ended_at else None,
            "origin_kind": _optional_text(session.origin_kind),
            "hidden_from_default_timeline": bool(session.hidden_from_default_timeline),
            "launch_actor": _optional_text(session.launch_actor),
            "launch_surface": _optional_text(session.launch_surface),
        },
        "sealed_at": datetime.now(UTC).isoformat(),
    }


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _optional_text(value: object) -> str | None:
    normalized = str(value).strip() if value is not None else ""
    return normalized or None


def _normalized_event_source(events: tuple[AgentEvent, ...] | list[AgentEvent]) -> str | None:
    if not events:
        return None
    payload = {
        "schema": "legacy_normalized_event.v1",
        "events": [
            {
                "branch_id": event.branch_id,
                "content_text": event.content_text,
                "event_id": event.id,
                "role": event.role,
                "source_offset": event.source_offset,
                "source_path": event.source_path,
                "thread_id": str(event.thread_id) if event.thread_id is not None else None,
                "timestamp": _aware(event.timestamp).isoformat(),
                "tool_call_id": event.tool_call_id,
                "tool_input_json": event.tool_input_json,
                "tool_name": event.tool_name,
                "tool_output_text": event.tool_output_text,
            }
            for event in events
        ],
    }
    try:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError):
        return None


def _event_tuple(event: AgentEvent, session_id: UUID, *, head_branch_id: int | None) -> tuple[object, ...]:
    timestamp = _aware(event.timestamp)
    thread_id = str(event.thread_id or _stable_uuid("thread", str(session_id), str(event.branch_id or 0)))
    branch_kind = "head" if head_branch_id is None or event.branch_id in {None, head_branch_id} else "abandoned"
    return (
        f"legacy:{event.id}",
        int(timestamp.timestamp() * 1_000_000),
        event.role,
        event.content_text,
        event.tool_name,
        _canonical_json(event.tool_input_json),
        event.tool_output_text,
        event.tool_call_id,
        thread_id,
        branch_kind,
    )


def _render_tuple(record: RenderRecord) -> tuple[object, ...]:
    return (
        record.event_id,
        record.order_time_us,
        record.role,
        record.content_text,
        record.tool_name,
        _canonical_json(record.tool_input_json),
        record.tool_output_text,
        record.tool_call_id,
        record.thread_id,
        record.branch_kind,
    )


def _canonical_json(value: object) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _event_parity_hash(rows: list[tuple[object, ...]]) -> str:
    ordered = sorted(rows, key=lambda row: (int(row[1]), str(row[0])))
    encoded = json.dumps(ordered, ensure_ascii=False, separators=(",", ":"), sort_keys=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _render_order_key(row: RenderRecord) -> tuple[int, int, int, str]:
    return (row.order_time_us, row.source_position, row.event_subordinal, row.event_id)


def _raw_records(batch: _SourceBatch) -> tuple[RawRecord, ...]:
    records: list[RawRecord] = []
    for index, record in enumerate(batch.records):
        records.append(RawRecord(source_position=batch.range_start + index, data=record.data))
    return tuple(records)


def _legacy_source_epoch(
    session_id: UUID,
    source_path: str,
    provenance_kind: str,
    watermark: LegacyHighWatermark,
) -> UUID:
    return _stable_uuid("source", str(session_id), source_path, provenance_kind, watermark.encode())


def _opaque_source_id(session_id: UUID, source_path: str, provenance: str) -> str:
    digest = hashlib.sha256(source_path.encode()).hexdigest()
    return f"legacy:{provenance}:{session_id}:{digest}"


def _stable_uuid(*parts: str) -> UUID:
    return uuid5(NAMESPACE_URL, "longhouse-storage-v2:" + "\x1f".join(parts))


def _proof(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


__all__ = [
    "LegacyCorpusConverter",
    "LegacyHighWatermark",
    "MigrationResult",
    "create_inventory_run",
    "freeze_high_watermark",
    "inventory_rows",
]
