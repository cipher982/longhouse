"""Live-catalog launch shells that do not open the cold monolith."""

from __future__ import annotations

import json
from datetime import datetime
from datetime import timezone
from typing import Any
from uuid import UUID
from uuid import uuid4

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionLaunchAttempt
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.models.live_store import LiveSessionThreadAlias
from zerg.models.live_store import LiveTimelineCard
from zerg.services.managed_provider_contracts import require_contract_for_provider

LIVE_CATALOG_CARD_REVISION = "live-catalog-v1"


def create_live_console_session_shell(db: Session, *, data: dict[str, Any]) -> LiveSessionCatalog:
    """Create an idle Console thread without a run or launch attempt."""

    session_id = str(data["session_id"])
    thread_id = str(data["thread_id"])
    provider = str(data["provider"])
    device_id = str(data["device_id"])
    cwd = str(data["cwd"])
    started_at = data["started_at"]
    project = str(data.get("project") or "console")
    display_name = str(data.get("display_name") or "").strip() or None
    session = db.get(LiveSessionCatalog, session_id)
    if session is None:
        session = LiveSessionCatalog(
            session_id=session_id,
            provider=provider,
            environment="development",
            project=project,
            device_id=device_id,
            device_name=str(data.get("machine_name") or device_id),
            cwd=cwd,
            git_repo=data.get("git_repo"),
            git_branch=data.get("git_branch"),
            started_at=started_at,
            last_activity_at=started_at,
            user_messages=0,
            assistant_messages=0,
            tool_calls=0,
            summary_title=display_name,
            primary_thread_id=thread_id,
            loop_mode="assist",
            permission_mode="bypass",
            launch_actor="user",
            launch_surface=str(data.get("launch_surface") or "console"),
            created_at=started_at,
            updated_at=started_at,
        )
        db.add(session)
    thread = db.get(LiveSessionThread, thread_id)
    if thread is None:
        db.add(
            LiveSessionThread(
                id=thread_id,
                session_id=session_id,
                provider=provider,
                device_id=device_id,
                cwd=cwd,
                provider_config_json=json.dumps(data.get("provider_config") or {}, sort_keys=True),
                branch_kind="root",
                is_primary=1,
                created_at=started_at,
                updated_at=started_at,
            )
        )
    card_values = {
        "session_id": session_id,
        "provider": provider,
        "environment": "development",
        "project": project,
        "device_id": device_id,
        "cwd": cwd,
        "started_at": started_at,
        "last_activity_at": started_at,
        "summary_title": display_name,
        "user_messages": 0,
        "assistant_messages": 0,
        "tool_calls": 0,
        "transcript_revision": 0,
        "archive_state": "pending",
        "archive_lag_records": 0,
        "origin_kind": None,
        "hidden_from_default_timeline": 0,
        "launch_actor": "user",
        "launch_surface": str(data.get("launch_surface") or "console"),
        "derived_state": "idle",
        "derived_revision": "0",
        "parser_revision": LIVE_CATALOG_CARD_REVISION,
        "updated_at": started_at,
    }
    card_insert = sqlite_insert(LiveTimelineCard).values(**card_values)
    db.execute(card_insert.on_conflict_do_nothing(index_elements=[LiveTimelineCard.session_id]))
    db.flush()
    return session


def _upsert_live_thread_alias(
    db: Session,
    *,
    thread_id: str,
    provider: str,
    kind: str,
    value: str | None,
    observed_at: datetime,
) -> None:
    clean = str(value or "").strip()
    if not clean:
        return
    row = (
        db.query(LiveSessionThreadAlias)
        .filter(
            LiveSessionThreadAlias.thread_id == thread_id,
            LiveSessionThreadAlias.provider == provider,
            LiveSessionThreadAlias.alias_kind == kind,
            LiveSessionThreadAlias.alias_value == clean,
        )
        .first()
    )
    if row is None:
        db.add(
            LiveSessionThreadAlias(
                thread_id=thread_id,
                provider=provider,
                alias_kind=kind,
                alias_value=clean,
                first_seen_at=observed_at,
                last_seen_at=observed_at,
            )
        )
    else:
        row.last_seen_at = observed_at


