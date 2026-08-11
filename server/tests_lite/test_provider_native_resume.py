from __future__ import annotations

import argparse
import hashlib
import json
import signal
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from zerg.managed_provider_contract_manifest import managed_provider_contract_entry_digest
from zerg.qa import antigravity_resume_policy
from zerg.qa import codex_native_resume
from zerg.qa import provider_native_resume
from zerg.qa.codex_native_resume import _redact_process_command
from zerg.qa.codex_native_resume import _validate_resume_intent
from zerg.qa.codex_native_resume import _write_json as write_codex_json
from zerg.qa.codex_native_resume import _write_resume_contract_snapshot
from zerg.qa.provider_native_resume import SPECS
from zerg.qa.provider_native_resume import TranscriptShipper
from zerg.qa.provider_native_resume import _accept_claude_development_channel_prompt
from zerg.qa.provider_native_resume import _accept_claude_permission_prompt
from zerg.qa.provider_native_resume import _accept_cursor_workspace_trust
from zerg.qa.provider_native_resume import _claude_input_prompt_visible
from zerg.qa.provider_native_resume import _cleanup_processes
from zerg.qa.provider_native_resume import _command_from_resume_intent
from zerg.qa.provider_native_resume import _control_send
from zerg.qa.provider_native_resume import _cursor_bootstrap_correlation
from zerg.qa.provider_native_resume import _cursor_bootstrap_prompt
from zerg.qa.provider_native_resume import _cursor_idle_then_flush
from zerg.qa.provider_native_resume import _cursor_interrupt_to_idle
from zerg.qa.provider_native_resume import _cursor_projection_diagnostics
from zerg.qa.provider_native_resume import _cursor_tui_input_ready
from zerg.qa.provider_native_resume import _finalize_result_payload
from zerg.qa.provider_native_resume import _initialize_cursor_workspace
from zerg.qa.provider_native_resume import _isolated_provider_home
from zerg.qa.provider_native_resume import _launch_command
from zerg.qa.provider_native_resume import _opencode_tui_is_connected
from zerg.qa.provider_native_resume import _post_resume_response_correlated
from zerg.qa.provider_native_resume import _provider_process_pid
from zerg.qa.provider_native_resume import _provision_transcript_roots
from zerg.qa.provider_native_resume import _refresh_failure_result_manifest
from zerg.qa.provider_native_resume import _resume_intent_timeout
from zerg.qa.provider_native_resume import _resume_marker
from zerg.qa.provider_native_resume import _resume_marker_prompt
from zerg.qa.provider_native_resume import _start_transcript_shipper
from zerg.qa.provider_native_resume import _state_candidates
from zerg.qa.provider_native_resume import _wait_assistant_response_after_marker
from zerg.qa.provider_native_resume import _wait_claude_tui_ready
from zerg.qa.provider_native_resume import _wait_cursor_bootstrap_hook_sequence
from zerg.qa.provider_native_resume import _wait_cursor_hook_sequence
from zerg.qa.provider_native_resume import _wait_cursor_idle
from zerg.qa.provider_native_resume import _wait_cursor_tui_ready
from zerg.qa.provider_native_resume import _wait_session_tail
from zerg.qa.provider_native_resume import _wait_state
from zerg.qa.provider_native_resume import _write_best_effort_json
from zerg.qa.provider_native_resume import registration_for


def _args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        engine=tmp_path / "engine",
        longhouse_cli=tmp_path / "longhouse",
        repo_root=tmp_path / "repo",
        api_url="https://runtime.example",
        agents_token="device-token",
        provider_bin=tmp_path / "provider",
    )


def test_each_native_provider_registers_both_exact_resume_variants() -> None:
    for provider in ("claude", "cursor", "opencode"):
        registration = registration_for(provider)
        assert registration.providers == (provider,)
        assert registration.assertion_cells == (
            ("native_provider_resume_proven", "clean_exit"),
            ("native_provider_resume_proven", "process_loss"),
        )
        assert registration.evidence_classes == ("live_token",)
        assert registration.executable is True
        assert registration.executable_module == SPECS[provider].executable_module
        assert {
            "transcript_shipper_receipt",
            "initial_seed_send",
            "initial_transcript_ship_receipt",
            "post_resume_response_correlation",
            "post_resume_transcript_ship_receipt",
            "post_stop_transcript_ship_receipt",
        } <= set(registration.required_artifacts)
        cursor_only = {
            "resume_bootstrap_response_correlation",
            "resume_bootstrap_transcript",
        }
        if provider == "cursor":
            assert cursor_only <= set(registration.required_artifacts)
        else:
            assert cursor_only.isdisjoint(registration.required_artifacts)


def test_cursor_resume_bootstrap_uses_a_unique_marker() -> None:
    assert _cursor_bootstrap_prompt() == "Reply with exactly READY and nothing else. Do not use tools or inspect files."
    assert _cursor_bootstrap_prompt("LH_CURSOR_BOOTSTRAP_abc123") == (
        "Reply with exactly LH_CURSOR_BOOTSTRAP_abc123 and nothing else. Do not use tools or inspect files."
    )


def test_transcript_shipper_provisions_all_discovery_roots(tmp_path: Path) -> None:
    home = tmp_path / "home"
    configured_claude = tmp_path / "claude-config"

    _provision_transcript_roots(home, {"CLAUDE_CONFIG_DIR": str(configured_claude)})

    for relative in (
        ".codex/sessions",
        ".local/share/opencode",
        ".cursor/chats",
        ".longhouse/agent/cursor-acp-source",
    ):
        assert (home / relative).is_dir()
    assert (configured_claude / "projects").is_dir()


def test_cursor_qualification_workspace_has_project_identity(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "cursor-workspace"
    workspace.mkdir()
    template = tmp_path / "git-template"
    (template / "hooks").mkdir(parents=True)
    (template / "hooks" / "pre-commit").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    global_config = tmp_path / "gitconfig"
    global_config.write_text(f"[init]\n\ttemplatedir = {template}\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(template))

    _initialize_cursor_workspace(workspace)

    assert (workspace / ".git").is_dir()
    assert not (workspace / ".git" / "hooks" / "pre-commit").exists()


def test_transcript_shipper_keeps_runtime_token_out_of_engine_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    args = argparse.Namespace(
        api_url="https://runtime.example",
        agents_token="device-token",
        engine=tmp_path / "longhouse-engine",
        repo_root=tmp_path,
    )
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 12345
        returncode: int | None = None

        def __init__(self, argv: list[str], **kwargs: object) -> None:
            captured["argv"] = argv
            db_path = Path(argv[argv.index("--db") + 1])
            socket_path = db_path.parent / "transcript-wake.sock"
            socket_path.touch()

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            self.returncode = 0
            return 0

    monkeypatch.setattr("zerg.qa.provider_native_resume.subprocess.Popen", FakeProcess)
    monkeypatch.setattr(
        "zerg.qa.provider_native_resume._api_json",
        lambda *_args, **_kwargs: {"machine_id": "sauron-clifford"},
    )
    monkeypatch.setattr("zerg.qa.provider_native_resume.os.killpg", lambda *_args: None)
    monkeypatch.setattr("zerg.qa.provider_native_resume._wait_process_group_dead", lambda _pid: True)

    shipper = _start_transcript_shipper(
        "codex",
        args,
        home=home,
        environment={"HOME": str(home), "CLAUDE_CONFIG_DIR": str(tmp_path / "staged-claude")},
        evidence_root=evidence,
    )
    argv = [str(value) for value in captured["argv"]]
    assert "--token" not in argv
    flush_run: dict[str, object] = {}

    class Completed:
        returncode = 0
        stdout = json.dumps(
            {
                "protocol": "storage-v2",
                "files_scanned": 3,
                "files_shipped": 2,
                "events_shipped": 4,
                "spool_replayed": 1,
            }
        )
        stderr = ""

    def fake_run(command: list[str], **_kwargs: object) -> Completed:
        flush_run["command"] = command
        return Completed()

    monkeypatch.setattr("zerg.qa.provider_native_resume.subprocess.run", fake_run)
    flush_receipt = shipper.flush("initial")
    flush_argv = [str(value) for value in flush_run["command"]]
    assert "device-token" not in flush_argv
    assert flush_argv[flush_argv.index("--machine-name") + 1] == "sauron-clifford"
    assert flush_receipt["status"] == "pass"
    assert flush_receipt["events_shipped"] == 4
    assert (home / ".longhouse/machine/device-token").read_text().strip() == "device-token"
    assert (home / ".longhouse/machine/state.json").read_text().find("sauron-clifford") >= 0
    assert shipper.stop()["process_dead"] is True


def test_transcript_shipper_flush_reuses_enrolled_db_and_restarts_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    args = argparse.Namespace(
        api_url="https://runtime.example",
        agents_token="device-token",
        engine=tmp_path / "longhouse-engine",
        repo_root=tmp_path,
    )
    commands: list[list[str]] = []

    class FakeProcess:
        next_pid = 12000

        def __init__(self, argv: list[str], **kwargs: object) -> None:
            self.pid = FakeProcess.next_pid
            FakeProcess.next_pid += 1
            self.returncode: int | None = None
            commands.append(argv)
            db_path = Path(argv[argv.index("--db") + 1])
            (db_path.parent / "transcript-wake.sock").touch()

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            self.returncode = 0
            return 0

    monkeypatch.setattr("zerg.qa.provider_native_resume.subprocess.Popen", FakeProcess)
    monkeypatch.setattr(
        "zerg.qa.provider_native_resume._api_json",
        lambda *_args, **_kwargs: {"machine_id": "sauron-clifford"},
    )
    monkeypatch.setattr("zerg.qa.provider_native_resume.os.killpg", lambda *_args: None)
    monkeypatch.setattr("zerg.qa.provider_native_resume._wait_process_group_dead", lambda _pid: True)

    class Completed:
        returncode = 0
        stdout = json.dumps({"protocol": "storage-v2", "files_scanned": 1, "events_shipped": 1})
        stderr = ""

    run_commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> Completed:
        run_commands.append(command)
        return Completed()

    monkeypatch.setattr("zerg.qa.provider_native_resume.subprocess.run", fake_run)
    shipper = _start_transcript_shipper(
        "codex",
        args,
        home=home,
        environment={"HOME": str(home), "CLAUDE_CONFIG_DIR": str(tmp_path / "staged-claude")},
        evidence_root=evidence,
        longhouse_home=tmp_path / "longhouse-home",
    )

    receipt = shipper.flush("initial")
    assert receipt["status"] == "pass"
    assert receipt["daemon_paused"] is True
    assert receipt["daemon_restarted"] is True
    assert run_commands[0][run_commands[0].index("--db") + 1] == str(tmp_path / "longhouse-home/agent/longhouse-shipper.db")
    assert len(commands) == 2
    assert shipper.stop()["process_dead"] is True


def test_transcript_shipper_retries_a_quarantined_source_epoch_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        [
            SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="source_epoch_conflict_unresolved: predecessor_not_open_for_this_identity",
            ),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"protocol": "storage-v2", "events_shipped": 2}),
                stderr="",
            ),
        ]
    )
    calls = 0

    def fake_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        return next(responses)

    monkeypatch.setattr("zerg.qa.provider_native_resume.subprocess.run", fake_run)
    shipper = provider_native_resume.TranscriptShipper(
        process=SimpleNamespace(poll=lambda: 0),
        log_stream=None,
        receipt={},
        engine=tmp_path / "engine",
        repo_root=tmp_path,
        api_url="https://runtime.example",
        machine_name="machine-1",
        db_path=tmp_path / "shipper.db",
        engine_environment={},
        evidence_root=tmp_path,
        redaction_secrets=(),
        connect_command=[],
    )

    receipt = shipper.flush("post-resume")

    assert calls == 2
    assert receipt["status"] == "pass"
    assert receipt["attempts"] == 2
    assert receipt["retry_reason"] == "source_epoch_conflict_unresolved"
    assert receipt["events_shipped"] == 2
    log = (tmp_path / "transcript-flush-post-resume.log").read_text()
    assert "attempt 1" in log
    assert "attempt 2" in log


