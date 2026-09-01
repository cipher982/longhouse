"""Claude PreToolUse permission-gate request/decision endpoints.

A managed Claude session runs a PreToolUse hook that blocks on a permission-gated
tool, registers the held request here, and long-polls for a decision. Longhouse
stores the held request as a ``SessionPauseRequest`` (``kind=permission_prompt``,
``can_respond=True``) so the existing pause-request answer surface can resolve it.
The hook then reads the resolved decision and returns ``permissionDecision`` to
Claude. See ``session_chat`` for the answer path and ``session_pause_requests``
for the store.

Authentication mirrors presence ingest: the same ``X-Agents-Token`` / managed-local
hook-scoped session token, and it must match the target session.
"""

from __future__ import annotations

import logging
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import Optional
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from pydantic import Field
from sqlalchemy.orm import Session

from zerg.auth.caller import caller_principal
from zerg.auth.managed_session_tokens import ManagedSessionToken
from zerg.dependencies.agents_auth import verify_agents_caller
from zerg.dependencies.request_db import no_request_db
from zerg.services.session_pause_requests import REPLY_TRANSPORT_CLAUDE_PULL
from zerg.services.session_pause_requests import REPLY_TRANSPORT_CURSOR_POLL
from zerg.services.session_pause_requests import make_pause_request_key
from zerg.services.session_runtime import runtime_key_for_session
from zerg.utils.time import UTCBaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])


# Distinct source so answerable permission-gate requests are NOT hidden by the
# legacy claude_hook placeholder filter in session_pause_requests.
PERMISSION_GATE_SOURCE = "claude_permission_gate"
CURSOR_PERMISSION_GATE_SOURCE = "cursor_permission_gate"
PERMISSION_PROMPT_KIND = "permission_prompt"


def _permission_contract(provider: str) -> tuple[str, str, str]:
    """Return the closed, provider-owned contract for held permission prompts."""

    if provider == "cursor":
        return CURSOR_PERMISSION_GATE_SOURCE, REPLY_TRANSPORT_CURSOR_POLL, "Cursor"
    if provider == "claude":
        return PERMISSION_GATE_SOURCE, REPLY_TRANSPORT_CLAUDE_PULL, "Claude"
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unsupported permission-gate provider: {provider}")


class PermissionRequestIn(UTCBaseModel):
    """PreToolUse hook payload registering a held permission request."""

    session_id: str
    tool_use_id: str
    tool_name: Optional[str] = None
    tool_input: Optional[dict[str, Any]] = None
    provider: Optional[str] = "claude"
    occurred_at: Optional[datetime] = None
    wait_timeout_seconds: float = Field(default=20.0, ge=1.0, le=120.0)


class PermissionExpireIn(UTCBaseModel):
    session_id: str
    reason: Optional[str] = None


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
    """Require a session-scoped token whose session matches the target.

    These endpoints act *as* a single managed session, so a machine-wide durable
    device token must not be able to register/poll/resolve arbitrary sessions'
    permission requests. Only a hook-scoped session token bound to this session is
    accepted (``None`` is the AUTH_DISABLED dev/test path).
    """
    token = caller_principal(token)
    if token is None:
        return
    if isinstance(token, ManagedSessionToken):
        if session_id != token.session_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Managed-session hook scope does not match session",
            )
        return
    # A durable device token (or anything else) is not session-scoped.
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Permission-gate endpoints require a session-scoped hook token",
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
    db: Session = Depends(no_request_db),
    _token: object = Depends(verify_agents_caller),
) -> PermissionRequestAck:
    """Register a held Claude permission request from a PreToolUse hook."""

    _enforce_session_scope(_token, payload.session_id)
    session_uuid = _coerce_session_uuid(payload.session_id)

    # tool_use_id is the idempotency key: re-registering the same id (a hook
    # network retry) updates the same row, and a genuine re-ask re-pends it.
    # An empty id would collapse unrelated asks onto a shared "unknown" key.
    tool_use_id = (payload.tool_use_id or "").strip()
    if not tool_use_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="tool_use_id is required")

    provider = (payload.provider or "claude").strip() or "claude"
    occurred_at = (payload.occurred_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    expires_at = occurred_at + timedelta(seconds=payload.wait_timeout_seconds)
    source, reply_transport, provider_label = _permission_contract(provider)
    runtime_key = runtime_key_for_session(provider, payload.session_id)
    request_key = make_pause_request_key(
        provider=provider,
        runtime_key=runtime_key,
        provider_request_id=tool_use_id,
    )
    tool_name = (payload.tool_name or "").strip() or None

    from zerg.catalogd.client import CatalogRemoteError
    from zerg.services.catalogd_supervisor import get_catalogd_client

    catalogd = get_catalogd_client()
    if catalogd is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="live interaction catalog unavailable")
    try:
        result = await catalogd.call(
            "interaction.register.v2",
            {
                "interaction": {
                    "session_id": str(session_uuid),
                    "runtime_key": runtime_key,
                    "provider": provider,
                    "device_id": None,
                    "source": source,
                    "reply_transport": reply_transport,
                    "provider_request_id": tool_use_id,
                    "request_key": request_key,
                    "kind": PERMISSION_PROMPT_KIND,
                    "tool_name": tool_name,
                    "title": f"Permission: {tool_name}" if tool_name else "Tool permission",
                    "summary": (
                        f"{provider_label} wants to use {tool_name}." if tool_name else f"{provider_label} is requesting tool permission."
                    ),
                    "request_payload": {"tool_name": tool_name, "tool_input": payload.tool_input or {}},
                    "can_respond": True,
                    "occurred_at": occurred_at.isoformat(),
                    "expires_at": expires_at.isoformat(),
                    "single_active": True,
                }
            },
            timeout_seconds=1.0,
        )
    except CatalogRemoteError as exc:
        if exc.code == "conflict" and exc.details.get("reason") == "session_not_found":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="session not found") from exc
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="live interaction registration failed") from exc
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="live interaction registration failed") from exc
    interaction = result["interaction"]
    return PermissionRequestAck(
        pause_request_id=str(interaction["id"]),
        request_key=request_key,
        status=str(interaction["status"]),
    )


