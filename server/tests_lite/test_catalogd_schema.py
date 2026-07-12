"""Focused ownership and migration tests for the isolated catalog schema."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from sqlalchemy import text

import zerg.catalogd.schema as catalog_schema
from zerg.catalogd.schema import CATALOG_SCHEMA_VERSION
from zerg.catalogd.schema import CatalogSchemaMismatchError
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema


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
    assert {"catalog_meta", "live_session_catalog", "live_runtime_state"}.issubset(tables)


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
    [(0, 1), (2, 1), (1, 2)],
)
def test_metadata_and_user_version_mismatch_is_typed(tmp_path, user_version, metadata_version):
    engine = create_catalog_engine(tmp_path / "longhouse-live.db")
    initialize_catalog_schema(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(f"PRAGMA user_version={user_version}")
        if metadata_version != 1:
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
    monkeypatch.setattr(catalog_schema, "CATALOG_SCHEMA_VERSION", 2)

    with pytest.raises(catalog_schema.CatalogSchemaMigrationError, match="missing catalog schema migration 1->2"):
        initialize_catalog_schema(engine)


def test_registered_version_migration_updates_both_markers_atomically(tmp_path, monkeypatch):
    engine = create_catalog_engine(tmp_path / "longhouse-live.db")
    initialize_catalog_schema(engine)

    def migrate(connection):
        connection.exec_driver_sql("CREATE TABLE catalog_v2_proof (value INTEGER NOT NULL)")

    monkeypatch.setattr(catalog_schema, "CATALOG_SCHEMA_VERSION", 2)
    monkeypatch.setattr(catalog_schema, "CATALOG_SCHEMA_MIGRATIONS", {1: migrate})

    metadata = initialize_catalog_schema(engine)

    assert metadata.schema_version == 2
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA user_version").scalar_one() == 2
        assert connection.exec_driver_sql(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='catalog_v2_proof'"
        ).scalar_one() == 1


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
for forbidden in ("zerg.database", "fastapi", "zerg.services.archive_worker"):
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
