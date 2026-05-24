"""Agent heartbeat ingest endpoint.

Receives periodic health check payloads from running engine daemons.
Stores latest heartbeat per device_id, retaining 30 days of history.

Authentication: same X-Agents-Token / device token as the ingest endpoint.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request
from fastapi import Response
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.metrics import agents_heartbeat_payload_bytes
from zerg.metrics import agents_heartbeat_requests_total
from zerg.metrics import agents_heartbeat_snapshot_skipped_total
from zerg.metrics import agents_heartbeat_write_seconds
from zerg.metrics import managed_session_heartbeat_lease_rows_total
from zerg.models.agents import AgentHeartbeat
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionObservation
from zerg.models.agents import SessionRuntimeState
from zerg.models.device_token import DeviceToken
from zerg.observability import get_tracer
from zerg.observability import set_span_attributes
from zerg.services.managed_control_state import mark_missing_managed_control_leases
from zerg.services.managed_control_state import refresh_managed_control_lease_health
from zerg.services.managed_control_state import upsert_managed_control_leases
from zerg.services.session_observations import OBS_KIND_RUNTIME_SIGNAL
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import ingest_runtime_events
from zerg.services.write_serializer import get_write_serializer
from zerg.session_execution_home import ManagedSessionTransport
from zerg.session_execution_home import SessionExecutionHome
from zerg.utils.time import UTCBaseModel
from zerg.utils.time import normalize_utc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])

MANAGED_SESSION_LEASE_SOURCE = "engine_attached_lease"
UNMANAGED_PROCESS_SNAPSHOT_SOURCE = "engine_process_snapshot"
DEFAULT_MANAGED_SESSION_LEASE_TTL_MS = 15 * 60 * 1000
MAX_MANAGED_SESSION_LEASE_TTL_MS = 60 * 60 * 1000
MANAGED_SESSION_LEASE_STATES = {"attached", "detached", "degraded"}
MANAGED_SESSION_LEASE_PHASES = {"idle", "thinking", "running", "blocked", "needs_user", "none"}
MANAGED_SESSION_LEASE_PROVIDERS = {"codex", "claude", "gemini", "opencode", "antigravity"}
CODEX_ROLLOUT_ID_RE = re.compile(r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(.+)$")
# One Codex phase freshness window. A complete process snapshot can close
# unbound sessions only after their last phase/progress signal is no longer
# current.
MISSING_UNBOUND_UNMANAGED_PROVIDERS = {"claude", "codex", "gemini", "antigravity"}
UNBOUND_UNMANAGED_CLOSE_GRACE = timedelta(seconds=90)


class UnmanagedSessionBindingIn(UTCBaseModel):
    """One row of Rust engine's unmanaged-session pid/cwd scan.

    Phase 5 of docs/specs/session-liveness-honesty.md. All fields except
    machine_id, provider, provider_session_id, and observed_at are
    tolerant of absence so the engine can ship partial observations
    (e.g. file-only, no process yet) without breaking heartbeat ingest.
    """

    machine_id: str = Field(..., max_length=255)
    provider: str = Field(..., max_length=64)
    provider_session_id: str = Field(..., max_length=255)
    source_path: str | None = Field(None, max_length=1024)
    source_inode: int | None = None
    source_device: int | None = None
    pid: int | None = None
    process_start_time: datetime | None = None
    cwd: str | None = Field(None, max_length=1024)
    source_offset: int | None = None
    source_mtime: datetime | None = None
    observed_at: datetime


class ManagedSessionLeaseIn(UTCBaseModel):
    session_id: UUID
    provider: str = Field(..., max_length=64)
    machine_id: str | None = Field(None, max_length=255)
    sequence: int = Field(..., ge=0)
    state: str = Field(..., max_length=32)
    phase: str | None = Field(None, max_length=32)
    tool_name: str | None = Field(None, max_length=128)
    bridge_status: str | None = Field(None, max_length=64)
    thread_subscription_status: str | None = Field(None, max_length=64)
    observed_at: datetime | None = None
    lease_ttl_ms: int = Field(DEFAULT_MANAGED_SESSION_LEASE_TTL_MS, ge=1, le=MAX_MANAGED_SESSION_LEASE_TTL_MS)


class ResolvedWorkspaceIn(UTCBaseModel):
    cwd: str | None = Field(None, max_length=1024)
    label: str | None = Field(None, max_length=255)
    branch: str | None = Field(None, max_length=255)


class ResolvedProcessIn(UTCBaseModel):
    pid: int | None = None
    process_start_time: datetime | None = None
    started_at: datetime | None = None


class ResolvedBridgeIn(UTCBaseModel):
    bridge_pid: int | None = None
    app_server_pid: int | None = None
    ws_url: str | None = Field(None, max_length=1024)
    heartbeat_at: datetime | None = None
    status: str | None = Field(None, max_length=64)
    thread_subscription_status: str | None = Field(None, max_length=64)


class ResolvedEvidenceIn(UTCBaseModel):
    process_observed: bool = False
    transcript_observed: bool = False
    bridge_state: str | None = Field(None, max_length=64)
    hook_seen_at: datetime | None = None
    join_keys: list[str] = Field(default_factory=list)


class ResolvedLocalSessionIn(UTCBaseModel):
    session_id: UUID | None = None
    provider: str = Field(..., max_length=64)
    provider_session_id: str | None = Field(None, max_length=255)
    control_path: str = Field(..., max_length=32)
    presentation_state: str = Field(..., max_length=32)
    state: str = Field(..., max_length=32)
    phase: str | None = Field(None, max_length=32)
    tool_name: str | None = Field(None, max_length=128)
    phase_observed_at: datetime | None = None
    last_activity_at: datetime | None = None
    workspace: ResolvedWorkspaceIn = Field(default_factory=ResolvedWorkspaceIn)
    process: ResolvedProcessIn = Field(default_factory=ResolvedProcessIn)
    bridge: ResolvedBridgeIn = Field(default_factory=ResolvedBridgeIn)
    evidence: ResolvedEvidenceIn = Field(default_factory=ResolvedEvidenceIn)
    reason_codes: list[str] = Field(default_factory=list)


class HeartbeatIn(BaseModel):
    """Payload from the engine daemon."""

    version: Optional[str] = None
    daemon_pid: Optional[int] = None
    last_ship_at: Optional[str] = None  # RFC3339 last successful ship or None
    last_ship_attempt_at: Optional[str] = None  # RFC3339 last ship attempt or None
    last_ship_result: Optional[str] = None
    last_ship_latency_ms: Optional[int] = None
    last_ship_http_status: Optional[int] = None
    last_ship_error_kind: Optional[str] = None
    last_ship_error_message: Optional[str] = None
    spool_pending_count: int = 0
    spool_dead_count: int = 0
    parse_error_count_1h: int = 0
    consecutive_ship_failures: int = 0
    ship_attempts_1h: int = 0
    ship_successes_1h: int = 0
    ship_rate_limited_1h: int = 0
    ship_server_errors_1h: int = 0
    ship_payload_rejections_1h: int = 0
    ship_payload_too_large_1h: int = 0
    ship_retryable_client_errors_1h: int = 0
    ship_connect_errors_1h: int = 0
    ship_latency_p50_ms_1h: Optional[int] = None
    ship_latency_p95_ms_1h: Optional[int] = None
    ship_attempts_10m: int = 0
    ship_successes_10m: int = 0
    ship_rate_limited_10m: int = 0
    ship_server_errors_10m: int = 0
    ship_retryable_client_errors_10m: int = 0
    ship_connect_errors_10m: int = 0
    disk_free_bytes: int = 0
    is_offline: bool = False
    managed_sessions: list[ManagedSessionLeaseIn] = Field(default_factory=list)
    # Phase 5 of session-liveness-honesty: unmanaged pid/cwd/source bindings.
    # Optional — older engines don't send this. See UnmanagedSessionBindingIn.
    unmanaged_session_bindings: list[UnmanagedSessionBindingIn] = Field(default_factory=list)
    # Canonical engine-resolved local session snapshot. When present, server
    # ingest prefers this over legacy managed/unmanaged arrays for identity.
    sessions: list[ResolvedLocalSessionIn] = Field(default_factory=list)
    # Stable digest/sequence over canonical session identity/control fields.
    # Older engines omit these, which forces the full compatibility path.
    sessions_digest: str | None = Field(None, max_length=128)
    sessions_sequence: int | None = None


def _managed_lease_provider_label(lease: ManagedSessionLeaseIn) -> str:
    provider = (lease.provider or "").strip().lower()
    return provider if provider in MANAGED_SESSION_LEASE_PROVIDERS else "other"


def _normalize_unmanaged_provider_session_id(provider: str, provider_session_id: str) -> str:
    """Normalize machine-scanner transcript identifiers to runtime session ids.

    Codex rollout filenames include the timestamp prefix
    ``rollout-YYYY-MM-DDTHH-MM-SS-`` while ingested sessions store only the
    provider UUID suffix. New engines normalize before sending; the Runtime
    Host repeats the normalization so older engines still link.
    """
    value = (provider_session_id or "").strip()
    if provider == "codex":
        match = CODEX_ROLLOUT_ID_RE.match(value)
        if match:
            return match.group(1)
    return value


def _managed_lease_state_label(lease: ManagedSessionLeaseIn) -> str:
    state = (lease.state or "").strip().lower()
    return state if state in MANAGED_SESSION_LEASE_STATES else "other"


def _managed_lease_phase_label(lease: ManagedSessionLeaseIn) -> str:
    if lease.phase is None or not str(lease.phase).strip():
        return "none"
    phase = str(lease.phase).strip().lower()
    return phase if phase in MANAGED_SESSION_LEASE_PHASES else "other"


def _record_managed_session_lease(lease: ManagedSessionLeaseIn) -> None:
    managed_session_heartbeat_lease_rows_total.labels(
        provider=_managed_lease_provider_label(lease),
        state=_managed_lease_state_label(lease),
        phase=_managed_lease_phase_label(lease),
    ).inc()


def _resolved_join_key_value(evidence: ResolvedEvidenceIn, prefix: str) -> str | None:
    match_prefix = f"{prefix}="
    for raw_key in evidence.join_keys:
        key = str(raw_key or "").strip()
        if key.startswith(match_prefix):
            return key[len(match_prefix) :] or None
    return None


def _resolved_session_control_path(session: ResolvedLocalSessionIn) -> str:
    return str(session.control_path or "").strip().lower()


def _resolved_session_presentation_state(session: ResolvedLocalSessionIn) -> str:
    return str(session.presentation_state or "").strip().lower()


def _managed_leases_from_resolved_sessions(
    sessions: list[ResolvedLocalSessionIn],
    *,
    device_id: str,
    received_at: datetime,
    legacy_leases: list[ManagedSessionLeaseIn],
) -> list[ManagedSessionLeaseIn]:
    legacy_by_session = {lease.session_id: lease for lease in legacy_leases if lease.session_id is not None}
    sequence = max(int(received_at.timestamp() * 1000), 0)
    leases: list[ManagedSessionLeaseIn] = []
    for session in sessions:
        if _resolved_session_control_path(session) != "managed" or session.session_id is None:
            continue
        legacy = legacy_by_session.get(session.session_id)
        observed_at = session.phase_observed_at or session.last_activity_at or session.bridge.heartbeat_at or received_at
        leases.append(
            ManagedSessionLeaseIn(
                session_id=session.session_id,
                provider=session.provider,
                machine_id=(legacy.machine_id if legacy else None) or device_id,
                sequence=(legacy.sequence if legacy else sequence),
                state=session.state,
                phase=session.phase,
                tool_name=session.tool_name,
                bridge_status=session.bridge.status,
                thread_subscription_status=session.bridge.thread_subscription_status,
                observed_at=observed_at,
                lease_ttl_ms=(legacy.lease_ttl_ms if legacy else DEFAULT_MANAGED_SESSION_LEASE_TTL_MS),
            )
        )
    return leases


def _unmanaged_bindings_from_resolved_sessions(
    sessions: list[ResolvedLocalSessionIn],
    *,
    device_id: str,
    received_at: datetime,
) -> list[UnmanagedSessionBindingIn]:
    bindings: list[UnmanagedSessionBindingIn] = []
    for session in sessions:
        control_path = _resolved_session_control_path(session)
        presentation_state = _resolved_session_presentation_state(session)
        if control_path != "unmanaged" and presentation_state != "unmanaged":
            continue
        provider_session_id = str(session.provider_session_id or "").strip()
        if not provider_session_id:
            continue
        observed_at = session.last_activity_at or session.phase_observed_at or session.evidence.hook_seen_at or received_at
        bindings.append(
            UnmanagedSessionBindingIn(
                machine_id=device_id,
                provider=session.provider,
                provider_session_id=provider_session_id,
                source_path=_resolved_join_key_value(session.evidence, "source_path"),
                pid=session.process.pid,
                process_start_time=session.process.process_start_time or session.process.started_at,
                cwd=session.workspace.cwd,
                observed_at=observed_at,
            )
        )
    return bindings


def _is_managed_codex_session(session: AgentSession | None) -> bool:
    if session is None:
        return False
    if str(session.provider or "").strip().lower() != "codex":
        return False
    execution_home = str(getattr(session, "execution_home", "") or "").strip()
    managed_transport = str(getattr(session, "managed_transport", "") or "").strip()
    return execution_home == SessionExecutionHome.MANAGED_LOCAL.value or managed_transport == ManagedSessionTransport.CODEX_APP_SERVER.value


def _is_managed_session(session: AgentSession | None) -> bool:
    if session is None:
        return False
    execution_home = str(getattr(session, "execution_home", "") or "").strip()
    managed_transport = str(getattr(session, "managed_transport", "") or "").strip()
    return execution_home == SessionExecutionHome.MANAGED_LOCAL.value or bool(managed_transport)


def _runtime_events_for_managed_leases(
    leases: list[ManagedSessionLeaseIn],
    *,
    device_id: str,
    received_at: datetime,
) -> list[RuntimeEventIngest]:
    del device_id, received_at
    for lease in leases:
        _record_managed_session_lease(lease)
    # Managed lease freshness is materialized into ManagedSessionControlState.
    # The runtime reducer still accepts historical engine_attached_lease events,
    # but the default heartbeat path must not synthesize provider phase events
    # merely to keep managed control alive.
    return []


def _clear_synthetic_managed_missing_runtime_on_reattach(
    db: Session,
    leases: list[ManagedSessionLeaseIn],
) -> set[UUID]:
    """Drop synthetic missing-lease runtime terminals when control reattaches."""

    attached_session_ids = {
        lease.session_id for lease in leases if lease.session_id is not None and (lease.state or "").strip().lower() == "attached"
    }
    if not attached_session_ids:
        return set()

    touched_session_ids: set[UUID] = set()
    rows = (
        db.query(SessionRuntimeState)
        .filter(SessionRuntimeState.session_id.in_(attached_session_ids))
        .filter(SessionRuntimeState.terminal_state == "process_gone")
        .filter(SessionRuntimeState.terminal_source == MANAGED_SESSION_LEASE_SOURCE)
        .all()
    )
    for row in rows:
        if row.session_id is None:
            continue
        touched_session_ids.add(row.session_id)
        db.delete(row)
    return touched_session_ids


def _runtime_observation_payload_from_raw(payload_raw: str | dict | None) -> dict:
    if isinstance(payload_raw, dict):
        return payload_raw
    try:
        payload = json.loads(payload_raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _heartbeat_payload_from_raw(payload_raw: str | dict | None) -> dict:
    if isinstance(payload_raw, dict):
        return payload_raw
    try:
        payload = json.loads(payload_raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _latest_heartbeat_sessions_digest(db: Session, device_id: str) -> str | None:
    row = (
        db.query(AgentHeartbeat.raw_json)
        .filter(AgentHeartbeat.device_id == device_id)
        .order_by(AgentHeartbeat.received_at.desc(), AgentHeartbeat.id.desc())
        .first()
    )
    if row is None:
        return None
    raw = _heartbeat_payload_from_raw(row.raw_json)
    digest = str(raw.get("sessions_digest") or "").strip()
    return digest or None


def _managed_lease_session_ids(leases: list[ManagedSessionLeaseIn]) -> set[UUID]:
    return {lease.session_id for lease in leases if lease.session_id is not None}


def _is_synthetic_missing_managed_lease_payload(payload: dict) -> bool:
    if str(payload.get("kind") or "").strip() != "phase_signal":
        return False
    if str(payload.get("phase") or "").strip() != "blocked":
        return False
    if str(payload.get("tool_name") or "").strip() != "control path":
        return False
    event_payload = payload.get("payload")
    return isinstance(event_payload, dict) and event_payload.get("state") == "missing"


def _state_is_synthetic_missing_managed_lease(state: SessionRuntimeState) -> bool:
    if str(state.phase_source or "").strip() != MANAGED_SESSION_LEASE_SOURCE:
        return False
    if str(state.phase or "").strip() != "blocked":
        return False
    return str(state.active_tool or "").strip() == "control path"


def _state_has_real_managed_lease_history(state: SessionRuntimeState) -> bool:
    return str(state.phase_source or "").strip() == MANAGED_SESSION_LEASE_SOURCE and not _state_is_synthetic_missing_managed_lease(state)


def _latest_real_managed_lease_at_for_key(db: Session, runtime_key: str) -> tuple[bool, datetime | None]:
    rows = (
        db.query(
            SessionObservation.observed_at,
            SessionObservation.payload_json,
        )
        .filter(SessionObservation.runtime_key == runtime_key)
        .filter(SessionObservation.source == MANAGED_SESSION_LEASE_SOURCE)
        .filter(SessionObservation.kind == OBS_KIND_RUNTIME_SIGNAL)
        .order_by(SessionObservation.observed_at.desc(), SessionObservation.id.desc())
        .limit(200)
        .all()
    )
    saw_lease_history = False
    for observed_at, payload_json in rows:
        saw_lease_history = True
        payload = _runtime_observation_payload_from_raw(payload_json)
        if _is_synthetic_missing_managed_lease_payload(payload):
            continue
        occurred_at = normalize_utc(observed_at)
        if occurred_at is not None:
            return True, occurred_at
    return saw_lease_history, None


def _runtime_events_for_missing_managed_leases(
    db: Session,
    leases: list[ManagedSessionLeaseIn],
    *,
    device_id: str,
    received_at: datetime,
) -> list[RuntimeEventIngest]:
    observed = {
        ((lease.provider or "").strip().lower(), lease.session_id)
        for lease in leases
        if (lease.provider or "").strip() and lease.session_id is not None
    }
    rows = (
        db.query(SessionRuntimeState, AgentSession)
        .join(AgentSession, SessionRuntimeState.session_id == AgentSession.id)
        .filter(SessionRuntimeState.device_id == device_id)
        .filter(SessionRuntimeState.session_id.isnot(None))
        .filter(SessionRuntimeState.terminal_state.is_(None))
        .all()
    )
    control_by_session: dict[UUID, object] = {}
    legacy_real_lease_at_by_key: dict[str, tuple[bool, datetime | None]] = {}

    events: list[RuntimeEventIngest] = []
    for state, session in rows:
        if not _is_managed_session(session):
            continue
        provider = (state.provider or session.provider or "").strip().lower()
        session_id = state.session_id
        if not provider or session_id is None or (provider, session_id) in observed:
            continue

        control_row = control_by_session.get(session_id)
        lease_history_at = None
        if control_row is not None:
            lease_history_at = normalize_utc(control_row.lease_observed_at) or normalize_utc(
                control_row.last_control_seen_at,
            )

        if lease_history_at is None and _state_has_real_managed_lease_history(state):
            lease_history_at = normalize_utc(state.last_runtime_signal_at) or normalize_utc(state.timeline_anchor_at)

        if lease_history_at is None and _state_is_synthetic_missing_managed_lease(state):
            if state.runtime_key not in legacy_real_lease_at_by_key:
                legacy_real_lease_at_by_key[state.runtime_key] = _latest_real_managed_lease_at_for_key(
                    db,
                    state.runtime_key,
                )
            saw_legacy_history, legacy_real_lease_at = legacy_real_lease_at_by_key[state.runtime_key]
            if saw_legacy_history:
                lease_history_at = legacy_real_lease_at or normalize_utc(state.timeline_anchor_at)

        if lease_history_at is None:
            continue

        timeline_anchor_at = (
            lease_history_at
            or normalize_utc(state.timeline_anchor_at)
            or normalize_utc(session.last_activity_at)
            or normalize_utc(state.last_progress_at)
            or normalize_utc(state.last_live_at)
            or received_at
        )
        events.append(
            RuntimeEventIngest(
                runtime_key=state.runtime_key,
                session_id=session_id,
                provider=provider,
                device_id=device_id,
                source=MANAGED_SESSION_LEASE_SOURCE,
                kind="terminal_signal",
                occurred_at=received_at,
                dedupe_key=(
                    f"engine-managed-missing-terminal:{device_id}:{session_id}:"
                    f"{int(state.runtime_version or 0)}:{timeline_anchor_at.isoformat()}"
                ),
                payload={
                    "terminal_state": "process_gone",
                    "terminal_reason": "process_gone",
                    "terminal_source": MANAGED_SESSION_LEASE_SOURCE,
                    "timeline_anchor_at": timeline_anchor_at.isoformat(),
                },
            )
        )

    return events


def _has_final_managed_codex_terminal(db: Session, session_id: UUID) -> bool:
    return (
        db.query(SessionRuntimeState.runtime_key)
        .filter(SessionRuntimeState.session_id == session_id)
        .filter(SessionRuntimeState.terminal_state == "session_ended")
        .first()
        is not None
    )


def _upsert_unmanaged_session_bindings(
    db: Session,
    bindings: list[UnmanagedSessionBindingIn],
    *,
    device_id: str,
    received_at: datetime,
) -> set[UUID]:
    """Compatibility stub: the legacy ``UnmanagedSessionBinding`` table is gone.

    The kernel writes ``SessionConnection`` rows for observe-only/log_tail
    evidence; bare unmanaged binding ingest is a no-op until that path lands.
    """

    del db, bindings, device_id, received_at
    return set()


def _mark_missing_unmanaged_session_bindings_stale(
    db: Session,
    bindings: list[UnmanagedSessionBindingIn],
    *,
    device_id: str,
) -> set[UUID]:
    """Compatibility stub for the deleted ``UnmanagedSessionBinding`` table."""

    del db, bindings, device_id
    return set()


def _runtime_events_for_missing_unbound_unmanaged_sessions(
    db: Session,
    bindings: list[UnmanagedSessionBindingIn],
    *,
    device_id: str,
    received_at: datetime,
) -> list[RuntimeEventIngest]:
    """Close stale local sessions absent from a complete process snapshot.

    Most unmanaged lifecycle truth flows through ``unmanaged_session_bindings``:
    once a binding has ever been observed, omission from a later complete
    snapshot marks that binding stale. Very short aborted sessions can produce
    transcript/phase events without ever being caught by the fd scan. When the
    engine explicitly sends a complete snapshot for providers whose local
    process identity is covered by the engine, close absent stale sessions
    instead of leaving them "unknown" forever.
    """
    observed_keys: set[tuple[str, str]] = set()
    for binding in bindings:
        provider = (binding.provider or "").strip().lower()
        session_key = _normalize_unmanaged_provider_session_id(
            provider,
            (binding.provider_session_id or "").strip(),
        )
        if provider in MISSING_UNBOUND_UNMANAGED_PROVIDERS and session_key:
            observed_keys.add((provider, session_key))

    # The legacy UnmanagedSessionBinding table is gone; treat every
    # session that has runtime state as eligible (kernel ``SessionConnection``
    # ingest will replace this path soon).
    existing_binding_keys: set[tuple[str, str]] = set()

    rows = (
        db.query(SessionRuntimeState, AgentSession)
        .join(AgentSession, SessionRuntimeState.session_id == AgentSession.id)
        .filter(SessionRuntimeState.device_id == device_id)
        .filter(SessionRuntimeState.session_id.isnot(None))
        .filter(SessionRuntimeState.terminal_state.is_(None))
        .filter(AgentSession.provider.in_(MISSING_UNBOUND_UNMANAGED_PROVIDERS))
        .all()
    )

    events: list[RuntimeEventIngest] = []
    for state, session in rows:
        if _is_managed_session(session):
            continue
        provider = str(session.provider or state.provider or "").strip().lower()
        if not str(session.provider_session_id or "").strip():
            continue
        session_key = _normalize_unmanaged_provider_session_id(
            provider,
            str(session.provider_session_id or "").strip(),
        )
        if not provider or not session_key:
            continue
        key = (provider, session_key)
        if key in observed_keys or key in existing_binding_keys:
            continue

        freshness_expires_at = normalize_utc(state.freshness_expires_at)
        if freshness_expires_at is not None and freshness_expires_at > received_at:
            continue

        latest_signal_at = max(
            (
                ts
                for ts in (
                    normalize_utc(session.last_activity_at),
                    normalize_utc(state.last_progress_at),
                    normalize_utc(state.last_runtime_signal_at),
                )
                if ts is not None
            ),
            default=None,
        )
        if latest_signal_at is not None and received_at - latest_signal_at < UNBOUND_UNMANAGED_CLOSE_GRACE:
            continue

        timeline_anchor_at = (
            normalize_utc(state.timeline_anchor_at)
            or latest_signal_at
            or normalize_utc(session.last_activity_at)
            or normalize_utc(session.started_at)
            or received_at
        )
        session_id = state.session_id
        if session_id is None:
            continue
        events.append(
            RuntimeEventIngest(
                runtime_key=state.runtime_key,
                session_id=session_id,
                provider=provider,
                device_id=device_id,
                source=UNMANAGED_PROCESS_SNAPSHOT_SOURCE,
                kind="terminal_signal",
                occurred_at=received_at,
                dedupe_key=(
                    f"engine-unmanaged-unbound-missing-terminal:{device_id}:{session_id}:"
                    f"{int(state.runtime_version or 0)}:{timeline_anchor_at.isoformat()}"
                ),
                payload={
                    "terminal_state": "process_gone",
                    "terminal_reason": "process_gone",
                    "terminal_source": UNMANAGED_PROCESS_SNAPSHOT_SOURCE,
                    "timeline_anchor_at": timeline_anchor_at.isoformat(),
                },
            )
        )

    return events


@router.post("/heartbeat", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def ingest_heartbeat(
    payload: HeartbeatIn,
    request: Request,
    db: Session = Depends(get_db),
    _token: DeviceToken | None = Depends(verify_agents_token),
) -> Response:
    """Accept a heartbeat from an engine daemon.

    Upserts (inserts) a new heartbeat row per device. History is retained
    for 30 days; older rows are cleaned up by the stale agent detection job.
    """
    tracer = get_tracer(__name__)
    auth_kind_label = "device_token" if _token is not None else "none"
    request_status_label = "internal_error"
    with tracer.start_as_current_span("longhouse.heartbeat") as span:
        set_span_attributes(
            span,
            {
                "http.route": "/api/agents/heartbeat",
                "longhouse.heartbeat.auth_kind": auth_kind_label,
            },
        )

        try:
            with tracer.start_as_current_span("longhouse.heartbeat.validate") as validate_span:
                # Determine device_id: prefer device token, fall back to request metadata
                device_id: str
                if _token is not None:
                    device_id = _token.device_id or f"device:{_token.id}"
                else:
                    # Dev mode or legacy token — use IP as proxy
                    device_id = request.client.host if request.client else "unknown"

                last_ship_at: datetime | None = None
                if payload.last_ship_at:
                    try:
                        last_ship_at = datetime.fromisoformat(payload.last_ship_at.replace("Z", "+00:00"))
                    except ValueError:
                        pass
                last_ship_attempt_at: datetime | None = None
                if payload.last_ship_attempt_at:
                    try:
                        last_ship_attempt_at = datetime.fromisoformat(payload.last_ship_attempt_at.replace("Z", "+00:00"))
                    except ValueError:
                        pass

                wire_bytes = len(await request.body())
                payload_json = json.dumps(payload.model_dump(mode="json"))
                agents_heartbeat_payload_bytes.observe(wire_bytes)
                set_span_attributes(
                    validate_span,
                    {
                        "longhouse.device.id": device_id,
                        "longhouse.build.version": payload.version,
                        "longhouse.heartbeat.last_ship_attempt_at": payload.last_ship_attempt_at,
                        "longhouse.heartbeat.last_ship_result": payload.last_ship_result,
                        "longhouse.heartbeat.last_ship_error_kind": payload.last_ship_error_kind,
                        "longhouse.heartbeat.ship_attempts_1h": payload.ship_attempts_1h,
                        "longhouse.heartbeat.spool_pending_count": payload.spool_pending_count,
                        "longhouse.heartbeat.spool_dead_count": payload.spool_dead_count,
                        "longhouse.heartbeat.payload_bytes_wire": wire_bytes,
                        "longhouse.heartbeat.is_offline": payload.is_offline,
                    },
                )
                set_span_attributes(
                    span,
                    {
                        "longhouse.device.id": device_id,
                        "longhouse.build.version": payload.version,
                        "longhouse.heartbeat.is_offline": payload.is_offline,
                    },
                )

            _device_id = device_id
            _payload_json = payload_json
            _now = datetime.now(timezone.utc)
            _version = payload.version
            _last_ship = last_ship_at
            _last_ship_attempt = last_ship_attempt_at
            _last_ship_result = payload.last_ship_result
            _last_ship_latency_ms = payload.last_ship_latency_ms
            _last_ship_http_status = payload.last_ship_http_status
            _spool = payload.spool_pending_count
            _spool_dead = payload.spool_dead_count
            _parse_err = payload.parse_error_count_1h
            _consec = payload.consecutive_ship_failures
            _ship_attempts = payload.ship_attempts_1h
            _ship_successes = payload.ship_successes_1h
            _ship_rate_limited = payload.ship_rate_limited_1h
            _ship_server_errors = payload.ship_server_errors_1h
            _ship_payload_rejections = payload.ship_payload_rejections_1h
            _ship_payload_too_large = payload.ship_payload_too_large_1h
            _ship_retryable_client_errors = payload.ship_retryable_client_errors_1h
            _ship_connect_errors = payload.ship_connect_errors_1h
            _ship_latency_p50 = payload.ship_latency_p50_ms_1h
            _ship_latency_p95 = payload.ship_latency_p95_ms_1h
            _disk = payload.disk_free_bytes
            _offline = 1 if payload.is_offline else 0
            _resolved_sessions = payload.sessions
            _resolved_sessions_present = "sessions" in payload.model_fields_set
            _managed_leases = (
                _managed_leases_from_resolved_sessions(
                    _resolved_sessions,
                    device_id=_device_id,
                    received_at=_now,
                    legacy_leases=payload.managed_sessions,
                )
                if _resolved_sessions_present
                else payload.managed_sessions
            )
            _managed_leases_present = _resolved_sessions_present or "managed_sessions" in payload.model_fields_set
            _unmanaged_bindings = (
                _unmanaged_bindings_from_resolved_sessions(
                    _resolved_sessions,
                    device_id=_device_id,
                    received_at=_now,
                )
                if _resolved_sessions_present
                else payload.unmanaged_session_bindings
            )
            _unmanaged_bindings_present = _resolved_sessions_present or "unmanaged_session_bindings" in payload.model_fields_set

            def _do_heartbeat(write_db: Session) -> dict[UUID, tuple[str | None, str]]:
                publish_sessions: dict[UUID, tuple[str | None, str]] = {}
                managed_snapshot_skip = False
                incoming_sessions_digest = str(payload.sessions_digest or "").strip() or None
                if _managed_leases_present and incoming_sessions_digest is not None:
                    managed_snapshot_skip = _latest_heartbeat_sessions_digest(write_db, _device_id) == incoming_sessions_digest
                hb = AgentHeartbeat(
                    device_id=_device_id,
                    received_at=_now,
                    version=_version,
                    last_ship_at=_last_ship,
                    last_ship_attempt_at=_last_ship_attempt,
                    last_ship_result=_last_ship_result,
                    last_ship_latency_ms=_last_ship_latency_ms,
                    last_ship_http_status=_last_ship_http_status,
                    spool_pending=_spool,
                    spool_dead=_spool_dead,
                    parse_errors_1h=_parse_err,
                    consecutive_failures=_consec,
                    ship_attempts_1h=_ship_attempts,
                    ship_successes_1h=_ship_successes,
                    ship_rate_limited_1h=_ship_rate_limited,
                    ship_server_errors_1h=_ship_server_errors,
                    ship_payload_rejections_1h=_ship_payload_rejections,
                    ship_payload_too_large_1h=_ship_payload_too_large,
                    ship_retryable_client_errors_1h=_ship_retryable_client_errors,
                    ship_connect_errors_1h=_ship_connect_errors,
                    ship_latency_p50_ms_1h=_ship_latency_p50,
                    ship_latency_p95_ms_1h=_ship_latency_p95,
                    disk_free_bytes=_disk,
                    is_offline=_offline,
                    raw_json=_payload_json,
                )
                write_db.add(hb)
                cutoff = _now - timedelta(days=30)
                write_db.query(AgentHeartbeat).filter(
                    AgentHeartbeat.device_id == _device_id,
                    AgentHeartbeat.received_at < cutoff,
                ).delete()
                if _unmanaged_bindings:
                    for session_id in _upsert_unmanaged_session_bindings(
                        write_db,
                        _unmanaged_bindings,
                        device_id=_device_id,
                        received_at=_now,
                    ):
                        publish_sessions.setdefault(
                            session_id,
                            (None, UNMANAGED_PROCESS_SNAPSHOT_SOURCE),
                        )
                if _unmanaged_bindings_present:
                    for session_id in _mark_missing_unmanaged_session_bindings_stale(
                        write_db,
                        _unmanaged_bindings,
                        device_id=_device_id,
                    ):
                        publish_sessions.setdefault(
                            session_id,
                            (None, UNMANAGED_PROCESS_SNAPSHOT_SOURCE),
                        )
                managed_snapshot_refreshed_ids: set[UUID] = set()
                if managed_snapshot_skip:
                    managed_snapshot_refreshed_ids = refresh_managed_control_lease_health(
                        write_db,
                        _managed_leases,
                        device_id=_device_id,
                        received_at=_now,
                    )
                    seen_ids = _managed_lease_session_ids(_managed_leases)
                    if not seen_ids or seen_ids.issubset(managed_snapshot_refreshed_ids):
                        agents_heartbeat_snapshot_skipped_total.labels(
                            reason="unchanged_sessions_digest",
                        ).inc()
                    else:
                        managed_snapshot_skip = False
                        managed_snapshot_refreshed_ids.clear()
                for session_id in managed_snapshot_refreshed_ids:
                    publish_sessions.setdefault(
                        session_id,
                        (None, MANAGED_SESSION_LEASE_SOURCE),
                    )
                if _managed_leases and not managed_snapshot_skip:
                    for session_id in upsert_managed_control_leases(
                        write_db,
                        _managed_leases,
                        device_id=_device_id,
                        received_at=_now,
                    ):
                        publish_sessions.setdefault(
                            session_id,
                            (None, MANAGED_SESSION_LEASE_SOURCE),
                        )
                    for session_id in _clear_synthetic_managed_missing_runtime_on_reattach(
                        write_db,
                        _managed_leases,
                    ):
                        publish_sessions.setdefault(
                            session_id,
                            (None, MANAGED_SESSION_LEASE_SOURCE),
                        )
                if _managed_leases_present and not managed_snapshot_skip:
                    for session_id in mark_missing_managed_control_leases(
                        write_db,
                        _managed_leases,
                        device_id=_device_id,
                        received_at=_now,
                    ):
                        publish_sessions.setdefault(
                            session_id,
                            (None, MANAGED_SESSION_LEASE_SOURCE),
                        )
                runtime_events = _runtime_events_for_managed_leases(
                    _managed_leases,
                    device_id=_device_id,
                    received_at=_now,
                )
                if _managed_leases_present and not managed_snapshot_skip:
                    runtime_events.extend(
                        _runtime_events_for_missing_managed_leases(
                            write_db,
                            _managed_leases,
                            device_id=_device_id,
                            received_at=_now,
                        )
                    )
                if _unmanaged_bindings_present:
                    runtime_events.extend(
                        _runtime_events_for_missing_unbound_unmanaged_sessions(
                            write_db,
                            _unmanaged_bindings,
                            device_id=_device_id,
                            received_at=_now,
                        )
                    )
                if runtime_events:
                    ingest_result = ingest_runtime_events(write_db, runtime_events)
                    updated_runtime_keys = set(ingest_result.updated_runtime_keys)
                    for event in runtime_events:
                        if event.session_id is not None and event.runtime_key in updated_runtime_keys:
                            # Runtime state is the more specific signal when both runtime and binding snapshots touch
                            # the same session in one heartbeat.
                            publish_sessions[event.session_id] = (event.provider, event.source)
                for lease in _managed_leases:
                    if (lease.state or "").strip().lower() != "attached":
                        continue
                    session = write_db.query(AgentSession).filter(AgentSession.id == lease.session_id).first()
                    if (
                        _is_managed_codex_session(session)
                        and session.ended_at is not None
                        and not _has_final_managed_codex_terminal(write_db, lease.session_id)
                    ):
                        session.ended_at = None
                        if lease.session_id is not None:
                            publish_sessions.setdefault(
                                lease.session_id,
                                (lease.provider, MANAGED_SESSION_LEASE_SOURCE),
                            )
                return publish_sessions

            ws = get_write_serializer()
            with tracer.start_as_current_span("longhouse.heartbeat.write") as write_span:
                write_started = time.monotonic()
                publish_sessions = await ws.execute_or_direct(_do_heartbeat, db, label="heartbeat")
                write_ms = round((time.monotonic() - write_started) * 1000, 1)
                agents_heartbeat_write_seconds.observe(write_ms / 1000.0)
                set_span_attributes(
                    write_span,
                    {
                        "longhouse.device.id": _device_id,
                        "longhouse.heartbeat.write_ms": write_ms,
                    },
                )

            if publish_sessions:
                from zerg.services.session_pubsub import publish_session_runtime_update

                for session_id, (provider, source) in sorted(
                    publish_sessions.items(),
                    key=lambda item: str(item[0]),
                ):
                    publish_session_runtime_update(
                        session_id=str(session_id),
                        provider=provider,
                        source=source,
                    )

            request_status_label = "ok"
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        except Exception:
            logger.exception("Failed to ingest heartbeat")
            request_status_label = "internal_error"
            raise
        finally:
            agents_heartbeat_requests_total.labels(
                auth_kind=auth_kind_label,
                status=request_status_label,
            ).inc()
