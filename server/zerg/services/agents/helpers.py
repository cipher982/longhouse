"""Helper functions for agent session operations."""

from datetime import datetime
from datetime import timezone
from typing import TYPE_CHECKING

from zerg.models.agents import AgentSession
from zerg.session_execution_home import SessionExecutionHome
from zerg.session_execution_home import infer_continuation_kind
from zerg.session_execution_home import infer_execution_home
from zerg.session_execution_home import infer_origin_label

if TYPE_CHECKING:
    from .models import SessionIngest


def _normalize_utc_naive(value: datetime | None) -> datetime | None:
    """Normalize aware datetimes to naive UTC for SQLite-safe comparison."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _infer_execution_home_from_ingest(data: "SessionIngest") -> SessionExecutionHome:
    return infer_execution_home(
        execution_home=getattr(data, "execution_home", None),
        continuation_kind=getattr(data, "continuation_kind", None),
        origin_label=getattr(data, "origin_label", None),
        environment=getattr(data, "environment", None),
    )


def _infer_execution_home_from_session(session: AgentSession) -> SessionExecutionHome:
    return infer_execution_home(
        execution_home=getattr(session, "execution_home", None),
        continuation_kind=getattr(session, "continuation_kind", None),
        origin_label=getattr(session, "origin_label", None),
        environment=getattr(session, "environment", None),
    )


_MANAGED_NATIVE_PROVIDER_SESSION_ID_PROVIDERS = {"codex", "antigravity"}


def _should_replace_managed_local_placeholder_provider_session_id(
    session: AgentSession,
    incoming_provider_session_id: str,
) -> bool:
    current_provider_session_id = str(session.provider_session_id or "").strip()
    if not current_provider_session_id:
        return False
    if current_provider_session_id != str(session.id):
        return False
    if incoming_provider_session_id == current_provider_session_id:
        return False
    provider = str(session.provider or "").strip().lower()
    if provider not in _MANAGED_NATIVE_PROVIDER_SESSION_ID_PROVIDERS:
        return False
    execution_home = str(getattr(session, "execution_home", "") or "").strip()
    return execution_home == SessionExecutionHome.MANAGED_LOCAL.value


def _infer_continuation_kind_from_ingest(data: "SessionIngest") -> str:
    return infer_continuation_kind(
        continuation_kind=getattr(data, "continuation_kind", None),
        execution_home=getattr(data, "execution_home", None),
        origin_label=getattr(data, "origin_label", None),
        environment=getattr(data, "environment", None),
    )


def _infer_origin_label_from_ingest(data: "SessionIngest") -> str:
    return infer_origin_label(
        origin_label=getattr(data, "origin_label", None),
        environment=getattr(data, "environment", None),
        device_id=getattr(data, "device_id", None),
        execution_home=getattr(data, "execution_home", None),
        continuation_kind=getattr(data, "continuation_kind", None),
    )


def _infer_continuation_kind_from_session(session: AgentSession) -> str:
    return infer_continuation_kind(
        continuation_kind=getattr(session, "continuation_kind", None),
        execution_home=getattr(session, "execution_home", None),
        origin_label=getattr(session, "origin_label", None),
        environment=getattr(session, "environment", None),
    )


def _infer_origin_label_from_session(session: AgentSession) -> str:
    return infer_origin_label(
        origin_label=getattr(session, "origin_label", None),
        environment=getattr(session, "environment", None),
        device_id=getattr(session, "device_id", None),
        execution_home=getattr(session, "execution_home", None),
        continuation_kind=getattr(session, "continuation_kind", None),
    )
