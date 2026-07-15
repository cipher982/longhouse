"""Provider-neutral durable Console turn creation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from uuid import UUID
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.models.agents import SessionInput
from zerg.models.agents import SessionRun
from zerg.models.agents import SessionThread
from zerg.models.agents import SessionThreadAlias
from zerg.models.agents import SessionTurn
from zerg.services.agents.kernel_writes import record_run
from zerg.services.agents.session_graph_writes import ensure_primary_thread
from zerg.services.session_inputs import INPUT_INTENT_AUTO
from zerg.services.session_inputs import INPUT_STATUS_DELIVERED
from zerg.services.session_inputs import INPUT_STATUS_DELIVERING
from zerg.services.session_inputs import INPUT_STATUS_FAILED
from zerg.services.session_inputs import INPUT_STATUS_QUEUED
from zerg.services.session_inputs import create_session_input_row
from zerg.services.session_turns import SESSION_TURN_SOURCE_CONSOLE
from zerg.services.session_turns import SESSION_TURN_STATE_ACTIVE
from zerg.services.session_turns import SESSION_TURN_STATE_CANCELLED
from zerg.services.session_turns import SESSION_TURN_STATE_COMPLETED
from zerg.services.session_turns import SESSION_TURN_STATE_DRAINING
from zerg.services.session_turns import SESSION_TURN_STATE_FAILED
from zerg.services.session_turns import SESSION_TURN_STATE_QUEUED
from zerg.services.session_turns import SESSION_TURN_STATE_STARTING
from zerg.services.session_turns import create_session_turn

CONSOLE_TURN_START_COMMAND = "session.turn.start"

CONSOLE_EXECUTION_OWNER_STATES = frozenset(
    {
        SESSION_TURN_STATE_STARTING,
        SESSION_TURN_STATE_ACTIVE,
        SESSION_TURN_STATE_DRAINING,
    }
)


class ConsoleTurnUnavailable(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ConsoleTurnConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class EnqueuedConsoleTurn:
    input_id: int
    turn_id: int
    state: str
    created: bool


@dataclass(frozen=True)
class ClaimedConsoleTurn:
    input_id: int
    turn_id: int
    run_id: UUID
    session_id: UUID
    thread_id: UUID
    provider: str
    device_id: str
    cwd: str
    message: str
    provider_config: dict[str, object]
    resume_provider_thread_id: str | None


@dataclass(frozen=True)
class ConsoleTurnDispatch:
    turn_id: int | None
    run_id: UUID | None
    state: str
    error: str | None = None


@dataclass(frozen=True)
class CatalogConsoleTurn:
    turn_id: UUID
    run_id: UUID | None
    state: str
    created: bool
    error: str | None = None


def enqueue_console_turn(
    db: Session,
    *,
    session: AgentSession,
    owner_id: int | None,
    message: str,
    client_request_id: str,
) -> EnqueuedConsoleTurn:
    """Atomically create or replay one durable input + queued turn."""

    normalized_message = str(message or "").strip()
    normalized_request_id = str(client_request_id or "").strip()
    if not normalized_message:
        raise ValueError("message is required")
    if not normalized_request_id:
        raise ValueError("client_request_id is required")

    thread = ensure_primary_thread(db, session)
    if not str(thread.device_id or "").strip() or not str(thread.cwd or "").strip():
        raise ConsoleTurnUnavailable(
            "execution_target_missing",
            "Session thread has no Console execution target",
        )

    existing = _existing_turn(
        db,
        session_id=session.id,
        owner_id=owner_id,
        client_request_id=normalized_request_id,
        expected_message=normalized_message,
    )
    if existing is not None:
        return existing

    try:
        input_row = create_session_input_row(
            db,
            session_id=session.id,
            text=normalized_message,
            owner_id=owner_id,
            intent=INPUT_INTENT_AUTO,
            status=INPUT_STATUS_QUEUED,
            client_request_id=normalized_request_id,
        )
        turn = create_session_turn(
            db,
            session_id=session.id,
            request_id=normalized_request_id,
            expected_user_text=normalized_message,
            session_input_id=int(input_row.id),
            source_kind=SESSION_TURN_SOURCE_CONSOLE,
            initial_state=SESSION_TURN_STATE_QUEUED,
        )
        db.commit()
        return EnqueuedConsoleTurn(
            input_id=int(input_row.id),
            turn_id=int(turn.id),
            state=str(turn.state),
            created=True,
        )
    except IntegrityError:
        db.rollback()
        existing = _existing_turn(
            db,
            session_id=session.id,
            owner_id=owner_id,
            client_request_id=normalized_request_id,
            expected_message=normalized_message,
        )
        if existing is None:
            raise
        return existing


def claim_next_console_turn(db: Session, *, thread_id: UUID) -> ClaimedConsoleTurn | None:
    """Claim the oldest queued turn and bind its single provider invocation."""

    owner = (
        db.query(SessionTurn.id)
        .filter(
            SessionTurn.thread_id == thread_id,
            SessionTurn.source_kind == SESSION_TURN_SOURCE_CONSOLE,
            SessionTurn.state.in_(tuple(CONSOLE_EXECUTION_OWNER_STATES)),
        )
        .first()
    )
    if owner is not None:
        return None

    turn = (
        db.query(SessionTurn)
        .filter(
            SessionTurn.thread_id == thread_id,
            SessionTurn.source_kind == SESSION_TURN_SOURCE_CONSOLE,
            SessionTurn.state == SESSION_TURN_STATE_QUEUED,
        )
        .order_by(SessionTurn.user_submitted_at.asc(), SessionTurn.created_at.asc(), SessionTurn.id.asc())
        .first()
    )
    if turn is None:
        return None

    thread = db.get(SessionThread, thread_id)
    input_row = db.get(SessionInput, turn.session_input_id)
    if thread is None or input_row is None:
        raise ConsoleTurnConflict("queued turn is missing its thread or input")
    device_id = str(thread.device_id or "").strip()
    cwd = str(thread.cwd or "").strip()
    if not device_id or not cwd:
        raise ConsoleTurnUnavailable("execution_target_missing", "Session thread has no Console execution target")

    try:
        run = record_run(
            db,
            thread=thread,
            provider=thread.provider,
            host_id=device_id,
            cwd=cwd,
            run_id=uuid4(),
        )
        turn.run_id = run.id
        turn.state = SESSION_TURN_STATE_STARTING
        input_row.status = INPUT_STATUS_DELIVERING
        input_row.delivery_request_id = str(run.id)
        db.commit()
    except IntegrityError:
        db.rollback()
        return None

    return ClaimedConsoleTurn(
        input_id=int(input_row.id),
        turn_id=int(turn.id),
        run_id=UUID(str(run.id)),
        session_id=UUID(str(turn.session_id)),
        thread_id=UUID(str(thread.id)),
        provider=str(thread.provider),
        device_id=device_id,
        cwd=cwd,
        message=str(input_row.body),
        provider_config=dict(thread.provider_config_json or {}),
        resume_provider_thread_id=_provider_resume_identity(db, thread),
    )


def mark_console_turn_active(db: Session, *, turn_id: int) -> None:
    turn, input_row, _run = _load_turn_lifecycle(db, turn_id)
    if turn.state != SESSION_TURN_STATE_STARTING:
        raise ConsoleTurnConflict(f"turn {turn_id} cannot become active from {turn.state}")
    now = datetime.now(timezone.utc)
    turn.state = SESSION_TURN_STATE_ACTIVE
    turn.send_accepted_at = turn.send_accepted_at or now
    turn.active_phase_observed_at = turn.active_phase_observed_at or now
    input_row.status = INPUT_STATUS_DELIVERED
    input_row.delivered_at = input_row.delivered_at or now
    db.commit()


def begin_console_turn_drain(db: Session, *, turn_id: int, terminal_phase: str | None = None) -> None:
    turn, _input_row, _run = _load_turn_lifecycle(db, turn_id)
    if turn.state not in {SESSION_TURN_STATE_STARTING, SESSION_TURN_STATE_ACTIVE}:
        raise ConsoleTurnConflict(f"turn {turn_id} cannot drain from {turn.state}")
    turn.state = SESSION_TURN_STATE_DRAINING
    turn.terminal_phase = terminal_phase
    turn.terminal_at = turn.terminal_at or datetime.now(timezone.utc)
    db.commit()


def settle_console_turn(
    db: Session,
    *,
    turn_id: int,
    outcome: str,
    exit_status: str | None = None,
) -> None:
    terminal_states = {
        SESSION_TURN_STATE_COMPLETED,
        SESSION_TURN_STATE_FAILED,
        SESSION_TURN_STATE_CANCELLED,
    }
    if outcome not in terminal_states:
        raise ValueError(f"invalid Console turn outcome: {outcome}")
    turn, input_row, run = _load_turn_lifecycle(db, turn_id)
    if turn.state != SESSION_TURN_STATE_DRAINING:
        raise ConsoleTurnConflict(f"turn {turn_id} cannot settle from {turn.state}")
    now = datetime.now(timezone.utc)
    turn.state = outcome
    turn.durable_at = now
    run.ended_at = now
    run.exit_status = exit_status or outcome
    if outcome != SESSION_TURN_STATE_COMPLETED:
        input_row.status = INPUT_STATUS_FAILED
        input_row.last_error = outcome
    db.commit()


async def dispatch_next_console_turn(
    db: Session,
    *,
    owner_id: int,
    thread_id: UUID,
    registry=None,
) -> ConsoleTurnDispatch:
    """Start the oldest queued turn through the selected machine adapter."""

    from zerg.services.machine_control_channel import get_machine_control_channel_registry

    claimed = claim_next_console_turn(db, thread_id=thread_id)
    if claimed is None:
        return ConsoleTurnDispatch(turn_id=None, run_id=None, state="idle")

    control = registry or get_machine_control_channel_registry()
    capability = f"{claimed.provider}.turn_start"
    if not control.supports(
        owner_id=owner_id,
        device_id=claimed.device_id,
        capability=capability,
    ):
        _fail_starting_console_turn(
            db,
            turn_id=claimed.turn_id,
            error=f"Machine Agent does not advertise {capability}",
        )
        return ConsoleTurnDispatch(
            turn_id=claimed.turn_id,
            run_id=claimed.run_id,
            state=SESSION_TURN_STATE_FAILED,
            error=f"Machine Agent does not advertise {capability}",
        )

    payload: dict[str, object] = {
        "run_id": str(claimed.run_id),
        "thread_id": str(claimed.thread_id),
        "provider": claimed.provider,
        "cwd": claimed.cwd,
        "message": claimed.message,
        "launch_actor": "user",
        "launch_surface": "console",
        **claimed.provider_config,
    }
    if claimed.resume_provider_thread_id:
        payload["resume_provider_thread_id"] = claimed.resume_provider_thread_id

    response = await control.send_command(
        owner_id=owner_id,
        device_id=claimed.device_id,
        session_id=str(claimed.session_id),
        command_type=CONSOLE_TURN_START_COMMAND,
        payload=payload,
        command_id=str(claimed.run_id),
        timeout_secs=15,
    )
    message = dict(response.message or {})
    if not response.transport_ok or message.get("ok") is not True:
        detail = message.get("error") if isinstance(message.get("error"), dict) else {}
        error = str(detail.get("message") or response.error or "Console turn dispatch failed")
        _fail_starting_console_turn(db, turn_id=claimed.turn_id, error=error)
        return ConsoleTurnDispatch(
            turn_id=claimed.turn_id,
            run_id=claimed.run_id,
            state=SESSION_TURN_STATE_FAILED,
            error=error,
        )

    mark_console_turn_active(db, turn_id=claimed.turn_id)
    return ConsoleTurnDispatch(
        turn_id=claimed.turn_id,
        run_id=claimed.run_id,
        state=SESSION_TURN_STATE_ACTIVE,
    )


async def enqueue_catalog_console_turn(
    *,
    owner_id: int,
    session_id: UUID,
    message: str,
    client_request_id: str,
    registry=None,
) -> CatalogConsoleTurn:
    """Live-catalog equivalent of enqueue + claim + machine dispatch."""

    from zerg.services.catalogd_supervisor import get_catalogd_client
    from zerg.services.machine_control_channel import get_machine_control_channel_registry

    client = get_catalogd_client()
    if client is None:
        raise ConsoleTurnUnavailable("catalog_unavailable", "Console turn catalog is unavailable")
    result = await client.call(
        "session.console.turn.enqueue.v2",
        {
            "turn": {
                "session_id": str(session_id),
                "owner_id": owner_id,
                "message": message,
                "client_request_id": client_request_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )
    if result.get("found") is not True:
        raise ConsoleTurnUnavailable("session_not_found", "Console session was not found")
    if result.get("idempotency_conflict") is True:
        raise ConsoleTurnConflict("client_request_id was reused with different text")
    if result.get("unavailable"):
        raise ConsoleTurnUnavailable(str(result["unavailable"]), "Console execution target is unavailable")
    turn = dict(result.get("turn") or {})
    turn_id = UUID(str(turn["turn_id"]))
    run_id = UUID(str(turn["run_id"])) if turn.get("run_id") else None
    state = str(turn.get("state") or "queued")
    if state != SESSION_TURN_STATE_STARTING or run_id is None:
        return CatalogConsoleTurn(turn_id=turn_id, run_id=run_id, state=state, created=bool(result.get("created")))

    control = registry or get_machine_control_channel_registry()
    provider = str(turn["provider"])
    device_id = str(turn["device_id"])
    capability = f"{provider}.turn_start"
    error = None
    if not control.supports(owner_id=owner_id, device_id=device_id, capability=capability):
        error = f"Machine Agent does not advertise {capability}"
    else:
        payload = {
            "run_id": str(run_id),
            "thread_id": str(turn["thread_id"]),
            "provider": provider,
            "cwd": str(turn["cwd"]),
            "message": str(turn.get("message") or message),
            "launch_actor": "user",
            "launch_surface": "console",
            **dict(turn.get("provider_config") or {}),
        }
        if turn.get("resume_provider_thread_id"):
            payload["resume_provider_thread_id"] = turn["resume_provider_thread_id"]
        response = await control.send_command(
            owner_id=owner_id,
            device_id=device_id,
            session_id=str(session_id),
            command_type=CONSOLE_TURN_START_COMMAND,
            payload=payload,
            command_id=str(run_id),
            timeout_secs=15,
        )
        response_message = dict(response.message or {})
        if not response.transport_ok or response_message.get("ok") is not True:
            detail = response_message.get("error") if isinstance(response_message.get("error"), dict) else {}
            error = str(detail.get("message") or response.error or "Console turn dispatch failed")

    state = SESSION_TURN_STATE_FAILED if error else SESSION_TURN_STATE_ACTIVE
    update_result = await client.call(
        "session.console.turn.update.v2",
        {
            "turn": {
                "turn_id": str(turn_id),
                "run_id": str(run_id),
                "state": state,
                "error": error,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )
    next_turn = update_result.get("next_turn")
    if isinstance(next_turn, dict):
        await dispatch_catalog_claimed_turn(
            owner_id=owner_id,
            turn=next_turn,
            client=client,
            registry=control,
        )
    return CatalogConsoleTurn(
        turn_id=turn_id,
        run_id=run_id,
        state=state,
        created=bool(result.get("created")),
        error=error,
    )


async def dispatch_catalog_claimed_turn(
    *,
    owner_id: int,
    turn: dict[str, object],
    client=None,
    registry=None,
) -> CatalogConsoleTurn:
    """Dispatch a turn already claimed by catalogd, used for FIFO wakeups."""

    from zerg.services.catalogd_supervisor import get_catalogd_client
    from zerg.services.machine_control_channel import get_machine_control_channel_registry

    turn_id = UUID(str(turn["turn_id"]))
    run_id = UUID(str(turn["run_id"]))
    provider = str(turn["provider"])
    device_id = str(turn["device_id"])
    session_id = UUID(str(turn["session_id"]))
    control = registry or get_machine_control_channel_registry()
    catalog = client or get_catalogd_client()
    if catalog is None:
        raise ConsoleTurnUnavailable("catalog_unavailable", "Console turn catalog is unavailable")
    capability = f"{provider}.turn_start"
    error = None
    if not control.supports(owner_id=owner_id, device_id=device_id, capability=capability):
        error = f"Machine Agent does not advertise {capability}"
    else:
        payload = {
            "run_id": str(run_id),
            "thread_id": str(turn["thread_id"]),
            "provider": provider,
            "cwd": str(turn["cwd"]),
            "message": str(turn.get("message") or ""),
            "launch_actor": "user",
            "launch_surface": "console",
            **dict(turn.get("provider_config") or {}),
        }
        if turn.get("resume_provider_thread_id"):
            payload["resume_provider_thread_id"] = turn["resume_provider_thread_id"]
        response = await control.send_command(
            owner_id=owner_id,
            device_id=device_id,
            session_id=str(session_id),
            command_type=CONSOLE_TURN_START_COMMAND,
            payload=payload,
            command_id=str(run_id),
            timeout_secs=15,
        )
        message = dict(response.message or {})
        if not response.transport_ok or message.get("ok") is not True:
            detail = message.get("error") if isinstance(message.get("error"), dict) else {}
            error = str(detail.get("message") or response.error or "Console turn dispatch failed")
    state = SESSION_TURN_STATE_FAILED if error else SESSION_TURN_STATE_ACTIVE
    update_result = await catalog.call(
        "session.console.turn.update.v2",
        {
            "turn": {
                "turn_id": str(turn_id),
                "run_id": str(run_id),
                "state": state,
                "error": error,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )
    next_turn = update_result.get("next_turn")
    if isinstance(next_turn, dict):
        await dispatch_catalog_claimed_turn(
            owner_id=owner_id,
            turn=next_turn,
            client=catalog,
            registry=control,
        )
    return CatalogConsoleTurn(turn_id=turn_id, run_id=run_id, state=state, created=True, error=error)


def _fail_starting_console_turn(db: Session, *, turn_id: int, error: str) -> None:
    turn, input_row, run = _load_turn_lifecycle(db, turn_id)
    if turn.state != SESSION_TURN_STATE_STARTING:
        raise ConsoleTurnConflict(f"turn {turn_id} cannot fail from {turn.state}")
    now = datetime.now(timezone.utc)
    turn.state = SESSION_TURN_STATE_FAILED
    turn.terminal_at = now
    turn.durable_at = now
    input_row.status = INPUT_STATUS_FAILED
    input_row.last_error = error[:1000]
    run.ended_at = now
    run.exit_status = "dispatch_failed"
    db.commit()


def _load_turn_lifecycle(db: Session, turn_id: int) -> tuple[SessionTurn, SessionInput, SessionRun]:
    turn = db.get(SessionTurn, turn_id)
    if turn is None or turn.run_id is None or turn.session_input_id is None:
        raise ConsoleTurnConflict(f"turn {turn_id} has no invocation")
    input_row = db.get(SessionInput, turn.session_input_id)
    run = db.get(SessionRun, turn.run_id)
    if input_row is None or run is None:
        raise ConsoleTurnConflict(f"turn {turn_id} has incomplete invocation linkage")
    return turn, input_row, run


def _provider_resume_identity(db: Session, thread: SessionThread) -> str | None:
    row = (
        db.query(SessionThreadAlias.alias_value)
        .filter(
            SessionThreadAlias.thread_id == thread.id,
            SessionThreadAlias.provider == thread.provider,
            SessionThreadAlias.alias_kind == "provider_session_id",
        )
        .order_by(SessionThreadAlias.last_seen_at.desc(), SessionThreadAlias.id.desc())
        .first()
    )
    return str(row[0]).strip() if row and str(row[0]).strip() else None


def _existing_turn(
    db: Session,
    *,
    session_id: UUID,
    owner_id: int | None,
    client_request_id: str,
    expected_message: str,
) -> EnqueuedConsoleTurn | None:
    input_query = db.query(SessionInput).filter(
        SessionInput.session_id == session_id,
        SessionInput.client_request_id == client_request_id,
    )
    input_query = (
        input_query.filter(SessionInput.owner_id.is_(None)) if owner_id is None else input_query.filter(SessionInput.owner_id == owner_id)
    )
    input_row = input_query.one_or_none()
    if input_row is None:
        return None
    if str(input_row.body) != expected_message:
        raise ConsoleTurnConflict("client_request_id already belongs to different text")

    turn = (
        db.query(SessionTurn)
        .filter(
            SessionTurn.session_id == session_id,
            SessionTurn.request_id == client_request_id,
        )
        .one_or_none()
    )
    if turn is None or int(turn.session_input_id or 0) != int(input_row.id):
        raise ConsoleTurnConflict("client_request_id has incomplete turn linkage")
    return EnqueuedConsoleTurn(
        input_id=int(input_row.id),
        turn_id=int(turn.id),
        state=str(turn.state),
        created=False,
    )
