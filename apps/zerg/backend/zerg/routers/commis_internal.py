"""Internal commis endpoints for hook-driven tool events."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.dependencies.auth import require_internal_call
from zerg.models.models import CommisJob
from zerg.services.event_store import append_run_event

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/internal/commis",
    tags=["internal"],
    dependencies=[Depends(require_internal_call)],
)

_HOOK_EVENT_MAP = {
    "PreToolUse": "commis_tool_started",
    "PostToolUse": "commis_tool_completed",
    "PostToolUseFailure": "commis_tool_failed",
}

_MAX_PAYLOAD_CHARS = 10000
_MAX_PREVIEW_CHARS = 200


class CommisToolEventPayload(BaseModel):
    """Payload for commis tool hook events."""

    job_id: int = Field(..., description="Commis job ID")
    event_type: str = Field(..., description="Hook event name (PreToolUse/PostToolUse/...)")
    timestamp: str | None = Field(None, description="Hook event timestamp")
    session_id: str | None = Field(None, description="Claude session identifier")
    tool_name: str | None = Field(None, description="Tool name")
    tool_input: dict | None = Field(None, description="Tool input payload")
    tool_use_id: str | None = Field(None, description="Tool call identifier")
    tool_response: Any | None = Field(None, description="Tool response payload")
    error: str | None = Field(None, description="Tool error message")


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "... [truncated]"


def _coerce_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=True, default=str)
    except Exception:
        return str(value)


def _pack_value(value: Any, limit: int) -> tuple[Any | None, bool, str]:
    if value is None:
        return None, False, ""

    text = _coerce_text(value)
    preview = _truncate_text(text, _MAX_PREVIEW_CHARS)
    if len(text) <= limit:
        try:
            json.dumps(value, ensure_ascii=True, default=str)
            return value, False, preview
        except Exception:
            return _truncate_text(text, limit), True, preview

    return _truncate_text(text, limit), True, preview


@router.post("/tool_event")
async def record_commis_tool_event(
    payload: CommisToolEventPayload,
    db: Session = Depends(get_db),
):
    """Record a commis tool event from Claude Code hooks."""

    job = db.query(CommisJob).filter(CommisJob.id == payload.job_id).first()
    if not job:
        return {"status": "ignored", "reason": "job not found"}

    if not job.oikos_run_id:
        return {"status": "ignored", "reason": "job has no oikos_run_id"}

    event_type = _HOOK_EVENT_MAP.get(payload.event_type)
    if not event_type:
        return {"status": "ignored", "reason": "unsupported event_type"}

    tool_args, tool_args_truncated, tool_args_preview = _pack_value(payload.tool_input, _MAX_PAYLOAD_CHARS)
    result_value, result_truncated, result_preview = _pack_value(payload.tool_response, _MAX_PAYLOAD_CHARS)

    base_payload: dict[str, Any] = {
        "job_id": job.id,
        "commis_id": job.commis_id,
        "owner_id": job.owner_id,
        "run_id": job.oikos_run_id,
        "trace_id": str(job.trace_id) if job.trace_id else None,
        "tool_name": payload.tool_name or "unknown",
        "tool_call_id": payload.tool_use_id or None,
        "tool_args_preview": tool_args_preview,
        "timestamp": payload.timestamp,
        "session_id": payload.session_id,
    }

    if tool_args is not None:
        base_payload["tool_args"] = tool_args
    if tool_args_truncated:
        base_payload["tool_args_truncated"] = True

    if event_type == "commis_tool_started":
        await append_run_event(
            run_id=job.oikos_run_id,
            event_type=event_type,
            payload=base_payload,
        )
        return {"status": "ok"}

    if event_type == "commis_tool_completed":
        completed_payload = dict(base_payload)
        completed_payload.update(
            {
                "result_preview": result_preview,
                "duration_ms": None,
            }
        )
        if result_value is not None:
            completed_payload["result"] = result_value
        if result_truncated:
            completed_payload["result_truncated"] = True

        await append_run_event(
            run_id=job.oikos_run_id,
            event_type=event_type,
            payload=completed_payload,
        )
        return {"status": "ok"}

    error_text = payload.error or "Unknown error"
    failed_payload = dict(base_payload)
    failed_payload.update(
        {
            "error": _truncate_text(str(error_text), 500),
            "duration_ms": None,
        }
    )

    await append_run_event(
        run_id=job.oikos_run_id,
        event_type=event_type,
        payload=failed_payload,
    )
    return {"status": "ok"}


__all__ = ["router"]
