"""Read-only detection of duplicate sessions that share one provider-native id.

This is the *detector* half of the provider-session-binding cleanup follow-up
(see ``docs/specs/provider-session-binding.md``). It is pure SELECT: it never
writes (no write lock), only takes ordinary SQLite read locks, and is safe to
run against the live hosted corpus.

The destructive merge is deliberately NOT implemented here. The spec records
four unresolved hazards (discovery after alias dedup, exact canonical-winner
ordering parity with the startup migration, interprocess write-lock contention,
and full session-scoped table coverage) that must be designed before any merge
touches production rows.

Discovery note. The startup migration already deletes the duplicate
``provider_session_id`` *aliases* that would block the routing index, so
duplicates can no longer be found by grouping aliases. We instead use the
surviving evidence: recorded ``provider_binding_conflict`` observations carry
``existing_thread_id`` / ``requested_thread_id`` / ``provider_session_id`` in
their payloads, which name the threads that competed for one native id. We map
those threads back to their sessions and report any native id that still
resolves to more than one distinct session.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from sqlalchemy.orm import Session

from zerg.models.agents import SessionObservation
from zerg.models.agents import SessionThread
from zerg.services.session_observations import OBS_KIND_PROVIDER_BINDING_CONFLICT
from zerg.services.session_observations import decode_observation_payload_json


@dataclass(frozen=True)
class DuplicateBindingGroup:
    provider: str
    provider_session_id: str
    session_ids: list[str]
    thread_ids: list[str]
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "provider_session_id": self.provider_session_id,
            "session_ids": self.session_ids,
            "thread_ids": self.thread_ids,
            "evidence": self.evidence,
        }


@dataclass
class _Accumulator:
    provider: str
    provider_session_id: str
    thread_ids: set[str] = field(default_factory=set)


def _payload_dict(observation: SessionObservation) -> dict[str, Any]:
    raw = decode_observation_payload_json(observation)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _sessions_for_threads(db: Session, thread_ids: set[str]) -> dict[str, str]:
    """Map thread_id -> session_id for the given threads (best effort)."""
    if not thread_ids:
        return {}
    rows = db.query(SessionThread.id, SessionThread.session_id).filter(SessionThread.id.in_(list(thread_ids))).all()
    mapping: dict[str, str] = {}
    for thread_id, session_id in rows:
        if thread_id is None or session_id is None:
            continue
        mapping[str(thread_id)] = str(session_id)
    return mapping


def detect_duplicate_sessions_by_provider_binding(db: Session) -> list[DuplicateBindingGroup]:
    """Return groups where one provider-native id maps to multiple sessions.

    Pure read. Uses recorded conflict observations as the discovery surface
    because the routing-index migration already removed the duplicate aliases.
    A group is only reported when the competing threads resolve to two or more
    *distinct* sessions that still exist — i.e. an unresolved split row, not a
    conflict that was already converged onto one session.
    """

    conflicts = (
        db.query(SessionObservation)
        .filter(SessionObservation.kind == OBS_KIND_PROVIDER_BINDING_CONFLICT)
        .order_by(SessionObservation.observed_at.asc(), SessionObservation.id.asc())
        .all()
    )

    accumulators: dict[tuple[str, str], _Accumulator] = {}
    for row in conflicts:
        payload = _payload_dict(row)
        provider_session_id = str(payload.get("provider_session_id") or "").strip()
        if not provider_session_id:
            continue
        provider = str(payload.get("provider") or row.provider or "").strip() or "unknown"
        key = (provider, provider_session_id)
        acc = accumulators.get(key)
        if acc is None:
            acc = _Accumulator(provider=provider, provider_session_id=provider_session_id)
            accumulators[key] = acc
        for field_name in ("existing_thread_id", "requested_thread_id"):
            value = payload.get(field_name)
            if value:
                acc.thread_ids.add(str(value).strip())

    groups: list[DuplicateBindingGroup] = []
    for (provider, provider_session_id), acc in accumulators.items():
        thread_to_session = _sessions_for_threads(db, acc.thread_ids)
        # Only surviving threads count; a thread merged/deleted by the migration
        # drops out here, which is what keeps already-converged conflicts quiet.
        surviving_threads = sorted(thread_to_session.keys())
        distinct_sessions = sorted(set(thread_to_session.values()))
        if len(distinct_sessions) < 2:
            continue
        groups.append(
            DuplicateBindingGroup(
                provider=provider,
                provider_session_id=provider_session_id,
                session_ids=distinct_sessions,
                thread_ids=surviving_threads,
                evidence="provider_binding_conflict",
            )
        )

    groups.sort(key=lambda g: (g.provider, g.provider_session_id))
    return groups
