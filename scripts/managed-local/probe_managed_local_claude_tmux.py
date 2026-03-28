#!/usr/bin/env python3
"""Directly probe Claude tmux input semantics using the real transcript JSONL.

This script avoids the Longhouse backend entirely. It launches a fresh
interactive `claude-code` session inside tmux using the same command builders
that managed-local launch/send paths use, sends repeated one-line prompts, and
verifies the resulting Claude transcript on disk.
"""

from __future__ import annotations

import argparse
import json
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "server"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from zerg.services.managed_local_tmux import build_tmux_attach_command
from zerg.services.managed_local_tmux import build_tmux_capture_command
from zerg.services.managed_local_tmux import build_tmux_has_session_command
from zerg.services.managed_local_tmux import build_tmux_kill_session_command
from zerg.services.managed_local_tmux import build_tmux_launch_command
from zerg.services.managed_local_tmux import build_tmux_send_text_command
from zerg.services.managed_local_tmux import normalize_tmux_session_name
from zerg.services.session_continuity import encode_cwd_for_claude
from zerg.services.session_continuity import get_claude_config_dir


@dataclass(frozen=True)
class TranscriptEvent:
    raw_type: str
    text: str


@dataclass(frozen=True)
class ProbeTurnResult:
    index: int
    prompt: str
    token: str
    user_events_before: int
    user_events_after: int
    assistant_text: str | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", type=Path, default=None, help="Workspace to launch Claude in (defaults to a temp dir).")
    parser.add_argument(
        "--claude-config-dir",
        type=Path,
        default=None,
        help="Claude config directory (defaults to CLAUDE_CONFIG_DIR or ~/.claude).",
    )
    parser.add_argument("--count", type=int, default=3, help="Number of prompts to send.")
    parser.add_argument("--display-name", default="Managed Local Claude Probe", help="Claude session display name.")
    parser.add_argument("--prompt-prefix", default="lh-claude-probe", help="Prefix for generated unique prompts.")
    parser.add_argument("--startup-timeout-secs", type=float, default=30.0, help="Timeout waiting for Claude to start.")
    parser.add_argument("--turn-timeout-secs", type=float, default=90.0, help="Timeout waiting for each turn to land.")
    parser.add_argument("--delay-secs", type=float, default=0.0, help="Delay between successful prompts.")
    parser.add_argument("--capture-lines", type=int, default=120, help="Pane lines to capture on debug output.")
    parser.add_argument("--tmux-tmpdir", default=None, help="Optional TMUX_TMPDIR override.")
    parser.add_argument("--keep-session", action="store_true", help="Leave the tmux session running for manual attach.")
    parser.add_argument("--keep-workspace", action="store_true", help="Keep the temp workspace if one was created.")
    args = parser.parse_args()

    if args.count <= 0:
        parser.error("--count must be positive")
    if args.delay_secs < 0:
        parser.error("--delay-secs must be non-negative")
    if args.startup_timeout_secs <= 0 or args.turn_timeout_secs <= 0:
        parser.error("timeouts must be positive")
    return args


