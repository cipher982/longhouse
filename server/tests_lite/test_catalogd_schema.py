"""Focused ownership and migration tests for the isolated catalog schema."""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC
from datetime import datetime

import pytest
from sqlalchemy import event
from sqlalchemy import text
from sqlalchemy.orm import Session

import zerg.catalogd.schema as catalog_schema
from zerg.catalogd.schema import CATALOG_SCHEMA_VERSION
from zerg.catalogd.schema import CatalogSchemaMismatchError
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveTimelineCard


def test_greenfield_catalog_has_pragmas_live_schema_and_identity(tmp_path):
    engine = create_catalog_engine(tmp_path / "longhouse-live.db", busy_timeout_ms=1_234)

    metadata = initialize_catalog_schema(engine)

    assert metadata.schema_version == CATALOG_SCHEMA_VERSION
    assert metadata.commit_seq == 0
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
        assert connection.exec_driver_sql("PRAGMA synchronous").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 1_234
        assert connection.exec_driver_sql("PRAGMA wal_autocheckpoint").scalar_one() == 0
        assert connection.exec_driver_sql("PRAGMA user_version").scalar_one() == CATALOG_SCHEMA_VERSION
        tables = {
            row[0] for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").all()
        }
    assert {
        "catalog_meta",
        "legacy_migration_runs",
        "legacy_migration_sessions",
        "live_session_catalog",
        "live_runtime_state",
        "fact_heads",
        "fact_receipts",
        "fact_conflicts",
    }.issubset(tables)


def test_initialize_is_idempotent_and_preserves_catalog_identity(tmp_path):
    database = tmp_path / "longhouse-live.db"
    first_engine = create_catalog_engine(database)
    first = initialize_catalog_schema(first_engine)
    first_engine.dispose()

    second_engine = create_catalog_engine(database)
    second = initialize_catalog_schema(second_engine)

    assert second == first
    with second_engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM catalog_meta")).scalar_one() == 1


