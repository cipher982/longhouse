from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from zerg.qa import codex_provider_release_canary as canary


def _args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        engine=str(tmp_path / "longhouse-engine"),
        repo_root=tmp_path,
        api_url="https://runtime.invalid",
        agents_token="test-agents-token",
        model=None,
        bridge_start_timeout_secs=5,
        live_send_timeout_secs=1,
        live_interrupt_timeout_secs=1,
    )


def test_command_evidence_normalizes_pathlike_argv_values() -> None:
    result = subprocess.CompletedProcess(
        [Path("/tmp/longhouse-engine"), "codex-bridge", "--agents-token", "test-agents-token"],
        1,
        "",
        "listener failed",
    )

    evidence = canary._command_evidence(result, secrets=["test-agents-token"])

    assert evidence["argv"] == ["/tmp/longhouse-engine", "codex-bridge", "--agents-token", "<redacted>"]


def test_provider_runtime_environment_isolated_from_worker_profile(tmp_path: Path) -> None:
    environment = canary._provider_runtime_environment(
        {
            "HOME": "/root",
            "CODEX_HOME": "/root/.codex",
            "CODEX_API_KEY": "factory-key",
            "OPENAI_API_KEY": "ambient-key",
            "PROBE_VALUE": "preserved",
        },
        tmp_path / "isolation",
    )

    provider_home = tmp_path / "isolation" / "provider-home"
    assert environment["HOME"] == str(provider_home)
    assert environment["CODEX_HOME"] == str(provider_home / ".codex")
    assert environment["XDG_CONFIG_HOME"] == str(provider_home / ".config")
    assert environment["XDG_DATA_HOME"] == str(provider_home / ".local" / "share")
    assert environment["XDG_CACHE_HOME"] == str(provider_home / ".cache")
    assert environment["LONGHOUSE_HOME"] == str(tmp_path / "isolation" / "longhouse")
    assert environment["PROBE_VALUE"] == "preserved"
    assert "CODEX_API_KEY" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert (provider_home / ".codex").is_dir()


def test_start_bridge_passes_isolated_environment_to_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _args(tmp_path)
    seen: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, '{"state_file":"state.json"}\n', "")

    monkeypatch.setattr(canary, "_run", fake_run)
    isolation_root = tmp_path / "isolation"
    canary._start_bridge(
        args,
        evidence_root=tmp_path / "evidence",
        codex_bin="/bin/codex",
        launch_mode="tui",
        isolation_root=isolation_root,
    )

    environment = seen["env"]
    assert isinstance(environment, dict)
    assert environment["HOME"] == str(isolation_root / "provider-home")
    assert environment["CODEX_HOME"] == str(isolation_root / "provider-home" / ".codex")
    assert environment["LONGHOUSE_HOME"] == str(isolation_root / "longhouse")
    assert environment["LONGHOUSE_CODEX_BRIDGE_TOKEN"] == "test-agents-token"


def test_start_bridge_registers_and_confirms_managed_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _args(tmp_path)
    seen: dict[str, object] = {}
    outcomes: list[dict[str, object]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen["argv"] = argv
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, '{"state_file":"state.json"}\n', "")

    monkeypatch.setattr(
        canary,
        "_register_managed_codex_launch",
        lambda *args, **kwargs: (
            {
                "session_id": "session-1",
                "run_id": "run-1",
                "coordination_token": "coordination-secret",
            },
            "machine-1",
        ),
    )
    monkeypatch.setattr(
        canary,
        "_report_managed_codex_launch_outcome",
        lambda args, **kwargs: outcomes.append(dict(kwargs)) or {"recorded": True},
    )
    monkeypatch.setattr(canary, "_run", fake_run)

    canary._start_bridge(
        args,
        evidence_root=tmp_path / "evidence",
        codex_bin="/bin/codex",
        launch_mode="detached_ui",
        isolation_root=tmp_path / "isolation",
        register_managed=True,
    )

    argv = seen["argv"]
    assert isinstance(argv, list)
    assert "--run-id" in argv and argv[argv.index("--run-id") + 1] == "run-1"
    assert "--machine-name" in argv and argv[argv.index("--machine-name") + 1] == "machine-1"
    environment = seen["env"]
    assert isinstance(environment, dict)
    assert environment["LONGHOUSE_COORDINATION_TOKEN"] == "coordination-secret"
    assert outcomes == [{"session_id": "session-1", "run_id": "run-1", "outcome": "confirmed"}]


