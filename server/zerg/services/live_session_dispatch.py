from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from uuid import UUID

from sqlalchemy.orm import Session

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentSessionBranch
from zerg.services.agents.kernel_capability_adapter import build_session_capabilities_from_kernel
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_COMMAND_SEND_TEXT
from zerg.services.managed_control_dispatcher import select_managed_control_transport
from zerg.services.managed_local_control import ManagedLocalSendResult
from zerg.services.managed_local_control import send_text_to_managed_local_session
from zerg.session_execution_home import SessionExecutionHome


def live_text_dispatch_label(db: Session | None, session: AgentSession | None) -> str | None:
    if session is None or db is None:
        return None
    flags = build_session_capabilities_from_kernel(db, session)
    return flags.execution_home.value


def supports_live_text_dispatch_metadata(
    session: AgentSession | None,
    *,
    db: Session | None = None,
    owner_id: int | None = None,
) -> bool:
    """Structural precondition only; callers must check current liveness first.

    When ``db`` is provided, kernel projection decides managed-local
    eligibility. The engine-channel transport check is independent of the
    kernel and works without a DB session.
    """
    if session is None:
        return False
    if db is not None:
        flags = build_session_capabilities_from_kernel(db, session)
        if flags.live_control_available or flags.host_reattach_available:
            return True
    return (
        select_managed_control_transport(
            session,
            owner_id=owner_id,
            command_type=MANAGED_CONTROL_COMMAND_SEND_TEXT,
        )
        is not None
    )


def _truthy_env(name: str) -> bool:
    return str(os.getenv(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _use_fake_live_text_dispatch() -> bool:
    return _truthy_env("TESTING") and _truthy_env("E2E_FAKE_SESSION_MESSAGES")


def _ensure_head_branch_id(db: Session, session_id: UUID) -> int:
    row = (
        db.query(AgentSessionBranch.id)
        .filter(AgentSessionBranch.session_id == session_id, AgentSessionBranch.is_head == 1)
        .order_by(AgentSessionBranch.id.desc())
        .first()
    )
    if row is not None:
        return int(row[0])

    branch = AgentSessionBranch(
        session_id=session_id,
        parent_branch_id=None,
        branched_at_source_path=None,
        branched_at_offset=None,
        branch_reason="root",
        is_head=1,
    )
    db.add(branch)
    db.flush()
    return int(branch.id)


async def _fake_send_text_to_live_session(
    *,
    db: Session,
    session: AgentSession,
    text: str,
) -> ManagedLocalSendResult:
    now = datetime.now(timezone.utc)
    head_branch_id = _ensure_head_branch_id(db, session.id)
    from zerg.services.agents.kernel_writes import ensure_primary_thread

    primary_thread = ensure_primary_thread(db, session)
    event = AgentEvent(
        session_id=session.id,
        thread_id=primary_thread.id,
        role="user",
        content_text=text,
        timestamp=now,
        branch_id=head_branch_id,
    )
    db.add(event)
    session.last_activity_at = now
    session.user_messages = int(getattr(session, "user_messages", 0) or 0) + 1
    db.flush()
    return ManagedLocalSendResult(
        ok=True,
        exit_code=0,
        baseline_event_id=event.id,
        verified_turn_started=True,
    )


async def send_text_to_live_session(
    *,
    db: Session,
    owner_id: int,
    session: AgentSession,
    text: str,
    commis_id: str | None = None,
    timeout_secs: int = 15,
    verify_turn_started: bool = False,
    verification_timeout_secs: float | None = None,
    attachments: list[dict] | None = None,
) -> ManagedLocalSendResult:
    """Send text to a live session through the single transport dispatch seam.

    Managed-local is the only supported live transport today. Other execution
    homes intentionally fail closed here until a second live delivery path is
    implemented.
    """

    if supports_live_text_dispatch_metadata(session, db=db, owner_id=owner_id):
        if _use_fake_live_text_dispatch():
            return await _fake_send_text_to_live_session(
                db=db,
                session=session,
                text=text,
            )
        return await send_text_to_managed_local_session(
            db=db,
            owner_id=owner_id,
            session=session,
            text=text,
            commis_id=commis_id,
            timeout_secs=timeout_secs,
            verify_turn_started=verify_turn_started,
            verification_timeout_secs=verification_timeout_secs,
            attachments=attachments,
        )

    execution_home = (
        build_session_capabilities_from_kernel(db, session).execution_home.value
        if session is not None
        else SessionExecutionHome.UNMANAGED_LOCAL.value
    )
    return ManagedLocalSendResult(
        ok=False,
        error=f"Live text dispatch is not supported for execution_home={execution_home}",
    )
