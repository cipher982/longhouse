"""Shared managed-local control helpers.

This module keeps tmux-backed local session control in one place so the
session-chat route and Loop actions use the same transport semantics.
"""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from uuid import UUID

from sqlalchemy.orm import Session

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionPresence
from zerg.services.agents_store import AgentsStore
from zerg.services.managed_local_runtime import mark_managed_local_input_sent
from zerg.services.managed_local_tmux import build_managed_local_shell_prelude
from zerg.services.managed_local_tmux import build_tmux_paste_text_command
from zerg.services.managed_local_tmux import build_tmux_send_text_command
from zerg.services.runner_job_dispatcher import get_runner_job_dispatcher
from zerg.services.session_continuity import encode_cwd_for_claude
from zerg.services.session_continuity import validate_session_id
from zerg.session_execution_home import ManagedSessionTransport
from zerg.session_execution_home import SessionExecutionHome

MANAGED_LOCAL_EVENT_TIMEOUT_SECS = 150.0
MANAGED_LOCAL_POLL_INTERVAL_SECS = 1.0
MANAGED_LOCAL_STABLE_POLLS = 1


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
        (
            "for delay in 0 1 2 4 8; do "
            'if [ "$delay" -gt 0 ]; then sleep "$delay"; fi; '
            '[ -f "$transcript" ] || continue; '
            f'"$engine" ship --file "$transcript" --session-id {shlex.quote(longhouse_session_id)} --json '
            '>"$tmp_json" 2>/dev/null || true; '
            'if grep -Eq \'"events_shipped"[[:space:]]*:[[:space:]]*[1-9][0-9]*\' "$tmp_json"; then '
            'rm -f "$tmp_json"; '
            "exit 0; "
            "fi; "
            "done"
        ),
        'rm -f "$tmp_json"',
        'echo "Managed local Claude transcript did not ship new events" >&2',
        "exit 13",
    ]
    return f"zsh -lc {shlex.quote('; '.join(inner))}"


def get_managed_local_latest_event_id(*, db: Session, session_id: UUID) -> int:
    """Return the latest stored event id for a managed-local session."""
    return int(AgentsStore(db).get_latest_event_id(session_id) or 0)


def _fetch_managed_local_events_since(*, db_bind, session_id: UUID, after_event_id: int) -> list[AgentEvent]:
    with Session(bind=db_bind) as poll_db:
        return (
            poll_db.query(AgentEvent)
            .filter(AgentEvent.session_id == session_id)
            .filter(AgentEvent.id > after_event_id)
            .order_by(AgentEvent.timestamp.asc(), AgentEvent.id.asc())
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

    def _to_utc_timestamp(value: datetime | None) -> float | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc).timestamp()
        return value.timestamp()

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_secs
    baseline_ts = _to_utc_timestamp(after_updated_at)

    while loop.time() < deadline:
        with Session(bind=db_bind) as poll_db:
            row = poll_db.query(SessionPresence).filter(SessionPresence.session_id == str(session_id)).one_or_none()
            if row is not None and row.updated_at is not None:
                row_ts = _to_utc_timestamp(row.updated_at)
                if baseline_ts is None or (row_ts is not None and row_ts > baseline_ts):
                    return row
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
    baseline_presence_updated_at = _get_managed_local_presence_updated_at(db_bind=db.get_bind(), session_id=session.id)
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
        if provider == "codex":
            presence = await await_managed_local_presence_update(
                db_bind=db.get_bind(),
                session_id=session.id,
                after_updated_at=baseline_presence_updated_at,
                timeout_secs=verification_timeout,
            )
            if presence is None:
                return ManagedLocalSendResult(
                    ok=False,
                    exit_code=0,
                    baseline_event_id=baseline_event_id,
                    error="Managed local Codex session did not acknowledge the prompt after send",
                    verified_turn_started=False,
                )
        else:
            persisted_events = await await_managed_local_turn_events(
                db_bind=db.get_bind(),
                session_id=session.id,
                after_event_id=baseline_event_id,
                timeout_secs=verification_timeout,
            )
            if not persisted_events:
                return ManagedLocalSendResult(
                    ok=False,
                    exit_code=0,
                    baseline_event_id=baseline_event_id,
                    error="Managed local session did not produce new timeline events after send",
                    verified_turn_started=False,
                )

    mark_managed_local_input_sent(
        db,
        session=session,
        dedupe_suffix=str(commis_id or ""),
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
        return ManagedLocalShipResult(
            ok=False,
            error=str(result.get("error", {}).get("message", "Failed to ship managed local Claude transcript")),
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