@router.get("/permission-decision", response_model=PermissionDecisionOut)
async def get_permission_decision(
    session_id: str,
    tool_use_id: str,
    pause_request_id: Optional[str] = None,
    provider: str = "claude",
    db: Session = Depends(no_request_db),
    _token: object = Depends(verify_agents_caller),
) -> PermissionDecisionOut:
    """Return the resolved permission decision, or pending if not yet answered.

    Polls by the unique pause_request_id returned at register when available, so
    concurrent or repeated tool_use_ids resolve independently; falls back to the
    (session, tool_use_id)-derived request_key only when no id was provided.
    """

    _enforce_session_scope(_token, session_id)
    session_uuid = _coerce_session_uuid(session_id)

    from zerg.services.catalogd_supervisor import get_catalogd_client

    interaction_id = None
    request_key = None
    if pause_request_id:
        try:
            interaction_id = str(UUID(pause_request_id))
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid pause_request_id: {pause_request_id}",
            ) from exc
    else:
        normalized_provider = (provider or "claude").strip() or "claude"
        request_key = make_pause_request_key(
            provider=normalized_provider,
            runtime_key=runtime_key_for_session(normalized_provider, session_id),
            provider_request_id=tool_use_id,
        )
    catalogd = get_catalogd_client()
    if catalogd is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="live interaction catalog unavailable")
    try:
        result = await catalogd.call(
            "interaction.decision.read.v2",
            {
                "session_id": str(session_uuid),
                "interaction_id": interaction_id,
                "request_key": request_key,
            },
            timeout_seconds=1.0,
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="live interaction decision unavailable") from exc
    return PermissionDecisionOut(
        decision=result.get("decision"),
        reason=result.get("reason"),
        resolved=result.get("resolved") is True,
    )


@router.post("/permission-requests/{pause_request_id}/expire", response_model=PermissionDecisionOut)
async def expire_permission_request(
    pause_request_id: str,
    payload: PermissionExpireIn,
    db: Session = Depends(no_request_db),
    _token: object = Depends(verify_agents_caller),
) -> PermissionDecisionOut:
    """Expire the exact held prompt when its provider-side wait deadline ends."""

    _enforce_session_scope(_token, payload.session_id)
    session_uuid = _coerce_session_uuid(payload.session_id)
    try:
        interaction_uuid = UUID(pause_request_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid pause_request_id") from exc
    reason = (payload.reason or "Approval deadline expired").strip() or "Approval deadline expired"
    response_payload = {"permissionDecision": "deny", "permissionDecisionReason": reason}
    now = datetime.now(timezone.utc)
    from zerg.catalogd.client import MANAGED_LAUNCH_CATALOG_TIMEOUT_SECONDS
    from zerg.catalogd.client import CatalogRemoteError
    from zerg.catalogd.client import CatalogUnavailable
    from zerg.services.catalogd_supervisor import get_catalogd_client

    catalogd = get_catalogd_client()
    if catalogd is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="live interaction catalog unavailable")
    try:
        result = await catalogd.call(
            "interaction.resolve.v2",
            {
                "session_id": str(session_uuid),
                "interaction_id": str(interaction_uuid),
                "status": "expired",
                "response_payload": response_payload,
                "response_text": reason,
                "resolved_at": now.isoformat(),
            },
            # Expiring an interaction is a mutation, not a hot read.
            timeout_seconds=MANAGED_LAUNCH_CATALOG_TIMEOUT_SECONDS,
        )
    except CatalogUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="live interaction expiry failed") from exc
    except CatalogRemoteError as exc:
        # A bare `except Exception` here reported catalog conflicts, missing
        # rows, and outright bugs as one infrastructure failure.
        status_code = {
            "not_found": status.HTTP_404_NOT_FOUND,
            "forbidden": status.HTTP_403_FORBIDDEN,
            "conflict": status.HTTP_409_CONFLICT,
        }.get(exc.code, status.HTTP_503_SERVICE_UNAVAILABLE)
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    if result.get("found") is not True:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="permission request not found")
    return PermissionDecisionOut(decision="deny", reason=reason, resolved=True)
