"""Session response models and builders.

Shared data layer for converting ORM session/event objects into API response
models.  Both the ``agents`` and ``timeline`` router families import from here
— no router should ever import response models from another router.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from uuid import UUID

from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionPresence
from zerg.services.agents_store import AgentsStore
from zerg.services.managed_local_transport import build_managed_local_attach_command
from zerg.services.session_capabilities import build_session_capabilities
from zerg.services.session_runtime import SessionRuntimeView
from zerg.services.session_runtime import build_fallback_runtime_view
from zerg.services.session_runtime import build_runtime_view
from zerg.services.session_runtime import load_runtime_state_map  # noqa: F401 — re-exported
from zerg.services.session_runtime import should_include_runtime_view
from zerg.session_execution_home import ManagedSessionTransport
from zerg.session_loop_mode import SessionLoopMode
from zerg.utils.time import UTCBaseModel

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------


def _coerce_session_loop_mode(value: str | None) -> SessionLoopMode:
    try:
        return SessionLoopMode(value or SessionLoopMode.MANUAL.value)
    except ValueError:
        return SessionLoopMode.MANUAL


def build_attach_command(session: AgentSession) -> str | None:
    return build_managed_local_attach_command(session=session)


def _coerce_managed_launch_profile(value: Any) -> ManagedLaunchProfileResponse | None:
    if not isinstance(value, dict):
        return None
    required_commands = value.get("required_commands")
    argv = value.get("argv")
    exported_env_keys = value.get("exported_env_keys")
    if not isinstance(required_commands, list) or not isinstance(argv, list) or not isinstance(exported_env_keys, list):
        return None
    if not all(isinstance(item, str) for item in required_commands + argv + exported_env_keys):
        return None
    return ManagedLaunchProfileResponse(
        required_commands=required_commands,
        argv=argv,
        exported_env_keys=exported_env_keys,
    )


def build_session_capabilities_response(
    session: AgentSession | None = None,
    *,
    capability_flags=None,
) -> SessionCapabilitiesResponse:
    capability_flags = capability_flags or build_session_capabilities(session)
    return SessionCapabilitiesResponse(
        live_control_available=capability_flags.live_control_available,
        cloud_branch_available=capability_flags.cloud_branch_available,
        host_reattach_available=capability_flags.host_reattach_available,
        reply_to_live_session_available=capability_flags.reply_to_live_session_available,
    )


def build_session_control_response(
    session: AgentSession | None,
    *,
    capability_flags=None,
) -> SessionControlResponse | None:
    if session is None:
        return None
    capability_flags = capability_flags or build_session_capabilities(session)
    source_runner_name = str(getattr(session, "source_runner_name", "") or "").strip() or None
    attach_command = build_attach_command(session) if capability_flags.host_reattach_available else None
    managed_launch_profile = _coerce_managed_launch_profile(getattr(session, "managed_launch_profile", None))
    if (
        capability_flags.managed_transport is None
        and getattr(session, "source_runner_id", None) is None
        and source_runner_name is None
        and attach_command is None
        and managed_launch_profile is None
    ):
        return None
    return SessionControlResponse(
        managed_transport=capability_flags.managed_transport,
        source_runner_id=getattr(session, "source_runner_id", None),
        source_runner_name=source_runner_name,
        attach_command=attach_command,
        managed_launch_profile=managed_launch_profile,
    )


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class ManagedLaunchProfileResponse(BaseModel):
    required_commands: List[str] = Field(..., description="Commands that must exist before managed launch")
    argv: List[str] = Field(..., description="Structured argv Longhouse resolved for the managed launch")
    exported_env_keys: List[str] = Field(..., description="Env var names Longhouse exported for the launch")


class SessionControlResponse(BaseModel):
    managed_transport: Optional[ManagedSessionTransport] = Field(
        None,
        description="Managed transport when Longhouse owns the session runtime",
    )
    source_runner_id: Optional[int] = Field(None, description="Runner id for managed local sessions")
    source_runner_name: Optional[str] = Field(None, description="Runner name for managed local sessions")
    attach_command: Optional[str] = Field(None, description="Local reattach command for managed-local sessions")
    managed_launch_profile: Optional[ManagedLaunchProfileResponse] = Field(
        None,
        description="Structured managed-launch metadata for debugging tmux-backed sessions",
    )


class SessionCapabilitiesResponse(BaseModel):
    live_control_available: bool = Field(False, description="True when Longhouse can inject into the live session now")
    cloud_branch_available: bool = Field(
        False,
        description="True when Longhouse currently supports starting or keeping a cloud branch from this session",
    )
    host_reattach_available: bool = Field(False, description="True when this session can be resumed from its host terminal")
    reply_to_live_session_available: bool = Field(
        False,
        description="True when operator flows may send a direct reply into the live session",
    )


class SessionResponse(UTCBaseModel):
    """Response for a single session."""

    id: str = Field(..., description="Session UUID")
    provider: str = Field(..., description="AI provider")
    project: Optional[str] = Field(None, description="Project name")
    device_id: Optional[str] = Field(None, description="Device ID")
    environment: Optional[str] = Field(None, description="Environment (production, development, test, e2e, commis)")
    cwd: Optional[str] = Field(None, description="Working directory")
    git_repo: Optional[str] = Field(None, description="Git remote URL")
    git_branch: Optional[str] = Field(None, description="Git branch")
    started_at: datetime = Field(..., description="Session start time")
    ended_at: Optional[datetime] = Field(None, description="Session end time")
    user_messages: int = Field(..., description="User message count")
    assistant_messages: int = Field(..., description="Assistant message count")
    tool_calls: int = Field(..., description="Tool call count")
    last_activity_at: Optional[datetime] = Field(None, description="Most recent transcript activity timestamp")
    timeline_anchor_at: Optional[datetime] = Field(None, description="Recency anchor used for timeline ordering")
    runtime_phase: Optional[str] = Field(None, description="Canonical runtime phase")
    phase_started_at: Optional[datetime] = Field(None, description="When the current runtime phase began")
    last_progress_at: Optional[datetime] = Field(None, description="Most recent progress signal timestamp")
    runtime_source: Optional[str] = Field(None, description="Materialized runtime source: semantic|progress|fallback")
    terminal_state: Optional[str] = Field(None, description="Terminal runtime state when known")
    runtime_version: Optional[int] = Field(None, description="Monotonic runtime version for patch ordering")
    status: Optional[str] = Field(None, description="Derived runtime status (working, active, idle, completed)")
    presence_state: Optional[str] = Field(None, description="Fresh presence signal when available")
    presence_tool: Optional[str] = Field(None, description="Tool currently executing (when applicable)")
    presence_updated_at: Optional[datetime] = Field(None, description="When presence was last signalled")
    last_live_at: Optional[datetime] = Field(None, description="Most recent live-signal timestamp")
    display_phase: Optional[str] = Field(None, description="User-facing runtime phase label")
    active_tool: Optional[str] = Field(None, description="Active tool label for runtime display")
    confidence: Optional[str] = Field(None, description="Runtime confidence: live|inferred|stale")
    summary: Optional[str] = Field(None, description="Session summary")
    summary_title: Optional[str] = Field(None, description="Short session title")
    first_user_message: Optional[str] = Field(None, description="First user message (truncated)")
    match_event_id: Optional[int] = Field(None, description="Matching event id for search queries")
    match_snippet: Optional[str] = Field(None, description="Snippet of matching content")
    match_role: Optional[str] = Field(None, description="Role for matching event")
    match_score: Optional[float] = Field(None, description="Semantic similarity score (0-1) when result is from vector search")
    thread_root_session_id: str = Field(..., description="Logical thread root session UUID")
    thread_head_session_id: str = Field(..., description="Current writable head session UUID")
    thread_continuation_count: int = Field(..., description="Number of concrete continuations in this logical thread")
    continued_from_session_id: Optional[str] = Field(None, description="Parent continuation session UUID")
    continuation_kind: Optional[str] = Field(None, description="Continuation kind: local|cloud|runner")
    origin_label: Optional[str] = Field(None, description="User-facing execution origin label")
    home_label: Optional[str] = Field(None, description="User-facing home label, e.g. On this Mac|Hosted|Moved to cloud")
    branched_from_event_id: Optional[int] = Field(None, description="Event id where this continuation branched")
    is_writable_head: bool = Field(False, description="True when this session is the current writable head")
    is_sidechain: bool = Field(False, description="True when session is a Task sub-agent (not human-initiated)")
    control: Optional[SessionControlResponse] = Field(None, description="Host-control and managed-launch debugging detail")
    capabilities: SessionCapabilitiesResponse = Field(..., description="Canonical session capability flags")
    loop_mode: SessionLoopMode = Field(SessionLoopMode.MANUAL, description="Session loop mode: manual|assist|autopilot")
    user_state: str = Field("active", description="User classification: active|parked|snoozed|archived")


class SessionSummaryResponse(UTCBaseModel):
    """Response for session summaries (picker UI)."""

    id: str = Field(..., description="Session UUID")
    project: Optional[str] = Field(None, description="Project name")
    provider: str = Field(..., description="AI provider")
    cwd: Optional[str] = Field(None, description="Working directory")
    git_branch: Optional[str] = Field(None, description="Git branch")
    started_at: datetime = Field(..., description="Session start time")
    ended_at: Optional[datetime] = Field(None, description="Session end time")
    duration_minutes: Optional[int] = Field(None, description="Duration in minutes")
    turn_count: int = Field(..., description="Number of user messages (exchanges)")
    last_user_message: Optional[str] = Field(None, description="Last user message (truncated)")
    last_ai_message: Optional[str] = Field(None, description="Last assistant message (truncated)")


class SessionsSummaryResponse(BaseModel):
    """Response for session summary list."""

    sessions: List[SessionSummaryResponse]
    total: int


class SessionsListResponse(BaseModel):
    """Response for session list."""

    sessions: List[SessionResponse]
    total: int
    has_real_sessions: bool = Field(
        True,
        description="True if any non-demo sessions exist (device_id != 'demo-mac'). " "False means only demo-seeded data is present.",
    )


class SessionThreadResponse(BaseModel):
    """Response for a logical thread and its concrete continuations."""

    root_session_id: str
    head_session_id: str
    sessions: List[SessionResponse]


class SessionPreviewMessage(UTCBaseModel):
    """Preview message entry for session picker."""

    role: str = Field(..., description="Message role")
    content: str = Field(..., description="Message content")
    timestamp: datetime = Field(..., description="Message timestamp")


class SessionPreviewResponse(BaseModel):
    """Response for session preview endpoint."""

    id: str = Field(..., description="Session UUID")
    messages: List[SessionPreviewMessage] = Field(..., description="Recent messages")
    total_messages: int = Field(..., description="Total message count")


class ActiveSessionResponse(UTCBaseModel):
    """Response for active session summary (Live Sessions UI)."""

    id: str = Field(..., description="Session UUID")
    project: Optional[str] = Field(None, description="Project name")
    provider: str = Field(..., description="AI provider")
    cwd: Optional[str] = Field(None, description="Working directory")
    git_branch: Optional[str] = Field(None, description="Git branch")
    started_at: datetime = Field(..., description="Session start time")
    ended_at: Optional[datetime] = Field(None, description="Session end time")
    last_activity_at: datetime = Field(..., description="Most recent transcript activity timestamp")
    timeline_anchor_at: datetime = Field(..., description="Recency anchor used for live ordering")
    runtime_phase: Optional[str] = Field(None, description="Canonical runtime phase")
    phase_started_at: Optional[datetime] = Field(None, description="When the current runtime phase began")
    last_progress_at: Optional[datetime] = Field(None, description="Most recent progress signal timestamp")
    runtime_source: Optional[str] = Field(None, description="Materialized runtime source: semantic|progress|fallback")
    terminal_state: Optional[str] = Field(None, description="Terminal runtime state when known")
    runtime_version: Optional[int] = Field(None, description="Monotonic runtime version for patch ordering")
    status: str = Field(..., description="Session status (working, active, idle, completed)")
    attention: str = Field(..., description="Attention level (auto by default)")
    duration_minutes: int = Field(..., description="Duration in minutes")
    last_user_message: Optional[str] = Field(None, description="Last user message (truncated)")
    last_assistant_message: Optional[str] = Field(None, description="Last assistant message (truncated)")
    message_count: int = Field(..., description="Total user + assistant messages")
    tool_calls: int = Field(..., description="Tool call count")
    presence_state: Optional[str] = Field(None, description="Real-time state: thinking|running|idle|needs_user|blocked")
    presence_tool: Optional[str] = Field(None, description="Tool currently executing (when state=running or blocked)")
    presence_updated_at: Optional[datetime] = Field(None, description="When presence was last signalled")
    last_live_at: Optional[datetime] = Field(None, description="Most recent live-signal timestamp")
    display_phase: Optional[str] = Field(None, description="User-facing runtime phase label")
    active_tool: Optional[str] = Field(None, description="Active tool label for runtime display")
    confidence: Optional[str] = Field(None, description="Runtime confidence: live|inferred|stale")
    user_state: str = Field("active", description="User classification: active|parked|snoozed|archived")
    home_label: Optional[str] = Field(None, description="User-facing home label, e.g. On this Mac|Hosted|Moved to cloud")
    control: Optional[SessionControlResponse] = Field(None, description="Host-control and managed-launch debugging detail")
    capabilities: SessionCapabilitiesResponse = Field(..., description="Canonical session capability flags")
    loop_mode: SessionLoopMode = Field(SessionLoopMode.MANUAL, description="Session loop mode: manual|assist|autopilot")


class ActiveSessionsResponse(UTCBaseModel):
    """Response for active session list."""

    sessions: List[ActiveSessionResponse]
    total: int
    last_refresh: datetime


class WallSessionResponse(UTCBaseModel):
    """A session's raw signal for the wall view. Schema-on-read: raw timestamps,
    no status bucketing. The consuming agent or UI decides relevance."""

    session_id: str
    device_name: Optional[str] = None
    device_id: Optional[str] = None
    cwd: Optional[str] = None
    git_repo: Optional[str] = None
    git_branch: Optional[str] = None
    project: Optional[str] = None
    provider: str
    summary_title: Optional[str] = None
    started_at: Optional[datetime] = None
    last_event_at: Optional[datetime] = None
    last_user_message_at: Optional[datetime] = None
    last_tool_call_at: Optional[datetime] = None
    has_live_presence: bool = False
    presence_state: Optional[str] = None
    pending_inbound_messages: int = 0
    user_messages: int = 0
    assistant_messages: int = 0
    tool_calls: int = 0


class WallResponse(UTCBaseModel):
    """Wall query response — sessions indexed by raw signal."""

    sessions: List[WallSessionResponse]
    total: int


class EventResponse(UTCBaseModel):
    """Response for a single event."""

    id: int = Field(..., description="Event ID")
    role: str = Field(..., description="Message role")
    content_text: Optional[str] = Field(None, description="Message content")
    tool_name: Optional[str] = Field(None, description="Tool name")
    tool_input_json: Optional[Dict[str, Any]] = Field(None, description="Tool input")
    tool_output_text: Optional[str] = Field(None, description="Tool output")
    tool_call_id: Optional[str] = Field(None, description="Cross-provider call/result linkage ID")
    timestamp: datetime = Field(..., description="Event timestamp")
    in_active_context: bool = Field(
        True,
        description="True when event is inside the current active model context boundary",
    )
    branch_id: Optional[int] = Field(None, description="Session branch ID for rewind-aware projections")
    is_head_branch: bool = Field(True, description="True when event belongs to the active head branch")


class EventsListResponse(BaseModel):
    """Response for events list."""

    events: List[EventResponse]
    total: int
    branch_mode: str = Field("head", description="Branch projection mode: head|all")
    abandoned_events: int = Field(0, description="Events excluded from head projection due to rewind branches")


class SessionProjectionItemResponse(UTCBaseModel):
    """One stitched item in a selected session's projected lineage path."""

    kind: str = Field(..., description="Projection item kind: event|seam")
    session_id: str = Field(..., description="Concrete session UUID for this item")
    timestamp: datetime = Field(..., description="Timestamp used for item ordering and display")
    event: Optional[EventResponse] = Field(None, description="Present when kind=event")
    continued_from_session_id: Optional[str] = Field(None, description="Parent continuation session UUID for seams")
    continuation_kind: Optional[str] = Field(None, description="Continuation kind for seam items")
    origin_label: Optional[str] = Field(None, description="Origin label for seam items")
    parent_origin_label: Optional[str] = Field(None, description="Origin label for the parent segment")
    parent_continuation_kind: Optional[str] = Field(None, description="Continuation kind for the parent segment")
    branched_from_event_id: Optional[int] = Field(None, description="Event id where the child continuation branched")