def test_fake_app_server_binary_proves_installed_engine_permission_protocol(tmp_path: Path) -> None:
    engine = tmp_path / "longhouse-engine"
    engine.write_text(
        f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
assert args[0] == "codex-app-server-canary"
assert "--auto-approve" in args
assert (Path(os.environ["HOME"]) / ".codex").is_dir()
codex_bin = Path(args[args.index("--codex-bin") + 1])
assert codex_bin.is_file() and codex_bin.stat().st_mode & 0o100
print(json.dumps({{
    "turn_status": "completed",
    "server_request_counts": {{
        "item/commandExecution/requestApproval": 1,
        "item/permissions/requestApproval": 1,
        "item/tool/requestUserInput": 1,
    }},
    "thread_active_flag_counts": {{
        "waitingOnApproval": 1,
        "waitingOnUserInput": 1,
    }},
    "response_errors": [],
}}))
""",
        encoding="utf-8",
    )
    engine.chmod(0o700)
    args = canary._coerce_args(
        {
            "engine": str(engine),
            "repo_root": tmp_path,
            "fake_app_server_timeout_secs": 10,
        }
    )

    result = canary.run_fake_app_server_binary(args, tmp_path / "evidence")

    assert result["status"] == "pass"
    assert result["operation_evidence"]["permission_prompt"] == {
        "status": "pass",
        "level": "hermetic",
        "source": "installed longhouse-engine against a deterministic Codex app-server permission fixture",
        "canary": "codex_fake_app_server_permission_approval",
        "next": "Promote with a live held-permission Codex provider canary.",
    }
    evidence_root = tmp_path / "evidence" / "fake-app-server-binary"
    assert (evidence_root / "codex").is_file()
    assert (evidence_root / "command.json").is_file()
    assert (evidence_root / "source-home" / ".codex").is_dir()
    assert (evidence_root / "summary.json").is_file()


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
    environments: list[dict[str, str] | None] = []

    def fake_run(argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        commands.append(argv)
        environments.append(kwargs.get("env"))
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
    assert environments[0]["LONGHOUSE_HOME"] == str(isolation_root / "longhouse")


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


def test_pty_recorder_times_out_captures_output_and_reaps_process_group(tmp_path: Path) -> None:
    args = _args(tmp_path)
    args.tui_record_secs = 1
    pid_path = tmp_path / "pty.pid"
    recording = tmp_path / "pty.tty"
    command = [
        "/bin/sh",
        "-c",
        f"echo $$ > {pid_path}; printf pty-ready; sleep 30",
    ]

    result = canary._record_pty_session(args, command, recording)

    assert result.returncode == 124
    assert "pty-ready" in result.stdout
    assert "pty-ready" in recording.read_text(encoding="utf-8")
    pid = int(pid_path.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_managed_cold_resume_keeps_thread_and_replaces_run_and_connection(tmp_path: Path, monkeypatch) -> None:
    args = _args(tmp_path)
    args.tui_record_secs = 1
    isolation_root = tmp_path / "isolation"
    state_root = isolation_root / "codex-bridge"
    state_root.mkdir(parents=True)
    state_file = state_root / "session-1.json"
    thread_path = isolation_root / "rollout.jsonl"
    thread_path.write_text("{}\n", encoding="utf-8")
    starts: list[dict[str, object]] = []

    def fake_start(*_args, **kwargs):
        starts.append(kwargs)
        if len(starts) == 1:
            state = {
                "session_id": "session-1",
                "thread_id": "thread-1",
                "thread_path": str(thread_path),
                "run_id": "run-1",
                "connection_id": "connection-1",
                "app_server_pid": 100,
                "app_server_process_start_time": "first",
                "thread_subscription_status": "subscribed",
            }
            summary = {"session_id": "session-1", "state_file": str(state_file)}
        else:
            state = {
                "session_id": "session-1",
                "thread_id": "thread-1",
                "thread_path": str(thread_path),
                "run_id": "run-2",
                "connection_id": "connection-2",
                "app_server_pid": 200,
                "app_server_process_start_time": "second",
                "thread_subscription_status": "subscribed",
            }
            summary = {
                "session_id": "session-1",
                "state_file": str(state_file),
                "ws_url": "ws://127.0.0.1:1234",
            }
        state_file.write_text(json.dumps(state), encoding="utf-8")
        return summary, subprocess.CompletedProcess(["start"], 0, json.dumps(summary), ""), isolation_root

    monkeypatch.setattr(canary, "_start_bridge", fake_start)
    monkeypatch.setattr(
        canary,
        "_stop_bridge",
        lambda *_args, **_kwargs: {"verification": {"verified": True}},
    )

    def fake_run(argv, **_kwargs):
        marker = argv[argv.index("--text") + 1].split("Reply exactly ", 1)[1].split(" and nothing else.", 1)[0]
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state["last_turn_status"] = "completed"
        state_file.write_text(json.dumps(state), encoding="utf-8")
        thread_path.write_text(json.dumps({"payload": {"type": "agent_message", "message": marker}}), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(canary, "_run", fake_run)

    def fake_record(_args, command, recording_path, **_kwargs):
        recording_path.write_text("resumed", encoding="utf-8")
        return subprocess.CompletedProcess(command, 124, "resumed", "")

    monkeypatch.setattr(canary, "_record_pty_session", fake_record)

    result = canary.run_managed_cold_resume(args, tmp_path / "evidence", "/exact/codex")

    assert result["status"] == "pass"
    assert result["provider_thread_id"] == "thread-1"
    assert result["initial_run_id"] == "run-1"
    assert result["resumed_run_id"] == "run-2"
    assert result["initial_connection_id"] == "connection-1"
    assert result["resumed_connection_id"] == "connection-2"
    assert all(result["assertions"].values())
    assert starts[1]["session_id"] == "session-1"
    assert starts[1]["resume_thread_id"] == "thread-1"
    assert starts[1]["resume_thread_path"] == str(thread_path)


def test_live_interrupt_semantic_failure_retains_start_send_and_turn_state(tmp_path: Path, monkeypatch) -> None:
    args = _args(tmp_path)
    evidence_root = tmp_path / "evidence"
    isolation_root = tmp_path / "isolation"
    state_root = isolation_root / "codex-bridge"
    state_root.mkdir(parents=True)
    state_file = state_root / "session-1.json"
    state = {"active_turn_id": None, "last_turn_status": "completed"}
    state_file.write_text(
        json.dumps({"active_turn_id": "turn-1", "last_turn_status": "inProgress"}),
        encoding="utf-8",
    )
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

    def run_command(*_args, **_kwargs):
        result = next(command_results)
        if result.args == ["interrupt"]:
            state_file.write_text(json.dumps(state), encoding="utf-8")
        return result

    monkeypatch.setattr(canary, "_run", run_command)
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


def test_live_interrupt_rejects_state_for_a_different_turn(tmp_path: Path, monkeypatch) -> None:
    args = _args(tmp_path)
    evidence_root = tmp_path / "evidence"
    isolation_root = tmp_path / "isolation"
    state_root = isolation_root / "codex-bridge"
    state_root.mkdir(parents=True)
    state_file = state_root / "session-1.json"
    state_file.write_text(
        json.dumps({"active_turn_id": "turn-other", "last_turn_status": "inProgress"}),
        encoding="utf-8",
    )
    start_summary = {
        "session_id": "session-1",
        "thread_id": "thread-1",
        "state_file": str(state_file),
    }
    send_summary = {
        "session_id": "session-1",
        "thread_id": "thread-1",
        "turn_id": "turn-1",
        "turn_status": "inProgress",
    }
    monkeypatch.setattr(
        canary,
        "_start_bridge",
        lambda *_args, **_kwargs: (
            start_summary,
            subprocess.CompletedProcess(["start"], 0, json.dumps(start_summary), ""),
            isolation_root,
        ),
    )
    monkeypatch.setattr(
        canary,
        "_run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(["send"], 0, json.dumps(send_summary), ""),
    )
    monkeypatch.setattr(canary, "_stop_bridge", lambda *_args, **_kwargs: {"attempted": True})

    result = canary.run_managed_live_interrupt(args, evidence_root, "/exact/codex")

    assert result["failure_code"] == "managed_live_interrupt_turn_mismatch"
    assert result["interrupted_turn_id"] == "turn-other"
    assert result["send_summary"] == send_summary