def test_transcript_shipper_retries_storage_lane_backpressure_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        [
            SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="Error: storage-v2 repair lane busy; retry after 5000ms",
            ),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"protocol": "storage-v2", "events_shipped": 1}),
                stderr="",
            ),
        ]
    )
    calls = 0
    sleeps: list[float] = []

    def fake_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        return next(responses)

    monkeypatch.setattr("zerg.qa.provider_native_resume.subprocess.run", fake_run)
    monkeypatch.setattr("zerg.qa.provider_native_resume.time.sleep", sleeps.append)
    shipper = provider_native_resume.TranscriptShipper(
        process=SimpleNamespace(poll=lambda: 0),
        log_stream=None,
        receipt={},
        engine=tmp_path / "engine",
        repo_root=tmp_path,
        api_url="https://runtime.example",
        machine_name="machine-1",
        db_path=tmp_path / "shipper.db",
        engine_environment={},
        evidence_root=tmp_path,
        redaction_secrets=(),
        connect_command=[],
    )

    receipt = shipper.flush("post-resume")

    assert calls == 2
    assert sleeps == [5.0]
    assert receipt["status"] == "pass"
    assert receipt["attempts"] == 2
    assert receipt["retry_reason"] == "storage_lane_busy"
    assert receipt["events_shipped"] == 1


def test_transcript_shipper_caps_storage_lane_backpressure_delay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        [
            SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="Error: storage-v2 repair lane busy; retry after 999999999ms",
            ),
            SimpleNamespace(returncode=1, stdout="", stderr="permanent ship failure"),
        ]
    )
    sleeps: list[float] = []

    monkeypatch.setattr(
        "zerg.qa.provider_native_resume.subprocess.run",
        lambda *_args, **_kwargs: next(responses),
    )
    monkeypatch.setattr("zerg.qa.provider_native_resume.time.sleep", sleeps.append)
    shipper = provider_native_resume.TranscriptShipper(
        process=SimpleNamespace(poll=lambda: 0),
        log_stream=None,
        receipt={},
        engine=tmp_path / "engine",
        repo_root=tmp_path,
        api_url="https://runtime.example",
        machine_name="machine-1",
        db_path=tmp_path / "shipper.db",
        engine_environment={},
        evidence_root=tmp_path,
        redaction_secrets=(),
        connect_command=[],
    )

    receipt = shipper.flush("post-resume")

    assert sleeps == [10.0]
    assert receipt["status"] == "fail"
    assert receipt["attempts"] == 2
    assert receipt["retry_after_ms"] == 999999999
    assert receipt["retry_sleep_secs"] == 10.0
    assert receipt["retry_reasons"] == ["storage_lane_busy"]


def test_transcript_shipper_does_not_retry_untyped_ship_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fake_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        return SimpleNamespace(returncode=1, stdout="", stderr="permanent ship failure")

    monkeypatch.setattr("zerg.qa.provider_native_resume.subprocess.run", fake_run)
    monkeypatch.setattr("zerg.qa.provider_native_resume.time.sleep", lambda _seconds: pytest.fail("unexpected retry"))
    shipper = provider_native_resume.TranscriptShipper(
        process=SimpleNamespace(poll=lambda: 0),
        log_stream=None,
        receipt={},
        engine=tmp_path / "engine",
        repo_root=tmp_path,
        api_url="https://runtime.example",
        machine_name="machine-1",
        db_path=tmp_path / "shipper.db",
        engine_environment={},
        evidence_root=tmp_path,
        redaction_secrets=(),
        connect_command=[],
    )

    receipt = shipper.flush("post-resume")

    assert calls == 1
    assert receipt["status"] == "fail"
    assert receipt["attempts"] == 1


def test_wait_session_tail_retries_projection_404_but_preserves_auth_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter([{"session_id": "session-1", "messages": []}])
    calls = 0

    def projected_tail(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise provider_native_resume._RuntimeHostHTTPError(404, "session not found")
        return next(responses)

    monkeypatch.setattr(provider_native_resume, "_api_json", projected_tail)
    monkeypatch.setattr(provider_native_resume.time, "sleep", lambda _seconds: None)

    assert _wait_session_tail("https://runtime.example", "device-token", "session-1") == {
        "session_id": "session-1",
        "messages": [],
    }
    assert calls == 2

    auth_error = provider_native_resume._RuntimeHostHTTPError(401, "unauthorized")
    monkeypatch.setattr(provider_native_resume, "_api_json", lambda *_args, **_kwargs: (_ for _ in ()).throw(auth_error))
    with pytest.raises(provider_native_resume._RuntimeHostHTTPError):
        _wait_session_tail("https://runtime.example", "device-token", "session-1")

    with pytest.raises(RuntimeError, match="did not project session"):
        _wait_session_tail("https://runtime.example", "device-token", "session-1", timeout=0)

    missing = provider_native_resume._RuntimeHostHTTPError(404, "session not found")
    monkeypatch.setattr(
        provider_native_resume,
        "_api_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(missing),
    )

    assert (
        _wait_session_tail(
            "https://runtime.example",
            "device-token",
            "session-1",
            timeout=0.01,
            allow_unprojected=True,
        )
        == {}
    )


def test_process_loss_resume_wait_covers_machine_reconciliation_window() -> None:
    assert _resume_intent_timeout(variant="clean_exit") == 45.0
    assert _resume_intent_timeout(variant="process_loss") == 180.0


def test_cursor_workspace_trust_is_acknowledged_once(tmp_path: Path) -> None:
    recording = tmp_path / "cursor.tty"
    recording.write_text("Workspace Trust Required\n▶ [a] Trust this workspace\n[q] Quit\n", encoding="utf-8")

    class FakeProcess:
        cursor_workspace_trust_sent = False

        def __init__(self) -> None:
            self.recording = recording
            self.sent: list[str] = []

        def send(self, value: str) -> None:
            self.sent.append(value)

    process = FakeProcess()
    _accept_cursor_workspace_trust(process)  # type: ignore[arg-type]
    _accept_cursor_workspace_trust(process)  # type: ignore[arg-type]

    assert process.sent == ["a"]
    assert process.cursor_workspace_trust_sent is True


def test_cursor_tui_readiness_handles_a_late_workspace_gate(tmp_path: Path) -> None:
    recording = tmp_path / "cursor.tty"
    recording.write_text("cursor-agent starting\n", encoding="utf-8")

    class FakeProcess:
        cursor_workspace_trust_sent = False

        def __init__(self) -> None:
            self.recording = recording
            self.process = SimpleNamespace(poll=lambda: None)
            self.sent: list[str] = []
            self.drains = 0

        def drain(self) -> bytes:
            self.drains += 1
            if self.drains == 2:
                recording.write_text(
                    "Workspace Trust Required\n[a] Trust this workspace\n",
                    encoding="utf-8",
                )
            elif self.drains == 4:
                recording.write_text(
                    "Cursor Agent\nPlan, search, build anything\n",
                    encoding="utf-8",
                )
            return b""

        def send(self, value: str) -> None:
            self.sent.append(value)

        def settle(self, **_kwargs: object) -> bytes:
            return b""

    process = FakeProcess()
    _wait_cursor_tui_ready(process, recording, timeout=2)  # type: ignore[arg-type]

    assert process.sent == ["a"]


def test_cursor_readiness_rejects_the_trust_transition_and_accepts_the_prompt() -> None:
    assert _cursor_tui_input_ready("Workspace Trust Required [a] Trust this workspace") is False
    assert _cursor_tui_input_ready("Cursor Agent — Plan, search, build anything") is True
    assert _cursor_tui_input_ready("Cursor Agent — Trusting workspace...") is False
    assert _cursor_tui_input_ready("Cursor Agent — Loading conversation") is False


def test_cursor_readiness_uses_the_latest_prompt_after_resume_loading() -> None:
    restored = "Cursor Agent — Plan, search, build anything Loading conversation Add a follow-up"
    assert _cursor_tui_input_ready(restored) is True
    assert _cursor_tui_input_ready(f"{restored} Working") is False
    assert _cursor_tui_input_ready(f"{restored} Loading conversation") is False


def test_cursor_readiness_waits_for_completed_turn_redraw(tmp_path: Path) -> None:
    recording = tmp_path / "cursor-resume.tty"
    recording.write_text("Cursor Agent — Add a follow-up Working\n", encoding="utf-8")

    class FakeProcess:
        cursor_workspace_trust_sent = False

        def __init__(self) -> None:
            self.recording = recording
            self.process = SimpleNamespace(poll=lambda: None)
            self.drains = 0

        def drain(self) -> bytes:
            self.drains += 1
            if self.drains == 3:
                recording.write_text("Cursor Agent — Add a follow-up\n", encoding="utf-8")
            return b""

        def settle(self, **_kwargs: object) -> bytes:
            return b""

    process = FakeProcess()
    _wait_cursor_tui_ready(process, recording, timeout=3)  # type: ignore[arg-type]

    assert process.drains >= 3


def test_cursor_resume_markers_stay_compact_and_are_explicitly_prompted() -> None:
    marker = _resume_marker("cursor", "SEED")

    assert marker.startswith("LH_CURSOR_SEED_")
    assert len(marker.rsplit("_", 1)[-1]) == 10
    assert len(marker) < 32
    assert _resume_marker_prompt("cursor", marker) == f"Reply with exactly {marker}"


def test_other_provider_resume_markers_keep_the_long_form() -> None:
    marker = _resume_marker("opencode", "POST")

    assert marker.startswith("LONGHOUSE_OPENCODE_RESUME_POST_")
    assert len(marker.rsplit("_", 1)[-1]) == 32
    assert _resume_marker_prompt("opencode", marker) == f"Reply exactly {marker} and nothing else."


def test_claude_tui_readiness_waits_for_the_provider_input_prompt(tmp_path: Path) -> None:
    recording = tmp_path / "claude.tty"
    recording.write_text("Loading development channel\n", encoding="utf-8")

    class FakeProcess:
        def __init__(self) -> None:
            self.recording = recording
            self.process = SimpleNamespace(poll=lambda: None)
            self.drains = 0
            self.settled = False

        def drain(self) -> bytes:
            self.drains += 1
            if self.drains == 2:
                recording.write_text("development selector\n❯ 1. I am using this for local development\n", encoding="utf-8")
            elif self.drains == 4:
                recording.write_text("main TUI\n❯ \n", encoding="utf-8")
            return b""

        def settle(self) -> bytes:
            self.settled = True
            return b""

    process = FakeProcess()
    _wait_claude_tui_ready(process, recording, timeout=2)  # type: ignore[arg-type]

    assert process.settled is True
    assert _claude_input_prompt_visible('screen redraw❯\u00a0Try "refactor <filepath>"') is True
    assert _claude_input_prompt_visible("❯ 1. I am using this for local development") is False


def test_claude_tui_readiness_accepts_the_bare_prompt_after_a_turn(tmp_path: Path) -> None:
    recording = tmp_path / "claude.tty"
    recording.write_text("\n❯\u00a0\n", encoding="utf-8")

    class FakeProcess:
        def __init__(self) -> None:
            self.recording = recording
            self.process = SimpleNamespace(poll=lambda: None)
            self.settled = False

        def drain(self) -> bytes:
            return b""

        def settle(self) -> bytes:
            self.settled = True
            return b""

    process = FakeProcess()
    _wait_claude_tui_ready(process, recording, timeout=2)  # type: ignore[arg-type]

    assert process.settled is True


def test_opencode_readiness_does_not_treat_disconnected_logs_as_connected() -> None:
    assert _opencode_tui_is_connected("OpenCode event monitor disconnected; retrying") is False
    assert _opencode_tui_is_connected("OpenCode connection lost; retrying") is False
    assert _opencode_tui_is_connected("  OpenCode   CONNECTED to server  ") is True
    assert _opencode_tui_is_connected("OpenCode connected to server") is True
    assert _opencode_tui_is_connected("OpenCode status: longhouse Connected") is True
    assert (
        _opencode_tui_is_connected("\x1b[38;2;238;238;238mlonghouse\x1b[0m \x1b[38;2;128;128;128mConnected\x1b[0mLSPs are disabled") is True
    )


def test_cursor_native_idle_requires_the_provider_hook_phase(tmp_path: Path) -> None:
    state = {"session_id": "session-1", "provider_session_id": "cursor-thread-1", "run_id": "run-1"}
    longhouse_home = tmp_path / "longhouse"
    phase = longhouse_home / "managed-local" / "cursor-helm" / "session-1.phase.json"
    phase.parent.mkdir(parents=True)
    claim = phase.parent / "binding-probes" / "session-1.json"
    claim.parent.mkdir(parents=True)
    claim.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "provider": "cursor",
                "status": "observed",
                "session_id": "session-1",
                "conversation_uuid": "cursor-thread-1",
                "launch_id": "launch-1",
                "run_id": "run-1",
            }
        ),
        encoding="utf-8",
    )
    phase.write_text(
        json.dumps(
            {
                "session_id": "session-1",
                "conversation_id": "cursor-thread-1",
                "launch_id": "launch-1",
                "phase": "idle",
            }
        ),
        encoding="utf-8",
    )

    assert _wait_cursor_idle(state, {"LONGHOUSE_HOME": str(longhouse_home)})["phase"] == "idle"