class SessionProjectionResponse(BaseModel):
    """Response for a stitched lineage-path projection."""

    root_session_id: str
    focus_session_id: str
    head_session_id: str
    path_session_ids: List[str]
    items: List[SessionProjectionItemResponse]
    total: int
    page_offset: int = Field(0, description="Offset of the first item in this page within the full projection")
    branch_mode: str = Field("head", description="Branch projection mode: head|all")
    abandoned_events: int = Field(0, description="Events excluded from head projection due to rewind branches")


class SessionWorkspaceResponse(BaseModel):
    """Response for the primary session workspace bootstrap payload."""

    session: SessionResponse = Field(..., description="Focused session metadata")
    thread: SessionThreadResponse = Field(..., description="Logical thread continuations for the focused session")
    projection: SessionProjectionResponse = Field(..., description="First page of the stitched lineage projection")


class IngestResponse(BaseModel):
    """Response for ingest endpoint."""

    session_id: str
    events_inserted: int
    events_skipped: int
    session_created: bool


class FiltersResponse(BaseModel):
    """Response for filters endpoint."""

    projects: List[str]
    providers: List[str]
    machines: List[str] = []


class DemoSeedResponse(BaseModel):
    """Response for demo session seeding."""

    seeded: bool
    sessions_created: int
    sessions_failed: int = 0
    sessions_deleted: int = 0


