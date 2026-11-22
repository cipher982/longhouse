from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from zerg.qa import antigravity_resume_policy
from zerg.qa import cursor_native_resume
from zerg.qa import ios_workspace_selection_source_producer
from zerg.qa import live_session_toolkit
from zerg.qa import opencode_native_resume
from zerg.qa import opencode_qualification_profile
from zerg.qa import provider_native_resume


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


class _FakeShipper:
    def __init__(self) -> None:
        self.receipt = {"status": "started", "events_shipped": 0}
        self.stop_calls = 0

    def flush(self, _label: str) -> dict[str, Any]:
        return {"status": "pass", "events_shipped": 0}

    def stop(self) -> dict[str, Any]:
        self.stop_calls += 1
        return {"status": "pass", "stopped": True, "process_dead": True, "process_group_dead": True, "stop_calls": self.stop_calls}

    def capture_cursor_projection_diagnostics(self, *_args: object, **_kwargs: object) -> None:
        return None

    def cursor_turn_delivery(self, *_args: object, **_kwargs: object) -> dict[str, Any]:
        return {"known": False, "delivered": False}


class _FakePty:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.process = SimpleNamespace(pid=4242, poll=lambda: None)
        self.sent: list[str] = []
        self.recording_finalized = False

    def send(self, text: str) -> None:
        self.sent.append(text)

    def settle(self) -> None:
        return None

    def drain(self) -> bytes:
        return b""

    def finalize_recording(self) -> bool:
        self.recording_finalized = True
        return False

    def close(self) -> None:
        self.finalize_recording()

    def wait(self, _timeout: float) -> int:
        return 1

    def kill_group(self, _signal: object) -> None:
        return None


class _FakeCursorSession:
    def __init__(self, session_id: str, provider_cwd: Path) -> None:
        self.session_id = session_id
        self.provider_thread_id = f"thread-{session_id}"
        self.home = provider_cwd
        self.environment = {"HOME": str(provider_cwd)}
        self.process = _FakePty()
        self.provider_cwd = provider_cwd
        self.state = {
            "session_id": session_id,
            "provider_session_id": self.provider_thread_id,
            "run_id": f"run-{session_id}",
            "connection_id": f"connection-{session_id}",
        }


