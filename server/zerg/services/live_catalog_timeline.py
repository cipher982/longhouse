"""Timeline list projection served entirely from the bounded live catalog."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from datetime import timezone
from time import monotonic
from typing import Any
from uuid import UUID

from zerg.models.live_store import LiveLaunchReadiness
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.models.live_store import LiveTimelineCard
from zerg.services.agent_heartbeat_health import DEFAULT_MACHINE_HEARTBEAT_STALE_AFTER_SECONDS
from zerg.services.agents.kernel_capabilities import KernelSessionCapabilities
from zerg.services.agents.kernel_capabilities import project_capabilities_from_rows
from zerg.services.agents.kernel_capabilities import project_console_turn_capabilities
from zerg.services.catalog_facts import decode_catalog_datetime
from zerg.services.catalog_facts import hydrate_catalog_row
from zerg.services.catalog_read_gateway import CatalogReadError
from zerg.services.catalog_read_gateway import canonical_timeline_snapshot
from zerg.services.catalog_read_gateway import shadow_session_state_snapshot
from zerg.services.live_launch_readiness import LiveLaunchReadinessView
from zerg.services.live_launch_readiness import project_live_launch_readiness
from zerg.services.machine_control_channel import get_machine_control_channel_registry
from zerg.services.managed_provider_contracts import contract_for_provider
from zerg.services.session_pubsub import TOPIC_TIMELINE
from zerg.services.session_pubsub import get_pubsub
from zerg.services.session_runtime_display import TRANSCRIPT_SYNC_DISPLAY_WINDOW
from zerg.services.session_state_contract import SessionHostFacts
from zerg.services.session_state_contract import project_pending_interaction_facts
from zerg.services.session_state_contract import project_transcript_facts
from zerg.services.session_state_facts_projector import project_served_session_state_facts
from zerg.services.session_title import resolve_timeline_title
from zerg.services.session_title import resolve_title_provenance
from zerg.services.session_title import sanitize_title
from zerg.services.session_views import SessionResponse
from zerg.services.session_views import SessionsListResponse
from zerg.services.session_views import build_compat_runtime_display_response
from zerg.services.session_views import build_live_launch_placeholder_response
from zerg.services.session_views import build_session_capabilities_response
from zerg.services.session_views import build_session_timeline_card_response
from zerg.services.session_views import project_compat_capabilities_from_state
from zerg.services.timeline_session_listing import TimelineSessionCardResponse
from zerg.services.timeline_session_listing import TimelineSessionListParams
from zerg.services.timeline_session_listing import TimelineSessionsListResponse
from zerg.utils.time import normalize_utc


def _supported_operations(provider: str | None) -> set[str]:
    contract = contract_for_provider(provider)
    if contract is None:
        return set()
    operations = {
        operation for operation in ("send_input", "interrupt", "terminate", "tail_output") if bool(getattr(contract, operation, False))
    }
    if contract.reattach:
        operations.add("reattach")
    if contract.can_resume:
        operations.add("resume")
    return operations


def _pending_interaction(catalog_facts: dict[str, Any]) -> dict[str, Any] | None:
    interaction = catalog_facts.get("pending_interaction")
    if not isinstance(interaction, dict):
        return None
    projection = interaction.get("projection")
    body = dict(projection) if isinstance(projection, dict) else {}
    body.update(
        {
            "id": interaction.get("id"),
            "session_id": interaction.get("session_id"),
            "runtime_key": interaction.get("runtime_key"),
            "kind": interaction.get("kind"),
            "status": interaction.get("status"),
            "provider": interaction.get("provider"),
            "can_respond": bool(interaction.get("can_respond")),
            "occurred_at": decode_catalog_datetime(interaction.get("occurred_at")),
            "last_seen_at": decode_catalog_datetime(interaction.get("last_seen_at")),
            "resolved_at": decode_catalog_datetime(interaction.get("resolved_at")),
            "expires_at": decode_catalog_datetime(interaction.get("expires_at")),
        }
    )
    return body


def _host_facts(catalog_facts: dict[str, Any], *, now: datetime) -> SessionHostFacts:
    heartbeat = catalog_facts.get("machine_heartbeat")
    if not isinstance(heartbeat, dict):
        return SessionHostFacts(state="unknown")
    observed_at = decode_catalog_datetime(heartbeat.get("received_at"))
    if observed_at is None:
        return SessionHostFacts(state="unknown")
    if bool(heartbeat.get("is_offline")):
        state = "offline"
    elif (now - observed_at).total_seconds() > DEFAULT_MACHINE_HEARTBEAT_STALE_AFTER_SECONDS:
        state = "stale"
    else:
        state = "online"
    return SessionHostFacts(
        state=state,
        observed_at=observed_at,
    )


def _console_control_facts(
    *,
    session: LiveSessionCatalog,
    thread: LiveSessionThread | None,
    latest_console_turn: dict[str, Any] | None,
    owner_id: int | None,
) -> dict[str, Any]:
    """Project Console action gates from catalog identity plus the channel registry."""

    device_id = str(thread.device_id or "").strip() if thread is not None else ""
    cwd = str(thread.cwd or "").strip() if thread is not None else ""
    registry = get_machine_control_channel_registry()
    machine_online = bool(owner_id is not None and device_id and registry.is_online(owner_id=owner_id, device_id=device_id))
    adapter_available = bool(
        machine_online
        and registry.supports(
            owner_id=owner_id,
            device_id=device_id,
            capability=f"{session.provider}.turn_start",
        )
    )
    interrupt_adapter_available = bool(
        machine_online
        and registry.supports(
            owner_id=owner_id,
            device_id=device_id,
            capability=f"{session.provider}.turn_interrupt",
        )
    )
    turn_state = str((latest_console_turn or {}).get("state") or "idle").strip().lower()
    if turn_state not in {"idle", "queued", "starting", "active", "draining"}:
        turn_state = "idle"
    execution_target_available = bool(device_id and cwd)
    blocked_by = "session_closed" if session.closed_at is not None else None
    if blocked_by is None and not execution_target_available:
        blocked_by = "execution_target_missing"
    if blocked_by is None and turn_state in {"queued", "starting", "active", "draining"}:
        blocked_by = "turn_active"
    if blocked_by is None and not machine_online:
        blocked_by = "machine_offline"
    if blocked_by is None and not adapter_available:
        blocked_by = "adapter_unavailable"
    return {
        "turn_state": turn_state,
        "can_start_turn": blocked_by is None,
        "start_turn_blocked_by": blocked_by,
        "can_interrupt_active_turn": turn_state in {"starting", "active", "draining"} and interrupt_adapter_available,
    }


def _canonical_runtime_aliases(*, session_state, runtime_display) -> dict[str, Any]:
    """Derive deprecated detail aliases from the canonical served contract."""

    activity = session_state.activity
    display_state = runtime_display.state.value if runtime_display.state is not None else None
    return {
        "runtime_phase": None if activity.state == "unknown" else activity.state,
        "phase_started_at": activity.observed_at,
        "last_progress_at": activity.observed_at,
        "runtime_source": activity.source,
        "terminal_state": (session_state.disposition.close_reason if session_state.disposition.state == "closed" else None),
        "runtime_version": session_state.commit_seq,
        "status": "closed" if session_state.disposition.state == "closed" else display_state,
        "presence_state": display_state,
        "presence_tool": activity.tool,
        "presence_updated_at": activity.observed_at,
        "last_live_at": activity.observed_at,
        "display_phase": runtime_display.phase_label,
        "active_tool": activity.tool,
        "confidence": "live" if runtime_display.is_live else "stale" if runtime_display.has_signal else None,
    }


def project_catalog_session_facts(
    facts: dict[str, Any],
    *,
    observed_at: datetime,
    canonical_heads: list[dict[str, Any]] | None = None,
    commit_seq: int | None = None,
) -> SessionResponse:
    """Project one catalogd snapshot through the canonical state projector."""

    session = hydrate_catalog_row(LiveSessionCatalog, facts.get("catalog"))
    if session is None:
        raise ValueError("catalog session facts are missing catalog")
    card = hydrate_catalog_row(LiveTimelineCard, facts.get("card"))
    if card is None:
        card = LiveTimelineCard(
            session_id=session.session_id,
            provider=session.provider,
            environment=session.environment,
            project=session.project,
            device_id=session.device_id,
            cwd=session.cwd,
            started_at=session.started_at,
            last_activity_at=session.last_activity_at,
            summary_title=session.summary_title,
            first_user_message_preview=session.first_user_message_preview,
            user_messages=int(session.user_messages or 0),
            assistant_messages=int(session.assistant_messages or 0),
            tool_calls=int(session.tool_calls or 0),
            transcript_revision=int(session.transcript_revision or 0),
            archive_state="pending",
        )
    readiness_row = hydrate_catalog_row(LiveLaunchReadiness, facts.get("readiness"))
    readiness = project_live_launch_readiness(readiness_row) if readiness_row is not None else None
    thread = hydrate_catalog_row(LiveSessionThread, facts.get("primary_thread"))
    run = hydrate_catalog_row(LiveSessionRun, facts.get("latest_run"))
    connections = [
        row for payload in facts.get("connections") or [] if (row := hydrate_catalog_row(LiveSessionConnection, payload)) is not None
    ]
    capabilities = project_capabilities_from_rows(
        session_id=str(session.session_id),
        thread=thread,
        latest_run=run,
        connections=connections,
        now=observed_at,
    )
    if str(session.origin_kind or "").strip() == "console":
        latest_console_turn = facts.get("latest_console_turn")
        owner_id = facts.get("owner_id")
        console_control = _console_control_facts(
            session=session,
            thread=thread,
            latest_console_turn=latest_console_turn if isinstance(latest_console_turn, dict) else None,
            owner_id=int(owner_id) if owner_id is not None else None,
        )
        facts = {**facts, "console_control": console_control}
        capabilities = project_console_turn_capabilities(
            capabilities,
            closed=session.closed_at is not None,
            execution_target_available=bool(thread is not None and str(thread.device_id or "").strip() and str(thread.cwd or "").strip()),
            turn_state=console_control["turn_state"],
            machine_online=console_control["start_turn_blocked_by"] not in {"machine_offline"},
            adapter_available=console_control["start_turn_blocked_by"] not in {"adapter_unavailable"},
            interrupt_adapter_available=bool(console_control["can_interrupt_active_turn"]),
        )
    return _response_from_catalog(
        session,
        card,
        readiness=readiness,
        capability_flags=capabilities,
        now=observed_at,
        catalog_facts=facts,
        canonical_heads=canonical_heads,
        commit_seq=commit_seq,
    )


def _title(session: LiveSessionCatalog, card: LiveTimelineCard) -> str:
    user_messages, assistant_messages, tool_calls = _message_counts(session, card)
    first_user_message = card.first_user_message_preview or session.first_user_message_preview
    if (
        not any((user_messages, assistant_messages, tool_calls))
        and not sanitize_title(session.anchor_title)
        and not sanitize_title(first_user_message)
    ):
        return resolve_timeline_title(
            anchor_title=session.anchor_title,
            summary_title=card.summary_title or session.summary_title,
            summary_status="ready" if session.summary else "unavailable",
            first_user_message=first_user_message,
            project=session.project,
            git_branch=session.git_branch,
            provider=session.provider,
            user_messages=user_messages,
            assistant_messages=assistant_messages,
            tool_calls=tool_calls,
        )
    for value in (
        session.anchor_title,
        card.summary_title,
        session.summary_title,
        first_user_message,
        session.project,
    ):
        normalized = str(value or "").strip()
        if normalized:
            return normalized[:255]
    return f"{session.provider.title()} session"


def _message_counts(session: LiveSessionCatalog, card: LiveTimelineCard) -> tuple[int, int, int]:
    return (
        max(int(session.user_messages or 0), int(card.user_messages or 0)),
        max(int(session.assistant_messages or 0), int(card.assistant_messages or 0)),
        max(int(session.tool_calls or 0), int(card.tool_calls or 0)),
    )


def _title_source(session: LiveSessionCatalog, card: LiveTimelineCard) -> str:
    user_messages, _assistant_messages, _tool_calls = _message_counts(session, card)
    return resolve_title_provenance(
        anchor_title=session.anchor_title,
        first_user_message=card.first_user_message_preview or session.first_user_message_preview,
        user_messages=user_messages,
        title_retry_at=session.title_retry_at,
    )[1]


def _title_state(session: LiveSessionCatalog, card: LiveTimelineCard) -> str:
    if sanitize_title(session.anchor_title, max_words=6):
        return "ready"
    if session.title_last_error == "no_meaningful_user_text":
        return "exempt"
    if session.title_retry_at is not None:
        return "degraded"
    if card.first_user_message_preview or session.first_user_message_preview:
        return "pending"
    return "awaiting_input"


def _project_session_state(
    *,
    session: LiveSessionCatalog,
    card: LiveTimelineCard,
    readiness: LiveLaunchReadinessView | None,
    capability_flags: KernelSessionCapabilities,
    last_activity_at: datetime,
    now: datetime,
    catalog_facts: dict[str, Any],
    canonical_heads: list[dict[str, Any]] | None,
    commit_seq: int | None,
):
    pause_request = _pending_interaction(catalog_facts)
    transcript = project_transcript_facts(
        session=session,
        last_activity_at=last_activity_at,
        user_messages=int(card.user_messages or 0),
        assistant_messages=int(card.assistant_messages or 0),
        archive_state=card.archive_state,
        live_observation=bool(capability_flags.observe_only),
        source_revision=(catalog_facts.get("transcript_coordinates") or {}).get("source_revision"),
        durable_revision=(catalog_facts.get("transcript_coordinates") or {}).get("durable_revision"),
        render_revision=(catalog_facts.get("transcript_coordinates") or {}).get("render_revision"),
        transcript_last_append_at=decode_catalog_datetime((catalog_facts.get("transcript_coordinates") or {}).get("last_append_at")),
    )
    return project_served_session_state_facts(
        session_id=str(session.session_id),
        commit_seq=commit_seq or 0,
        catalog_facts=catalog_facts,
        heads=canonical_heads or (),
        supported_operations=_supported_operations(session.provider),
        pending_interaction=project_pending_interaction_facts(pause_request),
        transcript=transcript,
        host=_host_facts(catalog_facts, now=now),
        now=now,
    )


def _pending_response_from_catalog(
    session: LiveSessionCatalog,
    card: LiveTimelineCard,
    *,
    readiness: LiveLaunchReadinessView,
    capability_flags: KernelSessionCapabilities,
    now: datetime,
    catalog_facts: dict[str, Any],
    canonical_heads: list[dict[str, Any]] | None,
    commit_seq: int | None,
):
    response = build_live_launch_placeholder_response(readiness)
    title = _title(session, card)
    ended_at = normalize_utc(session.ended_at)
    last_activity_at = normalize_utc(card.last_activity_at) or normalize_utc(session.last_activity_at) or response.started_at
    capabilities = response.capabilities
    session_state = _project_session_state(
        session=session,
        card=card,
        readiness=readiness,
        capability_flags=capability_flags,
        last_activity_at=last_activity_at,
        now=now,
        catalog_facts=catalog_facts,
        canonical_heads=canonical_heads,
        commit_seq=commit_seq,
    )
    runtime_display = build_compat_runtime_display_response(
        session_state=session_state,
        pause_request=_pending_interaction(catalog_facts),
        now=now,
    )
    capabilities = project_compat_capabilities_from_state(capabilities, session_state)
    canonical_aliases = _canonical_runtime_aliases(session_state=session_state, runtime_display=runtime_display)
    launch_state = readiness.launch_state
    execution_lifetime = readiness.execution_lifetime
    return response.model_copy(
        update={
            "provider": session.provider,
            "origin_kind": session.origin_kind,
            "project": session.project,
            "device_id": session.device_id,
            "environment": session.environment,
            "cwd": session.cwd,
            "git_repo": session.git_repo,
            "git_branch": session.git_branch,
            "started_at": normalize_utc(session.started_at) or response.started_at,
            "ended_at": ended_at,
            "user_messages": int(card.user_messages or 0),
            "assistant_messages": int(card.assistant_messages or 0),
            "tool_calls": int(card.tool_calls or 0),
            "last_activity_at": last_activity_at,
            "timeline_anchor_at": session_state.activity.observed_at or last_activity_at,
            "runtime_phase": canonical_aliases["runtime_phase"],
            "phase_started_at": canonical_aliases["phase_started_at"],
            "last_progress_at": canonical_aliases["last_progress_at"],
            "runtime_source": canonical_aliases["runtime_source"],
            "terminal_state": canonical_aliases["terminal_state"],
            "runtime_version": canonical_aliases["runtime_version"],
            "status": canonical_aliases["status"],
            "presence_state": canonical_aliases["presence_state"],
            "presence_tool": canonical_aliases["presence_tool"],
            "presence_updated_at": canonical_aliases["presence_updated_at"],
            "last_live_at": canonical_aliases["last_live_at"],
            "display_phase": canonical_aliases["display_phase"],
            "active_tool": canonical_aliases["active_tool"],
            "confidence": canonical_aliases["confidence"],
            "summary": session.summary,
            "summary_title": card.summary_title or session.summary_title,
            "anchor_title": session.anchor_title,
            "timeline_title": title,
            "title_state": _title_state(session, card),
            "title_source": _title_source(session, card)
            if card.first_user_message_preview or session.first_user_message_preview
            else "project",
            "summary_status": "ready" if session.summary else "unavailable",
            "first_user_message": card.first_user_message_preview or session.first_user_message_preview,
            "thread_root_session_id": str(session.session_id),
            "thread_head_session_id": str(session.session_id),
            "thread_continuation_count": 1,
            "origin_label": session.environment,
            "home_label": session.device_name or session.device_id,
            "is_writable_head": session_state.disposition.state == "open",
            "capabilities": capabilities,
            "session_state": session_state,
            "runtime_display": runtime_display,
            "timeline_card": build_session_timeline_card_response(
                runtime_view=None,
                runtime_display=runtime_display,
                session_state=session_state,
            ),
            "loop_mode": session.loop_mode or "assist",
            "user_state": session.user_state or "active",
            "launch_state": launch_state,
            "execution_lifetime": execution_lifetime,
            "launch_error_code": readiness.launch_error_code,
            "launch_error_message": readiness.launch_error_message,
        }
    )


def _pending_placeholder_is_current(readiness: LiveLaunchReadinessView | None, *, now: datetime) -> bool:
    if readiness is None:
        return False
    if readiness.launch_state in {"launch_failed", "launch_orphaned"}:
        return True
    updated_at = normalize_utc(readiness.updated_at)
    return updated_at is not None and now - updated_at <= TRANSCRIPT_SYNC_DISPLAY_WINDOW


def _response_from_catalog(
    session: LiveSessionCatalog,
    card: LiveTimelineCard,
    *,
    readiness: LiveLaunchReadinessView | None,
    capability_flags: KernelSessionCapabilities,
    now: datetime,
    catalog_facts: dict[str, Any],
    canonical_heads: list[dict[str, Any]] | None,
    commit_seq: int | None,
) -> SessionResponse:
    if card.archive_state == "pending" and _pending_placeholder_is_current(readiness, now=now):
        assert readiness is not None
        return _pending_response_from_catalog(
            session,
            card,
            readiness=readiness,
            capability_flags=capability_flags,
            now=now,
            catalog_facts=catalog_facts,
            canonical_heads=canonical_heads,
            commit_seq=commit_seq,
        )

    started_at = normalize_utc(session.started_at) or now
    ended_at = normalize_utc(session.ended_at)
    last_activity_at = normalize_utc(card.last_activity_at) or normalize_utc(session.last_activity_at) or started_at
    session_state = _project_session_state(
        session=session,
        card=card,
        readiness=readiness,
        capability_flags=capability_flags,
        last_activity_at=last_activity_at,
        now=now,
        catalog_facts=catalog_facts,
        canonical_heads=canonical_heads,
        commit_seq=commit_seq,
    )
    runtime_display = build_compat_runtime_display_response(
        session_state=session_state,
        pause_request=_pending_interaction(catalog_facts),
        now=now,
    )
    capabilities = build_session_capabilities_response(
        session=session,
        capability_flags=capability_flags,
        runtime_display=runtime_display,
        kernel_capabilities=capability_flags,
        session_mode=session_state.mode,
    )
    capabilities = project_compat_capabilities_from_state(capabilities, session_state)
    canonical_aliases = _canonical_runtime_aliases(session_state=session_state, runtime_display=runtime_display)
    title = _title(session, card)
    return SessionResponse(
        id=str(session.session_id),
        origin_kind=session.origin_kind,
        provider=session.provider,
        project=session.project,
        device_id=session.device_id,
        environment=session.environment,
        cwd=session.cwd,
        git_repo=session.git_repo,
        git_branch=session.git_branch,
        started_at=started_at,
        ended_at=ended_at,
        user_messages=int(card.user_messages or 0),
        assistant_messages=int(card.assistant_messages or 0),
        tool_calls=int(card.tool_calls or 0),
        last_activity_at=last_activity_at,
        timeline_anchor_at=session_state.activity.observed_at or last_activity_at,
        runtime_phase=canonical_aliases["runtime_phase"],
        phase_started_at=canonical_aliases["phase_started_at"],
        last_progress_at=canonical_aliases["last_progress_at"],
        runtime_source=canonical_aliases["runtime_source"],
        terminal_state=canonical_aliases["terminal_state"],
        runtime_version=canonical_aliases["runtime_version"],
        status=canonical_aliases["status"],
        presence_state=canonical_aliases["presence_state"],
        presence_tool=canonical_aliases["presence_tool"],
        presence_updated_at=canonical_aliases["presence_updated_at"],
        last_live_at=canonical_aliases["last_live_at"],
        display_phase=canonical_aliases["display_phase"],
        active_tool=canonical_aliases["active_tool"],
        confidence=canonical_aliases["confidence"],
        summary=session.summary,
        summary_title=card.summary_title or session.summary_title,
        anchor_title=session.anchor_title,
        timeline_title=title,
        title_state=_title_state(session, card),
        title_source=_title_source(session, card) if card.first_user_message_preview or session.first_user_message_preview else "project",
        summary_status="ready" if session.summary else "unavailable",
        first_user_message=card.first_user_message_preview or session.first_user_message_preview,
        thread_root_session_id=str(session.session_id),
        thread_head_session_id=str(session.session_id),
        thread_continuation_count=1,
        origin_label=session.environment,
        home_label=capability_flags.home_label,
        is_writable_head=runtime_display.lifecycle != "closed",
        capabilities=capabilities,
        session_state=session_state,
        runtime_display=runtime_display,
        timeline_card=build_session_timeline_card_response(
            runtime_view=None,
            runtime_display=runtime_display,
            session_state=session_state,
        ),
        loop_mode=session.loop_mode or "assist",
        user_state=session.user_state or "active",
        launch_state=readiness.launch_state if readiness is not None else None,
        execution_lifetime=readiness.execution_lifetime if readiness is not None else None,
        launch_error_code=readiness.launch_error_code if readiness is not None else None,
        launch_error_message=readiness.launch_error_message if readiness is not None else None,
    )


def list_live_catalog_timeline(
    *,
    params: TimelineSessionListParams,
    owner_id: int | None = None,
) -> TimelineSessionsListResponse:
    """List timeline cards from one catalogd-owned SQLite snapshot."""

    if params.query is not None or (params.mode or "lexical") != "lexical":
        raise ValueError("search_requires_archive")
    snapshot_params = {
        "project": params.project,
        "provider": params.provider,
        "environment": params.environment,
        "include_test": params.include_test,
        "hide_autonomous": params.hide_autonomous,
        "include_automation": params.include_automation,
        "device_id": params.device_id,
        "days_back": params.days_back,
        "limit": params.limit,
        "offset": params.offset,
    }
    if owner_id is None:
        raise CatalogReadError(
            "canonical_owner_required",
            "Canonical timeline projection requires an owner-scoped request.",
        )
    snapshot = canonical_timeline_snapshot(snapshot_params, owner_id=owner_id)
    return project_catalog_timeline_snapshot(snapshot)


def project_catalog_timeline_snapshot(snapshot: dict[str, Any]) -> TimelineSessionsListResponse:
    """Project a raw catalogd timeline snapshot without any storage access."""

    observed_at = decode_catalog_datetime(snapshot.get("observed_at"))
    if not isinstance(observed_at, datetime):
        raise ValueError("catalog timeline snapshot is missing observed_at")
    cards: list[TimelineSessionCardResponse] = []
    commit_seq = int(snapshot.get("commit_seq") or 0)
    for row in snapshot.get("rows") or []:
        canonical_heads = row.get("heads")
        if row.get("heads_truncated") is True:
            raise CatalogReadError(
                "shadow_fact_head_limit_exceeded",
                "Canonical session facts exceed the bounded timeline projection limit.",
            )
        projected = project_catalog_session_facts(
            row["facts"],
            observed_at=observed_at,
            canonical_heads=canonical_heads if isinstance(canonical_heads, list) else None,
            commit_seq=commit_seq if isinstance(canonical_heads, list) else None,
        )
        thread_id = str(row.get("thread_id") or projected.id)
        cards.append(
            TimelineSessionCardResponse(
                thread_id=thread_id,
                timeline_anchor_at=projected.timeline_anchor_at,
                head=projected,
                detail=projected,
                root=projected,
                continuation_count=1,
                started_origin_label=projected.origin_label or projected.environment,
                head_origin_label=projected.origin_label or projected.environment,
            )
        )
    return TimelineSessionsListResponse(
        sessions=cards,
        total=int(snapshot.get("total") or 0),
        has_real_sessions=bool(snapshot.get("has_real_sessions")),
    )


def list_live_catalog_sessions(
    *,
    params: TimelineSessionListParams,
    owner_id: int | None = None,
) -> SessionsListResponse:
    """Machine-facing flat session list from the same bounded card projection."""

    if params.query is not None or (params.mode or "lexical") != "lexical":
        raise ValueError("search_requires_archive")
    timeline = list_live_catalog_timeline(params=params, owner_id=owner_id)
    return SessionsListResponse(
        sessions=[card.head for card in timeline.sessions],
        total=timeline.total,
        has_real_sessions=timeline.has_real_sessions,
    )


def project_catalog_sessions_snapshot(snapshot: dict[str, Any]) -> SessionsListResponse:
    timeline = project_catalog_timeline_snapshot(snapshot)
    return SessionsListResponse(
        sessions=[card.head for card in timeline.sessions],
        total=timeline.total,
        has_real_sessions=timeline.has_real_sessions,
    )


def read_live_catalog_session(
    session_id: UUID,
    *,
    owner_id: int | None = None,
    include_hidden: bool = True,
) -> tuple[SessionResponse | None, str | None, str]:
    """Read one session shell and its provider alias from one catalog snapshot."""

    if owner_id is None:
        raise CatalogReadError(
            "canonical_owner_required",
            "Canonical session detail requires an owner-scoped request.",
        )
    snapshot = shadow_session_state_snapshot(str(session_id), owner_id=owner_id)
    commit_seq = str(snapshot.get("commit_seq") or "0")
    if snapshot.get("found") is not True:
        return None, None, commit_seq
    observed_at = decode_catalog_datetime(snapshot.get("observed_at"))
    if snapshot.get("heads_truncated") is True:
        raise CatalogReadError(
            "shadow_fact_head_limit_exceeded",
            "Canonical session facts exceed the bounded detail projection limit.",
        )
    facts = snapshot.get("legacy_facts")
    if not isinstance(observed_at, datetime) or not isinstance(facts, dict):
        raise CatalogReadError(
            "invalid_catalog_snapshot",
            "Catalog session snapshot is incomplete.",
        )
    heads = snapshot.get("heads")
    if not isinstance(heads, list):
        raise CatalogReadError(
            "invalid_catalog_snapshot",
            "Catalog session snapshot is missing reducer fact heads.",
        )
    session_facts = facts.get("session")
    if not include_hidden and isinstance(session_facts, dict) and bool(session_facts.get("hidden_from_default_timeline")):
        return None, None, commit_seq
    try:
        projected = project_catalog_session_facts(
            facts,
            observed_at=observed_at,
            canonical_heads=heads,
            commit_seq=int(commit_seq),
        )
    except (TypeError, ValueError) as exc:
        raise CatalogReadError(
            "invalid_catalog_snapshot",
            "Catalog session snapshot could not be projected.",
        ) from exc
    provider_alias = facts.get("provider_alias")
    return projected, str(provider_alias) if provider_alias else None, commit_seq


def project_machine_session_delta(
    session: SessionResponse,
    *,
    commit_seq: str | int | None,
    fanout: dict[str, Any] | None = None,
    canonical: bool = False,
) -> dict[str, Any]:
    """Project only the facts an ambient machine client can render."""

    payload: dict[str, Any] = {
        "session_id": session.id,
        "device_id": session.device_id,
        "timeline_title": session.timeline_title,
        "title_state": session.title_state,
        "title_source": session.title_source,
        "runtime_phase": session.runtime_phase,
        "display_phase": session.display_phase,
        "last_activity_at": session.last_activity_at.isoformat() if session.last_activity_at else None,
        "runtime_version": session.runtime_version,
        "commit_seq": str(commit_seq) if commit_seq is not None else None,
        "source": "runtime_host",
    }
    if canonical:
        state = session.session_state
        payload.update(
            {
                "authority": "runtime_host",
                "state_contract_version": state.state_contract_version,
                "presentation_policy_version": state.presentation_policy_version,
                "mode": state.mode,
                "presentation": {
                    "primary": state.presentation.primary.model_dump(mode="json") if state.presentation.primary else None,
                    "access": state.presentation.access.model_dump(mode="json") if state.presentation.access else None,
                },
                "activity": state.activity.model_dump(
                    mode="json",
                    include={"state", "raw_kind", "tool", "source", "observed_at", "valid_until"},
                ),
                "control": {
                    "ownership": state.control.ownership,
                    "connection": state.control.connection,
                    "control_plane": state.control.control_plane,
                    "actions": {
                        "terminate": state.control.actions.terminate.model_dump(mode="json"),
                        "reattach": state.control.actions.reattach.model_dump(mode="json"),
                    },
                },
                "run": state.run.model_dump(mode="json", include={"id", "lifecycle"}) if state.run else None,
                "pending_interaction": (
                    state.pending_interaction.model_dump(
                        mode="json",
                        include={"id", "kind", "opened_at", "can_respond"},
                    )
                    if state.pending_interaction
                    else None
                ),
            }
        )
    if fanout:
        payload["fanout_kind"] = fanout.get("kind")
        payload["server_fanout_at_ms"] = fanout.get("server_fanout_at_ms")
    return {key: value for key, value in payload.items() if value is not None}


def _machine_session_delta_signature(payload: dict[str, Any]) -> str:
    rendered = {key: value for key, value in payload.items() if key not in {"commit_seq", "fanout_kind", "server_fanout_at_ms"}}
    return json.dumps(rendered, sort_keys=True, separators=(",", ":"))


async def stream_live_catalog_machine_sessions(
    request,
    *,
    params: TimelineSessionListParams,
    skip_initial_replay: bool,
    owner_id: int | None = None,
):
    """Slim, targeted machine session stream; never serializes browser cards."""

    bus = get_pubsub()
    sequence = bus.peek_latest_seq(TOPIC_TIMELINE)
    previous: dict[str, str] = {}
    yield {"event": "connected", "data": json.dumps({"source": "runtime_host"})}

    if not skip_initial_replay:
        response = await asyncio.to_thread(
            list_live_catalog_timeline,
            params=params,
            owner_id=owner_id,
        )
        for card in response.sessions:
            initial_commit_seq = card.head.session_state.commit_seq
            delta = project_machine_session_delta(card.head, commit_seq=initial_commit_seq, canonical=True)
            signature = _machine_session_delta_signature(delta)
            previous[card.head.id] = signature
            yield {
                "event": "session_delta",
                "data": json.dumps(delta, sort_keys=True, separators=(",", ":")),
            }

    with bus.subscribe(TOPIC_TIMELINE, since_seq=sequence) as subscription:
        while not await request.is_disconnected():
            message = await subscription.next_message(timeout=30.0)
            if message is None:
                yield {
                    "event": "heartbeat",
                    "data": json.dumps({"source": "runtime_host"}),
                }
                continue
            session_id = str(message.payload.get("session_id") or "")
            if not session_id:
                continue
            try:
                session, _provider_alias, commit_seq = await asyncio.to_thread(
                    read_live_catalog_session,
                    UUID(session_id),
                    include_hidden=False,
                    owner_id=owner_id,
                )
            except (ValueError, TypeError):
                continue
            if session is None or (params.device_id and session.device_id != params.device_id):
                if session_id in previous:
                    previous.pop(session_id, None)
                    yield {
                        "event": "session_remove",
                        "data": json.dumps({"session_id": session_id, "source": "runtime_host"}),
                    }
                continue
            delta = project_machine_session_delta(session, commit_seq=commit_seq, canonical=True)
            signature = _machine_session_delta_signature(delta)
            if previous.get(session_id) == signature:
                continue
            previous[session_id] = signature
            event_delta = project_machine_session_delta(
                session,
                commit_seq=commit_seq,
                fanout=message.payload,
                canonical=True,
            )
            yield {
                "event": "session_delta",
                "data": json.dumps(event_delta, sort_keys=True, separators=(",", ":")),
            }


async def stream_live_catalog_timeline(
    request,
    *,
    params: TimelineSessionListParams,
    skip_initial_replay: bool,
    owner_id: int | None = None,
):
    """SSE list stream driven by the existing timeline pubsub wake signal."""

    bus = get_pubsub()
    sequence = bus.peek_latest_seq(TOPIC_TIMELINE)
    previous: dict[str, str] = {}
    previous_total: int | None = None
    last_heartbeat = monotonic()
    yield {"event": "connected", "data": json.dumps({"message": "Timeline session stream connected"})}

    with bus.subscribe(TOPIC_TIMELINE, since_seq=sequence) as subscription:
        while not await request.is_disconnected():
            if skip_initial_replay:
                skip_initial_replay = False
            else:
                response = await asyncio.to_thread(
                    list_live_catalog_timeline,
                    params=params,
                    owner_id=owner_id,
                )
                current = {
                    card.thread_id: json.dumps(card.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
                    for card in response.sessions
                }
                for thread_id in sorted(previous.keys() - current.keys()):
                    yield {
                        "event": "session_remove",
                        "data": json.dumps(
                            {
                                "thread_id": thread_id,
                                "total": response.total,
                                "has_real_sessions": response.has_real_sessions,
                            }
                        ),
                    }
                for card in response.sessions:
                    signature = current[card.thread_id]
                    if previous.get(card.thread_id) == signature:
                        continue
                    yield {
                        "event": "session_upsert",
                        "data": json.dumps(
                            {
                                "session": card.model_dump(mode="json"),
                                "total": response.total,
                                "has_real_sessions": response.has_real_sessions,
                            }
                        ),
                    }
                previous = current
                previous_total = response.total

            now = monotonic()
            if now - last_heartbeat >= 30.0:
                yield {
                    "event": "heartbeat",
                    "data": json.dumps(
                        {
                            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                            "total": previous_total,
                        }
                    ),
                }
                last_heartbeat = now
            await subscription.next_message(timeout=5.0)
