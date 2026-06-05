"""Data-plane store wiring for the hot/archive/derived split.

This module is Phase 3 scaffolding only. Existing product routes still use the
legacy default database until later cutover phases explicitly move them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy import Engine
from sqlalchemy import text
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import sessionmaker

from zerg.config import Settings
from zerg.config import get_settings
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.database import make_write_engine
from zerg.services.archive_store import FilesystemArchiveStore
from zerg.services.write_serializer import WriteSerializer

StoreRole = Literal["hot", "derived"]
DATA_PLANE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class DataPlanePaths:
    hot_database_url: str
    derived_database_url: str
    archive_root: Path


@dataclass
class DataPlaneStore:
    role: StoreRole
    database_url: str
    engine: Engine
    session_factory: sessionmaker
    write_engine: Engine
    write_session_factory: sessionmaker
    write_serializer: WriteSerializer

    def dispose(self) -> None:
        """Dispose SQLAlchemy engines owned by this store handle."""
        self.engine.dispose()
        if self.write_engine is not self.engine:
            self.write_engine.dispose()


def get_data_plane_paths(settings: Settings | None = None) -> DataPlanePaths:
    settings = settings or get_settings()
    return DataPlanePaths(
        hot_database_url=settings.hot_database_url,
        derived_database_url=settings.derived_database_url,
        archive_root=Path(settings.archive_root),
    )


def create_hot_store(settings: Settings | None = None) -> DataPlaneStore:
    paths = get_data_plane_paths(settings)
    return _create_store("hot", paths.hot_database_url)


def create_derived_store(settings: Settings | None = None) -> DataPlaneStore:
    paths = get_data_plane_paths(settings)
    return _create_store("derived", paths.derived_database_url)


def create_archive_store(settings: Settings | None = None) -> FilesystemArchiveStore:
    paths = get_data_plane_paths(settings)
    return FilesystemArchiveStore(paths.archive_root)


def initialize_hot_database(engine: Engine) -> None:
    initialize_data_plane_database(engine, role="hot")


def initialize_derived_database(engine: Engine) -> None:
    initialize_data_plane_database(engine, role="derived")


def initialize_data_plane_database(engine: Engine, *, role: StoreRole) -> None:
    """Initialize an empty hot/derived store with a tiny migration ledger."""

    if engine.dialect.name != "sqlite":
        raise ValueError("Longhouse data-plane stores are SQLite-only")

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS data_plane_store_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS data_plane_migration_runs (
                    migration_name TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    finished_at DATETIME,
                    details TEXT
                )
                """
            )
        )
        _upsert_store_meta(conn, "role", role)
        _upsert_store_meta(conn, "schema_version", str(DATA_PLANE_SCHEMA_VERSION))
        conn.execute(
            text(
                """
                INSERT INTO data_plane_migration_runs (
                    migration_name,
                    status,
                    started_at,
                    finished_at,
                    details
                )
                VALUES (
                    :migration_name,
                    'succeeded',
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP,
                    :details
                )
                ON CONFLICT(migration_name) DO UPDATE SET
                    status = excluded.status,
                    finished_at = excluded.finished_at,
                    details = excluded.details
                """
            ),
            {
                "migration_name": f"{role}:000_empty_store",
                "details": "empty store skeleton initialized",
            },
        )


def _create_store(role: StoreRole, database_url: str) -> DataPlaneStore:
    _ensure_sqlite_parent(database_url)
    engine = make_engine(database_url)
    session_factory = make_sessionmaker(engine)
    write_engine = make_write_engine(database_url)
    write_session_factory = make_sessionmaker(write_engine)
    write_serializer = WriteSerializer()
    write_serializer.configure(write_session_factory)
    return DataPlaneStore(
        role=role,
        database_url=database_url,
        engine=engine,
        session_factory=session_factory,
        write_engine=write_engine,
        write_session_factory=write_session_factory,
        write_serializer=write_serializer,
    )


def _ensure_sqlite_parent(database_url: str) -> None:
    parsed = make_url(database_url)
    if not parsed.drivername.startswith("sqlite"):
        raise ValueError("Longhouse data-plane stores are SQLite-only")
    db_path = parsed.database
    if not db_path or db_path == ":memory:":
        return
    Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)


def _upsert_store_meta(conn, key: str, value: str) -> None:
    conn.execute(
        text(
            """
            INSERT INTO data_plane_store_meta (key, value, updated_at)
            VALUES (:key, :value, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """
        ),
        {"key": key, "value": value},
    )
