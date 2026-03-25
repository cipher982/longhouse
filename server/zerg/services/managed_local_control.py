"""Shared managed-local control helpers.

This module keeps tmux-backed local session control in one place so the
session-chat route and Loop actions use the same transport semantics.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Mapping
from uuid import UUID

from sqlalchemy.orm import Session

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionPresence
from zerg.models.agents import SessionRuntimeEvent
from zerg.services.agents_store import AgentsStore
from zerg.services.managed_local_tmux import build_managed_local_shell_prelude
from zerg.services.managed_local_tmux import build_tmux_paste_text_command
from zerg.services.managed_local_tmux import build_tmux_send_text_command
from zerg.services.presence_cache import get_presence_cache
from zerg.services.runner_connection_manager import get_runner_connection_manager
from zerg.services.runner_job_dispatcher import get_runner_job_dispatcher
from zerg.services.session_continuity import encode_cwd_for_claude
from zerg.services.session_continuity import validate_session_id
from zerg.session_execution_home import ManagedSessionTransport
from zerg.session_execution_home import SessionExecutionHome

logger = logging.getLogger(__name__)

MANAGED_LOCAL_EVENT_TIMEOUT_SECS = 150.0
MANAGED_LOCAL_POLL_INTERVAL_SECS = 1.0
MANAGED_LOCAL_STABLE_POLLS = 1
MANAGED_LOCAL_RUNNER_RECONNECT_GRACE_SECS = 8.0
MANAGED_LOCAL_RUNNER_RECONNECT_POLL_INTERVAL_SECS = 0.25
MANAGED_LOCAL_SYNC_STATUS_PENDING = "pending"
MANAGED_LOCAL_SYNC_STATUS_COMPLETE = "complete"
MANAGED_LOCAL_SYNC_STATUS_FAILED = "failed"
MANAGED_LOCAL_CONTROL_STATUS_COMPLETED = "completed"
MANAGED_LOCAL_CONTROL_STATUS_NEEDS_USER = "needs_user"
MANAGED_LOCAL_CONTROL_STATUS_BLOCKED = "blocked"
MANAGED_LOCAL_CONTROL_STATUS_FAILED = "failed"
_MANAGED_LOCAL_HOOK_RUNTIME_SOURCE = "claude_hook"
_MANAGED_LOCAL_ACTIVE_HOOK_PHASES = frozenset({"thinking", "running"})
_MANAGED_LOCAL_TRANSIENT_RUNNER_DISPATCH_ERRORS = frozenset(
    {
        "Runner is offline",
        "Failed to send command to runner",
    }
)
_MANAGED_LOCAL_TERMINAL_PHASE_TO_CONTROL_STATUS = {
    "idle": MANAGED_LOCAL_CONTROL_STATUS_COMPLETED,
    "needs_user": MANAGED_LOCAL_CONTROL_STATUS_NEEDS_USER,
    "blocked": MANAGED_LOCAL_CONTROL_STATUS_BLOCKED,
}
_MANAGED_LOCAL_PRESENCE_CURSOR_UNSET = object()


@dataclass(frozen=True)
class ManagedLocalSendResult:
    ok: bool
    exit_code: int | None = None
    error: str | None = None
    baseline_event_id: int | None = None
    verified_turn_started: bool = False


@dataclass(frozen=True)
class ManagedLocalShipResult:
    ok: bool
    exit_code: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class ManagedLocalPhaseUpdate:
    phase: str
    runtime_event_id: int = 0
    occurred_at: datetime | None = None
    source: str = _MANAGED_LOCAL_HOOK_RUNTIME_SOURCE


@dataclass(frozen=True)
class ManagedLocalTerminalResult:
    phase: str
    control_status: str
    runtime_event_id: int
    occurred_at: datetime | None = None


def validate_managed_local_chat_done_payload(
    *,
    session_id: str,
    done_payload: Mapping[str, object] | None,
) -> str | None:
    """Validate the `/api/sessions/{id}/chat` done payload for managed-local sends."""

    if done_payload is None:
        return "missing done payload"
    if done_payload.get("created_continuation") is not False:
        return f"expected created_continuation=false, got {done_payload.get('created_continuation')!r}"
    if str(done_payload.get("shipped_session_id") or "") != session_id:
        return f"expected shipped_session_id={session_id}, got {done_payload.get('shipped_session_id')!r}"
    sync_status = str(done_payload.get("sync_status") or "").strip().lower()
    if sync_status not in {
        MANAGED_LOCAL_SYNC_STATUS_PENDING,
        MANAGED_LOCAL_SYNC_STATUS_COMPLETE,
    }:
        return f"expected sync_status in {{'pending','complete'}}, got {done_payload.get('sync_status')!r}"
    if done_payload.get("persistence_error") is not None:
        return f"unexpected persistence_error={done_payload.get('persistence_error')!r}"
    if sync_status == MANAGED_LOCAL_SYNC_STATUS_COMPLETE:
        if int(done_payload.get("persisted_events") or 0) <= 0:
            return f"expected persisted_events>0, got {done_payload.get('persisted_events')!r}"

    exit_code_raw = done_payload.get("exit_code")
    try:
        exit_code = int(exit_code_raw)
    except (TypeError, ValueError):
        return f"expected exit_code=0, got {exit_code_raw!r}"
    if exit_code != 0:
        return f"expected exit_code=0, got {done_payload.get('exit_code')!r}"
    return None


def build_managed_local_claude_ship_command(*, session: AgentSession) -> str:
    """Build a runner-side command that ships the exact managed-local Claude transcript.

    Interactive managed-local chat cannot rely on the background shipper queue for
    low-latency UX. This command targets the known Claude transcript directly and
    retries briefly until the current turn produces real shipped events, so we do
    not exit early on a successful zero-event ship before Claude has flushed the
    prompt/assistant lines into the transcript.
    """

    cwd = str(getattr(session, "cwd", "") or "").strip()
    if not cwd:
        raise ValueError("Managed local Claude session is missing cwd")

    provider_session_id = str(getattr(session, "provider_session_id", "") or "").strip()
    if not provider_session_id:
        raise ValueError("Managed local Claude session is missing provider_session_id")
    validate_session_id(provider_session_id)

    longhouse_session_id = str(getattr(session, "id", "") or "").strip()
    validate_session_id(longhouse_session_id)

    encoded_cwd = encode_cwd_for_claude(cwd)
    transcript_path = f"$HOME/.claude/projects/{encoded_cwd}/{provider_session_id}.jsonl"
    inner = [
        build_managed_local_shell_prelude(
            tmux_tmpdir=getattr(session, "managed_tmux_tmpdir", None),
            require_tmux=False,
        ),
        'engine="$(command -v longhouse-engine || true)"',
        '[ -n "$engine" ] || { echo "longhouse-engine is not available" >&2; exit 12; }',
        f'transcript="{transcript_path}"',
        'tmp_json="$(mktemp)"',
        "total_shipped=0",
        "delays=(0 0.25 0.5 1 2 4)",
        (
            'for delay in "${delays[@]}"; do '
            'if [ "$delay" != "0" ]; then sleep "$delay"; fi; '
            '[ -f "$transcript" ] || continue; '
            f'"$engine" ship --file "$transcript" --session-id {shlex.quote(longhouse_session_id)} --json '
            '>"$tmp_json" 2>/dev/null || true; '
            'shipped="$(grep -Eo \'"events_shipped"[[:space:]]*:[[:space:]]*[0-9]+\' "$tmp_json" '
            "| grep -Eo '[0-9]+' | head -1 || true)\"; "
            '[ -n "$shipped" ] || shipped=0; '
            "total_shipped=$((total_shipped + shipped)); "
            "done"
        ),
        'rm -f "$tmp_json"',
        '[ "$total_shipped" -gt 0 ] && exit 0',
        'echo "Managed local Claude transcript did not ship new events" >&2',
        "exit 13",
    ]
    return f"zsh -lc {shlex.quote('; '.join(inner))}"


def _normalize_utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _to_utc_timestamp(value: datetime | None) -> float | None:
    normalized = _normalize_utc_datetime(value)
    return normalized.timestamp() if normalized is not None else None


async def _await_managed_local_runner_reconnect(
    *,
    owner_id: int,
    runner_id: int,
    timeout_secs: float,
    poll_interval_secs: float = MANAGED_LOCAL_RUNNER_RECONNECT_POLL_INTERVAL_SECS,
) -> bool:
    """Wait briefly for a managed-local runner websocket to reconnect."""

    if timeout_secs <= 0:
        return False

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_secs
    connection_manager = get_runner_connection_manager()

    while loop.time() < deadline:
        if connection_manager.is_online(owner_id, runner_id):
            return True
        await asyncio.sleep(poll_interval_secs)

    return connection_manager.is_online(owner_id, runner_id)


def get_managed_local_presence_updated_at(*, session_id: UUID) -> datetime | None:
    """Return the latest in-memory presence timestamp for a managed-local session."""

    entry = get_presence_cache().get(str(session_id))
    return _normalize_utc_datetime(getattr(entry, "updated_at", None))


def _get_newer_cached_presence_entry(
    *,
    session_id: UUID,
    after_updated_at: datetime | None | object = _MANAGED_LOCAL_PRESENCE_CURSOR_UNSET,
):
    if after_updated_at is _MANAGED_LOCAL_PRESENCE_CURSOR_UNSET:
        return None

    cache = get_presence_cache()
    entry = cache.get(str(session_id))
    if entry is None:
        return None

    entry_updated_at = _normalize_utc_datetime(getattr(entry, "updated_at", None))
    baseline_ts = _to_utc_timestamp(after_updated_at if isinstance(after_updated_at, datetime) else None)
    entry_ts = _to_utc_timestamp(entry_updated_at)
    if baseline_ts is not None and (entry_ts is None or entry_ts <= baseline_ts):
        return None
    return entry


def get_managed_local_latest_event_id(*, db: Session, session_id: UUID) -> int:
    """Return the latest stored event id for a managed-local session."""
    return int(AgentsStore(db).get_latest_event_id(session_id) or 0)


def get_managed_local_latest_hook_runtime_event_id(*, db: Session, session_id: UUID) -> int:
    """Return the latest hook-driven runtime event id for a managed-local session."""
    row = (
        db.query(SessionRuntimeEvent.id)
        .filter(
            SessionRuntimeEvent.session_id == session_id,
            SessionRuntimeEvent.source == _MANAGED_LOCAL_HOOK_RUNTIME_SOURCE,
        )
        .order_by(SessionRuntimeEvent.id.desc())
        .first()
    )
    return int(row[0]) if row else 0


def _fetch_managed_local_events_since(*, db_bind, session_id: UUID, after_event_id: int) -> list[AgentEvent]:
    with Session(bind=db_bind) as poll_db:
        return (
            poll_db.query(AgentEvent)
            .filter(AgentEvent.session_id == session_id)
            .filter(AgentEvent.id > after_event_id)
            .order_by(AgentEvent.timestamp.asc(), AgentEvent.id.asc())
            .all()
        )


def _fetch_managed_local_hook_runtime_events_since(
    *,
    db_bind,
    session_id: UUID,
    after_runtime_event_id: int,
) -> list[SessionRuntimeEvent]:
    with Session(bind=db_bind) as poll_db:
        return (
            poll_db.query(SessionRuntimeEvent)
            .filter(
                SessionRuntimeEvent.session_id == session_id,
                SessionRuntimeEvent.source == _MANAGED_LOCAL_HOOK_RUNTIME_SOURCE,
                SessionRuntimeEvent.id > after_runtime_event_id,
            )
            .order_by(SessionRuntimeEvent.id.asc())
            .all()
        )


def _get_managed_local_presence_updated_at(*, db_bind, session_id: UUID) -> datetime | None:
    with Session(bind=db_bind) as poll_db:
        row = poll_db.query(SessionPresence).filter(SessionPresence.session_id == str(session_id)).one_or_none()
        return row.updated_at if row is not None else None


async def await_managed_local_presence_update(
    *,
    db_bind,
    session_id: UUID,
    after_updated_at: datetime | None,
    timeout_secs: float = MANAGED_LOCAL_EVENT_TIMEOUT_SECS,
    poll_interval_secs: float = MANAGED_LOCAL_POLL_INTERVAL_SECS,
) -> SessionPresence | None:
    """Wait until a managed-local session gets a newer presence update."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_secs
    cache = get_presence_cache()

    while loop.time() < deadline:
        cached = _get_newer_cached_presence_entry(
            session_id=session_id,
            after_updated_at=after_updated_at,
        )
        if cached is not None:
            return cache.to_presence_obj(cached)

        with Session(bind=db_bind) as poll_db:
            row = poll_db.query(SessionPresence).filter(SessionPresence.session_id == str(session_id)).one_or_none()
            if row is not None and row.updated_at is not None:
                row_ts = _to_utc_timestamp(row.updated_at)
                baseline_ts = _to_utc_timestamp(after_updated_at)
                if baseline_ts is None or (row_ts is not None and row_ts > baseline_ts):
                    return row
        await asyncio.sleep(poll_interval_secs)

    return None