def attach_live_catalog_control(
    db: Session,
    *,
    session_id: UUID | str,
    provider: str,
    device_id: str | None,
    state: str,
    external_name: str | None = None,
    run_id: UUID | str | None = None,
    provider_session_id: str | None = None,
    source_path: str | None = None,
    launch_origin: str = "longhouse_spawned",
    force_new_run: bool = False,
    observed_at: datetime | None = None,
) -> LiveSessionConnection:
    """Materialize live kernel control from launch/lease evidence."""

    now = observed_at or datetime.now(timezone.utc)
    session = db.get(LiveSessionCatalog, str(session_id))
    if session is None or not session.primary_thread_id:
        raise RuntimeError(f"live catalog session/thread missing for control attach: {session_id}")
    thread_id = str(session.primary_thread_id)
    run = None
    if run_id is not None:
        run = db.get(LiveSessionRun, str(run_id))
    if run is None and not force_new_run:
        run = (
            db.query(LiveSessionRun)
            .filter(LiveSessionRun.thread_id == thread_id, LiveSessionRun.ended_at.is_(None))
            .order_by(LiveSessionRun.started_at.desc(), LiveSessionRun.id.desc())
            .first()
        )
    if run is None:
        if force_new_run:
            open_runs = db.query(LiveSessionRun).filter(LiveSessionRun.thread_id == thread_id, LiveSessionRun.ended_at.is_(None)).all()
            for open_run in open_runs:
                open_run.ended_at = now
                for old_connection in (
                    db.query(LiveSessionConnection)
                    .filter(LiveSessionConnection.run_id == open_run.id, LiveSessionConnection.released_at.is_(None))
                    .all()
                ):
                    old_connection.state = "released"
                    old_connection.released_at = now
        run = LiveSessionRun(
            id=str(run_id or uuid4()),
            thread_id=thread_id,
            provider=provider,
            host_id=device_id,
            cwd=session.cwd,
            launch_origin=launch_origin,
            started_at=now,
        )
        db.add(run)
        db.flush()

    contract = require_contract_for_provider(provider)
    connection = (
        db.query(LiveSessionConnection)
        .filter(
            LiveSessionConnection.run_id == str(run.id),
            LiveSessionConnection.control_plane == contract.control_plane,
        )
        .first()
    )
    if connection is None:
        connection = LiveSessionConnection(
            run_id=str(run.id),
            control_plane=contract.control_plane,
            acquisition_kind="spawned_control",
            acquired_at=now,
        )
        db.add(connection)
    caps = contract.connection_capabilities
    connection.state = state
    connection.external_name = external_name
    connection.device_id = device_id
    connection.can_send_input = caps["can_send_input"]
    connection.can_interrupt = caps["can_interrupt"]
    connection.can_terminate = caps["can_terminate"]
    connection.can_tail_output = caps["can_tail_output"]
    connection.can_resume = caps["can_resume"]
    connection.last_health_at = now
    connection.released_at = now if state in {"released", "ended"} else None
    session.ended_at = None
    session.updated_at = now
    _upsert_live_thread_alias(
        db,
        thread_id=thread_id,
        provider=provider,
        kind="provider_session_id",
        value=provider_session_id,
        observed_at=now,
    )
    _upsert_live_thread_alias(
        db,
        thread_id=thread_id,
        provider=provider,
        kind="source_path",
        value=source_path,
        observed_at=now,
    )
    db.flush()
    return connection


