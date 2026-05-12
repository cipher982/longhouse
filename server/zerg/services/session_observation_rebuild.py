"""Session-scoped projection rebuilds from raw observations."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.orm import Session

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSourceLine
from zerg.models.agents import SessionObservation
from zerg.models.agents import SessionRuntimeState
from zerg.services.provisional_events import reconcile_provisional_transcript_events
from zerg.services.session_observation_reducers import reduce_bridge_transcript_observation
from zerg.services.session_observation_reducers import reduce_provider_event_observation
from zerg.services.session_observation_reducers import reduce_source_line_observation
from zerg.services.session_observations import OBS_KIND_BRIDGE_TRANSCRIPT_DELTA
from zerg.services.session_observations import OBS_KIND_PROVIDER_EVENT
from zerg.services.session_observations import OBS_KIND_PROVIDER_SOURCE_LINE
from zerg.services.session_observations import OBS_KIND_RUNTIME_SIGNAL
from zerg.services.session_runtime import reduce_runtime_signal_observation


@dataclass(frozen=True)
class SessionObservationReducerError:
    observation_db_id: int
    observation_id: str
    kind: str
    error: str


@dataclass(frozen=True)
class SessionObservationRebuildResult:
    session_id: UUID | None
    runtime_key: str | None
    observations_seen: int
    newest_observation_db_id: int | None
    provider_events_reduced: int
    bridge_events_reduced: int
    source_lines_reduced: int
    runtime_signals_reduced: int
    skipped_observations: int
    reducer_errors: tuple[SessionObservationReducerError, ...]
    agent_events: int
    source_lines: int
    runtime_states: int


def rebuild_session_observation_projections(
    db: Session,
    *,
    session_id: UUID | None = None,
    runtime_key: str | None = None,
) -> SessionObservationRebuildResult:
    """Rebuild disposable session read models from ``session_observations``.

    This is intentionally session-scoped and internal. It clears transcript,
    source archive, and runtime-state projections for the supplied scope, then
    replays observations in database order. Raw observations are never deleted.
    """

    if session_id is None and not runtime_key:
        raise ValueError("rebuild requires session_id or runtime_key")

    observations = _load_observations(db, session_id=session_id, runtime_key=runtime_key)
    _clear_projection_rows(db, session_id=session_id, runtime_key=runtime_key)

    provider_events_reduced = 0
    bridge_events_reduced = 0
    source_lines_reduced = 0
    runtime_signals_reduced = 0
    skipped_observations = 0
    errors: list[SessionObservationReducerError] = []
    transcript_touched = False

    for observation in observations:
        try:
            if observation.kind == OBS_KIND_PROVIDER_EVENT:
                reduction = reduce_provider_event_observation(db, observation)
                if reduction.event is not None:
                    provider_events_reduced += 1
                    transcript_touched = True
                else:
                    skipped_observations += 1
            elif observation.kind == OBS_KIND_BRIDGE_TRANSCRIPT_DELTA:
                event = reduce_bridge_transcript_observation(db, observation)
                if event is not None:
                    bridge_events_reduced += 1
                    transcript_touched = True
                else:
                    skipped_observations += 1
            elif observation.kind == OBS_KIND_PROVIDER_SOURCE_LINE:
                row = reduce_source_line_observation(db, observation)
                if row is not None:
                    source_lines_reduced += 1
                else:
                    skipped_observations += 1
            elif observation.kind == OBS_KIND_RUNTIME_SIGNAL:
                outcome = reduce_runtime_signal_observation(db, observation)
                if outcome == "applied":
                    runtime_signals_reduced += 1
                else:
                    skipped_observations += 1
            else:
                skipped_observations += 1
        except Exception as exc:
            errors.append(
                SessionObservationReducerError(
                    observation_db_id=int(observation.id),
                    observation_id=observation.observation_id,
                    kind=observation.kind,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )

    if transcript_touched and session_id is not None:
        reconcile_provisional_transcript_events(db, session_id=session_id)

    db.flush()
    return SessionObservationRebuildResult(
        session_id=session_id,
        runtime_key=runtime_key,
        observations_seen=len(observations),
        newest_observation_db_id=max((int(observation.id) for observation in observations), default=None),
        provider_events_reduced=provider_events_reduced,
        bridge_events_reduced=bridge_events_reduced,
        source_lines_reduced=source_lines_reduced,
        runtime_signals_reduced=runtime_signals_reduced,
        skipped_observations=skipped_observations,
        reducer_errors=tuple(errors),
        agent_events=_projection_count(db, AgentEvent, session_id=session_id),
        source_lines=_projection_count(db, AgentSourceLine, session_id=session_id),
        runtime_states=_runtime_state_count(db, session_id=session_id, runtime_key=runtime_key),
    )


def _load_observations(db: Session, *, session_id: UUID | None, runtime_key: str | None) -> list[SessionObservation]:
    query = db.query(SessionObservation)
    filters = []
    if session_id is not None:
        filters.append(SessionObservation.session_id == session_id)
    if runtime_key:
        filters.append(SessionObservation.runtime_key == runtime_key)
    return query.filter(or_(*filters)).order_by(SessionObservation.id.asc()).all()


def _clear_projection_rows(db: Session, *, session_id: UUID | None, runtime_key: str | None) -> None:
    if session_id is not None:
        db.query(AgentEvent).filter(AgentEvent.session_id == session_id).delete(synchronize_session=False)
        db.query(AgentSourceLine).filter(AgentSourceLine.session_id == session_id).delete(synchronize_session=False)

    runtime_query = db.query(SessionRuntimeState)
    runtime_filters = []
    if session_id is not None:
        runtime_filters.append(SessionRuntimeState.session_id == session_id)
    if runtime_key:
        runtime_filters.append(SessionRuntimeState.runtime_key == runtime_key)
    if runtime_filters:
        runtime_query.filter(or_(*runtime_filters)).delete(synchronize_session=False)
    db.flush()


def _projection_count(db: Session, model, *, session_id: UUID | None) -> int:
    if session_id is None:
        return 0
    return int(db.query(model).filter(model.session_id == session_id).count())


def _runtime_state_count(db: Session, *, session_id: UUID | None, runtime_key: str | None) -> int:
    filters = []
    if session_id is not None:
        filters.append(SessionRuntimeState.session_id == session_id)
    if runtime_key:
        filters.append(SessionRuntimeState.runtime_key == runtime_key)
    if not filters:
        return 0
    return int(db.query(SessionRuntimeState).filter(or_(*filters)).count())