def test_cursor_native_idle_can_require_the_completed_generation(tmp_path: Path) -> None:
    state = {"session_id": "session-1", "provider_session_id": "cursor-thread-1", "run_id": "run-1"}
    longhouse_home = tmp_path / "longhouse"
    phase = longhouse_home / "managed-local" / "cursor-helm" / "session-1.phase.json"
    phase.parent.mkdir(parents=True)
    claim = phase.parent / "binding-probes" / "session-1.json"
    claim.parent.mkdir(parents=True)
    claim.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "provider": "cursor",
                "status": "observed",
                "session_id": "session-1",
                "conversation_uuid": "cursor-thread-1",
                "launch_id": "launch-1",
                "run_id": "run-1",
            }
        ),
        encoding="utf-8",
    )
    phase.write_text(
        json.dumps(
            {
                "session_id": "session-1",
                "conversation_id": "cursor-thread-1",
                "launch_id": "launch-1",
                "phase": "idle",
                "generation_id": "generation-old",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="identity-matched idle phase"):
        _wait_cursor_idle(
            state,
            {"LONGHOUSE_HOME": str(longhouse_home)},
            expected_generation_id="generation-new",
            timeout=0.01,
        )

    phase.write_text(
        json.dumps(
            {
                "session_id": "session-1",
                "conversation_id": "cursor-thread-1",
                "launch_id": "launch-1",
                "phase": "idle",
                "generation_id": "generation-new",
            }
        ),
        encoding="utf-8",
    )
    assert (
        _wait_cursor_idle(
            state,
            {"LONGHOUSE_HOME": str(longhouse_home)},
            expected_generation_id="generation-new",
        )["generation_id"]
        == "generation-new"
    )


def test_cursor_idle_then_flush_records_idle_before_ship() -> None:
    order: list[str] = []

    class FakeShipper:
        def flush(self, label: str) -> dict[str, object]:
            order.append(f"flush:{label}")
            return {"status": "pass", "events_shipped": 1}

    def fake_idle(*_args, **_kwargs) -> dict[str, str]:
        order.append("idle")
        return {"phase": "idle", "generation_id": "generation-1"}

    original = provider_native_resume._wait_cursor_idle
    provider_native_resume._wait_cursor_idle = fake_idle
    try:
        receipt = _cursor_idle_then_flush(
            {"session_id": "session-1"},
            {},
            FakeShipper(),  # type: ignore[arg-type]
            label="post-resume",
            expected_generation_id="generation-1",
        )
    finally:
        provider_native_resume._wait_cursor_idle = original

    assert receipt["status"] == "pass"
    assert order == ["idle", "flush:post-resume"]


def test_cursor_projection_diagnostics_capture_marker_and_binding_state(tmp_path: Path) -> None:
    home = tmp_path / "home"
    transcript = home / ".cursor" / "projects" / "workspace" / "agent-transcripts" / "conversation-1" / "conversation-1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_bytes(b'{"text":"LH_CURSOR_POST_test"}\n')
    longhouse_home = home / ".longhouse"
    binding_dir = longhouse_home / "managed-local" / "cursor-helm" / "binding-probes"
    binding_dir.mkdir(parents=True)
    (binding_dir / "session-1.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "provider": "cursor",
                "status": "observed",
                "session_id": "session-1",
                "conversation_uuid": "conversation-1",
                "run_id": "run-1",
            }
        ),
        encoding="utf-8",
    )

    payload = provider_native_resume._cursor_projection_diagnostics(
        environment={
            "HOME": str(home),
            "CURSOR_HOME": str(home / ".cursor"),
            "LONGHOUSE_HOME": str(longhouse_home),
        },
        state={"session_id": "session-1", "provider_session_id": "conversation-1"},
        marker="LH_CURSOR_POST_test",
        engine_db_path=tmp_path / "missing.db",
        phase="post-resume-before-flush",
    )

    assert payload["schema"] == "cursor_projection_diagnostics.v1"
    assert payload["managed_state"][0]["fields"]["conversation_uuid"] == "conversation-1"
    assert payload["engine_db"]["present"] is False
    assert len(payload["files"]) == 1
    assert payload["files"][0]["kind"] == "agent_transcript"
    assert payload["files"][0]["marker_observed"] is True
    assert payload["files"][0]["marker_count"] == 1


def test_cursor_projection_diagnostics_retains_active_wal_marker(tmp_path: Path) -> None:
    home = tmp_path / "home"
    store = home / ".cursor" / "projects" / "conversation-1" / "store.db"
    store.parent.mkdir(parents=True)

    connection = sqlite3.connect(store)
    try:
        connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, value BLOB)")
        connection.execute("INSERT INTO meta(key, value) VALUES ('0', 'conversation-1')")
        connection.commit()
    finally:
        connection.close()
    wal = store.with_name("store.db-wal")
    wal.write_bytes(b"uncheckpointed LH_CURSOR_SEED_wal-marker")

    payload = _cursor_projection_diagnostics(
        environment={
            "HOME": str(home),
            "CURSOR_HOME": str(home / ".cursor"),
            "LONGHOUSE_HOME": str(home / ".longhouse"),
        },
        state={"session_id": "session-1", "provider_session_id": "conversation-1"},
        marker="LH_CURSOR_SEED_wal-marker",
        engine_db_path=tmp_path / "missing.db",
        phase="initial-seed-after-flush",
    )

    wal_observation = next(item for item in payload["files"] if item["kind"] == "store_db_wal")
    assert wal_observation["marker_observed"] is True


def test_cursor_projection_diagnostics_are_non_authoritative_on_capture_or_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    shipper = object.__new__(TranscriptShipper)
    shipper.evidence_root = evidence
    shipper.engine_environment = {}
    shipper.db_path = tmp_path / "engine.db"
    monkeypatch.setattr(
        provider_native_resume,
        "_cursor_projection_diagnostics",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("read race")),
    )

    path = shipper.capture_cursor_projection_diagnostics(
        {"session_id": "session-1", "provider_session_id": "conversation-1"},
        marker="LH_CURSOR_SEED_test",
        label="initial-seed-before-send",
    )

    payload = json.loads(Path(path).read_text())
    assert payload["schema"] == "cursor_projection_diagnostics_unavailable.v1"
    assert payload["status"] == "unavailable"

    monkeypatch.setattr(
        provider_native_resume,
        "_write_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    assert shipper.capture_cursor_projection_diagnostics(
        {"session_id": "session-1", "provider_session_id": "conversation-1"},
        marker="LH_CURSOR_SEED_test",
        label="initial-seed-after-flush",
    ).endswith("initial-seed-after-flush.json")
    assert _write_best_effort_json(evidence / "correlation.json", {"ok": True}) is False


