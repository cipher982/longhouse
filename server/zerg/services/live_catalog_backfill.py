"""Idempotent cold-monolith to live-catalog backfill.

This is deliberately an operator/worker seam, not a Runtime Host startup task.
The API must eventually be able to boot without opening the cold database, so
catalog readiness cannot depend on an implicit copy during API lifespan.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from dataclasses import dataclass
from typing import Any
from typing import Iterable
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
from zerg.models.apns_device_registration import APNSDeviceRegistration
from zerg.models.apns_live_activity_registration import APNSLiveActivityRegistration
from zerg.models.apns_widget_push_state import APNSWidgetPushState
from zerg.models.device_token import DeviceToken
from zerg.models.live_store import LiveAPNSDeviceRegistration
from zerg.models.live_store import LiveAPNSLiveActivityRegistration
from zerg.models.live_store import LiveAPNSWidgetPushState
from zerg.models.live_store import LiveDeviceToken
from zerg.models.live_store import LiveRefreshSession
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionLaunchAttempt
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.models.live_store import LiveSessionThreadAlias
from zerg.models.live_store import LiveTimelineCard
from zerg.models.live_store import LiveUser
from zerg.models.refresh_session import RefreshSession
from zerg.models.user import User
from zerg.utils.time import normalize_utc


@dataclass
class LiveCatalogBackfillResult:
    users: int = 0
    refresh_sessions: int = 0
    device_tokens: int = 0
    apns_device_registrations: int = 0
    apns_live_activity_registrations: int = 0
    apns_widget_push_states: int = 0
    sessions: int = 0
    timeline_cards: int = 0
    session_threads: int = 0
    session_thread_aliases: int = 0
    session_runs: int = 0
    session_connections: int = 0
    session_launch_attempts: int = 0

    @property
    def total(self) -> int:
        return sum(asdict(self).values())

    def as_dict(self) -> dict[str, int]:
        return {**asdict(self), "total": self.total}


def _json_text(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _identity(value: Any) -> Any:
    return value


def _string(value: Any) -> str | None:
    return None if value is None else str(value)


def _enum_name(value: Any) -> Any:
    return getattr(value, "name", value)


def _upsert_rows(
    archive_db: Session,
    live_db: Session,
    *,
    source_model,
    target_model,
    fields: dict[str, tuple[str, Any]],
    batch_size: int,
) -> int:
    processed = 0
    pk_names = [column.name for column in target_model.__table__.primary_key.columns]
    query = archive_db.query(source_model).order_by(*source_model.__table__.primary_key.columns)
    for source in query.yield_per(batch_size):
        values = {}
        for target_name, (source_name, transform) in fields.items():
            values[target_name] = transform(getattr(source, source_name))
        values = _omit_legacy_null_defaults(target_model, values)
        statement = sqlite_insert(target_model).values(**values)
        updates = {name: getattr(statement.excluded, name) for name in values if name not in pk_names}
        live_db.execute(
            statement.on_conflict_do_update(
                index_elements=[getattr(target_model, name) for name in pk_names],
                set_=updates,
            )
        )
        processed += 1
        if processed % batch_size == 0:
            live_db.commit()
    live_db.commit()
    return processed


def _same(*names: str) -> dict[str, tuple[str, Any]]:
    return {name: (name, _identity) for name in names}


def _omit_legacy_null_defaults(target_model, values: dict[str, Any]) -> dict[str, Any]:
    """Let live-store defaults repair nullable values from legacy schemas.

    Some old archive databases predate current NOT NULL constraints.  Their
    ORM models describe the current schema, but rows can still contain NULL in
    fields such as ``user_state``.  Sending those NULLs to the stricter live
    catalog defeats its server defaults and aborts the whole catalog sync.

    Omit only target columns that are non-nullable *and* have an explicit
    default.  Truly required values without a default still fail loudly.
    """

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


def backfill_live_catalog(
    archive_db: Session,
    live_db: Session,
    *,
    batch_size: int = 500,
) -> LiveCatalogBackfillResult:
    """Copy bounded launch/auth/timeline state into the live store.

    Re-running updates rows in place by primary key. It never deletes live
    rows; deletion parity belongs to the later authoritative cutover, where
    live writes and explicit tombstones own lifecycle.
    """

    size = max(1, int(batch_size))
    result = LiveCatalogBackfillResult()
    result.users = _upsert_rows(
        archive_db,
        live_db,
        source_model=User,
        target_model=LiveUser,
        batch_size=size,
        fields={
            **_same(
                "id",
                "provider",
                "provider_user_id",
                "email",
                "cp_user_id",
                "email_verified",
                "is_active",
                "display_name",
                "avatar_url",
                "last_login",
                "created_at",
                "updated_at",
            ),
            "role": ("role", _enum_name),
            "prefs": ("prefs", _identity),
            "context": ("context", _identity),
        },
    )
    result.refresh_sessions = _upsert_rows(
        archive_db,
        live_db,
        source_model=RefreshSession,
        target_model=LiveRefreshSession,
        batch_size=size,
        fields=_same(
            "id",
            "token_hash",
            "user_id",
            "family_id",
            "parent_id",
            "created_at",
            "absolute_expires_at",
            "idle_expires_at",
            "used_at",
            "revoked_at",
        ),
    )
    result.device_tokens = _upsert_rows(
        archive_db,
        live_db,
        source_model=DeviceToken,
        target_model=LiveDeviceToken,
        batch_size=size,
        fields={
            "id": ("id", _string),
            **_same("owner_id", "device_id", "token_hash", "created_at", "last_used_at", "revoked_at"),
        },
    )
    result.apns_device_registrations = _upsert_rows(
        archive_db,
        live_db,
        source_model=APNSDeviceRegistration,
        target_model=LiveAPNSDeviceRegistration,
        batch_size=size,
        fields={
            "id": ("id", _string),
            **_same(
                "owner_id",
                "platform",
                "device_token",
                "push_environment",
                "app_build_id",
                "created_at",
                "updated_at",
                "last_seen_at",
                "revoked_at",
            ),
        },
    )
    result.apns_live_activity_registrations = _upsert_rows(
        archive_db,
        live_db,
        source_model=APNSLiveActivityRegistration,
        target_model=LiveAPNSLiveActivityRegistration,
        batch_size=size,
        fields={
            "id": ("id", _string),
            **_same(
                "owner_id",
                "session_id",
                "activity_id",
                "push_token",
                "push_environment",
                "app_build_id",
                "last_state_hash",
                "last_push_at",
                "created_at",
                "updated_at",
                "last_seen_at",
                "ended_at",
            ),
        },
    )
    result.apns_widget_push_states = _upsert_rows(
        archive_db,
        live_db,
        source_model=APNSWidgetPushState,
        target_model=LiveAPNSWidgetPushState,
        batch_size=size,
        fields=_same("owner_id", "state_hash", "last_push_at", "created_at", "updated_at"),
    )
    result.sessions = _upsert_rows(
        archive_db,
        live_db,
        source_model=AgentSession,
        target_model=LiveSessionCatalog,
        batch_size=size,
        fields={
            "session_id": ("id", _string),
            **_same(
                "provider",
                "environment",
                "project",
                "device_id",
                "device_name",
                "cwd",
                "git_repo",
                "git_branch",
                "started_at",
                "ended_at",
                "last_activity_at",
                "user_messages",
                "assistant_messages",
                "tool_calls",
                "summary",
                "summary_title",
                "anchor_title",
                "first_user_message_preview",
                "last_visible_text_preview",
                "last_user_message_preview",
                "last_assistant_message_preview",
                "transcript_revision",
                "summary_revision",
                "user_state",
                "user_state_at",
                "loop_mode",
                "notification_muted",
                "origin_kind",
                "hidden_from_default_timeline",
                "launch_actor",
                "launch_surface",
                "permission_mode",
                "created_at",
                "updated_at",
            ),
            "primary_thread_id": ("primary_thread_id", _string),
        },
    )
    result.timeline_cards = _upsert_rows(
        archive_db,
        live_db,
        source_model=TimelineCard,
        target_model=LiveTimelineCard,
        batch_size=size,
        fields={
            "session_id": ("session_id", _string),
            **_same(
                "provider",
                "environment",
                "project",
                "device_id",
                "cwd",
                "started_at",
                "last_activity_at",
                "summary_title",
                "first_user_message_preview",
                "last_visible_text_preview",
                "last_user_message_preview",
                "last_assistant_message_preview",
                "user_messages",
                "assistant_messages",
                "tool_calls",
                "transcript_revision",
                "archive_state",
                "archive_lag_records",
                "archive_last_source_offset",
                "origin_kind",
                "hidden_from_default_timeline",
                "launch_actor",
                "launch_surface",
                "derived_state",
                "derived_revision",
                "parser_revision",
                "updated_at",
            ),
        },
    )
    result.session_threads = _upsert_rows(
        archive_db,
        live_db,
        source_model=SessionThread,
        target_model=LiveSessionThread,
        batch_size=size,
        fields={
            "id": ("id", _string),
            "session_id": ("session_id", _string),
            "parent_thread_id": ("parent_thread_id", _string),
            **_same(
                "provider",
                "parent_event_id",
                "branch_kind",
                "origin_kind",
                "hidden_from_default_timeline",
                "is_primary",
                "created_at",
                "updated_at",
            ),
        },
    )
    result.session_thread_aliases = _upsert_rows(
        archive_db,
        live_db,
        source_model=SessionThreadAlias,
        target_model=LiveSessionThreadAlias,
        batch_size=size,
        fields={
            "thread_id": ("thread_id", _string),
            **_same("id", "provider", "alias_kind", "alias_value", "first_seen_at", "last_seen_at"),
        },
    )
    result.session_runs = _upsert_rows(
        archive_db,
        live_db,
        source_model=SessionRun,
        target_model=LiveSessionRun,
        batch_size=size,
        fields={
            "id": ("id", _string),
            "thread_id": ("thread_id", _string),
            "argv_redacted_json": ("argv_redacted_json", _json_text),
            **_same(
                "provider",
                "host_id",
                "boot_id",
                "pid",
                "process_start_time",
                "cwd",
                "launch_origin",
                "started_at",
                "ended_at",
                "exit_status",
            ),
        },
    )
    result.session_connections = _upsert_rows(
        archive_db,
        live_db,
        source_model=SessionConnection,
        target_model=LiveSessionConnection,
        batch_size=size,
        fields={
            "run_id": ("run_id", _string),
            "capabilities_extra_json": ("capabilities_extra_json", _json_text),
            **_same(
                "id",
                "control_plane",
                "acquisition_kind",
                "state",
                "external_name",
                "device_id",
                "can_send_input",
                "can_interrupt",
                "can_terminate",
                "can_tail_output",
                "can_resume",
                "acquired_at",
                "released_at",
                "last_health_at",
            ),
        },
    )
    result.session_launch_attempts = _upsert_rows(
        archive_db,
        live_db,
        source_model=SessionLaunchAttempt,
        target_model=LiveSessionLaunchAttempt,
        batch_size=size,
        fields={
            "session_id": ("session_id", _string),
            "thread_id": ("thread_id", _string),
            "run_id": ("run_id", _string),
            **_same(
                "id",
                "provider",
                "host_id",
                "owner_id",
                "execution_lifetime",
                "client_request_id",
                "command_id",
                "state",
                "error_code",
                "error_message",
                "expires_at",
                "created_at",
                "updated_at",
            ),
        },
    )
    return result


def live_catalog_table_names() -> tuple[str, ...]:
    """Stable inventory used by readiness and repository guard tests."""

    return (
        "users",
        "refresh_sessions",
        "device_tokens",
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


def count_live_catalog_rows(live_db: Session, table_models: Iterable[Any] | None = None) -> dict[str, int]:
    models = tuple(table_models or (LiveUser, LiveDeviceToken, LiveSessionCatalog, LiveTimelineCard))
    return {model.__tablename__: int(live_db.query(model).count()) for model in models}


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
    _upsert_catalog_values(
        live_db,
        LiveSessionCatalog,
        _catalog_values(source_session, LiveSessionCatalog, id_mapping={"session_id": "id"}),
    )
    source_card = archive_db.get(TimelineCard, session_id)
    if source_card is not None:
        _upsert_catalog_values(
            live_db,
            LiveTimelineCard,
            _catalog_values(source_card, LiveTimelineCard),
        )
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
        attempt_query = live_db.query(LiveSessionLaunchAttempt)
        existing_attempt = attempt_query.filter(LiveSessionLaunchAttempt.command_id == attempt.command_id).first()
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
            synced = 0
            for session_id in session_ids:
                synced += int(sync_live_catalog_session(archive_db, live_db, session_id=session_id))
    return synced
