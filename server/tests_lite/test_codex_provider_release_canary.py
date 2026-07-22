from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from zerg.qa import codex_provider_release_canary as canary


def _args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        engine=str(tmp_path / "longhouse-engine"),
        repo_root=tmp_path,
        api_url="https://runtime.invalid",
        agents_token="test-agents-token",
        model=None,
        bridge_start_timeout_secs=5,
        live_interrupt_timeout_secs=1,
    )


def test_stop_bridge_uses_force_and_verifies_terminal_state_and_socket_absence(tmp_path: Path, monkeypatch) -> None:
    args = _args(tmp_path)
    isolation_root = tmp_path / "isolation"
    state_root = isolation_root / "codex-bridge"
    state_root.mkdir(parents=True)
    state_file = state_root / "session-1.json"
    socket_file = state_file.with_suffix(".sock")
    state_file.write_text(
        json.dumps({"status": "ready", "active_turn_id": "turn-1"}),
        encoding="utf-8",
    )
    socket_file.touch()
    commands: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        commands.append(argv)
        state_file.write_text(
            json.dumps({"status": "stopped", "active_turn_id": None}),
            encoding="utf-8",
        )
        socket_file.unlink()
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(canary, "_run", fake_run)
    result = canary._stop_bridge(args, "session-1", isolation_root)

    assert commands == [
        [
            str(tmp_path / "longhouse-engine"),
            "codex-bridge",
            "stop",
            "--session-id",
            "session-1",
            "--state-root",
            str(state_root),
            "--reason",
            "provider_release_canary",
            "--force",
        ]
    ]
    assert result["evidence"]["returncode"] == 0
    assert result["verification"]["verified"] is True
    assert result["verification"]["terminal_state"] is True
    assert result["verification"]["socket_absent"] is True


def test_stop_verification_rejects_zero_exit_shape_without_terminal_cleanup(tmp_path: Path) -> None:
    state_file = tmp_path / "session-1.json"
    state_file.write_text(
        json.dumps({"status": "ready", "active_turn_id": "turn-1"}),
        encoding="utf-8",
    )
    state_file.with_suffix(".sock").touch()

    result = canary._verify_bridge_stopped(state_file, timeout_secs=0)

    assert result["verified"] is False
    assert result["terminal_state"] is False
    assert result["socket_absent"] is False


def test_live_interrupt_semantic_failure_retains_start_send_and_turn_state(tmp_path: Path, monkeypatch) -> None:
    args = _args(tmp_path)
    evidence_root = tmp_path / "evidence"
    isolation_root = tmp_path / "isolation"
    state_root = isolation_root / "codex-bridge"
    state_root.mkdir(parents=True)
    state_file = state_root / "session-1.json"
    state = {"active_turn_id": None, "last_turn_status": "completed"}
    state_file.write_text(json.dumps(state), encoding="utf-8")
    start_summary = {
        "session_id": "session-1",
        "thread_id": "thread-1",
        "state_file": str(state_file),
    }
    start_result = subprocess.CompletedProcess(["start"], 0, json.dumps(start_summary), "")
    send_summary = {
        "session_id": "session-1",
        "thread_id": "thread-1",
        "turn_id": "turn-1",
        "turn_status": "inProgress",
    }
    command_results = iter(
        [
            subprocess.CompletedProcess(["send"], 0, json.dumps(send_summary), ""),
            subprocess.CompletedProcess(["interrupt"], 0, "", ""),
        ]
    )

    monkeypatch.setattr(
        canary,
        "_start_bridge",
        lambda *_args, **_kwargs: (start_summary, start_result, isolation_root),
    )
    monkeypatch.setattr(canary, "_run", lambda *_args, **_kwargs: next(command_results))
    monkeypatch.setattr(
        canary,
        "_stop_bridge",
        lambda *_args, **_kwargs: {
            "attempted": True,
            "evidence": {"returncode": 0},
            "verification": {"verified": True},
        },
    )

    result = canary.run_managed_live_interrupt(args, evidence_root, "/exact/codex")

    assert result["failure_code"] == "managed_live_interrupt_not_interrupted"
    assert result["start_summary"] == start_summary
    assert result["send_summary"] == send_summary
    assert result["state"] == state
    assert result["last_turn_status"] == "completed"
