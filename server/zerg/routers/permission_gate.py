"""Claude PreToolUse permission-gate request/decision endpoints.

A managed Claude session runs a PreToolUse hook that blocks on a permission-gated
tool, registers the held request here, and long-polls for a decision. Longhouse
stores the held request as a ``SessionPauseRequest`` (``kind=permission_prompt``,
``can_respond=True``) so the existing pause-request answer surface can resolve it.
The hook then reads the resolved decision and returns ``permissionDecision`` to
Claude. See ``session_chat`` for the answer path and ``session_pause_requests``
for the store.

Authentication mirrors presence ingest: the same ``X-Agents-Token`` / managed-local
hook token, and a managed-local hook token must match the target session.
"""

from __future__ import annotations

import logging
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Optional
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session

from zerg.auth.managed_local_hook_tokens import ManagedLocalHookToken
from zerg.database import get_db
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.models.agents import AgentSession
from zerg.services.session_pause_requests import PENDING_STATUS
from zerg.services.session_pause_requests import REPLY_TRANSPORT_CLAUDE_PULL
from zerg.services.session_pause_requests import make_pause_request_key
from zerg.services.session_pause_requests import upsert_pause_request
from zerg.services.session_runtime import runtime_key_for_session
from zerg.utils.time import UTCBaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])

# Distinct source so answerable permission-gate requests are NOT hidden by the
# legacy claude_hook placeholder filter in session_pause_requests.
PERMISSION_GATE_SOURCE = "claude_permission_gate"
PERMISSION_PROMPT_KIND = "permission_prompt"


class PermissionRequestIn(UTCBaseModel):
    """PreToolUse hook payload registering a held permission request."""

    session_id: str
    tool_use_id: str
    tool_name: Optional[str] = None
    tool_input: Optional[dict[str, Any]] = None
    provider: Optional[str] = "claude"
    occurred_at: Optional[datetime] = None


class PermissionRequestAck(UTCBaseModel):
    pause_request_id: str
    request_key: str
    status: str


class PermissionDecisionOut(UTCBaseModel):
    """Decision the hook returns to Claude, or pending when unresolved."""

    decision: Optional[str] = None  # allow | deny | None (still pending)
    reason: Optional[str] = None
    resolved: bool = False


def _enforce_session_scope(token: object, session_id: str) -> None:
    if isinstance(token, ManagedLocalHookToken) and session_id != token.session_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Managed-local hook token does not match session",
        )


def _coerce_session_uuid(session_id: str) -> UUID:
    try:
        return UUID(session_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid session id: {session_id}",
        ) from exc


@router.post("/permission-requests", response_model=PermissionRequestAck)
async def register_permission_request(
    payload: PermissionRequestIn,
    db: Session = Depends(get_db),
    _token: object = Depends(verify_agents_token),
) -> PermissionRequestAck:
    """Register a held Claude permission request from a PreToolUse hook."""

    _enforce_session_scope(_token, payload.session_id)
    session_uuid = _coerce_session_uuid(payload.session_id)
    if db.query(AgentSession.id).filter(AgentSession.id == session_uuid).first() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="session not found")

    # tool_use_id is the idempotency key: re-registering the same id (a hook
    # network retry) updates the same row, and a genuine re-ask re-pends it.
    # An empty id would collapse unrelated asks onto a shared "unknown" key.
    tool_use_id = (payload.tool_use_id or "").strip()
    if not tool_use_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="tool_use_id is required")

    provider = (payload.provider or "claude").strip() or "claude"
    occurred_at = (payload.occurred_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    runtime_key = runtime_key_for_session(provider, payload.session_id)
    request_key = make_pause_request_key(
        provider=provider,
        runtime_key=runtime_key,
        provider_request_id=tool_use_id,
    )
    tool_name = (payload.tool_name or "").strip() or None

    row, _changed = upsert_pause_request(
        db,
        session_id=session_uuid,
        runtime_key=runtime_key,
        provider=provider,
        request_key=request_key,
        occurred_at=occurred_at,
        provider_request_id=tool_use_id,
        provider_ref={"source": PERMISSION_GATE_SOURCE, "reply_transport": REPLY_TRANSPORT_CLAUDE_PULL},
        kind=PERMISSION_PROMPT_KIND,
        tool_name=tool_name,
        title=f"Permission: {tool_name}" if tool_name else "Tool permission",
        summary=f"Claude wants to use {tool_name}." if tool_name else "Claude is requesting tool permission.",
        request_payload={"tool_name": tool_name, "tool_input": payload.tool_input or {}},
        can_respond=True,
    )
    db.commit()
    return PermissionRequestAck(pause_request_id=str(row.id), request_key=request_key, status=row.status)


@router.get("/permission-decision", response_model=PermissionDecisionOut)
async def get_permission_decision(
    session_id: str,
    tool_use_id: str,
    pause_request_id: Optional[str] = None,
    provider: str = "claude",
    db: Session = Depends(get_db),
    _token: object = Depends(verify_agents_token),
) -> PermissionDecisionOut:
    """Return the resolved permission decision, or pending if not yet answered.

    Polls by the unique pause_request_id returned at register when available, so
    concurrent or repeated tool_use_ids resolve independently; falls back to the
    (session, tool_use_id)-derived request_key only when no id was provided.
    """

    _enforce_session_scope(_token, session_id)
    session_uuid = _coerce_session_uuid(session_id)

    # Import locally to avoid a router-import cycle through session_pause_requests.
    from zerg.models.agents import SessionPauseRequest

    query = db.query(SessionPauseRequest).filter(
        SessionPauseRequest.session_id == session_uuid,
        SessionPauseRequest.kind == PERMISSION_PROMPT_KIND,
    )
    if pause_request_id:
        try:
            query = query.filter(SessionPauseRequest.id == UUID(pause_request_id))
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid pause_request_id: {pause_request_id}",
            ) from exc
    else:
        normalized_provider = (provider or "claude").strip() or "claude"
        runtime_key = runtime_key_for_session(normalized_provider, session_id)
        request_key = make_pause_request_key(
            provider=normalized_provider,
            runtime_key=runtime_key,
            provider_request_id=tool_use_id,
        )
        query = query.filter(SessionPauseRequest.request_key == request_key)

    row = query.first()
    if row is None or not _is_permission_gate_row(row):
        return PermissionDecisionOut(decision=None, resolved=False)
    if row.status == PENDING_STATUS:
        return PermissionDecisionOut(decision=None, resolved=False)

    # SECURITY: only an explicit allow grants allow. Any resolved-but-unannotated
    # row (e.g. superseded, or resolved by a non-permission path) maps to deny —
    # never let an absent/unknown decision become a silent allow.
    response_payload = row.response_payload_json if isinstance(row.response_payload_json, dict) else {}
    raw_decision = str(response_payload.get("permissionDecision") or "").strip().lower()
    decision = "allow" if raw_decision == "allow" else "deny"
    reason = response_payload.get("permissionDecisionReason") or row.response_text
    return PermissionDecisionOut(decision=decision, reason=reason, resolved=True)


def _is_permission_gate_row(row: object) -> bool:
    """True only for rows this gate created (source=claude_permission_gate)."""
    ref = getattr(row, "provider_ref_json", None)
    source = (ref or {}).get("source") if isinstance(ref, dict) else None
    return str(source or "").strip() == PERMISSION_GATE_SOURCE
