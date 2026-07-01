"""Tests for BestEffortLogger — the rate-limited logger that prevents
best-effort background threads (transcript tailers, cleanup loops) from
failing silently for an entire session. The cursor Helm tailer swallowed a
config-validation RuntimeError on every poll for weeks because of a bare
``except: pass``; this logger is the structural fix.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

from zerg.utils.log import BestEffortLogger


def _make_bf() -> tuple[BestEffortLogger, MagicMock]:
    bf = BestEffortLogger("zerg.test.best_effort", every=3)
    bf._log = MagicMock()  # type: ignore[assignment] - inject a fake logger
    return bf, bf._log  # type: ignore[return-value]


def test_first_failure_logs_warning_immediately():
    bf, mock_log = _make_bf()
    bf.failure("poll", RuntimeError("boom"))
    assert mock_log.warning.call_count == 1
    assert bf.consecutive_failures == 1
    assert bf.total_failures == 1
    assert bf.last_error == "RuntimeError: boom"


def test_subsequent_failures_rate_limited_by_every():
    bf, mock_log = _make_bf()  # every=3
    for _ in range(5):
        bf.failure("poll", RuntimeError("boom"))
    # attempt 1 logs, attempt 3 logs (3 % 3 == 0), attempts 2/4/5 silent
    assert mock_log.warning.call_count == 2


def test_success_logs_recovery_once_after_failures_and_resets():
    bf, mock_log = _make_bf()
    bf.failure("poll", RuntimeError("boom"))
    bf.failure("poll", RuntimeError("boom"))
    assert mock_log.info.call_count == 0  # no recovery logged yet
    bf.success()
    assert mock_log.info.call_count == 1  # recovery logged
    assert bf.consecutive_failures == 0
    assert bf.total_successes == 1
    assert bf.last_error is None


def test_success_after_no_failures_does_not_log():
    bf, mock_log = _make_bf()
    bf.success()
    assert mock_log.info.call_count == 0
    assert mock_log.warning.call_count == 0
    assert bf.total_successes == 1


def test_summarize_warns_when_degraded_at_exit():
    bf, mock_log = _make_bf()
    bf.failure("poll", RuntimeError("boom"))
    bf.summarize("cursor helm ingest")
    assert mock_log.warning.call_count == 2  # 1 failure + 1 summary
    summary_call = mock_log.warning.call_args_list[-1]
    assert summary_call.kwargs["consecutive_failures"] == 1
    assert summary_call.kwargs["last_error"] == "RuntimeError: boom"


def test_summarize_info_when_clean_at_exit():
    bf, mock_log = _make_bf()
    bf.success()
    bf.summarize("cursor helm ingest")
    assert mock_log.info.call_count == 1  # summary only (success was a no-op log)
    summary_call = mock_log.info.call_args_list[-1]
    assert summary_call.kwargs["total_successes"] == 1
    assert summary_call.kwargs["total_failures"] == 0


def test_best_effort_logger_does_not_import_database():
    """BestEffortLogger must be importable without DATABASE_URL — it is used in
    remote-only CLI launchers. Guards against a transitive zerg.database import
    sneaking into zerg.utils.log."""
    import subprocess
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    env = {k: v for k, v in __import__("os").environ.items() if k not in {"DATABASE_URL", "FERNET_SECRET"}}
    result = subprocess.run(
        [sys.executable, "-c", "from zerg.utils.log import BestEffortLogger, get_logger; print('OK')"],
        cwd=str(repo_root / "server"),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"BestEffortLogger import failed without DATABASE_URL:\n{result.stderr}"
    assert "OK" in result.stdout


def test_real_logger_emits_via_stdlib_fallback(caplog):
    """BestEffortLogger must emit real log records (not just update counters)
    via the stdlib fallback when structlog is absent. Uses caplog to capture."""
    bf = BestEffortLogger("zerg.test.real_emit")
    with caplog.at_level(logging.WARNING, logger="zerg"):
        bf.failure("poll", RuntimeError("boom"))
    assert any("best-effort failure" in r.getMessage() for r in caplog.records)
    assert any(r.levelno == logging.WARNING for r in caplog.records)
