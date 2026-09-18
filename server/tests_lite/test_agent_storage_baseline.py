"""The agent-storage baseline must never present an unmeasured run as healthy.

A baseline exists to be compared before and after a storage change, so a report
that silently omits a measurement is worse than no report: an empty database, a
SQLite build without `dbstat`, a failed WAL checkpoint and an unavailable `lsof`
all look exactly like good news in a summary that only prints what it managed to
collect.

`scripts/ops/agent-storage-baseline.py` therefore has to say what it could not
measure. These cases pin that: every early return and every probe failure must
land in the top-level `degraded` list and clear `healthy_baseline`. The script is
loaded by path because it is ops tooling, not server code, and a second copy of
its parsing here would be the thing that drifts.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

BASELINE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ops" / "agent-storage-baseline.py"


def _load_baseline():
    spec = importlib.util.spec_from_file_location("agent_storage_baseline", BASELINE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def baseline():
    return _load_baseline()


def test_empty_database_is_reported_as_degraded(baseline, tmp_path: Path) -> None:
    empty = tmp_path / "empty.db"
    empty.touch()

    report, degraded = baseline.collect(empty, samples=1, day="2026-01-01")

    assert report["healthy_baseline"] is False
    assert report["degraded"] == degraded
    assert any("database_file_empty" in entry for entry in degraded)
    assert "DEGRADED MEASUREMENTS" in baseline.summarize(report)


def test_uninitialized_database_is_reported_as_degraded(baseline, tmp_path: Path) -> None:
    # A file that SQLite accepts but that holds no pages: the schema queries
    # succeed and every count is empty, which is the shape that used to look
    # like a clean, contention-free baseline.
    uninitialized = tmp_path / "uninitialized.db"
    conn = sqlite3.connect(uninitialized)
    conn.execute("VACUUM")  # page 1 exists, no tables do
    conn.close()

    report, _ = baseline.collect(uninitialized, samples=1, day="2026-01-01")

    assert report["healthy_baseline"] is False
    assert any("database_uninitialized" in entry for entry in report["degraded"])


def test_missing_engine_status_and_log_are_named(baseline, tmp_path: Path) -> None:
    # `engine_status` is read next to the database; a missing status file means
    # the transport numbers are absent, not zero.
    database = tmp_path / "longhouse-shipper.db"
    conn = sqlite3.connect(database)
    conn.execute("CREATE TABLE spool_queue (provider TEXT)")
    conn.commit()
    conn.close()

    report, _ = baseline.collect(database, samples=1, day="2026-01-01")

    assert any("engine_status_missing" in entry for entry in report["degraded"])
    assert report["healthy_baseline"] is False


def test_a_partially_failed_lock_probe_is_not_a_clean_measurement(baseline, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "longhouse-shipper.db"
    conn = sqlite3.connect(database)
    conn.execute("CREATE TABLE file_state (path TEXT)")
    conn.commit()
    conn.close()

    calls = {"count": 0}
    real_connect = sqlite3.connect

    def flaky_connect(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] % 2 == 0:
            raise sqlite3.OperationalError("database is locked")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(baseline.sqlite3, "connect", flaky_connect)
    probe = baseline.lock_probe(database, samples=4)

    assert probe["failures"] == 2
    assert probe["degraded"], "some samples failing must be reported, not averaged away"
