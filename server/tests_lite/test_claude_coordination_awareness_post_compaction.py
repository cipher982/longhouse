from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from zerg.qa import claude_coordination_awareness_post_compaction as m
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


def _args(tmp_path: Path, variant: str) -> argparse.Namespace:
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
        compaction_timeout_secs=5,
        variant=variant,
    )


def test_registration_covers_both_schema_declared_cells() -> None:
    assert m.REGISTRATION.producer_id == "claude.coordination_awareness_post_compaction.v1"
    assert m.REGISTRATION.scenario_id == "claude_coordination_awareness_post_compaction"
    assert m.REGISTRATION.assertion_cells == (
        (m._ASSERTION_VISIBLE, None),
        (m._ASSERTION_NO_DUP_BOOTSTRAP, None),
    )
    assert m.REGISTRATION.evidence_classes == ("live_token",)
    assert len(m._CELL_BY_VARIANT) == 2
    assert set(m._CELL_BY_VARIANT.values()) == {m._ASSERTION_VISIBLE, m._ASSERTION_NO_DUP_BOOTSTRAP}
    for assertion_id in (m._ASSERTION_VISIBLE, m._ASSERTION_NO_DUP_BOOTSTRAP):
        assert (
            execution_variant_key(provider="claude", assertion_id=assertion_id, scenario_id=m._SCENARIO_ID, variant=None)
            in m._CELL_BY_VARIANT
        )