class SessionActionRequest(BaseModel):
    action: str = Field(..., description="park | snooze | archive | resume")


class SessionActionResponse(BaseModel):
    session_id: str
    user_state: str


class SessionLoopModeRequest(BaseModel):
    loop_mode: SessionLoopMode = Field(..., description="manual | assist | autopilot")


class SessionLoopModeResponse(BaseModel):
    session_id: str
    loop_mode: SessionLoopMode


class BackfillSummariesResponse(BaseModel):
    """Response for summary backfill endpoint."""

    status: str = Field(..., description="'started', 'already_running', or 'nothing_to_do'")
    total: int = Field(0, description="Total sessions to process")
    message: str = Field("", description="Human-readable status message")


class BackfillProgressResponse(BaseModel):
    """Response for backfill progress check."""

    running: bool
    backfilled: int = 0
    skipped: int = 0
    errors: int = 0
    remaining: int = 0
    total: int = 0


class BackfillEmbeddingsResponse(BaseModel):
    """Response for embedding backfill endpoint."""

    status: str = Field(..., description="'started', 'already_running', or 'nothing_to_do'")
    total: int = Field(0, description="Total sessions to process")
    message: str = Field("", description="Human-readable status message")


class BackfillEmbeddingsProgressResponse(BaseModel):
    """Response for embedding backfill progress check."""

    running: bool
    embedded: int = 0
    skipped: int = 0
    errors: int = 0
    remaining: int = 0
    total: int = 0


