"""Provider-neutral durable Console turn creation."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from uuid import UUID
from uuid import uuid4

from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.services.session_turns import SESSION_TURN_STATE_ACTIVE
from zerg.services.session_turns import SESSION_TURN_STATE_FAILED
from zerg.services.session_turns import SESSION_TURN_STATE_STARTING

CONSOLE_TURN_START_COMMAND = "session.turn.start"
CONSOLE_TURN_INTERRUPT_COMMAND = "session.turn.interrupt"
# Leave enough room for the HTTP boundary to persist and return an ambiguous
# command outcome instead of cancelling the request at the same instant as the
# Machine Agent reply budget expires.
CONSOLE_CONTROL_REPLY_TIMEOUT_SECONDS = 10
logger = logging.getLogger("longhouse.console_latency")


class ConsoleTurnUnavailable(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ConsoleTurnConflict(RuntimeError):
    pass


def stamp_console_result(db: Session, *, session_id, outcome: str, at: datetime) -> None:
    """Denormalize a terminal Console turn onto the session row for unread derivation.

    Only terminal outcomes reach here — a draining turn's early terminal_at
    must never stamp (docs/specs/console-unread-acknowledgement.md).
    """

    session = db.get(AgentSession, session_id)
    if session is not None:
        session.last_console_result_at = at
        session.last_console_result_outcome = outcome


@dataclass(frozen=True)
class ConsoleTurnDispatch:
    turn_id: int | None
    run_id: UUID | None
    state: str
    error_code: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class CatalogConsoleTurn:
    turn_id: UUID
    run_id: UUID | None
    state: str
    created: bool
    error_code: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class ConsoleTurnInterrupt:
    turn_id: UUID | int
    run_id: UUID
    dispatched: bool
    error: str | None = None


async def interrupt_console_turn(
    db: Session | None,
    *,
    owner_id: int,
    session_id: UUID,
    registry=None,
) -> ConsoleTurnInterrupt:
    """Interrupt the exact active Console invocation, never a Helm control path."""

    from zerg.services.catalogd_supervisor import get_catalogd_client
    from zerg.services.machine_control_channel import get_machine_control_channel_registry

    turn: dict[str, object] | None
    client = get_catalogd_client()
    if client is None:
        raise ConsoleTurnUnavailable("catalog_unavailable", "Console turn catalog is unavailable")
    result = await client.call(
        "session.console.turn.current.v2",
        {"session_id": str(session_id), "owner_id": owner_id},
    )
    if result.get("found") is not True:
        raise ConsoleTurnUnavailable("session_not_found", "Console session was not found")
    turn = result.get("turn") if isinstance(result.get("turn"), dict) else None
    if turn is None or not turn.get("run_id"):
        raise ConsoleTurnUnavailable("no_active_turn", "Session has no active Console turn")

    provider = str(turn.get("provider") or "").strip()
    device_id = str(turn.get("device_id") or "").strip()
    run_id = UUID(str(turn["run_id"]))
    capability = f"{provider}.turn_interrupt"
    control = registry or get_machine_control_channel_registry()
    if not control.supports(owner_id=owner_id, device_id=device_id, capability=capability):
        raise ConsoleTurnUnavailable("adapter_unavailable", f"Machine Agent does not advertise {capability}")
    response = await control.send_command(
        owner_id=owner_id,
        device_id=device_id,
        session_id=str(session_id),
        command_type=CONSOLE_TURN_INTERRUPT_COMMAND,
        payload={
            "provider": provider,
            "run_id": str(run_id),
            "turn_id": str(turn.get("turn_id") or ""),
            "thread_id": str(turn.get("thread_id") or ""),
        },
        command_id=f"{run_id}:interrupt",
        timeout_secs=CONSOLE_CONTROL_REPLY_TIMEOUT_SECONDS,
    )
    message = dict(response.message or {})
    error = None
    if not response.transport_ok:
        error = str(response.error or "Console turn interrupt outcome is unknown")
    elif message.get("ok") is not True:
        detail = message.get("error") if isinstance(message.get("error"), dict) else {}
        error = str(detail.get("message") or response.error or "Console turn interrupt failed")
    return ConsoleTurnInterrupt(
        turn_id=UUID(str(turn["turn_id"])),
        run_id=run_id,
        dispatched=error is None,
        error=error,
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
    accepted_wall_ms = int(time.time() * 1000)
    accepted_mono = time.monotonic()
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
    error_code = None
    error = None
    if not control.supports(owner_id=owner_id, device_id=device_id, capability=capability):
        error_code = "adapter_unavailable"
        error = f"Machine Agent does not advertise {capability}"
    else:
        dispatch_wall_ms = int(time.time() * 1000)
        payload = {
            "run_id": str(run_id),
            "thread_id": str(turn["thread_id"]),
            "turn_id": str(turn_id),
            "client_request_id": str(turn.get("client_request_id") or client_request_id),
            "provider": provider,
            "cwd": str(turn["cwd"]),
            "message": str(turn.get("message") or message),
            "launch_actor": "user",
            "launch_surface": "console",
            "server_accepted_at_ms": accepted_wall_ms,
            "server_dispatched_at_ms": dispatch_wall_ms,
            **dict(turn.get("provider_config") or {}),
        }
        if turn.get("resume_provider_thread_id"):
            payload["resume_provider_thread_id"] = turn["resume_provider_thread_id"]
        if turn.get("fork_from_provider_thread_id"):
            payload["fork_from_provider_thread_id"] = turn["fork_from_provider_thread_id"]
        logger.info(
            "console_latency stage=command_dispatch session=%s turn=%s run=%s request=%s provider=%s device=%s accepted_elapsed_ms=%d",
            session_id,
            turn_id,
            run_id,
            payload["client_request_id"],
            provider,
            device_id,
            int((time.monotonic() - accepted_mono) * 1000),
        )
        command_started = time.monotonic()
        response = await control.send_command(
            owner_id=owner_id,
            device_id=device_id,
            session_id=str(session_id),
            command_type=CONSOLE_TURN_START_COMMAND,
            payload=payload,
            command_id=str(run_id),
            timeout_secs=CONSOLE_CONTROL_REPLY_TIMEOUT_SECONDS,
        )
        response_message = dict(response.message or {})
        detail = response_message.get("error") if isinstance(response_message.get("error"), dict) else {}
        response_error_code = str(detail.get("code") or "").strip() or None
        logger.info(
            "console_latency stage=command_response session=%s turn=%s run=%s request=%s transport_ok=%s command_ok=%s error_code=%s command_ms=%d total_ms=%d",
            session_id,
            turn_id,
            run_id,
            payload["client_request_id"],
            response.transport_ok,
            response_message.get("ok"),
            response_error_code,
            int((time.monotonic() - command_started) * 1000),
            int((time.monotonic() - accepted_mono) * 1000),
        )
        if not response.transport_ok:
            error = str(response.error or "Console turn dispatch outcome is unknown")
            update_result = await _mark_catalog_start_outcome_unknown(
                client,
                owner_id=owner_id,
                turn_id=turn_id,
                run_id=run_id,
                session_id=UUID(str(turn["session_id"])),
                thread_id=UUID(str(turn["thread_id"])),
                provider=provider,
                device_id=device_id,
                error=error,
            )
            persisted_turn = dict(update_result.get("turn") or {})
            persisted_state = str(persisted_turn.get("state") or SESSION_TURN_STATE_STARTING)
            if update_result.get("applied") is False:
                return CatalogConsoleTurn(
                    turn_id=turn_id,
                    run_id=run_id,
                    state=persisted_state,
                    created=bool(result.get("created")),
                    error=str(persisted_turn.get("error") or "") or None,
                )
            return CatalogConsoleTurn(
                turn_id=turn_id,
                run_id=run_id,
                state=persisted_state,
                created=bool(result.get("created")),
                error_code="turn_start_outcome_unknown",
                error=error,
            )
        if response_message.get("ok") is not True:
            error_code = response_error_code or "provider_launch_failed"
            error = str(detail.get("message") or response.error or "Console turn dispatch failed")

    state = SESSION_TURN_STATE_FAILED if error else SESSION_TURN_STATE_ACTIVE
    update_result = await client.call(
        "session.console.turn.update.v2",
        {
            "turn": {
                "turn_id": str(turn_id),
                "run_id": str(run_id),
                "owner_id": owner_id,
                "session_id": str(turn["session_id"]),
                "thread_id": str(turn["thread_id"]),
                "provider": provider,
                "device_id": device_id,
                "state": state,
                "expected_state": SESSION_TURN_STATE_STARTING,
                "error_code": error_code,
                "error": error,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )
    persisted_turn = dict(update_result.get("turn") or {})
    state = str(persisted_turn.get("state") or state)
    if update_result.get("applied") is False:
        return CatalogConsoleTurn(
            turn_id=turn_id,
            run_id=run_id,
            state=state,
            created=bool(result.get("created")),
            error=str(persisted_turn.get("error") or "") or None,
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
        error_code=error_code,
        error=error,
    )


@dataclass(frozen=True)
class CreatedBranch:
    """One branch, its first turn, and whether this call created it."""

    session_id: UUID
    thread_id: UUID
    turn_id: UUID
    run_id: UUID | None
    state: str
    created: bool


async def create_branch_with_first_turn(
    *,
    owner_id: int,
    parent_session_id: UUID,
    message: str,
    client_request_id: str,
    display_name: str | None = None,
    launch_surface: str = "console",
    client=None,
    registry=None,
) -> CreatedBranch:
    """Create a branch and send its first turn.

    The catalog mutation is atomic; the dispatch that follows is not, because
    the Machine Agent is reached over the wire after commit. A send failure
    therefore leaves a real branch whose first turn records the error, which is
    what the caller should surface -- the session exists and the user can see
    it, so raising would hide something that was genuinely created.
    """

    from zerg.services.catalogd_supervisor import get_catalogd_client

    catalog = client or get_catalogd_client()
    if catalog is None:
        raise ConsoleTurnUnavailable("catalog_unavailable", "Console turn catalog is unavailable")

    now = datetime.now(timezone.utc)
    result = await catalog.call(
        "session.branch.create.v2",
        {
            "branch": {
                "parent_session_id": str(parent_session_id),
                "session_id": str(uuid4()),
                "thread_id": str(uuid4()),
                "owner_id": int(owner_id),
                "message": message,
                "client_request_id": client_request_id,
                "display_name": display_name,
                "launch_surface": launch_surface,
                "created_at": now.isoformat(),
            }
        },
    )
    if result.get("found") is not True:
        raise ConsoleTurnUnavailable("session_not_found", "Parent session was not found")
    if result.get("idempotency_conflict") is True:
        raise ConsoleTurnConflict("client_request_id was reused with different text")
    if result.get("unavailable"):
        raise ConsoleTurnUnavailable(str(result["unavailable"]), "Branch execution target is unavailable")

    turn = dict(result.get("turn") or {})
    created = bool(result.get("created"))
    child_session_id = UUID(str(result["session_id"]))
    child_thread_id = UUID(str(result["thread_id"]))
    turn_id = UUID(str(turn["turn_id"]))
    run_id = UUID(str(turn["run_id"])) if turn.get("run_id") else None

    # A replay already dispatched; re-sending would start the provider twice for
    # one request id.
    if not created or run_id is None:
        return CreatedBranch(
            session_id=child_session_id,
            thread_id=child_thread_id,
            turn_id=turn_id,
            run_id=run_id,
            state=str(turn.get("state") or "queued"),
            created=created,
        )

    dispatched = await dispatch_catalog_claimed_turn(owner_id=owner_id, turn=turn, client=catalog, registry=registry)
    return CreatedBranch(
        session_id=child_session_id,
        thread_id=child_thread_id,
        turn_id=dispatched.turn_id,
        run_id=dispatched.run_id,
        state=dispatched.state,
        created=True,
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
    error_code = None
    error = None
    if not control.supports(owner_id=owner_id, device_id=device_id, capability=capability):
        error_code = "adapter_unavailable"
        error = f"Machine Agent does not advertise {capability}"
    else:
        payload = {
            "run_id": str(run_id),
            "thread_id": str(turn["thread_id"]),
            "turn_id": str(turn_id),
            "client_request_id": str(turn.get("client_request_id") or ""),
            "provider": provider,
            "cwd": str(turn["cwd"]),
            "message": str(turn.get("message") or ""),
            "launch_actor": "user",
            "launch_surface": "console",
            **dict(turn.get("provider_config") or {}),
        }
        if turn.get("resume_provider_thread_id"):
            payload["resume_provider_thread_id"] = turn["resume_provider_thread_id"]
        if turn.get("fork_from_provider_thread_id"):
            payload["fork_from_provider_thread_id"] = turn["fork_from_provider_thread_id"]
        response = await control.send_command(
            owner_id=owner_id,
            device_id=device_id,
            session_id=str(session_id),
            command_type=CONSOLE_TURN_START_COMMAND,
            payload=payload,
            command_id=str(run_id),
            timeout_secs=CONSOLE_CONTROL_REPLY_TIMEOUT_SECONDS,
        )
        message = dict(response.message or {})
        if not response.transport_ok:
            error = str(response.error or "Console turn dispatch outcome is unknown")
            update_result = await _mark_catalog_start_outcome_unknown(
                catalog,
                owner_id=owner_id,
                turn_id=turn_id,
                run_id=run_id,
                session_id=session_id,
                thread_id=UUID(str(turn["thread_id"])),
                provider=provider,
                device_id=device_id,
                error=error,
            )
            persisted_turn = dict(update_result.get("turn") or {})
            persisted_state = str(persisted_turn.get("state") or SESSION_TURN_STATE_STARTING)
            if update_result.get("applied") is False:
                return CatalogConsoleTurn(
                    turn_id=turn_id,
                    run_id=run_id,
                    state=persisted_state,
                    created=True,
                    error=str(persisted_turn.get("error") or "") or None,
                )
            return CatalogConsoleTurn(
                turn_id=turn_id,
                run_id=run_id,
                state=persisted_state,
                created=True,
                error_code="turn_start_outcome_unknown",
                error=error,
            )
        if message.get("ok") is not True:
            detail = message.get("error") if isinstance(message.get("error"), dict) else {}
            error_code = str(detail.get("code") or "provider_launch_failed")
            error = str(detail.get("message") or response.error or "Console turn dispatch failed")
    state = SESSION_TURN_STATE_FAILED if error else SESSION_TURN_STATE_ACTIVE
    update_result = await catalog.call(
        "session.console.turn.update.v2",
        {
            "turn": {
                "turn_id": str(turn_id),
                "run_id": str(run_id),
                "owner_id": owner_id,
                "session_id": str(session_id),
                "thread_id": str(turn["thread_id"]),
                "provider": provider,
                "device_id": device_id,
                "state": state,
                "expected_state": SESSION_TURN_STATE_STARTING,
                "error_code": error_code,
                "error": error,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )
    persisted_turn = dict(update_result.get("turn") or {})
    state = str(persisted_turn.get("state") or state)
    if update_result.get("applied") is False:
        return CatalogConsoleTurn(
            turn_id=turn_id,
            run_id=run_id,
            state=state,
            created=True,
            error=str(persisted_turn.get("error") or "") or None,
        )
    next_turn = update_result.get("next_turn")
    if isinstance(next_turn, dict):
        await dispatch_catalog_claimed_turn(
            owner_id=owner_id,
            turn=next_turn,
            client=catalog,
            registry=control,
        )
    return CatalogConsoleTurn(
        turn_id=turn_id,
        run_id=run_id,
        state=state,
        created=True,
        error_code=error_code,
        error=error,
    )


async def reconcile_starting_console_turns_for_device(
    db: Session | None,
    *,
    owner_id: int,
    device_id: str,
    registry=None,
) -> list[CatalogConsoleTurn | ConsoleTurnDispatch]:
    """Replay ambiguous dispatches after a Machine Agent reconnects.

    The stable run_id is also the machine command_id. The Machine Agent's
    durable claim registry therefore returns the existing launch outcome
    instead of spawning a second provider invocation.
    """

    from zerg.services.catalogd_supervisor import get_catalogd_client
    from zerg.services.machine_control_channel import get_machine_control_channel_registry

    control = registry or get_machine_control_channel_registry()
    catalog = get_catalogd_client()
    if catalog is None:
        raise ConsoleTurnUnavailable("catalog_unavailable", "Console turn catalog is unavailable")
    result = await catalog.call(
        "session.console.turn.starting_for_device.v2",
        {"owner_id": owner_id, "device_id": device_id},
    )
    turns = result.get("turns") if isinstance(result.get("turns"), list) else []
    reconciled: list[CatalogConsoleTurn | ConsoleTurnDispatch] = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        capability = f"{turn.get('provider')}.turn_start"
        if not control.supports(owner_id=owner_id, device_id=device_id, capability=capability):
            error = f"Machine Agent reconnected without advertising {capability}; launch outcome remains unknown"
            update_result = await _mark_catalog_start_outcome_unknown(
                catalog,
                owner_id=owner_id,
                turn_id=UUID(str(turn["turn_id"])),
                run_id=UUID(str(turn["run_id"])),
                session_id=UUID(str(turn["session_id"])),
                thread_id=UUID(str(turn["thread_id"])),
                provider=str(turn["provider"]),
                device_id=str(turn["device_id"]),
                error=error,
            )
            persisted_turn = dict(update_result.get("turn") or {})
            applied = update_result.get("applied") is not False
            reconciled.append(
                CatalogConsoleTurn(
                    turn_id=UUID(str(turn["turn_id"])),
                    run_id=UUID(str(turn["run_id"])),
                    state=str(persisted_turn.get("state") or SESSION_TURN_STATE_STARTING),
                    created=False,
                    error_code="turn_start_outcome_unknown" if applied else None,
                    error=error if applied else (str(persisted_turn.get("error") or "") or None),
                )
            )
            continue
        reconciled.append(
            await dispatch_catalog_claimed_turn(
                owner_id=owner_id,
                turn=turn,
                client=catalog,
                registry=control,
            )
        )
    return reconciled


async def _mark_catalog_start_outcome_unknown(
    catalog,
    *,
    owner_id: int,
    turn_id: UUID,
    run_id: UUID,
    session_id: UUID,
    thread_id: UUID,
    provider: str,
    device_id: str,
    error: str,
) -> dict[str, object]:
    return await catalog.call(
        "session.console.turn.update.v2",
        {
            "turn": {
                "turn_id": str(turn_id),
                "run_id": str(run_id),
                "owner_id": owner_id,
                "session_id": str(session_id),
                "thread_id": str(thread_id),
                "provider": provider,
                "device_id": device_id,
                "state": SESSION_TURN_STATE_STARTING,
                "expected_state": SESSION_TURN_STATE_STARTING,
                "error_code": "turn_start_outcome_unknown",
                "error": error,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )
