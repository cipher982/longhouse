from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from cryptography.fernet import Fernet
from typer.testing import CliRunner

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg import build_info
from zerg.cli.main import app


def _make_payload(**overrides) -> dict:
    payload = {
        "version": "0.2.0",
        "commit": "b672fccae990c020de56139d38dcd9990bae7aa0",
        "commit_short": "b672fcca",
        "dirty": False,
        "built_at": "2026-04-21T18:03:12Z",
        "channel": "release",
    }
    payload.update(overrides)
    return payload


class _FakeResource:
    def __init__(self, raw: str | None) -> None:
        self._raw = raw

    def is_file(self) -> bool:
        return self._raw is not None

    def read_text(self, encoding: str = "utf-8") -> str:
        assert self._raw is not None
        return self._raw

    def __truediv__(self, _other: str) -> "_FakeResource":
        return self


def _install_resource(monkeypatch, payload: dict | None) -> None:
    raw = None if payload is None else json.dumps(payload)
    monkeypatch.setattr(build_info.resources, "files", lambda _pkg: _FakeResource(raw))
    build_info.reset_cache()


def test_longhouse_version_flag_release(monkeypatch):
    _install_resource(monkeypatch, _make_payload())

    runner = CliRunner()
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "longhouse 0.2.0 (b672fcca)"


def test_longhouse_version_flag_dev_dirty(monkeypatch):
    _install_resource(monkeypatch, _make_payload(channel="dev", dirty=True))

    runner = CliRunner()
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "longhouse 0.2.0-dev+b672fcca.dirty"


def test_longhouse_version_flag_json(monkeypatch):
    _install_resource(monkeypatch, _make_payload(channel="dev", dirty=True))

    runner = CliRunner()
    result = runner.invoke(app, ["--version", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["installed_version"] == "0.2.0-dev+b672fcca.dirty"
    assert payload["build"]["commit_short"] == "b672fcca"
    assert payload["build"]["channel"] == "dev"
    assert payload["build"]["dirty"] is True


def test_longhouse_version_flag_missing_identity(monkeypatch):
    _install_resource(monkeypatch, None)

    runner = CliRunner(mix_stderr=False)
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 2, result.output
    combined = (result.output or "") + (result.stderr or "")
    assert "build identity missing" in combined


def _make_db_diagnostics_fixture(tmp_path: Path) -> tuple[Path, str]:
    db_path = tmp_path / "doctor.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY)")
        conn.execute(
            """
            CREATE TABLE events(
                id INTEGER PRIMARY KEY,
                raw_json TEXT,
                raw_json_codec INTEGER,
                thread_id TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE source_lines(
                id INTEGER PRIMARY KEY,
                raw_json_codec INTEGER,
                thread_id TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE session_observations(
                id INTEGER PRIMARY KEY,
                thread_id TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX ix_events_raw_json_pending
            ON events(id)
            WHERE raw_json_codec = 0 AND raw_json IS NOT NULL
            """
        )
        conn.execute(
            """
            CREATE INDEX ix_source_lines_raw_json_pending
            ON source_lines(id)
            WHERE raw_json_codec = 0
            """
        )
        conn.execute("INSERT INTO sessions(id) VALUES ('s1')")
        conn.execute("INSERT INTO events(raw_json, raw_json_codec, thread_id) VALUES ('{}', 0, NULL)")
        conn.execute("INSERT INTO source_lines(raw_json_codec, thread_id) VALUES (0, NULL)")
        conn.execute("INSERT INTO session_observations(thread_id) VALUES (NULL)")
        conn.execute("ANALYZE")
    return db_path, f"sqlite:///{db_path}"


def test_db_doctor_json_reports_file_schema_and_deep_counts(tmp_path):
    db_path, db_url = _make_db_diagnostics_fixture(tmp_path)

    result = CliRunner().invoke(app, ["db", "doctor", "--database-url", db_url, "--json", "--deep"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["db_path"] == str(db_path)
    assert payload["db_exists"] is True
    assert payload["db_bytes"] > 0
    assert payload["disk_free_bytes"] > 0
    assert payload["schema"]["sqlite_stat1_exists"] is True
    assert payload["schema"]["raw_json_pending_indexes"]["events"] is True
    assert payload["schema"]["raw_json_pending_indexes"]["source_lines"] is True
    assert payload["deep_counts_skipped"] is False
    assert payload["deep_counts"]["events_raw_json_pending"] == 1
    assert payload["deep_counts"]["source_lines_raw_json_pending"] == 1
    assert payload["deep_counts"]["events_thread_id_null"] == 1


def test_db_optimize_json_runs_pragma(tmp_path):
    _db_path, db_url = _make_db_diagnostics_fixture(tmp_path)

    result = CliRunner().invoke(app, ["db", "optimize", "--database-url", db_url, "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "ok"
    assert payload["pragma"] == "PRAGMA optimize"
    assert payload["elapsed_ms"] >= 0


def test_migrate_can_skip_schema_convergence(tmp_path):
    db_path = tmp_path / "migrate-plan.db"
    db_url = f"sqlite:///{db_path}"

    result = CliRunner().invoke(app, ["migrate", "--database-url", db_url, "--no-schema-converge", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_converged"] is False
    assert payload["pending_before"] == []
    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
    assert "migration_runs" in tables
    assert "sessions" not in tables


# Silence unused-path parameter if any pytest collector insists.
_ = Path