@pytest.mark.parametrize(
    ("provider", "correlation", "expected"),
    [
        (
            "cursor",
            {
                "marker_observed_in_transcript": True,
                "marker_observed_in_assistant": False,
                "new_assistant_events": 1,
            },
            False,
        ),
        (
            "cursor",
            {
                "marker_observed_in_transcript": True,
                "marker_observed_in_assistant": True,
                "new_assistant_events": 1,
            },
            True,
        ),
        (
            "cursor",
            {
                "marker_observed_in_transcript": False,
                "marker_observed_in_assistant": True,
                "new_assistant_events": 1,
            },
            False,
        ),
        (
            "cursor",
            {
                "marker_observed_in_transcript": True,
                "marker_observed_in_assistant": True,
                "new_assistant_events": 0,
            },
            False,
        ),
        (
            "claude",
            {
                "marker_observed_in_transcript": True,
                "marker_observed_in_assistant": False,
                "new_assistant_events": 1,
            },
            True,
        ),
    ],
)
def test_post_resume_correlation_requires_strict_assistant_proof(provider: str, correlation: dict[str, object], expected: bool) -> None:
    assert _post_resume_response_correlated(provider, correlation) is expected


def test_cursor_bootstrap_hook_sequence_requires_a_foreground_turn(tmp_path: Path) -> None:
    state = {"session_id": "session-1", "provider_session_id": "cursor-thread-1"}
    marker = "LH_CURSOR_BOOTSTRAP_abc123"
    longhouse_home = tmp_path / "longhouse"
    events = longhouse_home / "managed-local" / "cursor-helm" / "hook-events" / "session-1.ndjson"
    events.parent.mkdir(parents=True)
    events.write_text(json.dumps({"event": "sessionStart", "session_id": "session-1", "conversation_id": "cursor-thread-1"}) + "\n")
    baseline = events.stat().st_size
    events.write_text(
        events.read_text()
        + "\n".join(
            [
                json.dumps(
                    {
                        "event": "beforeSubmitPrompt",
                        "session_id": "session-1",
                        "conversation_id": "cursor-thread-1",
                        "payload": {
                            "session_id": "cursor-thread-1",
                            "conversation_id": "cursor-thread-1",
                            "generation_id": "generation-1",
                            "prompt": _cursor_bootstrap_prompt(marker),
                        },
                    }
                ),
                json.dumps(
                    {
                        "event": "afterAgentResponse",
                        "session_id": "session-1",
                        "conversation_id": "cursor-thread-1",
                        "payload": {
                            "session_id": "cursor-thread-1",
                            "conversation_id": "cursor-thread-1",
                            "generation_id": "generation-1",
                            "text": marker,
                        },
                    }
                ),
            ]
        )
        + "\n",
    )

    sequence = _wait_cursor_bootstrap_hook_sequence(
        state,
        {"LONGHOUSE_HOME": str(longhouse_home)},
        marker=marker,
        minimum_hook_event_bytes=baseline,
        timeout=1,
    )

    assert sequence["events"] == ["beforeSubmitPrompt", "afterAgentResponse"]
    assert sequence["generation_id"] == "generation-1"
    assert sequence["hook_response_correlated"] is True
    assert sequence["timed_out"] is False


def test_cursor_hook_timeout_diagnostics_do_not_repeat_polling_events(tmp_path: Path) -> None:
    state = {"session_id": "session-1", "provider_session_id": "cursor-thread-1"}
    longhouse_home = tmp_path / "longhouse"
    events = longhouse_home / "managed-local" / "cursor-helm" / "hook-events" / "session-1.ndjson"
    events.parent.mkdir(parents=True)
    row = {
        "event": "beforeSubmitPrompt",
        "session_id": "session-1",
        "conversation_id": "cursor-thread-1",
        "payload": {
            "session_id": "cursor-thread-1",
            "conversation_id": "cursor-thread-1",
            "generation_id": "generation-1",
            "prompt": "Reply with exactly LH_CURSOR_POST_abc123",
        },
    }
    events.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n")

    with pytest.raises(RuntimeError) as raised:
        _wait_cursor_hook_sequence(
            state,
            {"LONGHOUSE_HOME": str(longhouse_home)},
            marker="LH_CURSOR_POST_abc123",
            expected_prompt="Reply with exactly LH_CURSOR_POST_abc123",
            minimum_hook_event_bytes=0,
            label="post-resume",
            timeout=0.01,
        )

    assert "events=['beforeSubmitPrompt']" in str(raised.value)


def test_cursor_bootstrap_hook_sequence_does_not_accept_session_start(tmp_path: Path) -> None:
    state = {"session_id": "session-1", "provider_session_id": "cursor-thread-1"}
    marker = "LH_CURSOR_BOOTSTRAP_abc123"
    longhouse_home = tmp_path / "longhouse"
    events = longhouse_home / "managed-local" / "cursor-helm" / "hook-events" / "session-1.ndjson"
    events.parent.mkdir(parents=True)
    events.write_text(json.dumps({"event": "sessionStart", "session_id": "session-1", "conversation_id": "cursor-thread-1"}) + "\n")

    with pytest.raises(RuntimeError, match="beforeSubmitPrompt/afterAgentResponse"):
        _wait_cursor_bootstrap_hook_sequence(
            state,
            {"LONGHOUSE_HOME": str(longhouse_home)},
            marker=marker,
            minimum_hook_event_bytes=0,
            timeout=0.01,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session_id", "other-session"),
        ("conversation_id", "other-thread"),
        ("generation_id", ""),
        ("prompt", "Reply with exactly the wrong marker"),
    ],
)
def test_cursor_bootstrap_hook_sequence_rejects_unbound_response(tmp_path: Path, field: str, value: str) -> None:
    state = {"session_id": "session-1", "provider_session_id": "cursor-thread-1"}
    marker = "LH_CURSOR_BOOTSTRAP_abc123"
    longhouse_home = tmp_path / "longhouse"
    events = longhouse_home / "managed-local" / "cursor-helm" / "hook-events" / "session-1.ndjson"
    events.parent.mkdir(parents=True)
    before_payload = {
        "session_id": "cursor-thread-1",
        "conversation_id": "cursor-thread-1",
        "generation_id": "generation-1",
        "prompt": _cursor_bootstrap_prompt(marker),
    }
    after_payload = {
        "session_id": "cursor-thread-1",
        "conversation_id": "cursor-thread-1",
        "generation_id": "generation-1",
        "text": marker,
    }
    target = before_payload if field == "prompt" else after_payload if field == "generation_id" else None
    if target is not None:
        target[field] = value
    else:
        (before_payload if field == "session_id" else before_payload)[field] = value
    events.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "beforeSubmitPrompt",
                        "session_id": "session-1",
                        "conversation_id": "cursor-thread-1",
                        "payload": before_payload,
                    }
                ),
                json.dumps(
                    {
                        "event": "afterAgentResponse",
                        "session_id": "session-1",
                        "conversation_id": "cursor-thread-1",
                        "payload": after_payload,
                    }
                ),
            ]
        )
        + "\n"
    )

    with pytest.raises(RuntimeError, match="beforeSubmitPrompt/afterAgentResponse"):
        _wait_cursor_bootstrap_hook_sequence(
            state,
            {"LONGHOUSE_HOME": str(longhouse_home)},
            marker=marker,
            minimum_hook_event_bytes=0,
            timeout=0.01,
        )


def test_cursor_bootstrap_hook_sequence_rejects_multiple_matching_generations(tmp_path: Path) -> None:
    state = {"session_id": "session-1", "provider_session_id": "cursor-thread-1"}
    marker = "LH_CURSOR_BOOTSTRAP_abc123"
    longhouse_home = tmp_path / "longhouse"
    events = longhouse_home / "managed-local" / "cursor-helm" / "hook-events" / "session-1.ndjson"
    events.parent.mkdir(parents=True)
    rows = []
    for generation in ("generation-1", "generation-2"):
        rows.append(
            {
                "event": "beforeSubmitPrompt",
                "session_id": "session-1",
                "conversation_id": "cursor-thread-1",
                "payload": {
                    "session_id": "cursor-thread-1",
                    "conversation_id": "cursor-thread-1",
                    "generation_id": generation,
                    "prompt": _cursor_bootstrap_prompt(marker),
                },
            }
        )
    events.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    with pytest.raises(RuntimeError, match="multiple generations"):
        _wait_cursor_bootstrap_hook_sequence(
            state,
            {"LONGHOUSE_HOME": str(longhouse_home)},
            marker=marker,
            minimum_hook_event_bytes=0,
            timeout=1,
        )


def test_cursor_bootstrap_correlation_allows_exact_hook_with_shipped_events() -> None:
    result = _cursor_bootstrap_correlation(
        {
            "marker_observed_in_transcript": False,
            "marker_observed_in_assistant": False,
            "new_assistant_events": 0,
        },
        {"hook_response_correlated": True},
        {"status": "pass", "events_shipped": 11},
    )

    assert result == {
        "transcript_projection_correlated": False,
        "hook_response_correlated": True,
        "method": "cursor_hook_with_transcript_ship",
        "bootstrap_correlated": True,
    }


@pytest.mark.parametrize(
    "ship_receipt",
    [
        {"status": "failed", "events_shipped": 11},
        {"status": "pass", "events_shipped": 0},
        {"status": "pass", "events_shipped": True},
    ],
)
def test_cursor_bootstrap_correlation_rejects_unshipped_hook_fallback(ship_receipt: dict[str, object]) -> None:
    result = _cursor_bootstrap_correlation(
        {
            "marker_observed_in_transcript": False,
            "marker_observed_in_assistant": False,
            "new_assistant_events": 0,
        },
        {"hook_response_correlated": True},
        ship_receipt,
    )

    assert result["bootstrap_correlated"] is False
    assert result["method"] == "unverified"


def test_codex_resume_intent_uses_the_actual_isolated_workspace(tmp_path: Path) -> None:
    args = _args(tmp_path)
    workspace = tmp_path / "isolated-workspace"
    workspace.mkdir()
    session_id = "11111111-1111-4111-8111-111111111111"
    intent = {
        "available": True,
        "session_id": session_id,
        "provider": "codex",
        "cwd": str(workspace),
        "handoff": "terminal_command",
        "argv": ["longhouse", "codex", "--cwd", str(workspace), "--resume-session", session_id],
    }

    receipt = _validate_resume_intent(args, session_id, intent, cwd=workspace)

    assert receipt["identity_valid"] is True


def test_claude_resume_probe_follows_native_channel_state_root(tmp_path: Path) -> None:
    state = tmp_path / ".claude" / "channels" / "longhouse" / "sessions" / "session.json"
    state.parent.mkdir(parents=True)
    state.write_text("{}")

    assert state in _state_candidates(SPECS["claude"], tmp_path)