class IngestHealthResponse(UTCBaseModel):
    status: str  # "ok" | "stale" | "unknown"
    last_session_at: Optional[datetime] = None
    gap_hours: Optional[float] = None
    threshold_hours: float
    session_count: int


class UsageStatsByProvider(BaseModel):
    provider: str
    sessions: int
    messages: int


class UsageStatsResponse(BaseModel):
    total_sessions: int
    total_messages: int
    date_range: Dict[str, str]
    by_provider: List[UsageStatsByProvider]


class SemanticSearchResponse(BaseModel):
    """Response for semantic search."""

    sessions: List[SessionResponse]
    total: int
    has_real_sessions: bool = True


class RecallMatch(BaseModel):
    """A single recall match with context."""

    session_id: str
    chunk_index: int
    score: float
    event_index_start: Optional[int] = None
    event_index_end: Optional[int] = None
    total_events: int = 0
    context: List[Dict[str, Any]] = Field(default_factory=list)
    match_event_id: Optional[int] = None


class RecallResponse(BaseModel):
    """Response for recall endpoint."""

    matches: List[RecallMatch]
    total: int


_BRIEFING_MARKER_RE = re.compile(
    r"\[(?:BEGIN|END)\s+SESSION\s+NOTES[^\]]*\]",
    re.IGNORECASE,
)


