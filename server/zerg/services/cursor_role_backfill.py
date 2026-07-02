"""Backfill Cursor user-event roles + <user_query> unwrapping for legacy rows.

One-shot migration helper. New ingest (commit 441f015b4) classifies Cursor
user messages at decode time: Cursor's environment-context injection
(<user_info>/<rules>/<agent_transcripts>/...) is re-roled to ``system`` and
the real user turn is unwrapped from ``<user_query>...</user_query>``. This
applies the same classification to rows that predate that fix, so historical
Cursor sessions stop showing the 59KB context dump as "You" on the timeline.

Operates only on persisted ``content_text`` (and the ``role`` column) — it
does not need the source ``store.db``. ``raw_json`` is left untouched: it
remains Cursor's ground-truth original (``role="user"``) for archive fidelity.

Resumable by id cursor and bounded per call — callers loop until
``scanned == 0``. Idempotent: re-running on already-fixed rows is a no-op
(``system`` rows no longer match the ``role="user"`` filter; unwrapped rows
no longer contain ``<user_query>``).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.services.cursor_transcript import _classify_cursor_user_text


@dataclass(frozen=True)
class CursorRoleBackfillResult:
    scanned: int
    re_roleed: int
    unwrapped: int
    last_id: int | None


def backfill_cursor_user_roles(
    db: Session,
    *,
    after_id: int = 0,
    batch_size: int = 1000,
    dry_run: bool = False,
) -> CursorRoleBackfillResult:
    """Classify and repair one batch of legacy Cursor ``role="user"`` events.

    Scans Cursor-provider user events past ``after_id`` ordered by id. For each,
    applies the same string classification the decoder uses to ``content_text``:

    - Cursor context injection (markers, no ``<user_query>``) -> ``role="system"``.
    - Real user turn wrapped in ``<user_query>`` -> unwrap to inner text.
    - Plain user turn -> unchanged.

    ``raw_json`` is never modified. In ``dry_run`` mode rows are classified and
    counted but not written.
    """
    stmt = (
        select(AgentEvent)
        .join(AgentSession, AgentEvent.session_id == AgentSession.id)
        .where(AgentSession.provider == "cursor")
        .where(AgentEvent.role == "user")
        .where(AgentEvent.id > after_id)
        .order_by(AgentEvent.id.asc())
        .limit(batch_size)
    )
    rows = list(db.execute(stmt).scalars().all())
    if not rows:
        return CursorRoleBackfillResult(scanned=0, re_roleed=0, unwrapped=0, last_id=None)

    re_roleed = 0
    unwrapped = 0
    for event in rows:
        text = event.content_text
        if not text:
            continue
        new_text, new_role = _classify_cursor_user_text(text)
        if new_role != event.role:
            if not dry_run:
                event.role = new_role
            re_roleed += 1
        if new_text != text:
            if not dry_run:
                event.content_text = new_text
            unwrapped += 1

    if not dry_run:
        db.flush()
    return CursorRoleBackfillResult(
        scanned=len(rows),
        re_roleed=re_roleed,
        unwrapped=unwrapped,
        last_id=int(rows[-1].id),
    )
