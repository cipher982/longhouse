from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

COVERED_PRODUCERS = frozenset(
    {
        "zerg.qa.claude_helm_lifecycle",
        "zerg.qa.codex_helm_lifecycle",
        "zerg.qa.cursor_helm_lifecycle",
        "zerg.qa.opencode_helm_lifecycle",
        "zerg.qa.pi_helm_lifecycle",
        "zerg.qa.omp_helm_lifecycle",
    }
)


def test_all_registered_producers_have_failure_conformance() -> None:
    from tests_lite.test_claude_codex_producer_failure_conformance import COVERED_PRODUCERS as claude_codex
    from tests_lite.test_console_producer_failure_conformance import COVERED_PRODUCERS as console
    from tests_lite.test_other_provider_failure_conformance import COVERED_PRODUCERS as other
    from zerg.qa.provider_factory_model import PRODUCER_MODULES

    assert COVERED_PRODUCERS | claude_codex | console | other == set(PRODUCER_MODULES)


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
        terminal_path.parent.mkdir(parents=True, exist_ok=True)
        terminal_path.write_text("execution owner\n" if stale else "", encoding="utf-8")

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

    result = producer.run_lifecycle(_args(tmp_path, binary))
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
    monkeypatch.setattr(
        producer.bridge_canary,
        "_stop_bridge",
        lambda *a, **k: {"verification": {"verified": True, "socket_absent": True, "owned_processes_dead": True}, "evidence": {}},
    )
    monkeypatch.setattr(producer, "sha256_file", lambda path: "digest")
    result = producer.run_codex_helm_lifecycle(_args(tmp_path, binary))
    payload = _json(tmp_path / "evidence" / "result.json")
    cleanup = _json(tmp_path / "evidence" / "cleanup-receipt.json")

    assert result["status"] == "fail"
    assert payload["status"] == "fail"
    assert payload["assertions"]["codex_helm_send_idle"] is True
    assert payload["assertions"]["codex_helm_steer_active"] is False
    assert payload["error"] == "RuntimeError: late Codex native steer failure"
    assert cleanup["required_cleanup"]["final_bridge_stopped"] is True
    assert _json(tmp_path / "evidence" / "provider-rollout.json")[0]["type"] == "event_msg"


def test_cursor_entrypoint_retains_product_report_before_runtime_failure(monkeypatch, tmp_path) -> None:
    from zerg.qa import cursor_helm_lifecycle as producer
    from zerg.qa import cursor_helm_product_e2e as e2e

    binary = _binary(tmp_path, "cursor")
    root = tmp_path / "cursor-state"
    session_id = "cursor-session"
    state = {"ready": True, "session_id": session_id, "cursor_pid": 321, "run_id": "run-1", "socket_path": "/tmp/cursor.sock"}
    wait_values = {
        "Cursor Helm managed state": state,
        "native Cursor binding claim": {"conversation_uuid": "conversation-1"},
        "first Cursor reply in hosted archive": {"events": [], "total": 1},
        "remote Cursor reply in hosted archive": {"events": [], "total": 1},
        "first Cursor shell step": {"generation_id": "generation-1"},
    }

    class _Response:
        status_code = 200
        text = "{}"
        is_error = False

        def __init__(self, payload=None):
            self._payload = payload or {"events": [], "total": 1}

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    posts = {"count": 0}

    def fake_post(url, **kwargs):
        posts["count"] += 1
        if "/send-live" in url and posts["count"] == 2:
            raise RuntimeError("late Cursor runtime steer failure")
        return _Response({"accepted": True})

    def fake_wait(predicate, *, description, **kwargs):
        return wait_values.get(description, {"run_id": "run-1", "lifecycle": "running", "activity": "quiescent"})

    monkeypatch.setattr(e2e, "get_managed_local_dir", lambda name: root)
    monkeypatch.setattr(e2e.shutil, "which", lambda value: str(binary))
    monkeypatch.setattr(e2e, "_PtyProcess", type("FakePty", (), {"start": lambda cls, *a, **k: _FakePty(tmp_path / "cursor-terminal.raw")}))
    monkeypatch.setattr(e2e, "_wait_until", fake_wait)
    monkeypatch.setattr(e2e, "_state_ids", lambda path: set())
    monkeypatch.setattr(e2e, "_response_observed_at", lambda *a, **k: e2e.datetime.now(e2e.UTC))
    monkeypatch.setattr(e2e, "_hook_rows", lambda *a, **k: [])
    monkeypatch.setattr(e2e, "_engine_command", lambda *a, **k: None)
    monkeypatch.setattr(e2e.httpx, "get", lambda *a, **k: _Response())
    monkeypatch.setattr(e2e.httpx, "post", fake_post)
    monkeypatch.setattr(producer, "sha256_file", lambda path: "digest")
    monkeypatch.setattr(producer.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "cursor 1.2.3\n", ""))
    monkeypatch.setattr(producer, "isolated_provider_home", lambda: tmp_path / "home")
    monkeypatch.setattr(
        producer,
        "start_transcript_shipper",
        lambda *a, **k: type(
            "Shipper",
            (),
            {
                "receipt": {"status": "pass", "machine_name": "fixture-machine"},
                "flush": lambda *_: {
                    "status": "pass",
                    "exit_code": 0,
                    "daemon_paused": True,
                    "daemon_restarted": True,
                    "events_shipped": 0,
                },
                "stop": lambda *_: {"status": "pass", "stopped": True, "process_dead": True},
            },
        )(),
    )
    monkeypatch.setattr(producer, "wait_pid_dead", lambda *a, **k: True)
    monkeypatch.setattr(producer, "wait_process_group_dead", lambda *a, **k: True)
    monkeypatch.setattr(producer, "bound_terminal_recordings", lambda *a, **k: None)
    monkeypatch.setattr(producer, "secret_scan", lambda *a, **k: [])

    result = producer.run_lifecycle(_args(tmp_path, binary, longhouse_cli=binary))
    report = _json(tmp_path / "evidence" / "product-e2e-report.json")
    product = _json(tmp_path / "evidence" / "product-e2e" / "product-e2e.json")

    assert result["status"] == "fail"
    assert report["status"] == "failed"
    assert report["lifecycle"]["launch_registration"]["native_binding_claimed"] is True
    assert report["lifecycle"]["send_idle"]["remote_reply_archived"] is True
    assert product["lifecycle"]["send_idle"]["remote_reply_archived"] is True
    assert _json(tmp_path / "evidence" / "cleanup-receipt.json")["required_cleanup"]["no_orphan_provider_processes"] is True


