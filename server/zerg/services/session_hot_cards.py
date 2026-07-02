"""Hot timeline-card read model helpers.

Timeline cards are the small read model used by list endpoints.  They mirror
bounded session metadata without reaching into raw transcript tables.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.models.agents import TimelineCard

SESSION_HOT_CARD_PARSER_REVISION = "session-hot-card-v1"


def upsert_timeline_card_from_session(
    db: Session,
    session: AgentSession,
    *,
    parser_revision: str = SESSION_HOT_CARD_PARSER_REVISION,
) -> None:
    """Mirror small AgentSession fields into TimelineCard.

    Call this after mutating mirrored AgentSession fields outside archive
    projectors: provider/environment/project/device/cwd, activity, previews,
    counts, transcript revision, summary title, or projection state.

    The generic session mirror owns only hot-card display fields.  Archive
    projectors own archive metadata and parser revisions, so conflict updates
    intentionally preserve those columns.
    """

    values = {
        "session_id": session.id,
        "provider": session.provider,
        "environment": session.environment,
        "project": session.project,
        "device_id": session.device_id,
        "cwd": session.cwd,
        "started_at": session.started_at,
        "last_activity_at": session.last_activity_at,
        "summary_title": session.summary_title,
        "first_user_message_preview": session.first_user_message_preview,
        "last_visible_text_preview": session.last_visible_text_preview,
        "last_user_message_preview": session.last_user_message_preview,
        "last_assistant_message_preview": session.last_assistant_message_preview,
        "user_messages": int(session.user_messages or 0),
        "assistant_messages": int(session.assistant_messages or 0),
        "tool_calls": int(session.tool_calls or 0),
        "transcript_revision": int(session.transcript_revision or 0),
        "archive_state": "legacy_hot",
        "archive_lag_records": 0,
        "archive_last_source_offset": None,
        "derived_state": "pending" if int(session.needs_projection or 0) else "current",
        "derived_revision": str(session.summary_revision or 0),
        "parser_revision": parser_revision,
    }
    update_values = {
        key: value
        for key, value in values.items()
        if key
        not in {
            "session_id",
            "archive_state",
            "archive_lag_records",
            "archive_last_source_offset",
            "parser_revision",
        }
    }
    update_values["updated_at"] = datetime.now(timezone.utc)
    stmt = sqlite_insert(TimelineCard).values(**values)
    db.execute(stmt.on_conflict_do_update(index_elements=["session_id"], set_=update_values))
