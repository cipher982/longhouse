"""Isolated-worker Antigravity conversation-reset characterization canary."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from zerg.qa.claude_conversation_reset import _read_json
from zerg.qa.claude_conversation_reset import _sha256
from zerg.qa.claude_conversation_reset import _wait
from zerg.qa.codex_conversation_reset import _observe_longhouse_tail
from zerg.qa.conversation_reset import classify_identity_transition
from zerg.qa.conversation_reset import execution_summary
from zerg.qa.conversation_reset import longhouse_provider_aliases
from zerg.qa.conversation_reset import longhouse_source_binding
from zerg.qa.conversation_reset import marker_digest
from zerg.qa.conversation_reset import observation_exit_code
from zerg.qa.conversation_reset import tail_sequence
from zerg.qa.pty_session import ProviderPtySession
from zerg.qa.pty_session import wait_for_terminal_quiescence

ISOLATED_WORKER_ENABLE_ENV = "LONGHOUSE_ANTIGRAVITY_RESET_ISOLATED_WORKER"


def _accept_workspace_trust_if_prompted(
    session: ProviderPtySession,
    terminal_path: Path,
    *,
    timeout: float,
) -> bool:
    deadline = time.monotonic() + min(timeout, 30.0)
    while time.monotonic() < deadline:
        try:
            terminal = terminal_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            terminal = ""
        if "Do you trust the contents of this project?" in terminal:
            session.submit_line("")
            return True
        if not session.alive():
            raise RuntimeError("Antigravity exited during workspace trust preflight")
        time.sleep(0.1)
    return False


def _state_for_managed_session(state_root: Path, session_id: str) -> tuple[Path, dict[str, Any]] | None:
    path = state_root / f"{session_id}.json"
    state = _read_json(path)
    return (path, state) if state else None


def _completed_marker_state(
    state_root: Path,
    session_id: str,
    marker: str,
) -> tuple[Path, dict[str, Any], Path] | None:
    found = _state_for_managed_session(state_root, session_id)
    if not found:
        return None
    state_path, state = found
    transcript = Path(str(state.get("transcript_path") or "")).expanduser()
    try:
        transcript_text = transcript.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if state.get("state") != "idle" or marker not in transcript_text:
        return None
    return state_path, state, transcript


def run(args: argparse.Namespace) -> dict[str, Any]:
    if os.environ.get(ISOLATED_WORKER_ENABLE_ENV) != "1":
        raise RuntimeError(f"Antigravity live reset requires an isolated unwatched worker and {ISOLATED_WORKER_ENABLE_ENV}=1")
    if not args.longhouse_session_id:
        raise RuntimeError("--longhouse-session-id is required and must name the factory-owned managed session")
    provider_bin = Path(args.provider_bin or os.environ.get("LONGHOUSE_ANTIGRAVITY_BIN") or "agy").expanduser()
    if not provider_bin.is_absolute():
        resolved = subprocess.run(["/usr/bin/which", str(provider_bin)], text=True, capture_output=True, check=False).stdout.strip()
        if not resolved:
            raise RuntimeError(f"Antigravity binary not found: {provider_bin}")
        provider_bin = Path(resolved)
    provider_bin = provider_bin.resolve(strict=True)
    version_result = subprocess.run([str(provider_bin), "--version"], text=True, capture_output=True, timeout=15, check=False)
    if version_result.returncode != 0:
        raise RuntimeError("Antigravity version probe failed")
    version = version_result.stdout.strip().splitlines()[0]
    output_root = Path(args.artifact_root).expanduser().resolve() / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_root.mkdir(parents=True)
    workspace = Path(args.cwd).expanduser().resolve(strict=True)
    state_root = Path(args.state_root).expanduser().resolve()
    terminal_path = output_root / "terminal.raw"
    invocation = uuid4().hex
    marker_a = f"LONGHOUSE_RESET_ANTIGRAVITY_A_{invocation}"
    marker_b = f"LONGHOUSE_RESET_ANTIGRAVITY_B_{invocation}"
    env = os.environ.copy()
    env["LONGHOUSE_MANAGED_SESSION_ID"] = args.longhouse_session_id
    env["LONGHOUSE_RUN_ID"] = args.run_id or uuid4().hex
    session = ProviderPtySession.start(
        argv=[str(provider_bin)],
        cwd=workspace,
        env=env,
        terminal_path=terminal_path,
        thread_name="antigravity-conversation-reset-terminal-drain",
    )
    observation: dict[str, Any] | None = None
    try:
        _accept_workspace_trust_if_prompted(session, terminal_path, timeout=args.timeout)
        wait_for_terminal_quiescence(session, timeout=args.timeout)
        session.submit_line(f"Reply with exactly {marker_a}")
        _before_state_path, before_state, before_path = _wait(
            lambda: _completed_marker_state(state_root, args.longhouse_session_id, marker_a),
            timeout=args.timeout,
            message="Antigravity marker A Stop/transcript evidence was not observed",
        )
        before_provider_id = str(before_state.get("conversation_id") or before_state.get("provider_session_id") or "")
        before_text = before_path.read_text(encoding="utf-8", errors="replace")
        session.submit_line("/clear")

        def eager_identity() -> str | None:
            found = _state_for_managed_session(state_root, args.longhouse_session_id)
            if not found:
                return None
            candidate = str(found[1].get("conversation_id") or found[1].get("provider_session_id") or "")
            return candidate if candidate and candidate != before_provider_id else None

        try:
            _wait(eager_identity, timeout=3.0, message="Antigravity identity allocation remained lazy")
            allocation = "eager"
        except RuntimeError:
            allocation = "lazy"
        wait_for_terminal_quiescence(session, timeout=args.timeout)
        session.submit_line(f"Reply with exactly {marker_b}")
        _after_state_path, after_state, after_path = _wait(
            lambda: _completed_marker_state(state_root, args.longhouse_session_id, marker_b),
            timeout=args.timeout,
            message="Antigravity marker B Stop/transcript evidence was not observed",
        )
        after_provider_id = str(after_state.get("conversation_id") or after_state.get("provider_session_id") or "")
        after_text = after_path.read_text(encoding="utf-8", errors="replace")
        before_bound_session_id = _wait(
            lambda: longhouse_source_binding("antigravity", before_provider_id),
            timeout=args.timeout,
            message="Longhouse did not bind the pre-reset Antigravity source",
        )
        after_bound_session_id = _wait(
            lambda: longhouse_source_binding("antigravity", after_provider_id),
            timeout=args.timeout,
            message="Longhouse did not bind the post-reset Antigravity source",
        )
        aliases = _wait(
            lambda: (
                values
                if before_provider_id in (values := longhouse_provider_aliases("antigravity", args.longhouse_session_id))
                and after_provider_id in values
                else None
            ),
            timeout=args.timeout,
            message="Longhouse did not retain both Antigravity provider aliases",
        )
        tail_payload = _observe_longhouse_tail(args.longhouse_session_id, marker_a, marker_b, timeout=args.archive_timeout)
        sequence = tail_sequence(tail_payload, marker_a, "/clear", marker_b)
        observation = {
            "schema_version": 1,
            "scenario": "conversation_reset",
            "provider": "antigravity",
            "evidence_class": "live_token",
            "provider_version": version,
            "provider_executable_identity": f"sha256:{_sha256(provider_bin)}",
            "reset_command": "/clear",
            "reset_command_accepted": bool(after_provider_id) and marker_b in after_text,
            "identity_transition": classify_identity_transition(before_provider_id, after_provider_id),
            "identity_allocation": allocation,
            "before": {
                "provider_session_id": before_provider_id,
                "longhouse_session_id": args.longhouse_session_id,
                "provider_process_id": str(session.process.pid),
                "run_id": env["LONGHOUSE_RUN_ID"],
                "raw_source_ids": [str(before_path)],
                "raw_source_hashes": [_sha256(before_path)],
                "marker_digest": marker_digest(marker_a),
            },
            "after": {
                "provider_session_id": after_provider_id,
                "longhouse_session_id": args.longhouse_session_id,
                "provider_process_id": str(session.process.pid),
                "run_id": env["LONGHOUSE_RUN_ID"],
                "raw_source_ids": [str(after_path)],
                "raw_source_hashes": [_sha256(after_path)],
                "marker_digest": marker_digest(marker_b),
            },
            "provider_transition": {
                "pre_reset_history_retained": before_path.is_file() and marker_a in before_text,
                "post_reset_turn_bound_to_active_identity": marker_b in after_text,
                "pre_reset_messages_not_copied": before_path == after_path or marker_a not in after_text,
            },
            "archive": {
                **sequence,
                "source_identity_preserved": bool(before_provider_id and after_provider_id),
                "tail_artifact": str(output_root / "longhouse-tail.json"),
            },
            "longhouse": {
                "provider_alias_ids": list(aliases),
                "timeline_session_ids": [args.longhouse_session_id],
                "isolated_worker": True,
                "provider_alias_matches_before": before_provider_id in aliases,
                "provider_alias_matches_after": after_provider_id in aliases,
                "source_bound_session_id": after_bound_session_id,
                "source_binding_matches": before_bound_session_id == args.longhouse_session_id
                and after_bound_session_id == args.longhouse_session_id,
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
    parser.add_argument("--longhouse-session-id", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--cwd", default=str(Path(__file__).resolve().parents[3]))
    parser.add_argument(
        "--state-root",
        default=str(Path.home() / ".longhouse" / "managed-local" / "antigravity" / "sessions"),
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--archive-timeout", type=float, default=90.0)
    parser.add_argument(
        "--artifact-root",
        default=str(Path.home() / ".longhouse" / "canaries" / "provider-live" / "antigravity-reset"),
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