def test_opencode_entrypoint_retains_send_before_runtime_steer_failure(monkeypatch, tmp_path) -> None:
    from zerg.qa import opencode_helm_lifecycle as producer
    from zerg.qa import opencode_qualification_profile

    binary = _binary(tmp_path, "opencode")
    state = {
        "session_id": "opencode-session",
        "provider_session_id": "native-opencode",
        "run_id": "run-1",
        "server_url": "http://server",
        "ready": True,
    }
    home = tmp_path / "home"
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

    result = producer.run_opencode_helm_lifecycle(_args(tmp_path, binary))
    payload = _json(tmp_path / "evidence" / "result.json")
    observation = payload["observation"]

    assert result["status"] == "fail"
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


def test_pi_entrypoint_retains_send_before_native_late_failure(monkeypatch, tmp_path) -> None:
    from zerg.qa import pi_helm_lifecycle as producer

    binary = _binary(tmp_path, "pi")
    session_file = tmp_path / "pi-session.jsonl"
    session_file.write_text("{}\n", encoding="utf-8")
    state = {
        "provider": "pi",
        "ready": True,
        "status": "ready",
        "phase": "idle",
        "session_id": "pi-session",
        "provider_session_id": "native-pi",
        "session_file": str(session_file),
        "session_dir": str(tmp_path),
        "connection_id": 1,
        "lease_generation": 2,
        "launcher_pid": 101,
        "provider_pid": 102,
        "launcher_process_start_time": "birth",
        "provider_process_start_time": "birth",
    }
    snapshot = {
        "detail": {"id": "pi-session", "provider": "pi", "provider_session_id": "native-pi"},
        "thread": {"root_session_id": "pi-session", "head_session_id": "pi-session", "sessions": [{"id": "pi-session"}]},
        "events": {"events": [{"session_id": "pi-session", "role": "assistant", "content_text": "marker"}]},
        "diagnostic": {
            "served_path": "canonical_session_detail",
            "shadow": {"mode": "test", "control": {"actions": {"send_input": {"state": "available"}, "terminate": {"state": "available"}}}},
        },
    }
    binding = {
        "session_id": "pi-session",
        "thread_root_session_id": "pi-session",
        "thread_head_session_id": "pi-session",
        "provider": "pi",
        "provider_session_id": "native-pi",
        "provider_session_bound": True,
        "one_session": True,
        "one_thread": True,
        "event_count": 1,
    }
    native = {
        "metadata": {"provider_session_id": "native-pi", "model": "fixture"},
        "taxonomy": {"source": "fixture", "tool_pairs": [{"tool": "read"}], "tool_calls_without_results": []},
        "invocation_rows": [{"model": "fixture"}],
        "assistant_marker_rows": 1,
        "user_marker_rows": 1,
        "user_marker_occurrences": 1,
        "rows": 3,
        "source_end_offset": 3,
    }

    monkeypatch.setattr(producer, "require_disposable_runtime", lambda *a, **k: None)
    monkeypatch.setattr(
        producer,
        "new_qualification_isolation_root",
        lambda *a, **k: (tmp_path / "pi-isolation").mkdir(parents=True, exist_ok=True) or tmp_path / "pi-isolation",
    )
    monkeypatch.setattr(
        producer,
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
        producer, "ProviderPtySession", type("FakeSession", (), {"start": lambda *a, **k: _FakePty(tmp_path / "pi-terminal.raw")})
    )
    monkeypatch.setattr(producer, "_wait_state", lambda *a, **k: state)
    monkeypatch.setattr(producer, "_wait_native_marker", lambda *a, **k: native)
    monkeypatch.setattr(producer, "_wait_runtime_convergence", lambda *a, **k: {"binding": binding, "snapshot": snapshot})
    monkeypatch.setattr(producer, "_runtime_diagnostic", lambda *a, **k: snapshot["diagnostic"])
    monkeypatch.setattr(producer.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "101 101 birth", ""))
    monkeypatch.setattr(producer, "_pid_alive", lambda *a, **k: False)
    monkeypatch.setattr(producer, "_pid_dead", lambda *a, **k: True)
    monkeypatch.setattr(producer, "_pgid_dead", lambda *a, **k: True)
    calls = {"send": 0}

    def run_engine(engine, kind, session_id, env, *, text=None):
        if kind == "send":
            calls["send"] += 1
            if calls["send"] == 2:
                raise RuntimeError("late Pi native follow-up failure")
        return {"accepted": True, "command": [str(engine), "pi-helm", kind], "payload": {"ok": True}}

    monkeypatch.setattr(producer, "_run_engine", run_engine)
    monkeypatch.setattr(producer, "_send_live", lambda *a, **k: {"accepted": True})
    monkeypatch.setattr(producer.console_lifecycle, "_terminate_live_qualification_session", lambda *a, **k: {"accepted": True})
    monkeypatch.setattr(producer.console_lifecycle, "_wait_served_run_retirement", lambda *a, **k: {"retired": True, "active_run_count": 0})
    monkeypatch.setattr(
        producer,
        "retire_qualification_session",
        lambda *a, **k: {"status": "pass", "hidden": True, "archived": True, "present_in_served_inventory": False},
    )
    monkeypatch.setattr(producer, "_wait_recorded_execution_owners_dead", lambda *a, **k: None)
    monkeypatch.setattr(producer, "sha256_file", lambda path: "digest")

    result = producer.run_pi_helm_lifecycle(_args(tmp_path, binary))
    payload = _json(tmp_path / "evidence" / "result.json")

    assert result["status"] == "fail"
    assert payload["status"] == "fail"
    assert payload["diagnostic_observation"]["send_idle"] is True
    assert payload["error"] == "RuntimeError: late Pi native follow-up failure"
    assert _json(tmp_path / "evidence" / "cleanup-receipt.json")["status"] == "fail"


