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
) -> dict[tuple[str, int], str]:
    """Return ``{(source_path, source_offset): raw_json}`` from archive chunks.

    Reads every sealed ``source_lines`` chunk for the session and decodes each
    record's exact bytes. When the same (path, offset) appears in more than one
    chunk (idempotent re-archival), the highest ``source_seq`` wins so the result
    is deterministic. Unreadable chunks are skipped with a warning rather than
    failing the whole reconstruction.
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

    best_seq: dict[tuple[str, int], int] = {}
    out: dict[tuple[str, int], str] = {}
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
            key = (record.source_path, int(record.source_offset))
            if key in best_seq and best_seq[key] >= record.source_seq:
                continue
            best_seq[key] = record.source_seq
            out[key] = record.raw_bytes.decode("utf-8")
    return out
