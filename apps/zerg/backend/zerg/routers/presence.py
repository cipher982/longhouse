"""Session presence ingest endpoint.

Receives real-time state signals from Claude Code hooks:
  - UserPromptSubmit → state=thinking
  - PreToolUse       → state=running (tool_name set)
  - PostToolUse      → state=thinking
  - Stop             → state=idle

One row per session_id, upserted on each call. Stale rows (>10 min) are
treated as gone by the active sessions endpoint.

Authentication: same X-Agents-Token / device token as ingest.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from datetime import timezone
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request
from fastapi import Response
from fastapi import status
from pydantic import BaseModel
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionPresence
from zerg.routers.agents import verify_agents_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])

VALID_STATES = {"thinking", "running", "idle"}


class PresenceIn(BaseModel):
    """Payload from a Claude Code hook."""

    session_id: str
    state: str  # thinking | running | idle
    tool_name: Optional[str] = None
    cwd: Optional[str] = None
    provider: Optional[str] = "claude"


@router.post("/presence", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def upsert_presence(
    payload: PresenceIn,
    request: Request,
    db: Session = Depends(get_db),
    _token: object = Depends(verify_agents_token),
) -> Response:
    """Upsert real-time presence state for a session."""
    if payload.state not in VALID_STATES:
        # Silently ignore unknown states rather than erroring hooks
        return

    project: Optional[str] = None
    if payload.cwd:
        project = os.path.basename(payload.cwd.rstrip("/"))

    now = datetime.now(timezone.utc)

    stmt = (
        sqlite_insert(SessionPresence)
        .values(
            session_id=payload.session_id,
            state=payload.state,
            tool_name=payload.tool_name if payload.state == "running" else None,
            cwd=payload.cwd,
            project=project,
            provider=payload.provider or "claude",
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=["session_id"],
            set_={
                "state": payload.state,
                "tool_name": payload.tool_name if payload.state == "running" else None,
                "cwd": payload.cwd,
                "project": project,
                "updated_at": now,
            },
        )
    )
    db.execute(stmt)

    # Auto-resume snoozed sessions when they emit a new active signal.
    # A snoozed session signaling again means the user is back — show it.
    if payload.state in ("thinking", "running"):
        try:
            from uuid import UUID

            session_uuid = UUID(payload.session_id)
            db.query(AgentSession).filter(
                AgentSession.id == session_uuid,
                AgentSession.user_state == "snoozed",
            ).update(
                {"user_state": "active", "user_state_at": now},
                synchronize_session=False,
            )
        except (ValueError, AttributeError):
            pass  # session_id not a valid UUID — skip silently

    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
