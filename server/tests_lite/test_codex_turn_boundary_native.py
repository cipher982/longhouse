from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

import pytest

from zerg.qa import codex_turn_boundary_native as m


def _args(tmp_path: Path) -> argparse.Namespace:
    codex_bin = tmp_path / "codex"
    codex_bin.write_text("#!/bin/sh\n")
    codex_bin.chmod(0o755)
    engine = tmp_path / "longhouse-engine"
    engine.write_text("#!/bin/sh\n")
    engine.chmod(0o755)
    return argparse.Namespace(
        engine=engine,
        codex_bin=codex_bin,
        repo_root=tmp_path,
        api_url="https://runtime.invalid",
        agents_token="test-agents-token",
        model=None,
        bridge_start_timeout_secs=5,
        live_send_timeout_secs=2,
    )


def test_registration_matches_the_schemas_declared_cell() -> None:
    assert m.REGISTRATION.producer_id == "codex.turn_boundary_quiescent.v1"
    assert m.REGISTRATION.scenario_id == "codex_turn_boundary_quiescent"
    assert m.REGISTRATION.assertion_cells == ((m._ASSERTION_ID, None),)
    assert m.REGISTRATION.evidence_classes == ("live_token",)
    assert m.REGISTRATION.providers == ("codex",)
    # The exact --variant string execute_retained_plan will pass for a null
    # authored variant, independently recomputed from resume_assurance's own
    # execution_variant_key rather than hardcoded here.
    from zerg.qa.resume_assurance import execution_variant_key

    assert m._EXECUTION_VARIANT == execution_variant_key(
        provider="codex",
        assertion_id=m._ASSERTION_ID,
        scenario_id=m._SCENARIO_ID,
        variant=None,
    )


def test_main_registration_mode_prints_registration_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert m.main(["--registration"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["producer_id"] == "codex.turn_boundary_quiescent.v1"
    assert payload["assertion_cells"] == [{"assertion_id": m._ASSERTION_ID, "variant": None}]


def _install_bridge_fakes(monkeypatch: pytest.MonkeyPatch, *, active_window: bool) -> dict[str, Path]:
    """Fake the real bridge lifecycle exactly as test_codex_provider_release_canary.py does."""

    holder: dict[str, Path] = {}

    def fake_start(*_args: object, **kwargs: object) -> tuple[dict[str, object], subprocess.CompletedProcess[str], Path]:
        isolation_root = kwargs["isolation_root"]
        assert isinstance(isolation_root, Path)
        state_root = m.bridge_canary._bridge_state_root(isolation_root)
        state_root.mkdir(parents=True, exist_ok=True)
        state_file = state_root / "session-1.json"
        thread_path = isolation_root / "rollout.jsonl"
        thread_path.write_text("", encoding="utf-8")
        state_file.write_text(
            json.dumps({"session_id": "session-1", "thread_id": "thread-1", "thread_path": str(thread_path)}),
            encoding="utf-8",
        )
        holder["state_file"] = state_file
        holder["thread_path"] = thread_path
        summary = {"session_id": "session-1", "state_file": str(state_file), "thread_id": "thread-1"}
        return summary, subprocess.CompletedProcess(["start"], 0, json.dumps(summary), ""), isolation_root

    def fake_run(argv: list[object], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        argv_str = [str(item) for item in argv]
        if "--version" in argv_str:
            return subprocess.CompletedProcess(argv_str, 0, "0.1.0-test\n", "")
        if "send" in argv_str:
            marker = argv_str[argv_str.index("--text") + 1].split("Reply exactly ", 1)[1].split(" and nothing else.", 1)[0]
            state_file = holder["state_file"]
            thread_path = holder["thread_path"]
            state = json.loads(state_file.read_text(encoding="utf-8"))
            if active_window:
                state["active_turn_id"] = "turn-1"
                state["last_turn_status"] = "in_progress"
                state_file.write_text(json.dumps(state), encoding="utf-8")
                time.sleep(0.15)
            state["active_turn_id"] = None
            state["last_turn_status"] = "completed"
            state_file.write_text(json.dumps(state), encoding="utf-8")
            thread_path.write_text(
                json.dumps({"payload": {"type": "agent_message", "message": marker}}) + "\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv_str, 0, "{}", "")
        return subprocess.CompletedProcess(argv_str, 0, "", "")

    monkeypatch.setattr(m.bridge_canary, "_start_bridge", fake_start)
    monkeypatch.setattr(m.bridge_canary, "_run", fake_run)
    monkeypatch.setattr(m.bridge_canary, "_stop_bridge", lambda *_a, **_k: {"verification": {"verified": True, "socket_absent": True}})
    return holder


def test_run_turn_boundary_quiescent_passes_when_activity_transitions_through_a_real_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = _args(tmp_path)
    args.evidence_root = tmp_path / "evidence"
    args.variant = m._EXECUTION_VARIANT
    _install_bridge_fakes(monkeypatch, active_window=True)

    result = m.run_turn_boundary_quiescent(args)

    # The hard-won lesson this task warns about: the real validator checks
    # status == "pass" (no "-ed") and a per-assertion boolean in "assertions",
    # not a bare top-level verdict.
    assert result["status"] == "pass"
    assert result["assertions"] == {m._ASSERTION_ID: True}
    assert result["provider"] == "codex"
    assert result["variant"] is None
    assert result["scenario_id"] == m._SCENARIO_ID
    assert result["scenario_revision"] == m.REGISTRATION.scenario_revision
    assert result["evidence_class"] == "live_token"
    assert result["producer"]["producer_id"] == m.REGISTRATION.producer_id
    assert isinstance(result["artifact_manifest"], list) and result["artifact_manifest"]
    observation = result["observation"]
    assert observation["quiescent_before_turn"] is True
    assert observation["activity_became_active_during_turn"] is True
    assert observation["turn_completed"] is True
    assert observation["quiescent_after_turn_boundary"] is True
    assert observation["marker_in_assistant_transcript"] is True

    on_disk = json.loads((args.evidence_root / "result.json").read_text(encoding="utf-8"))
    assert on_disk == result


def test_run_turn_boundary_quiescent_fails_closed_without_an_observed_active_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = _args(tmp_path)
    args.evidence_root = tmp_path / "evidence"
    args.variant = m._EXECUTION_VARIANT
    args.live_send_timeout_secs = 1
    _install_bridge_fakes(monkeypatch, active_window=False)

    result = m.run_turn_boundary_quiescent(args)

    assert result["status"] == "fail"
    assert result["assertions"] == {m._ASSERTION_ID: False}
    assert result["observation"]["activity_became_active_during_turn"] is False


def test_main_serializes_result_and_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = tmp_path / "longhouse-engine"
    provider = tmp_path / "codex"
    for executable in (engine, provider):
        executable.write_text("#!/bin/sh\n")
        executable.chmod(0o755)

    monkeypatch.setenv("LONGHOUSE_RUNTIME_API_URL", "https://runtime.example")
    monkeypatch.setenv("LONGHOUSE_RUNTIME_AGENTS_TOKEN", "device-token")
    monkeypatch.setattr(m, "run_turn_boundary_quiescent", lambda _args: {"status": "pass", "marker": "sentinel"})

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
            "--codex-bin",
            str(provider),
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["marker"] == "sentinel"
