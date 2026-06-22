"""Explicit projections for API fields that used to be AgentSession shims."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.models.agents import SessionConnection
from zerg.models.agents import SessionEdge
from zerg.models.agents import SessionThread
from zerg.models.agents import SessionThreadAlias
from zerg.models.models import Runner
from zerg.services.agents.kernel_capabilities import KernelSessionCapabilities
from zerg.services.agents.kernel_capabilities import project_session_capabilities
from zerg.session_execution_home import infer_origin_label

_DIRECT_MACHINE_CONTROL_PLANES = frozenset(
    {
        "codex_bridge",
        "codex_app_server",
        "codex_exec",
        "antigravity_hook_inbox",
        "antigravity_process",
    }
)


@dataclass(frozen=True)
class SessionLineageProjection:
    thread_root_session_id: str
    continued_from_session_id: str | None
    continuation_kind: str | None
    origin_label: str
    branched_from_event_id: int | None
    is_writable_head: bool
    is_sidechain: bool


@dataclass(frozen=True)
class SessionControlProjection:
    source_runner_id: int | None
    source_runner_name: str | None
    managed_session_name: str | None


@dataclass(frozen=True)
class SessionKernelProjection:
    capabilities: KernelSessionCapabilities
    lineage: SessionLineageProjection
    control: SessionControlProjection
    provider_session_id: str | None


def session_lock_scope_id(session_id: UUID | str) -> str:
    """Return the lock scope for a session.

    The session identity kernel made a product session the lock boundary for
    now. Keep this helper explicit so future multi-thread ownership changes do
    not revive ``AgentSession.thread_root_session_id``.
    """

    return str(session_id)


def is_synthetic_provider_session_id(session: AgentSession, value: str | None) -> bool:
    """Return true for known non-native provider ids left by old placeholders."""

    normalized = str(value or "").strip()
    return bool(normalized) and normalized == str(session.id)


def project_provider_session_id(db: Session, session: AgentSession) -> str | None:
    """Return the provider-native id alias for a session when one is known."""
    thread = _primary_thread(db, session)
    return _provider_session_id_for_thread(db, session=session, thread=thread)


def _provider_session_id_for_thread(db: Session, *, session: AgentSession, thread: SessionThread | None) -> str | None:
    if thread is None:
        return None
    alias = (
        db.query(SessionThreadAlias.alias_value)
        .filter(SessionThreadAlias.thread_id == thread.id)
        .filter(SessionThreadAlias.provider == session.provider)
        .filter(SessionThreadAlias.alias_kind == "provider_session_id")
        .order_by(SessionThreadAlias.id.desc())
        .first()
    )
    if alias is not None and alias[0]:
        value = str(alias[0]).strip()
        if is_synthetic_provider_session_id(session, value):
            return None
        return value
    return None


def project_session_lineage_fields(db: Session, session: AgentSession) -> SessionLineageProjection:
    thread = _primary_thread(db, session)
    return _lineage_for_thread(db, session=session, thread=thread)


def _lineage_for_thread(db: Session, *, session: AgentSession, thread: SessionThread | None) -> SessionLineageProjection:
    source_session_id = _source_session_id_for_thread(db, thread=thread, session=session)
    return SessionLineageProjection(
        thread_root_session_id=str(session.id),
        continued_from_session_id=source_session_id,
        continuation_kind=_continuation_kind_for_thread(thread),
        origin_label=infer_origin_label(
            origin_label=None,
            environment=session.environment,
            device_id=session.device_id,
            execution_home=None,
            continuation_kind=None,
        ),
        branched_from_event_id=(int(thread.parent_event_id) if thread is not None and thread.parent_event_id is not None else None),
        is_writable_head=True,
        is_sidechain=bool(thread is not None and thread.branch_kind == "subagent"),
    )


def project_session_control_fields(
    db: Session,
    session: AgentSession,
    *,
    capabilities: KernelSessionCapabilities | None = None,
) -> SessionControlProjection:
    capabilities = capabilities or project_session_capabilities(db, session_id=session.id)
    if not (capabilities.live_control_available or capabilities.host_reattach_available):
        return SessionControlProjection(source_runner_id=None, source_runner_name=None, managed_session_name=None)

    connection = _connection_by_id(db, capabilities.connection_id)
    source_runner_name = _clean_str(connection.device_id if connection is not None else None) or _clean_str(session.device_id)
    managed_session_name = _clean_str(connection.external_name if connection is not None else None)
    source_runner_id = _source_runner_id_for_device(
        db,
        device_id=source_runner_name,
        control_plane=capabilities.control_plane,
    )
    return SessionControlProjection(
        source_runner_id=source_runner_id,
        source_runner_name=source_runner_name,
        managed_session_name=managed_session_name,
    )


def project_session_kernel_fields(
    db: Session,
    session: AgentSession,
    *,
    capabilities: KernelSessionCapabilities | None = None,
) -> SessionKernelProjection:
    """Return the common read projection used by session response surfaces."""

    thread = _primary_thread(db, session)
    resolved_capabilities = capabilities or project_session_capabilities(db, session_id=session.id)
    return SessionKernelProjection(
        capabilities=resolved_capabilities,
        lineage=_lineage_for_thread(db, session=session, thread=thread),
        control=project_session_control_fields(db, session, capabilities=resolved_capabilities),
        provider_session_id=_provider_session_id_for_thread(db, session=session, thread=thread),
    )


def _primary_thread(db: Session, session: AgentSession) -> SessionThread | None:
    thread_id = getattr(session, "primary_thread_id", None)
    query = db.query(SessionThread).filter(SessionThread.session_id == session.id)
    if thread_id is not None:
        by_id = query.filter(SessionThread.id == thread_id).one_or_none()
        if by_id is not None:
            return by_id
    return query.filter(SessionThread.is_primary == 1).order_by(SessionThread.created_at.asc(), SessionThread.id.asc()).first()


def _connection_by_id(db: Session, connection_id: int | None) -> SessionConnection | None:
    if connection_id is None:
        return None
    return db.query(SessionConnection).filter(SessionConnection.id == connection_id).one_or_none()


def _source_runner_id_for_device(db: Session, *, device_id: str | None, control_plane: str | None) -> int | None:
    if not device_id:
        return None
    if is_direct_machine_control_plane(control_plane):
        return None
    runner = db.query(Runner).filter(Runner.name == device_id).first()
    return int(runner.id) if runner is not None else None


def direct_machine_control_planes() -> frozenset[str]:
    """Return connection control planes that are local-machine direct paths."""

    return _DIRECT_MACHINE_CONTROL_PLANES


def is_direct_machine_control_plane(control_plane: str | None) -> bool:
    return (control_plane or "").strip() in _DIRECT_MACHINE_CONTROL_PLANES


def _source_session_id_for_thread(db: Session, *, thread: SessionThread | None, session: AgentSession) -> str | None:
    if thread is None:
        return None
    edge = (
        db.query(SessionEdge)
        .filter(SessionEdge.target_thread_id == thread.id)
        .filter(SessionEdge.source_session_id.isnot(None))
        .order_by(SessionEdge.updated_at.desc(), SessionEdge.created_at.desc())
        .first()
    )
    if edge is None or edge.source_session_id is None:
        return None
    if str(edge.source_session_id) == str(session.id):
        return None
    return str(edge.source_session_id)


def _continuation_kind_for_thread(thread: SessionThread | None) -> str | None:
    if thread is None:
        return None
    branch_kind = _clean_str(thread.branch_kind)
    if branch_kind and branch_kind != "root":
        return branch_kind
    return None


def _clean_str(value: str | None) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None
