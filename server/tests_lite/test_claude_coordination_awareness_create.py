from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from zerg.qa import claude_coordination_awareness_create as m
from zerg.qa.resume_assurance import execution_variant_key


class _FakeShipper:
    def __init__(self) -> None:
        self.receipt = {"status": "started"}
        self.stopped = False

    def stop(self) -> dict[str, Any]:
        self.stopped = True
        return {"stopped": True}


class _FakeProcess:
    returncode: int | None = 0


class _FakeSession:
    def __init__(self) -> None:
        self.process = _FakeProcess()
        self._alive = True
        self.submitted: list[str] = []

    def alive(self) -> bool:
        return self._alive

    def submit_line(self, text: str) -> None:
        self.submitted.append(text)

    def close(self) -> None:
        self._alive = False


def _args(tmp_path: Path) -> argparse.Namespace:
    claude_bin = tmp_path / "claude"
    claude_bin.write_text("#!/bin/sh\n")
    claude_bin.chmod(0o755)
    engine = tmp_path / "longhouse-engine"
    engine.write_text("#!/bin/sh\n")
    engine.chmod(0o755)
    return argparse.Namespace(
        evidence_root=tmp_path / "evidence",
        repo_root=tmp_path,
        engine=engine,
        claude_bin=claude_bin,
        project="zerg",
        model=None,
        api_url="https://runtime.invalid",
        agents_token="test-agents-token",
        launch_timeout_secs=5,
        response_timeout_secs=5,
        variant=m._EXECUTION_VARIANT,
    )


def test_registration_matches_the_schemas_declared_cell() -> None:
    assert m.REGISTRATION.producer_id == "claude.coordination_awareness_create.v1"
    assert m.REGISTRATION.scenario_id == "claude_coordination_awareness_create"
    assert m.REGISTRATION.assertion_cells == ((m._ASSERTION_ID, None),)
    assert m.REGISTRATION.evidence_classes == ("live_token",)
    assert m._EXECUTION_VARIANT == execution_variant_key(
        provider="claude",
        assertion_id=m._ASSERTION_ID,
        scenario_id=m._SCENARIO_ID,
        variant=None,
    )


def test_main_registration_mode_prints_registration_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert m.main(["--registration"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["producer_id"] == "claude.coordination_awareness_create.v1"
    assert payload["assertion_cells"] == [{"assertion_id": m._ASSERTION_ID, "variant": None}]


def _install_session_fakes(monkeypatch: pytest.MonkeyPatch, *, tool_invocation: dict[str, Any] | None) -> tuple[_FakeShipper, _FakeSession]:
    fake_shipper = _FakeShipper()
    fake_session = _FakeSession()
    monkeypatch.setattr(m, "start_machine_and_shipper", lambda *_a, **_k: (fake_shipper, {"HOME": "/tmp"}))
    monkeypatch.setattr(m, "_prepare_claude_profile", lambda **_k: {"status": "pass", "has_completed_onboarding": True})
    monkeypatch.setattr(m, "launch_claude_session", lambda **_k: (fake_session, "session-1", "provider-session-1"))
    monkeypatch.setattr(m, "await_assistant_marker", lambda **_k: ("/tmp/transcript.jsonl", 3, None))
    monkeypatch.setattr(m, "find_tool_invocation", lambda *_a, **_k: tool_invocation)
    monkeypatch.setattr(m, "close_session", lambda _session: {"exit_code": 0, "alive_after_close": False})

    def stable_manifest(_root: Path) -> list[dict[str, Any]]:
        assert fake_shipper.stopped is True
        return []

    monkeypatch.setattr(m, "artifact_manifest", stable_manifest)
    return fake_shipper, fake_session


def test_run_awareness_create_passes_when_the_model_actually_calls_peers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _args(tmp_path)
    _shipper, fake_session = _install_session_fakes(
        monkeypatch,
        tool_invocation={
            "tool_name": "mcp__longhouse-coordination__peers",
            "tool_use_line": 4,
            "tool_result_line": 5,
            "is_error": False,
            "result": [{"peers": []}],
        },
    )

    result = m.run_awareness_create_scenario(args)

    # The hard-won lesson this task warns about: the real validator checks
    # status == "pass" (no "-ed") and a per-assertion boolean in "assertions".
    assert result["status"] == "pass"
    assert result["assertions"] == {m._ASSERTION_ID: True}
    assert result["provider"] == "claude"
    assert result["scenario_id"] == m._SCENARIO_ID
    assert result["evidence_class"] == "live_token"
    assert result["producer"]["producer_id"] == m.REGISTRATION.producer_id
    assert result["observation"]["coordination_instructions_model_visible"] is True
    assert len(fake_session.submitted) == 1
    assert "Call your peers tool now" in fake_session.submitted[0]

    on_disk = json.loads((args.evidence_root / "result.json").read_text(encoding="utf-8"))
    assert on_disk == result


def test_run_awareness_create_fails_when_no_tool_call_is_observed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _args(tmp_path)
    _install_session_fakes(monkeypatch, tool_invocation=None)

    result = m.run_awareness_create_scenario(args)

    assert result["status"] == "fail"
    assert result["assertions"] == {m._ASSERTION_ID: False}


def test_run_awareness_create_fails_when_the_tool_call_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _args(tmp_path)
    _install_session_fakes(
        monkeypatch,
        tool_invocation={
            "tool_name": "mcp__longhouse-coordination__peers",
            "tool_use_line": 4,
            "tool_result_line": 5,
            "is_error": True,
            "result": "peers requires repo or a current managed session with git_repo or cwd",
        },
    )

    result = m.run_awareness_create_scenario(args)

    assert result["status"] == "fail"
    assert result["assertions"] == {m._ASSERTION_ID: False}


def test_main_serializes_result_and_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    engine = tmp_path / "longhouse-engine"
    provider = tmp_path / "claude"
    for executable in (engine, provider):
        executable.write_text("#!/bin/sh\n")
        executable.chmod(0o755)

    monkeypatch.setenv("LONGHOUSE_RUNTIME_API_URL", "https://runtime.example")
    monkeypatch.setenv("LONGHOUSE_RUNTIME_AGENTS_TOKEN", "device-token")
    monkeypatch.setattr(m, "run_awareness_create_scenario", lambda _args: {"status": "pass", "marker": "sentinel"})

    exit_code = m.main(
        [
            "--variant",
            m._EXECUTION_VARIANT,
            "--evidence-root",
            str(tmp_path / "evidence"),
            "--repo-root",
            str(tmp_path),
            "--engine",
            str(engine),
            "--claude-bin",
            str(provider),
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["marker"] == "sentinel"


def test_main_rejects_a_variant_it_does_not_recognize(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        m.main(["--variant", "bogus-variant", "--evidence-root", str(tmp_path / "evidence")])
