"""Live managed-Claude conversation-reset characterization canary."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from zerg.qa.conversation_reset import classify_identity_transition
from zerg.qa.conversation_reset import execution_summary
from zerg.qa.conversation_reset import longhouse_source_binding
from zerg.qa.conversation_reset import marker_digest
from zerg.qa.conversation_reset import observation_exit_code
from zerg.qa.conversation_reset import tail_sequence
from zerg.qa.managed_claude_live import strip_terminal_controls
from zerg.qa.pty_session import ProviderPtySession

_SESSION_ID = re.compile(r"Session ID:\s*([0-9a-fA-F-]{36})")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _terminal_text(path: Path) -> str:
    try:
        return strip_terminal_controls(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""


def _wait(predicate, *, timeout: float, message: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.2)
    raise RuntimeError(message)


def _state_path(session_id: str) -> Path:
    return Path.home() / ".claude" / "channels" / "longhouse" / "sessions" / f"{session_id}.json"


def _transcript_paths() -> list[Path]:
    return list((Path.home() / ".claude" / "projects").glob("**/*.jsonl"))


def _assistant_marker_path(marker: str, *, started_at: float) -> Path | None:
    for path in _transcript_paths():
        try:
            if path.stat().st_mtime < started_at:
                continue
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                row = json.loads(line)
                if row.get("type") == "assistant" and marker in json.dumps(row.get("message"), ensure_ascii=False):
                    return path
        except (OSError, json.JSONDecodeError):
            continue
    return None


def _tail(session_id: str) -> dict[str, Any] | None:
    result = subprocess.run(
        ["longhouse-server", "tail", session_id, "--limit", "100", "--json"],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _observe_longhouse_tail(session_id: str, marker_a: str, marker_b: str, *, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        payload = _tail(session_id)
        if payload:
            last = payload
            raw = json.dumps(payload, ensure_ascii=False)
            if marker_a in raw and marker_b in raw:
                break
        time.sleep(1.0)
    return last


def run(args: argparse.Namespace) -> dict[str, Any]:
    provider_bin = Path(args.provider_bin or os.environ.get("LONGHOUSE_CLAUDE_BIN") or "claude").expanduser()
    if not provider_bin.is_absolute():
        resolved = subprocess.run(["/usr/bin/which", str(provider_bin)], text=True, capture_output=True, check=False).stdout.strip()
        if not resolved:
            raise RuntimeError(f"Claude binary not found: {provider_bin}")
        provider_bin = Path(resolved)
    provider_bin = provider_bin.resolve(strict=True)
    version_result = subprocess.run([str(provider_bin), "--version"], text=True, capture_output=True, timeout=15, check=False)
    if version_result.returncode != 0:
        raise RuntimeError("Claude version probe failed")
    version = version_result.stdout.strip().splitlines()[0].split()[0]
    output_root = Path(args.artifact_root).expanduser().resolve() / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_root.mkdir(parents=True)
    workspace = Path(args.cwd).expanduser().resolve(strict=True)
    terminal_path = output_root / "terminal.raw"
    invocation = uuid4().hex
    marker_a = f"LONGHOUSE_RESET_CLAUDE_A_{invocation}"
    marker_b = f"LONGHOUSE_RESET_CLAUDE_B_{invocation}"
    prompt_a = f"Reply with exactly {marker_a}"
    prompt_b = f"Reply with exactly {marker_b}"
    started_at = time.time()
    env = os.environ.copy()
    env["LONGHOUSE_CLAUDE_BIN"] = str(provider_bin)
    if args.model:
        env["ANTHROPIC_MODEL"] = args.model
    session = ProviderPtySession.start(
        argv=[
            "longhouse",
            "claude",
            "--cwd",
            str(workspace),
            "--project",
            args.project,
            "--name",
            "Claude conversation reset qualification",
        ],
        cwd=workspace,
        env=env,
        terminal_path=terminal_path,
        thread_name="claude-conversation-reset-terminal-drain",
    )
    observation: dict[str, Any] | None = None
    try:
        confirmed_trust = False
        confirmed_channel = False

        def launched_state():
            nonlocal confirmed_trust, confirmed_channel
            if not session.alive():
                raise RuntimeError(f"longhouse claude exited during launch ({session.process.returncode})")
            text = _terminal_text(terminal_path)
            compact = re.sub(r"\s+", "", text)
            if not confirmed_trust and "Yes,Itrustthisfolder" in compact:
                session.write(b"\r")
                confirmed_trust = True
            if not confirmed_channel and "Iamusingthisforlocaldevelopment" in compact:
                session.write(b"\r")
                confirmed_channel = True
            state_root = Path.home() / ".claude" / "channels" / "longhouse" / "sessions"
            for path in state_root.glob("*.json"):
                try:
                    if path.stat().st_mtime < started_at - 1:
                        continue
                except OSError:
                    continue
                candidate = _read_json(path)
                if (
                    candidate
                    and candidate.get("ready") is True
                    and Path(str(candidate.get("cwd") or "")).resolve() == workspace
                    and str(candidate.get("provider_session_id") or "")
                ):
                    return path.stem, candidate, path
            match = _SESSION_ID.search(text)
            if not match:
                return None
            state = _read_json(_state_path(match.group(1)))
            path = _state_path(match.group(1))
            return (match.group(1), state, path) if state and state.get("ready") else None

        longhouse_session_id, state, state_path = _wait(
            launched_state,
            timeout=args.timeout,
            message="Claude managed session did not become ready",
        )
        before_provider_id = str(state.get("provider_session_id") or "")
        provider_pid = str(state.get("claude_pid") or session.process.pid)
        run_id = str(state.get("run_id") or "")
        session.submit_line(prompt_a)
        before_path = _wait(
            lambda: _assistant_marker_path(marker_a, started_at=started_at),
            timeout=args.timeout,
            message="Claude marker A assistant response was not observed",
        )
        before_provider_id = before_path.stem
        sources_before_reset = {str(path) for path in _transcript_paths() if path.stat().st_mtime >= started_at}
        session.submit_line("/clear")
        eager_deadline = time.monotonic() + 3.0
        allocation = "lazy"
        while time.monotonic() < eager_deadline:
            sources_after_command = {str(path) for path in _transcript_paths() if path.stat().st_mtime >= started_at}
            if sources_after_command - sources_before_reset:
                allocation = "eager"
                break
            time.sleep(0.1)
        session.submit_line(prompt_b)
        after_path = _wait(
            lambda: _assistant_marker_path(marker_b, started_at=started_at),
            timeout=args.timeout,
            message="Claude marker B assistant response was not observed",
        )
        after_provider_id = after_path.stem
        before_text = before_path.read_text(encoding="utf-8", errors="replace")
        after_text = after_path.read_text(encoding="utf-8", errors="replace")
        active_state = _wait(
            lambda: (
                candidate
                if (candidate := _read_json(state_path)) and str(candidate.get("provider_session_id") or "") == after_provider_id
                else None
            ),
            timeout=args.timeout,
            message="Longhouse did not rotate the active Claude provider identity",
        )
        bound_session_id = _wait(
            lambda: longhouse_source_binding("claude", after_provider_id),
            timeout=args.timeout,
            message="Longhouse did not bind the post-reset Claude source to the managed session",
        )
        tail_payload = _observe_longhouse_tail(
            longhouse_session_id,
            marker_a,
            marker_b,
            timeout=args.archive_timeout,
        )
        sequence = tail_sequence(tail_payload, marker_a, "/clear", marker_b)
        observation = {
            "schema_version": 1,
            "scenario": "conversation_reset",
            "provider": "claude",
            "evidence_class": "live_token",
            "provider_version": version,
            "provider_executable_identity": f"sha256:{_sha256(provider_bin)}",
            "reset_command": "/clear",
            "reset_command_accepted": "/clear" in after_text and marker_b in after_text,
            "identity_transition": classify_identity_transition(before_provider_id, after_provider_id),
            "identity_allocation": allocation,
            "before": {
                "provider_session_id": before_provider_id,
                "longhouse_session_id": longhouse_session_id,
                "provider_process_id": provider_pid,
                "run_id": run_id,
                "raw_source_ids": [str(before_path)],
                "raw_source_hashes": [_sha256(before_path)],
                "marker_digest": marker_digest(marker_a),
            },
            "after": {
                "provider_session_id": after_provider_id,
                "longhouse_session_id": longhouse_session_id,
                "provider_process_id": provider_pid,
                "run_id": run_id,
                "raw_source_ids": [str(after_path)],
                "raw_source_hashes": [_sha256(after_path)],
                "marker_digest": marker_digest(marker_b),
            },
            "provider_transition": {
                "pre_reset_history_retained": before_path.is_file() and marker_a in before_text,
                "post_reset_turn_bound_to_active_identity": marker_b in after_text,
                "pre_reset_messages_not_copied": marker_a not in after_text,
            },
            "archive": {
                **sequence,
                "source_identity_preserved": before_path != after_path,
                "tail_artifact": str(output_root / "longhouse-tail.json"),
            },
            "longhouse": {
                "provider_alias_ids": [str(active_state.get("provider_session_id") or "")],
                "timeline_session_ids": [longhouse_session_id],
                "channel_state_provider_session_id": active_state.get("provider_session_id"),
                "provider_alias_matches_before": active_state.get("provider_session_id") == before_provider_id,
                "provider_alias_matches_after": active_state.get("provider_session_id") == after_provider_id,
                "source_bound_session_id": bound_session_id,
                "source_binding_matches": bound_session_id == longhouse_session_id,
            },
        }
        (output_root / "longhouse-tail.json").write_text(json.dumps(tail_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (output_root / "conversation-reset-observation.json").write_text(
            json.dumps(observation, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        session.submit_line("/exit")
        return observation
    finally:
        session.close()
        summary = execution_summary(
            observation,
            observation_path=output_root / "conversation-reset-observation.json",
            terminal_path=terminal_path,
        )
        (output_root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-bin")
    parser.add_argument("--cwd", default=str(Path(__file__).resolve().parents[3]))
    parser.add_argument("--project", default="zerg")
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--archive-timeout", type=float, default=90.0)
    parser.add_argument(
        "--artifact-root",
        default=str(Path.home() / ".longhouse" / "canaries" / "provider-live" / "claude-reset"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        observation = run(build_parser().parse_args(argv))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"status": "failed", "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 1
    print(json.dumps(observation, indent=2, sort_keys=True))
    return observation_exit_code(observation)


if __name__ == "__main__":
    raise SystemExit(main())