def _run_shell(command: str, *, timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        shell=True,
        executable="/bin/zsh",
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _read_transcript_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def _iter_transcript_events(lines: Iterable[str]) -> Iterable[TranscriptEvent]:
    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        raw_type = str(obj.get("type") or "")
        if raw_type == "user":
            content = (obj.get("message") or {}).get("content")
            if isinstance(content, str):
                yield TranscriptEvent(raw_type=raw_type, text=content)
        elif raw_type == "assistant":
            message = obj.get("message") or {}
            content = message.get("content")
            if isinstance(content, str):
                yield TranscriptEvent(raw_type=raw_type, text=content)
                continue
            if not isinstance(content, list):
                continue
            parts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    text = str(item.get("text") or "")
                    if text:
                        parts.append(text)
            if parts:
                yield TranscriptEvent(raw_type=raw_type, text="\n".join(parts))


def _count_exact_user_events(lines: Iterable[str], prompt: str) -> int:
    return sum(1 for event in _iter_transcript_events(lines) if event.raw_type == "user" and event.text == prompt)


def _find_assistant_token(lines: Iterable[str], token: str) -> str | None:
    for event in reversed(list(_iter_transcript_events(lines))):
        if event.raw_type == "assistant" and token in event.text:
            return event.text
    return None


def _capture_pane(session_name: str, *, lines: int, tmux_tmpdir: str | None) -> str:
    completed = _run_shell(
        build_tmux_capture_command(session_name=session_name, lines=lines, tmux_tmpdir=tmux_tmpdir),
        timeout=15.0,
    )
    return (completed.stdout or completed.stderr or "").strip()


def _has_session(session_name: str, *, tmux_tmpdir: str | None) -> bool:
    completed = _run_shell(
        build_tmux_has_session_command(session_name=session_name, tmux_tmpdir=tmux_tmpdir),
        timeout=10.0,
    )
    return completed.returncode == 0


def _looks_blocked(capture: str) -> str | None:
    text = capture.lower()
    if "choose a theme" in text or "select a theme" in text:
        return "Claude is waiting on a theme-selection screen"
    if "run /login" in text or "not logged in" in text:
        return "Claude is not logged in"
    if "quick safety check" in text or "yes, i trust this folder" in text:
        return "Claude is waiting on the workspace trust confirmation screen"
    if "continue with" in text and "enter to confirm" in text:
        return "Claude appears to be on an onboarding/confirmation screen"
    return None


def _looks_ready(capture: str) -> bool:
    text = capture.lower()
    if _looks_blocked(capture):
        return False
    return any(
        marker in text
        for marker in [
            "claude code v",
            'try "',
            "bypass permissions",
            "\n❯",
            "❯ ",
        ]
    )


def _build_entry_command(*, session_id: str, display_name: str, claude_config_dir: Path | None) -> str:
    parts = ["claude-code", "--session-id", session_id]
    if display_name.strip():
        parts.extend(["-n", display_name.strip()])
    setup: list[str] = []
    if claude_config_dir is not None:
        setup.append(f"export CLAUDE_CONFIG_DIR={shlex.quote(str(claude_config_dir))}")
    setup.append("source ~/.zshrc >/dev/null 2>&1")
    setup.append("exec " + " ".join(shlex.quote(part) for part in parts))
    inner = "; ".join(setup)
    return f"zsh -lc {shlex.quote(inner)}"


def _build_prompts(*, count: int, prefix: str) -> list[tuple[str, str]]:
    prompts: list[tuple[str, str]] = []
    for index in range(1, count + 1):
        token = f"{prefix}-{index:02d}-{secrets.token_hex(3)}"
        prompts.append((f"Reply with exactly {token} and nothing else.", token))
    return prompts


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    args = _parse_args()

    created_workspace = args.cwd is None
    workspace = args.cwd.expanduser().resolve() if args.cwd else Path(tempfile.mkdtemp(prefix="lh-claude-probe-"))
    workspace.mkdir(parents=True, exist_ok=True)

    claude_config_dir = (args.claude_config_dir.expanduser().resolve() if args.claude_config_dir else get_claude_config_dir())
    session_id = str(uuid4())
    session_name = normalize_tmux_session_name(f"managed-local-claude-probe-{session_id[:8]}", prefix="lh")
    transcript = claude_config_dir / "projects" / encode_cwd_for_claude(str(workspace)) / f"{session_id}.jsonl"

    launch_command = build_tmux_launch_command(
        session_name=session_name,
        cwd=str(workspace),
        launch_command=_build_entry_command(
            session_id=session_id,
            display_name=args.display_name,
            claude_config_dir=claude_config_dir if args.claude_config_dir else None,
        ),
        tmux_tmpdir=args.tmux_tmpdir,
    )
    attach_command = build_tmux_attach_command(session_name=session_name, tmux_tmpdir=args.tmux_tmpdir)
    kill_command = build_tmux_kill_session_command(session_name=session_name, tmux_tmpdir=args.tmux_tmpdir)

    print(f"workspace={workspace}")
    print(f"claude_config_dir={claude_config_dir}")
    print(f"session_id={session_id}")
    print(f"tmux_session={session_name}")
    print(f"transcript={transcript}")
    print(f"attach={attach_command}")

    launch = _run_shell(launch_command, timeout=20.0)
    if launch.returncode != 0:
        print(launch.stderr.strip() or launch.stdout.strip() or "tmux launch failed", file=sys.stderr)
        return 1

    try:
        deadline = time.time() + args.startup_timeout_secs
        blocking_reason: str | None = None
        startup_ready = False
        while time.time() < deadline:
            if not _has_session(session_name, tmux_tmpdir=args.tmux_tmpdir):
                time.sleep(0.25)
                continue
            capture = _capture_pane(session_name, lines=args.capture_lines, tmux_tmpdir=args.tmux_tmpdir)
            blocking_reason = _looks_blocked(capture)
            startup_ready = _looks_ready(capture)
            if transcript.exists() and transcript.stat().st_size > 0:
                break
            if blocking_reason:
                break
            if startup_ready:
                break
            time.sleep(0.5)

        if blocking_reason:
            print(f"startup_blocked={blocking_reason}", file=sys.stderr)
            print(_capture_pane(session_name, lines=args.capture_lines, tmux_tmpdir=args.tmux_tmpdir), file=sys.stderr)
            return 1
        if not startup_ready and (not transcript.exists() or transcript.stat().st_size <= 0):
            print("Claude never reached a ready prompt during startup.", file=sys.stderr)
            print(_capture_pane(session_name, lines=args.capture_lines, tmux_tmpdir=args.tmux_tmpdir), file=sys.stderr)
            return 1

        failures = 0
        for result in _run_probe_turns(
            session_name=session_name,
            transcript=transcript,
            count=args.count,
            prefix=args.prompt_prefix,
            turn_timeout_secs=args.turn_timeout_secs,
            capture_lines=args.capture_lines,
            delay_secs=args.delay_secs,
            tmux_tmpdir=args.tmux_tmpdir,
        ):
            status = "ok" if result.ok else "fail"
            print(
                f"[{result.index}] {status} prompt={result.prompt!r} "
                f"user_before={result.user_events_before} user_after={result.user_events_after}"
            )
            if result.assistant_text:
                preview = result.assistant_text[:200].replace("\n", "\\n")
                print(f"  assistant={preview}")
            if result.error:
                print(f"  error={result.error}")
                failures += 1
                break

        if failures:
            print("Claude tmux probe failed.", file=sys.stderr)
            print(_capture_pane(session_name, lines=args.capture_lines, tmux_tmpdir=args.tmux_tmpdir), file=sys.stderr)
            return 1

        print("Claude tmux probe passed.")
        return 0
    finally:
        if not args.keep_session:
            _run_shell(kill_command, timeout=10.0)
        if created_workspace and not args.keep_workspace:
            shutil.rmtree(workspace, ignore_errors=True)


def _run_probe_turns(
    *,
    session_name: str,
    transcript: Path,
    count: int,
    prefix: str,
    turn_timeout_secs: float,
    capture_lines: int,
    delay_secs: float,
    tmux_tmpdir: str | None,
) -> Iterable[ProbeTurnResult]:
    for index, (prompt, token) in enumerate(_build_prompts(count=count, prefix=prefix), start=1):
        before_lines = _read_transcript_lines(transcript)
        before_count = _count_exact_user_events(before_lines, prompt)

        send = _run_shell(
            build_tmux_send_text_command(session_name=session_name, text=prompt, tmux_tmpdir=tmux_tmpdir),
            timeout=20.0,
        )
        if send.returncode != 0:
            yield ProbeTurnResult(
                index=index,
                prompt=prompt,
                token=token,
                user_events_before=before_count,
                user_events_after=before_count,
                assistant_text=None,
                error=send.stderr.strip() or send.stdout.strip() or "tmux send failed",
            )
            return

        deadline = time.time() + turn_timeout_secs
        final_lines = before_lines
        assistant_text: str | None = None
        user_after = before_count
        error: str | None = None

        while time.time() < deadline:
            final_lines = _read_transcript_lines(transcript)
            user_after = _count_exact_user_events(final_lines, prompt)
            assistant_text = _find_assistant_token(final_lines, token)
            if user_after > before_count and assistant_text:
                break
            time.sleep(1.0)

        if user_after == before_count:
            error = "prompt never appeared as a new user event"
        elif user_after > before_count + 1:
            error = "prompt appeared more than once as a user event"
        elif assistant_text is None:
            blocked = _looks_blocked(_capture_pane(session_name, lines=capture_lines, tmux_tmpdir=tmux_tmpdir))
            error = blocked or f"assistant never emitted token {token}"

        yield ProbeTurnResult(
            index=index,
            prompt=prompt,
            token=token,
            user_events_before=before_count,
            user_events_after=user_after,
            assistant_text=assistant_text,
            error=error,
        )
        if error:
            return
        if delay_secs:
            time.sleep(delay_secs)


if __name__ == "__main__":
    raise SystemExit(main())