class BriefingResponse(BaseModel):
    """Response for the briefing endpoint."""

    project: str
    session_count: int
    briefing: Optional[str] = None


class ReflectRequest(BaseModel):
    """Request body for triggering reflection."""

    project: Optional[str] = Field(None, description="Project to reflect on (None = all)")
    window_hours: int = Field(24, ge=1, le=168, description="Hours to look back")


class ReflectionRunResponse(UTCBaseModel):
    """Response for a single reflection run."""

    run_id: str
    project: Optional[str] = None
    status: str = "completed"
    session_count: int = 0
    insights_created: int = 0
    insights_merged: int = 0
    insights_skipped: int = 0
    model: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None


class ReflectionListResponse(BaseModel):
    """Response for reflection run history."""

    runs: List[ReflectionRunResponse]
    total: int


class CleanupRequest(BaseModel):
    """Request for test cleanup."""

    project_patterns: List[str] = Field(
        ...,
        description="LIKE patterns to match (e.g., 'test-%', 'ratelimit-%')",
    )


class CleanupResponse(BaseModel):
    """Response for test cleanup."""

    deleted: int


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def normalize_utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def load_presence_map(db: Session, session_ids: list[UUID]) -> dict[str, SessionPresence]:
    if not session_ids:
        return {}
    str_session_ids = [str(session_id) for session_id in session_ids]

    from zerg.services.presence_cache import get_presence_cache

    cache = get_presence_cache()
    if not cache.is_cold:
        cached = cache.get_many(str_session_ids)
        missing_ids = [sid for sid in str_session_ids if sid not in cached]
        if missing_ids:
            rows = db.query(SessionPresence).filter(SessionPresence.session_id.in_(missing_ids)).all()
            if rows:
                cache.warm_from_db(rows)
                cached = cache.get_many(str_session_ids)
        return {sid: cache.to_presence_obj(entry) for sid, entry in cached.items()}

    rows = db.query(SessionPresence).filter(SessionPresence.session_id.in_(str_session_ids)).all()
    return {row.session_id: row for row in rows}