async def await_managed_local_hook_phase_update(
    *,
    db_bind,
    session_id: UUID,
    after_runtime_event_id: int,
    after_presence_updated_at: datetime | None | object = _MANAGED_LOCAL_PRESENCE_CURSOR_UNSET,
    phases: set[str] | frozenset[str] | None = None,
    timeout_secs: float = MANAGED_LOCAL_EVENT_TIMEOUT_SECS,
    poll_interval_secs: float = MANAGED_LOCAL_POLL_INTERVAL_SECS,
) -> ManagedLocalPhaseUpdate | None:
    """Wait for a new hook-driven runtime phase after the provided cursor."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_secs
    cursor = after_runtime_event_id

    while loop.time() < deadline:
        cached = _get_newer_cached_presence_entry(
            session_id=session_id,
            after_updated_at=after_presence_updated_at,
        )
        if cached is not None:
            phase = str(getattr(cached, "state", "") or "").strip()
            if phases is None or phase in phases:
                return ManagedLocalPhaseUpdate(
                    phase=phase,
                    occurred_at=_normalize_utc_datetime(getattr(cached, "updated_at", None)),
                    source="presence_cache",
                )

        events = _fetch_managed_local_hook_runtime_events_since(
            db_bind=db_bind,
            session_id=session_id,
            after_runtime_event_id=cursor,
        )
        for event in events:
            cursor = max(cursor, int(getattr(event, "id", 0) or 0))
            phase = str(getattr(event, "phase", "") or "").strip()
            if phases is None or phase in phases:
                return ManagedLocalPhaseUpdate(
                    phase=phase,
                    runtime_event_id=int(getattr(event, "id", 0) or 0),
                    occurred_at=_normalize_utc_datetime(getattr(event, "occurred_at", None)),
                    source=str(getattr(event, "source", "") or _MANAGED_LOCAL_HOOK_RUNTIME_SOURCE),
                )
        await asyncio.sleep(poll_interval_secs)

    return None


async def await_managed_local_turn_terminal(
    *,
    db_bind,
    session_id: UUID,
    after_runtime_event_id: int,
    after_presence_updated_at: datetime | None | object = _MANAGED_LOCAL_PRESENCE_CURSOR_UNSET,
    timeout_secs: float = MANAGED_LOCAL_EVENT_TIMEOUT_SECS,
    poll_interval_secs: float = MANAGED_LOCAL_POLL_INTERVAL_SECS,
) -> ManagedLocalTerminalResult | None:
    """Wait for a new terminal phase for a managed-local turn.

    For live managed-local chat, trust the in-memory presence cache first so the
    route does not block on SQLite runtime-event persistence. Hook runtime rows
    remain a fallback for cold-cache or non-hot-path callers.
    """

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_secs
    cursor = after_runtime_event_id

    while loop.time() < deadline:
        cached = _get_newer_cached_presence_entry(
            session_id=session_id,
            after_updated_at=after_presence_updated_at,
        )
        if cached is not None:
            phase = str(getattr(cached, "state", "") or "").strip()
            if phase in _MANAGED_LOCAL_TERMINAL_PHASE_TO_CONTROL_STATUS:
                return ManagedLocalTerminalResult(
                    phase=phase,
                    control_status=_MANAGED_LOCAL_TERMINAL_PHASE_TO_CONTROL_STATUS.get(
                        phase,
                        MANAGED_LOCAL_CONTROL_STATUS_COMPLETED,
                    ),
                    runtime_event_id=0,
                    occurred_at=_normalize_utc_datetime(getattr(cached, "updated_at", None)),
                )

        events = _fetch_managed_local_hook_runtime_events_since(
            db_bind=db_bind,
            session_id=session_id,
            after_runtime_event_id=cursor,
        )
        for event in events:
            cursor = max(cursor, int(getattr(event, "id", 0) or 0))
            phase = str(getattr(event, "phase", "") or "").strip()
            if phase in _MANAGED_LOCAL_ACTIVE_HOOK_PHASES:
                continue
            if phase not in _MANAGED_LOCAL_TERMINAL_PHASE_TO_CONTROL_STATUS:
                continue
            # Treat a newer terminal hook event as authoritative for the current
            # turn even if the matching active phase never made it to this
            # worker's cache or SQLite. The session-chat route passes a
            # pre-send runtime-event cursor, so any later idle/needs_user/blocked
            # event still belongs to the in-flight managed-local turn.
            return ManagedLocalTerminalResult(
                phase=phase,
                control_status=_MANAGED_LOCAL_TERMINAL_PHASE_TO_CONTROL_STATUS.get(
                    phase,
                    MANAGED_LOCAL_CONTROL_STATUS_COMPLETED,
                ),
                runtime_event_id=int(getattr(event, "id", 0) or 0),
                occurred_at=getattr(event, "occurred_at", None),
            )
        await asyncio.sleep(poll_interval_secs)

    return None


async def await_managed_local_turn_events(
    *,
    db_bind,
    session_id: UUID,
    after_event_id: int,
    timeout_secs: float = MANAGED_LOCAL_EVENT_TIMEOUT_SECS,
    poll_interval_secs: float = MANAGED_LOCAL_POLL_INTERVAL_SECS,
) -> list[AgentEvent]:
    """Wait until a managed-local send produces persisted timeline events."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_secs
    latest_seen = after_event_id
    stable_polls = 0

    while loop.time() < deadline:
        with Session(bind=db_bind) as poll_db:
            latest_event_id = get_managed_local_latest_event_id(db=poll_db, session_id=session_id)
        if latest_event_id > after_event_id:
            if latest_event_id == latest_seen:
                stable_polls += 1
            else:
                latest_seen = latest_event_id
                stable_polls = 0

            if stable_polls >= MANAGED_LOCAL_STABLE_POLLS:
                return _fetch_managed_local_events_since(
                    db_bind=db_bind,
                    session_id=session_id,
                    after_event_id=after_event_id,
                )

        await asyncio.sleep(poll_interval_secs)

    return []


