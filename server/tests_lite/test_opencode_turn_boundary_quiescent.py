from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from zerg.qa import opencode_turn_boundary_quiescent as m
from zerg.qa.resume_assurance import capability_contract_shape
from zerg.services.provider_capability_schema import load_capability_assertions


def _schema_cell() -> dict[str, Any]:
    contract = capability_contract_shape(
        load_capability_assertions(),
        provider="opencode",
        capability="session.activity.turn_boundary",
    )
    assert len(contract) == 1
    return contract[0]


def test_registration_matches_the_schema_declared_cell_exactly() -> None:
    """Guard against a hand-typo'd REGISTRATION drifting from managed_providers.yml."""

    cell = _schema_cell()
    assert m.REGISTRATION.assertion_cells == ((cell["assertion_id"], cell["variant"]),)
    assert cell["variant"] is None
    assert m.REGISTRATION.scenario_id == cell["scenario_id"]
    assert "live_token" in cell["acceptable_evidence"]
    assert m.REGISTRATION.evidence_classes == ("live_token",)
    assert m.REGISTRATION.providers == ("opencode",)
    assert m.REGISTRATION.executable is True
    assert m.REGISTRATION.executable_module == "zerg.qa.opencode_turn_boundary_quiescent"
    # The schema-declared oracle_source is intentionally reproduced verbatim
    # even though (per the module docstring) it does not contain this
    # assertion's judgment; this test locks that specific, documented
    # mismatch rather than silently drifting either value.
    assert m.REGISTRATION.oracle_source == cell["oracle_source"] == "server/zerg/qa/opencode_server_qualification.py"


def test_cli_registration_flag_prints_registration_json(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = m.main(["--registration"])
    assert exit_code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == m.REGISTRATION.to_dict()


@pytest.mark.parametrize(
    "observation,expected",
    [
        (
            {
                "activity_left_quiescent_during_turn": True,
                "turn_completion_correlated_in_served_transcript": True,
                "activity_returned_to_quiescent_after_turn": True,
                "activity_remained_quiescent_post_turn": True,
            },
            True,
        ),
        (
            {
                "activity_left_quiescent_during_turn": False,
                "turn_completion_correlated_in_served_transcript": True,
                "activity_returned_to_quiescent_after_turn": True,
                "activity_remained_quiescent_post_turn": True,
            },
            False,
        ),
        (
            {
                "activity_left_quiescent_during_turn": True,
                "turn_completion_correlated_in_served_transcript": False,
                "activity_returned_to_quiescent_after_turn": True,
                "activity_remained_quiescent_post_turn": True,
            },
            False,
        ),
        (
            {
                "activity_left_quiescent_during_turn": True,
                "turn_completion_correlated_in_served_transcript": True,
                "activity_returned_to_quiescent_after_turn": False,
                "activity_remained_quiescent_post_turn": True,
            },
            False,
        ),
        (
            {
                "activity_left_quiescent_during_turn": True,
                "turn_completion_correlated_in_served_transcript": True,
                "activity_returned_to_quiescent_after_turn": True,
                "activity_remained_quiescent_post_turn": False,
            },
            False,
        ),
        ({}, False),
    ],
)
def test_turn_boundary_quiescent_assertions_requires_every_signal(observation: dict[str, Any], expected: bool) -> None:
    assert m.turn_boundary_quiescent_assertions(observation) == {m._ASSERTION_ID: expected}


class _FakePtyProcess:
    """Minimal PtyProcess stand-in: a scripted byte schedule, manually pumped."""

    def __init__(self, recording: Path, chunks: list[bytes]) -> None:
        self.recording = recording
        self._chunks = list(chunks)
        self.process = argparse.Namespace(poll=lambda: None)
        recording.touch()

    def drain(self) -> bytes:
        if self._chunks:
            chunk = self._chunks.pop(0)
            with self.recording.open("ab") as handle:
                handle.write(chunk)
            return chunk
        return b""


def test_wait_terminal_growth_detects_a_genuine_size_increase(tmp_path: Path) -> None:
    recording = tmp_path / "terminal.tty"
    process = _FakePtyProcess(recording, [b"", b"", b"some output"])
    result = m._wait_terminal_growth(process, recording, baseline=0, timeout=2.0)
    assert result is not None


def test_wait_terminal_growth_times_out_without_growth(tmp_path: Path) -> None:
    recording = tmp_path / "terminal.tty"
    process = _FakePtyProcess(recording, [])
    result = m._wait_terminal_growth(process, recording, baseline=0, timeout=0.3)
    assert result is None


def test_wait_terminal_quiescence_requires_a_stable_window(tmp_path: Path) -> None:
    recording = tmp_path / "terminal.tty"
    # Growth happens on the first couple of drains, then nothing: the
    # helper must wait out stable_seconds after the LAST byte before
    # declaring quiescence, not just after any single unchanged poll.
    process = _FakePtyProcess(recording, [b"a", b"b"])
    result = m._wait_terminal_quiescence(process, recording, timeout=2.0, stable_seconds=0.2)
    assert result is not None
    assert recording.read_bytes() == b"ab"


def test_wait_terminal_quiescence_raises_if_the_process_exits(tmp_path: Path) -> None:
    recording = tmp_path / "terminal.tty"
    process = _FakePtyProcess(recording, [])
    process.process = argparse.Namespace(poll=lambda: 1)
    with pytest.raises(RuntimeError):
        m._wait_terminal_quiescence(process, recording, timeout=1.0, stable_seconds=0.1)


class _FakeLaunchedProcess:
    """PtyProcess stand-in matching the real constructor signature.

    Writes nothing on its own -- ``_wait_terminal_growth``/
    ``_wait_terminal_quiescence`` are monkeypatched separately in the
    end-to-end tests below, so this fake only needs to satisfy the small
    surface ``run_turn_boundary_quiescent`` calls directly: ``.process.poll()``,
    ``.drain()``, ``.send()``, ``.pid``, ``.close()``.
    """

    def __init__(self, argv: list[str], *, cwd: Path, env: dict[str, str], recording: Path) -> None:
        self.argv = argv
        self.cwd = cwd
        self.env = env
        self.recording = recording
        recording.parent.mkdir(parents=True, exist_ok=True)
        recording.touch()
        self.pid = 4242
        self.process = argparse.Namespace(poll=lambda: None, pid=self.pid)
        self.closed = False

    def drain(self) -> bytes:
        return b""

    def send(self, _text: str) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeShipper:
    def __init__(self) -> None:
        self.receipt = {"status": "pass"}

    def stop(self) -> dict[str, Any]:
        return {"stopped": True, "process_dead": True, "process_group_dead": True}


def _args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        evidence_root=tmp_path / "evidence",
        variant="cell:opencode:activity_returns_to_quiescent_at_turn_boundary:opencode_turn_boundary_quiescent",
        repo_root=tmp_path / "repo",
        engine=tmp_path / "engine",
        longhouse_cli=tmp_path / "longhouse",
        provider_bin=tmp_path / "opencode",
        live_send_timeout_secs=5.0,
        api_url="https://runtime.example",
        agents_token="device-token",
    )


