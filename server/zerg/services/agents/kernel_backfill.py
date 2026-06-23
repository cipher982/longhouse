"""Idempotent backfill helpers for the session identity kernel.

Phase 1 created a root thread per session. Phase 3 stamps ``thread_id`` on
every legacy child row that still has it NULL, and synthesizes a single
``external_adopted`` run + connection per session so the kernel has a
complete view of historical sessions. Live launchers continue to write
their own runs/connections.

Most helpers are additive. The subagent cleanup intentionally rewrites legacy
rows that were previously attached to false top-level child sessions.

See docs/specs/session-identity-kernel.md.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from datetime import timezone
from uuid import UUID

from sqlalchemy import func
from sqlalchemy import text
from sqlalchemy import update as sql_update
from sqlalchemy.orm import Session

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentSessionBranch
from zerg.models.agents import AgentSourceLine
from zerg.models.agents import SessionConnection
from zerg.models.agents import SessionEdge
from zerg.models.agents import SessionEmbedding
from zerg.models.agents import SessionInput
from zerg.models.agents import SessionLaunchAttempt
from zerg.models.agents import SessionObservation
from zerg.models.agents import SessionRun
from zerg.models.agents import SessionRuntimeState
from zerg.models.agents import SessionTask
from zerg.models.agents import SessionThread
from zerg.models.agents import SessionThreadAlias
from zerg.models.agents import SessionTurn
from zerg.models.agents import TimelineCard
from zerg.services.agents.session_graph_writes import ensure_subagent_thread
from zerg.services.agents.session_graph_writes import record_session_edge
from zerg.services.agents.session_graph_writes import resolve_thread_by_provider_session_id
from zerg.services.raw_json_compression import decode_raw_json
from zerg.services.session_kernel_projection import project_provider_session_id

_CLAUDE_SUBAGENT_PARENT_RE = re.compile(
    r"/(?P<parent>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/subagents/"
)
_CLAUDE_AGENT_FILE_RE = re.compile(r"/agent-(?P<agent>[^/]+)\.jsonl$")
# Dynamic-workflow control ledger: `.../subagents/workflows/<run>/journal.jsonl`.
# It lives under `/subagents/` but is NOT a subagent transcript, so it must be
# excluded from subagent relink candidates (otherwise it gets re-parented into
# the workflow's parent as an empty child thread).
_CLAUDE_WORKFLOW_JOURNAL_RE = re.compile(r"/subagents/workflows/[^/]+/journal\.jsonl$")


def _is_workflow_journal_path(source_path: str | None) -> bool:
    if not source_path:
        return False
    return _CLAUDE_WORKFLOW_JOURNAL_RE.search(source_path.replace("\\", "/")) is not None


def _ensure_head_branch(db: Session, session_id: UUID) -> AgentSessionBranch:
    head = (
        db.query(AgentSessionBranch)
        .filter(AgentSessionBranch.session_id == session_id)
        .filter(AgentSessionBranch.is_head == 1)
        .order_by(AgentSessionBranch.id.desc())
        .first()
    )
    if head is not None:
        return head
    head = AgentSessionBranch(
        session_id=session_id,
        parent_branch_id=None,
        branched_at_source_path=None,
        branched_at_offset=None,
        branch_reason="root",
        is_head=1,
    )
    db.add(head)
    db.flush()
    return head


def _subagent_source_parent(source_path: str | None) -> str | None:
    if not source_path:
        return None
    match = _CLAUDE_SUBAGENT_PARENT_RE.search(source_path.replace("\\", "/"))
    return match.group("parent") if match is not None else None


def _subagent_id_from_source_path(source_path: str | None) -> str | None:
    if not source_path:
        return None
    match = _CLAUDE_AGENT_FILE_RE.search(source_path.replace("\\", "/"))
    return match.group("agent") if match is not None else None


def _sidechain_metadata_from_raw(raw_json: str | None) -> tuple[str | None, str | None, str | None]:
    if not raw_json:
        return None, None, None
    try:
        value = json.loads(raw_json)
    except json.JSONDecodeError:
        return None, None, None
    if not isinstance(value, dict) or value.get("isSidechain") is not True:
        return None, None, None
    parent = value.get("sessionId")
    agent_id = value.get("agentId")
    prompt_id = value.get("promptId")
    return (
        parent if isinstance(parent, str) else None,
        agent_id if isinstance(agent_id, str) else None,
        prompt_id if isinstance(prompt_id, str) else None,
    )


def _candidate_subagent_sessions(db: Session) -> dict[UUID, set[str]]:
    candidates: dict[UUID, set[str]] = {}
    for session_id, source_path in (
        db.query(AgentSourceLine.session_id, AgentSourceLine.source_path).filter(AgentSourceLine.source_path.like("%/subagents/%")).all()
    ):
        if _is_workflow_journal_path(source_path):
            continue
        candidates.setdefault(session_id, set()).add(source_path)
    for session_id, source_path in (
        db.query(AgentEvent.session_id, AgentEvent.source_path).filter(AgentEvent.source_path.like("%/subagents/%")).all()
    ):
        if _is_workflow_journal_path(source_path):
            continue
        candidates.setdefault(session_id, set()).add(source_path)
    return candidates


def _raw_sidechain_metadata_for_session(db: Session, session_id: UUID) -> tuple[str | None, str | None, str | None]:
    for row in (
        db.query(AgentSourceLine)
        .filter(AgentSourceLine.session_id == session_id)
        .order_by(AgentSourceLine.source_offset.asc(), AgentSourceLine.id.asc())
        .limit(50)
        .all()
    ):
        parent, agent_id, prompt_id = _sidechain_metadata_from_raw(decode_raw_json(row))
        if parent:
            return parent, agent_id, prompt_id
    for row in (
        db.query(AgentEvent)
        .filter(AgentEvent.session_id == session_id)
        .order_by(AgentEvent.source_offset.asc(), AgentEvent.id.asc())
        .limit(50)
        .all()
    ):
        parent, agent_id, prompt_id = _sidechain_metadata_from_raw(decode_raw_json(row))
        if parent:
            return parent, agent_id, prompt_id
    return None, None, None


def backfill_root_threads(db: Session) -> dict[str, int]:
    """Ensure every AgentSession has a primary thread row.

    Idempotent: re-running on the same database produces the same result with
    no duplicate rows. Order-independent: the function may be called repeatedly
    or interleaved with new session creation without producing inconsistent
    state.

    Returns counts: {sessions_seen, threads_created, primary_pointers_set,
    aliases_created}.
    """

    sessions_seen = 0
    threads_created = 0
    primary_pointers_set = 0
    aliases_created = 0

    # Cheap early-out for converged DBs: no sessions are missing
    # primary_thread_id. (Aliases and per-session thread checks still need a
    # walk if pointers are set but a session lacks an alias — we rely on the
    # caller's idempotency for that, since it's the rarer fix-up path.)
    if db.query(AgentSession.id).filter(AgentSession.primary_thread_id.is_(None)).limit(1).first() is None:
        return {
            "sessions_seen": 0,
            "threads_created": 0,
            "primary_pointers_set": 0,
            "aliases_created": 0,
        }

    sessions = db.query(AgentSession).all()
    for session in sessions:
        sessions_seen += 1

        thread = db.query(SessionThread).filter(SessionThread.session_id == session.id, SessionThread.is_primary == 1).one_or_none()
        if thread is None:
            thread = SessionThread(
                session_id=session.id,
                provider=session.provider,
                branch_kind="root",
                is_primary=1,
            )
            db.add(thread)
            db.flush()
            threads_created += 1

        if session.primary_thread_id != thread.id:
            session.primary_thread_id = thread.id
            primary_pointers_set += 1

    db.flush()
    return {
        "sessions_seen": sessions_seen,
        "threads_created": threads_created,
        "primary_pointers_set": primary_pointers_set,
        "aliases_created": aliases_created,
    }


_CHILD_THREAD_ID_TABLES = (
    AgentEvent,
    AgentSourceLine,
    SessionObservation,
    SessionTurn,
    SessionInput,
    SessionRuntimeState,
)


def backfill_child_thread_ids(db: Session) -> dict[str, int]:
    """Stamp thread_id on every legacy child row that's still NULL.

    Each child table has a ``session_id`` column; the backfill resolves the
    primary thread per session and bulk-updates rows whose thread_id is
    currently NULL. Rows that already carry a thread_id are never touched.

    Idempotent: re-running on a fully-backfilled DB is a no-op. Includes a
    cheap early-out so a converged DB exits in O(tables) probes instead of
    O(sessions × tables) per-session updates.
    """

    counts: dict[str, int] = {model.__tablename__: 0 for model in _CHILD_THREAD_ID_TABLES}

    # Cheap early-out: if no child row anywhere has thread_id IS NULL, we're done.
    has_null = False
    for model in _CHILD_THREAD_ID_TABLES:
        if db.query(model.thread_id).filter(model.thread_id.is_(None)).limit(1).first() is not None:
            has_null = True
            break
    if not has_null:
        return counts

    primaries = dict(db.query(SessionThread.session_id, SessionThread.id).filter(SessionThread.is_primary == 1).all())
    for model in _CHILD_THREAD_ID_TABLES:
        updated = 0
        for session_id, thread_id in primaries.items():
            stmt = sql_update(model).where(model.session_id == session_id, model.thread_id.is_(None)).values(thread_id=thread_id)
            result = db.execute(stmt)
            updated += int(result.rowcount or 0)
        counts[model.__tablename__] = updated
    db.flush()
    return counts


def cleanup_workflow_journal_sessions(db: Session) -> dict[str, int]:
    """Remove junk sessions ingested from a dynamic-workflow ``journal.jsonl``.

    Before the engine learned to skip ``.../subagents/workflows/<run>/journal.jsonl``,
    each workflow run leaked one empty session: zero role events, only archived
    source lines for the control ledger, ``ended_at IS NULL`` so it slipped past
    the timeline filter. This sweep finds sessions whose source evidence is
    EXCLUSIVELY workflow-journal lines and deletes them along with their kernel
    rows. Idempotent: a second run resolves nothing.
    """

    sessions_removed = 0
    source_lines_deleted = 0

    # Sessions that have at least one workflow-journal source line.
    journal_session_ids: set[UUID] = set()
    for (session_id,) in (
        db.query(AgentSourceLine.session_id)
        .filter(AgentSourceLine.source_path.like("%/subagents/workflows/%/journal.jsonl"))
        .distinct()
        .all()
    ):
        journal_session_ids.add(session_id)

    for session_id in journal_session_ids:
        # Only remove sessions that are PURELY journal junk: no role events, and
        # every source line is a workflow-journal line. Never touch a session
        # that also carries real transcript rows.
        if db.query(AgentEvent).filter(AgentEvent.session_id == session_id).limit(1).count() > 0:
            continue
        source_paths = [
            row.source_path for row in db.query(AgentSourceLine.source_path).filter(AgentSourceLine.session_id == session_id).all()
        ]
        if not source_paths or any(not _is_workflow_journal_path(p) for p in source_paths):
            continue

        thread_ids = [row.id for row in db.query(SessionThread.id).filter(SessionThread.session_id == session_id).all()]
        source_lines_deleted += db.query(AgentSourceLine).filter(AgentSourceLine.session_id == session_id).delete(synchronize_session=False)
        # Source-line ingest also records a session-scoped SessionObservation per
        # line (not an FK); delete them so they don't linger/replay after the
        # journal session is gone.
        db.query(SessionObservation).filter(SessionObservation.session_id == session_id).delete(synchronize_session=False)
        db.query(SessionTask).filter(SessionTask.session_id == str(session_id)).delete(synchronize_session=False)
        db.query(SessionEmbedding).filter(SessionEmbedding.session_id == session_id).delete(synchronize_session=False)
        db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == session_id).delete(synchronize_session=False)
        if thread_ids:
            db.query(SessionThreadAlias).filter(SessionThreadAlias.thread_id.in_(thread_ids)).delete(synchronize_session=False)
            db.query(SessionThread).filter(SessionThread.id.in_(thread_ids)).delete(synchronize_session=False)
        db.query(AgentSessionBranch).filter(AgentSessionBranch.session_id == session_id).delete(synchronize_session=False)
        db.query(TimelineCard).filter(TimelineCard.session_id == session_id).delete(synchronize_session=False)
        db.query(AgentSession).filter(AgentSession.id == session_id).delete(synchronize_session=False)
        sessions_removed += 1

    db.flush()
    return {
        "journal_sessions_seen": len(journal_session_ids),
        "sessions_removed": sessions_removed,
        "source_lines_deleted": source_lines_deleted,
    }


def _move_subagent_session_under_parent(
    db: Session,
    *,
    child_session: AgentSession,
    parent_thread: SessionThread,
    source_path: str | None,
    raw_agent_id: str | None,
    raw_prompt_id: str | None,
    parent_provider_id: str,
    child_provider_id: str | None = None,
    workflow_run_id: str | None = None,
    attribution_agent: str | None = None,
    attribution_skill: str | None = None,
) -> dict[str, int]:
    """Re-stamp a leaked standalone subagent session's transcript/runtime rows
    onto its parent session + a child ``SessionThread``, then remove the now-empty
    standalone session. Returns per-table move counts plus ``sessions_removed``.

    Shared by the one-shot backfill sweep and the live relink-on-parent-ingest
    path so both behave identically.
    """
    child_session_id = child_session.id
    child_provider_id = str(child_provider_id or "").strip()
    child_edge_id = child_provider_id or str(child_session.id)
    counts = {
        "events_moved": 0,
        "source_lines_moved": 0,
        "observations_moved": 0,
        "turns_moved": 0,
        "inputs_moved": 0,
        "runtime_rows_moved": 0,
        "runs_moved": 0,
        "legacy_tasks_deleted": 0,
        "embeddings_deleted": 0,
        "sessions_removed": 0,
    }
    old_thread_ids = [row.id for row in db.query(SessionThread.id).filter(SessionThread.session_id == child_session_id).all()]
    if child_provider_id and old_thread_ids:
        db.query(SessionThreadAlias).filter(SessionThreadAlias.thread_id.in_(old_thread_ids)).filter(
            SessionThreadAlias.provider == child_session.provider
        ).filter(SessionThreadAlias.alias_kind == "provider_session_id").filter(SessionThreadAlias.alias_value == child_provider_id).delete(
            synchronize_session=False
        )

    child_thread = ensure_subagent_thread(
        db,
        parent_thread=parent_thread,
        provider=child_session.provider,
        source_path=source_path,
        child_longhouse_session_id=str(child_session.id),
        child_provider_session_id=child_provider_id or None,
        subagent_id=raw_agent_id or _subagent_id_from_source_path(source_path),
        subagent_prompt_id=raw_prompt_id,
        workflow_run_id=workflow_run_id,
        attribution_agent=attribution_agent,
        attribution_skill=attribution_skill,
        parent_provider_session_id=parent_provider_id,
    )
    if old_thread_ids:
        db.query(SessionEdge).filter(SessionEdge.source_thread_id.in_(old_thread_ids)).delete(synchronize_session=False)
        db.query(SessionEdge).filter(SessionEdge.target_thread_id.in_(old_thread_ids)).delete(synchronize_session=False)
    record_session_edge(
        db,
        provider=child_session.provider,
        edge_kind="task_child",
        visibility="hidden",
        evidence_kind="relink",
        source_thread=parent_thread,
        target_thread=child_thread,
        provider_edge_id=f"{parent_provider_id}:{child_edge_id}",
        metadata={
            "parent_provider_session_id": parent_provider_id,
            "child_provider_session_id": child_provider_id or None,
            "subagent_id": raw_agent_id or _subagent_id_from_source_path(source_path),
            "subagent_prompt_id": raw_prompt_id,
            "workflow_run_id": workflow_run_id,
            "attribution_agent": attribution_agent,
            "attribution_skill": attribution_skill,
        },
    )
    parent_branch = _ensure_head_branch(db, parent_thread.session_id)

    result = db.execute(
        sql_update(AgentEvent)
        .where(AgentEvent.session_id == child_session_id)
        .values(session_id=parent_thread.session_id, thread_id=child_thread.id, branch_id=parent_branch.id)
    )
    counts["events_moved"] += int(result.rowcount or 0)

    result = db.execute(
        sql_update(AgentSourceLine)
        .where(AgentSourceLine.session_id == child_session_id)
        .values(session_id=parent_thread.session_id, thread_id=child_thread.id, branch_id=parent_branch.id)
    )
    counts["source_lines_moved"] += int(result.rowcount or 0)

    result = db.execute(
        sql_update(SessionObservation)
        .where(SessionObservation.session_id == child_session_id)
        .values(session_id=parent_thread.session_id, thread_id=child_thread.id)
    )
    counts["observations_moved"] += int(result.rowcount or 0)

    result = db.execute(
        sql_update(SessionTurn)
        .where(SessionTurn.session_id == child_session_id)
        .values(session_id=parent_thread.session_id, thread_id=child_thread.id)
    )
    counts["turns_moved"] += int(result.rowcount or 0)

    result = db.execute(
        sql_update(SessionInput)
        .where(SessionInput.session_id == child_session_id)
        .values(session_id=parent_thread.session_id, thread_id=child_thread.id)
    )
    counts["inputs_moved"] += int(result.rowcount or 0)

    result = db.execute(
        sql_update(SessionRuntimeState)
        .where(SessionRuntimeState.session_id == child_session_id)
        .values(session_id=parent_thread.session_id, thread_id=child_thread.id)
    )
    counts["runtime_rows_moved"] += int(result.rowcount or 0)

    if old_thread_ids:
        result = db.execute(sql_update(SessionRun).where(SessionRun.thread_id.in_(old_thread_ids)).values(thread_id=child_thread.id))
        counts["runs_moved"] += int(result.rowcount or 0)
        db.execute(
            sql_update(SessionLaunchAttempt)
            .where(SessionLaunchAttempt.thread_id.in_(old_thread_ids))
            .values(session_id=parent_thread.session_id, thread_id=child_thread.id)
        )

    remaining = 0
    for model in (AgentEvent, AgentSourceLine, SessionObservation, SessionTurn, SessionInput, SessionRuntimeState):
        remaining += db.query(model).filter(model.session_id == child_session_id).limit(1).count()
    if remaining == 0:
        counts["legacy_tasks_deleted"] += (
            db.query(SessionTask).filter(SessionTask.session_id == str(child_session_id)).delete(synchronize_session=False)
        )
        counts["embeddings_deleted"] += (
            db.query(SessionEmbedding).filter(SessionEmbedding.session_id == child_session_id).delete(synchronize_session=False)
        )
        if old_thread_ids:
            db.query(SessionEdge).filter(SessionEdge.source_thread_id.in_(old_thread_ids)).delete(synchronize_session=False)
            db.query(SessionEdge).filter(SessionEdge.target_thread_id.in_(old_thread_ids)).delete(synchronize_session=False)
            db.query(SessionThreadAlias).filter(SessionThreadAlias.thread_id.in_(old_thread_ids)).delete(synchronize_session=False)
            db.query(SessionThread).filter(SessionThread.id.in_(old_thread_ids)).delete(synchronize_session=False)
        db.query(AgentSessionBranch).filter(AgentSessionBranch.session_id == child_session_id).delete(synchronize_session=False)
        db.query(TimelineCard).filter(TimelineCard.session_id == child_session_id).delete(synchronize_session=False)
        db.query(AgentSession).filter(AgentSession.id == child_session_id).delete(synchronize_session=False)
        counts["sessions_removed"] += 1

    return counts


def _refresh_parent_counts(db: Session, parent_session_ids: set[UUID]) -> int:
    refreshed = 0
    for parent_session_id in parent_session_ids:
        parent_session = db.query(AgentSession).filter(AgentSession.id == parent_session_id).first()
        if parent_session is None:
            continue
        primary_thread_id = parent_session.primary_thread_id
        thread_filter = AgentEvent.thread_id == primary_thread_id if primary_thread_id is not None else text("1=1")
        parent_session.user_messages = (
            db.query(func.count(AgentEvent.id))
            .filter(AgentEvent.session_id == parent_session_id)
            .filter(thread_filter)
            .filter(AgentEvent.role == "user")
            .scalar()
            or 0
        )
        parent_session.assistant_messages = (
            db.query(func.count(AgentEvent.id))
            .filter(AgentEvent.session_id == parent_session_id)
            .filter(thread_filter)
            .filter(AgentEvent.role == "assistant")
            .scalar()
            or 0
        )
        parent_session.tool_calls = (
            db.query(func.count(AgentEvent.id))
            .filter(AgentEvent.session_id == parent_session_id)
            .filter(thread_filter)
            .filter(AgentEvent.tool_name.isnot(None))
            .scalar()
            or 0
        )
        parent_session.last_activity_at = (
            db.query(func.max(AgentEvent.timestamp)).filter(AgentEvent.session_id == parent_session_id).scalar()
            or parent_session.last_activity_at
        )
        parent_session.needs_embedding = True
        refreshed += 1
    return refreshed


def _rebuild_fts_if_sqlite(db: Session, touched: set[UUID]) -> int:
    bind = db.get_bind()
    if getattr(getattr(bind, "dialect", None), "name", None) == "sqlite" and touched:
        fts_exists = db.execute(text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='events_fts' LIMIT 1")).first()
        if fts_exists is not None:
            db.execute(text("INSERT INTO events_fts(events_fts) VALUES('rebuild')"))
            return 1
    return 0


def relink_orphan_subagents_for_parent(
    db: Session,
    *,
    provider: str,
    parent_provider_session_id: str | None,
) -> dict[str, int]:
    """Self-heal: when a parent session is ingested, re-parent any standalone
    subagent sessions that were ingested BEFORE it (ship-order race).

    Scoped to a SINGLE parent: only orphans whose primary thread carries a
    ``forked_from_provider_session_id`` alias equal to this parent are moved, so
    a live ingest never triggers a global scan. Idempotent — once an orphan is
    relinked its standalone session is gone, so a second call is a no-op.
    """
    summary = {"candidates_resolved": 0, "sessions_removed": 0}
    parent_provider_session_id = str(parent_provider_session_id or "").strip()
    if not parent_provider_session_id:
        return summary

    parent_thread = resolve_thread_by_provider_session_id(db, provider=provider, provider_session_id=parent_provider_session_id)
    if parent_thread is None:
        return summary

    # Orphans: primary subagent threads with a forked_from alias pointing here.
    orphan_thread_rows = (
        db.query(SessionThread)
        .join(SessionThreadAlias, SessionThreadAlias.thread_id == SessionThread.id)
        .filter(SessionThread.provider == provider)
        .filter(SessionThread.is_primary == 1)
        .filter(SessionThread.branch_kind == "subagent")
        # provider predicate first so the (provider, alias_kind, alias_value)
        # composite index drives this hot-path lookup.
        .filter(SessionThreadAlias.provider == provider)
        .filter(SessionThreadAlias.alias_kind == "forked_from_provider_session_id")
        .filter(SessionThreadAlias.alias_value == parent_provider_session_id)
        .all()
    )

    touched: set[UUID] = set()
    for orphan_thread in orphan_thread_rows:
        child_session = db.query(AgentSession).filter(AgentSession.id == orphan_thread.session_id).first()
        if child_session is None or child_session.id == parent_thread.session_id:
            continue
        # Never relink a workflow journal (excluded from candidates) or a session
        # that is not actually a leaked subagent.
        source_paths = {
            row.source_path
            for row in db.query(AgentSourceLine.source_path).filter(AgentSourceLine.session_id == child_session.id).all()
            if row.source_path and not _is_workflow_journal_path(row.source_path)
        }
        source_path = None
        for candidate_path in sorted(source_paths):
            if _subagent_source_parent(candidate_path):
                source_path = candidate_path
                break
        if source_path is None and source_paths:
            source_path = sorted(source_paths)[0]
        raw_parent_id, raw_agent_id, raw_prompt_id = _raw_sidechain_metadata_for_session(db, child_session.id)

        # Carry forward workflow attribution recorded on the orphan primary thread.
        orphan_labels = {
            row.alias_kind: row.alias_value
            for row in db.query(SessionThreadAlias).filter(SessionThreadAlias.thread_id == orphan_thread.id).all()
        }

        counts = _move_subagent_session_under_parent(
            db,
            child_session=child_session,
            parent_thread=parent_thread,
            source_path=source_path,
            raw_agent_id=raw_agent_id or orphan_labels.get("subagent_id") or _subagent_id_from_source_path(source_path),
            raw_prompt_id=raw_prompt_id,
            parent_provider_id=parent_provider_session_id,
            child_provider_id=orphan_labels.get("provider_session_id"),
            workflow_run_id=orphan_labels.get("workflow_run_id"),
            attribution_agent=orphan_labels.get("workflow_attribution_agent"),
            attribution_skill=orphan_labels.get("workflow_attribution_skill"),
        )
        summary["candidates_resolved"] += 1
        summary["sessions_removed"] += counts["sessions_removed"]
        touched.add(parent_thread.session_id)

    if touched:
        _refresh_parent_counts(db, touched)
        _rebuild_fts_if_sqlite(db, touched)
        db.flush()
    return summary


def backfill_subagent_child_threads(db: Session) -> dict[str, int]:
    """Move leaked provider subagent sessions under their parent session.

    Older engine/server pairs imported Claude ``subagents/agent-*.jsonl`` files
    as standalone sessions. This backfill resolves those rows by durable source
    evidence, creates a child ``SessionThread`` under the parent session, and
    re-stamps transcript/runtime rows to the parent session + child thread.
    """

    candidates_seen = 0
    totals = {
        "candidates_resolved": 0,
        "sessions_removed": 0,
        "events_moved": 0,
        "source_lines_moved": 0,
        "observations_moved": 0,
        "turns_moved": 0,
        "inputs_moved": 0,
        "runtime_rows_moved": 0,
        "runs_moved": 0,
        "legacy_tasks_deleted": 0,
        "embeddings_deleted": 0,
    }
    parent_sessions_touched: set[UUID] = set()

    for child_session_id, source_paths in _candidate_subagent_sessions(db).items():
        candidates_seen += 1
        child_session = db.query(AgentSession).filter(AgentSession.id == child_session_id).first()
        if child_session is None:
            continue

        source_path = sorted(source_paths)[0] if source_paths else None
        parent_provider_id = None
        for candidate_path in sorted(source_paths):
            parent_provider_id = _subagent_source_parent(candidate_path)
            if parent_provider_id:
                source_path = candidate_path
                break
        raw_parent_id, raw_agent_id, raw_prompt_id = _raw_sidechain_metadata_for_session(db, child_session_id)
        parent_provider_id = parent_provider_id or raw_parent_id
        if not parent_provider_id or str(parent_provider_id) == str(child_session_id):
            continue

        parent_thread = resolve_thread_by_provider_session_id(
            db,
            provider=child_session.provider,
            provider_session_id=parent_provider_id,
        )
        if parent_thread is None:
            continue

        counts = _move_subagent_session_under_parent(
            db,
            child_session=child_session,
            parent_thread=parent_thread,
            source_path=source_path,
            raw_agent_id=raw_agent_id,
            raw_prompt_id=raw_prompt_id,
            parent_provider_id=parent_provider_id,
            child_provider_id=project_provider_session_id(db, child_session),
        )
        totals["candidates_resolved"] += 1
        for key, value in counts.items():
            totals[key] = totals.get(key, 0) + value
        parent_sessions_touched.add(parent_thread.session_id)

    parent_counts_refreshed = _refresh_parent_counts(db, parent_sessions_touched)
    fts_rebuilt = _rebuild_fts_if_sqlite(db, parent_sessions_touched)

    db.flush()
    return {
        "candidates_seen": candidates_seen,
        **totals,
        "parent_counts_refreshed": parent_counts_refreshed,
        "fts_rebuilt": fts_rebuilt,
    }


def backfill_runs_and_connections(db: Session) -> dict[str, int]:
    """Synthesize one ``external_adopted`` run per primary thread that lacks
    one, plus a ``log_tail`` observe-only connection on the synthesized run.

    Phase 2 launchers create their own runs eagerly, so this only fills in
    history: pre-kernel sessions get a single run keyed to the primary thread.

    For ``run_id`` stamping on legacy ``SessionRuntimeState`` and
    ``SessionTurn`` rows, the **latest** run on the primary thread is used —
    a resumed session must land on the active run, not the original. Rows
    are filtered by ``thread_id == primary.id`` so subagent/branch threads
    keep their own run pointer.

    Launcher-owned runs are not touched and no connection is fabricated for
    them — those came in through Phase 2 dual-write paths and any missing
    connection there is a launcher bug, not a backfill concern.

    Idempotent: skips threads that already have any run row. Re-running over
    a converged DB is a no-op.
    """

    runs_created = 0
    connections_created = 0
    runtime_state_run_ids = 0
    turn_run_ids = 0

    # Cheap early-out for converged DBs: no primary threads missing a run
    # and no runtime/turn rows with run_id=NULL.
    threads_missing_run_subq = (
        db.query(SessionThread.id)
        .outerjoin(SessionRun, SessionRun.thread_id == SessionThread.id)
        .filter(SessionThread.is_primary == 1, SessionRun.id.is_(None))
        .limit(1)
        .first()
    )
    runtime_null = db.query(SessionRuntimeState.runtime_key).filter(SessionRuntimeState.run_id.is_(None)).limit(1).first()
    turn_null = db.query(SessionTurn.id).filter(SessionTurn.run_id.is_(None)).limit(1).first()
    if threads_missing_run_subq is None and runtime_null is None and turn_null is None:
        return {
            "runs_created": 0,
            "connections_created": 0,
            "runtime_state_run_ids": 0,
            "turn_run_ids": 0,
        }

    threads = db.query(SessionThread).filter(SessionThread.is_primary == 1).all()
    now = datetime.now(timezone.utc)

    for thread in threads:
        existing_run = (
            db.query(SessionRun)
            .filter(SessionRun.thread_id == thread.id)
            .order_by(SessionRun.started_at.desc(), SessionRun.id.desc())
            .first()
        )
        if existing_run is None:
            session = db.query(AgentSession).filter(AgentSession.id == thread.session_id).first()
            if session is None:
                continue
            run = SessionRun(
                thread_id=thread.id,
                provider=thread.provider or session.provider,
                host_id=getattr(session, "device_id", None),
                cwd=getattr(session, "cwd", None),
                launch_origin="external_adopted",
                started_at=getattr(session, "started_at", None) or now,
                ended_at=getattr(session, "ended_at", None),
            )
            db.add(run)
            db.flush()
            runs_created += 1

            # Synthesize a log_tail connection only when we synthesized the
            # run. A launcher-owned run is responsible for its own connection.
            db.add(
                SessionConnection(
                    run_id=run.id,
                    control_plane="log_tail",
                    acquisition_kind="observe_only",
                    state="ended" if run.ended_at is not None else "attached",
                    can_send_input=0,
                    can_interrupt=0,
                    can_terminate=0,
                    can_tail_output=1,
                    can_resume=0,
                )
            )
            connections_created += 1
        else:
            run = existing_run

        # Stamp run_id on runtime state / turns where NULL — but only on rows
        # already keyed to *this* primary thread. Rows pointing at a child or
        # branch thread keep their own (eventually-stamped) run pointer.
        result = db.execute(
            sql_update(SessionRuntimeState)
            .where(
                SessionRuntimeState.thread_id == thread.id,
                SessionRuntimeState.run_id.is_(None),
            )
            .values(run_id=run.id)
        )
        runtime_state_run_ids += int(result.rowcount or 0)

        result = db.execute(
            sql_update(SessionTurn)
            .where(
                SessionTurn.thread_id == thread.id,
                SessionTurn.run_id.is_(None),
            )
            .values(run_id=run.id)
        )
        turn_run_ids += int(result.rowcount or 0)

    db.flush()
    return {
        "runs_created": runs_created,
        "connections_created": connections_created,
        "runtime_state_run_ids": runtime_state_run_ids,
        "turn_run_ids": turn_run_ids,
    }


def backfill_session_identity_kernel(db: Session) -> dict[str, dict[str, int]]:
    """Run the three-stage backfill in dependency order.

    1. ``backfill_root_threads`` — primary thread + provider_session_id alias.
    2. ``backfill_child_thread_ids`` — stamp thread_id on every legacy child row.
    3. ``cleanup_workflow_journal_sessions`` — delete empty junk sessions that
       leaked from dynamic-workflow ``journal.jsonl`` ledgers.
    4. ``backfill_subagent_child_threads`` — move leaked provider subagent
       sessions under their parent session as child threads.
    5. ``backfill_runs_and_connections`` — synthesize one observe-only run +
       connection per session for sessions without launcher-owned runs.

    Idempotent end-to-end. Safe to run on every startup or as a one-shot CLI.
    """

    return {
        "threads": backfill_root_threads(db),
        "children": backfill_child_thread_ids(db),
        "workflow_journals": cleanup_workflow_journal_sessions(db),
        "subagents": backfill_subagent_child_threads(db),
        "runs": backfill_runs_and_connections(db),
    }
