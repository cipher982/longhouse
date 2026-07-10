"""Hot session identity and capability projection for catalog-mode control."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from uuid import UUID
from uuid import uuid4

from sqlalchemy.orm import Session

from zerg.models.live_store import LiveRuntimeState
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread


@dataclass(frozen=True)
class LiveControlSession:
    """The bounded AgentSession shape required by machine-control dispatch."""

    id: UUID
    provider: str
    device_id: str | None
    device_name: str | None
    cwd: str | None
    project: str | None
    git_repo: str | None
    git_branch: str | None
    ended_at: object | None
    loop_mode: str
    permission_mode: str
    primary_thread_id: UUID | None


def load_live_control_session(db: Session, session_id: UUID | str) -> LiveControlSession | None:
    key = str(session_id)
    row = db.query(LiveSessionCatalog).filter(LiveSessionCatalog.session_id == key).first()
    if row is None:
        return None
    try:
        parsed_id = UUID(str(row.session_id))
    except ValueError:
        return None
    thread_id = None
    if row.primary_thread_id:
        try:
            thread_id = UUID(str(row.primary_thread_id))
        except ValueError:
            thread_id = None
    return LiveControlSession(
        id=parsed_id,
        provider=str(row.provider or "unknown"),
        device_id=str(row.device_id).strip() if row.device_id else None,
        device_name=str(row.device_name).strip() if row.device_name else None,
        cwd=row.cwd,
        project=row.project,
        git_repo=row.git_repo,
        git_branch=row.git_branch,
        ended_at=row.ended_at,
        loop_mode=str(row.loop_mode or "assist"),
        permission_mode=str(row.permission_mode or "bypass"),
        primary_thread_id=thread_id,
    )


def live_control_capability_available(
    db: Session,
    *,
    session_id: UUID | str,
    capability: str,
) -> bool:
    """Return whether an attached live kernel connection grants a capability."""

    column = {
        "send": LiveSessionConnection.can_send_input,
        "interrupt": LiveSessionConnection.can_interrupt,
        "terminate": LiveSessionConnection.can_terminate,
    }.get(capability)
    if column is None:
        raise ValueError(f"unknown live control capability: {capability}")
    return (
        db.query(LiveSessionConnection.id)
        .join(LiveSessionRun, LiveSessionRun.id == LiveSessionConnection.run_id)
        .join(LiveSessionThread, LiveSessionThread.id == LiveSessionRun.thread_id)
        .filter(
            LiveSessionThread.session_id == str(session_id),
            LiveSessionConnection.state == "attached",
            LiveSessionConnection.released_at.is_(None),
            column == 1,
        )
        .limit(1)
        .first()
        is not None
    )


def live_session_closed_for_input(db: Session, session: LiveControlSession) -> bool:
    if session.ended_at is not None:
        return True
    row = (
        db.query(LiveRuntimeState.terminal_state)
        .filter(LiveRuntimeState.session_id == session.id)
        .order_by(LiveRuntimeState.updated_at.desc(), LiveRuntimeState.runtime_version.desc())
        .first()
    )
    if row is None:
        return False
    terminal_state = str(row[0] or "").strip()
    return terminal_state not in {"", "finished", "host_expired"}


async def wake_next_live_catalog_input(session_id: UUID | str) -> bool:
    """Claim and dispatch one queued hot receipt after a terminal signal."""

    import zerg.database as database_module
    from zerg.services.live_session_inputs import claim_next_live_queued_receipt
    from zerg.services.live_session_inputs import mark_live_receipt_delivered_with_projection
    from zerg.services.live_session_inputs import mark_live_receipt_failed
    from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_COMMAND_SEND_TEXT
    from zerg.services.managed_control_dispatcher import dispatch_managed_control_command
    from zerg.services.session_kernel_projection import session_lock_scope_id
    from zerg.services.session_locks import session_lock_manager
    from zerg.services.write_serializer import get_live_write_serializer

    factory = database_module.get_live_session_factory()
    live_ws = get_live_write_serializer()
    if factory is None or not live_ws.is_configured:
        return False
    with factory() as read_db:
        session = load_live_control_session(read_db, session_id)
    if session is None:
        return False

    request_id = uuid4().hex
    lock_scope_id = session_lock_scope_id(session.id)
    if not await session_lock_manager.acquire(session_id=lock_scope_id, holder=request_id, ttl_seconds=300):
        return False
    receipt = await live_ws.execute(
        lambda live_db: claim_next_live_queued_receipt(
            live_db,
            session_id=session.id,
            delivery_request_id=request_id,
        ),
        auto_commit=False,
        label="live-input-queue-claim",
    )
    if receipt is None:
        await session_lock_manager.release(lock_scope_id, request_id)
        return False

    dispatched_at = datetime.now(timezone.utc)
    with factory() as dispatch_db:
        result = await dispatch_managed_control_command(
            db=dispatch_db,
            owner_id=receipt.owner_id,
            session=session,
            timeout_secs=15,
            command_type=MANAGED_CONTROL_COMMAND_SEND_TEXT,
            payload={"text": receipt.text},
            request_id=request_id,
            run_id=None,
        )
    data = dict(result.data or {})
    if not result.ok or int(data.get("exit_code", 1)) != 0:
        await live_ws.execute(
            lambda live_db: mark_live_receipt_failed(
                live_db,
                receipt_id=receipt.id,
                error=str(result.error or data.get("stderr") or "queued send failed"),
            ),
            auto_commit=False,
            label="live-input-queue-failed",
        )
        await session_lock_manager.release(lock_scope_id, request_id)
        return False

    await live_ws.execute(
        lambda live_db: mark_live_receipt_delivered_with_projection(
            live_db,
            receipt_id=receipt.id,
            delivery_request_id=request_id,
        ),
        auto_commit=False,
        label="live-input-queue-delivered",
    )
    from zerg.services.session_chat_impl import _schedule_catalog_lock_release

    _schedule_catalog_lock_release(
        session_id=session.id,
        lock_scope_id=lock_scope_id,
        request_id=request_id,
        dispatched_at=dispatched_at,
    )
    return True
