"""Session control router for managed-local live-send and launch.

Enables live interaction with managed-local CLI sessions launched through
Longhouse. Per-session locks prevent concurrent send collisions.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session

from zerg.config import get_settings
from zerg.database import get_db
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.dependencies.oikos_auth import get_current_oikos_user
from zerg.models.device_token import DeviceToken
from zerg.models.user import User
from zerg.services.managed_local_launcher import ManagedLocalLaunchError
from zerg.services.managed_local_launcher import ManagedLocalLaunchParams
from zerg.services.managed_local_launcher import launch_managed_local_session
from zerg.services.session_chat_impl import ManagedLocalSessionLaunchResponse
from zerg.services.session_chat_impl import SessionLockInfo
from zerg.services.session_chat_impl import _acquire_session_lock_or_raise
from zerg.services.session_chat_impl import _assert_live_session_send_available
from zerg.services.session_chat_impl import _authorize_live_send
from zerg.services.session_chat_impl import _build_managed_local_chat_response
from zerg.services.session_chat_impl import _load_session_for_continuation
from zerg.services.session_chat_impl import _lock_scope_id_for_session
from zerg.services.session_chat_impl import _managed_local_launch_response
from zerg.services.session_chat_impl import _resolve_agents_owner_id
from zerg.services.session_continuity import session_lock_manager
from zerg.session_loop_mode import SessionLoopMode

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["session-chat"])
agents_router = APIRouter(prefix="/agents/sessions", tags=["agents"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class SessionMessageRequest(BaseModel):
    """Request to send one message into an explicit session interaction path."""

    message: str = Field(..., min_length=1, max_length=10000, description="User message")


class ManagedLocalThisDeviceLaunchRequest(BaseModel):
    """Request to start a managed local AI agent session on the calling device."""

    cwd: str = Field(..., min_length=1, description="Working directory on this device")
    provider: str = Field("claude", description="AI provider CLI to launch (claude or codex)")
    project: str | None = Field(None, description="Optional project label")
    git_repo: str | None = Field(None, description="Optional git repository path")
    git_branch: str | None = Field(None, description="Optional git branch name")
    display_name: str | None = Field(None, description="Optional display name for the session")
    loop_mode: SessionLoopMode = Field(SessionLoopMode.MANUAL, description="manual | assist | autopilot")
    machine_name: str | None = Field(
        None,
        description="Optional local Longhouse machine label override used to resolve this device's runner",
    )
    native_claude_channels_available: bool | None = Field(
        None,
        description="Optional CLI capability hint for whether native Claude channels are available on this device",
    )
    claude_launch_env: dict[str, str] | None = Field(
        None,
        description="Optional allowlisted Claude launch env overrides to apply on the local runner",
    )


class SessionChatError(BaseModel):
    """Error response for session chat."""

    error: str
    code: str
    lock_info: SessionLockInfo | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/{session_id}/send-live")
async def send_to_live_session(
    session_id: str,
    body: SessionMessageRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_oikos_user),
):
    """Send text into the live managed-local session and return a fast JSON ack."""
    request_id = str(uuid.uuid4())[:8]
    source_session = _load_session_for_continuation(db, session_id)
    logger.info(f"[{request_id}] Live session send request for session {source_session.id}")
    _assert_live_session_send_available(source_session)
    lock_scope_id = await _acquire_session_lock_or_raise(source_session=source_session, request_id=request_id)
    try:
        return await _build_managed_local_chat_response(
            source_session=source_session,
            owner_id=current_user.id,
            message=body.message,
            request_id=request_id,
            lock_scope_id=lock_scope_id,
            db=db,
        )
    except HTTPException:
        await session_lock_manager.release(lock_scope_id, request_id)
        raise
    except Exception as exc:
        await session_lock_manager.release(lock_scope_id, request_id)
        logger.exception(f"[{request_id}] Error in send_to_live_session")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal error: {str(exc)[:200]}",
        ) from exc


@agents_router.post("/{session_id}/send-live")
async def send_to_live_session_agents(
    session_id: str,
    body: SessionMessageRequest,
    request: Request,
    db: Session = Depends(get_db),
    device_token: DeviceToken | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
):
    """Machine-facing explicit live-send surface for managed-local sessions."""
    settings = get_settings()
    resolved_device_token = device_token if isinstance(device_token, DeviceToken) else None

    request_id = str(uuid.uuid4())[:8]
    source_session = _load_session_for_continuation(db, session_id)
    _authorize_live_send(
        request=request,
        device_token=resolved_device_token,
        auth_disabled=settings.auth_disabled,
    )
    _assert_live_session_send_available(source_session)
    owner_id = _resolve_agents_owner_id(db, resolved_device_token)
    lock_scope_id = await _acquire_session_lock_or_raise(source_session=source_session, request_id=request_id)

    try:
        return await _build_managed_local_chat_response(
            source_session=source_session,
            owner_id=owner_id,
            message=body.message,
            request_id=request_id,
            lock_scope_id=lock_scope_id,
            db=db,
        )
    except HTTPException:
        await session_lock_manager.release(lock_scope_id, request_id)
        raise
    except Exception as exc:
        await session_lock_manager.release(lock_scope_id, request_id)
        logger.exception(f"[{request_id}] Error in send_to_live_session_agents")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal error: {str(exc)[:200]}",
        ) from exc


@router.post("/managed-local/this-device", response_model=ManagedLocalSessionLaunchResponse)
async def launch_managed_local_this_device(
    body: ManagedLocalThisDeviceLaunchRequest,
    db: Session = Depends(get_db),
    device_token: DeviceToken | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
):
    """Start a managed local AI agent session on the calling machine's connected runner."""

    owner_id = _resolve_agents_owner_id(db, device_token)
    machine_name = (body.machine_name or "").strip() or str(getattr(device_token, "device_id", "") or "").strip()
    if not machine_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Could not determine this device name")

    try:
        result = await launch_managed_local_session(
            db,
            ManagedLocalLaunchParams(
                owner_id=owner_id,
                runner_target=machine_name,
                cwd=body.cwd,
                provider=body.provider,
                project=body.project,
                git_repo=body.git_repo,
                git_branch=body.git_branch,
                display_name=body.display_name,
                loop_mode=body.loop_mode.value,
                machine_name=machine_name,
                native_claude_channels_available=body.native_claude_channels_available,
                claude_launch_env=body.claude_launch_env,
            ),
        )
    except ManagedLocalLaunchError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except Exception:
        db.rollback()
        logger.exception("Managed local launch for this device failed unexpectedly")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Managed local launch failed",
        )

    return _managed_local_launch_response(result)


@router.get("/{session_id}/lock")
async def get_session_lock_status(
    session_id: str,
    db: Session = Depends(get_db),
    _current_user=Depends(get_current_oikos_user),
) -> SessionLockInfo:
    """Check if a session is currently locked.

    Used by UI to show lock status before attempting to chat.
    """
    lock_scope_id = _lock_scope_id_for_session(db, session_id)
    lock = await session_lock_manager.get_lock_info(lock_scope_id)

    if lock:
        return SessionLockInfo(
            locked=True,
            holder=lock.holder,
            time_remaining_seconds=lock.time_remaining,
            fork_available=True,
        )
    else:
        return SessionLockInfo(
            locked=False,
            fork_available=False,
        )


@router.delete("/{session_id}/lock")
async def force_release_lock(
    session_id: str,
    db: Session = Depends(get_db),
    _current_user=Depends(get_current_oikos_user),
) -> dict:
    """Force release a session lock (admin operation).

    Use with caution - may cause issues if a chat is in progress.
    """
    lock_scope_id = _lock_scope_id_for_session(db, session_id)
    released = await session_lock_manager.release(lock_scope_id)
    return {
        "released": released,
        "session_id": session_id,
        "lock_session_id": lock_scope_id,
    }
