#!/usr/bin/env python3
"""Empirically stress-test Codex turn submission through tmux.

This harness focuses on the exact managed-local question Longhouse cares about:
does the chosen tmux transport create one real Codex turn on a live interactive
session, and does it keep working across multiple sends on the same session?

By default it uses the new bracketed-paste transport because raw `send-keys -l`
has proven insufficient for Codex's composer. Use `--strategy literal-enter` if
you want to demonstrate the failure mode directly.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from zerg.services.managed_local_tmux import build_tmux_capture_command
from zerg.services.managed_local_tmux import build_tmux_kill_session_command
from zerg.services.managed_local_tmux import build_tmux_launch_command
from zerg.services.managed_local_tmux import build_tmux_paste_text_command
from zerg.services.managed_local_tmux import build_tmux_send_text_command

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _strip_ansi(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text).replace("\r", "")


def _run_shell(command: str, *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, shell=True, text=True, capture_output=True, env=env)


def _copy_codex_home(*, root: Path) -> Path:
    home = Path(tempfile.mkdtemp(prefix="lh-codex-home-", dir=str(root))).resolve()
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)
    for name in ("auth.json", "config.toml", "hooks.json"):
        src = Path.home() / ".codex" / name
        if src.exists():
            shutil.copy2(src, codex_dir / name)
    for directory in ("hooks", "skills"):
        src = Path.home() / ".codex" / directory
        dest = codex_dir / directory
        if not src.exists():
            continue
        if src.is_symlink():
            os.symlink(os.readlink(src), dest)
        elif src.is_dir():
            shutil.copytree(src, dest)
    (home / ".claude" / "outbox").mkdir(parents=True, exist_ok=True)
    return home


def _capture_pane(*, session_name: str, tmux_tmpdir: Path, env: dict[str, str]) -> str:
    command = build_tmux_capture_command(
        session_name=session_name,
        lines=160,
        tmux_tmpdir=str(tmux_tmpdir),
    )
    completed = _run_shell(command, env=env)
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout).strip())
    return _strip_ansi(completed.stdout)


def _parse_events(path: Path, *, env: dict[str, str]) -> list[dict]:
    completed = subprocess.run(
        ["longhouse-engine", "parse", "--dump-events", str(path)],
        text=True,
        capture_output=True,
        env=env,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout).strip())
    rows: list[dict] = []
    for line in completed.stdout.splitlines():
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "raw_type" in obj:
            rows.append(obj)
    return rows


def _wait_for_idle_prompt(*, session_name: str, tmux_tmpdir: Path, env: dict[str, str], timeout_secs: float) -> str:
    deadline = time.monotonic() + timeout_secs
    last_pane = ""
    while time.monotonic() < deadline:
        pane = _capture_pane(session_name=session_name, tmux_tmpdir=tmux_tmpdir, env=env)
        last_pane = pane
        if "OpenAI Codex" in pane and "Starting MCP servers" not in pane and "Loading conversation history" not in pane:
            return pane
        time.sleep(1.0)
    raise RuntimeError(f"Codex never reached idle.\nLast pane:\n{last_pane[-4000:]}")


def _send_prompt(
    *,
    strategy: str,
    session_name: str,
    tmux_tmpdir: Path,
    env: dict[str, str],
    prompt: str,
) -> None:
    if strategy == "pastep-enter":
        command = build_tmux_paste_text_command(
            session_name=session_name,
            text=prompt,
            tmux_tmpdir=str(tmux_tmpdir),
        )
    elif strategy == "literal-enter":
        command = build_tmux_send_text_command(
            session_name=session_name,
            text=prompt,
            tmux_tmpdir=str(tmux_tmpdir),
        )
    else:
        raise ValueError(f"Unsupported strategy: {strategy}")
    completed = _run_shell(command, env=env)
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout).strip())


def _wait_for_rollout_file(*, sessions_root: Path, timeout_secs: float) -> Path:
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        files = sorted(sessions_root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime) if sessions_root.exists() else []
        if files:
            return files[-1]
        time.sleep(1.0)
    raise RuntimeError("Codex never created a rollout file after submit")


def _wait_for_turn(
    *,
    rollout_path: Path,
    token: str,
    env: dict[str, str],
    timeout_secs: float,
) -> tuple[dict, dict]:
    deadline = time.monotonic() + timeout_secs
    last_events: list[dict] = []
    while time.monotonic() < deadline:
        events = _parse_events(rollout_path, env=env)
        last_events = events
        user_event = next(
            (row for row in events if row.get("raw_type") == "codex_user" and token in row.get("content_text", "")),
            None,
        )
        assistant_event = next(
            (
                row
                for row in events
                if row.get("raw_type") == "codex_assistant" and row.get("content_text") == token
            ),
            None,
        )
        if user_event and assistant_event:
            return user_event, assistant_event
        time.sleep(1.0)
    raise RuntimeError(
        f"Timed out waiting for Codex token {token}.\n"
        f"Last parsed event types: {[row.get('raw_type') for row in last_events[-10:]]}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stress-test real Codex tmux turn submission.")
    parser.add_argument(
        "--workspace",
        default=str(Path.cwd()),
        help="Workspace to launch Codex in (default: current directory).",
    )
    parser.add_argument(
        "--turns",
        type=int,
        default=3,
        help="Number of sequential prompts to submit (default: 3).",
    )
    parser.add_argument(
        "--strategy",
        choices=("pastep-enter", "literal-enter"),
        default="pastep-enter",
        help="Submission strategy to test (default: pastep-enter).",
    )
    parser.add_argument(
        "--ready-timeout",
        type=float,
        default=30.0,
        help="Seconds to wait for Codex to reach an idle prompt (default: 30).",
    )
    parser.add_argument(
        "--turn-timeout",
        type=float,
        default=30.0,
        help="Seconds to wait for each turn to parse fully (default: 30).",
    )
    parser.add_argument(
        "--keep-home",
        action="store_true",
        help="Keep the temporary HOME directory for debugging.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        raise SystemExit(f"Workspace does not exist: {workspace}")

    scratch_root = Path("/tmp")
    home = _copy_codex_home(root=scratch_root)
    tmux_root = Path(tempfile.mkdtemp(prefix="cxt-", dir=str(scratch_root))).resolve()
    tmux_tmpdir = tmux_root / "t"
    tmux_tmpdir.mkdir(parents=True, exist_ok=True)
    session_name = f"lh-codex-{int(time.time()) % 100000}"
    env = os.environ.copy()
    env["HOME"] = str(home)

    launch_command = "zsh -lc 'source ~/.zshrc >/dev/null 2>&1 || true; exec codex --enable codex_hooks'"
    launch = build_tmux_launch_command(
        session_name=session_name,
        cwd=str(workspace),
        launch_command=launch_command,
        tmux_tmpdir=str(tmux_tmpdir),
    )
    completed = _run_shell(launch, env=env)
    if completed.returncode != 0:
        raise SystemExit((completed.stderr or completed.stdout).strip())

    sessions_root = home / ".codex" / "sessions"
    print(f"home={home}")
    print(f"session_name={session_name}")
    print(f"strategy={args.strategy}")

    try:
        _wait_for_idle_prompt(
            session_name=session_name,
            tmux_tmpdir=tmux_tmpdir,
            env=env,
            timeout_secs=args.ready_timeout,
        )
        rollout_path: Path | None = None
        for turn in range(1, args.turns + 1):
            token = f"LH_CODEX_TMUX_{turn}_{random.randint(10000, 99999)}"
            prompt = f"Reply with exactly {token} and nothing else."
            print(f"turn={turn} sending token={token}")
            _send_prompt(
                strategy=args.strategy,
                session_name=session_name,
                tmux_tmpdir=tmux_tmpdir,
                env=env,
                prompt=prompt,
            )
            if rollout_path is None:
                rollout_path = _wait_for_rollout_file(sessions_root=sessions_root, timeout_secs=10.0)
                print(f"rollout={rollout_path}")
            user_event, assistant_event = _wait_for_turn(
                rollout_path=rollout_path,
                token=token,
                env=env,
                timeout_secs=args.turn_timeout,
            )
            print(
                "turn={} ok session_id={} user_offset={} assistant_offset={}".format(
                    turn,
                    assistant_event.get("session_id"),
                    user_event.get("source_offset"),
                    assistant_event.get("source_offset"),
                )
            )
            _wait_for_idle_prompt(
                session_name=session_name,
                tmux_tmpdir=tmux_tmpdir,
                env=env,
                timeout_secs=args.ready_timeout,
            )

        print("success=1")
        print("final_pane_tail:")
        print(_capture_pane(session_name=session_name, tmux_tmpdir=tmux_tmpdir, env=env)[-2000:])
        return 0
    finally:
        _run_shell(
            build_tmux_kill_session_command(session_name=session_name, tmux_tmpdir=str(tmux_tmpdir)),
            env=env,
        )
        shutil.rmtree(tmux_root, ignore_errors=True)
        if not args.keep_home:
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
