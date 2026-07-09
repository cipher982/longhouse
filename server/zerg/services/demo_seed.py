"""Demo seed/reset helpers shared by startup and API routes."""

from __future__ import annotations

import logging
from datetime import datetime
from datetime import timezone

from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.models.agents import SessionRun
from zerg.models.agents import SessionThread
from zerg.models.agents import SessionThreadAlias
from zerg.services.agents import AgentsStore
from zerg.services.agents.kernel_writes import ensure_primary_thread
from zerg.services.agents.kernel_writes import record_run
from zerg.services.agents.kernel_writes import upsert_connection_for_run
from zerg.services.demo_sessions import build_demo_agent_sessions
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import ingest_runtime_events
from zerg.services.session_runtime import runtime_key_for_session

logger = logging.getLogger(__name__)

DEMO_PROVIDER_SESSION_PREFIX = "demo-"

# This is the public capture contract. Keep it in the shared seed service so
# `longhouse serve --demo`, admin seeding, and screenshot generation agree.
DEMO_PRESENTATION = {
    "demo-claude-01": (
        "Ship semantic search across 12,000 sessions",
        "Added embedding-backed recall with an FTS fallback, migration coverage, and a clean timeline toggle.",
    ),
    "demo-codex-01": (
        "Eliminate duplicate session ingest under load",
        "Traced a check-then-insert race and replaced it with a database-enforced idempotent ingest path.",
    ),
    "demo-antigravity-01": (
        "Fix the empty morning digest",
        "Found a timezone mismatch in the reporting window and verified the next scheduled delivery end to end.",
    ),
    "demo-claude-02": (
        "Add fast watermark detection to the photo pipeline",
        "Built a lightweight OpenCV preflight, friendly validation errors, and representative image tests.",
    ),
    "demo-codex-02": (
        "Polish the timeline search experience",
        "Wired the semantic-search control into the UI and validated responsive layout, types, and lint.",
    ),
    "demo-claude-03": (
        "Recover a production host from a memory spike",
        "Isolated an unbounded embedding backfill, restored headroom without downtime, " "and made processing stream in batches.",
    ),
    "demo-antigravity-02": (
        "Make session recall actionable",
        "Connected related past work to the active session and kept the recovery path visible while editing.",
    ),
    "demo-claude-04": (
        "Map failure patterns across every project",
        "Compared recent incidents across repositories and distilled recurring causes into practical guardrails.",
    ),
    "demo-codex-03": (
        "Repair the release image build",
        "Diagnosed the missing native dependency in CI and verified the rebuilt runtime image passed.",
    ),
    "demo-claude-05": (
        "Resolve concurrent OAuth refresh races",
        "Tracing a duplicated refresh-token path so multiple browser tabs recover " "cleanly without invalidating each other.",
    ),
}

DEMO_RUNTIME = {
    "demo-claude-05": ("claude_channel_bridge", "spawned_control", "attached", "running", "Bash"),
    "demo-codex-03": ("codex_bridge", "spawned_control", "attached", "thinking", None),
    "demo-claude-04": ("opencode_server_bridge", "spawned_control", "attached", "needs_user", None),
    "demo-antigravity-02": ("cursor_helm", "spawned_control", "attached", "running", "Edit"),
    "demo-claude-03": ("claude_channel_bridge", "spawned_control", "detached", "needs_user", None),
    "demo-antigravity-01": ("antigravity_hook_inbox", "observe_only", "attached", "running", "Bash"),
}


def is_demo_provider_session_id(provider_session_id: str | None) -> bool:
    """Return True when provider_session_id uses the demo seed prefix."""
    return bool(provider_session_id and provider_session_id.startswith(DEMO_PROVIDER_SESSION_PREFIX))


def get_existing_demo_provider_session_ids(db: Session) -> set[str]:
    """Return currently-seeded demo provider session ids.

    Post session-identity-kernel cleanup: ``provider_session_id`` is no
    longer a column on ``AgentSession``. The truth lives on
    ``session_thread_aliases`` rows scoped to each session's primary
    thread, so we filter there instead.
    """
    rows = (
        db.query(SessionThreadAlias.alias_value)
        .filter(SessionThreadAlias.alias_kind == "provider_session_id")
        .filter(SessionThreadAlias.alias_value.like(f"{DEMO_PROVIDER_SESSION_PREFIX}%"))
        .all()
    )
    return {row[0] for row in rows if row[0]}


def delete_demo_sessions(db: Session) -> int:
    """Delete all demo sessions and return number of rows removed.

    Find sessions via their primary ``session_thread_aliases`` row whose
    ``alias_value`` starts with the demo prefix. Cascading FKs handle the
    kernel rows + events + source lines.
    """
    session_ids = (
        db.query(SessionThread.session_id)
        .join(
            SessionThreadAlias,
            SessionThreadAlias.thread_id == SessionThread.id,
        )
        .filter(SessionThreadAlias.alias_kind == "provider_session_id")
        .filter(SessionThreadAlias.alias_value.like(f"{DEMO_PROVIDER_SESSION_PREFIX}%"))
        .distinct()
        .all()
    )
    ids = [sid for (sid,) in session_ids if sid is not None]
    if not ids:
        return 0
    deleted = db.query(AgentSession).filter(AgentSession.id.in_(ids)).delete(synchronize_session=False)
    db.commit()
    return int(deleted or 0)


