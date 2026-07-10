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
        values = {target_name: transform(getattr(source, source_name)) for target_name, (source_name, transform) in fields.items()}
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