def create_live_launch_catalog_shell(
    db: Session,
    *,
    session_id: UUID,
    thread_id: UUID,
    run_id: UUID | None,
    owner_id: int,
    provider: str,
    device_id: str,
    device_name: str | None,
    cwd: str,
    project: str,
    git_repo: str | None,
    git_branch: str | None,
    display_name: str | None,
    initial_prompt: str | None,
    execution_lifetime: str,
    client_request_id: str | None,
    command_id: str,
    started_at: datetime,
    expires_at: datetime,
    launch_actor: str | None,
    launch_surface: str | None,
    loop_mode: str = "assist",
    permission_mode: str = "bypass",
) -> LiveSessionLaunchAttempt:
    """Create the synchronous launch identity in the live writer transaction."""

    session_key = str(session_id)
    thread_key = str(thread_id)
    run_key = str(run_id) if run_id is not None else None
    session = db.get(LiveSessionCatalog, session_key)
    if session is None:
        session = LiveSessionCatalog(
            session_id=session_key,
            provider=provider,
            environment="development",
            project=project,
            device_id=device_id,
            device_name=device_name or device_id,
            cwd=cwd,
            git_repo=git_repo,
            git_branch=git_branch,
            started_at=started_at,
            last_activity_at=started_at,
            user_messages=0,
            assistant_messages=0,
            tool_calls=0,
            summary_title=display_name,
            first_user_message_preview=(initial_prompt or "").strip()[:500] or None,
            last_visible_text_preview=(initial_prompt or "").strip()[:500] or None,
            primary_thread_id=thread_key,
            loop_mode=loop_mode,
            permission_mode=permission_mode,
            launch_actor=launch_actor,
            launch_surface=launch_surface,
            created_at=started_at,
            updated_at=started_at,
        )
        db.add(session)
    else:
        session.device_id = device_id
        session.device_name = device_name or device_id
        session.cwd = cwd
        session.project = project
        session.git_repo = git_repo or session.git_repo
        session.git_branch = git_branch or session.git_branch
        session.primary_thread_id = session.primary_thread_id or thread_key
        session.updated_at = started_at

    card_values = {
        "session_id": session_key,
        "provider": provider,
        "environment": "development",
        "project": project,
        "device_id": device_id,
        "cwd": cwd,
        "started_at": started_at,
        "last_activity_at": started_at,
        "summary_title": display_name,
        "first_user_message_preview": (initial_prompt or "").strip()[:500] or None,
        "last_visible_text_preview": (initial_prompt or "").strip()[:500] or None,
        "user_messages": 0,
        "assistant_messages": 0,
        "tool_calls": 0,
        "transcript_revision": 0,
        "archive_state": "pending",
        "archive_lag_records": 0,
        "origin_kind": None,
        "hidden_from_default_timeline": 0,
        "launch_actor": launch_actor,
        "launch_surface": launch_surface,
        "derived_state": "pending",
        "derived_revision": "0",
        "parser_revision": LIVE_CATALOG_CARD_REVISION,
        "updated_at": started_at,
    }
    card_insert = sqlite_insert(LiveTimelineCard).values(**card_values)
    db.execute(
        card_insert.on_conflict_do_update(
            index_elements=[LiveTimelineCard.session_id],
            set_={key: getattr(card_insert.excluded, key) for key in card_values if key != "session_id"},
        )
    )

    thread = db.get(LiveSessionThread, thread_key)
    if thread is None:
        db.add(
            LiveSessionThread(
                id=thread_key,
                session_id=session_key,
                provider=provider,
                branch_kind="root",
                is_primary=1,
                created_at=started_at,
                updated_at=started_at,
            )
        )

    if run_key is not None and db.get(LiveSessionRun, run_key) is None:
        db.add(
            LiveSessionRun(
                id=run_key,
                thread_id=thread_key,
                provider=provider,
                host_id=device_id,
                cwd=cwd,
                launch_origin="longhouse_spawned",
                started_at=started_at,
            )
        )

    attempt = db.query(LiveSessionLaunchAttempt).filter(LiveSessionLaunchAttempt.command_id == command_id).one_or_none()
    if attempt is None:
        attempt = LiveSessionLaunchAttempt(
            session_id=session_key,
            thread_id=thread_key,
            run_id=run_key,
            provider=provider,
            host_id=device_id,
            owner_id=owner_id,
            execution_lifetime=execution_lifetime,
            client_request_id=client_request_id,
            command_id=command_id,
            state="pending",
            expires_at=expires_at,
            created_at=started_at,
            updated_at=started_at,
        )
        db.add(attempt)
    db.flush()
    return attempt


def update_live_launch_catalog_outcome(
    db: Session,
    *,
    session_id: UUID,
    command_id: str,
    state: str,
    error_code: str | None = None,
    error_message: str | None = None,
    now: datetime | None = None,
) -> LiveSessionLaunchAttempt:
    observed_at = now or datetime.now(timezone.utc)
    attempt = db.query(LiveSessionLaunchAttempt).filter(LiveSessionLaunchAttempt.command_id == command_id).one_or_none()
    if attempt is None:
        raise RuntimeError(f"live launch attempt missing for command {command_id}")
    terminal = state in {"adopted", "failed", "abandoned"}
    attempt.state = state
    attempt.error_code = error_code
    attempt.error_message = error_message
    attempt.expires_at = None if terminal else attempt.expires_at
    attempt.updated_at = observed_at

    session = db.get(LiveSessionCatalog, str(session_id))
    if session is not None:
        session.last_activity_at = observed_at
        session.updated_at = observed_at
        if state == "failed":
            session.ended_at = observed_at
    card = db.get(LiveTimelineCard, str(session_id))
    if card is not None:
        card.last_activity_at = observed_at
        card.updated_at = observed_at
    db.flush()
    return attempt


def live_launch_result(attempt: LiveSessionLaunchAttempt) -> dict[str, Any]:
    state = str(attempt.state or "pending")
    projected_state = {
        "failed": "launch_failed",
        "abandoned": "launch_orphaned",
        "adopted": "live",
        "dispatched": "launching_unknown",
    }.get(state, "launching")
    from zerg.services.session_launch_lifecycle import format_remote_launch_error_message
    from zerg.services.session_launch_lifecycle import normalize_remote_launch_error_code

    error_code = normalize_remote_launch_error_code(attempt.error_code) if attempt.error_code is not None else None
    return {
        "session_id": UUID(str(attempt.session_id)),
        "launch_state": projected_state,
        "execution_lifetime": str(attempt.execution_lifetime or "live_control"),
        "launch_error_code": error_code,
        "launch_error_message": format_remote_launch_error_message(error_code, attempt.error_message),
    }