def _demo_sessions_by_provider_id(db: Session) -> dict[str, AgentSession]:
    rows = (
        db.query(AgentSession, SessionThreadAlias.alias_value)
        .join(SessionThread, SessionThread.session_id == AgentSession.id)
        .join(SessionThreadAlias, SessionThreadAlias.thread_id == SessionThread.id)
        .filter(SessionThread.is_primary == 1)
        .filter(SessionThreadAlias.alias_kind == "provider_session_id")
        .filter(SessionThreadAlias.alias_value.in_(DEMO_PRESENTATION))
        .all()
    )
    return {str(provider_session_id): session for session, provider_session_id in rows}


def _seed_demo_presentation(db: Session, sessions_by_provider_id: dict[str, AgentSession], *, anchor: datetime) -> None:
    """Seed titles and the kernel/runtime facts used by public demo captures."""
    if set(sessions_by_provider_id) != set(DEMO_PRESENTATION):
        return

    for provider_session_id, (title, summary) in DEMO_PRESENTATION.items():
        session = sessions_by_provider_id[provider_session_id]
        session.summary_title = title
        session.anchor_title = title
        session.summary = summary
        session.summary_event_count = session.user_messages + session.assistant_messages + session.tool_calls

    runtime_events = []
    for provider_session_id, runtime in DEMO_RUNTIME.items():
        control_plane, acquisition_kind, connection_state, phase, tool_name = runtime
        session = sessions_by_provider_id[provider_session_id]
        thread = ensure_primary_thread(db, session)
        run = (
            db.query(SessionRun)
            .filter(SessionRun.thread_id == thread.id)
            .filter(SessionRun.launch_origin == "longhouse_spawned")
            .filter(SessionRun.ended_at.is_(None))
            .order_by(SessionRun.started_at.desc())
            .first()
        )
        if run is None:
            run = record_run(
                db,
                thread=thread,
                provider=session.provider,
                host_id=session.device_id,
                cwd=session.cwd,
                launch_origin="longhouse_spawned",
                started_at=session.started_at,
            )

        is_observe_only = acquisition_kind == "observe_only"
        connection = upsert_connection_for_run(
            db,
            run=run,
            control_plane=control_plane,
            acquisition_kind=acquisition_kind,
            state=connection_state,
            external_name=session.device_name,
            device_id=session.device_id,
            can_send_input=0 if is_observe_only else 1,
            can_interrupt=0 if is_observe_only else 1,
            can_terminate=0 if is_observe_only else 1,
            can_tail_output=1,
            can_resume=0 if is_observe_only else 1,
        )
        connection.last_health_at = anchor
        runtime_events.append(
            RuntimeEventIngest(
                runtime_key=runtime_key_for_session(session.provider, str(session.id)),
                session_id=session.id,
                thread_id=thread.id,
                run_id=run.id,
                provider=session.provider,
                device_id=session.device_id,
                source="engine_attached_lease",
                kind="phase_signal",
                phase=phase,
                tool_name=tool_name,
                occurred_at=anchor,
                # A repeat seed is a fresh demo heartbeat, rather than a
                # duplicate of an old phase observation.  Keep the value
                # deterministic within a capture while allowing a later
                # startup/admin seed to refresh the live-state lease.
                dedupe_key=f"demo-runtime:{provider_session_id}:{int(anchor.timestamp())}",
                payload={"lease_refresh_at": anchor.isoformat()},
            )
        )
    ingest_runtime_events(db, runtime_events)


def seed_missing_demo_sessions(db: Session, now: datetime | None = None) -> tuple[int, int]:
    """Seed only missing demo sessions.

    Returns:
        (seeded_count, failed_count)
    """
    existing_ids = get_existing_demo_provider_session_ids(db)
    seeded_count = 0
    failed_count = 0

    store = AgentsStore(db)
    anchor = now or datetime.now(timezone.utc)
    sessions = build_demo_agent_sessions(anchor)

    for session in sessions:
        provider_session_id = session.provider_session_id
        if not is_demo_provider_session_id(provider_session_id):
            logger.warning(
                "Skipping demo seed session with non-demo provider_session_id: %s",
                provider_session_id or "<missing>",
            )
            continue
        if provider_session_id in existing_ids:
            continue

        try:
            store.ingest_session(session, trigger_initial_title_generation=False)
            seeded_count += 1
            existing_ids.add(provider_session_id)
        except Exception:
            failed_count += 1
            db.rollback()
            logger.exception("Demo seed failed for provider_session_id=%s", provider_session_id)

    _seed_demo_presentation(db, _demo_sessions_by_provider_id(db), anchor=anchor)

    # IngestSession commits each session; this commit persists the deterministic
    # presentation/runtime state and any FTS rebuild.
    if seeded_count > 0:
        store.rebuild_fts()
    db.commit()

    return seeded_count, failed_count
