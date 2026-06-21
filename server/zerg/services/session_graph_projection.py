from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from zerg.models.agents import SessionEdge
from zerg.models.agents import SessionThread
from zerg.models.agents import SessionThreadAlias


def _labels_by_thread(db: Session, thread_ids: set[UUID]) -> dict[UUID, dict[str, str]]:
    if not thread_ids:
        return {}
    labels: dict[UUID, dict[str, str]] = {}
    for row in db.query(SessionThreadAlias).filter(SessionThreadAlias.thread_id.in_(thread_ids)).all():
        labels.setdefault(row.thread_id, {})[row.alias_kind] = row.alias_value
    return labels


def _thread_rows_by_id(db: Session, thread_ids: set[UUID]) -> dict[UUID, SessionThread]:
    if not thread_ids:
        return {}
    return {row.id: row for row in db.query(SessionThread).filter(SessionThread.id.in_(thread_ids)).all()}


def _edge_payload(edge: SessionEdge, labels_by_thread: dict[UUID, dict[str, str]]) -> dict:
    labels = labels_by_thread.get(edge.target_thread_id, {}) if edge.target_thread_id else {}
    metadata = dict(edge.metadata_json or {})
    return {
        "edge_id": str(edge.id),
        "provider": edge.provider,
        "edge_kind": edge.edge_kind,
        "visibility": edge.visibility,
        "evidence_kind": edge.evidence_kind,
        "source_session_id": str(edge.source_session_id) if edge.source_session_id else None,
        "source_thread_id": str(edge.source_thread_id) if edge.source_thread_id else None,
        "target_session_id": str(edge.target_session_id) if edge.target_session_id else None,
        "target_thread_id": str(edge.target_thread_id) if edge.target_thread_id else None,
        "provider_edge_id": edge.provider_edge_id,
        "metadata": metadata,
        "labels": labels,
        "agent_id": labels.get("subagent_id") or labels.get("claude_agent_id") or metadata.get("subagent_id"),
        "workflow_run_id": labels.get("workflow_run_id") or metadata.get("workflow_run_id"),
        "attribution_agent": labels.get("workflow_attribution_agent") or metadata.get("attribution_agent"),
        "attribution_skill": labels.get("workflow_attribution_skill") or metadata.get("attribution_skill"),
    }


def build_session_graph_projection(db: Session, session_id: UUID) -> dict:
    """Project child/fork/link graph context for one session."""

    edges = (
        db.query(SessionEdge)
        .filter((SessionEdge.source_session_id == session_id) | (SessionEdge.target_session_id == session_id))
        .order_by(SessionEdge.created_at.asc(), SessionEdge.id.asc())
        .all()
    )
    thread_ids = {edge.source_thread_id for edge in edges if edge.source_thread_id}
    thread_ids.update(edge.target_thread_id for edge in edges if edge.target_thread_id)
    thread_rows = _thread_rows_by_id(db, thread_ids)
    labels_by_thread = _labels_by_thread(db, thread_ids)
    edge_payloads = [_edge_payload(edge, labels_by_thread) for edge in edges]

    child_edges = [item for item in edge_payloads if item["edge_kind"] == "task_child" and item["source_session_id"] == str(session_id)]
    fork_edges = [
        item
        for item in edge_payloads
        if item["edge_kind"] == "fork" and (item["source_session_id"] == str(session_id) or item["target_session_id"] == str(session_id))
    ]
    linked_edges = [
        item
        for item in edge_payloads
        if item["edge_kind"] == "unknown" and (item["source_session_id"] == str(session_id) or item["target_session_id"] == str(session_id))
    ]
    return {
        "session_id": str(session_id),
        "edges": edge_payloads,
        "children": child_edges,
        "forks": fork_edges,
        "linked": linked_edges,
        "thread_count": len(thread_rows),
    }