def test_main_registration_mode_prints_registration_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert m.main(["--registration"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["producer_id"] == "claude.coordination_awareness_post_compaction.v1"
    assert len(payload["assertion_cells"]) == 2


def _install_session_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pre_invocation: dict[str, Any] | None,
    post_invocation: dict[str, Any] | None,
    bootstrap_config_paths: list[Path],
) -> tuple[_FakeShipper, _FakeSession]:
    fake_shipper = _FakeShipper()
    fake_session = _FakeSession()
    monkeypatch.setattr(m, "start_machine_and_shipper", lambda *_a, **_k: (fake_shipper, {"HOME": "/tmp"}))
    monkeypatch.setattr(m, "_prepare_claude_profile", lambda **_k: {"status": "pass", "has_completed_onboarding": True})
    monkeypatch.setattr(m, "launch_claude_session", lambda **_k: (fake_session, "session-1", "provider-session-1"))
    monkeypatch.setattr(m, "await_assistant_marker", lambda **_k: ("/tmp/transcript.jsonl", 3, None))
    invocations = iter([pre_invocation, post_invocation])
    monkeypatch.setattr(m, "find_tool_invocation", lambda *_a, **_k: next(invocations))
    monkeypatch.setattr(
        m,
        "find_compaction_boundary",
        lambda *_a, **_k: {
            "transcript_path": "/tmp/transcript.jsonl",
            "line": 7,
            "type": "system",
            "subtype": "compact_boundary",
        },
    )
    monkeypatch.setattr(m, "transcript_line_counts", lambda *_a, **_k: {})
    monkeypatch.setattr(m, "wait_for_terminal_quiescence", lambda *_a, **_k: None)
    monkeypatch.setattr(m, "mcp_bootstrap_config_paths", lambda *_a, **_k: bootstrap_config_paths)
    monkeypatch.setattr(m, "close_session", lambda _session: {"exit_code": 0, "alive_after_close": False})

    def stable_manifest(_root: Path) -> list[dict[str, Any]]:
        assert fake_shipper.stopped is True
        return []

    monkeypatch.setattr(m, "artifact_manifest", stable_manifest)
    return fake_shipper, fake_session


def _tool_ok(line: int) -> dict[str, Any]:
    return {"tool_name": "mcp__longhouse-coordination__peers", "tool_use_line": line, "tool_result_line": line + 1, "is_error": False}


def test_run_passes_the_visibility_cell_when_compaction_preserves_tool_access(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    variant = execution_variant_key(provider="claude", assertion_id=m._ASSERTION_VISIBLE, scenario_id=m._SCENARIO_ID, variant=None)
    args = _args(tmp_path, variant)
    bootstrap_path = tmp_path / "run" / "claude-mcp" / "session-1-abc.json"
    _shipper, fake_session = _install_session_fakes(
        monkeypatch, pre_invocation=_tool_ok(4), post_invocation=_tool_ok(9), bootstrap_config_paths=[bootstrap_path]
    )

    result = m.run_awareness_post_compaction_scenario(args)

    # The hard-won lesson this task warns about: the real validator checks
    # status == "pass" (no "-ed") and a per-assertion boolean in "assertions".
    assert result["status"] == "pass"
    assert result["assertions"][m._ASSERTION_VISIBLE] is True
    assert result["assertions"][m._ASSERTION_NO_DUP_BOOTSTRAP] is True
    assert result["observation"]["visible_bootstrap_count"] == 1
    assert result["observation"]["compaction_signal_observed"] is True
    assert len(fake_session.submitted) == 3
    assert "Call your peers tool now" in fake_session.submitted[0]
    assert fake_session.submitted[1] == "/compact"
    assert "Call your peers tool now" in fake_session.submitted[2]

    on_disk = json.loads((args.evidence_root / "result.json").read_text(encoding="utf-8"))
    assert on_disk == result


def test_run_passes_the_no_duplicate_bootstrap_cell_on_the_same_underlying_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    variant = execution_variant_key(provider="claude", assertion_id=m._ASSERTION_NO_DUP_BOOTSTRAP, scenario_id=m._SCENARIO_ID, variant=None)
    args = _args(tmp_path, variant)
    bootstrap_path = tmp_path / "run" / "claude-mcp" / "session-1-abc.json"
    _install_session_fakes(monkeypatch, pre_invocation=_tool_ok(4), post_invocation=_tool_ok(9), bootstrap_config_paths=[bootstrap_path])

    result = m.run_awareness_post_compaction_scenario(args)

    # This --variant is scored on no_duplicate_visible_bootstrap specifically,
    # even though the same run also computed the visibility assertion True.
    assert result["status"] == "pass"
    assert result["assertions"][m._ASSERTION_NO_DUP_BOOTSTRAP] is True


def test_run_fails_the_visibility_cell_when_the_post_compaction_call_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    variant = execution_variant_key(provider="claude", assertion_id=m._ASSERTION_VISIBLE, scenario_id=m._SCENARIO_ID, variant=None)
    args = _args(tmp_path, variant)
    bootstrap_path = tmp_path / "run" / "claude-mcp" / "session-1-abc.json"
    post_invocation = {"tool_name": "mcp__longhouse-coordination__peers", "tool_use_line": 9, "tool_result_line": None, "is_error": None}
    _install_session_fakes(
        monkeypatch, pre_invocation=_tool_ok(4), post_invocation=post_invocation, bootstrap_config_paths=[bootstrap_path]
    )

    result = m.run_awareness_post_compaction_scenario(args)

    assert result["status"] == "fail"
    assert result["assertions"][m._ASSERTION_VISIBLE] is False


def test_run_fails_the_no_duplicate_bootstrap_cell_when_a_second_config_appears(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    variant = execution_variant_key(provider="claude", assertion_id=m._ASSERTION_NO_DUP_BOOTSTRAP, scenario_id=m._SCENARIO_ID, variant=None)
    args = _args(tmp_path, variant)
    two_bootstrap_paths = [
        tmp_path / "run" / "claude-mcp" / "session-1-abc.json",
        tmp_path / "run" / "claude-mcp" / "session-1-def.json",
    ]
    _install_session_fakes(monkeypatch, pre_invocation=_tool_ok(4), post_invocation=_tool_ok(9), bootstrap_config_paths=two_bootstrap_paths)

    result = m.run_awareness_post_compaction_scenario(args)

    assert result["status"] == "fail"
    assert result["assertions"][m._ASSERTION_NO_DUP_BOOTSTRAP] is False
    assert result["observation"]["visible_bootstrap_count"] == 2


def test_run_records_a_typed_failure_with_the_requested_assertion_scored_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    variant = execution_variant_key(provider="claude", assertion_id=m._ASSERTION_VISIBLE, scenario_id=m._SCENARIO_ID, variant=None)
    args = _args(tmp_path, variant)
    fake_shipper = _FakeShipper()
    monkeypatch.setattr(m, "start_machine_and_shipper", lambda *_a, **_k: (fake_shipper, {"HOME": "/tmp"}))
    monkeypatch.setattr(m, "_prepare_claude_profile", lambda **_k: {"status": "pass", "has_completed_onboarding": True})

    def _boom(**_k: object) -> object:
        raise RuntimeError("longhouse claude exited before channel readiness")

    monkeypatch.setattr(m, "launch_claude_session", _boom)

    result = m.run_awareness_post_compaction_scenario(args)

    assert result["status"] == "fail"
    assert result["assertions"] == {m._ASSERTION_VISIBLE: False}
    assert fake_shipper.stopped is True


def test_main_rejects_a_variant_it_does_not_recognize(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        m.main(["--variant", "bogus-variant", "--evidence-root", str(tmp_path / "evidence")])