def test_claude_profile_bootstrap_accepts_observed_main_tui_as_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePtyProcess:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.process = SimpleNamespace(poll=lambda: None)
            self.sent: list[str] = []
            self.recording = Path(str(_kwargs["recording"]))
            self.claude_permission_acceptance_sent = False

        def drain(self) -> None:
            pass

        def send(self, value: str) -> None:
            self.sent.append(value)

        def wait(self, _timeout: float) -> int:
            return 0

        def kill_group(self, _signal: int) -> None:
            pass

        def close(self) -> None:
            pass

    moments = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr(provider_native_resume, "PtyProcess", FakePtyProcess)
    monkeypatch.setattr(provider_native_resume.time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(provider_native_resume.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        provider_native_resume,
        "_terminal_text",
        lambda _recording: "Claude Code v2.1.221  Longhouse qualification bootstrap  Welcome back!",
    )

    result = provider_native_resume._prepare_claude_profile(
        binary=tmp_path / "claude",
        home=tmp_path / "home",
        workspace=tmp_path,
        environment={},
        recording=tmp_path / "recording.tty",
        timeout=1.0,
    )

    assert result["status"] == "pass"
    assert result["completion_signal"] == "main_tui"


def test_codex_resume_receipts_normalize_path_values(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"
    write_codex_json(receipt, {"path": tmp_path / "provider"})

    assert json.loads(receipt.read_text()) == {"path": str(tmp_path / "provider")}


def test_native_resume_rejects_normal_provider_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/root")
    monkeypatch.setenv("LONGHOUSE_QUALIFICATION_SANDBOX", "provider-qualification-bwrap-v3")
    monkeypatch.setenv("LONGHOUSE_QUALIFICATION_HOME", "/root")

    with pytest.raises(RuntimeError, match="isolated provider HOME"):
        _isolated_provider_home()


def test_native_resume_requires_the_factory_sandbox_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "provider-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LONGHOUSE_QUALIFICATION_SANDBOX", raising=False)
    monkeypatch.delenv("LONGHOUSE_QUALIFICATION_HOME", raising=False)

    with pytest.raises(RuntimeError, match="qualification sandbox"):
        _isolated_provider_home()


def test_native_resume_accepts_disposable_provider_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "provider-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LONGHOUSE_QUALIFICATION_SANDBOX", "provider-qualification-bwrap-v3")
    monkeypatch.setenv("LONGHOUSE_QUALIFICATION_HOME", str(home))

    assert _isolated_provider_home() == home


def test_codex_native_resume_tui_uses_the_bridge_provider_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", "/root")
    isolation_root = tmp_path / "isolation"

    environment = codex_native_resume._native_resume_tui_environment(isolation_root, "session-1")

    assert environment["HOME"] == str(isolation_root / "provider-home")
    assert environment["CODEX_HOME"] == str(isolation_root / "provider-home" / ".codex")
    assert environment["LONGHOUSE_MANAGED_SESSION_ID"] == "session-1"


def test_codex_initial_bridge_reuses_the_shipper_isolation_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _args(tmp_path)
    evidence_root = tmp_path / "evidence"
    isolation_root = tmp_path / "isolation"
    seen: dict[str, object] = {}

    def fake_start(*_args: object, **kwargs: object) -> tuple[dict[str, object], subprocess.CompletedProcess[str], Path]:
        seen.update(kwargs)
        root = kwargs["isolation_root"]
        assert isinstance(root, Path)
        return {}, subprocess.CompletedProcess([], 0), root

    monkeypatch.setattr(codex_native_resume.bridge_canary, "_start_bridge", fake_start)

    codex_native_resume._start_initial_bridge(
        args,
        evidence_root=evidence_root,
        codex_bin=str(args.provider_bin),
        isolation_root=isolation_root,
    )

    assert seen["isolation_root"] == isolation_root


def test_codex_post_stop_ship_receipt_is_retained_separately_from_transition(tmp_path: Path) -> None:
    class FakeShipper:
        def flush(self, label: str) -> dict[str, object]:
            assert label == "post-stop"
            return {"status": "pass", "files_shipped": 1}

    transition: dict[str, object] = {}
    receipt = codex_native_resume._record_post_stop_ship_receipt(
        tmp_path,
        transition,
        FakeShipper(),  # type: ignore[arg-type]
    )

    assert receipt == {"status": "pass", "files_shipped": 1}
    assert transition["post_stop_transcript_ship"] == receipt
    assert json.loads((tmp_path / "post-stop-transcript-ship-receipt.json").read_text()) == receipt


def test_codex_resume_contract_snapshot_matches_machine_scanner_layout(tmp_path: Path) -> None:
    isolation_root = tmp_path / "isolation"
    workspace = isolation_root / "workspace"
    provider = isolation_root / "provider"
    workspace.mkdir(parents=True)
    provider.write_text("provider")

    state = {
        "session_id": "11111111-1111-4111-8111-111111111111",
        "cwd": str(workspace),
        "thread_id": "thread-1",
        "thread_path": str(isolation_root / "thread.jsonl"),
    }
    (isolation_root / "thread.jsonl").write_text("{}\n")

    state_path, contract_path = _write_resume_contract_snapshot(
        isolation_root=isolation_root,
        state=state,
        codex_bin=provider,
        provider_version="0.147.0-test",
    )

    assert state_path.parent.name == "sessions"
    assert state_path.is_file()
    assert json.loads(contract_path.read_text())["control"]["state_path"] == str(state_path.resolve())


def test_codex_process_evidence_redacts_bridge_tokens() -> None:
    command = 'env LONGHOUSE_COORDINATION_TOKEN="zst_secret" codex app-server'

    assert _redact_process_command(command) == "env LONGHOUSE_COORDINATION_TOKEN=<redacted> codex app-server"