def _install_common_fakes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, correlation_timed_out: bool) -> None:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(m, "_isolated_provider_home", lambda: home)
    monkeypatch.setattr(m, "_start_transcript_shipper", lambda *a, **k: _FakeShipper())
    monkeypatch.setattr(m, "_launch_command", lambda *a, **k: ["longhouse", "opencode"])
    monkeypatch.setattr(m, "PtyProcess", _FakeLaunchedProcess)
    monkeypatch.setattr(
        m,
        "_wait_state",
        lambda *a, **k: {"session_id": "sess-1", "provider_session_id": "psess-1", "pid": 4242, "opencode_pid": 4242},
    )
    monkeypatch.setattr(m, "_wait_opencode_tui_ready", lambda *a, **k: None)
    monkeypatch.setattr(m, "_wait_session_tail", lambda *a, **k: {})
    monkeypatch.setattr(m, "_assistant_event_digests", lambda *a, **k: set())
    monkeypatch.setattr(
        m,
        "_wait_assistant_response_after_marker",
        lambda *a, **k: (
            {},
            {
                "timed_out": correlation_timed_out,
                "marker_observed_in_assistant": not correlation_timed_out,
                "marker_observed_in_transcript": not correlation_timed_out,
                "new_assistant_events": 0 if correlation_timed_out else 1,
            },
        ),
    )
    monkeypatch.setattr(m, "_wait_terminal_growth", lambda *a, **k: 1.0)
    monkeypatch.setattr(m, "_wait_terminal_quiescence", lambda *a, **k: 2.0)
    monkeypatch.setattr(m, "_stop", lambda *a, **k: {"dead": True, "clean": True, "provider_process_dead": True})
    monkeypatch.setattr(m, "_qualification_secrets", lambda *a, **k: ())

    class _Completed:
        stdout = "1.2.3\n"

    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: _Completed())

    provider_bin = tmp_path / "opencode"
    provider_bin.write_bytes(b"fake-opencode-binary")


