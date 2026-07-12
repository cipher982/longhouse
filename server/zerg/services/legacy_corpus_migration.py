"""Resumable conversion of the frozen legacy SQLite corpus into storage-v2."""

from __future__ import annotations

import asyncio
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

from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentSessionBranch
from zerg.models.agents import AgentSourceLine
from zerg.models.agents import MediaObject
from zerg.models.agents import SessionMediaRef
from zerg.services.archive_store import FilesystemArchiveStore
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
from zerg.storage_v2.render_objects import RenderRecord
from zerg.storage_v2.render_objects import seal_render_object

PARSER_REVISION = "legacy-normalized-v1"
ORDERING_REVISION = "semantic-order-v2"
INVENTORY_BATCH = 500


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
                            result = await self.convert_session(db, UUID(str(row["session_id"])), watermark)
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
    ) -> MigrationResult:
        session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        events = (
            db.query(AgentEvent)
            .filter(AgentEvent.session_id == session_id, AgentEvent.id <= watermark.event_id)
            .order_by(AgentEvent.timestamp.asc(), AgentEvent.id.asc())
            .all()
        )
        sources, source_covered, source_missing = self._load_sources(db, session_id, watermark, events)
        event_groups = _events_by_source(events)
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
        owner_id = await self._active_owner_id() if batches else None

        for batch_index, batch in enumerate(batches):
            source_path = batch.source_path
            opaque_id = _opaque_source_id(session_id, source_path, batch.provenance_kind)
            source_epoch = _legacy_source_epoch(session_id, source_path, batch.provenance_kind, watermark)
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
            batch_position: dict[tuple[str, int], int] = {}
            for index, item in enumerate(batch.records):
                position = batch.range_start + index
                batch_position.setdefault((item.source_path, item.source_offset), position)
                for event in event_groups.get((item.source_path, item.source_offset), ()):
                    if event.id in consumed_event_ids:
                        continue
                    rendered = _render_record(event, position, index, session_id, head_branch_id=head_branch_id)
                    render_records.append(rendered)
                    consumed_event_ids.add(int(event.id))
            if batch_index == len(batches) - 1:
                if batch.records:
                    fallback_position = batch.range_start
                    for event in events:
                        if event.id not in consumed_event_ids:
                            render_records.append(
                                _render_record(
                                    event,
                                    fallback_position,
                                    0,
                                    session_id,
                                    head_branch_id=head_branch_id,
                                )
                            )
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
            sealed_render = await asyncio.to_thread(seal_render_object, self.object_root, render_spec)
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
                ),
                timeout_seconds=10.0,
            )
            envelope_ids.append(sealed_raw.envelope_id)
            committed_seq = str(committed["receipt"]["commit_seq"])
            output_parts.extend((sealed_raw.object_hash, sealed_render.object_hash, committed_seq))

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
            envelope_ids=tuple(envelope_ids),
            media_hashes=tuple(sorted(media_hashes)),
        )

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


def _raw_commit(*, session, raw_spec, sealed_raw, render_spec, sealed_render, tenant_id: str, owner_id: str) -> dict[str, Any]:  # noqa: E501
    return {
        "protocol_version": 2,
        "tenant_id": tenant_id,
        "owner_id": owner_id,
        "session_id": str(session.id),
        "machine_id": raw_spec.machine_id,
        "provider": raw_spec.provider,
        "opaque_source_id": raw_spec.opaque_source_id,
        "source_epoch": str(raw_spec.source_epoch),
        "predecessor_source_epoch": None,
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
        "render_state": "ready",
        "media_refs": [],
        "projectors": ["search-v2"],
        "render_manifest": {
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
        },
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
