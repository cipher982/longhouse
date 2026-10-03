"""One read model for the Machines surface: directory, activity and sync per machine.

The Machines page (web) and the Machines screen (iOS) show, for every enrolled
machine, three independent axes side by side: the live control connection
(the machine directory), what happened there (sessions started per day, the
latest session, and what the Timeline currently shows under "Live now"), and
whether its Machine Agent is shipping (the latest catalog heartbeat). Each axis
comes from its own authority and none is inferred from another.
"""

from __future__ import annotations

from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from zerg.schemas.machines import MachineActivity
from zerg.schemas.machines import MachineActivityDay
from zerg.schemas.machines import MachineDirectoryEntry
from zerg.schemas.machines import MachineHistorySync
from zerg.schemas.machines import MachineProjectCount
from zerg.schemas.machines import MachineSessionBrief
from zerg.schemas.machines import MachinesSummaryResponse
from zerg.schemas.machines import MachineSummary
from zerg.schemas.machines import MachineSync
from zerg.services.agent_heartbeat_health import MachineTransportHealthSummary
from zerg.services.agent_heartbeat_health import machine_transport_health_from_catalog_rows
from zerg.services.catalog_read_gateway import enrolled_machines
from zerg.services.catalog_read_gateway import machine_activity
from zerg.services.catalog_read_gateway import machine_heartbeats
from zerg.services.live_catalog_timeline import list_live_catalog_timeline
from zerg.services.machines_directory import build_machines_directory
from zerg.services.timeline_session_listing import TimelineSessionListParams

LIVE_SESSIONS_SHOWN = 5
# Sync evidence older than this is not shown at all: a machine that has not
# reported in a month has no sync state worth describing.
SYNC_EVIDENCE_WINDOW = timedelta(days=30)
# The catalog's timeline RPC pages at most this many rows.
_TIMELINE_PAGE_LIMIT = 200
# Unread-plus-open rows read per machine to find its live sessions: five pages.
# A real machine has a handful of open sessions; this bounds a pathological
# unread backlog without silently cutting at the first page.
_MAX_LIVE_ROWS = 5 * _TIMELINE_PAGE_LIMIT


def build_machines_summary(*, owner_id: int, days: int, utc_offset_minutes: int) -> MachinesSummaryResponse:
    enrollments = enrolled_machines(owner_id).get("enrollments", [])
    directory = build_machines_directory(owner_id=owner_id, enrollments=enrollments)
    activity_payload = machine_activity(owner_id=owner_id, days_back=days, utc_offset_minutes=utc_offset_minutes)
    observed_at = _parse_datetime(activity_payload.get("observed_at")) or datetime.now(timezone.utc)
    heartbeats = machine_heartbeats(
        owner_id=owner_id,
        device_id=None,
        recent_after=(observed_at - SYNC_EVIDENCE_WINDOW).isoformat(),
        limit=100,
    )
    transport, _total = machine_transport_health_from_catalog_rows(heartbeats.get("heartbeats", []), limit=100)
    sync_by_device = {item.device_id: item for item in transport}
    activity_by_device = {str(item["device_id"]): item for item in activity_payload.get("machines", [])}

    last_day = (observed_at + timedelta(minutes=utc_offset_minutes)).date()
    first_day = last_day - timedelta(days=days - 1)
    calendar = [first_day + timedelta(days=offset) for offset in range(days)]

    machines: list[MachineSummary] = []
    for entry in directory:
        machine = MachineDirectoryEntry(**entry.to_response())
        raw_activity = activity_by_device.get(machine.device_id)
        machines.append(
            MachineSummary(
                machine=machine,
                activity=_activity(raw_activity, calendar=calendar, owner_id=owner_id, device_id=machine.device_id, days=days),
                sync=_sync(sync_by_device.get(machine.device_id)),
            )
        )
    return MachinesSummaryResponse(
        generated_at=observed_at,
        days=days,
        utc_offset_minutes=utc_offset_minutes,
        first_day=first_day.isoformat(),
        last_day=last_day.isoformat(),
        machines=machines,
    )