async def send_text_to_managed_local_session(
    *,
    db: Session,
    owner_id: int,
    session: AgentSession,
    text: str,
    commis_id: str | None = None,
    timeout_secs: int = 15,
    verify_turn_started: bool = False,
    verification_timeout_secs: float | None = None,
) -> ManagedLocalSendResult:
    """Send text into a tmux-backed managed-local session.

    Returns a normalized result so callers do not need to know the runner
    dispatch envelope details.
    """

    if str(getattr(session, "execution_home", "") or "").strip() != SessionExecutionHome.MANAGED_LOCAL.value:
        return ManagedLocalSendResult(ok=False, error="Session is not managed_local")
    if str(getattr(session, "managed_transport", "") or "").strip() != ManagedSessionTransport.TMUX.value:
        return ManagedLocalSendResult(ok=False, error="Managed local session does not use tmux transport")
    if not getattr(session, "source_runner_id", None):
        return ManagedLocalSendResult(ok=False, error="Managed local session is missing source runner metadata")
    if not getattr(session, "managed_session_name", None):
        return ManagedLocalSendResult(ok=False, error="Managed local session is missing tmux metadata")

    provider = str(getattr(session, "provider", "") or "").strip().lower()
    baseline_event_id = get_managed_local_latest_event_id(db=db, session_id=session.id)
    baseline_hook_runtime_event_id = (
        get_managed_local_latest_hook_runtime_event_id(db=db, session_id=session.id) if verify_turn_started else 0
    )
    baseline_presence_updated_at = (
        get_managed_local_presence_updated_at(session_id=session.id) if verify_turn_started else _MANAGED_LOCAL_PRESENCE_CURSOR_UNSET
    )
    if provider == "codex":
        command = build_tmux_paste_text_command(
            session_name=str(session.managed_session_name),
            text=text,
            tmux_tmpdir=getattr(session, "managed_tmux_tmpdir", None),
        )
    else:
        command = build_tmux_send_text_command(
            session_name=str(session.managed_session_name),
            text=text,
            tmux_tmpdir=getattr(session, "managed_tmux_tmpdir", None),
        )
    dispatcher = get_runner_job_dispatcher()
    result = await dispatcher.dispatch_job(
        db=db,
        owner_id=owner_id,
        runner_id=int(session.source_runner_id),
        command=command,
        timeout_secs=timeout_secs,
        commis_id=commis_id,
        run_id=None,
    )

    if not result.get("ok"):
        return ManagedLocalSendResult(
            ok=False,
            baseline_event_id=baseline_event_id,
            error=str(result.get("error", {}).get("message", "Failed to send text to managed local session")),
        )

    data = result.get("data", {})
    exit_code = int(data.get("exit_code", 1))
    if exit_code != 0:
        detail = (data.get("stderr") or "").strip() or (data.get("stdout") or "").strip()
        return ManagedLocalSendResult(
            ok=False,
            exit_code=exit_code,
            baseline_event_id=baseline_event_id,
            error=detail or "Managed local send-text command failed",
        )

    if verify_turn_started:
        verification_timeout = float(
            verification_timeout_secs if verification_timeout_secs is not None else MANAGED_LOCAL_EVENT_TIMEOUT_SECS
        )
        hook_event = await await_managed_local_hook_phase_update(
            db_bind=db.get_bind(),
            session_id=session.id,
            after_runtime_event_id=baseline_hook_runtime_event_id,
            after_presence_updated_at=baseline_presence_updated_at,
            phases=set(_MANAGED_LOCAL_ACTIVE_HOOK_PHASES),
            timeout_secs=verification_timeout,
        )
        if hook_event is None:
            return ManagedLocalSendResult(
                ok=False,
                exit_code=0,
                baseline_event_id=baseline_event_id,
                error="Managed local session did not acknowledge the prompt after send",
                verified_turn_started=False,
            )

    return ManagedLocalSendResult(
        ok=True,
        exit_code=0,
        baseline_event_id=baseline_event_id,
        verified_turn_started=verify_turn_started,
    )


