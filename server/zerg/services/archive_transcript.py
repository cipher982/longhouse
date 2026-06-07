"""Archive-backed raw transcript reconstruction.

Builds a ``(source_path, source_offset) -> raw bytes`` map for a session from
its sealed ``source_lines`` archive chunks. This lets transcript export / resume
reconstruct the exact provider JSONL after raw payloads have moved off the
monolith into the archive — the slim ``source_lines`` index still drives ordering
and branch selection; only the bytes come from here.

Non-destructive read path: callers keep the monolith raw column as the source of
truth until the closeout's reclaim phase actually drops it.
"""

from __future__ import annotations

import hashlib
import logging
from uuid import UUID

from sqlalchemy.orm import Session

from zerg.data_plane import create_archive_store
from zerg.models.agents import ArchiveChunk
from zerg.services.archive_store import FilesystemArchiveStore

logger = logging.getLogger(__name__)


def load_session_source_line_bytes(
    db: Session,
    session_id: UUID | str,
    *,
    archive_store: FilesystemArchiveStore | None = None,
) -> dict[tuple[str, int, str], str]:
    """Return ``{(source_path, source_offset, line_hash): raw_json}`` from archive.

    Keyed by ``line_hash`` (sha256 of the exact raw bytes), NOT by offset alone:
    rewrites and branch forks produce multiple revisions at the same
    ``(source_path, source_offset)``, so the caller must select the row whose
    ``line_hash`` it wants. Hash-derived ``source_seq`` is deliberately not used
    for selection — it carries no temporal/revision meaning.

    Unreadable chunks are skipped with a warning rather than failing the whole
    reconstruction.
    """
    store = archive_store or create_archive_store()
    chunks = (
        db.query(ArchiveChunk)
        .filter(ArchiveChunk.session_id == UUID(str(session_id)))
        .filter(ArchiveChunk.stream == "source_lines")
        .filter(ArchiveChunk.state == "sealed")
        .order_by(ArchiveChunk.first_source_seq.asc())
        .all()
    )

    out: dict[tuple[str, int, str], str] = {}
    for chunk in chunks:
        try:
            records = store.read_chunk(chunk.relative_path)
        except Exception as exc:
            logger.warning(
                "Skipping unreadable source_lines archive chunk %s for session %s: %s",
                chunk.relative_path,
                session_id,
                exc,
                exc_info=True,
            )
            continue
        for record in records:
            if record.source_path is None or record.source_offset is None:
                continue
            line_hash = hashlib.sha256(record.raw_bytes).hexdigest()
            key = (record.source_path, int(record.source_offset), line_hash)
            # Exact-byte identity: any record with this key is byte-identical, so
            # first-writer-wins is safe and deterministic.
            out.setdefault(key, record.raw_bytes.decode("utf-8"))
    return out
