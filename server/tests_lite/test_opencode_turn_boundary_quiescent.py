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
    assert m.REGISTRATION.producer_revision == 4
    assert m.REGISTRATION.scenario_revision == 2
    assert "opencode_model_profile_receipt" in m.REGISTRATION.required_artifacts
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


_REAL_WAIT_SESSION_QUIESCENCE = m._wait_session_quiescence
_REAL_SESSION_STAYS_IDLE = m._session_stays_idle


class _SpinnerPtyProcess(_FakePtyProcess):
    """A TUI whose reasoning spinner keeps redrawing: every drain produces bytes."""

    def __init__(self, recording: Path) -> None:
        super().__init__(recording, [])
        self.drains = 0

    def drain(self) -> bytes:
        self.drains += 1
        with self.recording.open("ab") as handle:
            handle.write(b"\x1b[7;6H\xe2\xa0\x8b Thinking")
        return b"x"


def _status_script(monkeypatch: pytest.MonkeyPatch, *readings: bool) -> list[bool]:
    """Feed ``opencode_session_busy`` a script; the last reading repeats."""

    remaining = list(readings)
    seen: list[bool] = []

    def busy(_state: dict[str, Any]) -> bool:
        value = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        seen.append(value)
        return value

    monkeypatch.setattr(m, "opencode_session_busy", busy)
    return seen


def test_session_quiescence_waits_for_a_stable_idle_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    process = _FakePtyProcess(tmp_path / "terminal.tty", [])
    # busy, a one-sample idle blip, busy again, then idle for good
    _status_script(monkeypatch, True, False, True, False)

    settled_at, transitions = m._wait_session_quiescence(process, {}, timeout=5.0, stable_seconds=0.3, poll_seconds=0.05)

    assert settled_at is not None
    assert [kind for _t, kind in transitions] == ["busy", "idle", "busy", "idle"]


def test_session_quiescence_ignores_a_spinner_that_never_stops_redrawing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """2026-09-30 03:58Z: the reply rendered, the server read idle, the spinner redrew 2,152 times."""

    process = _SpinnerPtyProcess(tmp_path / "terminal.tty")
    _status_script(monkeypatch, True, True, False)

    settled_at, _transitions = m._wait_session_quiescence(process, {}, timeout=5.0, stable_seconds=0.3, poll_seconds=0.05)

    assert settled_at is not None
    assert process.drains > 3
    assert process.recording.stat().st_size > 0


def test_session_quiescence_times_out_while_the_provider_stays_busy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    process = _FakePtyProcess(tmp_path / "terminal.tty", [])
    _status_script(monkeypatch, True)

    settled_at, transitions = m._wait_session_quiescence(process, {}, timeout=0.3, stable_seconds=0.1, poll_seconds=0.05)

    assert settled_at is None
    assert transitions == [[transitions[0][0], "busy"]]


def test_session_quiescence_raises_if_the_process_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    process = _FakePtyProcess(tmp_path / "terminal.tty", [])
    process.process = argparse.Namespace(poll=lambda: 1)
    _status_script(monkeypatch, False)
    with pytest.raises(RuntimeError):
        m._wait_session_quiescence(process, {}, timeout=1.0, stable_seconds=0.1)


