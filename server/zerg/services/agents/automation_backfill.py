"""Conservative repair helpers for Hatch automation session origin."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.orm import Session

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionThread
from zerg.models.agents import TimelineCard
from zerg.services.internal_sessions import is_hatch_execution_contract
from zerg.services.session_visibility_policy import evaluate_origin_visibility
from zerg.services.session_visibility_policy import facts_from_row

HATCH_AUTOMATION_ORIGIN_KIND = "hatch_automation"
TEST_OR_CANARY_ORIGIN_KIND = "test_or_canary"
REVIEWABLE_HIDDEN_ORIGIN_KINDS = frozenset({HATCH_AUTOMATION_ORIGIN_KIND, TEST_OR_CANARY_ORIGIN_KIND})
_HATCH_BACKED_PROVIDERS = {"opencode", "claude", "codex", "cursor"}
_HATCH_PROMPT_HINTS = (
    "code review",
    "review this branch",
    "review the current branch",
    "final review",
    "quick phase review",
    "phase review",
    "drill down",
)


@dataclass(frozen=True)
class AutomationBackfillResult:
    """Result payload for the report-only Hatch automation repair."""

    applied_session_ids: list[str]
    missing_session_ids: list[str]
    already_marked_session_ids: list[str]
    heuristic_candidates: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied_session_ids": self.applied_session_ids,
            "missing_session_ids": self.missing_session_ids,
            "already_marked_session_ids": self.already_marked_session_ids,
            "heuristic_candidate_count": len(self.heuristic_candidates),
            "heuristic_candidates": self.heuristic_candidates,
        }


@dataclass(frozen=True)
class VisibilityReconcileResult:
    evaluated: int
    actionable_session_ids: list[str]
    proven_hidden_session_ids: list[str]
    derived_visibility_rows: list[dict[str, Any]]
    unresolved_hidden_session_ids: list[str]
    reason_counts: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluated": self.evaluated,
            "actionable_count": len(self.actionable_session_ids),
            "actionable_session_ids": self.actionable_session_ids,
            "proven_hidden_count": len(self.proven_hidden_session_ids),
            "proven_hidden_session_ids": self.proven_hidden_session_ids,
            "derived_visibility_count": len(self.derived_visibility_rows),
            "unresolved_hidden_count": len(self.unresolved_hidden_session_ids),
            "unresolved_hidden_session_ids": self.unresolved_hidden_session_ids,
            "reason_counts": self.reason_counts,
        }


def reconcile_legacy_session_visibility(db: Session, *, apply: bool) -> VisibilityReconcileResult:
    """Evaluate every legacy session and replace the derived projection.

    ``hidden_from_default_timeline`` is an output of the policy, never an
    input.  Reconciliation therefore repairs stale false positives as well as
    stale false negatives while leaving raw provenance and user preferences
    untouched.
    """

    sessions = db.query(AgentSession).order_by(AgentSession.started_at.asc(), AgentSession.id.asc()).all()
    primary_threads = {thread.session_id: thread for thread in db.query(SessionThread).filter(SessionThread.is_primary == 1).all()}
    actionable: list[str] = []
    proven_hidden: list[str] = []
    derived_visibility_rows: list[dict[str, Any]] = []
    unresolved: list[str] = []
    reason_counts: dict[str, int] = {}
    for session in sessions:
        if not session.first_user_message_preview:
            session.first_user_message_preview = _event_preview(db, session.id) or None
        primary = primary_threads.get(session.id)
        decision = evaluate_origin_visibility(
            facts_from_row(
                session,
                primary_thread_is_worker_only=bool(primary and primary.branch_kind == "subagent"),
            )
        )
        for reason in decision.reason_keys:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        current_hidden = bool(session.hidden_from_default_timeline)
        if decision.system_hidden:
            proven_hidden.append(str(session.id))
        if decision.system_hidden != current_hidden:
            actionable.append(str(session.id))
            if apply:
                target = int(decision.system_hidden)
                session.hidden_from_default_timeline = target
                for thread in db.query(SessionThread).filter(SessionThread.session_id == session.id).all():
                    thread.hidden_from_default_timeline = target
                card = db.get(TimelineCard, session.id)
                if card is not None:
                    card.hidden_from_default_timeline = target
        # Changed visible rows must be mirrored too; otherwise searchd retains
        # the stale hidden bit even though the source store was repaired.
        if (
            str(session.id) in actionable
            or decision.system_hidden
            or bool(session.user_hidden_from_timeline)
            or session.user_state not in {"active", "parked"}
        ):
            derived_visibility_rows.append(
                {
                    "session_id": str(session.id),
                    "system_hidden": decision.system_hidden,
                    "user_hidden_from_timeline": bool(session.user_hidden_from_timeline),
                    "user_state": str(session.user_state or "active"),
                }
            )
    if apply:
        db.commit()
    else:
        db.rollback()
    return VisibilityReconcileResult(
        evaluated=len(sessions),
        actionable_session_ids=actionable,
        proven_hidden_session_ids=proven_hidden,
        derived_visibility_rows=derived_visibility_rows,
        unresolved_hidden_session_ids=unresolved,
        reason_counts=reason_counts,
    )


def _normalize_session_id(value: str | UUID) -> UUID:
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _normalize_reviewed_origin_kind(value: str) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized not in REVIEWABLE_HIDDEN_ORIGIN_KINDS:
        raise ValueError(f"unsupported hidden origin kind: {value}")
    return normalized


def _event_preview(db: Session, session_id: UUID) -> str:
    row = (
        db.query(AgentEvent.content_text)
        .filter(AgentEvent.session_id == session_id)
        .filter(AgentEvent.role == "user")
        .order_by(AgentEvent.timestamp.asc(), AgentEvent.id.asc())
        .first()
    )
    return str(row[0] or "").strip() if row else ""


def _source_path_preview(db: Session, session_id: UUID) -> str:
    row = (
        db.query(AgentEvent.source_path)
        .filter(AgentEvent.session_id == session_id)
        .filter(AgentEvent.source_path.is_not(None))
        .order_by(AgentEvent.timestamp.asc(), AgentEvent.id.asc())
        .first()
    )
    return str(row[0] or "").strip() if row else ""


def _candidate_dict(db: Session, session: AgentSession, thread: SessionThread) -> dict[str, Any]:
    prompt = (session.first_user_message_preview or _event_preview(db, session.id))[:500]
    source_path = _source_path_preview(db, session.id)
    return {
        "session_id": str(session.id),
        "thread_id": str(thread.id),
        "provider": session.provider,
        "project": session.project,
        "started_at": session.started_at.isoformat() if session.started_at else None,
        "user_messages": session.user_messages,
        "branch_kind": thread.branch_kind,
        "confidence": "medium",
        "reason": "hatch-shaped prompt/provider/root-thread; report-only until reviewed",
        "prompt_preview": prompt,
        "source_path_preview": source_path,
    }


def find_hatch_automation_candidates(db: Session, *, limit: int = 100) -> list[dict[str, Any]]:
    """Report medium-confidence Hatch-shaped rows without mutating them."""

    rows = (
        db.query(AgentSession, SessionThread)
        .join(SessionThread, SessionThread.session_id == AgentSession.id)
        .filter(SessionThread.is_primary == 1)
        .filter(SessionThread.branch_kind == "root")
        .filter(AgentSession.provider.in_(_HATCH_BACKED_PROVIDERS))
        .filter(or_(AgentSession.origin_kind.is_(None), AgentSession.origin_kind == ""))
        .filter(AgentSession.hidden_from_default_timeline == 0)
        .filter(AgentSession.user_messages <= 2)
        .order_by(AgentSession.started_at.desc())
        .limit(max(1, min(limit, 500)))
        .all()
    )
    candidates: list[dict[str, Any]] = []
    for session, thread in rows:
        prompt_text = session.first_user_message_preview or _event_preview(db, session.id)
        if is_hatch_execution_contract(prompt_text):
            candidate = _candidate_dict(db, session, thread)
            candidate["confidence"] = "high"
            candidate["reason"] = "exact Hatch execution contract; safe to classify"
            candidates.append(candidate)
            continue
        prompt = prompt_text.lower()
        if not any(hint in prompt for hint in _HATCH_PROMPT_HINTS):
            continue
        candidates.append(_candidate_dict(db, session, thread))
    return candidates


def find_test_or_canary_candidates(db: Session, *, limit: int = 100) -> list[dict[str, Any]]:
    """Report QA/probe-shaped rows (coordination probes) without mutating them."""

    rows = (
        db.query(AgentSession, SessionThread)
        .join(SessionThread, SessionThread.session_id == AgentSession.id)
        .filter(SessionThread.is_primary == 1)
        .filter(AgentSession.hidden_from_default_timeline == 0)
        .filter(
            or_(
                AgentSession.project.ilike("%longhouse-coordination-awareness-probe%"),
                AgentSession.cwd.ilike("%longhouse-coordination-awareness-probe%"),
            )
        )
        .order_by(AgentSession.started_at.desc())
        .limit(max(1, min(limit, 500)))
        .all()
    )
    candidates: list[dict[str, Any]] = []
    for session, thread in rows:
        candidate = _candidate_dict(db, session, thread)
        candidate["reason"] = "qa-probe project/source; report-only until reviewed"
        candidate["confidence"] = "high"
        candidates.append(candidate)
    return candidates


def find_benchmark_candidates(db: Session, *, limit: int = 100) -> list[dict[str, Any]]:
    """Report benchmark-shaped rows (legacy ``ws_*`` workspaces) without mutating them."""

    rows = (
        db.query(AgentSession, SessionThread)
        .join(SessionThread, SessionThread.session_id == AgentSession.id)
        .filter(SessionThread.is_primary == 1)
        .filter(AgentSession.hidden_from_default_timeline == 0)
        .filter(
            or_(
                AgentSession.project.ilike("ws_%"),
                AgentSession.cwd.ilike("%/ws_%"),
            )
        )
        .order_by(AgentSession.started_at.desc())
        .limit(max(1, min(limit, 500)))
        .all()
    )
    candidates: list[dict[str, Any]] = []
    for session, thread in rows:
        candidate = _candidate_dict(db, session, thread)
        candidate["reason"] = "benchmark ws_* workspace; report-only until reviewed"
        candidate["confidence"] = "low"
        candidates.append(candidate)
    return candidates


def classify_reviewed_hatch_automation_sessions(
    db: Session,
    *,
    session_ids: list[str | UUID],
    apply: bool,
    origin_kind: str = HATCH_AUTOMATION_ORIGIN_KIND,
    candidate_limit: int = 100,
) -> AutomationBackfillResult:
    """Mark explicit reviewed rows with a hidden origin; heuristics stay report-only."""

    reviewed_origin_kind = _normalize_reviewed_origin_kind(origin_kind)
    normalized_ids = [_normalize_session_id(value) for value in session_ids]
    sessions_by_id: dict[UUID, AgentSession] = {}
    if normalized_ids:
        sessions = db.query(AgentSession).filter(AgentSession.id.in_(normalized_ids)).all()
        sessions_by_id = {session.id: session for session in sessions}

    missing = [str(session_id) for session_id in normalized_ids if session_id not in sessions_by_id]
    already_marked: list[str] = []
    applied: list[str] = []

    if apply:
        for session_id in normalized_ids:
            session = sessions_by_id.get(session_id)
            if session is None:
                continue
            expected_launch_surface = "hatch" if reviewed_origin_kind == HATCH_AUTOMATION_ORIGIN_KIND else "test"
            if (
                session.origin_kind == reviewed_origin_kind
                and session.hidden_from_default_timeline == 1
                and session.launch_actor == "automation"
                and session.launch_surface == expected_launch_surface
            ):
                already_marked.append(str(session_id))
                continue

            session.origin_kind = reviewed_origin_kind
            session.hidden_from_default_timeline = 1
            session.launch_actor = "automation"
            session.launch_surface = expected_launch_surface
            for thread in db.query(SessionThread).filter(SessionThread.session_id == session_id).all():
                thread.origin_kind = reviewed_origin_kind
                thread.hidden_from_default_timeline = 1
            card = db.get(TimelineCard, session_id)
            if card is not None:
                card.origin_kind = reviewed_origin_kind
                card.hidden_from_default_timeline = 1
                card.launch_actor = session.launch_actor
                card.launch_surface = session.launch_surface
            applied.append(str(session_id))
        db.commit()

    return AutomationBackfillResult(
        applied_session_ids=applied,
        missing_session_ids=missing,
        already_marked_session_ids=already_marked,
        heuristic_candidates=find_hatch_automation_candidates(db, limit=candidate_limit),
    )


def reclassify_catalogd_origins(
    session_ids: list[str],
    origin_kind: str,
    *,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Reclassify reviewed sessions on the catalogd/storage and searchd sides.

    The agents-ORM write fixes the legacy timeline; the worklog projection reads
    the durable StorageSession via searchd, so the same reviewed IDs must also be
    reclassified through catalogd's single-writer transaction and through
    searchd's ``session_index`` so the digest line flips without a re-publish.
    Fail-loud per session: an error is reported, never swallowed.
    """

    from datetime import UTC
    from datetime import datetime

    from zerg.catalogd.client import CatalogRemoteError
    from zerg.catalogd.client import CatalogUnavailable
    from zerg.catalogd.client import call_catalogd_sync
    from zerg.services.catalogd_supervisor import catalogd_paths
    from zerg.services.searchd_supervisor import searchd_paths

    try:
        _, catalogd_socket = catalogd_paths()
    except (RuntimeError, OSError) as exc:
        return {
            "applied_catalogd_session_ids": [],
            "failed_catalogd_session_ids": [{"session_id": "", "error": f"catalogd unavailable: {exc}"}],
            "catalogd_unavailable": True,
        }
    searchd_socket: Path | None = None
    try:
        if len(session_ids) > 0:
            _, searchd_socket = searchd_paths()
    except (RuntimeError, OSError):
        pass
    applied: list[str] = []
    failed: list[dict[str, str]] = []
    searchd_failed: list[dict[str, str]] = []
    for session_id in session_ids:
        try:
            result = call_catalogd_sync(
                catalogd_socket,
                "catalogd.session.reclassify_origin.v2",
                params={
                    "session_id": session_id,
                    "origin_kind": origin_kind,
                    "observed_at": datetime.now(UTC).isoformat(),
                },
                timeout_seconds=timeout_seconds,
            )
        except (CatalogUnavailable, CatalogRemoteError) as exc:
            failed.append({"session_id": session_id, "error": str(exc)})
            continue
        if result.get("reclassified") is True:
            applied.append(session_id)
        else:
            failed.append({"session_id": session_id, "error": "not found or not reclassified"})
        if searchd_socket is not None:
            try:
                searchd_result = call_catalogd_sync(
                    searchd_socket,
                    "search.session.reclassify_origin.v2",
                    params={
                        "session_id": session_id,
                        "origin_kind": origin_kind,
                        "observed_at": datetime.now(UTC).isoformat(),
                    },
                    timeout_seconds=timeout_seconds,
                )
                if not searchd_result.get("reclassified"):
                    searchd_failed.append({"session_id": session_id, "error": "searchd not reclassified"})
            except (CatalogUnavailable, CatalogRemoteError) as exc:
                searchd_failed.append({"session_id": session_id, "error": str(exc)})
    return {
        "applied_catalogd_session_ids": applied,
        "failed_catalogd_session_ids": failed,
        "searchd_failed_session_ids": searchd_failed,
    }


