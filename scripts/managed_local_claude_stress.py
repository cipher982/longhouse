#!/usr/bin/env python3
"""Stress a real managed-local Claude session through the local dev API.

This harness is intentionally narrow:
- local-dev only (`AUTH_DISABLED=1` expected on the target API)
- simple one-line prompts only
- repeated serial sends through the real `/api/sessions/{id}/chat` route
- DB-side verification that each prompt appears exactly once as a user event

It is meant to answer one question: can the current tmux-backed managed-local
Claude path accept repeated plain-text turns without dropped or duplicate
prompts under stress?
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
from zerg.services.session_continuity import get_machine_name_label


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
class TurnRunResult:
    index: int
    prompt: str
    ok: bool
    http_status: int
    sse_error: str | None
    done_payload: dict[str, object] | None
    delivery: PromptDeliveryCheck


def build_stress_prompts(*, count: int, prefix: str, nonce: str | None = None) -> list[str]:
    normalized_prefix = str(prefix or "").strip() or "lh-claude-stress"
    suffix = nonce or secrets.token_hex(4)
    prompts: list[str] = []
    for idx in range(1, count + 1):
        prompts.append(f"{normalized_prefix}-{idx:02d}-{suffix}")
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
    parser.add_argument("--count", type=int, default=6, help="Number of stress turns to send.")
    parser.add_argument("--delay-secs", type=float, default=0.0, help="Delay between turns.")
    parser.add_argument("--prompt-prefix", default="lh-claude-stress", help="Prompt prefix for generated messages.")
    parser.add_argument("--timeout-secs", type=float, default=60.0, help="HTTP timeout per request.")
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


def _fetch_new_events(db: Session, session_id: UUID, after_event_id: int) -> list[AgentEvent]:
    return (
        db.query(AgentEvent)
        .filter(AgentEvent.session_id == session_id)
        .filter(AgentEvent.id > after_event_id)
        .order_by(AgentEvent.id.asc())
        .all()
    )


def _launch_managed_local_session(client: httpx.Client, *, base_url: str, cwd: str, project: str, display_name: str, machine_name: str) -> dict[str, object]:
    response = client.post(
        f"{base_url.rstrip('/')}/api/sessions/managed-local/this-device",
        json={
            "cwd": cwd,
            "provider": "claude",
            "project": project,
            "display_name": display_name,
            "loop_mode": "assist",
            "machine_name": machine_name,
        },
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
) -> TurnRunResult:
    with session_factory() as db:
        session = _fetch_managed_local_claude_session(db, session_id)
        before_event_id = _latest_event_id(db, session.id)
        before_count = _count_exact_user_events(db, session.id, prompt)

    sse_error: str | None = None
    done_payload: dict[str, object] | None = None
    status_code = 0

    with client.stream(
        "POST",
        f"{base_url.rstrip('/')}/api/sessions/{session_id}/chat",
        json={"message": prompt},
    ) as response:
        status_code = response.status_code
        if response.status_code != 200:
            return TurnRunResult(
                index=index,
                prompt=prompt,
                ok=False,
                http_status=response.status_code,
                sse_error=response.text[:400],
                done_payload=None,
                delivery=PromptDeliveryCheck(
                    ok=False,
                    exact_user_events_before=before_count,
                    exact_user_events_after=before_count,
                    exact_user_events_in_new_batch=0,
                    assistant_messages=(),
                    error="Chat route did not return 200",
                ),
            )

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
                except json.JSONDecodeError:
                    done_payload = {"raw": event.data}

    with session_factory() as db:
        session = _fetch_managed_local_claude_session(db, session_id)
        new_events = _fetch_new_events(db, session.id, before_event_id)
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

    ok = status_code == 200 and sse_error is None and delivery.ok
    return TurnRunResult(
        index=index,
        prompt=prompt,
        ok=ok,
        http_status=status_code,
        sse_error=sse_error,
        done_payload=done_payload,
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
                f"status={result.http_status} prompt={prompt!r} "
                f"new_exact={result.delivery.exact_user_events_in_new_batch} "
                f"total_exact={result.delivery.exact_user_events_after} "
                f"persisted={persisted_events}"
            )
            if assistant_summary:
                print(f"  assistant: {assistant_summary}")
            if result.sse_error:
                print(f"  sse_error: {result.sse_error}")
            if result.delivery.error:
                print(f"  delivery_error: {result.delivery.error}")

            if not result.ok:
                failures += 1
                break

            if args.delay_secs:
                time.sleep(args.delay_secs)

    if failures:
        print("Claude managed-local stress run failed.")
        return 1

    print("Claude managed-local stress run passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
