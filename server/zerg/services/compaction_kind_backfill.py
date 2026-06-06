"""Backfill ``events.compaction_kind`` from existing raw payloads.

One-shot migration helper. New ingest stamps ``compaction_kind`` at write time;
this fills the column for rows that predate it, so active-context projection can
drop its request-time raw read once raw payloads move to the archive.

Resumable by id cursor and bounded per call — the events table can hold millions
of rows, so callers loop until ``scanned == 0``.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from zerg.models.agents import AgentEvent
from zerg.services.agents.compaction import classify_compaction_kind
from zerg.services.raw_json_compression import decode_raw_json


@dataclass(frozen=True)
class CompactionKindBackfillResult:
    scanned: int
    updated: int
    last_id: int | None


def backfill_compaction_kind(db: Session, *, after_id: int = 0, batch_size: int = 1000) -> CompactionKindBackfillResult:
    """Classify one batch of system/summary events past ``after_id``.

    Only ``role`` in (system) and rows still NULL are candidates — those are the
    only ones that can carry a boundary marker, which keeps the scan cheap. The
    raw decode happens here (migration time), never on the request path.
    """
    stmt = (
        select(AgentEvent)
        .where(AgentEvent.id > after_id)
        .where(AgentEvent.compaction_kind.is_(None))
        .where(AgentEvent.role == "system")
        .order_by(AgentEvent.id.asc())
        .limit(batch_size)
    )
    rows = list(db.execute(stmt).scalars().all())
    if not rows:
        return CompactionKindBackfillResult(scanned=0, updated=0, last_id=None)

    updated = 0
    for event in rows:
        kind = classify_compaction_kind(decode_raw_json(event))
        if kind is not None:
            event.compaction_kind = kind
            updated += 1
    db.flush()
    return CompactionKindBackfillResult(scanned=len(rows), updated=updated, last_id=int(rows[-1].id))