def resolve_runtime_overlay(
    session: AgentSession,
    *,
    last_activity_at: datetime | None,
    presence_map: dict[str, SessionPresence],
    runtime_state_map: dict[str, Any],
    now: datetime,
) -> SessionRuntimeView:
    runtime_state = runtime_state_map.get(str(session.id))
    if runtime_state is not None:
        return build_runtime_view(
            state=runtime_state,
            session=session,
            now=now,
        )

    return build_fallback_runtime_view(
        session=session,
        last_activity_at=last_activity_at,
        presence=presence_map.get(str(session.id)),
        now=now,
    )


def get_thread_meta(store: AgentsStore, session: AgentSession, thread_cache: Dict[str, tuple[str, int]]) -> tuple[str, int]:
    root_id = str(session.thread_root_session_id or session.id)
    cached = thread_cache.get(root_id)
    if cached is not None:
        return cached
    head = store.get_thread_head(session)
    thread_sessions = store.list_thread_sessions(session)
    meta = (str(head.id if head else session.id), max(1, len(thread_sessions)))
    thread_cache[root_id] = meta
    return meta


def build_session_response(
    store: AgentsStore,
    session: AgentSession,
    *,
    thread_cache: Dict[str, tuple[str, int]] | None = None,
    last_activity_at: datetime | None = None,
    runtime_overlay: SessionRuntimeView | None = None,
    first_user_message: str | None = None,
    match_event_id: int | None = None,
    match_snippet: str | None = None,
    match_role: str | None = None,
    match_score: float | None = None,
) -> SessionResponse:
    cache = thread_cache if thread_cache is not None else {}
    thread_head_session_id, thread_continuation_count = get_thread_meta(store, session, cache)
    include_runtime = should_include_runtime_view(session=session, runtime_view=runtime_overlay)
    capability_flags = build_session_capabilities(session)
    return SessionResponse(
        id=str(session.id),
        provider=session.provider,
        project=session.project,
        device_id=session.device_id,
        environment=session.environment,
        cwd=session.cwd,
        git_repo=session.git_repo,
        git_branch=session.git_branch,
        started_at=session.started_at,
        ended_at=session.ended_at,
        user_messages=session.user_messages or 0,
        assistant_messages=session.assistant_messages or 0,
        tool_calls=session.tool_calls or 0,
        last_activity_at=last_activity_at,
        timeline_anchor_at=(runtime_overlay.timeline_anchor_at if runtime_overlay is not None else last_activity_at),
        runtime_phase=(runtime_overlay.runtime_phase if runtime_overlay is not None else None),
        phase_started_at=(runtime_overlay.phase_started_at if runtime_overlay is not None else None),
        last_progress_at=(runtime_overlay.last_progress_at if runtime_overlay is not None else None),
        runtime_source=(runtime_overlay.runtime_source if runtime_overlay is not None else None),
        terminal_state=(runtime_overlay.terminal_state if runtime_overlay is not None else None),
        runtime_version=(runtime_overlay.runtime_version if runtime_overlay is not None else None),
        status=(runtime_overlay.status if include_runtime else None),
        presence_state=(runtime_overlay.presence_state if include_runtime else None),
        presence_tool=(runtime_overlay.presence_tool if include_runtime else None),
        presence_updated_at=(runtime_overlay.presence_updated_at if include_runtime else None),
        last_live_at=(runtime_overlay.last_live_at if include_runtime else None),
        display_phase=(runtime_overlay.display_phase if include_runtime else None),
        active_tool=(runtime_overlay.active_tool if include_runtime else None),
        confidence=(runtime_overlay.confidence if include_runtime else None),
        summary=session.summary,
        summary_title=session.summary_title,
        first_user_message=first_user_message,
        match_event_id=match_event_id,
        match_snippet=match_snippet,
        match_role=match_role,
        match_score=match_score,
        thread_root_session_id=str(session.thread_root_session_id or session.id),
        thread_head_session_id=thread_head_session_id,
        thread_continuation_count=thread_continuation_count,
        continued_from_session_id=(str(session.continued_from_session_id) if session.continued_from_session_id else None),
        continuation_kind=session.continuation_kind,
        origin_label=session.origin_label,
        home_label=capability_flags.home_label,
        branched_from_event_id=session.branched_from_event_id,
        is_writable_head=bool(session.is_writable_head),
        is_sidechain=bool(session.is_sidechain or False),
        control=build_session_control_response(session, capability_flags=capability_flags),
        capabilities=build_session_capabilities_response(capability_flags=capability_flags),
        loop_mode=_coerce_session_loop_mode(getattr(session, "loop_mode", None)),
        user_state=session.user_state or "active",
    )


