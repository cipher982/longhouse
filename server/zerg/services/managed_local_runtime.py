"""Managed-local runtime signal helpers for Timeline."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone

from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.services.agents.kernel_capabilities import project_session_capabilities
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import ingest_runtime_events
from zerg.services.session_runtime import phase_freshness_ms
from zerg.services.session_runtime import runtime_key_for_session

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
