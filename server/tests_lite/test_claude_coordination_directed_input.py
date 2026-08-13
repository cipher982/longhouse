from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from zerg.qa import claude_coordination_directed_input as m
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

    def alive(self) -> bool:
        return self._alive

    def submit_line(self, _text: str) -> None:
        return None

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
        receipt_timeout_secs=2,
        inbox_timeout_secs=2,
        variant=variant,
    )


def test_registration_covers_both_schema_declared_cells() -> None:
    assert m.REGISTRATION.producer_id == "claude.coordination_directed_input.v1"
    assert m.REGISTRATION.scenario_id == "claude_coordination_directed_input"
    assert m.REGISTRATION.assertion_cells == ((m._ASSERTION_SEND, None), (m._ASSERTION_RECEIVE, None))
    assert m.REGISTRATION.evidence_classes == ("live_token",)
    assert len(m._CELL_BY_VARIANT) == 2


def test_main_registration_mode_prints_registration_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert m.main(["--registration"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["producer_id"] == "claude.coordination_directed_input.v1"
    assert len(payload["assertion_cells"]) == 2


def _install_session_and_api_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    created: dict[str, Any],
    receipt_record: dict[str, Any] | None,
    inbox_record: dict[str, Any] | None,
) -> _FakeShipper:
    fake_shipper = _FakeShipper()
    sessions = iter([("sender-session", _FakeSession()), ("receiver-session", _FakeSession())])

    def fake_launch(**_k: object) -> tuple[_FakeSession, str]:
        session_id, session = next(sessions)
        return session, session_id

    monkeypatch.setattr(m, "start_machine_and_shipper", lambda *_a, **_k: (fake_shipper, {"HOME": "/tmp"}))
    monkeypatch.setattr(m, "launch_claude_session", lambda **k: fake_launch(**k))
    monkeypatch.setattr(m, "read_coordination_token", lambda _home, session_id: f"coord-token-{session_id}")
    monkeypatch.setattr(m, "close_session", lambda _session: {"exit_code": 0, "alive_after_close": False})

    def fake_api_json(_api_url: str, token: str, path: str, *, method: str = "GET", **_k: object) -> dict[str, Any]:
        if method == "POST" and path == "directed-inputs":
            return created
        if "direction=outbound" in path:
            return {"directed_inputs": [receipt_record] if receipt_record else []}
        if "direction=inbound" in path:
            return {"directed_inputs": [inbox_record] if inbox_record else []}
        raise AssertionError(f"unexpected call: {method} {path} token={token}")

    monkeypatch.setattr(m, "api_json", fake_api_json)
    return fake_shipper


def test_run_passes_the_send_cell_when_the_receipt_links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    variant = execution_variant_key(provider="claude", assertion_id=m._ASSERTION_SEND, scenario_id=m._SCENARIO_ID, variant=None)
    args = _args(tmp_path, variant)
    created = {"id": 42, "source_session_id": "sender-session", "target_session_id": "receiver-session", "input_receipt": None}
    receipt_record = {**created, "input_receipt": {"id": "receipt-1"}}
    inbox_record = {**created, "source_session_id": "sender-session"}
    _install_session_and_api_fakes(monkeypatch, created=created, receipt_record=receipt_record, inbox_record=inbox_record)

    result = m.run_directed_input_scenario(args)

    # The hard-won lesson this task warns about: the real validator checks
    # status == "pass" (no "-ed") and a per-assertion boolean in "assertions".
    assert result["status"] == "pass"
    assert result["assertions"][m._ASSERTION_SEND] is True
    assert result["assertions"][m._ASSERTION_RECEIVE] is True
    assert result["observation"]["directed_input_id"] == 42
    assert result["observation"]["source_session_id"] == "sender-session"

    on_disk = json.loads((args.evidence_root / "result.json").read_text(encoding="utf-8"))
    assert on_disk == result


def test_run_passes_the_receive_cell_on_the_same_underlying_observation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    variant = execution_variant_key(provider="claude", assertion_id=m._ASSERTION_RECEIVE, scenario_id=m._SCENARIO_ID, variant=None)
    args = _args(tmp_path, variant)
    created = {"id": 7, "source_session_id": "sender-session", "target_session_id": "receiver-session", "input_receipt": None}
    receipt_record = {**created, "input_receipt": {"id": "receipt-2"}}
    inbox_record = {**created, "source_session_id": "sender-session"}
    _install_session_and_api_fakes(monkeypatch, created=created, receipt_record=receipt_record, inbox_record=inbox_record)

    result = m.run_directed_input_scenario(args)

    assert result["status"] == "pass"
    assert result["assertions"][m._ASSERTION_RECEIVE] is True


def test_run_fails_the_send_cell_when_no_receipt_ever_links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    variant = execution_variant_key(provider="claude", assertion_id=m._ASSERTION_SEND, scenario_id=m._SCENARIO_ID, variant=None)
    args = _args(tmp_path, variant)
    created = {"id": 9, "source_session_id": "sender-session", "target_session_id": "receiver-session", "input_receipt": None}
    inbox_record = {**created, "source_session_id": "sender-session"}
    _install_session_and_api_fakes(monkeypatch, created=created, receipt_record=None, inbox_record=inbox_record)

    result = m.run_directed_input_scenario(args)

    assert result["status"] == "fail"
    assert result["assertions"][m._ASSERTION_SEND] is False


def test_run_fails_the_receive_cell_when_the_receiver_never_sees_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    variant = execution_variant_key(provider="claude", assertion_id=m._ASSERTION_RECEIVE, scenario_id=m._SCENARIO_ID, variant=None)
    args = _args(tmp_path, variant)
    created = {"id": 11, "source_session_id": "sender-session", "target_session_id": "receiver-session", "input_receipt": None}
    receipt_record = {**created, "input_receipt": {"id": "receipt-3"}}
    _install_session_and_api_fakes(monkeypatch, created=created, receipt_record=receipt_record, inbox_record=None)

    result = m.run_directed_input_scenario(args)

    assert result["status"] == "fail"
    assert result["assertions"][m._ASSERTION_RECEIVE] is False
    assert result["observation"]["input_visible"] is False


def test_run_records_a_typed_failure_with_the_requested_assertion_scored_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    variant = execution_variant_key(provider="claude", assertion_id=m._ASSERTION_SEND, scenario_id=m._SCENARIO_ID, variant=None)
    args = _args(tmp_path, variant)
    fake_shipper = _FakeShipper()
    monkeypatch.setattr(m, "start_machine_and_shipper", lambda *_a, **_k: (fake_shipper, {"HOME": "/tmp"}))

    def _boom(**_k: object) -> object:
        raise RuntimeError("longhouse claude exited before channel readiness")

    monkeypatch.setattr(m, "launch_claude_session", _boom)

    result = m.run_directed_input_scenario(args)

    assert result["status"] == "fail"
    assert result["assertions"] == {m._ASSERTION_SEND: False}
    assert fake_shipper.stopped is True


def test_main_rejects_a_variant_it_does_not_recognize(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        m.main(["--variant", "bogus-variant", "--evidence-root", str(tmp_path / "evidence")])