def build_active_session_response(
    session: AgentSession,
    *,
    last_activity_at: datetime,
    runtime_overlay: SessionRuntimeView,
    last_user_message: str | None,
    last_assistant_message: str | None,
    attention: str,
    now: datetime,
) -> ActiveSessionResponse:
    capability_flags = build_session_capabilities(session)
    _started = (
        session.started_at.replace(tzinfo=timezone.utc) if session.started_at and session.started_at.tzinfo is None else session.started_at
    )
    _ended = session.ended_at.replace(tzinfo=timezone.utc) if session.ended_at and session.ended_at.tzinfo is None else session.ended_at
    end_time = _ended or now
    duration_minutes = int((end_time - _started).total_seconds() / 60) if _started else 0
    message_count = (session.user_messages or 0) + (session.assistant_messages or 0)

    return ActiveSessionResponse(
        id=str(session.id),
        project=session.project,
        provider=session.provider,
        cwd=session.cwd,
        git_branch=session.git_branch,
        started_at=session.started_at,
        ended_at=session.ended_at,
        last_activity_at=last_activity_at,
        timeline_anchor_at=runtime_overlay.timeline_anchor_at,
        runtime_phase=runtime_overlay.runtime_phase,
        phase_started_at=runtime_overlay.phase_started_at,
        last_progress_at=runtime_overlay.last_progress_at,
        runtime_source=runtime_overlay.runtime_source,
        terminal_state=runtime_overlay.terminal_state,
        runtime_version=runtime_overlay.runtime_version,
        status=runtime_overlay.status,
        attention=attention,
        duration_minutes=duration_minutes,
        last_user_message=last_user_message,
        last_assistant_message=last_assistant_message,
        message_count=message_count,
        tool_calls=session.tool_calls or 0,
        presence_state=runtime_overlay.presence_state,
        presence_tool=runtime_overlay.presence_tool,
        presence_updated_at=runtime_overlay.presence_updated_at,
        last_live_at=runtime_overlay.last_live_at,
        display_phase=runtime_overlay.display_phase,
        active_tool=runtime_overlay.active_tool,
        confidence=runtime_overlay.confidence,
        user_state=session.user_state or "active",
        home_label=capability_flags.home_label,
        control=build_session_control_response(session, capability_flags=capability_flags),
        capabilities=build_session_capabilities_response(capability_flags=capability_flags),
        loop_mode=_coerce_session_loop_mode(getattr(session, "loop_mode", None)),
    )


