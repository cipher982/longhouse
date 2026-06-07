"""Row-level events-stream archive coverage verifier for raw reclaim.

Before events.raw_json_z can be dropped from the monolith, every durable event
row that still carries raw bytes must have a byte-identical record in the
filesystem archive's ``events`` stream.

Identity is the raw-byte sha256: live archive-primary event records use a
hash-derived source_seq while legacy-exported records use the rowid, and their
legacy_ref shapes differ — but both carry the exact raw bytes, so matching on
sha256(raw_bytes) is the shape-independent byte-identity guarantee. This mirrors
the source_lines verifier (which matches on line_hash) for the events stream.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from dataclasses import field
from uuid import UUID

from sqlalchemy.orm import Session

from zerg.data_plane import create_archive_store
from zerg.models.agents import AgentEvent
from zerg.models.agents import ArchiveChunk
from zerg.services.archive_store import FilesystemArchiveStore
from zerg.services.provisional_events import durable_transcript_event_predicate
from zerg.services.raw_json_compression import decode_raw_json


@dataclass
class SessionEventReclaimVerification:
    session_id: str
    rows_with_raw: int = 0
    rows_covered: int = 0
    missing: list[int] = field(default_factory=list)  # event ids lacking archive coverage

    @property
    def fully_covered(self) -> bool:
        return self.rows_with_raw == self.rows_covered and not self.missing


def _archived_event_byte_hashes(
    db: Session,
    session_id: UUID | str,
    *,
    archive_store: FilesystemArchiveStore,
) -> set[str]:
    """Return sha256(raw_bytes) for every sealed events-stream archive record."""
    chunks = (
        db.query(ArchiveChunk)
        .filter(ArchiveChunk.session_id == UUID(str(session_id)))
        .filter(ArchiveChunk.stream == "events")
        .filter(ArchiveChunk.state == "sealed")
        .order_by(ArchiveChunk.first_source_seq.asc())
        .all()
    )
    hashes: set[str] = set()
    for chunk in chunks:
        for record in archive_store.read_chunk(chunk.relative_path):
            hashes.add(hashlib.sha256(record.raw_bytes).hexdigest())
    return hashes


def verify_session_event_archive_coverage(
    db: Session,
    session_id: UUID | str,
    *,
    archive_store: FilesystemArchiveStore | None = None,
) -> SessionEventReclaimVerification:
    """Verify every durable event row carrying raw bytes is archive-covered.

    A row is "covered" when an events-stream archive record exists whose raw
    bytes sha256 to the same value as the row's stored raw. Rows that carry no
    raw (already reclaimed or never had any) are not counted — there is nothing
    to lose for them.
    """
    result = SessionEventReclaimVerification(session_id=str(session_id))
    store = archive_store or create_archive_store()
    archived = _archived_event_byte_hashes(db, session_id, archive_store=store)

    rows = db.query(AgentEvent).filter(AgentEvent.session_id == UUID(str(session_id))).filter(durable_transcript_event_predicate()).all()
    for row in rows:
        raw = decode_raw_json(row)
        if not raw:
            continue  # no raw bytes to protect
        result.rows_with_raw += 1
        if hashlib.sha256(raw.encode("utf-8")).hexdigest() in archived:
            result.rows_covered += 1
        else:
            result.missing.append(int(row.id))
    return result