def test_codex_main_serializes_path_values_in_result_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    engine = tmp_path / "longhouse-engine"
    provider = tmp_path / "codex"
    for executable in (engine, provider):
        executable.write_text("#!/bin/sh\n")
        executable.chmod(0o755)
    result_path = tmp_path / "evidence"

    monkeypatch.setenv("LONGHOUSE_RUNTIME_API_URL", "https://runtime.example")
    monkeypatch.setenv("LONGHOUSE_RUNTIME_AGENTS_TOKEN", "device-token")
    monkeypatch.setattr(
        codex_native_resume,
        "run_native_resume",
        lambda args: {"status": "pass", "path": result_path},
    )

    assert (
        codex_native_resume.main(
            [
                "--variant",
                "clean_exit",
                "--evidence-root",
                str(tmp_path),
                "--repo-root",
                str(tmp_path),
                "--engine",
                str(engine),
                "--codex-bin",
                str(provider),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["path"] == str(result_path)


def test_shipped_facade_receives_provider_native_resume_selector(tmp_path: Path) -> None:
    args = _args(tmp_path)
    session_id = "11111111-1111-4111-8111-111111111111"

    for provider, selector in (
        ("claude", "--resume"),
        ("cursor", "--resume-session"),
        ("opencode", "--resume-session"),
    ):
        command = _launch_command(SPECS[provider], args, session_id)
        assert command[:2] == [str(args.longhouse_cli), provider]
        assert command[command.index(selector) + 1] == session_id
        assert command[command.index(SPECS[provider].binary_flag) + 1] == str(args.provider_bin)

        secure_command = _launch_command(SPECS[provider], args, session_id, use_credential_files=True)
        assert args.agents_token not in secure_command
        assert secure_command[secure_command.index(selector) + 1] == session_id


def test_cursor_resume_commands_pin_the_factory_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _args(tmp_path)
    session_id = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setenv("CURSOR_MODEL", "cursor-grok-4.5-high")

    command = _launch_command(SPECS["cursor"], args, session_id)
    assert command[-3:] == ["--", "--model", "cursor-grok-4.5-high"]

    intent = {
        "session_id": session_id,
        "provider": "cursor",
        "machine_id": "factory-worker",
        "cwd": str(args.repo_root),
        "available": True,
        "argv": ["longhouse", "cursor", "--cwd", str(args.repo_root), "--resume-session", session_id],
        "handoff": "terminal_command",
    }
    resumed, receipt = _command_from_resume_intent(SPECS["cursor"], args, session_id, intent)
    assert resumed[-3:] == ["--", "--model", "cursor-grok-4.5-high"]
    assert "cursor_model" in receipt["factory_overrides"]

    bootstrap = "Reply with exactly READY and nothing else. Do not use tools or inspect files."
    resumed_with_bootstrap, bootstrap_receipt = _command_from_resume_intent(
        SPECS["cursor"],
        args,
        session_id,
        intent,
        prompt=bootstrap,
    )
    assert resumed_with_bootstrap[-1] == bootstrap
    assert bootstrap_receipt["executed_argv"][-1] == bootstrap


@pytest.mark.parametrize(
    ("provider", "selector"),
    (("claude", "--resume"), ("cursor", "--resume-session"), ("opencode", "--resume-session")),
)
def test_resume_command_is_derived_from_exact_provider_neutral_intent(
    tmp_path: Path,
    provider: str,
    selector: str,
) -> None:
    args = _args(tmp_path)
    session_id = "11111111-1111-4111-8111-111111111111"
    intent = {
        "session_id": session_id,
        "provider": provider,
        "machine_id": "factory-worker",
        "cwd": str(args.repo_root),
        "available": True,
        "argv": ["longhouse", provider, "--cwd", str(args.repo_root), selector, session_id],
        "command": f"longhouse {provider}",
        "handoff": "terminal_command",
    }

    command, receipt = _command_from_resume_intent(SPECS[provider], args, session_id, intent)

    assert receipt["identity_valid"] is True
    assert command[:2] == [str(args.longhouse_cli), provider]
    assert command[command.index(selector) + 1] == session_id
    assert command[command.index(SPECS[provider].binary_flag) + 1] == str(args.provider_bin)
    assert args.agents_token in command
    assert args.agents_token not in json.dumps(receipt)
    assert "<redacted>" in receipt["executed_argv"]


def test_resume_command_rejects_nearby_session_intent(tmp_path: Path) -> None:
    args = _args(tmp_path)
    session_id = "11111111-1111-4111-8111-111111111111"
    intent = {
        "session_id": "22222222-2222-4222-8222-222222222222",
        "provider": "claude",
        "cwd": str(args.repo_root),
        "available": True,
        "argv": ["longhouse", "claude", "--cwd", str(args.repo_root), "--resume", session_id],
        "handoff": "terminal_command",
    }

    with pytest.raises(RuntimeError, match="exact session"):
        _command_from_resume_intent(SPECS["claude"], args, session_id, intent)


def test_provider_process_identity_comes_from_exact_provider_state() -> None:
    states = {
        "claude": {"claude_pid": 101},
        "cursor": {"cursor_pid": 202},
        "opencode": {"pid": 303},
    }

    for provider, state in states.items():
        assert _provider_process_pid(SPECS[provider], state) == next(iter(state.values()))


@pytest.mark.parametrize("invalid", [None, 0, -1, True, "101"])
def test_provider_process_identity_rejects_missing_or_invalid_pid(invalid: object) -> None:
    with pytest.raises(RuntimeError, match="positive claude_pid"):
        _provider_process_pid(SPECS["claude"], {"claude_pid": invalid})


def test_wait_state_ignores_claude_contract_without_provider_pid(tmp_path: Path) -> None:
    home = tmp_path / "home"
    native = home / ".claude/channels/longhouse/sessions/session.json"
    native.parent.mkdir(parents=True)
    native.write_text(
        json.dumps(
            {
                "session_id": "11111111-1111-4111-8111-111111111111",
                "provider_session_id": "provider-1",
                "provider": "claude",
                "run_id": "run-1",
                "connection_id": "connection-1",
                "claude_pid": 123,
            }
        )
    )
    contract = home / ".longhouse/managed-local/contracts/claude/session.json"
    contract.parent.mkdir(parents=True)
    contract.write_text(
        json.dumps(
            {
                "session_id": "11111111-1111-4111-8111-111111111111",
                "provider_session_id": "provider-1",
                "provider": "claude",
                "run_id": "run-1",
                "connection_id": "connection-1",
            }
        )
    )

    state = _wait_state(SPECS["claude"], home, timeout=0.1)

    assert state["state_path"] == str(native)
    assert state["claude_pid"] == 123


def test_claude_permission_prompt_is_acknowledged_once(tmp_path: Path) -> None:
    recording = tmp_path / "claude.tty"
    recording.write_text("1. No, exit\n2. Yes, I accept\n", encoding="utf-8")

    class FakeProcess:
        claude_permission_acceptance_sent = False

        def __init__(self) -> None:
            self.recording = recording
            self.sent: list[str] = []

        def send(self, value: str) -> None:
            self.sent.append(value)

    process = FakeProcess()

    _accept_claude_permission_prompt(process)  # type: ignore[arg-type]
    _accept_claude_permission_prompt(process)  # type: ignore[arg-type]

    assert process.sent == ["2\r"]
    assert process.claude_permission_acceptance_sent is True


def test_claude_development_channel_prompt_selects_local_development_once(tmp_path: Path) -> None:
    recording = tmp_path / "claude.tty"
    recording.write_text(
        # Cursor-addressed Claude redraws can drop characters from the loading
        # label once ANSI controls are stripped. The option text is stable.
        "Loding developmetchannel\nI am using this for local development\nExit\n",
        encoding="utf-8",
    )

    class FakeProcess:
        claude_development_channel_acceptance_sent = False
        claude_development_channel_prompt_seen_at = 0.0

        def __init__(self) -> None:
            self.recording = recording
            self.sent: list[str] = []

        def send(self, value: str) -> None:
            self.sent.append(value)

    process = FakeProcess()

    _accept_claude_development_channel_prompt(process)  # type: ignore[arg-type]
    _accept_claude_development_channel_prompt(process)  # type: ignore[arg-type]

    assert process.sent == ["\r"]
    assert process.claude_development_channel_acceptance_sent is True


def test_provider_state_evidence_redacts_nested_secrets() -> None:
    from zerg.qa.provider_native_resume import _redact_state_for_evidence

    state = _redact_state_for_evidence(
        {
            "session_id": "session-1",
            "auth_token": "bridge-secret",
            "nested": {"hook_token": "hook-secret", "provider_pid": 123},
        }
    )

    assert state == {
        "session_id": "session-1",
        "auth_token": "<redacted>",
        "nested": {"hook_token": "<redacted>", "provider_pid": 123},
    }


def test_cursor_initial_seed_bootstraps_through_the_provider_pty(tmp_path: Path) -> None:
    args = _args(tmp_path)

    class FakeProviderProcess:
        class Process:
            @staticmethod
            def poll() -> None:
                return None

        process = Process()
        sent: list[str] = []

        def send(self, value: str) -> None:
            self.sent.append(value)

    process = FakeProviderProcess()
    result = _control_send(
        SPECS["cursor"],
        args,
        {"session_id": "session-1"},
        process,  # type: ignore[arg-type]
        "seed",
        initial=True,
    )

    assert result["method"] == "provider_tty_bootstrap"
    assert result["returncode"] == 0
    assert process.sent == ["seed", "\x1b", "\r"]


def test_cursor_control_send_retries_only_provider_idle_race(tmp_path: Path, monkeypatch) -> None:
    args = _args(tmp_path)

    class FakeProviderProcess:
        class Process:
            @staticmethod
            def poll() -> None:
                return None

        process = Process()
        sent: list[str] = []

        def send(self, value: str) -> None:
            self.sent.append(value)

    process = FakeProviderProcess()
    commands: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        commands.append(argv)
        if len(commands) == 1:
            return subprocess.CompletedProcess(argv, 1, "", "provider_not_idle")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(provider_native_resume.subprocess, "run", fake_run)
    monkeypatch.setattr(provider_native_resume.time, "sleep", lambda _seconds: None)
    result = _control_send(
        SPECS["cursor"],
        args,
        {"session_id": "session-1"},
        process,  # type: ignore[arg-type]
        "seed",
        initial=False,
    )

    assert result["attempts"] == 2
    assert len(commands) == 2
    assert process.sent == []


def test_cursor_clean_stop_waits_for_provider_idle_before_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _args(tmp_path)
    args.live_send_timeout_secs = 17
    calls: list[tuple[str, object]] = []
    wait_timeouts: list[float] = []

    class FakeProviderProcess:
        pid = 101

        class Process:
            @staticmethod
            def poll() -> int:
                return 0

        process = Process()

        @staticmethod
        def wait(_timeout: float) -> int:
            wait_timeouts.append(_timeout)
            return 0

        @staticmethod
        def kill_group(_signal: int) -> None:
            raise AssertionError("idle clean stop must not need a fallback signal")

    monkeypatch.setattr(
        provider_native_resume,
        "_wait_cursor_idle",
        lambda state, environment, **kwargs: calls.append(("idle", kwargs)) or {"phase": "idle"},
    )
    monkeypatch.setattr(
        provider_native_resume,
        "_control_send",
        lambda spec, args, state, process, text: calls.append(("send", text))
        or {"returncode": 0},
    )
    monkeypatch.setattr(provider_native_resume, "_wait_process_group_dead", lambda _pid: True)
    monkeypatch.setattr(provider_native_resume, "_wait_pid_dead", lambda _pid: True)
    monkeypatch.setattr(provider_native_resume, "_signal_pid_if_alive", lambda *_args: False)

    receipt = provider_native_resume._stop(
        SPECS["cursor"],
        args,
        {"session_id": "session-1", "cursor_pid": 202},
        FakeProviderProcess(),  # type: ignore[arg-type]
        force=False,
        environment={"LONGHOUSE_HOME": str(tmp_path / "home")},
    )

    assert receipt["clean"] is True
    assert calls == [("idle", {"timeout": 15.0}), ("send", "/exit")]
    assert wait_timeouts == [30]


def test_cursor_clean_stop_recovers_stranded_generation_before_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _args(tmp_path)
    args.live_send_timeout_secs = 17
    calls: list[tuple[str, object]] = []

    class FakeProviderProcess:
        pid = 101

        class Process:
            @staticmethod
            def poll() -> int:
                return 0

        process = Process()

        @staticmethod
        def wait(_timeout: float) -> int:
            return 0

        @staticmethod
        def kill_group(_signal: int) -> None:
            raise AssertionError("recovery clean stop must not need a fallback signal")

    def fake_idle(*_args: object, **kwargs: object) -> dict[str, str]:
        calls.append(("idle", kwargs))
        raise RuntimeError("provider remained active")

    monkeypatch.setattr(provider_native_resume, "_wait_cursor_idle", fake_idle)
    monkeypatch.setattr(
        provider_native_resume,
        "_cursor_interrupt_to_idle",
        lambda *_args, **_kwargs: calls.append(("interrupt", {})) or {"method": "cursor_ctrl_c_recovery"},
    )
    monkeypatch.setattr(
        provider_native_resume,
        "_control_send",
        lambda spec, args, state, process, text: calls.append(("send", text))
        or {"returncode": 0},
    )
    monkeypatch.setattr(provider_native_resume, "_wait_process_group_dead", lambda _pid: True)
    monkeypatch.setattr(provider_native_resume, "_wait_pid_dead", lambda _pid: True)
    monkeypatch.setattr(provider_native_resume, "_signal_pid_if_alive", lambda *_args: False)

    receipt = provider_native_resume._stop(
        SPECS["cursor"],
        args,
        {"session_id": "session-1", "cursor_pid": 202},
        FakeProviderProcess(),  # type: ignore[arg-type]
        force=False,
        environment={"LONGHOUSE_HOME": str(tmp_path / "home")},
    )

    assert receipt["clean"] is True
    assert receipt["cursor_recovery"]["method"] == "cursor_ctrl_c_recovery"
    assert calls == [("idle", {"timeout": 15.0}), ("interrupt", {}), ("send", "/exit")]


def test_cursor_forced_stop_keeps_short_shutdown_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _args(tmp_path)
    wait_timeouts: list[float] = []
    kill_signals: list[int] = []

    class FakeProviderProcess:
        pid = 101

        class Process:
            @staticmethod
            def poll() -> int:
                return 0

        process = Process()

        @staticmethod
        def wait(timeout: float) -> int:
            wait_timeouts.append(timeout)
            return 0

        @staticmethod
        def kill_group(signal_number: int) -> None:
            kill_signals.append(signal_number)

    monkeypatch.setattr(provider_native_resume, "_wait_process_group_dead", lambda _pid: True)
    monkeypatch.setattr(provider_native_resume, "_wait_pid_dead", lambda _pid: True)
    monkeypatch.setattr(provider_native_resume, "_signal_pid_if_alive", lambda *_args: False)

    receipt = provider_native_resume._stop(
        SPECS["cursor"],
        args,
        {"session_id": "session-1", "cursor_pid": 202},
        FakeProviderProcess(),  # type: ignore[arg-type]
        force=True,
    )

    assert wait_timeouts == [10]
    assert kill_signals == [signal.SIGKILL]
    assert receipt["clean"] is False


def test_cursor_interrupt_recovery_rejects_stale_conversation_before_signal(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = home / "managed-local" / "cursor-helm"
    (root / "binding-probes").mkdir(parents=True)
    (root / "hook-events").mkdir(parents=True)
    state = {"session_id": "session-1", "provider_session_id": "conversation-1", "run_id": "run-1"}
    (root / "binding-probes" / "session-1.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "provider": "cursor",
                "status": "observed",
                "session_id": "session-1",
                "conversation_uuid": "conversation-1",
                "launch_id": "launch-1",
                "run_id": "run-1",
            }
        ),
        encoding="utf-8",
    )
    (root / "session-1.phase.json").write_text(
        json.dumps(
            {
                "session_id": "session-1",
                "conversation_id": "stale-conversation",
                "launch_id": "launch-1",
                "phase": "active",
                "generation_id": "generation-1",
            }
        ),
        encoding="utf-8",
    )

    class FakeProcess:
        recording = tmp_path / "terminal"
        sent: list[str] = []

        def send(self, value: str) -> None:
            self.sent.append(value)

    process = FakeProcess()
    with pytest.raises(RuntimeError, match="identity-matched generation"):
        _cursor_interrupt_to_idle(state, {"LONGHOUSE_HOME": str(home)}, process)  # type: ignore[arg-type]
    assert process.sent == []


@pytest.mark.parametrize("late_response", [False, True])
def test_cursor_interrupt_recovery_handles_error_stop_and_late_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    late_response: bool,
) -> None:
    home = tmp_path / "home"
    root = home / "managed-local" / "cursor-helm"
    (root / "binding-probes").mkdir(parents=True)
    events_path = root / "hook-events" / "session-1.ndjson"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    state = {"session_id": "session-1", "provider_session_id": "conversation-1", "run_id": "run-1"}
    (root / "binding-probes" / "session-1.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "provider": "cursor",
                "status": "observed",
                "session_id": "session-1",
                "conversation_uuid": "conversation-1",
                "launch_id": "launch-1",
                "run_id": "run-1",
            }
        ),
        encoding="utf-8",
    )
    (root / "session-1.phase.json").write_text(
        json.dumps(
            {
                "session_id": "session-1",
                "conversation_id": "conversation-1",
                "launch_id": "launch-1",
                "phase": "active",
                "generation_id": "generation-1",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        provider_native_resume,
        "_wait_cursor_idle",
        lambda *_args, **_kwargs: {"phase": "idle", "generation_id": "generation-1"},
    )
    monkeypatch.setattr(provider_native_resume, "_wait_cursor_tui_ready", lambda *_args, **_kwargs: None)

    class FakeProcess:
        recording = tmp_path / "terminal"
        sent: list[str] = []
        emitted = False

        def send(self, value: str) -> None:
            self.sent.append(value)

        def drain(self) -> bytes:
            if not self.emitted:
                events = [
                    {
                        "event": "stop",
                        "session_id": "session-1",
                        "conversation_id": "conversation-1",
                        "payload": {"generation_id": "generation-1", "status": "error"},
                    }
                ]
                if late_response:
                    events.append(
                        {
                            "event": "afterAgentResponse",
                            "session_id": "session-1",
                            "conversation_id": "conversation-1",
                            "payload": {"generation_id": "generation-1", "text": "late"},
                        }
                    )
                events_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
                self.emitted = True
            return b""

    process = FakeProcess()
    if late_response:
        with pytest.raises(RuntimeError, match="allowed the stranded generation"):
            _cursor_interrupt_to_idle(state, {"LONGHOUSE_HOME": str(home)}, process)  # type: ignore[arg-type]
    else:
        receipt = _cursor_interrupt_to_idle(state, {"LONGHOUSE_HOME": str(home)}, process)  # type: ignore[arg-type]
        assert receipt["method"] == "cursor_ctrl_c_recovery"
        assert receipt["observed_events"] == ["stop"]
    assert process.sent == ["\x03"]


def test_cursor_interrupt_recovery_records_late_normal_completion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    root = home / "managed-local" / "cursor-helm"
    (root / "binding-probes").mkdir(parents=True)
    events_path = root / "hook-events" / "session-1.ndjson"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    state = {"session_id": "session-1", "provider_session_id": "conversation-1", "run_id": "run-1"}
    (root / "binding-probes" / "session-1.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "provider": "cursor",
                "status": "observed",
                "session_id": "session-1",
                "conversation_uuid": "conversation-1",
                "launch_id": "launch-1",
                "run_id": "run-1",
            }
        ),
        encoding="utf-8",
    )
    (root / "session-1.phase.json").write_text(
        json.dumps(
            {
                "session_id": "session-1",
                "conversation_id": "conversation-1",
                "launch_id": "launch-1",
                "phase": "active",
                "generation_id": "generation-1",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        provider_native_resume,
        "_wait_cursor_idle",
        lambda *_args, **_kwargs: {"phase": "idle", "generation_id": "generation-1"},
    )
    monkeypatch.setattr(provider_native_resume, "_wait_cursor_tui_ready", lambda *_args, **_kwargs: None)

    class FakeProcess:
        recording = tmp_path / "terminal"
        sent: list[str] = []
        emitted = False

        def send(self, value: str) -> None:
            self.sent.append(value)

        def drain(self) -> bytes:
            if not self.emitted:
                events_path.write_text(
                    json.dumps(
                        {
                            "event": "stop",
                            "session_id": "session-1",
                            "conversation_id": "conversation-1",
                            "payload": {"generation_id": "generation-1", "status": "completed"},
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                self.emitted = True
            return b""

    process = FakeProcess()
    receipt = _cursor_interrupt_to_idle(state, {"LONGHOUSE_HOME": str(home)}, process)  # type: ignore[arg-type]
    assert receipt["method"] == "cursor_native_idle_late"
    assert receipt["stop_hook"]["payload"]["status"] == "completed"


def test_claude_initial_seed_uses_managed_channel_control(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _args(tmp_path)

    class FakeProviderProcess:
        class Process:
            @staticmethod
            def poll() -> None:
                return None

        process = Process()
        sent: list[str] = []

        def send(self, value: str) -> None:
            self.sent.append(value)

    process = FakeProviderProcess()
    commands: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, "sent", "")

    monkeypatch.setattr(provider_native_resume.subprocess, "run", fake_run)
    result = _control_send(
        SPECS["claude"],
        args,
        {"session_id": "session-1"},
        process,  # type: ignore[arg-type]
        "seed",
        initial=True,
    )

    assert result["method"] == "longhouse_control"
    assert result["returncode"] == 0
    assert commands == [[str(args.engine), "claude-channel", "send", "--session-id", "session-1", "--text", "seed"]]
    assert process.sent == []


def test_response_correlation_returns_measured_facts_for_strict_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "MARKER"
    tail = {
        "events": [
            {"role": "assistant", "content": "prior"},
            {"role": "user", "content": marker},
            {"role": "assistant", "content": f"Reply exactly {marker} and nothing else."},
        ]
    }
    monkeypatch.setattr(provider_native_resume, "_api_json", lambda *_args, **_kwargs: tail)

    observed_tail, correlation = _wait_assistant_response_after_marker(
        "https://runtime.example",
        "token",
        "session-1",
        marker,
        prior_assistant_event_digests=provider_native_resume._assistant_event_digests(
            {"events": [{"role": "assistant", "content": "prior"}]}
        ),
        require_assistant_marker=True,
        timeout=1,
    )

    assert observed_tail == tail
    assert correlation == {
        "method": "assistant_marker_then_new_assistant_event",
        "marker_observed_in_transcript": True,
        "marker_observed_in_assistant": True,
        "prior_assistant_events": 1,
        "observed_assistant_events": 2,
        "new_assistant_events": 1,
        "timed_out": False,
    }


def test_strict_response_correlation_rejects_unrelated_assistant_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tail = {
        "events": [
            {"role": "user", "content": "MARKER"},
            {"role": "assistant", "content": "unrelated response"},
        ]
    }
    monkeypatch.setattr(provider_native_resume, "_api_json", lambda *_args, **_kwargs: tail)
    ticks = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr(provider_native_resume.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(provider_native_resume.time, "sleep", lambda _seconds: None)

    _observed_tail, correlation = _wait_assistant_response_after_marker(
        "https://runtime.example",
        "token",
        "session-1",
        "MARKER",
        prior_assistant_event_digests=set(),
        require_assistant_marker=True,
        timeout=1,
    )

    assert correlation["method"] == "assistant_marker_then_new_assistant_event"
    assert correlation["marker_observed_in_transcript"] is True
    assert correlation["marker_observed_in_assistant"] is False
    assert correlation["new_assistant_events"] == 1
    assert correlation["timed_out"] is True


def test_claude_response_correlation_returns_false_facts_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        provider_native_resume,
        "_api_json",
        lambda *_args, **_kwargs: {"events": [{"role": "user", "content": "MARKER"}]},
    )
    ticks = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr(provider_native_resume.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(provider_native_resume.time, "sleep", lambda _seconds: None)

    _tail, correlation = _wait_assistant_response_after_marker(
        "https://runtime.example",
        "token",
        "session-1",
        "MARKER",
        prior_assistant_event_digests=set(),
        timeout=1,
    )

    assert correlation["marker_observed_in_transcript"] is True
    assert correlation["marker_observed_in_assistant"] is False
    assert correlation["new_assistant_events"] == 0
    assert correlation["timed_out"] is True


@pytest.mark.parametrize(("exit_code", "expected_clean"), [(0, True), (1, False)])
def test_claude_clean_stop_requires_zero_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
    expected_clean: bool,
) -> None:
    class FakeProviderProcess:
        pid = 101

        class Process:
            @staticmethod
            def poll() -> int:
                return exit_code

        process = Process()

        @staticmethod
        def send(_value: str) -> None:
            return None

        @staticmethod
        def wait(_timeout: float) -> int:
            return exit_code

        @staticmethod
        def kill_group(_signal: int) -> None:
            raise AssertionError("clean stop must not need a fallback signal")

    monkeypatch.setattr(provider_native_resume, "_wait_process_group_dead", lambda _pid: True)
    monkeypatch.setattr(provider_native_resume, "_wait_pid_dead", lambda _pid: True)
    monkeypatch.setattr(provider_native_resume, "_signal_pid_if_alive", lambda *_args: False)

    receipt = provider_native_resume._stop(
        SPECS["claude"],
        _args(tmp_path),
        {"session_id": "session-1", "claude_pid": 202},
        FakeProviderProcess(),  # type: ignore[arg-type]
        force=False,
    )

    assert receipt["exit_code"] == exit_code
    assert receipt["clean"] is expected_clean


def test_codex_concurrent_lock_error_is_a_refusal_even_if_state_identity_races() -> None:
    assert codex_native_resume._is_concurrent_lock_refusal(
        "codex bridge start failed: another codex bridge already owns lock /tmp/bridge.lock"
    )
    assert not codex_native_resume._is_concurrent_lock_refusal("codex bridge start failed: provider exited")


def test_cleanup_retains_failed_pid_identity_as_unverified_receipt() -> None:
    receipt = _cleanup_processes(SPECS["claude"], (), [{"session_id": "session-without-provider-pid"}])

    assert receipt["verified"] is False
    assert receipt["orphan_count"] == 1
    assert receipt["provider_pid_errors"][0]["session_id"] == "session-without-provider-pid"


def test_failure_manifest_is_refreshed_after_final_cleanup(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "cleanup-receipt.json").write_text('{"verified":true}\n')
    (evidence / "result.json").write_text(
        json.dumps(
            {
                "status": "fail",
                "artifact_manifest": [],
            }
        )
    )

    _refresh_failure_result_manifest(evidence)

    result = json.loads((evidence / "result.json").read_text())
    manifest = {entry["path"]: entry for entry in result["artifact_manifest"]}
    assert manifest["cleanup-receipt.json"]["size"] == (evidence / "cleanup-receipt.json").stat().st_size
    assert manifest["cleanup-receipt.json"]["sha256"].startswith("sha256:")


def test_finalized_secret_scan_downgrades_pass_and_refreshes_manifest(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    receipt = evidence / "cleanup-receipt.json"
    receipt.write_text('{"verified":true}\n')
    result = {
        "status": "pass",
        "artifact_manifest": [],
        "observation": {"artifact_secret_scan_passed": True},
        "assertions": {"native_provider_resume_proven": True},
    }
    (evidence / "result.json").write_text(json.dumps(result))

    finalized = _finalize_result_payload(
        evidence,
        result,
        redacted_files=["cleanup-receipt.json"],
        finalization_errors=[],
    )

    written = json.loads((evidence / "result.json").read_text())
    manifest = {entry["path"]: entry for entry in written["artifact_manifest"]}
    assert finalized == written
    assert written["status"] == "fail"
    assert written["failure_code"] == "finalized_artifact_secret_scan_failed"
    assert written["observation"]["artifact_secret_scan_passed"] is False
    assert written["assertions"]["native_provider_resume_proven"] is False
    assert manifest["cleanup-receipt.json"]["sha256"] == (
        "sha256:" + hashlib.sha256(receipt.read_bytes()).hexdigest()
    )


@pytest.mark.parametrize(
    ("redacted_files", "failed_write_calls"),
    (([], (2,)), (["cleanup-receipt.json"], (1, 3))),
)
def test_final_result_write_failure_removes_stale_green_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    redacted_files: list[str],
    failed_write_calls: tuple[int, ...],
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "cleanup-receipt.json").write_text('{"verified":true}\n')
    (evidence / "result.json").write_text(
        json.dumps(
            {
                "status": "pass",
                "artifact_manifest": [],
                "observation": {"artifact_secret_scan_passed": True},
                "assertions": {"native_provider_resume_proven": True},
            }
        )
    )
    original_write_json = provider_native_resume._write_json
    write_calls = 0

    def fail_second_write(path: Path, payload: object) -> None:
        nonlocal write_calls
        write_calls += 1
        if write_calls in failed_write_calls:
            raise OSError("final result filesystem race")
        original_write_json(path, payload)

    monkeypatch.setattr(provider_native_resume, "_write_json", fail_second_write)

    finalized = _finalize_result_payload(
        evidence,
        {
            "status": "pass",
            "artifact_manifest": [],
            "observation": {"artifact_secret_scan_passed": True},
            "assertions": {"native_provider_resume_proven": True},
        },
        redacted_files=redacted_files,
        finalization_errors=[],
    )

    assert write_calls == max(failed_write_calls)
    assert finalized is not None
    assert finalized["status"] == "fail"
    assert finalized["failure_code"] in {
        "finalization_failed",
        "finalized_artifact_secret_scan_failed",
    }
    assert not (evidence / "result.json").exists()


@pytest.mark.parametrize("first_cleanup_raises", [False, True])
def test_run_native_resume_refreshes_failure_manifest_after_finally_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_cleanup_raises: bool,
) -> None:
    """Exercise the real failure path, including the second finally cleanup."""

    home = tmp_path / "provider-home"
    home.mkdir()
    provider_bin = tmp_path / "provider"
    provider_bin.write_text("#!/bin/sh\nprintf 'test-provider\\n'\n", encoding="utf-8")
    provider_bin.chmod(0o755)
    args = _args(tmp_path)
    args.evidence_root = tmp_path / "evidence"
    args.provider_bin = provider_bin
    args.variant = "clean_exit"
    args.live_send_timeout_secs = 1

    class FakeProcess:
        process = SimpleNamespace(poll=lambda: None)

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def settle(self) -> None:
            pass

    class FakeShipper:
        receipt = {"status": "pass", "events_shipped": 0}

        def __init__(self) -> None:
            self.stop_calls = 0

        def flush(self, _label: str) -> dict[str, object]:
            return self.receipt

        def stop(self) -> dict[str, object]:
            self.stop_calls += 1
            return {"status": "pass", "process_dead": True, "stop_generation": self.stop_calls}

    cleanup_calls = 0
    shipper: FakeShipper | None = None

    def fake_start_shipper(*_args: object, **_kwargs: object) -> FakeShipper:
        nonlocal shipper
        shipper = FakeShipper()
        return shipper

    def fake_cleanup(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal cleanup_calls
        cleanup_calls += 1
        if first_cleanup_raises and cleanup_calls == 1:
            raise OSError("cleanup receipt raced with process exit")
        return {"verified": cleanup_calls > 1, "cleanup_generation": cleanup_calls}

    monkeypatch.setattr(provider_native_resume, "_isolated_provider_home", lambda: home)
    monkeypatch.setattr(provider_native_resume, "_launch_command", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(provider_native_resume, "PtyProcess", FakeProcess)
    monkeypatch.setattr(provider_native_resume, "_start_transcript_shipper", fake_start_shipper)
    monkeypatch.setattr(provider_native_resume, "_wait_opencode_tui_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        provider_native_resume,
        "_wait_state",
        lambda *_args, **_kwargs: {
            "session_id": "session-1",
            "run_id": "run-1",
            "connection_id": "connection-1",
            "provider_session_id": "provider-session-1",
            "claude_pid": 101,
        },
    )
    monkeypatch.setattr(provider_native_resume, "_provider_process_pid", lambda *_args: 101)
    monkeypatch.setattr(provider_native_resume, "_wait_session_tail", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(provider_native_resume, "_assistant_event_digests", lambda *_args: set())
    monkeypatch.setattr(provider_native_resume, "_control_send", lambda *_args, **_kwargs: {"returncode": 0})
    monkeypatch.setattr(
        provider_native_resume,
        "_wait_assistant_response_after_marker",
        lambda *_args, **_kwargs: ({}, {"marker_observed_in_transcript": False, "new_assistant_events": 0}),
    )
    monkeypatch.setattr(provider_native_resume, "_cleanup_processes", fake_cleanup)
    monkeypatch.setattr(provider_native_resume, "_close_recordings", lambda *_args: None)
    monkeypatch.setattr(provider_native_resume, "_secret_scan", lambda *_args: [])

    result = provider_native_resume.run_native_resume("opencode", args)

    assert result["status"] == "fail"
    assert result["error"].startswith("RuntimeError: provider transcript did not correlate initial opencode marker")
    initial_correlation = json.loads((args.evidence_root / "initial-response-correlation.json").read_text())
    assert initial_correlation["marker_observed_in_transcript"] is False
    assert initial_correlation["new_assistant_events"] == 0
    assert not (args.evidence_root / "cursor-projection-diagnostics-initial-seed-before-send.json").exists()
    assert not (args.evidence_root / "cursor-projection-diagnostics-initial-seed-after-flush.json").exists()
    assert cleanup_calls == 2
    assert shipper is not None
    assert shipper.stop_calls == 2
    written = json.loads((args.evidence_root / "result.json").read_text())
    manifest = {entry["path"]: entry for entry in written["artifact_manifest"]}
    receipt = args.evidence_root / "cleanup-receipt.json"
    assert json.loads(receipt.read_text())["cleanup_generation"] == 2
    assert manifest["cleanup-receipt.json"]["size"] == receipt.stat().st_size
    assert manifest["cleanup-receipt.json"]["sha256"] == (
        "sha256:" + hashlib.sha256(receipt.read_bytes()).hexdigest()
    )
    ship_receipt = args.evidence_root / "transcript-shipper-receipt.json"
    assert json.loads(ship_receipt.read_text())["stop_generation"] == 2
    assert manifest["transcript-shipper-receipt.json"]["sha256"] == (
        "sha256:" + hashlib.sha256(ship_receipt.read_bytes()).hexdigest()
    )
    assert result == written


def test_antigravity_policy_proof_has_no_registration_or_spawn(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    # The producer must read the generated, digest-pinned contract that the
    # factory mounts as --repo-root; a synthetic tmp_path would bypass that seam.
    repo_root = Path(__file__).resolve().parents[2]
    exit_code = antigravity_resume_policy.main(
        [
            "--variant",
            "policy_disabled",
            "--evidence-root",
            str(evidence),
            "--repo-root",
            str(repo_root),
            "--engine",
            str(tmp_path / "engine"),
            "--longhouse-cli",
            str(tmp_path / "longhouse"),
        ]
    )

    result = json.loads((evidence / "result.json").read_text())
    assert exit_code == 0
    assert result["status"] == "pass"
    assert result["observation"] == {
        "disposition": "policy_disabled",
        "provider_spawn_count": 0,
        "registration_count": 0,
    }
    source = json.loads((evidence / "policy-source-receipt.json").read_text())
    assert source["reattach"] is False
    assert source["resume_capability"]["disposition"] == "policy_disabled"
    assert source["scenario_result"]["status"] == "pass"
    assert json.loads((evidence / "cleanup-receipt.json").read_text())["orphan_count"] == 0


def test_antigravity_policy_contract_digest_matches_canonical_authority() -> None:
    repo_root = Path(__file__).resolve().parents[2]

    _contract, digest = antigravity_resume_policy._policy_contract(repo_root)

    assert digest == managed_provider_contract_entry_digest("antigravity")


@pytest.mark.parametrize(
    ("reattach", "disposition"),
    ((True, "policy_disabled"), (False, "implemented")),
)
def test_antigravity_policy_proof_fails_closed_when_contract_enables_resume(
    tmp_path: Path,
    reattach: bool,
    disposition: str,
) -> None:
    manifest_path = tmp_path / "server/zerg/config/managed_provider_contracts.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "providers": [
                    {
                        "provider": "antigravity",
                        "reattach": reattach,
                        "capabilities": {"session.resume.helm": {"disposition": disposition}},
                    }
                ],
            }
        )
    )
    evidence = tmp_path / "evidence"

    exit_code = antigravity_resume_policy.main(
        [
            "--variant",
            "policy_disabled",
            "--evidence-root",
            str(evidence),
            "--repo-root",
            str(tmp_path),
            "--engine",
            str(tmp_path / "engine"),
            "--longhouse-cli",
            str(tmp_path / "longhouse"),
        ]
    )

    result = json.loads((evidence / "result.json").read_text())
    assert exit_code == 0
    assert result["status"] == "fail"
    source = json.loads((evidence / "policy-source-receipt.json").read_text())
    assert source["scenario_result"]["failure_code"] == "resume_unsupported_oracle_failed"
