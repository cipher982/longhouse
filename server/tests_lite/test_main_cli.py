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
        conn.execute("CREATE TABLE empty_payload(id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("CREATE TABLE filled_payload(id INTEGER PRIMARY KEY, body TEXT)")
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
        conn.executemany(
            "INSERT INTO filled_payload(body) VALUES (?)",
            [("x" * 2048,) for _ in range(100)],
        )
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
    assert payload["backup_file_count"] == 0
    assert payload["backup_scan_truncated"] is False
    assert payload["schema"]["sqlite_stat1_exists"] is True
    assert payload["schema"]["raw_json_pending_indexes"]["events"] is True
    assert payload["schema"]["raw_json_pending_indexes"]["source_lines"] is True
    assert payload["deep_counts_skipped"] is False
    assert payload["deep_counts"]["events_raw_json_pending"] == 1
    assert payload["deep_counts"]["source_lines_raw_json_pending"] == 1
    assert payload["deep_counts"]["identity_counts_skipped"] is True
    assert payload["deep_counts"]["events_thread_id_null"] is None
    assert payload["table_bytes_skipped"] is True
    assert payload["table_bytes"] is None


def test_db_doctor_table_bytes_are_separately_opted_in(tmp_path):
    _db_path, db_url = _make_db_diagnostics_fixture(tmp_path)

    result = CliRunner().invoke(app, ["db", "doctor", "--database-url", db_url, "--json", "--table-bytes"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    table_bytes = payload["table_bytes"]
    assert payload["table_bytes_skipped"] is False
    assert table_bytes["available"] is True
    assert table_bytes["total_bytes"] > 0
    assert table_bytes["total_pages"] > 0
    assert table_bytes["tables"]["filled_payload"]["bytes"] > table_bytes["tables"]["empty_payload"]["bytes"]
    assert table_bytes["tables"]["events"]["index_bytes"] > 0
    assert table_bytes["tables"]["events"]["index_count"] >= 1
    freelist_bytes = payload["db_freelist_count"] * payload["db_page_size"]
    assert abs(payload["db_page_bytes"] - freelist_bytes - table_bytes["total_bytes"]) <= payload["db_page_size"]


def test_db_sample_table_bytes_writes_cache_and_doctor_reports_fresh_cache(tmp_path):
    db_path, db_url = _make_db_diagnostics_fixture(tmp_path)
    cache_path = Path(f"{db_path}.table-bytes.json")

    sample = CliRunner().invoke(app, ["db", "sample-table-bytes", "--database-url", db_url, "--json"])

    assert sample.exit_code == 0, sample.output
    sample_payload = json.loads(sample.output)
    assert sample_payload["status"] == "ok"
    assert sample_payload["table_bytes"]["available"] is True
    assert cache_path.exists()

    doctor = CliRunner().invoke(app, ["db", "doctor", "--database-url", db_url, "--json", "--table-bytes-cache"])

    assert doctor.exit_code == 0, doctor.output
    payload = json.loads(doctor.output)
    cache = payload["table_bytes_cache"]
    assert cache["exists"] is True
    assert cache["status"] == "ok"
    assert cache["fresh"] is True
    assert cache["db_bytes_at_sample"] == sample_payload["db_bytes_at_sample"]
    assert cache["db_bytes_now"] == payload["db_bytes"]
    assert cache["table_bytes"]["tables"]["filled_payload"]["bytes"] > 0
    assert cache["top_tables"][0]["bytes"] >= cache["top_tables"][-1]["bytes"]


def test_db_doctor_reports_missing_cache_without_sampling_small_db(tmp_path):
    _db_path, db_url = _make_db_diagnostics_fixture(tmp_path)

    result = CliRunner().invoke(app, ["db", "doctor", "--database-url", db_url, "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    cache = payload["table_bytes_cache"]
    assert cache["exists"] is False
    assert cache["status"] == "missing"
    assert cache["suggested_command"] is None
    assert payload["live_store"]["status"] == "disabled"
    assert payload["live_store"]["configured"] is False


def test_db_doctor_reports_configured_live_store_path(tmp_path):
    _db_path, db_url = _make_db_diagnostics_fixture(tmp_path)
    live_path = tmp_path / "live" / "live.db"
    live_url = f"sqlite:///{live_path}"

    result = CliRunner().invoke(
        app,
        ["db", "doctor", "--database-url", db_url, "--live-database-url", live_url, "--json"],
    )

    assert result.exit_code == 0, result.output
    live_store = json.loads(result.output)["live_store"]
    assert live_store["configured"] is True
    assert live_store["status"] == "missing"
    assert live_store["db_path"] == str(live_path)
    assert live_store["db_exists"] is False
    assert live_store["same_db_path_as_archive"] is False
    assert live_store["same_directory_as_archive"] is False


def test_db_doctor_reports_live_archive_outbox_stats(tmp_path):
    from datetime import datetime
    from datetime import timezone

    from sqlalchemy.orm import sessionmaker

    from zerg.database import initialize_live_database
    from zerg.database import make_live_engine
    from zerg.models.live_store import LiveArchiveOutbox

    _db_path, db_url = _make_db_diagnostics_fixture(tmp_path)
    live_path = tmp_path / "live.db"
    live_url = f"sqlite:///{live_path}"
    live_engine = make_live_engine(live_url)
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)
    now = datetime.now(timezone.utc)
    try:
        with LiveSession() as live_db:
            live_db.add_all(
                [
                    LiveArchiveOutbox(
                        idempotency_key="pending-ok",
                        kind="heartbeat_stamp.v1",
                        payload_json="{}",
                        created_at=now,
                    ),
                    LiveArchiveOutbox(
                        idempotency_key="pending-failed",
                        kind="heartbeat_stamp.v1",
                        payload_json="{}",
                        created_at=now,
                        attempts=3,
                        last_error="boom",
                    ),
                    LiveArchiveOutbox(
                        idempotency_key="drained",
                        kind="heartbeat_stamp.v1",
                        payload_json="{}",
                        created_at=now,
                        drained_at=now,
                        attempts=1,
                    ),
                ]
            )
            live_db.commit()

        result = CliRunner().invoke(
            app,
            ["db", "doctor", "--database-url", db_url, "--live-database-url", live_url, "--json"],
        )
    finally:
        live_engine.dispose()

    assert result.exit_code == 0, result.output
    outbox = json.loads(result.output)["live_store"]["live_archive_outbox"]
    assert outbox["checked"] is True
    assert outbox["table_exists"] is True
    assert outbox["pending_count"] == 2
    assert outbox["failed_count"] == 1
    assert outbox["max_attempts"] == 3
    assert outbox["oldest_pending_created_at"] is not None


def test_db_doctor_warns_when_live_store_is_archive_db(tmp_path):
    db_path, db_url = _make_db_diagnostics_fixture(tmp_path)

    result = CliRunner().invoke(
        app,
        ["db", "doctor", "--database-url", db_url, "--live-database-url", db_url, "--json"],
    )

    assert result.exit_code == 0, result.output
    live_store = json.loads(result.output)["live_store"]
    assert live_store["status"] == "ok"
    assert live_store["db_path"] == str(db_path)
    assert live_store["same_db_path_as_archive"] is True
    assert "same_as_archive_db" in live_store["warnings"]


def test_collect_sqlite_store_stats_warning_branches(tmp_path):
    from zerg.services.db_diagnostics import collect_sqlite_store_stats

    db_path, db_url = _make_db_diagnostics_fixture(tmp_path)

    same_directory = collect_sqlite_store_stats(
        f"sqlite:///{tmp_path / 'live.db'}",
        archive_database_url=db_url,
    )
    assert same_directory["status"] == "missing"
    assert same_directory["same_directory_as_archive"] is True
    assert "same_directory_as_archive_db" in same_directory["warnings"]

    same_file = collect_sqlite_store_stats(db_url, archive_database_url=db_url)
    assert same_file["status"] == "ok"
    assert same_file["db_path"] == str(db_path)
    assert same_file["same_db_path_as_archive"] is True
    assert "same_as_archive_db" in same_file["warnings"]

    tmp_path_store = collect_sqlite_store_stats("sqlite:////tmp/longhouse-live-test.db")
    assert tmp_path_store["status"] == "missing"
    assert "tmp_path" in tmp_path_store["warnings"]

    unsupported = collect_sqlite_store_stats("sqlite://")
    assert unsupported["status"] == "unsupported"
    assert "not_file_backed_sqlite" in unsupported["warnings"]


def test_live_store_factories_route_by_test_worker(tmp_path, monkeypatch):
    from zerg import database as database_module

    monkeypatch.setattr(database_module._settings, "testing", True)
    monkeypatch.setattr(database_module._settings, "live_database_url", f"sqlite:///{tmp_path / 'live.db'}")
    monkeypatch.setenv("E2E_DB_DIR", str(tmp_path))
    database_module._live_worker_session_factories.clear()
    database_module._live_worker_write_session_factories.clear()

    token = database_module.set_test_worker_id("alpha")
    try:
        alpha = database_module.get_live_session_factory()
        alpha_write = database_module.get_live_write_session_factory()
    finally:
        database_module.reset_test_worker_id(token)

    token = database_module.set_test_worker_id("beta")
    try:
        beta = database_module.get_live_session_factory()
    finally:
        database_module.reset_test_worker_id(token)

    assert alpha is not None
    assert alpha_write is not None
    assert beta is not None
    assert alpha.kw["bind"].url.database.endswith("live_live_worker_alpha.db")
    assert alpha_write.kw["bind"].url.database.endswith("live_live_worker_alpha.db")
    assert beta.kw["bind"].url.database.endswith("live_live_worker_beta.db")


def test_db_doctor_reports_stale_and_corrupt_cache(tmp_path):
    db_path, db_url = _make_db_diagnostics_fixture(tmp_path)
    cache_path = Path(f"{db_path}.table-bytes.json")
    sample = CliRunner().invoke(app, ["db", "sample-table-bytes", "--database-url", db_url, "--json"])
    assert sample.exit_code == 0, sample.output

    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    payload["completed_at"] = "2020-01-01T00:00:00Z"
    cache_path.write_text(json.dumps(payload), encoding="utf-8")

    stale = CliRunner().invoke(
        app,
        ["db", "doctor", "--database-url", db_url, "--json", "--table-bytes-cache-max-age-seconds", "1"],
    )

    assert stale.exit_code == 0, stale.output
    stale_cache = json.loads(stale.output)["table_bytes_cache"]
    assert stale_cache["status"] == "ok"
    assert stale_cache["fresh"] is False
    assert stale_cache["age_seconds"] > 1

    cache_path.write_text("{", encoding="utf-8")
    corrupt = CliRunner().invoke(app, ["db", "doctor", "--database-url", db_url, "--json"])

    assert corrupt.exit_code == 0, corrupt.output
    corrupt_cache = json.loads(corrupt.output)["table_bytes_cache"]
    assert corrupt_cache["status"] == "corrupt"
    assert corrupt_cache["error"]


def test_table_bytes_cache_rejects_oversized_and_schema_mismatch(tmp_path):
    from zerg.services.db_diagnostics import load_sqlite_table_bytes_cache

    db_path, db_url = _make_db_diagnostics_fixture(tmp_path)
    cache_path = Path(f"{db_path}.table-bytes.json")
    cache_path.write_text(json.dumps({"schema_version": 999}), encoding="utf-8")

    mismatch = load_sqlite_table_bytes_cache(db_url, max_cache_bytes=1024)
    assert mismatch["status"] == "schema_version_unsupported"

    cache_path.write_text("x" * 32, encoding="utf-8")
    oversized = load_sqlite_table_bytes_cache(db_url, max_cache_bytes=8)
    assert oversized["status"] == "cache_too_large"


def test_table_bytes_cache_concurrent_writes_leave_valid_json(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from zerg.services.db_diagnostics import write_sqlite_table_bytes_cache

    cache_path = tmp_path / "doctor.db.table-bytes.json"

    def _write(index: int) -> None:
        write_sqlite_table_bytes_cache(
            {
                "schema_version": 1,
                "status": "ok",
                "writer": index,
                "table_bytes": {"available": True, "error": None, "total_bytes": index, "total_pages": 1, "tables": {}},
            },
            cache_path,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(_write, [1, 2]))

    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert payload["status"] == "ok"
    assert payload["writer"] in {1, 2}


def test_db_sample_table_bytes_timeout_and_unavailable_write_cache(tmp_path, monkeypatch):
    from zerg.services import db_diagnostics

    db_path, db_url = _make_db_diagnostics_fixture(tmp_path)

    def _timeout(*_args, **_kwargs):
        raise db_diagnostics.SQLiteTableBytesTimeout("interrupted")

    monkeypatch.setattr(db_diagnostics, "collect_sqlite_table_bytes_with_deadline", _timeout)
    timeout = CliRunner().invoke(app, ["db", "sample-table-bytes", "--database-url", db_url, "--json"])

    assert timeout.exit_code == 1, timeout.output
    timeout_payload = json.loads(timeout.output)
    assert timeout_payload["status"] == "timeout"
    cached_timeout = json.loads(Path(f"{db_path}.table-bytes.json").read_text(encoding="utf-8"))
    assert cached_timeout["status"] == "timeout"

    def _unavailable(*_args, **_kwargs):
        return {
            "available": False,
            "error": "no such table: dbstat",
            "total_bytes": None,
            "total_pages": None,
            "tables": {},
        }

    monkeypatch.setattr(db_diagnostics, "collect_sqlite_table_bytes_with_deadline", _unavailable)
    unavailable = CliRunner().invoke(app, ["db", "sample-table-bytes", "--database-url", db_url, "--json"])

    assert unavailable.exit_code == 1, unavailable.output
    unavailable_payload = json.loads(unavailable.output)
    assert unavailable_payload["status"] == "unavailable"
    assert "dbstat" in unavailable_payload["error"]


def test_db_sample_table_bytes_rejects_bad_timeout_and_missing_db(tmp_path):
    db_path = tmp_path / "missing.db"
    db_url = f"sqlite:///{db_path}"

    bad_timeout = CliRunner().invoke(
        app,
        ["db", "sample-table-bytes", "--database-url", db_url, "--json", "--timeout-seconds", "0"],
    )
    assert bad_timeout.exit_code == 2

    missing = CliRunner().invoke(app, ["db", "sample-table-bytes", "--database-url", db_url, "--json"])
    assert missing.exit_code == 1, missing.output
    payload = json.loads(missing.output)
    assert payload["status"] == "error"
    assert payload["error"] == "database file not found"
    assert Path(f"{db_path}.table-bytes.json").exists()


def test_collect_sqlite_table_bytes_gracefully_handles_unavailable_dbstat():
    from zerg.services.db_diagnostics import collect_sqlite_table_bytes

    class BrokenDbstatConnection:
        def execute(self, *_args, **_kwargs):
            raise RuntimeError("no such table: dbstat")

    table_bytes = collect_sqlite_table_bytes(BrokenDbstatConnection())  # type: ignore[arg-type]

    assert table_bytes["available"] is False
    assert table_bytes["tables"] == {}
    assert "dbstat" in table_bytes["error"]


def test_db_doctor_identity_counts_are_separately_opted_in(tmp_path):
    _db_path, db_url = _make_db_diagnostics_fixture(tmp_path)

    result = CliRunner().invoke(
        app,
        ["db", "doctor", "--database-url", db_url, "--json", "--deep", "--identity-counts"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["deep_counts"]["identity_counts_skipped"] is False
    assert payload["deep_counts"]["events_thread_id_null"] == 1
    assert payload["deep_counts"]["source_lines_thread_id_null"] == 1
    assert payload["deep_counts"]["session_observations_thread_id_null"] == 1


def test_db_doctor_without_deep_skips_counts(tmp_path):
    _db_path, db_url = _make_db_diagnostics_fixture(tmp_path)

    result = CliRunner().invoke(app, ["db", "doctor", "--database-url", db_url, "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["deep_counts_skipped"] is True
    assert payload["deep_counts"] is None
    assert payload["table_bytes_skipped"] is True
    assert payload["table_bytes"] is None


def test_db_optimize_json_runs_pragma(tmp_path):
    _db_path, db_url = _make_db_diagnostics_fixture(tmp_path)

    result = CliRunner().invoke(app, ["db", "optimize", "--database-url", db_url, "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "ok"
    assert payload["pragma"] == "PRAGMA optimize"
    assert payload["elapsed_ms"] >= 0


def test_db_optimize_json_reports_failures(monkeypatch):
    import zerg.cli.main as main_cli

    class BadBegin:
        def __enter__(self):
            raise OSError("disk I/O error")

        def __exit__(self, exc_type, exc, tb):
            return False

    class BadEngine:
        def begin(self):
            return BadBegin()

    monkeypatch.setattr(main_cli, "_resolve_db_engine", lambda _database_url: (BadEngine(), "sqlite:///bad.db"))

    result = CliRunner().invoke(app, ["db", "optimize", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "error"
    assert payload["pragma"] == "PRAGMA optimize"
    assert "disk I/O error" in payload["error"]


def test_migrate_can_skip_schema_convergence(tmp_path):
    db_path = tmp_path / "migrate-plan.db"
    db_url = f"sqlite:///{db_path}"

    result = CliRunner().invoke(app, ["migrate", "--database-url", db_url, "--no-schema-converge", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_converged"] is False
    assert payload["pending_before"] == []
    with sqlite3.connect(db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    assert "migration_runs" in tables
    assert "sessions" not in tables


# Silence unused-path parameter if any pytest collector insists.
_ = Path
