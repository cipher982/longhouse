from __future__ import annotations

import argparse
import json
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
from zerg.qa.provider_native_resume import _accept_claude_development_channel_prompt
from zerg.qa.provider_native_resume import _accept_claude_permission_prompt
from zerg.qa.provider_native_resume import _accept_cursor_workspace_trust
from zerg.qa.provider_native_resume import _cleanup_processes
from zerg.qa.provider_native_resume import _command_from_resume_intent
from zerg.qa.provider_native_resume import _control_send
from zerg.qa.provider_native_resume import _initialize_cursor_workspace
from zerg.qa.provider_native_resume import _isolated_provider_home
from zerg.qa.provider_native_resume import _launch_command
from zerg.qa.provider_native_resume import _opencode_tui_is_connected
from zerg.qa.provider_native_resume import _provider_process_pid
from zerg.qa.provider_native_resume import _provision_transcript_roots
from zerg.qa.provider_native_resume import _start_transcript_shipper
from zerg.qa.provider_native_resume import _state_candidates
from zerg.qa.provider_native_resume import _wait_assistant_response_after_marker
from zerg.qa.provider_native_resume import _wait_claude_tui_ready
from zerg.qa.provider_native_resume import _wait_cursor_idle
from zerg.qa.provider_native_resume import _wait_cursor_tui_ready
from zerg.qa.provider_native_resume import _wait_session_tail
from zerg.qa.provider_native_resume import _wait_state
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
    assert _wait_session_tail(
        "https://runtime.example",
        "device-token",
        "session-1",
        timeout=0.01,
        allow_unprojected=True,
    ) == {}


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
            return b""

        def send(self, value: str) -> None:
            self.sent.append(value)

    process = FakeProcess()
    _wait_cursor_tui_ready(process, recording, timeout=2)  # type: ignore[arg-type]

    assert process.sent == ["a"]


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
                recording.write_text('screen redraw❯\u00a0Try "refactor <filepath>"', encoding="utf-8")
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

    monkeypatch.setenv("CODEX_API_URL", "https://runtime.example")
    monkeypatch.setenv("CODEX_AGENTS_TOKEN", "device-token")
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
    assert process.sent == ["seed\r"]


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
    assert commands == [
        [str(args.engine), "claude-channel", "send", "--session-id", "session-1", "--text", "seed"]
    ]
    assert process.sent == []


def test_claude_response_correlation_returns_measured_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "MARKER"
    tail = {
        "events": [
            {"role": "assistant", "content": "prior"},
            {"role": "user", "content": marker},
            {"role": "assistant", "content": "new response"},
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
        timeout=1,
    )

    assert observed_tail == tail
    assert correlation == {
        "method": "transcript_marker_then_new_assistant_event",
        "marker_observed_in_transcript": True,
        "prior_assistant_events": 1,
        "observed_assistant_events": 2,
        "new_assistant_events": 1,
        "timed_out": False,
    }


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