def build_event_response(
    store: AgentsStore,
    event: AgentEvent,
    *,
    boundary: int | None,
    head_branch_id: int | None,
) -> EventResponse:
    return EventResponse(
        id=event.id,
        role=event.role,
        content_text=event.content_text,
        tool_name=event.tool_name,
        tool_input_json=event.tool_input_json,
        tool_output_text=event.tool_output_text,
        tool_call_id=event.tool_call_id,
        timestamp=event.timestamp,
        in_active_context=store.is_event_in_active_context(event, boundary) if boundary is not None else True,
        branch_id=event.branch_id,
        is_head_branch=(head_branch_id is None or event.branch_id in {None, head_branch_id}),
    )


def format_age(dt: datetime) -> str:
    """Format a datetime as human-readable relative time."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    delta = now - dt

    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        minutes = seconds // 60
        return f"{minutes}m ago"
    if seconds < 86400:
        hours = seconds // 3600
        return f"{hours}h ago"
    days = seconds // 86400
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days}d ago"
    weeks = days // 7
    if weeks == 1:
        return "1w ago"
    return f"{weeks}w ago"


def sanitize_briefing_field(value: str) -> str:
    """Strip control markers from user-sourced text to prevent boundary escape."""
    return _BRIEFING_MARKER_RE.sub("", value).strip()
