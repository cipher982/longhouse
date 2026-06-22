"""Managed-local runtime signal helpers for Timeline."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.services.agents.kernel_capabilities import project_session_capabilities
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import ingest_runtime_events
from zerg.services.session_runtime import phase_freshness_ms
from zerg.services.session_runtime import runtime_key_for_session
from zerg.services.write_serializer import get_write_serializer

MANAGED_LOCAL_RUNTIME_SOURCE = "managed_local_transport"


def _is_managed_local_session(db: Session, session: AgentSession) -> bool:
    capabilities = project_session_capabilities(db, session_id=session.id)
    return bool(capabilities.live_control_available or capabilities.host_reattach_available)


def _emit_managed_local_phase_signal(
    db: Session,
    *,
    session: AgentSession,
    phase: str,
    dedupe_key: str,
    occurred_at: datetime | None = None,
) -> None:
    if not _is_managed_local_session(db, session):
        return
    capabilities = project_session_capabilities(db, session_id=session.id)

    signal_at = occurred_at or datetime.now(timezone.utc)
    runtime_key = runtime_key_for_session(str(session.provider or "claude"), str(session.id))
    ingest_runtime_events(
        db,
        [
            RuntimeEventIngest(
                runtime_key=runtime_key,
                session_id=session.id,
                provider=str(session.provider or "claude"),
                device_id=str(session.device_id or "") or None,
                source=MANAGED_LOCAL_RUNTIME_SOURCE,
                kind="phase_signal",
                phase=phase,
                tool_name=None,
                occurred_at=signal_at,
                freshness_ms=phase_freshness_ms(phase),
                dedupe_key=dedupe_key,
                payload={"managed_transport": (capabilities.managed_transport.value if capabilities.managed_transport else None)},
            )
        ],
    )


def mark_managed_local_session_launched(db: Session, *, session: AgentSession) -> None:
    _emit_managed_local_phase_signal(
        db,
        session=session,
        phase="idle",
        dedupe_key=f"managed-local-launch:{session.id}",
    )


def mark_managed_local_turn_idle(
    db: Session,
    *,
    session: AgentSession,
    dedupe_suffix: str | None = None,
) -> None:
    _emit_managed_local_phase_signal(
        db,
        session=session,
        phase="idle",
        dedupe_key=f"managed-local-idle:{session.id}:{dedupe_suffix or uuid4().hex}",
    )


def mark_managed_local_turn_needs_user(
    db: Session,
    *,
    session: AgentSession,
    dedupe_suffix: str | None = None,
) -> None:
    _emit_managed_local_phase_signal(
        db,
        session=session,
        phase="needs_user",
        dedupe_key=f"managed-local-needs-user:{session.id}:{dedupe_suffix or uuid4().hex}",
    )


def mark_managed_local_turn_blocked(
    db: Session,
    *,
    session: AgentSession,
    dedupe_suffix: str | None = None,
) -> None:
    _emit_managed_local_phase_signal(
        db,
        session=session,
        phase="blocked",
        dedupe_key=f"managed-local-blocked:{session.id}:{dedupe_suffix or uuid4().hex}",
    )


async def persist_managed_local_phase_signal(
    db: Session,
    *,
    session: AgentSession,
    phase: str,
    dedupe_key: str,
    occurred_at: datetime | None = None,
) -> None:
    if not _is_managed_local_session(session):
        return

    ws = get_write_serializer()

    def _do(wdb: Session) -> None:
        _emit_managed_local_phase_signal(
            wdb,
            session=session,
            phase=phase,
            dedupe_key=dedupe_key,
            occurred_at=occurred_at,
        )

    await ws.execute_or_direct(_do, db, label="runtime-observations")


async def persist_managed_local_turn_idle(
    db: Session,
    *,
    session: AgentSession,
    dedupe_suffix: str | None = None,
) -> None:
    await persist_managed_local_phase_signal(
        db,
        session=session,
        phase="idle",
        dedupe_key=f"managed-local-idle:{session.id}:{dedupe_suffix or uuid4().hex}",
    )


async def persist_managed_local_turn_needs_user(
    db: Session,
    *,
    session: AgentSession,
    dedupe_suffix: str | None = None,
) -> None:
    await persist_managed_local_phase_signal(
        db,
        session=session,
        phase="needs_user",
        dedupe_key=f"managed-local-needs-user:{session.id}:{dedupe_suffix or uuid4().hex}",
    )


async def persist_managed_local_turn_blocked(
    db: Session,
    *,
    session: AgentSession,
    dedupe_suffix: str | None = None,
) -> None:
    await persist_managed_local_phase_signal(
        db,
        session=session,
        phase="blocked",
        dedupe_key=f"managed-local-blocked:{session.id}:{dedupe_suffix or uuid4().hex}",
    )
