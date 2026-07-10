"""Project durable archive session state into the bounded live catalog."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.models.agents import SessionConnection
from zerg.models.agents import SessionLaunchAttempt
from zerg.models.agents import SessionRun
from zerg.models.agents import SessionThread
from zerg.models.agents import SessionThreadAlias
from zerg.models.agents import TimelineCard
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionLaunchAttempt
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.models.live_store import LiveSessionThreadAlias
from zerg.models.live_store import LiveTimelineCard
from zerg.utils.time import normalize_utc


def _json_text(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _omit_legacy_null_defaults(target_model, values: dict[str, Any]) -> dict[str, Any]:
    """Let live-store defaults repair nullable values from old archive rows."""

    columns = target_model.__table__.columns
    return {
        name: value
        for name, value in values.items()
        if not (
            value is None
            and name in columns
            and not columns[name].nullable
            and (columns[name].default is not None or columns[name].server_default is not None)
        )
    }


def live_catalog_table_names() -> tuple[str, ...]:
    """Stable inventory used by readiness and repository guard tests."""

    return (
        "users",
        "refresh_sessions",
        "device_tokens",
        "notification_client_presence",
        "apns_device_registrations",
        "apns_live_activity_registrations",
        "apns_widget_push_states",
        "live_session_catalog",
        "live_timeline_cards",
        "live_session_threads",
        "live_session_thread_aliases",
        "live_session_runs",
        "live_session_connections",
        "live_session_launch_attempts",
    )


def _catalog_values(source: Any, target_model, *, id_mapping: dict[str, str] | None = None) -> dict[str, Any]:
    mapping = id_mapping or {}
    values: dict[str, Any] = {}
    for column in target_model.__table__.columns:
        source_name = mapping.get(column.name, column.name)
        if not hasattr(source, source_name):
            continue
        value = getattr(source, source_name)
        if isinstance(value, UUID):
            value = str(value)
        elif column.name in {"session_id", "primary_thread_id", "id", "thread_id", "run_id"} and value is not None:
            value = str(value)
        values[column.name] = value
    return _omit_legacy_null_defaults(target_model, values)


def _upsert_catalog_values(live_db: Session, target_model, values: dict[str, Any]) -> None:
    if not values:
        return
    primary_keys = [column.name for column in target_model.__table__.primary_key.columns]
    statement = sqlite_insert(target_model).values(**values)
    updates = {name: getattr(statement.excluded, name) for name in values if name not in primary_keys}
    live_db.execute(
        statement.on_conflict_do_update(
            index_elements=[getattr(target_model, name) for name in primary_keys],
            set_=updates,
        )
    )


def sync_live_catalog_session(archive_db: Session, live_db: Session, *, session_id: Any) -> bool:
    """Refresh bounded card and control-kernel rows after a cold commit."""

    source_session = archive_db.get(AgentSession, session_id)
    if source_session is None:
        return False
    session_values = _catalog_values(source_session, LiveSessionCatalog, id_mapping={"session_id": "id"})
    if live_db.get(LiveSessionCatalog, str(session_id)) is not None:
        # These fields are user-owned live state. Cold projection may seed them
        # for a newly discovered session, but must never overwrite later edits.
        for field in ("user_state", "user_state_at", "loop_mode", "notification_muted"):
            session_values.pop(field, None)
    _upsert_catalog_values(live_db, LiveSessionCatalog, session_values)
    source_card = archive_db.get(TimelineCard, session_id)
    if source_card is not None:
        _upsert_catalog_values(live_db, LiveTimelineCard, _catalog_values(source_card, LiveTimelineCard))

    threads = archive_db.query(SessionThread).filter(SessionThread.session_id == source_session.id).all()
    thread_ids = [thread.id for thread in threads]
    for thread in threads:
        _upsert_catalog_values(live_db, LiveSessionThread, _catalog_values(thread, LiveSessionThread))
    if thread_ids:
        aliases = archive_db.query(SessionThreadAlias).filter(SessionThreadAlias.thread_id.in_(thread_ids)).all()
        for alias in aliases:
            existing_alias = (
                live_db.query(LiveSessionThreadAlias)
                .filter(
                    LiveSessionThreadAlias.thread_id == str(alias.thread_id),
                    LiveSessionThreadAlias.provider == alias.provider,
                    LiveSessionThreadAlias.alias_kind == alias.alias_kind,
                    LiveSessionThreadAlias.alias_value == alias.alias_value,
                )
                .first()
            )
            if existing_alias is not None:
                existing_alias.first_seen_at = min(existing_alias.first_seen_at, alias.first_seen_at)
                existing_alias.last_seen_at = max(existing_alias.last_seen_at, alias.last_seen_at)
            else:
                _upsert_catalog_values(live_db, LiveSessionThreadAlias, _catalog_values(alias, LiveSessionThreadAlias))

        runs = archive_db.query(SessionRun).filter(SessionRun.thread_id.in_(thread_ids)).all()
        run_ids = [run.id for run in runs]
        for run in runs:
            values = _catalog_values(run, LiveSessionRun)
            values["argv_redacted_json"] = _json_text(run.argv_redacted_json)
            _upsert_catalog_values(live_db, LiveSessionRun, values)
        if run_ids:
            connections = archive_db.query(SessionConnection).filter(SessionConnection.run_id.in_(run_ids)).all()
            for connection in connections:
                values = _catalog_values(connection, LiveSessionConnection)
                values["capabilities_extra_json"] = _json_text(connection.capabilities_extra_json)
                existing_connection = (
                    live_db.query(LiveSessionConnection)
                    .filter(
                        LiveSessionConnection.run_id == str(connection.run_id),
                        LiveSessionConnection.control_plane == connection.control_plane,
                    )
                    .first()
                )
                if existing_connection is not None:
                    values.pop("id", None)
                    live_health = normalize_utc(existing_connection.last_health_at)
                    archive_health = normalize_utc(connection.last_health_at)
                    if live_health is not None and (archive_health is None or live_health > archive_health):
                        for key in (
                            "state",
                            "released_at",
                            "last_health_at",
                            "can_send_input",
                            "can_interrupt",
                            "can_terminate",
                            "can_tail_output",
                            "can_resume",
                        ):
                            values.pop(key, None)
                    for key, value in values.items():
                        setattr(existing_connection, key, value)
                else:
                    _upsert_catalog_values(live_db, LiveSessionConnection, values)

    attempts = archive_db.query(SessionLaunchAttempt).filter(SessionLaunchAttempt.session_id == source_session.id).all()
    for attempt in attempts:
        values = _catalog_values(attempt, LiveSessionLaunchAttempt)
        existing_attempt = live_db.query(LiveSessionLaunchAttempt).filter(LiveSessionLaunchAttempt.command_id == attempt.command_id).first()
        if existing_attempt is not None:
            values.pop("id", None)
            for key, value in values.items():
                setattr(existing_attempt, key, value)
        else:
            _upsert_catalog_values(live_db, LiveSessionLaunchAttempt, values)
    live_db.commit()
    return True


def sync_recent_live_catalog(*, limit: int = 25) -> int:
    """Refresh the most recently changed cold sessions from inside the worker."""

    from zerg.database import get_live_session_factory
    from zerg.database import get_write_session_factory

    archive_factory = get_write_session_factory()
    live_factory = get_live_session_factory()
    if archive_factory is None or live_factory is None:
        return 0
    with archive_factory() as archive_db:
        session_ids = [
            row[0]
            for row in (
                archive_db.query(AgentSession.id)
                .order_by(AgentSession.updated_at.desc(), AgentSession.last_activity_at.desc(), AgentSession.id.desc())
                .limit(max(1, int(limit)))
                .all()
            )
        ]
        with live_factory() as live_db:
            return sum(int(sync_live_catalog_session(archive_db, live_db, session_id=session_id)) for session_id in session_ids)