def test_antigravity_resume_policy_persists_contract_failure_without_spawn(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "repo"
    manifest = repo / "server/zerg/config/managed_provider_contracts.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "provider": "antigravity",
                        "reattach": True,
                        "capabilities": {"session.resume.helm": {"disposition": "policy_disabled"}},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    evidence = tmp_path / "evidence"

    exit_code = antigravity_resume_policy.main(
        [
            "--variant",
            "policy_disabled",
            "--evidence-root",
            str(evidence),
            "--repo-root",
            str(repo),
            "--engine",
            str(tmp_path / "engine"),
            "--longhouse-cli",
            str(tmp_path / "longhouse"),
        ]
    )

    printed = json.loads(capsys.readouterr().out)
    # A contract that still reattaches has not disabled resume: the assertion is what fails,
    # so the status and the assertion map agree, and the exit status follows the verdict.
    assert exit_code == 1
    assert printed["status"] == "fail"
    assert printed["assertions"]["unsupported_resume_is_typed_and_side_effect_free"] is False
    assert printed["observation"]["provider_spawn_count"] == 0
    assert json.loads((evidence / "cleanup-receipt.json").read_text())["orphan_count"] == 0
    assert json.loads((evidence / "result.json").read_text()) == printed


@pytest.mark.parametrize(
    ("producer_module", "provider"),
    ((cursor_native_resume, "cursor"), (opencode_native_resume, "opencode")),
)
def test_native_resume_entrypoints_retain_initial_evidence_when_late_boundary_fails(
    producer_module: Any,
    provider: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = _executable(tmp_path / "engine")
    cli = _executable(tmp_path / "longhouse")
    provider_bin = _executable(tmp_path / f"{provider}-provider")
    repo = tmp_path / "repo"
    repo.mkdir()
    evidence = tmp_path / "evidence"
    home = tmp_path / "provider-home"
    home.mkdir()

    monkeypatch.setenv(live_session_toolkit.RUNTIME_API_URL_ENV, "http://127.0.0.1:9")
    monkeypatch.setenv(live_session_toolkit.RUNTIME_AGENTS_TOKEN_ENV, "device-token")
    monkeypatch.setattr(live_session_toolkit, "require_disposable_runtime", lambda _url: None)
    monkeypatch.setattr(
        provider_native_resume,
        "subprocess",
        SimpleNamespace(run=lambda *_a, **_k: SimpleNamespace(stdout="provider 1.0\n", stderr="", returncode=0)),
    )
    monkeypatch.setattr(provider_native_resume, "sha256_file", lambda _path: "sha256:test")
    monkeypatch.setattr(live_session_toolkit, "isolated_provider_home", lambda: home)
    monkeypatch.setattr(live_session_toolkit, "initialize_cursor_workspace", lambda _path: None)
    monkeypatch.setattr(live_session_toolkit, "launch_command", lambda *_args, **_kwargs: ["fake-provider"])
    monkeypatch.setattr(live_session_toolkit, "PtyProcess", _FakePty)
    monkeypatch.setattr(live_session_toolkit, "start_transcript_shipper", lambda *_args, **_kwargs: _FakeShipper())
    monkeypatch.setattr(
        live_session_toolkit,
        "wait_state",
        lambda *_args, **_kwargs: {
            "session_id": "session-1",
            "run_id": "run-1",
            "connection_id": "connection-1",
            "provider_session_id": "provider-thread-1",
        },
    )
    monkeypatch.setattr(live_session_toolkit, "wait_session_tail", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(live_session_toolkit, "assistant_event_digests", lambda *_args, **_kwargs: set())
    monkeypatch.setattr(live_session_toolkit, "provider_process_pid", lambda *_args, **_kwargs: 4242)
    monkeypatch.setattr(
        live_session_toolkit,
        "wait_assistant_response_after_marker",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("controlled native response failure")),
    )
    monkeypatch.setattr(live_session_toolkit, "cleanup_processes", lambda *_args, **_kwargs: {"verified": True, "orphan_count": 0})
    monkeypatch.setattr(live_session_toolkit, "secret_scan", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(live_session_toolkit, "qualification_secrets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(live_session_toolkit, "bound_terminal_recordings", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(provider_native_resume, "_close_recordings", lambda *_args: None)
    monkeypatch.setattr(
        provider_native_resume,
        "_cursor_shutdown_barrier",
        lambda *_args, **_kwargs: {"status": "pass", "clean": True, "dead": True, "provider_process_dead": True},
    )
    monkeypatch.setattr(
        opencode_qualification_profile, "prepare_opencode_qualification_profile", lambda *_args, **_kwargs: {"profile": "fake"}
    )
    monkeypatch.setattr(provider_native_resume, "_send_initial_seed", lambda *_args, **_kwargs: {"returncode": 0})
    monkeypatch.setattr(provider_native_resume, "_cursor_initial_send_then_flush", lambda *_args, **_kwargs: ({}, {"status": "pass"}))
    monkeypatch.setattr(provider_native_resume, "_wait_cursor_initial_idle", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(provider_native_resume, "_wait_cursor_bootstrap_hook_sequence", lambda *_args, **_kwargs: {"observed": True})
    monkeypatch.setattr(provider_native_resume, "_cursor_hook_event_bytes", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(live_session_toolkit, "wait_cursor_tui_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(live_session_toolkit, "wait_opencode_tui_ready", lambda *_args, **_kwargs: None)

    exit_code = producer_module.main_for(
        provider,
        [
            "--variant",
            "clean_exit",
            "--evidence-root",
            str(evidence),
            "--repo-root",
            str(repo),
            "--engine",
            str(engine),
            "--longhouse-cli",
            str(cli),
            "--provider-bin",
            str(provider_bin),
            "--live-send-timeout-secs",
            "1",
        ],
    )

    printed = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert printed["status"] == "fail"
    assert printed["failure_code"] == "direct_native_resume_failed"
    assert "controlled native response failure" in printed["error"]
    assert (evidence / "provider-binary-receipt.json").exists()
    assert (evidence / "initial-seed-send.json").exists()
    assert (evidence / "initial-transcript-ship-receipt.json").exists()
    cleanup = json.loads((evidence / "cleanup-receipt.json").read_text())
    assert cleanup["verified"] is True
    assert cleanup["orphan_count"] == 0
    assert json.loads((evidence / "result.json").read_text())["status"] == "fail"


def test_ios_workspace_source_failure_keeps_typed_result_and_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = ios_workspace_selection_source_producer
    evidence = tmp_path / "evidence"

    def fail_source_oracle(*, evidence_root: Path, repo_root: Path | None = None) -> dict[str, Any]:
        raise RuntimeError("controlled source boundary failure")

    monkeypatch.setattr(module, "run_ios_workspace_selection_source_oracle", fail_source_oracle)
    result = module.run(evidence, repo_root=tmp_path / "repo")

    assert result["status"] == "fail"
    assert result["failure_code"] == "ios_workspace_selection_source_contract_failed"
    assert "controlled source boundary failure" in result["error"]
    cleanup = json.loads((evidence / "cleanup-receipt.json").read_text())
    assert cleanup["status"] == "pass"
    assert cleanup["orphan_count"] == 0
    # The source boundary failed before the oracle returned: no verdict.
    assert "assertions" not in result and "observation" not in result
    assert json.loads((evidence / "result.json").read_text()) == result