def _activity(
    raw: dict[str, Any] | None,
    *,
    calendar: list[date],
    owner_id: int,
    device_id: str,
    days: int,
) -> MachineActivity:
    raw = raw or {}
    by_day = {str(item["date"]): dict(item.get("by_provider") or {}) for item in raw.get("daily", [])}
    daily = [
        MachineActivityDay(
            date=day.isoformat(), total=sum(by_day.get(day.isoformat(), {}).values()), by_provider=by_day.get(day.isoformat(), {})
        )
        for day in calendar
    ]
    latest = raw.get("latest")
    live_count, live_sessions = _live_sessions(
        owner_id=owner_id,
        device_id=device_id,
        days=days,
        # Unread rows sort ahead of open ones, so the first unread + open
        # rows of the machine's timeline hold every open candidate.
        candidates=int(raw.get("open_candidates") or 0) + int(raw.get("unread") or 0),
        has_open_candidates=int(raw.get("open_candidates") or 0) > 0,
    )
    return MachineActivity(
        sessions_started=int(raw.get("sessions_started") or 0),
        daily=daily,
        top_projects=[MachineProjectCount(**item) for item in raw.get("top_projects", [])],
        latest_session=(
            MachineSessionBrief(
                session_id=str(latest["session_id"]),
                title=str(latest.get("title") or ""),
                project=latest.get("project"),
                provider=latest.get("provider"),
                last_activity_at=_parse_datetime(latest.get("last_activity_at")),
            )
            if isinstance(latest, dict)
            else None
        ),
        live_count=live_count,
        live_sessions=live_sessions,
    )


def _live_sessions(
    *,
    owner_id: int,
    device_id: str,
    days: int,
    candidates: int,
    has_open_candidates: bool,
) -> tuple[int, list[MachineSessionBrief]]:
    """Project the served working set; the SQL open flag is only a superset.

    The timeline orders unread, then open, then recency, so every open
    candidate sits inside its first ``candidates`` rows. Those rows are paged
    rather than cut at one RPC page; a machine with more unread-plus-open
    sessions than ``_MAX_LIVE_ROWS`` reports the open ones it found there.
    """

    if not has_open_candidates:
        return 0, []
    live = []
    wanted = min(candidates, _MAX_LIVE_ROWS)
    for offset in range(0, wanted, _TIMELINE_PAGE_LIMIT):
        limit = min(_TIMELINE_PAGE_LIMIT, wanted - offset)
        listed = list_live_catalog_timeline(
            params=TimelineSessionListParams(
                project=None,
                provider=None,
                environment=None,
                include_test=False,
                hide_autonomous=True,
                include_automation=False,
                include_hidden=False,
                device_id=device_id,
                days_back=days,
                query=None,
                limit=limit,
                offset=offset,
                sort=None,
                mode="lexical",
                context_mode="forensic",
            ),
            owner_id=owner_id,
        )
        live.extend(card.head for card in listed.sessions if card.head.session_state.working_set == "open")
        if len(listed.sessions) < limit:
            break
    live.sort(key=lambda head: head.last_activity_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return len(live), [
        MachineSessionBrief(
            session_id=head.id,
            title=head.timeline_title or head.anchor_title or head.summary_title or "",
            project=head.project,
            provider=head.provider,
            last_activity_at=head.last_activity_at,
            activity_state=head.session_state.activity.state,
        )
        for head in live[:LIVE_SESSIONS_SHOWN]
    ]


def _sync(item: MachineTransportHealthSummary | None) -> MachineSync | None:
    if item is None:
        return None
    history = item.history_import
    inventory = history.inventory
    progress = history.progress
    return MachineSync(
        reported_at=item.last_heartbeat_at,
        report_age_seconds=item.heartbeat_age_seconds,
        stale=item.is_stale,
        status=item.status,
        status_summary=item.status_summary,
        engine_version=item.version,
        last_upload_at=item.last_ship_at,
        upload_p95_ms=item.ship_latency_p95_ms_1h,
        waiting_uploads=item.spool_pending,
        failed_uploads=item.spool_dead,
        history=MachineHistorySync(
            state=history.state,
            source_count=inventory.source_count if inventory is not None else None,
            remaining_bytes=progress.remaining_source_bytes if progress is not None else None,
            remaining_records=progress.remaining_records if progress is not None else None,
            acknowledged_records=progress.acknowledged_records if progress is not None else None,
        ),
    )


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