def test_feature_marker_refuses_to_heal_an_incomplete_reducer_schema(tmp_path):
    engine = create_catalog_engine(tmp_path / "longhouse-live.db")
    initialize_catalog_schema(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE fact_receipts")

    with pytest.raises(CatalogSchemaMismatchError, match="fact reducer schema"):
        initialize_catalog_schema(engine)


def test_existing_v2_adopts_reducer_tables_without_advancing_global_version(tmp_path):
    engine = create_catalog_engine(tmp_path / "longhouse-live.db")
    initialize_catalog_schema(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE fact_parity_deltas")
        connection.exec_driver_sql("DROP TABLE fact_conflicts")
        connection.exec_driver_sql("DROP TABLE fact_receipts")
        connection.exec_driver_sql("DROP TABLE fact_heads")
        connection.exec_driver_sql("ALTER TABLE catalog_meta DROP COLUMN fact_reducer_generation")

    metadata = initialize_catalog_schema(engine)

    assert metadata.schema_version == 2
    with engine.connect() as connection:
        tables = {row[0] for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"fact_heads", "fact_receipts", "fact_conflicts", "fact_parity_deltas"}.issubset(tables)
        assert connection.exec_driver_sql(
            "SELECT fact_reducer_generation FROM catalog_meta WHERE singleton = 1"
        ).scalar_one()


def test_interrupted_reducer_adoption_rolls_back_without_partial_marker(tmp_path):
    engine = create_catalog_engine(tmp_path / "longhouse-live.db")
    initialize_catalog_schema(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE fact_parity_deltas")
        connection.exec_driver_sql("DROP TABLE fact_conflicts")
        connection.exec_driver_sql("DROP TABLE fact_receipts")
        connection.exec_driver_sql("DROP TABLE fact_heads")
        connection.exec_driver_sql("ALTER TABLE catalog_meta DROP COLUMN fact_reducer_generation")

    def fail_mid_adoption(_connection, _cursor, statement, _parameters, _context, _executemany):
        if "CREATE TABLE" in statement and "fact_receipts" in statement:
            raise RuntimeError("simulated adoption interruption")

    event.listen(engine, "before_cursor_execute", fail_mid_adoption)
    try:
        with pytest.raises(RuntimeError, match="simulated adoption interruption"):
            initialize_catalog_schema(engine)
    finally:
        event.remove(engine, "before_cursor_execute", fail_mid_adoption)

    with engine.connect() as connection:
        tables = set(connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").scalars())
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(catalog_meta)")}
    assert not ({"fact_heads", "fact_receipts", "fact_conflicts", "fact_parity_deltas"} & tables)
    assert "fact_reducer_generation" not in columns

    metadata = initialize_catalog_schema(engine)
    assert metadata.schema_version == 2


def test_existing_reducer_generation_atomically_adopts_parity_diagnostics(tmp_path):
    engine = create_catalog_engine(tmp_path / "longhouse-live.db")
    initialize_catalog_schema(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE fact_parity_deltas")
        connection.exec_driver_sql("DROP INDEX ix_fact_heads_session_family_recent")
        connection.exec_driver_sql("ALTER TABLE fact_heads DROP COLUMN session_id")
        connection.exec_driver_sql(
            "UPDATE catalog_meta SET fact_reducer_generation = ? WHERE singleton = 1",
            (catalog_schema._FACT_REDUCER_V1_GENERATION,),
        )

    def fail_parity_adoption(_connection, _cursor, statement, _parameters, _context, _executemany):
        if "CREATE TABLE" in statement and "fact_parity_deltas" in statement:
            raise RuntimeError("simulated parity adoption interruption")

    event.listen(engine, "before_cursor_execute", fail_parity_adoption)
    try:
        with pytest.raises(RuntimeError, match="simulated parity adoption interruption"):
            initialize_catalog_schema(engine)
    finally:
        event.remove(engine, "before_cursor_execute", fail_parity_adoption)

    with engine.connect() as connection:
        tables = set(connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").scalars())
        marker = connection.exec_driver_sql(
            "SELECT fact_reducer_generation FROM catalog_meta WHERE singleton = 1"
        ).scalar_one()
    assert "fact_parity_deltas" not in tables
    assert marker == catalog_schema._FACT_REDUCER_V1_GENERATION

    initialize_catalog_schema(engine)
    with engine.connect() as connection:
        tables = set(connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").scalars())
        marker = connection.exec_driver_sql(
            "SELECT fact_reducer_generation FROM catalog_meta WHERE singleton = 1"
        ).scalar_one()
    assert "fact_parity_deltas" in tables
    assert marker == catalog_schema.FACT_REDUCER_GENERATION


def test_reducer_schema_validation_rejects_matching_names_without_constraints(tmp_path):
    engine = create_catalog_engine(tmp_path / "longhouse-live.db")
    initialize_catalog_schema(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE fact_heads")
        connection.exec_driver_sql(
            "CREATE TABLE fact_heads ("
            "family TEXT, subject_key TEXT, source TEXT, source_epoch TEXT, ordering_mode TEXT, "
            "source_seq INTEGER, evidence_hash TEXT, observed_at TEXT, valid_until TEXT, value_json TEXT, "
            "raw_locator TEXT, updated_commit_seq INTEGER, received_at TEXT)"
        )
        connection.exec_driver_sql("CREATE INDEX ix_fact_heads_subject ON fact_heads(family, subject_key)")
        connection.exec_driver_sql("CREATE INDEX ix_fact_heads_commit ON fact_heads(updated_commit_seq)")

    with pytest.raises(CatalogSchemaMismatchError, match="primary-key:fact_heads"):
        initialize_catalog_schema(engine)


def test_reducer_schema_validation_rejects_wrong_server_default(tmp_path):
    engine = create_catalog_engine(tmp_path / "longhouse-live.db")
    initialize_catalog_schema(engine)
    with engine.begin() as connection:
        create_sql = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'fact_heads'"
        ).scalar_one()
        wrong_default_sql = create_sql.replace("DEFAULT ''", "DEFAULT 'wrong'", 1)
        assert wrong_default_sql != create_sql
        connection.exec_driver_sql("DROP TABLE fact_heads")
        connection.exec_driver_sql(wrong_default_sql)
        connection.exec_driver_sql("CREATE INDEX ix_fact_heads_subject ON fact_heads(family, subject_key)")
        connection.exec_driver_sql("CREATE INDEX ix_fact_heads_commit ON fact_heads(updated_commit_seq)")

    with pytest.raises(CatalogSchemaMismatchError, match="default:fact_heads.source_epoch"):
        initialize_catalog_schema(engine)


def test_additive_reducer_storage_remains_readable_to_previous_v2_shape(tmp_path):
    engine = create_catalog_engine(tmp_path / "longhouse-live.db")
    initialize_catalog_schema(engine)

    with engine.connect() as connection:
        row = connection.exec_driver_sql(
            "SELECT singleton, catalog_id, schema_version, commit_seq, created_at, updated_at FROM catalog_meta"
        ).one()
        tables = set(connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").scalars())

    assert row.schema_version == 2
    assert row.singleton == 1
    assert {"fact_heads", "fact_receipts", "fact_conflicts"}.issubset(tables)


def test_reducer_generation_is_stable_across_processes():
    command = "from zerg.catalogd.schema import FACT_REDUCER_GENERATION; print(FACT_REDUCER_GENERATION)"
    values = []
    for _ in range(2):
        completed = subprocess.run(
            [sys.executable, "-c", command],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        values.append(completed.stdout.strip())

    assert values[0] == values[1] == catalog_schema.FACT_REDUCER_GENERATION


def test_concurrent_reducer_adoption_serializes_without_partial_schema(tmp_path):
    database = tmp_path / "longhouse-live.db"
    engine = create_catalog_engine(database)
    initialize_catalog_schema(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE fact_parity_deltas")
        connection.exec_driver_sql("DROP TABLE fact_conflicts")
        connection.exec_driver_sql("DROP TABLE fact_receipts")
        connection.exec_driver_sql("DROP TABLE fact_heads")
        connection.exec_driver_sql("ALTER TABLE catalog_meta DROP COLUMN fact_reducer_generation")
    engine.dispose()

    def initialize_once():
        worker_engine = create_catalog_engine(database)
        try:
            return initialize_catalog_schema(worker_engine)
        finally:
            worker_engine.dispose()

    with ThreadPoolExecutor(max_workers=2) as executor:
        metadata = list(executor.map(lambda _index: initialize_once(), range(2)))

    assert [item.schema_version for item in metadata] == [CATALOG_SCHEMA_VERSION] * 2
    final_engine = create_catalog_engine(database)
    initialize_catalog_schema(final_engine)
    final_engine.dispose()


def test_existing_live_database_gets_safe_additive_columns(tmp_path):
    database = tmp_path / "longhouse-live.db"
    engine = create_catalog_engine(database)
    initialize_catalog_schema(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE live_session_catalog DROP COLUMN device_name")

    # Model metadata is process-global; removing a physical nullable column is
    # enough to exercise the same additive path used by a newly added model
    # column without mutating that shared metadata.
    restored = initialize_catalog_schema(engine)

    assert restored.schema_version == CATALOG_SCHEMA_VERSION
    with engine.connect() as connection:
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(live_session_catalog)").all()}
    assert "device_name" in columns


@pytest.mark.parametrize(
    ("user_version", "metadata_version"),
    [
        (0, CATALOG_SCHEMA_VERSION),
        (CATALOG_SCHEMA_VERSION - 1, CATALOG_SCHEMA_VERSION),
        (CATALOG_SCHEMA_VERSION, CATALOG_SCHEMA_VERSION - 1),
    ],
)
def test_metadata_and_user_version_mismatch_is_typed(tmp_path, user_version, metadata_version):
    engine = create_catalog_engine(tmp_path / "longhouse-live.db")
    initialize_catalog_schema(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(f"PRAGMA user_version={user_version}")
        if metadata_version != CATALOG_SCHEMA_VERSION:
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            connection.exec_driver_sql(
                "UPDATE catalog_meta SET schema_version = ? WHERE singleton = 1",
                (metadata_version,),
            )

    with pytest.raises(CatalogSchemaMismatchError):
        initialize_catalog_schema(engine)


def test_version_bump_requires_an_explicit_migration(tmp_path, monkeypatch):
    engine = create_catalog_engine(tmp_path / "longhouse-live.db")
    initialize_catalog_schema(engine)
    next_version = CATALOG_SCHEMA_VERSION + 1
    monkeypatch.setattr(catalog_schema, "CATALOG_SCHEMA_VERSION", next_version)

    with pytest.raises(
        catalog_schema.CatalogSchemaMigrationError,
        match=f"missing catalog schema migration {CATALOG_SCHEMA_VERSION}->{next_version}",
    ):
        initialize_catalog_schema(engine)


def test_registered_version_migration_updates_both_markers_atomically(tmp_path, monkeypatch):
    engine = create_catalog_engine(tmp_path / "longhouse-live.db")
    initialize_catalog_schema(engine)

    def migrate(connection):
        connection.exec_driver_sql("CREATE TABLE catalog_v2_proof (value INTEGER NOT NULL)")

    next_version = CATALOG_SCHEMA_VERSION + 1
    monkeypatch.setattr(catalog_schema, "CATALOG_SCHEMA_VERSION", next_version)
    monkeypatch.setattr(catalog_schema, "CATALOG_SCHEMA_MIGRATIONS", {CATALOG_SCHEMA_VERSION: migrate})

    metadata = initialize_catalog_schema(engine)

    assert metadata.schema_version == next_version
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA user_version").scalar_one() == next_version
        assert (
            connection.exec_driver_sql(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='catalog_v2_proof'"
            ).scalar_one()
            == 1
        )


def test_empty_human_shell_backfill_hides_live_projection(tmp_path):
    engine = create_catalog_engine(tmp_path / "longhouse-live.db")
    initialize_catalog_schema(engine)
    now = datetime.now(UTC)
    with Session(engine) as db:
        session = LiveSessionCatalog(
            session_id="empty-shell",
            provider="codex",
            environment="production",
            project="longhouse",
            started_at=now,
            last_activity_at=now,
            launch_actor="human_ui",
            launch_surface="ios",
            created_at=now,
            updated_at=now,
        )
        card = LiveTimelineCard(
            session_id="empty-shell",
            provider="codex",
            environment="production",
            project="longhouse",
            started_at=now,
            last_activity_at=now,
            archive_state="pending",
            launch_actor="human_ui",
            launch_surface="ios",
            derived_state="idle",
            parser_revision="test",
            updated_at=now,
        )
        db.add_all([session, card])
        db.commit()

    with engine.begin() as connection:
        catalog_schema._hide_empty_human_launch_shells(connection)

    with Session(engine) as db:
        assert db.get(LiveSessionCatalog, "empty-shell").hidden_from_default_timeline == 1
        assert db.get(LiveTimelineCard, "empty-shell").hidden_from_default_timeline == 1


def test_importing_schema_does_not_import_runtime_database_module():
    environment = os.environ.copy()
    command = (
        "import sys; import zerg.catalogd.schema; "
        "assert 'zerg.database' not in sys.modules, sorted(k for k in sys.modules if k.startswith('zerg.database'))"
    )

    completed = subprocess.run(
        [sys.executable, "-c", command],
        cwd=os.path.dirname(os.path.dirname(__file__)),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_catalogd_import_graph_excludes_web_and_archive_runtime():
    command = """
import sys
import zerg.catalogd.server
for forbidden in ("zerg.database", "fastapi"):
    assert forbidden not in sys.modules, (forbidden, sorted(k for k in sys.modules if k.startswith(forbidden)))
assert not any(name.startswith("zerg.routers") for name in sys.modules)
"""
    completed = subprocess.run(
        [sys.executable, "-c", command],
        cwd=os.path.dirname(os.path.dirname(__file__)),
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_lazy_model_package_preserves_representative_legacy_imports():
    command = """
from zerg.models import AgentSession, GUID, NotificationEvent, Runner, User
assert AgentSession.__module__ == "zerg.models.agents"
assert GUID.__module__ == "zerg.models.types"
assert NotificationEvent.__module__ == "zerg.models.notification_event"
assert Runner.__module__ == "zerg.models.models"
assert User.__module__ == "zerg.models.user"
"""
    completed = subprocess.run(
        [sys.executable, "-c", command],
        cwd=os.path.dirname(os.path.dirname(__file__)),
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