def test_session_stays_idle_fails_on_any_busy_sample(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    process = _FakePtyProcess(tmp_path / "terminal.tty", [])
    _status_script(monkeypatch, False, False, True)
    assert m._session_stays_idle(process, {}, seconds=0.5, poll_seconds=0.05) is False

    _status_script(monkeypatch, False)
    assert m._session_stays_idle(process, {}, seconds=0.2, poll_seconds=0.05) is True


class _FakeLaunchedProcess:
    """PtyProcess stand-in matching the real constructor signature.

    Writes nothing on its own -- ``_wait_terminal_growth``/
    ``_wait_session_quiescence`` are monkeypatched separately in the
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
        api_url="http://127.0.0.1:9",
        agents_token="device-token",
    )


def _install_common_fakes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, correlation_timed_out: bool) -> None:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LONGHOUSE_OPENCODE_QUALIFICATION_MODEL", "deepseek/deepseek-v4-flash")
    monkeypatch.setattr(m, "isolated_provider_home", lambda: home)
    monkeypatch.setattr(m, "start_transcript_shipper", lambda *a, **k: _FakeShipper())
    monkeypatch.setattr(m, "launch_command", lambda *a, **k: ["longhouse", "opencode"])
    monkeypatch.setattr(m, "PtyProcess", _FakeLaunchedProcess)
    monkeypatch.setattr(
        m,
        "wait_state",
        lambda *a, **k: {"session_id": "sess-1", "provider_session_id": "psess-1", "pid": 4242, "opencode_pid": 4242},
    )
    monkeypatch.setattr(m, "wait_opencode_tui_ready", lambda *a, **k: None)
    monkeypatch.setattr(m, "wait_session_tail", lambda *a, **k: {})
    monkeypatch.setattr(m, "assistant_event_digests", lambda *a, **k: set())
    monkeypatch.setattr(
        m,
        "wait_assistant_response_after_marker",
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
    monkeypatch.setattr(m, "_wait_session_quiescence", lambda *a, **k: (2.0, [[0.0, "busy"], [1.5, "idle"]]))
    monkeypatch.setattr(m, "_session_stays_idle", lambda *a, **k: True)
    monkeypatch.setattr(m, "stop_session", lambda *a, **k: {"dead": True, "clean": True, "provider_process_dead": True})
    monkeypatch.setattr(m, "qualification_secrets", lambda *a, **k: ())

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
    activity = json.loads((args.evidence_root / "turn-activity-receipt.json").read_text())
    assert activity["quiescence_authority"] == "opencode_session_status"
    assert activity["session_status_transitions"] == [[0.0, "busy"], [1.5, "idle"]]
    cleanup = json.loads((args.evidence_root / "cleanup-receipt.json").read_text())
    assert cleanup["status"] == "pass"
    assert cleanup["orphan_count"] == 0
    assert cleanup["required_cleanup"] == {
        "managed_opencode_process_exited": True,
        "no_orphan_provider_processes": True,
    }


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

    monkeypatch.setattr(m, "wait_state", _explode)
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
    monkeypatch.setenv(m.RUNTIME_API_URL_ENV, "http://127.0.0.1:9")
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


def test_run_turn_boundary_quiescent_retains_the_serve_log_without_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The server's log goes with the sandbox home; the evidence keeps its tail, redacted."""

    _install_common_fakes(monkeypatch, tmp_path, correlation_timed_out=False)
    log = tmp_path / "serve.log"
    log.write_bytes(b"old line\n" * 100_000 + b"stream started key=sk-or-secret password=bridge-pass end\n")
    monkeypatch.setattr(m, "qualification_secrets", lambda *a, **k: ("sk-or-secret",))
    monkeypatch.setattr(
        m,
        "wait_state",
        lambda *a, **k: {
            "session_id": "sess-1",
            "provider_session_id": "psess-1",
            "pid": 4242,
            "log_path": str(log),
            "password": "bridge-pass",
        },
    )
    args = _args(tmp_path)

    result = m.run_turn_boundary_quiescent(args)

    retained = (args.evidence_root / "opencode-serve.log").read_bytes()
    assert retained.endswith(b"stream started key=<redacted> password=<redacted> end\n")
    # Bounded at 256 KiB of what is kept, after the credentials are removed.
    assert 256 * 1024 - 64 <= len(retained) <= 256 * 1024
    assert b"sk-or-secret" not in retained and b"bridge-pass" not in retained
    assert result["observation"]["serve_log"] == {
        "file": "opencode-serve.log",
        "source": "serve.log",
        "bytes_total": log.stat().st_size,
        "bytes_retained": len(retained),
        "truncated": True,
        "error": None,
    }
    # Retaining it redacted in memory means the evidence scan has nothing to flag.
    assert result["observation"]["artifact_secret_scan_passed"] is True


def test_the_serve_log_is_retained_on_a_failed_run_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_common_fakes(monkeypatch, tmp_path, correlation_timed_out=False)
    log = tmp_path / "serve.log"
    log.write_text("stream started and never finished\n")
    monkeypatch.setattr(
        m, "wait_state", lambda *a, **k: {"session_id": "sess-1", "provider_session_id": "psess-1", "pid": 4242, "log_path": str(log)}
    )

    def _explode(*_a: object, **_k: object) -> None:
        raise RuntimeError("timed out waiting for the assistant reply")

    monkeypatch.setattr(m, "wait_assistant_response_after_marker", _explode)
    args = _args(tmp_path)

    result = m.run_turn_boundary_quiescent(args)

    assert result["status"] == "fail"
    assert (args.evidence_root / "opencode-serve.log").read_text() == "stream started and never finished\n"
    assert result["serve_log"]["bytes_retained"] == len("stream started and never finished\n")


def test_a_missing_serve_log_is_reported_not_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_common_fakes(monkeypatch, tmp_path, correlation_timed_out=False)
    monkeypatch.setattr(
        m,
        "wait_state",
        lambda *a, **k: {"session_id": "sess-1", "provider_session_id": "psess-1", "pid": 4242, "log_path": str(tmp_path / "gone.log")},
    )
    args = _args(tmp_path)

    result = m.run_turn_boundary_quiescent(args)

    assert result["status"] == "pass"
    assert result["observation"]["serve_log"]["error"].startswith("FileNotFoundError")
    assert not (args.evidence_root / "opencode-serve.log").exists()


def test_a_turn_whose_spinner_never_stops_still_passes_when_the_provider_reads_idle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 2026-09-30 03:58Z shape end to end: bytes keep arriving, OpenCode says idle."""

    _install_common_fakes(monkeypatch, tmp_path, correlation_timed_out=False)
    monkeypatch.setattr(m, "_QUIESCENCE_STABLE_SECONDS", 0.2)
    # The common fakes stub the two status waits; this test runs the real ones.
    monkeypatch.setattr(m, "_wait_session_quiescence", _REAL_WAIT_SESSION_QUIESCENCE)
    monkeypatch.setattr(m, "_session_stays_idle", _REAL_SESSION_STAYS_IDLE)
    _status_script(monkeypatch, True, True, False)

    class _Spinner(_FakeLaunchedProcess):
        def drain(self) -> bytes:
            with self.recording.open("ab") as handle:
                handle.write(b"\xe2\xa0\x8b Thinking")
            return b"x"

    monkeypatch.setattr(m, "PtyProcess", _Spinner)
    args = _args(tmp_path)

    result = m.run_turn_boundary_quiescent(args)

    assert result["status"] == "pass"
    assert result["observation"]["activity_returned_to_quiescent_after_turn"] is True
    assert result["observation"]["activity_remained_quiescent_post_turn"] is True
    activity = json.loads((args.evidence_root / "turn-activity-receipt.json").read_text())
    # The terminal never settled; it is evidence, and it did not decide the verdict.
    assert activity["terminal_bytes_after_idle"] > 0


def test_a_transient_status_error_neither_counts_as_idle_nor_fails_the_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    process = _FakePtyProcess(tmp_path / "terminal.tty", [])
    readings: list[object] = [True, OSError("connection reset"), False, False]

    def busy(_state: dict[str, Any]) -> bool:
        value = readings.pop(0) if len(readings) > 1 else readings[0]
        if isinstance(value, Exception):
            raise value
        return bool(value)

    monkeypatch.setattr(m, "opencode_session_busy", busy)

    settled_at, transitions = m._wait_session_quiescence(process, {}, timeout=5.0, stable_seconds=0.2, poll_seconds=0.05)

    assert settled_at is not None
    assert [kind for _t, kind in transitions] == ["busy", "idle"]


def test_a_status_endpoint_that_stays_unreadable_is_a_failure_to_observe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    process = _FakePtyProcess(tmp_path / "terminal.tty", [])

    def gone(_state: dict[str, Any]) -> bool:
        raise OSError("connection refused")

    monkeypatch.setattr(m, "opencode_session_busy", gone)

    with pytest.raises(RuntimeError, match="unreadable"):
        m._wait_session_quiescence(process, {}, timeout=5.0, stable_seconds=0.2, poll_seconds=0.01)
    with pytest.raises(RuntimeError, match="unreadable"):
        m._session_stays_idle(process, {}, seconds=5.0, poll_seconds=0.01)


def test_one_dropped_status_read_is_retried_inside_the_shared_busy_check(monkeypatch: pytest.MonkeyPatch) -> None:
    from zerg.qa import live_session_toolkit as toolkit

    replies: list[object] = [ConnectionResetError("reset"), {"psess-1": {"type": "busy"}}]

    def get(_state: dict[str, Any], _path: str, **_kw: object) -> object:
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(toolkit, "opencode_get", get)
    monkeypatch.setattr(toolkit.time, "sleep", lambda _s: None)

    assert toolkit.opencode_session_busy({"provider_session_id": "psess-1"}) is True


def test_a_status_endpoint_that_keeps_failing_still_raises_after_the_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    from zerg.qa import live_session_toolkit as toolkit

    calls = {"count": 0}

    def get(_state: dict[str, Any], _path: str, **_kw: object) -> object:
        calls["count"] += 1
        raise ConnectionResetError("reset")

    monkeypatch.setattr(toolkit, "opencode_get", get)
    monkeypatch.setattr(toolkit.time, "sleep", lambda _s: None)

    with pytest.raises(ConnectionResetError):
        toolkit.opencode_session_busy({"provider_session_id": "psess-1"})
    assert calls["count"] == 3


def test_redaction_that_lengthens_the_log_is_still_bounded_and_says_it_was_cut(tmp_path: Path) -> None:
    """A secret shorter than the marker grows the text; the bound is on what is kept."""

    from zerg.qa import live_session_toolkit as toolkit

    log = tmp_path / "serve.log"
    log.write_bytes(b"ab" * 50)  # exactly the bound: nothing is truncated on read
    destination = tmp_path / "opencode-serve.log"

    receipt = toolkit.retain_opencode_serve_log(destination, {"log_path": str(log)}, ["ab"], max_bytes=100)

    assert len(destination.read_bytes()) == 100
    assert destination.read_bytes() == b"<redacted>" * 10
    assert receipt["truncated"] is True and receipt["bytes_total"] == 100 and receipt["bytes_retained"] == 100


def test_a_serve_log_that_cannot_be_written_is_reported_not_raised(tmp_path: Path) -> None:
    from zerg.qa import live_session_toolkit as toolkit

    log = tmp_path / "serve.log"
    log.write_text("stream started\n")

    receipt = toolkit.retain_opencode_serve_log(tmp_path / "no-such-dir" / "opencode-serve.log", {"log_path": str(log)}, [])

    assert receipt["error"].startswith("FileNotFoundError")
    assert receipt["bytes_retained"] == 0
