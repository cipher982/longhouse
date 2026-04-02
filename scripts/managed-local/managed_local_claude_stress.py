#!/usr/bin/env python3
# ruff: noqa: E402
"""Stress a real managed-local Claude session through the local dev API.

This harness is intentionally narrow:
- prefers local-dev (`AUTH_DISABLED=1`) but can also hit the machine route with
  an explicit device token
- simple one-line prompts only
- repeated serial sends through the real `/api/sessions/{id}/chat` route
- DB-side verification that each prompt appears exactly once as a user event

It is meant to answer two tightly-related questions:
- can the current tmux-backed managed-local Claude path accept and execute
  repeated plain-text turns under stress?
- and, when needed, do those prompts also land durably in the transcript DB?
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from typing import Iterator
from uuid import UUID

import httpx

DEFAULT_DEV_DB_PATH = Path.home() / ".longhouse" / "dev.db"
DEFAULT_DEV_DATABASE_URL = f"sqlite:///{DEFAULT_DEV_DB_PATH}"
os.environ.setdefault("DATABASE_URL", DEFAULT_DEV_DATABASE_URL)

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "server"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import func
from sqlalchemy.orm import Session

from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionRuntimeEvent
from zerg.services.session_continuity import get_machine_name_label

_CLAUDE_LAUNCH_ENV_KEYS = (
    "CLAUDE_CODE_USE_BEDROCK",
    "AWS_PROFILE",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "ANTHROPIC_MODEL",
)


@dataclass(frozen=True)
class SSEEvent:
    event: str
    data: str


@dataclass(frozen=True)
class PromptDeliveryCheck:
    ok: bool
    exact_user_events_before: int
    exact_user_events_after: int
    exact_user_events_in_new_batch: int
    assistant_messages: tuple[str, ...]
    error: str | None = None


@dataclass(frozen=True)
class ControlFlowCheck:
    ok: bool
    active_phase: str | None
    terminal_phase: str | None
    observed_phases: tuple[str, ...]
    error: str | None = None


@dataclass(frozen=True)
class TurnRunResult:
    index: int
    prompt: str
    ok: bool
    http_status: int
    sse_error: str | None
    done_payload: dict[str, object] | None
    sync_status: str | None
    control: ControlFlowCheck
    delivery: PromptDeliveryCheck


def build_stress_prompts(*, count: int, prefix: str, nonce: str | None = None) -> list[str]:
    normalized_prefix = str(prefix or "").strip() or "lh-claude-stress"
    suffix = nonce or secrets.token_hex(4)
    prompts: list[str] = []
    for idx in range(1, count + 1):
        token = f"{normalized_prefix}-{idx:02d}-{suffix}"
        prompts.append(f"Reply with exactly {token} and nothing else. Do not use any tools.")
    return prompts


def parse_sse_lines(lines: Iterable[str]) -> Iterator[SSEEvent]:
    event_name = "message"
    data_lines: list[str] = []

    for raw_line in lines:
        line = raw_line.rstrip("\n")
        if not line:
            if data_lines:
                yield SSEEvent(event=event_name, data="\n".join(data_lines))
            event_name = "message"
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line.split(":", 1)[1].strip() or "message"
            continue
        if line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].lstrip())
            continue

    if data_lines:
        yield SSEEvent(event=event_name, data="\n".join(data_lines))


def assess_prompt_delivery(
    *,
    prompt: str,
    exact_user_events_before: int,
    new_events: Iterable[object],
) -> PromptDeliveryCheck:
    exact_in_batch = 0
    assistant_messages: list[str] = []
    for event in new_events:
        role = str(getattr(event, "role", "") or "")
        content = str(getattr(event, "content_text", "") or "")
        if role == "user" and content == prompt:
            exact_in_batch += 1
        if role == "assistant" and content:
            assistant_messages.append(content)

    exact_after = exact_user_events_before + exact_in_batch
    if exact_in_batch == 0:
        return PromptDeliveryCheck(
            ok=False,
            exact_user_events_before=exact_user_events_before,
            exact_user_events_after=exact_after,
            exact_user_events_in_new_batch=exact_in_batch,
            assistant_messages=tuple(assistant_messages),
            error="Prompt did not appear in new user events",
        )
    if exact_in_batch > 1:
        return PromptDeliveryCheck(
            ok=False,
            exact_user_events_before=exact_user_events_before,
            exact_user_events_after=exact_after,
            exact_user_events_in_new_batch=exact_in_batch,
            assistant_messages=tuple(assistant_messages),
            error="Prompt appeared more than once in new user events",
        )

    return PromptDeliveryCheck(
        ok=True,
        exact_user_events_before=exact_user_events_before,
        exact_user_events_after=exact_after,
        exact_user_events_in_new_batch=exact_in_batch,
        assistant_messages=tuple(assistant_messages),
    )


def assess_control_flow(*, new_runtime_events: Iterable[object]) -> ControlFlowCheck:
    active_phase: str | None = None
    terminal_phase: str | None = None
    observed_phases: list[str] = []

    for event in new_runtime_events:
        source = str(getattr(event, "source", "") or "").strip().lower()
        phase = str(getattr(event, "phase", "") or "").strip().lower()
        if not source or not phase:
            continue
        observed_phases.append(f"{source}:{phase}")
        if source != "claude_hook":
            continue
        if active_phase is None and phase in {"thinking", "running"}:
            active_phase = phase
        if terminal_phase is None and phase in {"idle", "needs_user", "blocked"}:
            terminal_phase = phase

    if active_phase is None:
        return ControlFlowCheck(
            ok=False,
            active_phase=None,
            terminal_phase=terminal_phase,
            observed_phases=tuple(observed_phases),
            error="Claude hook never reported an active phase",
        )
    if terminal_phase is None:
        return ControlFlowCheck(
            ok=False,
            active_phase=active_phase,
            terminal_phase=None,
            observed_phases=tuple(observed_phases),
            error="Claude hook never reported a terminal phase",
        )

    return ControlFlowCheck(
        ok=True,
        active_phase=active_phase,
        terminal_phase=terminal_phase,
        observed_phases=tuple(observed_phases),
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:47300", help="Local Longhouse base URL.")
    parser.add_argument(
        "--database-url",
        default=DEFAULT_DEV_DATABASE_URL,
        help=f"SQLite database URL used for verification (default: {DEFAULT_DEV_DATABASE_URL})",
    )
    parser.add_argument("--session-id", help="Existing managed-local Claude session UUID to target.")
    parser.add_argument("--cwd", help="Working directory for a newly launched session.")
    parser.add_argument("--project", default="managed-local-claude-stress", help="Project label for launch mode.")
    parser.add_argument("--display-name", default="Managed Local Claude Stress", help="Display name for launch mode.")
    parser.add_argument("--machine-name", default=get_machine_name_label(), help="Runner/machine label for launch mode.")
    parser.add_argument(
        "--device-token",
        help="Optional agents device token for `/managed-local/this-device` when auth is enabled.",
    )
    parser.add_argument(
        "--native-claude-channels",
        choices=("auto", "true", "false"),
        default="auto",
        help="Launch hint for native Claude channel availability (default: auto / omit hint).",
    )
    parser.add_argument("--count", type=int, default=6, help="Number of stress turns to send.")
    parser.add_argument("--delay-secs", type=float, default=0.0, help="Delay between turns.")
    parser.add_argument("--prompt-prefix", default="lh-claude-stress", help="Prompt prefix for generated messages.")
    parser.add_argument("--timeout-secs", type=float, default=60.0, help="HTTP timeout per request.")
    parser.add_argument(
        "--verification-mode",
        choices=("full", "control"),
        default="full",
        help="`full` requires Claude control plus prompt durability; `control` requires only Claude hook phase proof.",
    )
    parser.add_argument(
        "--control-timeout-secs",
        type=float,
        default=20.0,
        help="Follow-up wait for Claude hook runtime phases after the route accepts the turn.",
    )
    parser.add_argument(
        "--durability-timeout-secs",
        type=float,
        default=20.0,
        help="Follow-up wait for prompt durability when the done payload reports sync_status=pending.",
    )
    args = parser.parse_args()

    if not args.session_id and not args.cwd:
        parser.error("Provide either --session-id to target an existing session or --cwd to launch a new one.")
    if args.count <= 0:
        parser.error("--count must be positive")
    if args.delay_secs < 0:
        parser.error("--delay-secs must be non-negative")
    return args


def _build_session_factory(database_url: str):
    engine = make_engine(database_url)
    return make_sessionmaker(engine)


def _collect_claude_launch_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for key in _CLAUDE_LAUNCH_ENV_KEYS:
        value = str(os.environ.get(key) or "").strip()
        if value:
            env[key] = value
    return env


def _launch_headers(device_token: str | None) -> dict[str, str]:
    if not device_token:
        return {}
    return {"X-Agents-Token": device_token}


def _native_channels_hint(raw_value: str) -> bool | None:
    normalized = str(raw_value or "").strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    return None


def _fetch_managed_local_claude_session(db: Session, session_id: str) -> AgentSession:
    try:
        session_uuid = UUID(session_id)
    except ValueError as exc:
        raise SystemExit(f"Invalid session id: {session_id}") from exc

    session = db.query(AgentSession).filter(AgentSession.id == session_uuid).one_or_none()
    if session is None:
        raise SystemExit(f"Session not found: {session_id}")
    if str(session.provider or "").strip().lower() != "claude":
        raise SystemExit(f"Session {session_id} is not Claude (provider={session.provider!r})")
    if str(session.execution_home or "").strip() != "managed_local":
        raise SystemExit(f"Session {session_id} is not managed_local (execution_home={session.execution_home!r})")
    if str(session.managed_transport or "").strip() != "tmux":
        raise SystemExit(f"Session {session_id} is not tmux-backed (managed_transport={session.managed_transport!r})")
    return session


def _latest_event_id(db: Session, session_id: UUID) -> int:
    value = db.query(func.max(AgentEvent.id)).filter(AgentEvent.session_id == session_id).scalar()
    return int(value or 0)


def _count_exact_user_events(db: Session, session_id: UUID, prompt: str) -> int:
    value = (
        db.query(func.count(AgentEvent.id))
        .filter(AgentEvent.session_id == session_id)
        .filter(AgentEvent.role == "user")
        .filter(AgentEvent.content_text == prompt)
        .scalar()
    )
    return int(value or 0)


def _latest_runtime_event_id(db: Session, session_id: UUID) -> int:
    value = db.query(func.max(SessionRuntimeEvent.id)).filter(SessionRuntimeEvent.session_id == session_id).scalar()
    return int(value or 0)


def _fetch_new_events(db: Session, session_id: UUID, after_event_id: int) -> list[AgentEvent]:
    return (
        db.query(AgentEvent)
        .filter(AgentEvent.session_id == session_id)
        .filter(AgentEvent.id > after_event_id)
        .order_by(AgentEvent.id.asc())
        .all()
    )


def _fetch_new_runtime_events(db: Session, session_id: UUID, after_event_id: int) -> list[SessionRuntimeEvent]:
    return (
        db.query(SessionRuntimeEvent)
        .filter(SessionRuntimeEvent.session_id == session_id)
        .filter(SessionRuntimeEvent.id > after_event_id)
        .order_by(SessionRuntimeEvent.id.asc())
        .all()
    )


def _launch_managed_local_session(
    client: httpx.Client,
    *,
    base_url: str,
    cwd: str,
    project: str,
    display_name: str,
    machine_name: str,
    device_token: str | None,
    native_claude_channels: str,
) -> dict[str, object]:
    launch_body: dict[str, object] = {
        "cwd": cwd,
        "provider": "claude",
        "project": project,
        "display_name": display_name,
        "loop_mode": "assist",
        "machine_name": machine_name,
    }
    native_hint = _native_channels_hint(native_claude_channels)
    if native_hint is not None:
        launch_body["native_claude_channels_available"] = native_hint

    claude_launch_env = _collect_claude_launch_env()
    if claude_launch_env:
        launch_body["claude_launch_env"] = claude_launch_env

    response = client.post(
        f"{base_url.rstrip('/')}/api/sessions/managed-local/this-device",
        headers=_launch_headers(device_token),
        json=launch_body,
    )
    if response.status_code != 200:
        raise SystemExit(f"Managed-local launch failed ({response.status_code}): {response.text[:400]}")
    return response.json()


def _run_stress_turn(
    *,
    client: httpx.Client,
    base_url: str,
    session_id: str,
    prompt: str,
    index: int,
    session_factory,
    verification_mode: str,
    control_timeout_secs: float,
    durability_timeout_secs: float,
) -> TurnRunResult:
    with session_factory() as db:
        session = _fetch_managed_local_claude_session(db, session_id)
        before_event_id = _latest_event_id(db, session.id)
        before_runtime_event_id = _latest_runtime_event_id(db, session.id)
        before_count = _count_exact_user_events(db, session.id, prompt)

    sse_error: str | None = None
    done_payload: dict[str, object] | None = None
    status_code = 0
    sync_status: str | None = None

    with client.stream(
        "POST",
        f"{base_url.rstrip('/')}/api/sessions/{session_id}/chat",
        json={"message": prompt},
    ) as response:
        status_code = response.status_code
        content_type = str(response.headers.get("content-type") or "").lower()
        if response.status_code != 200:
            response.read()
            return TurnRunResult(
                index=index,
                prompt=prompt,
                ok=False,
                http_status=response.status_code,
                sse_error=response.text[:400],
                done_payload=None,
                sync_status=None,
                control=ControlFlowCheck(
                    ok=False,
                    active_phase=None,
                    terminal_phase=None,
                    observed_phases=(),
                    error="Chat route did not return 200",
                ),
                delivery=PromptDeliveryCheck(
                    ok=False,
                    exact_user_events_before=before_count,
                    exact_user_events_after=before_count,
                    exact_user_events_in_new_batch=0,
                    assistant_messages=(),
                    error="Chat route did not return 200",
                ),
            )

        if "application/json" in content_type:
            response.read()
            try:
                parsed_json = response.json()
            except json.JSONDecodeError:
                parsed_json = {"raw": response.text[:400]}

            done_payload = parsed_json if isinstance(parsed_json, dict) else {"raw": str(parsed_json)}
            if not bool(done_payload.get("accepted")):
                sse_error = str(done_payload.get("error") or done_payload)[:400]
            else:
                # Managed-local chat returns a fast JSON acceptance ack and then
                # persistence catches up asynchronously through the normal shipper
                # path. Treat that as a pending durability state and poll the DB.
                sync_status = "pending"
        else:
            for event in parse_sse_lines(response.iter_lines()):
                if event.event == "error":
                    try:
                        parsed = json.loads(event.data)
                        sse_error = str(parsed.get("error") or event.data)
                    except json.JSONDecodeError:
                        sse_error = event.data
                elif event.event == "done":
                    try:
                        parsed_done = json.loads(event.data)
                        if isinstance(parsed_done, dict):
                            done_payload = parsed_done
                            raw_sync_status = str(parsed_done.get("sync_status") or "").strip().lower()
                            if raw_sync_status:
                                sync_status = raw_sync_status
                    except json.JSONDecodeError:
                        done_payload = {"raw": event.data}

    if sync_status is None and isinstance(done_payload, dict):
        persisted_events = done_payload.get("persisted_events")
        try:
            if int(persisted_events or 0) > 0:
                sync_status = "complete"
        except (TypeError, ValueError):
            sync_status = None

    control = ControlFlowCheck(
        ok=False,
        active_phase=None,
        terminal_phase=None,
        observed_phases=(),
        error="Claude hook phases were not evaluated",
    )
    delivery = PromptDeliveryCheck(
        ok=False,
        exact_user_events_before=before_count,
        exact_user_events_after=before_count,
        exact_user_events_in_new_batch=0,
        assistant_messages=(),
        error="Prompt durability was not evaluated",
    )
    require_delivery = verification_mode == "full"
    control_deadline = time.monotonic() + control_timeout_secs
    delivery_deadline = time.monotonic() + (durability_timeout_secs if sync_status == "pending" else 0.0)

    while True:
        with session_factory() as db:
            session = _fetch_managed_local_claude_session(db, session_id)
            new_events = _fetch_new_events(db, session.id, after_event_id=before_event_id)
            new_runtime_events = _fetch_new_runtime_events(
                db,
                session.id,
                after_event_id=before_runtime_event_id,
            )
            control = assess_control_flow(new_runtime_events=new_runtime_events)
            delivery = assess_prompt_delivery(
                prompt=prompt,
                exact_user_events_before=before_count,
                new_events=new_events,
            )
            actual_total = _count_exact_user_events(db, session.id, prompt)
            if actual_total != before_count + delivery.exact_user_events_in_new_batch:
                delivery = PromptDeliveryCheck(
                    ok=False,
                    exact_user_events_before=before_count,
                    exact_user_events_after=actual_total,
                    exact_user_events_in_new_batch=delivery.exact_user_events_in_new_batch,
                    assistant_messages=delivery.assistant_messages,
                    error="Prompt total count drifted after send",
                )
        now = time.monotonic()
        control_waiting = not control.ok and now < control_deadline
        delivery_waiting = (
            require_delivery
            and not delivery.ok
            and sync_status == "pending"
            and now < delivery_deadline
        )
        if not control_waiting and not delivery_waiting:
            break
        time.sleep(1.0)

    ok = status_code == 200 and sse_error is None and control.ok and (delivery.ok or not require_delivery)
    return TurnRunResult(
        index=index,
        prompt=prompt,
        ok=ok,
        http_status=status_code,
        sse_error=sse_error,
        done_payload=done_payload,
        sync_status=sync_status,
        control=control,
        delivery=delivery,
    )


def main() -> int:
    args = _parse_args()
    session_factory = _build_session_factory(args.database_url)

    with httpx.Client(timeout=args.timeout_secs) as client:
        health = client.get(f"{args.base_url.rstrip('/')}/api/health")
        if health.status_code != 200:
            raise SystemExit(f"Health check failed ({health.status_code}): {health.text[:400]}")

        session_id = args.session_id
        if not session_id:
            launch_payload = _launch_managed_local_session(
                client,
                base_url=args.base_url,
                cwd=str(Path(args.cwd).expanduser().resolve()),
                project=args.project,
                display_name=args.display_name,
                machine_name=args.machine_name,
                device_token=str(args.device_token or "").strip() or None,
                native_claude_channels=args.native_claude_channels,
            )
            session_id = str(launch_payload["session_id"])
            print(f"Launched session: {session_id}")
            print(f"Attach: {launch_payload['attach_command']}")

        prompts = build_stress_prompts(count=args.count, prefix=args.prompt_prefix)
        failures = 0

        for idx, prompt in enumerate(prompts, start=1):
            result = _run_stress_turn(
                client=client,
                base_url=args.base_url,
                session_id=session_id,
                prompt=prompt,
                index=idx,
                session_factory=session_factory,
                verification_mode=args.verification_mode,
                control_timeout_secs=args.control_timeout_secs,
                durability_timeout_secs=args.durability_timeout_secs,
            )
            assistant_summary = (
                result.delivery.assistant_messages[0][:120]
                if result.delivery.assistant_messages
                else ""
            )
            persisted_events = (
                int(result.done_payload.get("persisted_events", 0))
                if isinstance(result.done_payload, dict)
                else 0
            )
            status_label = "ok" if result.ok else "fail"
            print(
                f"[{idx}/{len(prompts)}] {status_label} "
                f"mode={args.verification_mode} "
                f"status={result.http_status} prompt={prompt!r} "
                f"control={result.control.active_phase or 'missing'}->{result.control.terminal_phase or 'missing'} "
                f"new_exact={result.delivery.exact_user_events_in_new_batch} "
                f"total_exact={result.delivery.exact_user_events_after} "
                f"persisted={persisted_events} "
                f"sync_status={result.sync_status or 'missing'}"
            )
            if assistant_summary:
                print(f"  assistant: {assistant_summary}")
            if result.sse_error:
                print(f"  sse_error: {result.sse_error}")
            if result.control.error:
                print(f"  control_error: {result.control.error}")
                if result.control.observed_phases:
                    print(f"  control_phases: {', '.join(result.control.observed_phases[:8])}")
            if result.delivery.error:
                delivery_label = "delivery_error" if args.verification_mode == "full" else "delivery_note"
                print(f"  {delivery_label}: {result.delivery.error}")

            if not result.ok:
                failures += 1
                break

            if args.delay_secs:
                time.sleep(args.delay_secs)

    if failures:
        print(f"Claude managed-local stress run failed (mode={args.verification_mode}).")
        return 1

    print(f"Claude managed-local stress run passed (mode={args.verification_mode}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
