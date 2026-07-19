"""Hot session identity and capability projection for catalog-mode control."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Literal
from uuid import UUID
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from zerg.catalogd.models import FactHead
from zerg.models.live_store import LiveRuntimeState
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.services.managed_control_state import DEFAULT_MANAGED_CONTROL_LEASE_TTL_MS
from zerg.services.managed_provider_contracts import contract_for_provider
from zerg.services.session_state_facts_projector import authorize_exact_control_fact
from zerg.utils.time import normalize_utc

logger = logging.getLogger(__name__)
_QUEUE_DRAINABLE_PHASES = frozenset({"idle", "needs_user", "blocked"})
_CONTROL_ACQUISITION_KINDS = ("spawned_control", "adopted_control")
_COMMAND_AUTH_ENV = "LONGHOUSE_SESSION_STATE_COMMAND_AUTH"
_COMMAND_AUTH_PROVIDERS_ENV = "LONGHOUSE_SESSION_STATE_COMMAND_AUTH_PROVIDERS"
_SHADOW_REDUCER_INGEST_ENV = "LONGHOUSE_SHADOW_REDUCER_INGEST_ENABLED"
_TRUTHY_ENV = frozenset({"1", "true", "yes", "on"})
_CANONICAL_AUTH_PROVIDERS = frozenset({"codex", "claude", "opencode", "cursor"})


def canonical_command_authorization_providers() -> tuple[str, ...]:
    """Return providers explicitly eligible for the canonical authority path."""

    if os.getenv(_COMMAND_AUTH_ENV, "legacy").strip().lower() != "canonical":
        return ()
    configured = {
        provider.strip().lower()
        for provider in os.getenv(
            _COMMAND_AUTH_PROVIDERS_ENV,
            ",".join(sorted(_CANONICAL_AUTH_PROVIDERS)),
        ).split(",")
        if provider.strip()
    }
    return tuple(sorted(configured & _CANONICAL_AUTH_PROVIDERS))


def canonical_command_authorization_enabled(provider: str | None = None) -> bool:
    providers = canonical_command_authorization_providers()
    if provider is None:
        return bool(providers)
    return str(provider or "").strip().lower() in providers


def shadow_reducer_ingest_enabled() -> bool:
    return os.getenv(_SHADOW_REDUCER_INGEST_ENV, "").strip().lower() in _TRUTHY_ENV


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
    closed_at: object | None
    close_reason: str | None
    loop_mode: str
    permission_mode: str
    primary_thread_id: UUID | None
    command_family: Literal["console_turn", "live_control"]
    catalog_facts: dict | None = None


@dataclass(frozen=True)
class LiveControlGrant:
    catalog_connection_id: int
    connection_id: str | int
    run_id: str
    lease_generation: str
    identity_source: Literal["adapter_bound", "legacy_synthetic"]


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
        closed_at=row.closed_at,
        close_reason=str(row.close_reason).strip() if row.close_reason else None,
        command_family="console_turn" if str(row.origin_kind or "").strip() == "console" else "live_control",
        loop_mode=str(row.loop_mode or "assist"),
        permission_mode=str(row.permission_mode or "bypass"),
        primary_thread_id=thread_id,
    )


def load_live_control_session_snapshot(session_id: UUID | str, *, owner_id: int | None = None) -> LiveControlSession | None:
    """Load bounded control facts through catalogd without opening SQLite."""

    from zerg.services.catalog_read_gateway import CatalogReadError
    from zerg.services.catalog_read_gateway import session_snapshot

    try:
        if owner_id is None:
            result = session_snapshot(str(session_id))
        else:
            result = session_snapshot(str(session_id), owner_id=owner_id)
    except CatalogReadError:
        logger.warning("Catalog control snapshot failed for session %s", session_id, exc_info=True)
        return None
    facts = result.get("facts")
    if result.get("found") is not True or not isinstance(facts, dict):
        return None
    catalog = facts.get("catalog")
    if not isinstance(catalog, dict):
        return None

    def _datetime(value):
        return normalize_utc(datetime.fromisoformat(value)) if isinstance(value, str) and value else None

    primary_thread_id = catalog.get("primary_thread_id")
    return LiveControlSession(
        id=UUID(str(catalog["session_id"])),
        provider=str(catalog.get("provider") or "unknown"),
        device_id=str(catalog["device_id"]) if catalog.get("device_id") else None,
        device_name=str(catalog["device_name"]) if catalog.get("device_name") else None,
        cwd=catalog.get("cwd"),
        project=catalog.get("project"),
        git_repo=catalog.get("git_repo"),
        git_branch=catalog.get("git_branch"),
        ended_at=_datetime(catalog.get("ended_at")),
        closed_at=_datetime(catalog.get("closed_at")),
        close_reason=str(catalog["close_reason"]) if catalog.get("close_reason") else None,
        command_family="console_turn" if str(catalog.get("origin_kind") or "").strip() == "console" else "live_control",
        loop_mode=str(catalog.get("loop_mode") or "assist"),
        permission_mode=str(catalog.get("permission_mode") or "bypass"),
        primary_thread_id=UUID(str(primary_thread_id)) if primary_thread_id else None,
        catalog_facts=facts,
    )


def get_live_control_grant(
    db: Session,
    *,
    session_id: UUID | str,
    capability: str,
    now: datetime | None = None,
) -> LiveControlGrant | None:
    """Return the exact grant on the primary latest open run, or fail closed."""

    column = {
        "send": LiveSessionConnection.can_send_input,
        "interrupt": LiveSessionConnection.can_interrupt,
        "terminate": LiveSessionConnection.can_terminate,
    }.get(capability)
    if column is None:
        raise ValueError(f"unknown live control capability: {capability}")
    observed_at = normalize_utc(now) or datetime.now(timezone.utc)
    freshness_cutoff = observed_at - timedelta(milliseconds=DEFAULT_MANAGED_CONTROL_LEASE_TTL_MS)
    latest_open_run = (
        db.query(LiveSessionRun.id)
        .join(LiveSessionThread, LiveSessionThread.id == LiveSessionRun.thread_id)
        .filter(
            LiveSessionThread.session_id == str(session_id),
            LiveSessionThread.is_primary == 1,
            LiveSessionRun.ended_at.is_(None),
        )
        .order_by(LiveSessionRun.started_at.desc(), LiveSessionRun.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    row = (
        db.query(
            LiveSessionConnection.id,
            LiveSessionConnection.run_id,
            LiveSessionConnection.adapter_connection_id,
            LiveSessionConnection.lease_generation,
            LiveSessionConnection.acquired_at,
        )
        .join(LiveSessionRun, LiveSessionRun.id == LiveSessionConnection.run_id)
        .join(LiveSessionThread, LiveSessionThread.id == LiveSessionRun.thread_id)
        .filter(
            LiveSessionThread.session_id == str(session_id),
            LiveSessionThread.is_primary == 1,
            LiveSessionRun.id == latest_open_run,
            LiveSessionRun.ended_at.is_(None),
            LiveSessionConnection.acquisition_kind.in_(_CONTROL_ACQUISITION_KINDS),
            LiveSessionConnection.state == "attached",
            LiveSessionConnection.released_at.is_(None),
            LiveSessionConnection.last_health_at.is_not(None),
            LiveSessionConnection.last_health_at > freshness_cutoff,
            column == 1,
        )
        .order_by(
            LiveSessionConnection.adapter_connection_id.is_not(None).desc(),
            LiveSessionConnection.last_health_at.desc(),
            LiveSessionConnection.id.desc(),
        )
        .limit(1)
        .first()
    )
    if row is None:
        return None
    adapter_connection_id = str(row.adapter_connection_id or "").strip()
    adapter_generation = str(row.lease_generation or "").strip()
    if bool(adapter_connection_id) != bool(adapter_generation):
        return None
    if adapter_connection_id and adapter_generation:
        return LiveControlGrant(
            catalog_connection_id=int(row.id),
            connection_id=adapter_connection_id,
            run_id=str(row.run_id),
            lease_generation=adapter_generation,
            identity_source="adapter_bound",
        )

    acquired_at = normalize_utc(row.acquired_at)
    generation = f"{row.id}:{acquired_at.isoformat()}" if acquired_at is not None else str(row.id)
    return LiveControlGrant(
        catalog_connection_id=int(row.id),
        connection_id=int(row.id),
        run_id=str(row.run_id),
        lease_generation=generation,
        identity_source="legacy_synthetic",
    )


def get_bound_live_control_identity(
    db: Session,
    *,
    session_id: UUID | str,
) -> LiveControlGrant | None:
    """Resolve the current catalog binding without consulting legacy grants."""

    latest_open_run = (
        db.query(LiveSessionRun.id)
        .join(LiveSessionThread, LiveSessionThread.id == LiveSessionRun.thread_id)
        .filter(
            LiveSessionThread.session_id == str(session_id),
            LiveSessionThread.is_primary == 1,
            LiveSessionRun.ended_at.is_(None),
        )
        .order_by(LiveSessionRun.started_at.desc(), LiveSessionRun.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    row = (
        db.query(
            LiveSessionConnection.id,
            LiveSessionConnection.run_id,
            LiveSessionConnection.adapter_connection_id,
            LiveSessionConnection.lease_generation,
        )
        .join(LiveSessionRun, LiveSessionRun.id == LiveSessionConnection.run_id)
        .join(LiveSessionThread, LiveSessionThread.id == LiveSessionRun.thread_id)
        .filter(
            LiveSessionThread.session_id == str(session_id),
            LiveSessionThread.is_primary == 1,
            LiveSessionRun.id == latest_open_run,
            LiveSessionRun.ended_at.is_(None),
            LiveSessionConnection.acquisition_kind.in_(_CONTROL_ACQUISITION_KINDS),
            LiveSessionConnection.state == "attached",
            LiveSessionConnection.released_at.is_(None),
            LiveSessionConnection.adapter_connection_id.is_not(None),
            LiveSessionConnection.lease_generation.is_not(None),
        )
        .order_by(LiveSessionConnection.last_health_at.desc(), LiveSessionConnection.id.desc())
        .limit(1)
        .first()
    )
    if row is None:
        return None
    connection_id = str(row.adapter_connection_id or "").strip()
    generation = str(row.lease_generation or "").strip()
    if not connection_id or not generation:
        return None
    return LiveControlGrant(
        catalog_connection_id=int(row.id),
        connection_id=connection_id,
        run_id=str(row.run_id),
        lease_generation=generation,
        identity_source="adapter_bound",
    )


def get_canonical_live_control_grant(
    db: Session,
    *,
    session_id: UUID | str,
    provider: str,
    device_id: str,
    capability: str,
    now: datetime,
) -> tuple[LiveControlGrant | None, str | None]:
    """Require exact agreement between the catalog lease and reducer evidence."""

    if not shadow_reducer_ingest_enabled():
        return None, "canonical_ingest_disabled"
    operation = {
        "send": "send_input",
        "interrupt": "interrupt",
        "terminate": "terminate",
    }.get(capability)
    if operation is None:
        return None, "unsupported"
    normalized_provider = str(provider or "").strip().lower()
    contract = contract_for_provider(normalized_provider)
    if contract is None or not bool(getattr(contract, operation, False)):
        return None, "unsupported"
    session = db.query(LiveSessionCatalog).filter(LiveSessionCatalog.session_id == str(session_id)).one_or_none()
    if session is None:
        return None, "control_unavailable"
    if session.closed_at is not None or session.ended_at is not None:
        return None, "session_closed"
    if str(session.provider or "").strip().lower() != normalized_provider:
        return None, "identity_diverged"
    if device_id and str(session.device_id or "").strip() != str(device_id).strip():
        return None, "identity_diverged"
    grant = get_bound_live_control_identity(db, session_id=session_id)
    if grant is None:
        return None, "identity_unbound"
    connection_id = str(grant.connection_id)
    subject_key = f"connection:{connection_id}:{grant.lease_generation}"
    heads = [
        dict(row)
        for row in db.execute(
            select(FactHead.__table__)
            .where(FactHead.family == "control", FactHead.subject_key == subject_key)
            .order_by(FactHead.source, FactHead.source_epoch)
        ).mappings()
    ]
    supported_operations = {
        name
        for name in ("send_input", "interrupt", "terminate", "tail_output", "resume")
        if bool(getattr(contract, "can_resume" if name == "resume" else name, False))
    }
    authorization = authorize_exact_control_fact(
        session_id=str(session_id),
        run_id=grant.run_id,
        provider=normalized_provider,
        connection_id=connection_id,
        lease_generation=grant.lease_generation,
        operation=operation,
        heads=heads,
        supported_operations=supported_operations,
        now=now,
    )
    if not authorization.allowed:
        return None, authorization.reason or "control_unavailable"
    return grant, None


def live_control_capability_available(
    db: Session,
    *,
    session_id: UUID | str,
    capability: str,
) -> bool:
    return get_live_control_grant(db, session_id=session_id, capability=capability) is not None


def live_control_session_capability_available(session: LiveControlSession, *, capability: str) -> bool:
    """Project a preflight capability from one catalogd session snapshot."""

    facts = session.catalog_facts
    if not isinstance(facts, dict):
        raise ValueError("session does not contain catalog snapshot facts")
    latest_run = facts.get("latest_run")
    if not isinstance(latest_run, dict) or latest_run.get("ended_at") is not None:
        return False
    connections = facts.get("connections")
    if not isinstance(connections, list) or not connections or not isinstance(connections[0], dict):
        return False
    connection = connections[0]
    column = {
        "send": "can_send_input",
        "interrupt": "can_interrupt",
        "terminate": "can_terminate",
    }.get(capability)
    if column is None:
        raise ValueError(f"unknown live control capability: {capability}")
    return connection.get("state") == "attached" and connection.get("released_at") is None and int(connection.get(column) or 0) == 1


def live_session_input_block_reason(db: Session, session: LiveControlSession) -> str | None:
    if normalize_utc(session.closed_at) is not None:
        return "session_closed"
    if isinstance(session.catalog_facts, dict):
        runtime = session.catalog_facts.get("runtime")
        terminal_state = str(runtime.get("terminal_state") or "").strip() if isinstance(runtime, dict) else ""
        if terminal_state == "user_closed":
            return "session_closed"
        if terminal_state in {"", "finished", "host_expired"}:
            return None
        return "run_ended"
    row = (
        db.query(LiveRuntimeState.terminal_state)
        .filter(LiveRuntimeState.session_id == session.id)
        .order_by(LiveRuntimeState.updated_at.desc(), LiveRuntimeState.runtime_version.desc())
        .first()
    )
    if row is None:
        return None
    terminal_state = str(row[0] or "").strip()
    if terminal_state == "user_closed":
        return "session_closed"
    if terminal_state in {"", "finished", "host_expired"}:
        return None
    return "run_ended"


def live_session_closed_for_input(db: Session, session: LiveControlSession) -> bool:
    """Compatibility predicate for whether the current run rejects new input."""
    return live_session_input_block_reason(db, session) is not None


async def wake_next_live_catalog_input(session_id: UUID | str) -> bool:
    """Claim and dispatch one queued hot receipt after a terminal signal."""

    from zerg.services.catalogd_supervisor import get_catalogd_client
    from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_COMMAND_SEND_TEXT
    from zerg.services.managed_control_dispatcher import dispatch_managed_control_command
    from zerg.services.session_kernel_projection import session_lock_scope_id
    from zerg.services.session_locks import session_lock_manager

    catalogd = get_catalogd_client()
    if catalogd is None:
        return False

    request_id = uuid4().hex
    lock_scope_id = session_lock_scope_id(session_id)
    if not await session_lock_manager.acquire(session_id=lock_scope_id, holder=request_id, ttl_seconds=300):
        return False
    try:
        claimed = await catalogd.call(
            "session.input.claim.v2",
            {"session_id": str(session_id), "delivery_request_id": request_id},
            timeout_seconds=1.0,
        )
    except Exception:
        logger.warning("Failed to claim queued catalog input for session %s", session_id, exc_info=True)
        await session_lock_manager.release(lock_scope_id, request_id)
        return False
    session_payload = claimed.get("session")
    receipt = claimed.get("receipt")
    if claimed.get("claimed") is not True or not isinstance(session_payload, dict) or not isinstance(receipt, dict):
        await session_lock_manager.release(lock_scope_id, request_id)
        return False
    session = LiveControlSession(
        id=UUID(str(session_payload["id"])),
        provider=str(session_payload["provider"]),
        device_id=session_payload.get("device_id"),
        device_name=session_payload.get("device_name"),
        cwd=session_payload.get("cwd"),
        project=session_payload.get("project"),
        git_repo=session_payload.get("git_repo"),
        git_branch=session_payload.get("git_branch"),
        ended_at=session_payload.get("ended_at"),
        closed_at=session_payload.get("closed_at"),
        close_reason=session_payload.get("close_reason"),
        loop_mode=str(session_payload.get("loop_mode") or "assist"),
        permission_mode=str(session_payload.get("permission_mode") or "bypass"),
        primary_thread_id=(UUID(str(session_payload["primary_thread_id"])) if session_payload.get("primary_thread_id") else None),
        command_family="live_control",
    )

    dispatched_at = datetime.now(timezone.utc)
    result = await dispatch_managed_control_command(
        db=None,  # type: ignore[arg-type] -- catalog mode validates through catalogd.
        owner_id=int(receipt["owner_id"]),
        session=session,  # type: ignore[arg-type] -- bounded DTO matches the dispatcher contract.
        timeout_secs=15,
        command_type=MANAGED_CONTROL_COMMAND_SEND_TEXT,
        payload={"text": str(receipt.get("text") or "")},
        request_id=request_id,
        run_id=None,
    )
    data = dict(result.data or {})
    if not result.ok or int(data.get("exit_code", 1)) != 0:
        try:
            await catalogd.call(
                "session.input.finish.v2",
                {
                    "receipt_id": str(receipt["id"]),
                    "delivery_request_id": request_id,
                    "status": "failed",
                    "error": str(result.error or data.get("stderr") or "queued send failed")[:500],
                },
                timeout_seconds=1.0,
            )
        finally:
            await session_lock_manager.release(lock_scope_id, request_id)
        return False

    await catalogd.call(
        "session.input.finish.v2",
        {
            "receipt_id": str(receipt["id"]),
            "delivery_request_id": request_id,
            "status": "delivered",
            "error": None,
        },
        timeout_seconds=1.0,
    )
    from zerg.services.session_chat_impl import _schedule_catalog_lock_release

    _schedule_catalog_lock_release(
        session_id=session.id,
        lock_scope_id=lock_scope_id,
        request_id=request_id,
        dispatched_at=dispatched_at,
    )
    return True


async def run_live_catalog_input_recovery_loop() -> None:
    """Recover queued hot receipts without ever opening the cold database."""

    from zerg.services.catalogd_supervisor import get_catalogd_client

    interval = 5.0
    while True:
        try:
            await asyncio.sleep(interval)
            catalogd = get_catalogd_client()
            if catalogd is None:
                continue
            queued = await catalogd.call("session.input.queued.list.v2", {"limit": 100}, timeout_seconds=1.0)
            for session_id in queued.get("session_ids", []):
                await wake_next_live_catalog_input(session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Live catalog input recovery tick failed")
