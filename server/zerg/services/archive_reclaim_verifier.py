"""Row-level archive coverage verifier for raw reclaim.

Before any source_lines raw payload can be dropped from the monolith, every
candidate row must have a byte-identical record in the filesystem archive,
matched on ``(session_id, source_path, source_offset, line_hash)``.

This proves byte identity at the ROW level — not session or chunk level — which
is the single most important safety property before reclaim: rewrites and branch
forks put multiple revisions at the same offset, so offset-level checks can pass
while individual lines are wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from uuid import UUID

from sqlalchemy.orm import Session

from zerg.models.agents import AgentSourceLine
from zerg.services.archive_store import FilesystemArchiveStore
from zerg.services.archive_transcript import load_session_source_line_bytes


@dataclass
class SessionReclaimVerification:
    session_id: str
    rows_checked: int = 0
    rows_covered: int = 0
    missing: list[tuple[str, int, str]] = field(default_factory=list)  # (path, offset, line_hash)

    @property
    def fully_covered(self) -> bool:
        return self.rows_checked == self.rows_covered and not self.missing


def verify_session_archive_coverage(
    db: Session,
    session_id: UUID | str,
    *,
    archive_store: FilesystemArchiveStore | None = None,
) -> SessionReclaimVerification:
    """Verify every source_lines row for a session has a matching archive record.

    A row is "covered" when an archive source_lines record exists with the same
    ``(source_path, source_offset, line_hash)``. Because line_hash is the sha256
    of the exact raw bytes, a key match is a byte-identity guarantee.
    """
    result = SessionReclaimVerification(session_id=str(session_id))
    archived = load_session_source_line_bytes(db, session_id, archive_store=archive_store)
    archived_keys = set(archived.keys())

    rows = db.query(AgentSourceLine).filter(AgentSourceLine.session_id == UUID(str(session_id))).all()
    for row in rows:
        result.rows_checked += 1
        key = (row.source_path, int(row.source_offset), row.line_hash)
        if key in archived_keys:
            result.rows_covered += 1
        else:
            result.missing.append(key)
    return result