def test_run_turn_boundary_quiescent_end_to_end_admissible_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_common_fakes(monkeypatch, tmp_path, correlation_timed_out=False)
    args = _args(tmp_path)

    result = m.run_turn_boundary_quiescent(args)

    # Every field zerg.qa's real _validate_execution_result top-level gate
    # checks (see provider_factory/assurance.py, quoted in the task and
    # re-verified by reading the live function), reproduced here as an
    # explicit lock so a future edit cannot silently break admissibility.
    assert result["status"] == "pass"
    assert result["provider"] == "opencode"
    assert result["variant"] is None
    assert result["scenario_id"] == m.REGISTRATION.scenario_id
    assert result["scenario_revision"] == m.REGISTRATION.scenario_revision
    assert result["evidence_class"] == "live_token"
    assert result["producer"]["producer_id"] == m.REGISTRATION.producer_id
    assert result["assertions"][m._ASSERTION_ID] is True
    assert isinstance(result["observation"], dict)
    assert isinstance(result["artifact_manifest"], list)
    assert result["artifact_manifest"], "a passing run must retain at least one evidence file"

    on_disk = json.loads((args.evidence_root / "result.json").read_text())
    assert on_disk == result

    for relative in (
        "provider-binary-receipt.json",
        "transcript-shipper-receipt.json",
        "launch-state-receipt.json",
        "turn-activity-receipt.json",
        "turn-correlation-receipt.json",
        "cleanup-receipt.json",
    ):
        assert (args.evidence_root / relative).is_file(), relative


def test_run_turn_boundary_quiescent_fails_when_correlation_times_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_common_fakes(monkeypatch, tmp_path, correlation_timed_out=True)
    args = _args(tmp_path)

    result = m.run_turn_boundary_quiescent(args)

    assert result["status"] == "fail"
    assert result["assertions"][m._ASSERTION_ID] is False
    assert result["observation"]["turn_completion_correlated_in_served_transcript"] is False
    # variant is still None on a genuine (non-exceptional) failure -- only
    # the except-branch failure shape omits it in favor of a bare status/error.
    assert result["variant"] is None


def test_run_turn_boundary_quiescent_writes_a_typed_failure_on_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_common_fakes(monkeypatch, tmp_path, correlation_timed_out=False)

    def _explode(*_a: object, **_k: object) -> None:
        raise RuntimeError("opencode Helm process is no longer live")

    monkeypatch.setattr(m, "_wait_state", _explode)
    args = _args(tmp_path)

    result = m.run_turn_boundary_quiescent(args)

    assert result["status"] == "fail"
    assert result["failure_code"] == "direct_turn_boundary_quiescent_failed"
    assert "assertions" not in result
    on_disk = json.loads((args.evidence_root / "result.json").read_text())
    assert on_disk == result


def test_main_requires_runtime_host_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(m.RUNTIME_API_URL_ENV, raising=False)
    monkeypatch.delenv(m.RUNTIME_AGENTS_TOKEN_ENV, raising=False)
    exit_code = m.main(
        [
            "--evidence-root",
            str(tmp_path / "evidence"),
            "--variant",
            "cell:opencode:activity_returns_to_quiescent_at_turn_boundary:opencode_turn_boundary_quiescent",
            "--repo-root",
            str(tmp_path),
            "--engine",
            str(tmp_path / "engine"),
            "--longhouse-cli",
            str(tmp_path / "longhouse"),
            "--provider-bin",
            str(tmp_path / "opencode"),
        ]
    )
    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["failure_code"] == "runtime_host_control_credentials_missing"


def test_main_requires_the_provider_binary_to_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(m.RUNTIME_API_URL_ENV, "https://runtime.example")
    monkeypatch.setenv(m.RUNTIME_AGENTS_TOKEN_ENV, "device-token")
    engine = tmp_path / "engine"
    engine.write_bytes(b"")
    engine.chmod(0o755)
    cli = tmp_path / "longhouse"
    cli.write_bytes(b"")
    cli.chmod(0o755)
    exit_code = m.main(
        [
            "--evidence-root",
            str(tmp_path / "evidence"),
            "--variant",
            "cell:opencode:activity_returns_to_quiescent_at_turn_boundary:opencode_turn_boundary_quiescent",
            "--repo-root",
            str(tmp_path),
            "--engine",
            str(engine),
            "--longhouse-cli",
            str(cli),
            "--provider-bin",
            str(tmp_path / "missing-opencode"),
        ]
    )
    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["failure_code"] == "opencode_binary_missing"
