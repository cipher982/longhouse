"""Focused safety tests for the installed managed-launch evidence harness."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "qa" / "installed-managed-launch-fault-matrix.py"
_SPEC = importlib.util.spec_from_file_location("installed_launch_fault_matrix", _SCRIPT)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)


def _result(returncode: int, *, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_process_records_fails_closed_on_unexpected_ps_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _MODULE.subprocess,
        "run",
        lambda *args, **kwargs: _result(2, stderr="ps failed"),
    )

    with pytest.raises(_MODULE.ProcessScanFailure, match="returncode=2"):
        _MODULE.process_records({1234})


def test_process_group_pids_accepts_pgrep_no_match(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _MODULE.subprocess,
        "run",
        lambda *args, **kwargs: _result(1),
    )

    assert _MODULE.process_group_pids(1234) == set()


def test_process_group_pids_fails_closed_on_unexpected_pgrep_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _MODULE.subprocess,
        "run",
        lambda *args, **kwargs: _result(2, stderr="pgrep failed"),
    )

    with pytest.raises(_MODULE.ProcessScanFailure, match="returncode=2"):
        _MODULE.process_group_pids(1234)


def test_process_start_identity_fails_closed_on_unexpected_ps_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _MODULE.subprocess,
        "run",
        lambda *args, **kwargs: _result(2, stderr="ps failed"),
    )

    with pytest.raises(_MODULE.ProcessScanFailure, match="returncode=2"):
        _MODULE.process_start_identity(1234)


def test_success_measurements_include_post_teardown_completion() -> None:
    artifact = {"artifact_kind": "installed_managed_launch_fault_matrix"}

    result = _MODULE.record_success_measurements(
        artifact,
        run_started_at="2026-08-04T15:00:00Z",
        run_started_monotonic=10.0,
        host_outage_started_at="2026-08-04T15:00:01Z",
        host_recovery_started_at="2026-08-04T15:00:01Z",
        host_recovery_started_monotonic=11.0,
        recovery_completed_at="2026-08-04T15:00:02Z",
        recovery_completed_monotonic=12.0,
        cleanup_completed_at="2026-08-04T15:00:03Z",
        run_completed_at="2026-08-04T15:00:04Z",
        run_completed_monotonic=14.0,
    )

    assert result["generated_at"] == "2026-08-04T15:00:04Z"
    assert result["measurements"]["cleanup_completed_at"] == ("2026-08-04T15:00:03Z")
    assert result["measurements"]["run_completed_at"] > result["measurements"]["cleanup_completed_at"]
    assert result["measurements"]["run_duration_seconds"] == 4.0


def test_retry_backoff_cadence_requires_two_scheduled_transitions() -> None:
    observations = [
        {
            "session_id": "session-1",
            "attempts": 1,
            "observed_monotonic_seconds": 0.0,
        },
        {
            "session_id": "session-1",
            "attempts": 2,
            "observed_monotonic_seconds": 2.2,
        },
        {
            "session_id": "session-1",
            "attempts": 3,
            "observed_monotonic_seconds": 6.5,
        },
    ]

    _MODULE.validate_retry_backoff_cadence(
        observations, expected_session_ids={"session-1"}
    )


def test_retry_backoff_cadence_rejects_missing_or_tight_evidence() -> None:
    with pytest.raises(RuntimeError, match="incomplete"):
        _MODULE.validate_retry_backoff_cadence(
            [
                {
                    "session_id": "session-1",
                    "attempts": 1,
                    "observed_monotonic_seconds": 0.0,
                }
            ],
            expected_session_ids={"session-1"},
        )

    with pytest.raises(RuntimeError, match="too tight"):
        _MODULE.validate_retry_backoff_cadence(
            [
                {
                    "session_id": "session-1",
                    "attempts": 1,
                    "observed_monotonic_seconds": 0.0,
                },
                {
                    "session_id": "session-1",
                    "attempts": 2,
                    "observed_monotonic_seconds": 0.5,
                },
                {
                    "session_id": "session-1",
                    "attempts": 3,
                    "observed_monotonic_seconds": 5.2,
                },
            ],
            expected_session_ids={"session-1"},
        )


def test_temporary_root_deletion_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    temporary_root = tmp_path / "temporary-root"
    temporary_root.mkdir()

    def fail_rmtree(*args: object, **kwargs: object) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(_MODULE.shutil, "rmtree", fail_rmtree)

    with pytest.raises(_MODULE.ProcessScanFailure, match="deletion failed"):
        _MODULE.remove_temporary_root(temporary_root)


def test_temporary_root_residual_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    temporary_root = tmp_path / "temporary-root"
    temporary_root.mkdir()
    monkeypatch.setattr(_MODULE.shutil, "rmtree", lambda *args, **kwargs: None)

    with pytest.raises(_MODULE.ProcessScanFailure, match="left the path present"):
        _MODULE.remove_temporary_root(temporary_root)


def test_harness_provenance_verification_fails_closed_on_git_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _MODULE.subprocess,
        "run",
        lambda *args, **kwargs: _result(1, stderr="git unavailable"),
    )

    with pytest.raises(_MODULE.ProcessScanFailure, match="repository_git_sha"):
        _MODULE.verified_harness_provenance()


def test_source_provenance_uses_identity_compiled_into_binary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    binary = tmp_path / "longhouse"
    binary.write_bytes(b"stale binary")
    reported_sha = "a" * 40
    monkeypatch.setattr(
        _MODULE.subprocess,
        "run",
        lambda *args, **kwargs: _result(
            0,
            stdout=json.dumps(
                {
                    "facade": {
                        "commit": reported_sha,
                        "dirty": False,
                    },
                    "engine_path": str(tmp_path / "longhouse-engine"),
                }
            ),
        ),
    )

    provenance = _MODULE.source_provenance(binary)

    assert provenance["source_git_sha"] == reported_sha
    assert provenance["source_dirty"] is False
    assert provenance["build_identity"]["facade"]["commit"] == reported_sha


def test_implementation_stability_rejects_replaced_binary() -> None:
    before = {
        "longhouse": {"sha256": "a" * 64},
        "longhouse_engine": {"sha256": "b" * 64},
    }
    after = {
        "longhouse": {"sha256": "c" * 64},
        "longhouse_engine": {"sha256": "b" * 64},
    }

    with pytest.raises(RuntimeError, match="longhouse executable changed"):
        _MODULE.assert_implementation_stable(before, after)


def test_finish_live_command_waits_before_forcing_provider_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    provider_identities = iter(("start-1", None))

    class NaturalProcess:
        pid = 123
        returncode = 0
        stdout = None

        def poll(self) -> int:
            events.append("poll")
            return 0

    monkeypatch.setattr(
        _MODULE,
        "process_start_identity",
        lambda pid: next(provider_identities, None),
    )
    monkeypatch.setattr(_MODULE, "scoped_process_table", lambda scope: {})
    monkeypatch.setattr(
        _MODULE,
        "kill_group",
        lambda *args, **kwargs: events.append("kill"),
    )
    monkeypatch.setattr(_MODULE.os, "write", lambda *args: events.append("input"))
    monkeypatch.setattr(_MODULE.os, "close", lambda *args: events.append("close"))

    result = _MODULE.finish_live_command(
        _MODULE.LiveCommand(
            process=NaturalProcess(),
            output_fd=-1,
            output=bytearray(),
            is_tty=True,
            provider_ready_observed=True,
        ),
        provider_pid=456,
        provider_process_start_time="start-1",
        cleanup_scope={"provider_pgid": 456, "provider_process_observed": True},
    )

    assert result["status"] == "pass"
    assert result["natural_cleanup_observed"] is True
    assert result["provider_process_group_cleanup"] == "natural"
    assert "kill" not in events


def test_finish_live_command_rejects_unverified_natural_provider_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NaturalProcess:
        pid = 123
        returncode = 0
        stdout = None

        def poll(self) -> int:
            return 0

    monkeypatch.setattr(_MODULE, "process_start_identity", lambda pid: "start-1")
    monkeypatch.setattr(_MODULE.os, "write", lambda *args: None)
    monkeypatch.setattr(_MODULE.os, "close", lambda *args: None)
    monkeypatch.setattr(_MODULE, "scoped_process_table", lambda scope: {})

    result = _MODULE.finish_live_command(
        _MODULE.LiveCommand(
            process=NaturalProcess(),
            output_fd=-1,
            output=bytearray(),
            is_tty=True,
            provider_ready_observed=True,
        ),
        provider_pid=456,
        provider_process_start_time="start-1",
        cleanup_scope={"provider_pgid": None, "provider_process_observed": True},
    )

    assert result["natural_cleanup_observed"] is True
    assert result["provider_process_group_cleanup"] == "natural_unscoped"
    assert result["status"] == "fail"


def test_finish_live_command_accepts_natural_cleanup_after_provider_group_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NaturalProcess:
        pid = 123
        returncode = 0
        stdout = None

        def poll(self) -> int:
            return 0

    identities = iter(("start-1", None))
    monkeypatch.setattr(
        _MODULE,
        "process_start_identity",
        lambda pid: next(identities, None),
    )
    monkeypatch.setattr(_MODULE.os, "write", lambda *args: None)
    monkeypatch.setattr(_MODULE.os, "close", lambda *args: None)
    monkeypatch.setattr(_MODULE, "scoped_process_table", lambda scope: {})

    result = _MODULE.finish_live_command(
        _MODULE.LiveCommand(
            process=NaturalProcess(),
            output_fd=-1,
            output=bytearray(),
            is_tty=True,
            provider_ready_observed=True,
        ),
        provider_pid=456,
        provider_process_start_time="start-1",
        cleanup_scope={"provider_pgid": 456, "provider_process_observed": True},
    )

    assert result["natural_cleanup_observed"] is True
    assert result["provider_process_group_cleanup"] == "natural"
    assert result["status"] == "pass"


def test_finish_live_command_rejects_provider_cleanup_without_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NaturalProcess:
        pid = 123
        returncode = 0
        stdout = None

        def poll(self) -> int:
            return 0

    monkeypatch.setattr(_MODULE, "process_start_identity", lambda pid: "start-1")
    monkeypatch.setattr(_MODULE.os, "write", lambda *args: None)
    monkeypatch.setattr(_MODULE.os, "close", lambda *args: None)
    monkeypatch.setattr(_MODULE, "scoped_process_table", lambda scope: {})

    result = _MODULE.finish_live_command(
        _MODULE.LiveCommand(
            process=NaturalProcess(),
            output_fd=-1,
            output=bytearray(),
            is_tty=True,
            provider_ready_observed=True,
        ),
        provider_pid=456,
        provider_process_start_time=None,
        cleanup_scope={"provider_pgid": 456, "provider_process_observed": True},
    )

    assert result["provider_process_group_cleanup"] == "identity_missing"
    assert result["status"] == "fail"


def test_run_tty_command_uses_pty_interrupt_without_direct_group_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[str] = []
    monkeypatch.setattr(
        _MODULE.os,
        "killpg",
        lambda *args: signals.append("killpg"),
    )
    monkeypatch.setattr(
        _MODULE,
        "kill_group",
        lambda *args, **kwargs: signals.append("kill_group"),
    )

    evidence = _MODULE.run_tty_command(
        [
            sys.executable,
            "-c",
            "print('READY', flush=True); input()",
        ],
        {},
        marker="READY",
        timeout=5,
    )

    assert evidence.marker_seen is True
    assert evidence.timed_out is False
    assert signals == []

    natural_exit = _MODULE.run_tty_command(
        [sys.executable, "-c", "print('READY', flush=True)"],
        {},
        marker="READY",
        timeout=5,
    )
    assert natural_exit.marker_seen is True
    assert natural_exit.timed_out is False
    assert signals == []


def test_run_tty_command_reaps_child_after_pty_eio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_read = _MODULE.os.read
    reads = 0

    def read_with_eio(fd: int, size: int) -> bytes:
        nonlocal reads
        reads += 1
        if reads > 1:
            raise OSError(_MODULE.errno.EIO, "pty closed")
        return original_read(fd, size)

    monkeypatch.setattr(_MODULE.os, "read", read_with_eio)
    monkeypatch.setattr(_MODULE, "kill_group", lambda *args, **kwargs: pytest.fail("timed out"))

    evidence = _MODULE.run_tty_command(
        [sys.executable, "-c", "print('READY', flush=True)"],
        {},
        marker="READY",
        timeout=5,
    )

    assert evidence.marker_seen is True
    assert evidence.timed_out is False


def test_run_tty_command_drains_output_after_fast_exit() -> None:
    evidence = _MODULE.run_tty_command(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('x' * 20000 + 'READY\\n'); sys.stdout.flush()",
        ],
        {},
        marker="READY",
        timeout=5,
    )

    assert evidence.marker_seen is True
    assert evidence.timed_out is False
    assert evidence.output.replace("\r\n", "\n").endswith("READY\n")


def test_run_matrix_records_completion_after_teardown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    events: list[str] = []
    provider_root: Path | None = None
    retry_count_calls = 0

    class FakeEngine:
        def wait(self, **kwargs: object) -> None:
            return None

    def fake_resolve_file(raw: str | None, name: str) -> Path:
        return tmp_path / name

    def fake_run_provider_live(*args: object, **kwargs: object) -> tuple[dict, None]:
        nonlocal provider_root
        provider_root = kwargs["root"]
        return (
            {
                "provider": "claude",
                "launch_intent_created": True,
                "provider_cwd": str(provider_root / "provider"),
                "session_id_observed": "session-1",
                "provider_ready_observed": True,
                "startup_failure": None,
                "provider_pid": 123,
                "launcher_pid": 456,
            },
            None,
        )

    def fake_read_retry_count(root: Path) -> int:
        nonlocal retry_count_calls
        retry_count_calls += 1
        return 1 if retry_count_calls == 1 else 0

    def fake_read_retry_intents(root: Path) -> list[dict[str, object]]:
        assert provider_root is not None
        return [
            {
                "provider_name": "claude",
                "expected_session_id": "session-1",
                "provider_ready": True,
                "provider_pid": 123,
                "provider_process_start_time": "start-1",
                "provider_exited": False,
                "launcher_pid": 456,
                "attempts": 1,
            }
        ]

    def fake_record_success_measurements(artifact: dict[str, object], **kwargs: object) -> dict[str, object]:
        nonlocal measurement_helper_called
        measurement_helper_called = True
        assert events == ["temporary-root-deleted"]
        return artifact

    original_rmtree = _MODULE.shutil.rmtree
    measurement_helper_called = False

    def tracked_rmtree(path: Path, **kwargs: object) -> None:
        events.append("temporary-root-deleted")
        original_rmtree(path, **kwargs)

    monkeypatch.setattr(_MODULE, "resolve_file", fake_resolve_file)
    monkeypatch.setattr(_MODULE, "version_probe", lambda path: {"returncode": 0})
    monkeypatch.setattr(_MODULE, "run_provider_live", fake_run_provider_live)
    monkeypatch.setattr(_MODULE, "read_retry_count", fake_read_retry_count)
    monkeypatch.setattr(_MODULE, "read_retry_intents", fake_read_retry_intents)
    monkeypatch.setattr(_MODULE, "retry_intent_matches_launch", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        _MODULE,
        "launch_attempt_states",
        lambda *args: {"session-1": "adopted"},
    )
    monkeypatch.setattr(_MODULE, "start_host", lambda *args, **kwargs: object())
    monkeypatch.setattr(_MODULE, "stop_host", lambda *args, **kwargs: None)
    monkeypatch.setattr(_MODULE, "stop_processes_for_root", lambda *args: None)
    monkeypatch.setattr(_MODULE, "create_device_token", lambda *args: "token")
    monkeypatch.setattr(_MODULE, "runtime_env", lambda *args: {})
    monkeypatch.setattr(_MODULE, "kill_group", lambda *args, **kwargs: None)
    monkeypatch.setattr(_MODULE.subprocess, "Popen", lambda *args, **kwargs: FakeEngine())

    def fake_source_provenance(path: Path, **kwargs: object) -> dict[str, object]:
        del kwargs
        path = path.resolve()
        commit = "c" * 40
        if path.name == "longhouse":
            identity = {
                "facade": {"commit": commit, "dirty": False},
                "engine": {"commit": commit, "dirty": False},
                "engine_path": str((tmp_path / "longhouse-engine").resolve()),
            }
        else:
            identity = {"commit": commit, "dirty": False}
        return {
            "path": str(path),
            "sha256": "a" * 64 if path.name == "longhouse" else "b" * 64,
            "source_git_sha": commit,
            "source_dirty": False,
            "build_identity": identity,
            "build_identity_error": None,
        }

    monkeypatch.setattr(_MODULE, "source_provenance", fake_source_provenance)
    monkeypatch.setattr(
        _MODULE,
        "harness_provenance",
        lambda: {
            "repository_git_sha": "test-sha",
            "repository_dirty": False,
            "harness_file_dirty": False,
        },
    )
    monkeypatch.setattr(_MODULE.shutil, "rmtree", tracked_rmtree)
    monkeypatch.setattr(_MODULE, "record_success_measurements", fake_record_success_measurements)

    args = SimpleNamespace(
        longhouse_bin=None,
        engine_bin=None,
        evidence_root=tmp_path / "evidence",
        provider=["claude"],
        credentialed=False,
        concurrent=False,
        cold_restart=False,
        allow_unqualified_recovery=False,
    )

    _MODULE.run_matrix(args)
    assert measurement_helper_called is True