async def ship_managed_local_claude_transcript(
    *,
    db: Session,
    owner_id: int,
    session: AgentSession,
    commis_id: str | None = None,
    timeout_secs: int = 20,
) -> ManagedLocalShipResult:
    """Force-ship the exact managed-local Claude transcript via the runner.

    This bypasses background shipper lag for interactive managed-local chat.
    """

    if str(getattr(session, "provider", "") or "").strip().lower() != "claude":
        return ManagedLocalShipResult(ok=False, error="Managed local direct ship only supports Claude")
    if str(getattr(session, "execution_home", "") or "").strip() != SessionExecutionHome.MANAGED_LOCAL.value:
        return ManagedLocalShipResult(ok=False, error="Session is not managed_local")
    if not getattr(session, "source_runner_id", None):
        return ManagedLocalShipResult(ok=False, error="Managed local session is missing source runner metadata")

    try:
        command = build_managed_local_claude_ship_command(session=session)
    except Exception as exc:
        return ManagedLocalShipResult(ok=False, error=str(exc))

    dispatcher = get_runner_job_dispatcher()
    runner_id = int(session.source_runner_id)
    reconnect_budget_secs = max(0.0, float(MANAGED_LOCAL_RUNNER_RECONNECT_GRACE_SECS))
    attempt = 0
    max_attempts = 3

    while attempt < max_attempts:
        attempt += 1
        result = await dispatcher.dispatch_job(
            db=db,
            owner_id=owner_id,
            runner_id=runner_id,
            command=command,
            timeout_secs=timeout_secs,
            commis_id=commis_id,
            run_id=None,
        )
        if result.get("ok"):
            break

        error_detail = result.get("error", {})
        error_message = str(error_detail.get("message", "Failed to ship managed local Claude transcript"))
        if error_message not in _MANAGED_LOCAL_TRANSIENT_RUNNER_DISPATCH_ERRORS or reconnect_budget_secs <= 0:
            return ManagedLocalShipResult(ok=False, error=error_message)

        wait_started = asyncio.get_running_loop().time()
        reconnected = await _await_managed_local_runner_reconnect(
            owner_id=owner_id,
            runner_id=runner_id,
            timeout_secs=reconnect_budget_secs,
        )
        reconnect_budget_secs = max(0.0, reconnect_budget_secs - (asyncio.get_running_loop().time() - wait_started))
        if not reconnected:
            return ManagedLocalShipResult(ok=False, error=error_message)

        logger.info(
            "Managed-local Claude direct ship retrying after runner %s reconnected (attempt %s, session=%s)",
            runner_id,
            attempt + 1,
            getattr(session, "id", None),
        )

    data = result.get("data", {})
    exit_code = int(data.get("exit_code", 1))
    if exit_code != 0:
        detail = (data.get("stderr") or "").strip() or (data.get("stdout") or "").strip()
        return ManagedLocalShipResult(
            ok=False,
            exit_code=exit_code,
            error=detail or "Managed local Claude transcript ship command failed",
        )

    return ManagedLocalShipResult(ok=True, exit_code=0)