def test_omp_entrypoint_retains_send_before_native_late_failure(monkeypatch, tmp_path) -> None:
    from zerg.qa import omp_helm_lifecycle as producer

    binary = _binary(tmp_path, "omp")
    monkeypatch.setenv("LONGHOUSE_OMP_LIVE", "1")
    session_file = tmp_path / "omp-session.jsonl"
    session_file.write_text("{}\n", encoding="utf-8")
    state = {
        "ready": True,
        "status": "ready",
        "phase": "idle",
        "session_id": "omp-session",
        "run_id": "run-1",
        "native_session_id": "native-omp",
        "session_file": str(session_file),
        "connection_id": 1,
        "lease_generation": 2,
        "agent_end_observed": True,
    }
    row = {"type": "message", "id": "event-1", "_source_offset": 1, "message": {"role": "assistant", "content": "marker"}}
    calls = {"send": 0}

    def command(_engine, kind, session_id, env, *, text=None):
        if kind == "send":
            calls["send"] += 1
            if calls["send"] == 3:
                raise RuntimeError("late OMP native follow-up failure")
            request_id = f"request-{calls['send']}"
            return {
                "accepted": True,
                "payload": {"client_request_id": request_id},
                "request": {
                    "method": "POST",
                    "path": f"/api/agents/sessions/{session_id}/input",
                    "payload": {"text": text or "marker", "intent": "auto", "client_request_id": request_id},
                },
            }
        return {"accepted": True, "payload": {}, "request": {}}

    class _OmpSession(_FakePty):
        @classmethod
        def start(cls, *, terminal_path: Path, **kwargs):
            return cls(terminal_path, stale="stale" in terminal_path.name)

    monkeypatch.setattr(producer, "require_disposable_runtime", lambda *a, **k: None)
    monkeypatch.setattr(
        producer,
        "new_qualification_isolation_root",
        lambda *a, **k: (tmp_path / "omp-isolation").mkdir(parents=True, exist_ok=True) or tmp_path / "omp-isolation",
    )
    monkeypatch.setattr(
        producer,
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
    monkeypatch.setattr(producer, "ProviderPtySession", _OmpSession)
    monkeypatch.setattr(
        producer,
        "_process_record",
        lambda pid, expected_birth, label, *, owner: {
            "owner": owner,
            "label": label,
            "pid": 321,
            "process_group_id": 321,
            "pgid": 321,
            "birth": "birth",
            "expected_birth": "birth",
            "birth_matches": True,
            "pid_positive": True,
            "process_group_positive": True,
            "pid_dead": True,
            "process_group_dead": True,
            "alive": False,
        },
    )
    monkeypatch.setattr(producer, "_wait_state", lambda *a, **k: state | ({"phase": "running"} if k.get("predicate") else {}))
    monkeypatch.setattr(
        producer, "_wait_native_marker", lambda _path, marker, **_kwargs: row | {"message": {"role": "assistant", "content": marker}}
    )
    monkeypatch.setattr(
        producer,
        "_wait_channel_terminal",
        lambda *a, **k: ({"type": "agent_end", "source": "omp_helm_extension_channel", "isTerminal": True, "willContinue": False}, state),
    )
    monkeypatch.setattr(producer, "_run_engine", command)
    monkeypatch.setattr(producer, "_runtime_convergence", lambda *a, **k: {"status": "pass"})
    monkeypatch.setattr(producer, "_wait_runtime_control_identity", lambda *a, **k: {"status": "pass"})
    monkeypatch.setattr(producer, "_wait_task_tool_boundary", lambda *a, **k: {"status": "pass"})
    monkeypatch.setattr(producer, "_read_source_size", lambda *a, **k: 0)
    monkeypatch.setattr(producer.lifecycle, "_terminate_live_qualification_session", lambda *a, **k: {"status": "pass"})
    monkeypatch.setattr(
        producer,
        "_wait_cleanup_receipt",
        lambda *a, **k: {"status": "pass", "provider_process_dead": True, "process_group_dead": True, "no_orphan_provider_processes": True},
    )
    monkeypatch.setattr(producer, "_wait_served_run_retirement", lambda *a, **k: {"retired": True, "active_run_count": 0})
    monkeypatch.setattr(producer, "_record_retirement_claim_terminal", lambda *a, **k: None)
    monkeypatch.setattr(
        producer,
        "retire_qualification_session",
        lambda *a, **k: {"status": "pass", "hidden": True, "archived": True, "present_in_served_inventory": False},
    )
    monkeypatch.setattr(producer, "sha256_file", lambda path: "digest")

    args = _args(tmp_path, binary, variant=producer._VARIANTS[0])
    exit_code = producer.main(
        [
            "--variant",
            args.variant,
            "--evidence-root",
            str(args.evidence_root),
            "--repo-root",
            str(args.repo_root),
            "--engine",
            str(args.engine),
            "--longhouse-cli",
            str(args.longhouse_cli),
            "--provider-bin",
            str(args.provider_bin),
            "--provider-version",
            args.provider_version,
            "--api-url",
            args.api_url,
            "--agents-token",
            args.agents_token,
        ]
    )
    payload = _json(tmp_path / "evidence" / "result.json")

    assert exit_code == 1
    assert payload["status"] == "fail"
    assert payload["observation"]["send_idle"] is True
    assert payload["assertions"]["omp_helm_send_idle"] is True
    assert payload["assertions"]["omp_helm_follow_up_native"] is False
    assert payload["error"] == "RuntimeError: late OMP native follow-up failure"
    cleanup = _json(tmp_path / "evidence" / "cleanup-receipt.json")
    assert cleanup["status"] == "fail"
    assert payload["observation"]["cleanup"] == cleanup