def archive_owner_session_ids(db: Session, session_id: UUID) -> set[str]:
    """Return session ids whose archive chunks belong to this projected graph."""

    session_key = str(session_id)
    owners = {session_key}
    for edge in build_session_graph_projection(db, session_id)["children"]:
        labels = edge.get("labels") or {}
        metadata = edge.get("metadata") or {}
        for candidate in (
            labels.get("longhouse_session_id"),
            metadata.get("child_longhouse_session_id"),
            edge.get("target_session_id") if edge.get("target_session_id") != session_key else None,
        ):
            normalized = str(candidate or "").strip()
            if normalized:
                owners.add(normalized)
    return owners


def workflow_run_projection(db: Session, workflow_run_id: str, *, provider: str | None = None) -> dict | None:
    """Return a provider-neutral projection for one workflow run id."""

    run_id = str(workflow_run_id or "").strip()
    if not run_id:
        return None
    query = (
        db.query(SessionThread)
        .join(SessionThreadAlias, SessionThreadAlias.thread_id == SessionThread.id)
        .filter(SessionThreadAlias.alias_kind == "workflow_run_id")
        .filter(SessionThreadAlias.alias_value == run_id)
    )
    if provider:
        query = query.filter(SessionThreadAlias.provider == provider)
    threads = query.order_by(SessionThread.created_at.asc(), SessionThread.id.asc()).all()
    if not threads:
        return None

    unique_threads: list[SessionThread] = []
    seen: set[UUID] = set()
    for thread in threads:
        if thread.id not in seen:
            seen.add(thread.id)
            unique_threads.append(thread)
    labels_by_thread = _labels_by_thread(db, {thread.id for thread in unique_threads})

    agents: list[dict] = []
    parent_session_ids: set[str] = set()
    skill = None
    for thread in unique_threads:
        labels = labels_by_thread.get(thread.id, {})
        parent_session_ids.add(str(thread.session_id))
        skill = skill or labels.get("workflow_attribution_skill")
        agents.append(
            {
                "thread_id": str(thread.id),
                "session_id": str(thread.session_id),
                "is_primary": bool(thread.is_primary),
                "branch_kind": thread.branch_kind,
                "agent_id": labels.get("subagent_id") or labels.get("claude_agent_id"),
                "attribution_agent": labels.get("workflow_attribution_agent"),
                "attribution_skill": labels.get("workflow_attribution_skill"),
                "source_path": labels.get("source_path"),
            }
        )
    return {
        "workflow_run_id": run_id,
        "skill": skill,
        "parent_session_id": next(iter(parent_session_ids)) if len(parent_session_ids) == 1 else None,
        "agent_count": len(agents),
        "agents": agents,
    }


def workflow_runs_for_session(db: Session, session_id: UUID) -> list[dict]:
    """List workflow run summaries for child threads under one parent session."""

    rows = (
        db.query(SessionThreadAlias.alias_value, SessionThread.id)
        .join(SessionThread, SessionThreadAlias.thread_id == SessionThread.id)
        .filter(SessionThread.session_id == session_id)
        .filter(SessionThreadAlias.alias_kind == "workflow_run_id")
        .all()
    )
    run_to_threads: dict[str, set[UUID]] = {}
    for run_id, thread_id in rows:
        if run_id:
            run_to_threads.setdefault(run_id, set()).add(thread_id)
    if not run_to_threads:
        return []

    labels_by_thread = _labels_by_thread(db, {tid for tids in run_to_threads.values() for tid in tids})
    runs: list[dict] = []
    for run_id, thread_ids in sorted(run_to_threads.items()):
        skill = next(
            (
                labels_by_thread.get(tid, {}).get("workflow_attribution_skill")
                for tid in sorted(thread_ids, key=str)
                if labels_by_thread.get(tid, {}).get("workflow_attribution_skill")
            ),
            None,
        )
        runs.append({"workflow_run_id": run_id, "agent_count": len(thread_ids), "skill": skill})
    return runs
