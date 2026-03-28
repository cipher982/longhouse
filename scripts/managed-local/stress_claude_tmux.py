#!/usr/bin/env python3
"""Empirically stress-test Claude Code turn submission through tmux.

This harness focuses on the exact question Longhouse cares about:
does `tmux send-keys -l <text>` + `Enter` create one real Claude turn on a
live interactive Claude Code session, and does that keep working across
multiple sends on the same session?

It launches a detached tmux session running `claude-code --session-id ...`,
waits until Claude is actually at a usable prompt, submits a sequence of
simple one-line prompts with unique tokens, and validates the resulting
transcript and pane output.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _now() -> float:
    return time.monotonic()


def _encode_cwd_for_claude(absolute_path: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]", "-", absolute_path)


def _strip_ansi(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text).replace("\r", "")


def _run_tmux(
    *args: str,
    tmux_tmpdir: Path,
    check: bool = True,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["TMUX_TMPDIR"] = str(tmux_tmpdir)
    completed = subprocess.run(
        ["tmux", *args],
        check=False,
        capture_output=capture_output,
        text=True,
        env=env,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(
            f"tmux {' '.join(args)} failed with {completed.returncode}: "
            f"{(completed.stderr or completed.stdout).strip()}"
        )
    return completed


def _capture_pane(*, session_name: str, tmux_tmpdir: Path) -> str:
    completed = _run_tmux("capture-pane", "-p", "-t", session_name, tmux_tmpdir=tmux_tmpdir)
    return _strip_ansi(completed.stdout)


def _send_keys(*, session_name: str, tmux_tmpdir: Path, literal: str | None = None, key: str | None = None) -> None:
    if (literal is None) == (key is None):
        raise ValueError("Specify exactly one of literal or key")
    if literal is not None:
        _run_tmux("send-keys", "-t", session_name, "-l", literal, tmux_tmpdir=tmux_tmpdir)
        return
    _run_tmux("send-keys", "-t", session_name, key or "", tmux_tmpdir=tmux_tmpdir)


def _load_transcript(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def _assistant_texts(rows: list[dict[str, Any]]) -> list[str]:
    texts: list[str] = []
    for row in rows:
        if row.get("type") != "assistant":
            continue
        message = row.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        parts: list[str] = []
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
        elif isinstance(content, str):
            parts.append(content)
        text = "".join(parts).strip()
        if text:
            texts.append(text)
    return texts


@dataclass(frozen=True)
class TurnResult:
    prompt: str
    token: str
    user_count: int
    assistant_exact_count: int
    pane_contains_token: bool


@dataclass(frozen=True)
class ClaudeHarnessContext:
    workspace: Path
    claude_config_dir: Path
    session_name: str
    provider_session_id: str
    transcript_path: Path
    tmux_tmpdir: Path
    cleanup_config_dir: bool


def _build_entry_command(*, provider_session_id: str, claude_config_dir: Path, display_name: str) -> str:
    parts = ["claude-code", "--session-id", provider_session_id, "-n", display_name]
    inner = (
        f"export CLAUDE_CONFIG_DIR={shlex.quote(str(claude_config_dir))}; "
        "source ~/.zshrc >/dev/null 2>&1; "
        "exec "
        + " ".join(shlex.quote(part) for part in parts)
    )
    return f"zsh -lc {shlex.quote(inner)}"


def _launch_session(context: ClaudeHarnessContext) -> None:
    command = _build_entry_command(
        provider_session_id=context.provider_session_id,
        claude_config_dir=context.claude_config_dir,
        display_name="Longhouse Claude tmux stress",
    )
    _run_tmux(
        "new-session",
        "-d",
        "-s",
        context.session_name,
        "-c",
        str(context.workspace),
        command,
        tmux_tmpdir=context.tmux_tmpdir,
    )


def _handle_known_gate(pane: str, *, session_name: str, tmux_tmpdir: Path) -> str | None:
    if "Choose the text style" in pane:
        _send_keys(session_name=session_name, tmux_tmpdir=tmux_tmpdir, key="Enter")
        return "theme"
    if "Press Enter to continue" in pane:
        _send_keys(session_name=session_name, tmux_tmpdir=tmux_tmpdir, key="Enter")
        return "security_notes"
    if "Yes, I trust this folder" in pane:
        _send_keys(session_name=session_name, tmux_tmpdir=tmux_tmpdir, key="Enter")
        return "trust_workspace"
    if "Bypass Permissions mode" in pane and "Yes, I accept" in pane:
        _send_keys(session_name=session_name, tmux_tmpdir=tmux_tmpdir, key="2")
        _send_keys(session_name=session_name, tmux_tmpdir=tmux_tmpdir, key="Enter")
        return "bypass_permissions"
    return None


def _pane_is_ready(pane: str) -> bool:
    if "❯" not in pane:
        return False
    blocked_markers = (
        "Choose the text style",
        "Press Enter to continue",
        "Yes, I trust this folder",
        "Bypass Permissions mode",
    )
    return not any(marker in pane for marker in blocked_markers)


def _wait_for_ready_prompt(
    *,
    session_name: str,
    tmux_tmpdir: Path,
    timeout_secs: float,
) -> tuple[str, list[str]]:
    deadline = _now() + timeout_secs
    handled: list[str] = []
    last_pane = ""
    while _now() < deadline:
        pane = _capture_pane(session_name=session_name, tmux_tmpdir=tmux_tmpdir)
        last_pane = pane
        action = _handle_known_gate(pane, session_name=session_name, tmux_tmpdir=tmux_tmpdir)
        if action is not None:
            handled.append(action)
            time.sleep(2.0)
            continue
        if _pane_is_ready(pane):
            return pane, handled
        time.sleep(1.0)
    raise RuntimeError(
        "Claude never reached an idle prompt.\n"
        f"Handled gates: {handled}\n"
        f"Last pane:\n{last_pane[-4000:]}"
    )


def _wait_for_turn(
    *,
    context: ClaudeHarnessContext,
    prompt: str,
    token: str,
    timeout_secs: float,
) -> TurnResult:
    deadline = _now() + timeout_secs
    last_pane = ""
    while _now() < deadline:
        pane = _capture_pane(session_name=context.session_name, tmux_tmpdir=context.tmux_tmpdir)
        last_pane = pane
        rows = _load_transcript(context.transcript_path)
        user_count = sum(
            1
            for row in rows
            if row.get("type") == "user"
            and isinstance(row.get("message"), dict)
            and row["message"].get("content") == prompt
        )
        assistant_exact_count = sum(1 for text in _assistant_texts(rows) if text == token)
        pane_contains_token = token in pane
        if user_count == 1 and assistant_exact_count == 1 and pane_contains_token:
            return TurnResult(
                prompt=prompt,
                token=token,
                user_count=user_count,
                assistant_exact_count=assistant_exact_count,
                pane_contains_token=pane_contains_token,
            )
        if user_count > 1 or assistant_exact_count > 1:
            raise RuntimeError(
                f"Observed duplicate transcript records for token {token}: "
                f"user_count={user_count} assistant_exact_count={assistant_exact_count}\n"
                f"Transcript: {context.transcript_path}\n"
                f"Last pane:\n{last_pane[-4000:]}"
            )
        time.sleep(1.0)
    raise RuntimeError(
        f"Timed out waiting for Claude to complete token {token}.\n"
        f"Transcript: {context.transcript_path}\n"
        f"Last pane:\n{last_pane[-4000:]}"
    )


def _kill_session(context: ClaudeHarnessContext) -> None:
    _run_tmux("kill-session", "-t", context.session_name, tmux_tmpdir=context.tmux_tmpdir, check=False)


def _cleanup(context: ClaudeHarnessContext, *, keep_session: bool, keep_tmux_tmpdir: bool) -> None:
    if not keep_session:
        _kill_session(context)
    if not keep_tmux_tmpdir and context.tmux_tmpdir.parent.exists():
        shutil.rmtree(context.tmux_tmpdir.parent, ignore_errors=True)
    if context.cleanup_config_dir:
        shutil.rmtree(context.claude_config_dir, ignore_errors=True)


def _make_context(args: argparse.Namespace) -> ClaudeHarnessContext:
    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        raise SystemExit(f"Workspace does not exist: {workspace}")

    if args.fresh_config and args.claude_config_dir:
        raise SystemExit("Use either --fresh-config or --claude-config-dir, not both")

    cleanup_config_dir = False
    if args.fresh_config:
        claude_config_dir = Path(tempfile.mkdtemp(prefix="lh-claude-config-")).resolve()
        cleanup_config_dir = True
    elif args.claude_config_dir:
        claude_config_dir = Path(args.claude_config_dir).expanduser().resolve()
        claude_config_dir.mkdir(parents=True, exist_ok=True)
    else:
        claude_config_dir = Path.home() / ".claude"

    provider_session_id = str(uuid.uuid4())
    encoded_workspace = _encode_cwd_for_claude(str(workspace))
    transcript_path = claude_config_dir / "projects" / encoded_workspace / f"{provider_session_id}.jsonl"
    transcript_path.parent.mkdir(parents=True, exist_ok=True)

    tmux_root = Path(tempfile.mkdtemp(prefix="lh-claude-tmux-")).resolve()
    tmux_tmpdir = tmux_root / "tmux"
    tmux_tmpdir.mkdir(parents=True, exist_ok=True)

    return ClaudeHarnessContext(
        workspace=workspace,
        claude_config_dir=claude_config_dir,
        session_name=args.session_name or f"lh-claude-stress-{provider_session_id.split('-')[0]}",
        provider_session_id=provider_session_id,
        transcript_path=transcript_path,
        tmux_tmpdir=tmux_tmpdir,
        cleanup_config_dir=cleanup_config_dir,
    )


def _require_binary(name: str) -> None:
    if shutil.which(name) is None:
        raise SystemExit(f"Required binary not found: {name}")


def _build_prompts(turns: int) -> list[tuple[str, str]]:
    prompts: list[tuple[str, str]] = []
    for idx in range(1, turns + 1):
        token = f"LH_TMUX_TURN_{idx}_{random.randint(10000, 99999)}"
        prompt = f"Reply with exactly {token} on the first line and nothing else."
        prompts.append((prompt, token))
    return prompts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stress-test real Claude tmux turn submission.")
    parser.add_argument(
        "--workspace",
        default=str(Path.cwd()),
        help="Workspace to launch Claude in (default: current directory).",
    )
    parser.add_argument(
        "--turns",
        type=int,
        default=3,
        help="Number of sequential prompts to submit (default: 3).",
    )
    parser.add_argument(
        "--ready-timeout",
        type=float,
        default=45.0,
        help="Seconds to wait for Claude to reach a usable prompt (default: 45).",
    )
    parser.add_argument(
        "--turn-timeout",
        type=float,
        default=45.0,
        help="Seconds to wait for each turn to appear in transcript + pane (default: 45).",
    )
    parser.add_argument(
        "--claude-config-dir",
        default=None,
        help="Claude config dir to use instead of ~/.claude.",
    )
    parser.add_argument(
        "--fresh-config",
        action="store_true",
        help="Use a fresh temporary Claude config dir and auto-handle first-run prompts.",
    )
    parser.add_argument(
        "--session-name",
        default=None,
        help="Override the tmux session name.",
    )
    parser.add_argument(
        "--keep-session",
        action="store_true",
        help="Leave the tmux session running after the harness exits.",
    )
    parser.add_argument(
        "--keep-tmux-tmpdir",
        action="store_true",
        help="Keep the temporary TMUX_TMPDIR root for debugging.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    _require_binary("tmux")

    context = _make_context(args)
    print(f"workspace={context.workspace}")
    print(f"claude_config_dir={context.claude_config_dir}")
    print(f"session_name={context.session_name}")
    print(f"provider_session_id={context.provider_session_id}")
    print(f"transcript_path={context.transcript_path}")

    try:
        _launch_session(context)
        ready_pane, handled = _wait_for_ready_prompt(
            session_name=context.session_name,
            tmux_tmpdir=context.tmux_tmpdir,
            timeout_secs=args.ready_timeout,
        )
        print(f"ready=1 handled_gates={','.join(handled) if handled else 'none'}")

        results: list[TurnResult] = []
        for idx, (prompt, token) in enumerate(_build_prompts(args.turns), start=1):
            print(f"turn={idx} sending token={token}")
            _send_keys(session_name=context.session_name, tmux_tmpdir=context.tmux_tmpdir, literal=prompt)
            _send_keys(session_name=context.session_name, tmux_tmpdir=context.tmux_tmpdir, key="Enter")
            result = _wait_for_turn(context=context, prompt=prompt, token=token, timeout_secs=args.turn_timeout)
            results.append(result)
            _wait_for_ready_prompt(
                session_name=context.session_name,
                tmux_tmpdir=context.tmux_tmpdir,
                timeout_secs=args.ready_timeout,
            )
            print(
                f"turn={idx} ok token={token} "
                f"user_count={result.user_count} assistant_exact_count={result.assistant_exact_count}"
            )

        print(f"success=1 turns={len(results)} transcript={context.transcript_path}")
        print("final_pane_tail:")
        print(_capture_pane(session_name=context.session_name, tmux_tmpdir=context.tmux_tmpdir)[-2000:])
        return 0
    finally:
        _cleanup(
            context,
            keep_session=args.keep_session,
            keep_tmux_tmpdir=args.keep_tmux_tmpdir,
        )


if __name__ == "__main__":
    raise SystemExit(main())
