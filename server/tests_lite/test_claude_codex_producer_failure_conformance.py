"""Failure/cleanup conformance for the Claude and Codex producer entrypoints."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from zerg.qa import claude_live_session_support
from zerg.qa import claude_native_resume
from zerg.qa import producer_shell
from zerg.qa import provider_native_resume


class _FakeShipper:
    receipt = {"status": "pass", "machine_name": "conformance-machine"}

    def __init__(self) -> None:
        self.stop_calls = 0

    def stop(self) -> dict[str, object]:
        self.stop_calls += 1
        return {"status": "pass", "stopped": True}


class _FakeSession:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.submitted: list[str] = []

    def submit_line(self, line: str) -> None:
        self.submitted.append(line)


def _executable(path: Path, output: str = "test-provider 1.0") -> Path:
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _fail_first_result_write(module: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the first write of ``result.json``, wherever the producer writes it from (the shell, once it is built in it)."""

    raised = False
    for owner in (module, producer_shell):
        original = getattr(owner, "write_json", None)
        if original is None:
            continue

        def controlled_write(path: Path, payload: object, original=original) -> None:
            nonlocal raised
            if path.name == "result.json" and not raised:
                raised = True
                raise RuntimeError("controlled failure after producer judgment")
            original(path, payload)

        monkeypatch.setattr(owner, "write_json", controlled_write)


def _assert_persisted_failure(root: Path, result: dict[str, object]) -> dict[str, object]:
    assert result["status"] == "fail"
    persisted = json.loads((root / "result.json").read_text(encoding="utf-8"))
    assert persisted["status"] == "fail"
    assert persisted["failure_code"]
    return persisted


def _install_claude_session_boundaries(
    module: object, monkeypatch: pytest.MonkeyPatch, root: Path
) -> tuple[_FakeShipper, list[_FakeSession]]:
    shipper = _FakeShipper()
    sessions: list[_FakeSession] = []

    def start_machine(*_args: object, **_kwargs: object):
        return shipper, {"HOME": str(root), "LONGHOUSE_HOME": str(root / "longhouse")}

    def launch_session(*_args: object, **kwargs: object):
        session = _FakeSession(f"session-{len(sessions) + 1}")
        sessions.append(session)
        return session, session.session_id, f"provider-{len(sessions)}"

    # The single-session producers launch through the shared scaffold; the two-session one launches its own.
    boundaries = {
        "start_machine_and_shipper": start_machine,
        "prepare_claude_profile": lambda **_kwargs: {"status": "pass"},
        "launch_claude_session": launch_session,
        "read_coordination_token": lambda *_args: "coordination-token",
        "close_session": lambda _session: {"exit_code": 0, "alive_after_close": False},
    }
    for owner in (module, claude_live_session_support):
        for name, boundary in boundaries.items():
            if hasattr(owner, name):
                monkeypatch.setattr(owner, name, boundary)
    return shipper, sessions


def _claude_args(root: Path, binary: Path, **changes: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "evidence_root": root,
        "provider_bin": binary,
        "engine": binary,
        "model": "test-model",
        "project": "test-project",
        "launch_timeout_secs": 1,
        "response_timeout_secs": 1,
        "compaction_timeout_secs": 1,
        "receipt_timeout_secs": 1,
        "inbox_timeout_secs": 1,
        "quiescent_timeout_secs": 1,
        "variant": "test-variant",
        "api_url": "http://127.0.0.1:9",
        "agents_token": "test-token",
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _codex_args(root: Path, binary: Path, **changes: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "evidence_root": root,
        "codex_bin": binary,
        "engine": binary,
        "repo_root": root,
        "api_url": "http://127.0.0.1:9",
        "agents_token": "test-token",
        "model": "test-model",
        "variant": "test-variant",
        "live_send_timeout_secs": 1,
        "bridge_start_timeout_secs": 1,
        "tui_record_secs": 1,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_claude_native_resume_bounded_provider_failure_is_typed_and_shipper_is_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "evidence"
    binary = _executable(tmp_path / "claude")
    engine = _executable(tmp_path / "engine")
    cli = _executable(tmp_path / "longhouse")
    shipper = _FakeShipper()
    monkeypatch.setenv("LONGHOUSE_RUNTIME_API_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("LONGHOUSE_RUNTIME_AGENTS_TOKEN", "test-token")
    provider_home = tmp_path / "provider-home"
    provider_home.mkdir()
    monkeypatch.setenv("HOME", str(provider_home))
    monkeypatch.setenv("LONGHOUSE_QUALIFICATION_HOME", str(provider_home))
    monkeypatch.setenv("LONGHOUSE_QUALIFICATION_SANDBOX", "provider-qualification-bwrap-v3")
    monkeypatch.setattr(provider_native_resume.live_session_toolkit, "prepare_claude_profile", lambda **_kwargs: {"status": "pass"})
    monkeypatch.setattr(provider_native_resume.live_session_toolkit, "start_transcript_shipper", lambda *_args, **_kwargs: shipper)
    # launch_command is the native provider boundary; fail it after the enrolled shipper exists.
    monkeypatch.setattr(
        provider_native_resume.live_session_toolkit,
        "launch_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("provider launch failed")),
    )
    argv = [
        "--variant",
        "clean_exit",
        "--evidence-root",
        str(root),
        "--repo-root",
        str(tmp_path),
        "--engine",
        str(engine),
        "--longhouse-cli",
        str(cli),
        "--provider-bin",
        str(binary),
    ]
    claude_native_resume.main_for("claude", argv)
    persisted = json.loads((root / "result.json").read_text(encoding="utf-8"))
    assert persisted["status"] == "fail"
    assert persisted["failure_code"]
    assert shipper.stop_calls >= 1
