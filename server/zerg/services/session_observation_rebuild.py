"""Session-scoped projection rebuilds from raw observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from uuid import UUID

from sqlalchemy import and_
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy.orm import Session

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentSessionBranch
from zerg.models.agents import AgentSourceLine
from zerg.models.agents import SessionObservation
from zerg.models.agents import SessionRuntimeState
from zerg.services.provisional_events import durable_transcript_event_predicate
from zerg.services.session_observation_reducers import reduce_bridge_transcript_observation
from zerg.services.session_observation_reducers import reduce_provider_event_observation
from zerg.services.session_observation_reducers import reduce_source_line_observation
from zerg.services.session_observations import OBS_KIND_BRIDGE_TRANSCRIPT_DELTA
from zerg.services.session_observations import OBS_KIND_PROVIDER_EVENT
from zerg.services.session_observations import OBS_KIND_PROVIDER_SOURCE_LINE
from zerg.services.session_observations import OBS_KIND_RUNTIME_SIGNAL
from zerg.services.session_runtime import reduce_runtime_signal_observation


class SessionObservationRebuildCoverageError(RuntimeError):
    """Raised when existing projections are not covered by observations."""


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
    _assert_projection_coverage(db, session_id=session_id, observations=observations)
    _clear_projection_rows(db, session_id=session_id, runtime_key=runtime_key)

    provider_events_reduced = 0
    bridge_events_reduced = 0
    source_lines_reduced = 0
    runtime_signals_reduced = 0
    skipped_observations = 0
    errors: list[SessionObservationReducerError] = []

    for observation in observations:
        try:
            if observation.kind == OBS_KIND_PROVIDER_EVENT:
                reduction = reduce_provider_event_observation(db, observation)
                if reduction.event is not None:
                    provider_events_reduced += 1
                else:
                    skipped_observations += 1
            elif observation.kind == OBS_KIND_BRIDGE_TRANSCRIPT_DELTA:
                reduce_bridge_transcript_observation(db, observation)
                bridge_events_reduced += 1
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

    if session_id is not None:
        _rebuild_branch_prefix_projections(db, session_id=session_id)
        _recompute_session_metadata(db, session_id=session_id)

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
    return (
        db.query(SessionObservation)
        .filter(_session_runtime_scope(SessionObservation, session_id=session_id, runtime_key=runtime_key))
        .order_by(SessionObservation.id.asc())
        .all()
    )


def _assert_projection_coverage(db: Session, *, session_id: UUID | None, observations: list[SessionObservation]) -> None:
    if session_id is None:
        return

    existing_events = int(db.query(AgentEvent.id).filter(AgentEvent.session_id == session_id).count())
    existing_source_lines = int(db.query(AgentSourceLine.id).filter(AgentSourceLine.session_id == session_id).count())
    if existing_events == 0 and existing_source_lines == 0:
        return

    transcript_observations = [
        observation
        for observation in observations
        if observation.session_id == session_id and observation.kind in (OBS_KIND_PROVIDER_EVENT, OBS_KIND_BRIDGE_TRANSCRIPT_DELTA)
    ]
    source_observations = [
        observation
        for observation in observations
        if observation.session_id == session_id and observation.kind == OBS_KIND_PROVIDER_SOURCE_LINE
    ]

    if existing_events and not transcript_observations:
        raise SessionObservationRebuildCoverageError(
            f"refusing to rebuild session {session_id}: {existing_events} transcript projection rows exist, "
            "but no transcript observations cover them"
        )
    if existing_source_lines and not source_observations:
        raise SessionObservationRebuildCoverageError(
            f"refusing to rebuild session {session_id}: {existing_source_lines} source archive rows exist, "
            "but no source-line observations cover them"
        )

    if existing_events:
        oldest_event_at = _as_utc_naive(db.query(func.min(AgentEvent.timestamp)).filter(AgentEvent.session_id == session_id).scalar())
        oldest_observation_at = min(
            (
                _as_utc_naive(observation.observed_at or observation.received_at)
                for observation in transcript_observations
                if observation.observed_at is not None or observation.received_at is not None
            ),
            default=None,
        )
        if oldest_event_at is not None and oldest_observation_at is not None and oldest_observation_at > oldest_event_at:
            raise SessionObservationRebuildCoverageError(
                f"refusing to rebuild session {session_id}: oldest transcript observation "
                f"{oldest_observation_at.isoformat()} is newer than oldest transcript projection "
                f"{oldest_event_at.isoformat()}"
            )


def _as_utc_naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _clear_projection_rows(db: Session, *, session_id: UUID | None, runtime_key: str | None) -> None:
    if session_id is not None:
        db.query(AgentEvent).filter(AgentEvent.session_id == session_id).delete(synchronize_session=False)
        db.query(AgentSourceLine).filter(AgentSourceLine.session_id == session_id).delete(synchronize_session=False)

    runtime_query = db.query(SessionRuntimeState)
    if session_id is not None or runtime_key:
        runtime_query.filter(_session_runtime_scope(SessionRuntimeState, session_id=session_id, runtime_key=runtime_key)).delete(
            synchronize_session=False
        )
    db.flush()


def _session_runtime_scope(model, *, session_id: UUID | None, runtime_key: str | None):
    if session_id is not None and runtime_key:
        return or_(
            model.session_id == session_id,
            and_(model.session_id.is_(None), model.runtime_key == runtime_key),
        )
    if session_id is not None:
        return model.session_id == session_id
    if runtime_key:
        return model.runtime_key == runtime_key
    raise ValueError("rebuild requires session_id or runtime_key")


def _rebuild_branch_prefix_projections(db: Session, *, session_id: UUID) -> None:
    branches = (
        db.query(AgentSessionBranch)
        .filter(AgentSessionBranch.session_id == session_id)
        .filter(AgentSessionBranch.parent_branch_id.isnot(None))
        .order_by(AgentSessionBranch.id.asc())
        .all()
    )
    for branch in branches:
        if branch.parent_branch_id is None:
            continue
        source_path = branch.branched_at_source_path
        offset = int(branch.branched_at_offset) if branch.branched_at_offset is not None else None
        if source_path is None or offset is None:
            continue
        _copy_source_prefix(
            db,
            session_id=session_id,
            from_branch_id=int(branch.parent_branch_id),
            to_branch_id=int(branch.id),
            source_path=source_path,
            offset=offset,
        )
        _copy_event_prefix(
            db,
            session_id=session_id,
            from_branch_id=int(branch.parent_branch_id),
            to_branch_id=int(branch.id),
            source_path=source_path,
            offset=offset,
        )
    if branches:
        db.flush()


def _recompute_session_metadata(db: Session, *, session_id: UUID) -> None:
    session_obj = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    if session_obj is None:
        return

    head_branch_id = (
        db.query(AgentSessionBranch.id)
        .filter(AgentSessionBranch.session_id == session_id)
        .filter(AgentSessionBranch.is_head == 1)
        .order_by(AgentSessionBranch.id.desc())
        .scalar()
    )
    if head_branch_id is None:
        head_branch_id = (
            db.query(AgentSessionBranch.id)
            .filter(AgentSessionBranch.session_id == session_id)
            .order_by(AgentSessionBranch.id.asc())
            .scalar()
        )
    if head_branch_id is None:
        return

    base_query = (
        db.query(AgentEvent)
        .filter(AgentEvent.session_id == session_id)
        .filter(AgentEvent.branch_id == int(head_branch_id))
        .filter(durable_transcript_event_predicate())
    )
    session_obj.user_messages = int(
        base_query.filter(AgentEvent.role == "user")
        .filter(or_(AgentEvent.content_text.is_(None), func.lower(func.trim(AgentEvent.content_text)) != "warmup"))
        .count()
    )
    session_obj.assistant_messages = int(base_query.filter(AgentEvent.role == "assistant").filter(AgentEvent.tool_name.is_(None)).count())
    session_obj.tool_calls = int(base_query.filter(AgentEvent.role == "assistant").filter(AgentEvent.tool_name.isnot(None)).count())

    last_activity_at = base_query.with_entities(func.max(AgentEvent.timestamp)).scalar()
    if last_activity_at is not None:
        session_obj.last_activity_at = last_activity_at

    revision_markers = {
        str(row.received_at or row.observed_at)
        for row in db.query(SessionObservation.received_at, SessionObservation.observed_at)
        .filter(SessionObservation.session_id == session_id)
        .filter(SessionObservation.kind == OBS_KIND_PROVIDER_EVENT)
        .all()
    }
    if revision_markers:
        session_obj.transcript_revision = len(revision_markers)
        session_obj.needs_embedding = 1


def _copy_source_prefix(
    db: Session,
    *,
    session_id: UUID,
    from_branch_id: int,
    to_branch_id: int,
    source_path: str,
    offset: int,
) -> None:
    from zerg.services.agents.kernel_writes import ensure_thread_id_for_session

    fallback_thread_id = ensure_thread_id_for_session(db, session_id)
    parent_rows = (
        db.query(AgentSourceLine)
        .filter(AgentSourceLine.session_id == session_id)
        .filter(AgentSourceLine.branch_id == from_branch_id)
        .order_by(AgentSourceLine.source_path.asc(), AgentSourceLine.source_offset.asc(), AgentSourceLine.revision.asc())
        .all()
    )
    latest_by_offset: dict[tuple[str, int], AgentSourceLine] = {}
    for row in parent_rows:
        key = (row.source_path, int(row.source_offset))
        prev = latest_by_offset.get(key)
        if prev is None or int(row.revision) > int(prev.revision):
            latest_by_offset[key] = row

    for row in latest_by_offset.values():
        row_offset = int(row.source_offset)
        if row.source_path == source_path and row_offset >= offset:
            continue
        exists = (
            db.query(AgentSourceLine.id)
            .filter(AgentSourceLine.session_id == session_id)
            .filter(AgentSourceLine.branch_id == to_branch_id)
            .filter(AgentSourceLine.source_path == row.source_path)
            .filter(AgentSourceLine.source_offset == row_offset)
            .filter(AgentSourceLine.line_hash == row.line_hash)
            .first()
        )
        if exists is not None:
            continue
        db.add(
            AgentSourceLine(
                session_id=session_id,
                thread_id=row.thread_id or fallback_thread_id,
                source_path=row.source_path,
                source_offset=row_offset,
                branch_id=to_branch_id,
                revision=1,
                is_branch_copy=1,
                raw_json=row.raw_json,
                raw_json_z=row.raw_json_z,
                raw_json_codec=row.raw_json_codec,
                line_hash=row.line_hash,
            )
        )


def _copy_event_prefix(
    db: Session,
    *,
    session_id: UUID,
    from_branch_id: int,
    to_branch_id: int,
    source_path: str,
    offset: int,
) -> None:
    from zerg.services.agents.kernel_writes import ensure_thread_id_for_session

    fallback_thread_id = ensure_thread_id_for_session(db, session_id)
    parent_events = (
        db.query(AgentEvent)
        .filter(AgentEvent.session_id == session_id)
        .filter(AgentEvent.branch_id == from_branch_id)
        .filter(durable_transcript_event_predicate())
        .order_by(AgentEvent.timestamp.asc(), AgentEvent.id.asc())
        .all()
    )
    for event in parent_events:
        event_offset = int(event.source_offset) if event.source_offset is not None else None
        if event.source_path == source_path and event_offset is not None and event_offset >= offset:
            continue
        exists = (
            db.query(AgentEvent.id)
            .filter(AgentEvent.session_id == session_id)
            .filter(AgentEvent.branch_id == to_branch_id)
            .filter(AgentEvent.source_path == event.source_path)
            .filter(AgentEvent.source_offset == event.source_offset)
            .filter(AgentEvent.event_hash == event.event_hash)
            .first()
        )
        if exists is not None:
            continue
        db.add(
            AgentEvent(
                session_id=session_id,
                thread_id=event.thread_id or fallback_thread_id,
                branch_id=to_branch_id,
                role=event.role,
                content_text=event.content_text,
                tool_name=event.tool_name,
                tool_input_json=event.tool_input_json,
                tool_output_text=event.tool_output_text,
                tool_call_id=event.tool_call_id,
                timestamp=event.timestamp,
                source_path=event.source_path,
                source_offset=event.source_offset,
                event_hash=event.event_hash,
                schema_version=event.schema_version,
                raw_json=event.raw_json,
                raw_json_z=event.raw_json_z,
                raw_json_codec=event.raw_json_codec,
                compaction_kind=event.compaction_kind,
                event_uuid=event.event_uuid,
                parent_event_uuid=event.parent_event_uuid,
                event_origin="durable",
            )
        )


def _projection_count(db: Session, model, *, session_id: UUID | None) -> int:
    if session_id is None:
        return 0
    return int(db.query(model).filter(model.session_id == session_id).count())


def _runtime_state_count(db: Session, *, session_id: UUID | None, runtime_key: str | None) -> int:
    if session_id is None and not runtime_key:
        return 0
    return int(
        db.query(SessionRuntimeState)
        .filter(_session_runtime_scope(SessionRuntimeState, session_id=session_id, runtime_key=runtime_key))
        .count()
    )
