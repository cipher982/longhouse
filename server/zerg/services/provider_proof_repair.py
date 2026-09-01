"""Repair historical provider-proof sessions into the test environment."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.models.agents import TimelineCard
from zerg.services.internal_sessions import classify_provider_proof_environment
from zerg.services.internal_sessions import provider_proof_session_clause


@dataclass(frozen=True)
class ProviderProofRepairResult:
    scanned_sessions: int
    repairable_sessions: int
    updated_sessions: int
    updated_timeline_cards: int
    session_ids: list[str]


def repair_provider_proof_session_environments(
    db: Session,
    *,
    limit: int = 500,
    apply: bool = False,
) -> ProviderProofRepairResult:
    """Mark historical provider-proof rows as environment=test.

    Both the SQL candidate clause and the Python classifier read only cwd and
    machine namespace, but they are not equals: the SQL set is a strict subset.
    Python additionally normalizes backslashes, strips the machine id, and
    accepts ``/tmp/evidence/raw/`` without an intermediate segment. So the
    classifier below can only ever confirm a candidate, never reject one, and
    the divergence costs recall (rows this tool will not offer to repair)
    rather than precision. It stays as the authority anyway: SQL narrows,
    Python decides.
    """
    if limit <= 0:
        return ProviderProofRepairResult(
            scanned_sessions=0,
            repairable_sessions=0,
            updated_sessions=0,
            updated_timeline_cards=0,
            session_ids=[],
        )

    rows = (
        db.query(AgentSession)
        .filter(AgentSession.environment.notin_(["test", "e2e"]))
        .filter(provider_proof_session_clause(AgentSession))
        .order_by(AgentSession.last_activity_at.desc().nullslast(), AgentSession.started_at.desc())
        .limit(limit)
        .all()
    )
    repairable: list[AgentSession] = []
    for session in rows:
        if (
            classify_provider_proof_environment(
                cwd=session.cwd,
                machine_id=session.device_id,
            )
            != "test"
        ):
            continue
        repairable.append(session)

    updated_sessions = 0
    updated_timeline_cards = 0
    if apply:
        for session in repairable:
            session.environment = "test"
            updated_sessions += 1
            updated_timeline_cards += (
                db.query(TimelineCard)
                .filter(TimelineCard.session_id == session.id)
                .update({"environment": "test"}, synchronize_session=False)
            )

    return ProviderProofRepairResult(
        scanned_sessions=len(rows),
        repairable_sessions=len(repairable),
        updated_sessions=updated_sessions,
        updated_timeline_cards=updated_timeline_cards,
        session_ids=[str(session.id) for session in repairable],
    )
