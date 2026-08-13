from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from zerg.qa import claude_turn_boundary_quiescent as m
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
        quiescent_timeout_secs=5,
        variant=m._EXECUTION_VARIANT,
    )


def test_registration_matches_the_schemas_declared_cell() -> None:
    assert m.REGISTRATION.producer_id == "claude.turn_boundary_quiescent.v1"
    assert m.REGISTRATION.scenario_id == "claude_turn_boundary_quiescent"
    assert m.REGISTRATION.assertion_cells == ((m._ASSERTION_ID, None),)
    assert m.REGISTRATION.evidence_classes == ("live_token",)
    assert m.REGISTRATION.providers == ("claude",)
    # The exact --variant string execute_retained_plan will pass for a null
    # authored variant, independently recomputed from resume_assurance's own
    # execution_variant_key rather than hardcoded here.
    assert m._EXECUTION_VARIANT == execution_variant_key(
        provider="claude",
        assertion_id=m._ASSERTION_ID,
        scenario_id=m._SCENARIO_ID,
        variant=None,
    )


def test_main_registration_mode_prints_registration_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert m.main(["--registration"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["producer_id"] == "claude.turn_boundary_quiescent.v1"
    assert payload["assertion_cells"] == [{"assertion_id": m._ASSERTION_ID, "variant": None}]


def test_run_turn_boundary_passes_when_activity_settles_to_quiescent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = _args(tmp_path)
    fake_shipper = _FakeShipper()
    fake_session = _FakeSession()

    monkeypatch.setattr(m, "start_machine_and_shipper", lambda *_a, **_k: (fake_shipper, {"HOME": str(tmp_path)}))
    monkeypatch.setattr(m, "launch_claude_session", lambda **_k: (fake_session, "session-1"))
    monkeypatch.setattr(
        m,
        "send_and_await_marker",
        lambda **_k: ("/tmp/transcript.jsonl", 3, "2026-01-01T00:00:00Z"),
    )
    monkeypatch.setattr(
        m,
        "wait_for_served_quiescent",
        lambda **_k: (True, 1.25, ["thinking", "quiescent"]),
    )
    monkeypatch.setattr(m, "close_session", lambda _session: {"exit_code": 0, "alive_after_close": False})

    result = m.run_turn_boundary_scenario(args)

    # The hard-won lesson this task warns about: the real validator checks
    # status == "pass" (no "-ed") and a per-assertion boolean in "assertions",
    # not a bare top-level verdict.
    assert result["status"] == "pass"
    assert result["assertions"] == {m._ASSERTION_ID: True}
    assert result["provider"] == "claude"
    assert result["variant"] is None
    assert result["scenario_id"] == m._SCENARIO_ID
    assert result["scenario_revision"] == m.REGISTRATION.scenario_revision
    assert result["evidence_class"] == "live_token"
    assert result["producer"]["producer_id"] == m.REGISTRATION.producer_id
    assert isinstance(result["artifact_manifest"], list) and result["artifact_manifest"]
    observation = result["observation"]
    assert observation["returned_to_quiescent"] is True
    assert observation["session_closed_cleanly"] is True

    on_disk = json.loads((args.evidence_root / "result.json").read_text(encoding="utf-8"))
    assert on_disk == result


def test_run_turn_boundary_fails_closed_when_activity_never_settles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = _args(tmp_path)
    fake_shipper = _FakeShipper()
    fake_session = _FakeSession()

    monkeypatch.setattr(m, "start_machine_and_shipper", lambda *_a, **_k: (fake_shipper, {"HOME": str(tmp_path)}))
    monkeypatch.setattr(m, "launch_claude_session", lambda **_k: (fake_session, "session-1"))
    monkeypatch.setattr(m, "send_and_await_marker", lambda **_k: ("/tmp/transcript.jsonl", 3, None))
    monkeypatch.setattr(m, "wait_for_served_quiescent", lambda **_k: (False, 90.0, ["thinking", "thinking"]))
    monkeypatch.setattr(m, "close_session", lambda _session: {"exit_code": 0, "alive_after_close": False})

    result = m.run_turn_boundary_scenario(args)

    assert result["status"] == "fail"
    assert result["assertions"] == {m._ASSERTION_ID: False}
    assert result["observation"]["returned_to_quiescent"] is False
    assert fake_shipper.stopped is True


def test_run_turn_boundary_records_a_typed_failure_on_launch_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = _args(tmp_path)
    fake_shipper = _FakeShipper()

    monkeypatch.setattr(m, "start_machine_and_shipper", lambda *_a, **_k: (fake_shipper, {"HOME": str(tmp_path)}))

    def _boom(**_k: object) -> object:
        raise RuntimeError("Claude Helm process exited before its TUI became ready")

    monkeypatch.setattr(m, "launch_claude_session", _boom)

    result = m.run_turn_boundary_scenario(args)

    assert result["status"] == "fail"
    assert result["failure_code"] == "claude_turn_boundary_scenario_failed"
    assert "TUI became ready" in result["error"]
    assert fake_shipper.stopped is True


def test_main_serializes_result_and_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = tmp_path / "longhouse-engine"
    provider = tmp_path / "claude"
    for executable in (engine, provider):
        executable.write_text("#!/bin/sh\n")
        executable.chmod(0o755)

    monkeypatch.setenv("LONGHOUSE_RUNTIME_API_URL", "https://runtime.example")
    monkeypatch.setenv("LONGHOUSE_RUNTIME_AGENTS_TOKEN", "device-token")
    monkeypatch.setattr(m, "run_turn_boundary_scenario", lambda _args: {"status": "pass", "marker": "sentinel"})

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