def reconcile_derived_visibility(
    rows: list[dict[str, Any]],
    *,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Mirror a proven policy decision through catalogd and searchd."""

    from datetime import UTC
    from datetime import datetime

    from zerg.catalogd.client import CatalogRemoteError
    from zerg.catalogd.client import CatalogUnavailable
    from zerg.catalogd.client import call_catalogd_sync
    from zerg.services.catalogd_supervisor import catalogd_paths
    from zerg.services.searchd_supervisor import searchd_paths

    try:
        _, catalogd_socket = catalogd_paths()
    except (RuntimeError, OSError) as exc:
        return {"applied": [], "failed": [{"session_id": "", "error": f"catalogd unavailable: {exc}"}]}
    try:
        _, searchd_socket = searchd_paths()
    except (RuntimeError, OSError):
        searchd_socket = None
    applied: list[str] = []
    failed: list[dict[str, str]] = []
    for row in rows:
        session_id = str(row["session_id"])
        system_hidden = bool(row["system_hidden"])
        observed_at = datetime.now(UTC).isoformat()
        try:
            catalog_result = call_catalogd_sync(
                catalogd_socket,
                "catalogd.session.reconcile_visibility.v2",
                params={"session_id": session_id, "system_hidden": system_hidden, "observed_at": observed_at},
                timeout_seconds=timeout_seconds,
            )
            if catalog_result.get("reconciled") is not True:
                failed.append({"session_id": session_id, "error": "catalogd session not found"})
                continue
            if searchd_socket is not None:
                call_catalogd_sync(
                    searchd_socket,
                    "search.session.reconcile_visibility.v2",
                    params={
                        "session_id": session_id,
                        "system_hidden": system_hidden,
                        "user_hidden_from_timeline": bool(row["user_hidden_from_timeline"]),
                        "user_state": str(row["user_state"]),
                        "source_commit_seq": int(catalog_result.get("commit_seq") or 0),
                    },
                    timeout_seconds=timeout_seconds,
                )
            applied.append(session_id)
        except (CatalogUnavailable, CatalogRemoteError, ValueError) as exc:
            failed.append({"session_id": session_id, "error": str(exc)})
    return {"applied": applied, "failed": failed}


def reconcile_catalogd_all_visibility(
    *,
    apply: bool,
    timeout_seconds: float = 120.0,
) -> dict[str, Any]:
    """Run the authoritative all-session catalog reconciliation and mirror searchd."""

    from datetime import UTC
    from datetime import datetime

    from zerg.catalogd.client import call_catalogd_sync
    from zerg.services.catalogd_supervisor import catalogd_paths
    from zerg.services.searchd_supervisor import searchd_paths

    _, catalogd_socket = catalogd_paths()
    result = call_catalogd_sync(
        catalogd_socket,
        "catalogd.session.reconcile_visibility_all.v2",
        params={"apply": apply, "observed_at": datetime.now(UTC).isoformat()},
        timeout_seconds=timeout_seconds,
    )
    failures: list[dict[str, str]] = []
    applied: list[str] = []
    mirror_rows = result.pop("mirror_rows", [])
    if apply and mirror_rows:
        _, searchd_socket = searchd_paths()
        source_commit_seq = int(result.get("commit_seq") or 0)
        for row in mirror_rows:
            session_id = str(row["session_id"])
            try:
                call_catalogd_sync(
                    searchd_socket,
                    "search.session.reconcile_visibility.v2",
                    params={
                        "session_id": session_id,
                        "system_hidden": bool(row["system_hidden"]),
                        "user_hidden_from_timeline": bool(row["user_hidden_from_timeline"]),
                        "user_state": str(row["user_state"]),
                        "source_commit_seq": source_commit_seq,
                    },
                    timeout_seconds=30.0,
                )
                applied.append(session_id)
            except Exception as exc:  # per-row maintenance report; do not hide partial convergence
                failures.append({"session_id": session_id, "error": str(exc)})
    return {
        **result,
        "derived_applied_count": len(applied),
        "derived_failures": failures,
    }
