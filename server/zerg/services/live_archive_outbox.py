"""Live Store outbox bridge into the archive SQLite lane."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from zerg.models.agents import AgentHeartbeat
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionConnection
from zerg.models.agents import SessionInput
from zerg.models.agents import SessionLaunchAttempt
from zerg.models.agents import SessionRun
from zerg.models.agents import SessionThread
from zerg.models.agents import SessionTurn
from zerg.models.live_store import LiveArchiveOutbox
from zerg.models.live_store import LiveLaunchReadiness
from zerg.models.live_store import LiveSessionInputReceipt
from zerg.models.live_store import LiveSessionLaunchAttempt
from zerg.services.agents.kernel_writes import ensure_open_run_for_session
from zerg.services.agents.kernel_writes import ensure_primary_thread
from zerg.services.agents.kernel_writes import record_thread_alias
from zerg.services.agents.kernel_writes import set_thread_execution_target
from zerg.services.agents.kernel_writes import upsert_connection_for_run
from zerg.services.live_catalog_launch import update_live_launch_catalog_outcome
from zerg.services.live_launch_readiness import MANAGED_LOCAL_LAUNCH_OUTBOX_KIND
from zerg.services.live_launch_readiness import REMOTE_LAUNCH_OUTBOX_KIND
from zerg.services.live_launch_readiness import REMOTE_LAUNCH_OUTCOME_OUTBOX_KIND
from zerg.services.live_launch_readiness import update_live_launch_readiness_state
from zerg.services.managed_local_launcher import ManagedLocalLaunchPlan
from zerg.services.managed_local_launcher import managed_provider_requires_readiness_proof
from zerg.services.managed_local_launcher import materialize_managed_local_launch_plan_sync
from zerg.services.managed_provider_contracts import require_contract_for_provider
from zerg.services.session_inputs import INPUT_STATUS_DELIVERED
from zerg.services.session_inputs import INPUT_STATUS_DELIVERING
from zerg.services.session_inputs import VALID_INTENTS
from zerg.services.session_kernel_projection import is_synthetic_provider_session_id
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import ingest_runtime_events
from zerg.session_loop_mode import SessionLoopMode
from zerg.utils.time import normalize_utc

HEARTBEAT_STAMP_KIND = "heartbeat_stamp.v1"
MANAGED_LOCAL_LAUNCH_KIND = MANAGED_LOCAL_LAUNCH_OUTBOX_KIND
REMOTE_LAUNCH_KIND = REMOTE_LAUNCH_OUTBOX_KIND
REMOTE_LAUNCH_OUTCOME_KIND = REMOTE_LAUNCH_OUTCOME_OUTBOX_KIND
RUNTIME_EVENT_KIND = "runtime_event.v1"
SESSION_INPUT_RECEIPT_KIND = "session_input_receipt.v1"
CONSOLE_SESSION_CREATE_KIND = "console_session_create.v1"
AUTO_RESUME_PHASES = {"thinking", "running"}
ONE_SHOT_CONTROL_PLANE_BY_PROVIDER = {
    "codex": "codex_exec",
    "cursor": "cursor_acp",
}
_LAUNCH_RECEIPT_RETENTION = timedelta(days=30)
_LAUNCH_RECEIPT_KINDS = (MANAGED_LOCAL_LAUNCH_KIND, REMOTE_LAUNCH_KIND, REMOTE_LAUNCH_OUTCOME_KIND)


@dataclass(frozen=True)
class LiveArchiveDrainResult:
    processed: int = 0
    drained: int = 0
    failed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "processed": self.processed,
            "drained": self.drained,
            "failed": self.failed,
        }


def heartbeat_stamp_idempotency_key(heartbeat: dict[str, Any]) -> str:
    device_id = str(heartbeat.get("device_id") or "").strip()
    received_at = heartbeat.get("received_at")
    if isinstance(received_at, datetime):
        received_key = received_at.isoformat()
    else:
        received_key = str(received_at or "").strip()
    return f"{HEARTBEAT_STAMP_KIND}:{device_id}:{received_key}"


def enqueue_heartbeat_stamp_outbox(
    db: Session,
    heartbeat: dict[str, Any],
    *,
    idempotency_key: str | None = None,
) -> bool:
    """Queue a live heartbeat stamp for async archive durability.

    This is intentionally called inside the same Live Store transaction as the
    hot heartbeat stamp write. If enqueue fails, the hot write fails too; the
    live lane must not claim a fact that cannot be durably replayed.
    """

    key = idempotency_key or heartbeat_stamp_idempotency_key(heartbeat)
    existing = db.query(LiveArchiveOutbox.id).filter(LiveArchiveOutbox.idempotency_key == key).first()
    if existing is not None:
        return False
    db.add(
        LiveArchiveOutbox(
            idempotency_key=key,
            kind=HEARTBEAT_STAMP_KIND,
            payload_json=json.dumps({"heartbeat": _jsonable(heartbeat)}, sort_keys=True),
        )
    )
    return True


def runtime_event_idempotency_key(event: RuntimeEventIngest) -> str:
    occurred_at = normalize_utc(event.occurred_at) or event.occurred_at
    raw_key = f"{RUNTIME_EVENT_KIND}:{event.source}:{event.dedupe_key}:" f"{event.runtime_key}:{event.kind}:{occurred_at.isoformat()}"
    if len(raw_key) <= 512:
        return raw_key
    digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    return f"{RUNTIME_EVENT_KIND}:sha256:{digest}"


def enqueue_runtime_event_outbox(
    db: Session,
    event: RuntimeEventIngest,
    *,
    idempotency_key: str | None = None,
) -> bool:
    """Queue a runtime event for archive durability in the live transaction."""

    key = idempotency_key or runtime_event_idempotency_key(event)
    existing = db.query(LiveArchiveOutbox.id).filter(LiveArchiveOutbox.idempotency_key == key).first()
    if existing is not None:
        return False
    db.add(
        LiveArchiveOutbox(
            idempotency_key=key,
            kind=RUNTIME_EVENT_KIND,
            payload_json=json.dumps(
                {"event": _jsonable(event.model_dump())},
                sort_keys=True,
            ),
        )
    )
    return True


def enqueue_runtime_events_outbox(db: Session, events: list[RuntimeEventIngest]) -> int:
    queued = 0
    for event in events:
        if enqueue_runtime_event_outbox(db, event):
            queued += 1
    return queued


def managed_local_launch_idempotency_key(*, session_id: UUID | str) -> str:
    return f"{MANAGED_LOCAL_LAUNCH_KIND}:{str(session_id).strip()}"


def _prune_completed_launch_receipts(db: Session, *, now: datetime) -> None:
    db.query(LiveArchiveOutbox).filter(
        LiveArchiveOutbox.kind.in_(_LAUNCH_RECEIPT_KINDS),
        LiveArchiveOutbox.drained_at.isnot(None),
        LiveArchiveOutbox.drained_at < now - _LAUNCH_RECEIPT_RETENTION,
    ).delete(synchronize_session=False)


def enqueue_managed_local_launch_outbox(
    db: Session,
    *,
    plan: ManagedLocalLaunchPlan,
    owner_id: int,
    git_repo: str | None,
    git_branch: str | None,
    started_at: datetime,
    idempotency_key: str | None = None,
    completed: bool = False,
) -> bool:
    """Persist managed-local launch idempotency evidence."""

    _prune_completed_launch_receipts(db, now=datetime.now(timezone.utc))
    key = idempotency_key or managed_local_launch_idempotency_key(session_id=plan.session_id)
    existing = db.query(LiveArchiveOutbox.id).filter(LiveArchiveOutbox.idempotency_key == key).first()
    if existing is not None:
        return False
    db.add(
        LiveArchiveOutbox(
            idempotency_key=key,
            kind=MANAGED_LOCAL_LAUNCH_KIND,
            payload_json=json.dumps(
                {
                    "launch": _jsonable(
                        {
                            "owner_id": int(owner_id),
                            "git_repo": git_repo,
                            "git_branch": git_branch,
                            "started_at": started_at,
                            "plan": {
                                "session_id": plan.session_id,
                                "provider": plan.provider,
                                "provider_session_id": plan.provider_session_id,
                                "source_name": plan.source_name,
                                "source_runner_id": plan.source_runner_id,
                                "cwd": plan.cwd,
                                "project": plan.project,
                                "display_name": plan.display_name,
                                "managed_session_name": plan.managed_session_name,
                                "loop_mode": plan.loop_mode,
                                "permission_mode": plan.permission_mode,
                                "launch_actor": plan.launch_actor,
                                "launch_surface": plan.launch_surface,
                                "managed_transport": plan.managed_transport,
                                "attach_command": plan.attach_command,
                            },
                        }
                    )
                },
                sort_keys=True,
            ),
            drained_at=started_at if completed else None,
        )
    )
    return True


def remote_launch_idempotency_key(*, session_id: UUID | str) -> str:
    return f"{REMOTE_LAUNCH_KIND}:{str(session_id).strip()}"


def remote_launch_outcome_idempotency_key(*, session_id: UUID | str) -> str:
    return f"{REMOTE_LAUNCH_OUTCOME_KIND}:{str(session_id).strip()}"


def enqueue_remote_launch_outbox(
    db: Session,
    *,
    launch: dict[str, Any],
    idempotency_key: str | None = None,
    completed: bool = False,
) -> bool:
    """Persist remote-launch idempotency evidence."""

    session_id = str(launch.get("session_id") or "").strip()
    if not session_id:
        raise ValueError("remote launch outbox is missing session_id")
    key = idempotency_key or remote_launch_idempotency_key(session_id=session_id)
    return _enqueue_json_outbox(
        db,
        idempotency_key=key,
        kind=REMOTE_LAUNCH_KIND,
        payload={"launch": _jsonable(launch)},
        completed=completed,
    )


def enqueue_remote_launch_outcome_outbox(
    db: Session,
    *,
    launch: dict[str, Any],
    outcome: dict[str, Any],
    idempotency_key: str | None = None,
    completed: bool = False,
) -> bool:
    """Persist remote-launch outcome idempotency evidence."""

    session_id = str(launch.get("session_id") or "").strip()
    if not session_id:
        raise ValueError("remote launch outcome outbox is missing session_id")
    outcome_state = str(outcome.get("state") or "unknown").strip() or "unknown"
    key = idempotency_key or f"{remote_launch_outcome_idempotency_key(session_id=session_id)}:{outcome_state}"
    return _enqueue_json_outbox(
        db,
        idempotency_key=key,
        kind=REMOTE_LAUNCH_OUTCOME_KIND,
        payload={"launch": _jsonable(launch), "outcome": _jsonable(outcome)},
        completed=completed,
    )


def _enqueue_json_outbox(
    db: Session,
    *,
    idempotency_key: str,
    kind: str,
    payload: dict[str, Any],
    completed: bool,
) -> bool:
    now = datetime.now(timezone.utc)
    _prune_completed_launch_receipts(db, now=now)
    existing = db.query(LiveArchiveOutbox.id).filter(LiveArchiveOutbox.idempotency_key == idempotency_key).first()
    if existing is not None:
        return False
    db.add(
        LiveArchiveOutbox(
            idempotency_key=idempotency_key,
            kind=kind,
            payload_json=json.dumps(payload, sort_keys=True),
            drained_at=now if completed else None,
        )
    )
    return True


def enqueue_console_session_create_outbox(db: Session, *, session: dict[str, Any]) -> bool:
    session_id = str(session.get("session_id") or "").strip()
    return _enqueue_json_outbox(
        db,
        idempotency_key=f"{CONSOLE_SESSION_CREATE_KIND}:{session_id}",
        kind=CONSOLE_SESSION_CREATE_KIND,
        payload={"session": _jsonable(session)},
        completed=False,
    )


def session_input_receipt_idempotency_key(*, receipt_id: str) -> str:
    return f"{SESSION_INPUT_RECEIPT_KIND}:{str(receipt_id).strip()}"


def enqueue_session_input_receipt_outbox(
    db: Session,
    *,
    receipt_id: str,
    owner_id: int,
    session_id: UUID | str,
    text: str,
    intent: str,
    client_request_id: str | None,
    delivery_request_id: str | None,
    idempotency_key: str | None = None,
) -> bool:
    """Queue a delivered live input receipt for async archive provenance."""

    clean_receipt_id = str(receipt_id or "").strip()
    clean_delivery_request_id = str(delivery_request_id or "").strip()
    if not clean_receipt_id:
        raise ValueError("session input receipt outbox is missing receipt_id")
    if not clean_delivery_request_id:
        raise ValueError("session input receipt outbox is missing delivery_request_id")

    key = idempotency_key or session_input_receipt_idempotency_key(receipt_id=clean_receipt_id)
    existing = db.query(LiveArchiveOutbox.id).filter(LiveArchiveOutbox.idempotency_key == key).first()
    if existing is not None:
        return False
    db.add(
        LiveArchiveOutbox(
            idempotency_key=key,
            kind=SESSION_INPUT_RECEIPT_KIND,
            payload_json=json.dumps(
                {
                    "receipt": _jsonable(
                        {
                            "id": clean_receipt_id,
                            "owner_id": int(owner_id),
                            "session_id": str(session_id),
                            "text": str(text or ""),
                            "intent": str(intent or "auto"),
                            "client_request_id": str(client_request_id).strip() if client_request_id else None,
                            "delivery_request_id": clean_delivery_request_id,
                        }
                    )
                },
                sort_keys=True,
            ),
        )
    )
    return True


def cleanup_drained_live_archive_outbox(
    db: Session,
    *,
    older_than: datetime,
    limit: int = 1000,
) -> int:
    """Delete drained outbox rows older than the retention cutoff."""

    if limit <= 0:
        return 0
    cutoff = normalize_utc(older_than) or older_than
    ids = [
        row.id
        for row in (
            db.query(LiveArchiveOutbox.id)
            .filter(LiveArchiveOutbox.drained_at.isnot(None))
            .filter(LiveArchiveOutbox.drained_at < cutoff)
            .order_by(LiveArchiveOutbox.drained_at.asc(), LiveArchiveOutbox.id.asc())
            .limit(limit)
            .all()
        )
    ]
    if not ids:
        return 0
    return db.query(LiveArchiveOutbox).filter(LiveArchiveOutbox.id.in_(ids)).delete(synchronize_session=False)


def drain_live_archive_outbox(
    live_db: Session,
    archive_db: Session,
    *,
    limit: int = 100,
    now: datetime | None = None,
    exclude_kinds: set[str] | None = None,
) -> LiveArchiveDrainResult:
    """Drain a bounded Live Store outbox batch into archive SQLite.

    Archive commit happens before the outbox row is marked drained. If the live
    commit fails after the archive commit, a future drain retries the row and
    idempotency prevents duplicate archive heartbeat rows.
    """

    if limit <= 0:
        return LiveArchiveDrainResult()

    drained_at = now or datetime.now(timezone.utc)
    query = live_db.query(LiveArchiveOutbox).filter(LiveArchiveOutbox.drained_at.is_(None))
    if exclude_kinds:
        query = query.filter(LiveArchiveOutbox.kind.notin_(sorted(exclude_kinds)))
    rows = query.order_by(LiveArchiveOutbox.created_at.asc(), LiveArchiveOutbox.id.asc()).limit(limit).all()
    if not rows:
        return LiveArchiveDrainResult()

    processed = 0
    drained = 0
    failed = 0
    for row in rows:
        processed += 1
        row.attempts = int(row.attempts or 0) + 1
        try:
            effects = apply_live_archive_outbox_to_archive(row, archive_db)
            archive_db.commit()
            apply_live_archive_outbox_ack(row, live_db, effects)
        except Exception as exc:
            archive_db.rollback()
            row.last_error = f"{type(exc).__name__}: {exc}"
            failed += 1
            continue

        row.drained_at = drained_at
        row.last_error = None
        drained += 1

    try:
        live_db.commit()
    except Exception:
        live_db.rollback()
        return LiveArchiveDrainResult(processed=processed, drained=0, failed=processed)

    return LiveArchiveDrainResult(processed=processed, drained=drained, failed=failed)


def apply_live_archive_outbox_to_archive(row: LiveArchiveOutbox, archive_db: Session) -> dict[str, Any]:
    """Apply one outbox row to cold state without opening or mutating live state."""

    if row.kind == HEARTBEAT_STAMP_KIND:
        _drain_heartbeat_stamp(row, archive_db)
        return {}
    if row.kind == RUNTIME_EVENT_KIND:
        _drain_runtime_event(row, archive_db)
        return {}
    if row.kind == MANAGED_LOCAL_LAUNCH_KIND:
        _drain_managed_local_launch(row, archive_db)
        return {}
    if row.kind == REMOTE_LAUNCH_KIND:
        _drain_remote_launch(row, archive_db)
        return {}
    if row.kind == REMOTE_LAUNCH_OUTCOME_KIND:
        _drain_remote_launch_outcome(row, archive_db)
        return {}
    if row.kind == CONSOLE_SESSION_CREATE_KIND:
        _drain_console_session_create(row, archive_db)
        return {}
    if row.kind == SESSION_INPUT_RECEIPT_KIND:
        return _project_session_input_receipt(row, archive_db)
    raise ValueError(f"Unsupported live archive outbox kind: {row.kind}")


def apply_live_archive_outbox_ack(row: LiveArchiveOutbox, live_db: Session, effects: dict[str, Any]) -> None:
    """Apply the short live-side acknowledgement after cold state commits."""

    if row.kind == SESSION_INPUT_RECEIPT_KIND:
        _ack_session_input_receipt(row, live_db, effects)
        return
    _mark_live_side_effects_after_archive_commit(row, live_db)


def _drain_heartbeat_stamp(row: LiveArchiveOutbox, archive_db: Session) -> None:
    payload = json.loads(row.payload_json or "{}")
    heartbeat = _restore_jsonable(payload.get("heartbeat") or {})
    device_id = str(heartbeat.get("device_id") or "").strip()
    received_at = normalize_utc(heartbeat.get("received_at"))
    if not device_id or received_at is None:
        raise ValueError("heartbeat outbox payload is missing device_id or received_at")

    exists = (
        archive_db.query(AgentHeartbeat.id)
        .filter(
            AgentHeartbeat.device_id == device_id,
            AgentHeartbeat.received_at == received_at,
        )
        .first()
    )
    if exists is not None:
        return
    archive_payload = {**heartbeat, "received_at": received_at}
    archive_db.add(AgentHeartbeat(**archive_payload))


def _drain_runtime_event(row: LiveArchiveOutbox, archive_db: Session) -> None:
    payload = json.loads(row.payload_json or "{}")
    event_payload = _restore_jsonable(payload.get("event") or {})
    event = RuntimeEventIngest.model_validate(event_payload)
    ingest_runtime_events(archive_db, [event])
    if event.session_id is not None and event.kind == "phase_signal" and event.phase in AUTO_RESUME_PHASES:
        occurred_at = normalize_utc(event.occurred_at) or datetime.now(timezone.utc)
        archive_db.query(AgentSession).filter(
            AgentSession.id == event.session_id,
            AgentSession.user_state == "snoozed",
        ).update(
            {"user_state": "active", "user_state_at": occurred_at},
            synchronize_session=False,
        )


def _drain_managed_local_launch(row: LiveArchiveOutbox, archive_db: Session) -> None:
    payload = json.loads(row.payload_json or "{}")
    launch = _restore_jsonable(payload.get("launch") or {})
    plan = _restore_managed_local_launch_plan(launch.get("plan") or {})
    started_at = normalize_utc(launch.get("started_at")) or datetime.now(timezone.utc)
    materialize_managed_local_launch_plan_sync(
        archive_db,
        plan,
        git_repo=str(launch.get("git_repo") or "").strip() or None,
        git_branch=str(launch.get("git_branch") or "").strip() or None,
        started_at=started_at,
    )


def _drain_remote_launch(row: LiveArchiveOutbox, archive_db: Session) -> None:
    payload = json.loads(row.payload_json or "{}")
    launch = _restore_jsonable(payload.get("launch") or {})
    _materialize_remote_launch(archive_db, launch, outcome=None)


def _drain_console_session_create(row: LiveArchiveOutbox, archive_db: Session) -> None:
    payload = json.loads(row.payload_json or "{}")
    data = _restore_jsonable(payload.get("session") or {})
    session_id = UUID(str(data["session_id"]))
    thread_id = UUID(str(data["thread_id"]))
    provider = str(data["provider"]).strip().lower()
    device_id = str(data["device_id"]).strip()
    cwd = str(data["cwd"]).strip()
    started_at = normalize_utc(data.get("started_at")) or datetime.now(timezone.utc)
    session = archive_db.get(AgentSession, session_id)
    if session is None:
        session = AgentSession(
            id=session_id,
            provider=provider,
            environment="development",
            project=str(data.get("project") or "").strip() or "console",
            device_id=device_id,
            device_name=str(data.get("machine_name") or "").strip() or device_id,
            cwd=cwd,
            git_repo=str(data.get("git_repo") or "").strip() or None,
            git_branch=str(data.get("git_branch") or "").strip() or None,
            started_at=started_at,
            ended_at=None,
            user_messages=0,
            assistant_messages=0,
            tool_calls=0,
            loop_mode=SessionLoopMode.ASSIST.value,
            launch_actor="user",
            launch_surface=str(data.get("launch_surface") or "console"),
            origin_kind="console",
        )
        archive_db.add(session)
        archive_db.flush()
    thread = archive_db.get(SessionThread, thread_id)
    if thread is None:
        thread = SessionThread(
            id=thread_id,
            session_id=session.id,
            provider=provider,
            branch_kind="root",
            is_primary=1,
            created_at=started_at,
            updated_at=started_at,
        )
        archive_db.add(thread)
        archive_db.flush()
    session.primary_thread_id = thread.id
    session.device_id = device_id
    session.cwd = cwd
    set_thread_execution_target(
        thread,
        device_id=device_id,
        cwd=cwd,
        provider_config=dict(data.get("provider_config") or {}),
    )


def _drain_remote_launch_outcome(row: LiveArchiveOutbox, archive_db: Session) -> None:
    payload = json.loads(row.payload_json or "{}")
    launch = _restore_jsonable(payload.get("launch") or {})
    outcome = _restore_jsonable(payload.get("outcome") or {})
    _materialize_remote_launch(archive_db, launch, outcome=outcome)


def _materialize_remote_launch(
    db: Session,
    launch: dict[str, Any],
    *,
    outcome: dict[str, Any] | None,
) -> None:
    session_id = UUID(str(launch.get("session_id") or ""))
    provider = str(launch.get("provider") or "").strip().lower()
    device_id = str(launch.get("device_id") or "").strip()
    cwd = str(launch.get("cwd") or "").strip()
    project = str(launch.get("project") or "").strip() or "managed-local"
    execution_lifetime = str(launch.get("execution_lifetime") or "live_control").strip() or "live_control"
    command_id = str(launch.get("command_id") or "").strip() or None
    started_at = normalize_utc(launch.get("started_at")) or datetime.now(timezone.utc)
    expires_at = normalize_utc(launch.get("expires_at"))

    session = db.get(AgentSession, session_id)
    if session is None:
        session = AgentSession(
            id=session_id,
            provider=provider,
            environment="development",
            project=project,
            device_id=device_id,
            device_name=str(launch.get("machine_id") or "").strip() or device_id,
            cwd=cwd,
            git_repo=str(launch.get("git_repo") or "").strip() or None,
            git_branch=str(launch.get("git_branch") or "").strip() or None,
            started_at=started_at,
            ended_at=None,
            user_messages=0,
            assistant_messages=0,
            tool_calls=0,
            loop_mode=SessionLoopMode.ASSIST.value,
        )
        db.add(session)
        db.flush()
    else:
        session.device_id = device_id or session.device_id
        session.device_name = str(launch.get("machine_id") or "").strip() or device_id or session.device_name
        session.cwd = cwd or session.cwd
        session.git_repo = str(launch.get("git_repo") or "").strip() or session.git_repo
        session.git_branch = str(launch.get("git_branch") or "").strip() or session.git_branch
        session.project = project or session.project

    requested_thread_id_text = str(launch.get("primary_thread_id") or "").strip()
    requested_thread_id = UUID(requested_thread_id_text) if requested_thread_id_text else None
    thread = db.query(SessionThread).filter(SessionThread.session_id == session.id, SessionThread.is_primary == 1).one_or_none()
    if thread is None and requested_thread_id is not None:
        thread = SessionThread(
            id=requested_thread_id,
            session_id=session.id,
            provider=provider,
            branch_kind="root",
            is_primary=1,
            created_at=started_at,
            updated_at=started_at,
        )
        db.add(thread)
        db.flush()
        session.primary_thread_id = thread.id
    elif thread is None:
        thread = ensure_primary_thread(db, session)
    resume_payload = launch.get("resume") if isinstance(launch.get("resume"), dict) else {}
    provider_thread_id = str(resume_payload.get("thread_id") or launch.get("provider_thread_id") or "").strip() or None
    if provider_thread_id and not is_synthetic_provider_session_id(session, provider_thread_id):
        record_thread_alias(
            db,
            thread=thread,
            provider=provider,
            alias_kind="provider_session_id",
            alias_value=provider_thread_id,
        )
    thread_path = str(resume_payload.get("thread_path") or launch.get("thread_path") or "").strip() or None
    if thread_path:
        record_thread_alias(
            db,
            thread=thread,
            provider=provider,
            alias_kind="source_path",
            alias_value=thread_path,
        )
    launch_origin = str(launch.get("launch_origin") or "longhouse_spawned").strip() or "longhouse_spawned"
    is_continue = str(launch.get("mode") or "").strip() == "continue" or launch_origin == "longhouse_continued"
    attempt = _remote_launch_attempt_for_command(db, command_id=command_id)
    if attempt is None:
        attempt = SessionLaunchAttempt(
            session_id=session.id,
            thread_id=thread.id,
            provider=provider,
            host_id=device_id,
            owner_id=int(launch.get("owner_id") or 0) or None,
            execution_lifetime=execution_lifetime,
            client_request_id=str(launch.get("client_request_id") or "").strip() or None,
            command_id=command_id,
            state="pending",
            expires_at=expires_at,
        )
        db.add(attempt)
        db.flush()
    else:
        attempt.session_id = session.id
        attempt.thread_id = attempt.thread_id or thread.id
        attempt.provider = provider
        attempt.host_id = device_id
        attempt.owner_id = int(launch.get("owner_id") or 0) or attempt.owner_id
        attempt.execution_lifetime = execution_lifetime
        attempt.client_request_id = str(launch.get("client_request_id") or "").strip() or attempt.client_request_id
        attempt.command_id = command_id or attempt.command_id

    if execution_lifetime == "one_shot":
        if is_continue:
            _release_open_runs_for_thread(db, thread=thread, now=datetime.now(timezone.utc))
        run = _ensure_one_shot_remote_run(db, launch=launch, thread_id=thread.id, provider=provider, device_id=device_id, cwd=cwd)
        attempt.run_id = run.id

    if not outcome:
        if attempt.state not in {"dispatched", "adopted", "failed", "abandoned"}:
            attempt.state = "pending"
            attempt.expires_at = expires_at
        return

    state = str(outcome.get("state") or "").strip()
    if state == "dispatched":
        if attempt.state not in {"adopted", "failed", "abandoned"}:
            attempt.state = "dispatched"
            attempt.error_message = str(outcome.get("error_message") or "").strip() or None
            attempt.expires_at = expires_at
        return

    if state == "adopted":
        if execution_lifetime == "one_shot":
            _attach_one_shot_remote_launch(
                db,
                session=session,
                attempt=attempt,
                launch=launch,
                outcome=outcome,
                cwd=cwd,
            )
        else:
            _attach_live_remote_launch(
                db,
                session=session,
                attempt=attempt,
                launch=launch,
                outcome=outcome,
                cwd=cwd,
            )
        return

    if state == "failed":
        session.ended_at = datetime.now(timezone.utc)
        if execution_lifetime == "one_shot":
            _mark_one_shot_remote_run_failed(db, attempt=attempt, error_code=str(outcome.get("error_code") or "provider_launch_failed"))
        attempt.state = "failed"
        attempt.error_code = str(outcome.get("error_code") or "provider_launch_failed")
        attempt.error_message = str(outcome.get("error_message") or "unknown error")
        attempt.expires_at = None


def _remote_launch_attempt_for_command(db: Session, *, command_id: str | None) -> SessionLaunchAttempt | None:
    if not command_id:
        return None
    return db.query(SessionLaunchAttempt).filter(SessionLaunchAttempt.command_id == command_id).first()


def _ensure_one_shot_remote_run(
    db: Session,
    *,
    launch: dict[str, Any],
    thread_id: UUID,
    provider: str,
    device_id: str,
    cwd: str,
) -> SessionRun:
    run_id = UUID(str(launch.get("run_id") or ""))
    run = db.get(SessionRun, run_id)
    if run is not None:
        return run
    run = SessionRun(
        id=run_id,
        thread_id=thread_id,
        provider=provider,
        host_id=device_id,
        cwd=cwd,
        launch_origin=str(launch.get("launch_origin") or "longhouse_spawned").strip() or "longhouse_spawned",
        started_at=normalize_utc(launch.get("started_at")) or datetime.now(timezone.utc),
    )
    db.add(run)
    db.flush()
    return run


def _attach_live_remote_launch(
    db: Session,
    *,
    session: AgentSession,
    attempt: SessionLaunchAttempt,
    launch: dict[str, Any],
    outcome: dict[str, Any],
    cwd: str,
) -> None:
    thread = ensure_primary_thread(db, session)
    launch_origin = str(launch.get("launch_origin") or "longhouse_spawned").strip() or "longhouse_spawned"
    is_continue = str(launch.get("mode") or "").strip() == "continue" or launch_origin == "longhouse_continued"
    provider_thread_id = str(outcome.get("provider_thread_id") or "").strip() or None
    if provider_thread_id and not is_synthetic_provider_session_id(session, provider_thread_id):
        record_thread_alias(
            db,
            thread=thread,
            provider=session.provider,
            alias_kind="provider_session_id",
            alias_value=provider_thread_id,
        )
    thread_path = str(outcome.get("thread_path") or "").strip() or None
    if thread_path:
        record_thread_alias(
            db,
            thread=thread,
            provider=session.provider,
            alias_kind="source_path",
            alias_value=thread_path,
        )
    if is_continue:
        run_id_text = str(launch.get("run_id") or "").strip()
        run_id = UUID(run_id_text) if run_id_text else None
        run = db.get(SessionRun, run_id) if run_id is not None else None
        if run is None:
            _release_open_runs_for_thread(db, thread=thread, now=datetime.now(timezone.utc))
            run = SessionRun(
                id=run_id,
                thread_id=thread.id,
                provider=session.provider,
                host_id=session.device_id,
                cwd=cwd or session.cwd,
                launch_origin=launch_origin,
                started_at=normalize_utc(launch.get("started_at")) or datetime.now(timezone.utc),
            )
            db.add(run)
            db.flush()
    else:
        run_id_text = str(launch.get("run_id") or "").strip()
        run_id = UUID(run_id_text) if run_id_text else None
        run = db.get(SessionRun, run_id) if run_id is not None else None
        if run is None and run_id is not None:
            run = SessionRun(
                id=run_id,
                thread_id=thread.id,
                provider=session.provider,
                host_id=session.device_id,
                cwd=cwd or session.cwd,
                launch_origin=launch_origin,
                started_at=normalize_utc(launch.get("started_at")) or datetime.now(timezone.utc),
            )
            db.add(run)
            db.flush()
        if run is None:
            run = ensure_open_run_for_session(
                db,
                session,
                launch_origin=launch_origin,
                host_id=session.device_id,
            )
        run.cwd = cwd or run.cwd
    contract = require_contract_for_provider(session.provider)
    caps = contract.connection_capabilities
    requires_readiness_proof = managed_provider_requires_readiness_proof(session.provider)
    conn = upsert_connection_for_run(
        db,
        run=run,
        control_plane=contract.control_plane,
        acquisition_kind="spawned_control",
        state="detached" if requires_readiness_proof else "attached",
        external_name=str(outcome.get("external_name") or launch.get("machine_id") or launch.get("device_id") or "").strip() or None,
        can_send_input=0 if requires_readiness_proof else caps["can_send_input"],
        can_interrupt=caps["can_interrupt"],
        can_terminate=caps["can_terminate"],
        can_tail_output=caps["can_tail_output"],
        can_resume=caps["can_resume"],
    )
    now = datetime.now(timezone.utc)
    conn.last_health_at = None if requires_readiness_proof else now
    session.ended_at = None
    attempt.state = "adopted"
    attempt.run_id = run.id
    attempt.expires_at = None
    attempt.error_code = None
    attempt.error_message = None


def _release_open_runs_for_thread(db: Session, *, thread, now: datetime) -> None:
    open_runs = db.query(SessionRun).filter(SessionRun.thread_id == thread.id, SessionRun.ended_at.is_(None)).all()
    if not open_runs:
        return
    open_run_ids = [run.id for run in open_runs]
    for run in open_runs:
        run.ended_at = now
    for conn in (
        db.query(SessionConnection)
        .filter(SessionConnection.run_id.in_(open_run_ids))
        .filter(SessionConnection.state.in_(("attached", "degraded")))
        .all()
    ):
        conn.state = "released"
        conn.released_at = now
        conn.last_health_at = now
        conn.can_send_input = 0
        conn.can_interrupt = 0
        conn.can_terminate = 0
        conn.can_tail_output = 0
        conn.can_resume = 0


def _attach_one_shot_remote_launch(
    db: Session,
    *,
    session: AgentSession,
    attempt: SessionLaunchAttempt,
    launch: dict[str, Any],
    outcome: dict[str, Any],
    cwd: str,
) -> None:
    thread = ensure_primary_thread(db, session)
    run = _ensure_one_shot_remote_run(
        db,
        launch=launch,
        thread_id=thread.id,
        provider=session.provider,
        device_id=str(launch.get("device_id") or ""),
        cwd=cwd,
    )
    pid = outcome.get("pid")
    if pid is not None:
        run.pid = int(pid)
    argv = outcome.get("argv")
    if isinstance(argv, list):
        run.argv_redacted_json = [str(item) for item in argv]
    run_ended_at = run.ended_at
    state = "ended" if run_ended_at is not None else "attached"
    control_plane = ONE_SHOT_CONTROL_PLANE_BY_PROVIDER.get(session.provider, f"{session.provider}_exec")
    conn = upsert_connection_for_run(
        db,
        run=run,
        control_plane=control_plane,
        acquisition_kind="spawned_control",
        state=state,
        external_name=str(outcome.get("external_name") or launch.get("machine_id") or launch.get("device_id") or "").strip() or None,
        device_id=str(launch.get("device_id") or "").strip() or None,
        can_send_input=0,
        can_interrupt=0,
        can_terminate=0,
        can_tail_output=0,
        can_resume=0,
    )
    now = datetime.now(timezone.utc)
    if run_ended_at is not None:
        conn.released_at = run_ended_at
        conn.last_health_at = run_ended_at
    else:
        conn.last_health_at = now
    session.ended_at = None
    attempt.state = "adopted"
    attempt.run_id = run.id
    attempt.expires_at = None
    attempt.error_code = None
    attempt.error_message = None


def _mark_one_shot_remote_run_failed(db: Session, *, attempt: SessionLaunchAttempt, error_code: str) -> None:
    if attempt.run_id is None:
        return
    run = db.get(SessionRun, attempt.run_id)
    if run is None:
        return
    now = datetime.now(timezone.utc)
    run_ended_at = run.ended_at
    if run_ended_at is None:
        run.ended_at = now
        run.exit_status = (error_code or "provider_launch_failed")[:64]
        connection_released_at = now
    else:
        connection_released_at = run_ended_at
        if not run.exit_status:
            run.exit_status = (error_code or "provider_launch_failed")[:64]
    for conn in (
        db.query(SessionConnection)
        .filter(SessionConnection.run_id == run.id)
        .filter(SessionConnection.state.in_(("attached", "degraded", "detached")))
        .all()
    ):
        conn.state = "ended"
        conn.released_at = connection_released_at
        conn.last_health_at = connection_released_at
        conn.can_send_input = 0
        conn.can_interrupt = 0
        conn.can_terminate = 0
        conn.can_tail_output = 0
        conn.can_resume = 0


def _mark_live_side_effects_after_archive_commit(row: LiveArchiveOutbox, live_db: Session) -> None:
    if row.kind not in {MANAGED_LOCAL_LAUNCH_KIND, REMOTE_LAUNCH_OUTCOME_KIND}:
        return
    if row.kind == REMOTE_LAUNCH_OUTCOME_KIND:
        payload = json.loads(row.payload_json or "{}")
        launch = _restore_jsonable(payload.get("launch") or {})
        outcome = _restore_jsonable(payload.get("outcome") or {})
        state = str(outcome.get("state") or "").strip()
        if state in {"adopted", "failed", "dispatched"}:
            session_id = UUID(str(launch.get("session_id") or ""))
            current = live_db.get(LiveLaunchReadiness, str(session_id))
            if current is not None:
                current_state = str(current.state or "").strip()
                current_rank = _remote_launch_state_rank(current_state)
                next_rank = _remote_launch_state_rank(state)
                if current_rank > next_rank or (current_rank == next_rank and current_state != state):
                    return
            update_live_launch_readiness_state(
                live_db,
                session_id=session_id,
                state=state,
                error_code=str(outcome.get("error_code") or "").strip() or None,
                error_message=str(outcome.get("error_message") or "").strip() or None,
                clear_expires=state in {"adopted", "failed"},
                now=datetime.now(timezone.utc),
            )
        return
    payload = json.loads(row.payload_json or "{}")
    launch = _restore_jsonable(payload.get("launch") or {})
    plan = _restore_managed_local_launch_plan(launch.get("plan") or {})
    current = live_db.get(LiveLaunchReadiness, str(plan.session_id))
    if current is not None and str(current.state or "").strip() in {"adopted", "failed", "abandoned"}:
        return
    command_id = f"managed-local-{plan.session_id}"
    catalog_attempt = live_db.query(LiveSessionLaunchAttempt).filter(LiveSessionLaunchAttempt.command_id == command_id).one_or_none()
    if catalog_attempt is not None:
        update_live_launch_catalog_outcome(
            live_db,
            session_id=plan.session_id,
            command_id=command_id,
            state="adopted",
        )
    update_live_launch_readiness_state(
        live_db,
        session_id=plan.session_id,
        state="adopted",
        clear_expires=True,
        now=datetime.now(timezone.utc),
    )


def _remote_launch_state_rank(state: str) -> int:
    return {
        "pending": 0,
        "dispatched": 1,
        "adopted": 2,
        "failed": 2,
        "abandoned": 2,
    }.get(str(state or "").strip(), 0)


def _restore_managed_local_launch_plan(plan_payload: dict[str, Any]) -> ManagedLocalLaunchPlan:
    session_id = UUID(str(plan_payload.get("session_id") or ""))
    return ManagedLocalLaunchPlan(
        session_id=session_id,
        provider=str(plan_payload.get("provider") or ""),
        provider_session_id=str(plan_payload.get("provider_session_id") or "").strip() or None,
        source_name=str(plan_payload.get("source_name") or ""),
        source_runner_id=plan_payload.get("source_runner_id"),
        cwd=str(plan_payload.get("cwd") or ""),
        project=str(plan_payload.get("project") or ""),
        display_name=str(plan_payload.get("display_name") or ""),
        managed_session_name=str(plan_payload.get("managed_session_name") or ""),
        loop_mode=str(plan_payload.get("loop_mode") or "assist"),
        permission_mode=str(plan_payload.get("permission_mode") or "bypass"),
        launch_actor=str(plan_payload.get("launch_actor") or "").strip() or None,
        launch_surface=str(plan_payload.get("launch_surface") or "").strip() or None,
        managed_transport=str(plan_payload.get("managed_transport") or ""),
        attach_command=str(plan_payload.get("attach_command") or ""),
    )


def _project_session_input_receipt(row: LiveArchiveOutbox, archive_db: Session) -> dict[str, Any]:
    payload = json.loads(row.payload_json or "{}")
    receipt = _restore_jsonable(payload.get("receipt") or {})
    receipt_id = str(receipt.get("id") or "").strip()
    session_id = str(receipt.get("session_id") or "").strip()
    owner_id = int(receipt.get("owner_id") or 0)
    text = str(receipt.get("text") or "")
    intent = str(receipt.get("intent") or "auto").strip() or "auto"
    client_request_id = str(receipt.get("client_request_id") or "").strip() or None
    delivery_request_id = str(receipt.get("delivery_request_id") or "").strip()
    if not receipt_id or not session_id or not owner_id or not delivery_request_id:
        raise ValueError("session input receipt outbox payload is missing identity fields")

    archive_input_id = project_session_input_receipt_to_archive(
        archive_db,
        source_session_id=session_id,
        owner_id=owner_id,
        text=text,
        intent=intent,
        client_request_id=client_request_id,
        delivery_request_id=delivery_request_id,
    )
    return {
        "receipt_id": receipt_id,
        "archive_session_input_id": archive_input_id,
        "delivery_request_id": delivery_request_id,
    }


def _ack_session_input_receipt(row: LiveArchiveOutbox, live_db: Session, effects: dict[str, Any]) -> None:
    del row
    receipt_id = str(effects.get("receipt_id") or "").strip()
    archive_input_id = int(effects.get("archive_session_input_id") or 0)
    delivery_request_id = str(effects.get("delivery_request_id") or "").strip()
    if not receipt_id or not archive_input_id or not delivery_request_id:
        raise ValueError("session input receipt acknowledgement is missing projection effects")
    receipt = live_db.get(LiveSessionInputReceipt, receipt_id)
    if receipt is None:
        return
    receipt.archive_session_input_id = archive_input_id
    receipt.delivery_request_id = receipt.delivery_request_id or delivery_request_id
    if receipt.status == INPUT_STATUS_DELIVERING:
        receipt.status = INPUT_STATUS_DELIVERED
    receipt.updated_at = datetime.now(timezone.utc)


def project_session_input_receipt_to_archive(
    db: Session,
    *,
    source_session_id: UUID | str,
    owner_id: int,
    text: str,
    intent: str,
    client_request_id: str | None,
    delivery_request_id: str,
) -> int:
    """Materialize live input receipt provenance in archive SQLite idempotently."""

    existing_query = db.query(SessionInput).filter(
        SessionInput.session_id == source_session_id,
        SessionInput.owner_id == owner_id,
    )
    if client_request_id:
        existing_query = existing_query.filter(SessionInput.client_request_id == client_request_id)
    else:
        existing_query = existing_query.filter(SessionInput.delivery_request_id == delivery_request_id)
    existing = existing_query.order_by(SessionInput.id.asc()).first()
    if existing is not None:
        input_id = int(existing.id)
        if existing.status == INPUT_STATUS_DELIVERING:
            now = datetime.now(timezone.utc)
            existing.status = INPUT_STATUS_DELIVERED
            existing.delivered_at = now
            existing.updated_at = now
            existing.last_error = None
        _link_session_turn(db, source_session_id=source_session_id, delivery_request_id=delivery_request_id, input_id=input_id)
        return input_id

    if intent not in VALID_INTENTS:
        raise ValueError(f"invalid intent: {intent}")
    from zerg.services.agents.kernel_writes import ensure_thread_id_for_session

    now = datetime.now(timezone.utc)
    row = SessionInput(
        session_id=source_session_id,
        thread_id=ensure_thread_id_for_session(db, source_session_id),
        body=text,
        owner_id=owner_id,
        intent=intent,
        status=INPUT_STATUS_DELIVERED,
        client_request_id=client_request_id,
        delivery_request_id=delivery_request_id,
        delivered_at=now,
    )
    db.add(row)
    db.flush()
    input_id = int(row.id)
    _link_session_turn(db, source_session_id=source_session_id, delivery_request_id=delivery_request_id, input_id=input_id)
    return input_id


def _link_session_turn(
    db: Session,
    *,
    source_session_id: UUID | str,
    delivery_request_id: str,
    input_id: int,
) -> None:
    db.query(SessionTurn).filter(
        SessionTurn.session_id == source_session_id,
        SessionTurn.request_id == delivery_request_id,
    ).update({"session_input_id": input_id}, synchronize_session=False)


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        normalized = normalize_utc(value) or value
        return {"__longhouse_datetime__": normalized.isoformat()}
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _restore_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        marker = value.get("__longhouse_datetime__")
        if marker is not None:
            return datetime.fromisoformat(str(marker))
        return {str(key): _restore_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_restore_jsonable(item) for item in value]
    return value
