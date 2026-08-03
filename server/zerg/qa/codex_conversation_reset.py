"""Live managed-Codex conversation-reset characterization canary."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
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
from zerg.qa.codex_auth import login_with_api_key
from zerg.qa.conversation_reset import classify_identity_transition
from zerg.qa.conversation_reset import execution_summary
from zerg.qa.conversation_reset import longhouse_provider_aliases
from zerg.qa.conversation_reset import longhouse_source_binding
from zerg.qa.conversation_reset import marker_digest
from zerg.qa.conversation_reset import observation_exit_code
from zerg.qa.conversation_reset import tail_sequence
from zerg.qa.pty_session import ProviderPtySession
from zerg.qa.pty_session import wait_for_terminal_quiescence


def _rollout_paths(codex_home: Path) -> list[Path]:
    return list((codex_home / "sessions").glob("**/*.jsonl"))


def _assistant_marker_path(marker: str, *, started_at: float, codex_home: Path) -> Path | None:
    for path in _rollout_paths(codex_home):
        try:
            if path.stat().st_mtime < started_at:
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if marker not in line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            if payload.get("role") == "assistant" or payload.get("type") in {"agent_message", "assistant_message"}:
                return path
    return None


def _thread_id(path: Path) -> str:
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        candidate = payload.get("id") if row.get("type") == "session_meta" else None
        if candidate:
            return str(candidate)
    return path.stem.rsplit("-", 1)[-1]


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
    provider_bin = Path(args.provider_bin or os.environ.get("LONGHOUSE_CODEX_BIN") or "codex").expanduser()
    if not provider_bin.is_absolute():
        resolved = subprocess.run(["/usr/bin/which", str(provider_bin)], text=True, capture_output=True, check=False).stdout.strip()
        if not resolved:
            raise RuntimeError(f"Codex binary not found: {provider_bin}")
        provider_bin = Path(resolved)
    provider_bin = provider_bin.resolve(strict=True)
    version_result = subprocess.run([str(provider_bin), "--version"], text=True, capture_output=True, timeout=15, check=False)
    if version_result.returncode != 0:
        raise RuntimeError("Codex version probe failed")
    version = version_result.stdout.strip().splitlines()[0]
    output_root = Path(args.artifact_root).expanduser().resolve() / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_root.mkdir(parents=True)
    workspace = Path(args.cwd).expanduser().resolve(strict=True)
    terminal_path = output_root / "terminal.raw"
    api_key = os.environ.get("CODEX_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("CODEX_API_KEY is required; the conversation-reset canary never copies the daily Codex profile")
    engine_bin = shutil.which("longhouse-engine")
    if not engine_bin:
        raise RuntimeError("longhouse-engine is required for the isolated Codex coordination MCP")
    runtime_root = Path(tempfile.mkdtemp(prefix="longhouse-codex-reset-"))
    codex_home = runtime_root / "codex-home"
    codex_home.mkdir(mode=0o700)
    isolated_home = runtime_root / "home"
    isolated_home.mkdir(mode=0o700)
    (codex_home / "config.toml").write_text(
        f'[mcp_servers.longhouse]\ncommand = {json.dumps(engine_bin)}\nargs = ["claude-channel", "serve"]\n',
        encoding="utf-8",
    )
    invocation = uuid4().hex
    marker_a = f"LONGHOUSE_RESET_CODEX_A_{invocation}"
    marker_b = f"LONGHOUSE_RESET_CODEX_B_{invocation}"
    started_at = time.time()
    env = os.environ.copy()
    env["HOME"] = str(isolated_home)
    env["LONGHOUSE_CODEX_BIN"] = str(provider_bin)
    env["LONGHOUSE_CODEX_TUI_HOME"] = str(codex_home)
    env["CODEX_HOME"] = str(codex_home)
    try:
        auth_receipt = login_with_api_key(
            provider_bin,
            api_key=api_key,
            environment=env,
            cwd=workspace,
        )
    except Exception:
        shutil.rmtree(runtime_root, ignore_errors=True)
        raise
    env.pop("CODEX_API_KEY", None)
    argv = [
        "longhouse",
        "codex",
        "--cwd",
        str(workspace),
        "--project",
        args.project,
        "--name",
        "Codex conversation reset qualification",
        "--codex-bin",
        str(provider_bin),
    ]
    if args.model:
        argv.extend(("--model", args.model))
    session: ProviderPtySession | None = None
    observation: dict[str, Any] | None = None
    try:
        session = ProviderPtySession.start(
            argv=argv,
            cwd=workspace,
            env=env,
            terminal_path=terminal_path,
            thread_name="codex-conversation-reset-terminal-drain",
        )
        state_root = isolated_home / ".longhouse" / "managed-local" / "codex-bridge"

        def launched_state():
            if not session.alive():
                raise RuntimeError(f"longhouse codex exited during launch ({session.process.returncode})")
            for path in state_root.glob("*.json"):
                try:
                    if path.stat().st_mtime < started_at - 1:
                        continue
                except OSError:
                    continue
                state = _read_json(path)
                if state and state.get("status") == "ready" and Path(str(state.get("cwd") or "")).resolve() == workspace:
                    return str(state.get("session_id") or path.stem), path, state
            return None

        longhouse_session_id, state_path, state = _wait(
            launched_state, timeout=args.timeout, message="Codex managed bridge did not become ready"
        )
        provider_pid = str(state.get("app_server_pid") or state.get("pid") or session.process.pid)
        run_id = str(state.get("run_id") or "")
        wait_for_terminal_quiescence(session, timeout=args.timeout)
        session.submit_line(f"Reply with exactly {marker_a}")
        before_path = _wait(
            lambda: _assistant_marker_path(marker_a, started_at=started_at, codex_home=codex_home),
            timeout=args.timeout,
            message="Codex marker A assistant response was not observed",
        )
        before_provider_id = _thread_id(before_path)
        source_paths_before = {str(path) for path in _rollout_paths(codex_home) if path.stat().st_mtime >= started_at}
        session.submit_line("/clear")
        _wait(
            lambda: str((_read_json(state_path) or {}).get("thread_id") or "") not in {"", before_provider_id},
            timeout=min(args.timeout, 15.0),
            message="Codex did not return to a ready prompt after /clear",
        )
        eager_deadline = time.monotonic() + 3.0
        allocation = "lazy"
        while time.monotonic() < eager_deadline:
            current_state = _read_json(state_path) or {}
            source_paths_after = {str(path) for path in _rollout_paths(codex_home) if path.stat().st_mtime >= started_at}
            if str(current_state.get("thread_id") or "") not in {"", before_provider_id} or source_paths_after - source_paths_before:
                allocation = "eager"
                break
            time.sleep(0.1)
        session.submit_line(f"Reply with exactly {marker_b}")
        after_path = _wait(
            lambda: _assistant_marker_path(marker_b, started_at=started_at, codex_home=codex_home),
            timeout=args.timeout,
            message="Codex marker B assistant response was not observed",
        )
        after_provider_id = _thread_id(after_path)
        before_text = before_path.read_text(encoding="utf-8", errors="replace")
        after_text = after_path.read_text(encoding="utf-8", errors="replace")
        final_state = _read_json(state_path) or state
        bound_session_id = _wait(
            lambda: longhouse_source_binding("codex", after_provider_id),
            timeout=args.timeout,
            message="Longhouse did not bind the post-reset Codex source to the managed session",
        )
        provider_aliases = _wait(
            lambda: (
                aliases
                if {before_provider_id, after_provider_id}.issubset(
                    aliases := set(longhouse_provider_aliases("codex", longhouse_session_id))
                )
                else None
            ),
            timeout=args.timeout,
            message="Longhouse did not retain both Codex provider aliases",
        )
        tail_payload = _observe_longhouse_tail(longhouse_session_id, marker_a, marker_b, timeout=args.archive_timeout)
        sequence = tail_sequence(tail_payload, marker_a, "/clear", marker_b)
        provider_alias = str(final_state.get("thread_id") or "").strip()
        observation = {
            "schema_version": 1,
            "scenario": "conversation_reset",
            "provider": "codex",
            "evidence_class": "live_token",
            "provider_version": version,
            "provider_executable_identity": f"sha256:{_sha256(provider_bin)}",
            "authentication": auth_receipt,
            "reset_command": "/clear",
            "reset_command_accepted": provider_alias == after_provider_id
            and Path(str(final_state.get("thread_path") or "")) == after_path
            and marker_b in after_text,
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
                "pre_reset_history_retained": marker_a in before_text,
                "post_reset_turn_bound_to_active_identity": marker_b in after_text,
                "pre_reset_messages_not_copied": marker_a not in after_text,
            },
            "archive": {
                **sequence,
                "source_identity_preserved": before_path != after_path,
                "tail_artifact": str(output_root / "longhouse-tail.json"),
            },
            "longhouse": {
                "provider_alias_ids": sorted(provider_aliases),
                "timeline_session_ids": [longhouse_session_id],
                "bridge_thread_path": final_state.get("thread_path"),
                "provider_alias_matches_before": before_provider_id in provider_aliases,
                "provider_alias_matches_after": after_provider_id in provider_aliases,
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
        if session is not None:
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
        shutil.rmtree(runtime_root, ignore_errors=True)


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
        default=str(Path.home() / ".longhouse" / "canaries" / "provider-live" / "codex-reset"),
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
