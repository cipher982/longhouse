"""Live managed-OpenCode conversation-reset characterization canary."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import time
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from zerg.qa.claude_conversation_reset import _read_json
from zerg.qa.claude_conversation_reset import _sha256
from zerg.qa.claude_conversation_reset import _tail
from zerg.qa.claude_conversation_reset import _wait
from zerg.qa.conversation_reset import classify_identity_transition
from zerg.qa.conversation_reset import execution_summary
from zerg.qa.conversation_reset import marker_digest
from zerg.qa.conversation_reset import tail_sequence
from zerg.qa.pty_session import ProviderPtySession
from zerg.qa.pty_session import wait_for_terminal_quiescence


def _matching_session(db: Path, marker: str, *, assistant: bool) -> str | None:
    try:
        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                """
                SELECT part.session_id, part.data, message.data
                FROM part JOIN message ON message.id = part.message_id
                WHERE part.data LIKE ?
                ORDER BY part.time_created DESC
                """,
                (f"%{marker}%",),
            ).fetchall()
    except sqlite3.Error:
        return None
    for session_id, _part_raw, message_raw in rows:
        lowered = str(message_raw).lower()
        if not assistant or '"role":"assistant"' in lowered or '"role": "assistant"' in lowered:
            return str(session_id)
    return None


def _session_ids_since(db: Path, started_at_ms: int) -> set[str]:
    try:
        with sqlite3.connect(db) as conn:
            return {str(row[0]) for row in conn.execute("SELECT id FROM session WHERE time_created >= ?", (started_at_ms,))}
    except sqlite3.Error:
        return set()


def _session_snapshot(db: Path, session_id: str) -> tuple[str, str]:
    with sqlite3.connect(db) as conn:
        message_rows = conn.execute("SELECT id, data FROM message WHERE session_id = ? ORDER BY time_created, id", (session_id,)).fetchall()
        part_rows = conn.execute("SELECT id, data FROM part WHERE session_id = ? ORDER BY time_created, id", (session_id,)).fetchall()
    raw = "\n".join(f"message\t{row[0]}\t{row[1]}" for row in message_rows)
    raw += "\n" + "\n".join(f"part\t{row[0]}\t{row[1]}" for row in part_rows)
    return raw, __import__("hashlib").sha256(raw.encode()).hexdigest()


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
    provider_bin = Path(args.provider_bin or os.environ.get("LONGHOUSE_OPENCODE_BIN") or "opencode").expanduser()
    if not provider_bin.is_absolute():
        resolved = subprocess.run(["/usr/bin/which", str(provider_bin)], text=True, capture_output=True, check=False).stdout.strip()
        if not resolved:
            raise RuntimeError(f"OpenCode binary not found: {provider_bin}")
        provider_bin = Path(resolved)
    provider_bin = provider_bin.resolve(strict=True)
    version_result = subprocess.run([str(provider_bin), "--version"], text=True, capture_output=True, timeout=15, check=False)
    if version_result.returncode != 0:
        raise RuntimeError("OpenCode version probe failed")
    version = version_result.stdout.strip().splitlines()[-1].strip()
    output_root = Path(args.artifact_root).expanduser().resolve() / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_root.mkdir(parents=True)
    workspace = Path(args.cwd).expanduser().resolve(strict=True)
    terminal_path = output_root / "terminal.raw"
    db = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
    invocation = uuid4().hex
    marker_a = f"LONGHOUSE_RESET_OPENCODE_A_{invocation}"
    marker_b = f"LONGHOUSE_RESET_OPENCODE_B_{invocation}"
    prompt_a = f"Reply with exactly {marker_a}"
    prompt_b = f"Reply with exactly {marker_b}"
    started_at = time.time()
    started_at_ms = int(started_at * 1000)
    env = os.environ.copy()
    env["LONGHOUSE_OPENCODE_BIN"] = str(provider_bin)
    argv = [
        "longhouse",
        "opencode",
        "--cwd",
        str(workspace),
        "--project",
        args.project,
        "--name",
        "OpenCode conversation reset qualification",
        "--opencode-bin",
        str(provider_bin),
    ]
    if args.model:
        argv.extend(("--model", args.model))
    session = ProviderPtySession.start(
        argv=argv,
        cwd=workspace,
        env=env,
        terminal_path=terminal_path,
        thread_name="opencode-conversation-reset-terminal-drain",
    )
    observation: dict[str, Any] | None = None
    try:
        bridge_roots = (
            Path.home() / ".longhouse" / "managed-local" / "opencode" / "bridge" / "sessions",
            Path.home() / ".claude" / "managed-local" / "opencode-server",
        )

        def launched_state():
            if not session.alive():
                raise RuntimeError(f"longhouse opencode exited during launch ({session.process.returncode})")
            for bridge_root in bridge_roots:
                for path in bridge_root.glob("*.json"):
                    try:
                        if path.stat().st_mtime < started_at - 1:
                            continue
                    except OSError:
                        continue
                    state = _read_json(path)
                    if (
                        state
                        and (state.get("ready") is True or state.get("server_url"))
                        and Path(str(state.get("cwd") or "")).resolve() == workspace
                    ):
                        return str(state.get("session_id") or path.stem), state
            return None

        longhouse_session_id, state = _wait(launched_state, timeout=args.timeout, message="OpenCode managed bridge did not become ready")
        provider_pid = str(state.get("opencode_pid") or state.get("pid") or session.process.pid)
        wait_for_terminal_quiescence(session, timeout=args.timeout)
        session.submit_line(prompt_a)
        before_provider_id = _wait(
            lambda: _matching_session(db, marker_a, assistant=True),
            timeout=args.timeout,
            message="OpenCode marker A assistant response was not observed",
        )
        before_text, before_hash = _session_snapshot(db, before_provider_id)
        ids_before_reset = _session_ids_since(db, started_at_ms)
        session.submit_line("/new")
        eager_deadline = time.monotonic() + 3.0
        allocation = "lazy"
        while time.monotonic() < eager_deadline:
            if _session_ids_since(db, started_at_ms) - ids_before_reset:
                allocation = "eager"
                break
            time.sleep(0.1)
        session.submit_line(prompt_b)
        after_provider_id = _wait(
            lambda: _matching_session(db, marker_b, assistant=True),
            timeout=args.timeout,
            message="OpenCode marker B assistant response was not observed",
        )
        after_text, after_hash = _session_snapshot(db, after_provider_id)
        tail_payload = _observe_longhouse_tail(
            longhouse_session_id,
            marker_a,
            marker_b,
            timeout=args.archive_timeout,
        )
        sequence = tail_sequence(tail_payload, marker_a, "/new", marker_b)
        provider_alias = str(state.get("provider_session_id") or "").strip()
        observation = {
            "schema_version": 1,
            "scenario": "conversation_reset",
            "provider": "opencode",
            "evidence_class": "live_token",
            "provider_version": version,
            "provider_executable_identity": f"sha256:{_sha256(provider_bin)}",
            "reset_command": "/new",
            "reset_command_accepted": after_provider_id in _session_ids_since(db, started_at_ms) and marker_b in after_text,
            "identity_transition": classify_identity_transition(before_provider_id, after_provider_id),
            "identity_allocation": allocation,
            "before": {
                "provider_session_id": before_provider_id,
                "longhouse_session_id": longhouse_session_id,
                "provider_process_id": provider_pid,
                "run_id": str(state.get("run_id") or state.get("launch_id") or ""),
                "raw_source_ids": [f"sqlite://{db}#session={before_provider_id}"],
                "raw_source_hashes": [before_hash],
                "marker_digest": marker_digest(marker_a),
            },
            "after": {
                "provider_session_id": after_provider_id,
                "longhouse_session_id": longhouse_session_id,
                "provider_process_id": provider_pid,
                "run_id": str(state.get("run_id") or state.get("launch_id") or ""),
                "raw_source_ids": [f"sqlite://{db}#session={after_provider_id}"],
                "raw_source_hashes": [after_hash],
                "marker_digest": marker_digest(marker_b),
            },
            "provider_transition": {
                "pre_reset_history_retained": marker_a in before_text,
                "post_reset_turn_bound_to_active_identity": marker_b in after_text,
                "pre_reset_messages_not_copied": marker_a not in after_text,
            },
            "archive": {
                **sequence,
                "source_identity_preserved": (
                    f"sqlite://{db}#session={before_provider_id}" != f"sqlite://{db}#session={after_provider_id}"
                ),
                "tail_artifact": str(output_root / "longhouse-tail.json"),
            },
            "longhouse": {
                "provider_alias_ids": [provider_alias] if provider_alias else [],
                "timeline_session_ids": [longhouse_session_id],
                "provider_alias_matches_before": provider_alias == before_provider_id,
                "provider_alias_matches_after": provider_alias == after_provider_id,
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
        (output_root / "summary.json").write_text(
            json.dumps(
                execution_summary(
                    observation,
                    observation_path=output_root / "conversation-reset-observation.json",
                    terminal_path=terminal_path,
                ),
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


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
        default=str(Path.home() / ".longhouse" / "canaries" / "provider-live" / "opencode-reset"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        observation = run(build_parser().parse_args(argv))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"status": "failed", "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 1
    print(json.dumps(observation, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
