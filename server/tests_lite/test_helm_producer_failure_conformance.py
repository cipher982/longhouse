from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


class _FakeProcess:
    pid = 321

    def __init__(self, *, returncode: int | None = None) -> None:
        self.returncode = returncode
        self._alive = returncode is None

    def poll(self) -> int | None:
        return None if self._alive else self.returncode

    def wait(self, *args, **kwargs) -> int:
        self._alive = False
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode

    def kill(self) -> None:
        self._alive = False
        self.returncode = -9


class _FakePty:
    def __init__(self, terminal_path: Path, *, stale: bool = False) -> None:
        self.process = _FakeProcess(returncode=1 if stale else None)
        self.terminal_path = terminal_path
        self.reader_join_timed_out = False
        self.recording_incomplete = False
        self.reader_error = None
        self.recording_lock_timed_out = False
        self.reader_wake_timed_out = False
        self.master_close_timed_out = False
        terminal_path.parent.mkdir(parents=True, exist_ok=True)
        terminal_path.write_text("execution owner\n" if stale else "", encoding="utf-8")
        self.reader_wake_timed_out = False
        self.master_close_timed_out = False

    @classmethod
    def start(cls, *, terminal_path: Path, **kwargs):
        return cls(terminal_path)

    def alive(self) -> bool:
        return self.process.poll() is None

    def close(self) -> None:
        self.process.wait()


def _binary(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_text("fixture\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _args(tmp_path: Path, binary: Path, **overrides: object) -> argparse.Namespace:
    values = {
        "evidence_root": tmp_path / "evidence",
        "provider_bin": binary,
        "codex_bin": binary,
        "engine": binary,
        "longhouse_cli": binary,
        "repo_root": tmp_path,
        "api_url": "https://runtime.example",
        "agents_token": "fixture-token",
        "model": "fixture-model",
        "project": "fixture",
        "launch_timeout_secs": 1.0,
        "response_timeout_secs": 1.0,
        "live_send_timeout_secs": 1.0,
        "timeout_secs": 1.0,
        "max_archive_lag_secs": 1.0,
        "provider_version": "1.2.3",
        "negative_control": None,
        "variant": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_claude_entrypoint_retains_send_before_native_late_failure(monkeypatch, tmp_path) -> None:
    from zerg.qa import claude_helm_lifecycle as producer

    binary = _binary(tmp_path, "claude")

    class _ClaudeSession(_FakePty):
        def submit_line(self, _text: str) -> None:
            raise RuntimeError("late Claude native steer failure")

    session = _ClaudeSession(tmp_path / "terminal.log")
    monkeypatch.setattr(producer, "scanner_visible_claude_binary", lambda path, **_: path)
    monkeypatch.setattr(
        producer,
        "start_machine_and_shipper",
        lambda *a, **k: (
            type(
                "Shipper",
                (),
                {
                    "flush": lambda *_: {
                        "status": "pass",
                        "exit_code": 0,
                        "daemon_paused": True,
                        "daemon_restarted": True,
                        "events_shipped": 0,
                    },
                    "stop": lambda *_: {"status": "pass", "stopped": True, "process_dead": True},
                    "receipt": {"status": "pass", "machine_name": "fixture-machine"},
                },
            )(),
            {},
        ),
    )
    monkeypatch.setattr(producer, "prepare_claude_profile", lambda **kwargs: {"status": "pass"})
    monkeypatch.setattr(producer, "claude_launch_environment", lambda environment, **kwargs: dict(environment))
    monkeypatch.setattr(producer, "launch_claude_session", lambda **kwargs: (session, "claude-session", "native-claude"))
    monkeypatch.setattr(producer, "close_session", lambda value: {"alive_after_close": False, "status": "pass"})
    monkeypatch.setattr(producer, "secret_scan", lambda *a, **k: [])
    monkeypatch.setattr(producer, "api_json_tolerant", lambda *a, **k: {"session_id": "claude-session"})
    monkeypatch.setattr(producer, "_channel_state", lambda *a, **k: {"ready": True, "provider_session_id": "native-claude"})
    monkeypatch.setattr(producer, "_served_state", lambda *a, **k: {"control": {"actions": {"send_input": {"state": "available"}}}})
    monkeypatch.setattr(producer, "_transcript_rows", lambda *a, **k: [])
    monkeypatch.setattr(producer, "_hosted_assistant_texts", lambda *a, **k: [])
    monkeypatch.setattr(producer, "send_outcome", lambda *a, **k: "answered")
    monkeypatch.setattr(producer, "_turn_bounds", lambda *a, **k: (0, 1))
    monkeypatch.setattr(producer, "wait_until", lambda predicate, **kwargs: True)
    monkeypatch.setattr(producer, "_post", lambda *a, **k: {"accepted": True})

    # The factory passes each cell's execution key as --variant, never the authored variant.
    variant = producer._VARIANTS[0]
    result = producer.run_lifecycle(_args(tmp_path, binary, variant=variant))
    report = _json(tmp_path / "evidence" / "lifecycle-report.json")
    payload = _json(tmp_path / "evidence" / "result.json")

    assert result["status"] == "fail"
    assert payload["status"] == "fail"
    assert report["error"] == "RuntimeError: late Claude native steer failure"
    assert report["lifecycle"]["launch_registration"]["passed"] is True
    assert report["lifecycle"]["send_idle"]["passed"] is True
    assert "steer_active" not in report["lifecycle"]
    assert _json(tmp_path / "evidence" / "session-close-receipt.json")["alive_after_close"] is False


def test_codex_entrypoint_retains_rollout_before_native_late_failure(monkeypatch, tmp_path) -> None:
    from zerg.qa import codex_helm_lifecycle as producer

    binary = _binary(tmp_path, "codex")
    state_file = tmp_path / "bridge-state.json"
    rollout = tmp_path / "rollout.jsonl"
    state_file.write_text(
        json.dumps(
            {"thread_path": str(rollout), "last_turn_status": "completed", "pid": 101, "app_server_pid": 102, "app_server_pgid": 103}
        ),
        encoding="utf-8",
    )
    calls = {"send": 0}

    def fake_start(*args, **kwargs):
        return ({"session_id": "codex-session", "state_file": str(state_file), "thread_id": "thread-1"}, None, kwargs["isolation_root"])

    def fake_run(argv, **kwargs):
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, "codex 1.2.3\n", "")
        if "send" in argv:
            calls["send"] += 1
            if calls["send"] == 1:
                text = argv[argv.index("--text") + 1]
                marker = text.split("exactly ", 1)[1].split(" and", 1)[0]
                rollout.write_text(
                    "\n".join(
                        [
                            json.dumps({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1"}}),
                            json.dumps(
                                {
                                    "type": "event_msg",
                                    "payload": {
                                        "type": "item_completed",
                                        "turn_id": "t1",
                                        "item": {"type": "UserMessage", "content": [{"type": "text", "text": marker}]},
                                    },
                                }
                            ),
                            json.dumps(
                                {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t1", "last_agent_message": marker}}
                            ),
                        ]
                    )
                    + "\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(argv, 0, json.dumps({"turn_id": "t1"}), "")
            raise RuntimeError("late Codex native steer failure")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(producer.bridge_canary, "_start_bridge", fake_start)
    monkeypatch.setattr(producer.bridge_canary, "_run", fake_run)
    # The bridge state above names processes that never were. Whether the host happens to have live ones under
    # those numbers is not this test's subject: under a parallel run it does, and the stop then reads as failed.
    monkeypatch.setattr(producer.bridge_canary, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(
        producer.bridge_canary,
        "_stop_bridge",
        lambda *a, **k: {"verification": {"verified": True, "socket_absent": True, "owned_processes_dead": True}, "evidence": {}},
    )
    monkeypatch.setattr(producer, "sha256_file", lambda path: "digest")
    variant = producer._VARIANTS[0]
    result = producer.run_codex_helm_lifecycle(_args(tmp_path, binary, variant=variant))
    payload = _json(tmp_path / "evidence" / "result.json")
    cleanup = _json(tmp_path / "evidence" / "cleanup-receipt.json")

    assert result["status"] == "fail"
    assert payload["status"] == "fail"
    # Steer raised before its phase returned, so it was never reached; send and
    # terminate held. No failing verdict was reached: a typed harness failure,
    # with the phases it saw kept as evidence and steer never reported False.
    assert "observation" not in payload and "assertions" not in payload
    assert "send" in payload["partial_observation"] and "steer" not in payload["partial_observation"]
    assert payload["error"] == "RuntimeError: late Codex native steer failure"
    assert cleanup["required_cleanup"]["final_bridge_stopped"] is True
    assert _json(tmp_path / "evidence" / "provider-rollout.json")[0]["type"] == "event_msg"


def test_opencode_entrypoint_retains_send_before_runtime_steer_failure(monkeypatch, tmp_path) -> None:
    from zerg.qa import opencode_helm_lifecycle as producer
    from zerg.qa import opencode_qualification_profile

    binary = _binary(tmp_path, "opencode")
    serve_log = tmp_path / "opencode-serve.log"
    serve_log.write_text("stream started and never finished\n", encoding="utf-8")
    state = {
        "session_id": "opencode-session",
        "provider_session_id": "native-opencode",
        "run_id": "run-1",
        "server_url": "http://server",
        "log_path": str(serve_log),
        "ready": True,
    }
    home = tmp_path / "home"
    # The failure snapshot reads the provider's own session status; no network here.
    monkeypatch.setattr(producer, "_opencode_get", lambda *a, **k: {"native-opencode": {"type": "busy"}})
    monkeypatch.setattr(producer.live_session_toolkit, "require_disposable_runtime", lambda *a, **k: None)
    monkeypatch.setattr(producer, "sha256_file", lambda path: "digest")
    monkeypatch.setattr(producer.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "opencode 1.2.3\n", ""))
    monkeypatch.setattr(producer.live_session_toolkit, "isolated_provider_home", lambda: home)
    monkeypatch.setattr(opencode_qualification_profile, "prepare_opencode_qualification_profile", lambda *a, **k: {"status": "pass"})
    config = home / ".config" / "opencode" / "opencode.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        producer.live_session_toolkit,
        "start_transcript_shipper",
        lambda *a, **k: type(
            "Shipper",
            (),
            {
                "flush": lambda *_: {
                    "status": "pass",
                    "exit_code": 0,
                    "daemon_paused": True,
                    "daemon_restarted": True,
                    "events_shipped": 0,
                },
                "stop": lambda *_: {"status": "pass", "stopped": True, "process_dead": True},
                "receipt": {"status": "pass", "machine_name": "fixture-machine"},
            },
        )(),
    )
    monkeypatch.setattr(
        producer.live_session_toolkit,
        "PtyProcess",
        type(
            "FakeSession",
            (),
            {
                "__init__": lambda self, *a, **k: setattr(self, "process", _FakeProcess(returncode=0)),
                "alive": lambda self: False,
                "close": lambda self: None,
                "finalize_recording": lambda self: False,
            },
        ),
    )
    monkeypatch.setattr(producer.live_session_toolkit, "launch_command", lambda *a, **k: [str(binary)])
    monkeypatch.setattr(producer.live_session_toolkit, "wait_state", lambda *a, **k: state)
    monkeypatch.setattr(producer.live_session_toolkit, "wait_opencode_tui_ready", lambda *a, **k: True)
    monkeypatch.setattr(producer, "_session_busy", lambda *a, **k: False)
    monkeypatch.setattr(producer, "_task_prompt", lambda *a, **k: "fixture busy task")
    busy_calls = {"count": 0}
    monkeypatch.setattr(
        producer, "_session_busy", lambda *a, **k: (busy_calls.__setitem__("count", busy_calls["count"] + 1) or busy_calls["count"] == 2)
    )
    monkeypatch.setattr(
        producer,
        "_messages",
        lambda *a, **k: [
            {"info": {"id": "user-task", "role": "user"}, "parts": [{"type": "text", "text": "fixture busy task"}]},
            {
                "info": {"id": "assistant-tool", "role": "assistant", "parentID": "user-task", "finish": "tool"},
                "parts": [{"type": "tool", "state": {"input": {"command": "sleep 1"}}}],
            },
        ],
    )
    monkeypatch.setattr(producer, "_wait_marker_answer", lambda *a, **k: [])
    calls = {"count": 0}

    def runtime_input(*args, **kwargs):
        calls["count"] += 1
        if kwargs.get("intent") == "steer":
            raise RuntimeError("late OpenCode runtime steer failure")
        return {"accepted": True, "request": {"path": "/input"}, "payload": {}}

    monkeypatch.setattr(producer, "_runtime_input", runtime_input)
    monkeypatch.setattr(producer, "_runtime_post", lambda *a, **k: {"accepted": True})
    monkeypatch.setattr(producer.live_session_toolkit, "provider_process_pid", lambda *a, **k: None)
    monkeypatch.setattr(producer.live_session_toolkit, "cleanup_processes", lambda *a, **k: {"verified": True, "orphan_count": 0})
    monkeypatch.setattr(producer, "_kill_session_processes", lambda *a, **k: [])
    monkeypatch.setattr(producer.console_lifecycle, "_wait_served_run_retirement", lambda *a, **k: {"retired": True, "active_run_count": 0})
    monkeypatch.setattr(
        producer.live_session_toolkit,
        "retire_qualification_session",
        lambda *a, **k: {"status": "pass", "hidden": True, "archived": True, "present_in_served_inventory": False},
    )

    variant = producer._VARIANTS[0]
    result = producer.run_opencode_helm_lifecycle(_args(tmp_path, binary, variant=variant))
    payload = _json(tmp_path / "evidence" / "result.json")
    # Steer raised before its record was written, so it is never reported. Launch
    # was reached (its input was dispatched) and fails on this fixture, which has
    # no connection_id: a reached failing step, so the result stays a verdict over
    # the reached steps only.
    assert payload["assertions"] == {"opencode_helm_launch_registration": False, "opencode_helm_send_idle": True}
    observation = payload["observation"]
    assert "steer" not in observation

    assert result["status"] == "fail"
    assert result["failure_code"] == "opencode_helm_lifecycle_failed"
    assert result["error"] == "RuntimeError: late OpenCode runtime steer failure"
    assert payload["status"] == "fail"
    assert observation["send"]["dispatch_accepted"] is True
    assert observation["error"] == "RuntimeError: late OpenCode runtime steer failure"
    assert observation["cleanup"]["provider_process_dead"] is True
    assert observation["cleanup"]["no_orphan_provider_processes"] is True
    assert observation["cleanup"]["served_run_retired"] is True
    assert observation["cleanup"]["canary_session_hidden"] is True
    assert observation["cleanup"]["shipper_stopped"] is True
    assert observation["cleanup"]["status"] == "pass"
    assert calls["count"] >= 2
    # What OpenCode itself knew when the scenario gave up, and its server's log, are kept:
    # before this the 2026-09-30 stall could only be attributed from OpenRouter's side.
    snapshot = _json(tmp_path / "evidence" / "native-messages-at-failure.json")
    assert snapshot["session_status"] == {"native-opencode": {"type": "busy"}}
    assert [row["id"] for row in snapshot["messages"]] == ["user-task", "assistant-tool"]
    assert observation["stall_snapshot"] == {
        "file": "native-messages-at-failure.json",
        "message_count": 2,
        "session_status": {"native-opencode": {"type": "busy"}},
    }
    assert (tmp_path / "evidence" / "opencode-serve.log").read_text(encoding="utf-8") == "stream started and never finished\n"
    assert observation["serve_log"]["bytes_retained"] == len("stream started and never finished\n")


def test_a_failed_snapshot_write_is_reported_and_never_masks_the_failure(monkeypatch, tmp_path) -> None:
    from zerg.qa import opencode_helm_lifecycle as producer

    monkeypatch.setattr(producer, "_opencode_get", lambda *a, **k: {})
    monkeypatch.setattr(producer, "_messages", lambda *a, **k: [])

    def refuse(*_a: object, **_k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(producer, "_write_json", refuse)
    state = {"server_url": "http://server", "provider_session_id": "native-opencode"}

    assert producer._write_stall_snapshot(tmp_path, state) == {"file": None, "error": "OSError: disk full"}


def test_the_busy_sampler_and_the_idle_watch_survive_a_status_read_that_keeps_failing(monkeypatch) -> None:
    import http.client

    from zerg.qa import opencode_helm_lifecycle as producer

    reads = {"count": 0}

    def flaky(_state):
        reads["count"] += 1
        if reads["count"] <= 2:
            raise http.client.BadStatusLine("")  # an HTTPException that is not an OSError
        return False

    monkeypatch.setattr(producer, "_session_busy", flaky)
    monkeypatch.setattr(producer.time, "sleep", lambda _s: None)

    samples = producer._watch_until_idle({"provider_session_id": "p"}, timeout=5)

    assert [("error" in sample) for sample in samples[:2]] == [True, True]
    assert sum(1 for sample in samples if sample.get("busy") is False) >= 4
    # A failed read is evidence and never counts toward the idle streak.
    assert all("busy" not in sample for sample in samples if "error" in sample)

    reads["count"] = 0
    sampler = producer._BusySampler({"provider_session_id": "p"})
    monkeypatch.setattr(sampler._stop, "wait", lambda _s: sampler._stop.is_set() or sampler._stop.set())
    sampler._run()
    assert "error" in sampler.samples[0]
