from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text as sa_text

from zerg.config import get_settings
from zerg.data_plane import create_archive_store
from zerg.data_plane import create_derived_store
from zerg.data_plane import create_hot_store
from zerg.data_plane import get_data_plane_paths
from zerg.data_plane import initialize_derived_database
from zerg.data_plane import initialize_hot_database


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _clear_data_plane_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "LONGHOUSE_DATA_ROOT",
        "LONGHOUSE_HOT_DATABASE_URL",
        "LONGHOUSE_DERIVED_DATABASE_URL",
        "LONGHOUSE_ARCHIVE_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)


def test_data_plane_paths_preserve_active_database_url_by_default(tmp_path, monkeypatch):
    _clear_data_plane_env(monkeypatch)
    db_url = _sqlite_url(tmp_path / "longhouse.db")
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("DATABASE_URL", db_url)

    settings = get_settings()
    paths = get_data_plane_paths(settings)

    assert paths.hot_database_url == db_url
    assert paths.derived_database_url == _sqlite_url(tmp_path / "derived.db")
    assert paths.archive_root == tmp_path / "archive"


def test_data_plane_paths_support_hosted_absolute_sqlite_url(tmp_path, monkeypatch):
    _clear_data_plane_env(monkeypatch)
    db_path = tmp_path / "hosted" / "longhouse.db"
    db_url = _sqlite_url(db_path)
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("DATABASE_URL", db_url)

    paths = get_data_plane_paths(get_settings())

    assert paths.hot_database_url == db_url
    assert paths.derived_database_url == _sqlite_url(db_path.parent / "derived.db")
    assert paths.archive_root == db_path.parent / "archive"


def test_data_plane_paths_strip_quoted_database_url(tmp_path, monkeypatch):
    _clear_data_plane_env(monkeypatch)
    db_path = tmp_path / "quoted" / "longhouse.db"
    db_url = _sqlite_url(db_path)
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("DATABASE_URL", f'"{db_url}"')

    paths = get_data_plane_paths(get_settings())

    assert paths.hot_database_url == db_url
    assert paths.derived_database_url == _sqlite_url(db_path.parent / "derived.db")
    assert paths.archive_root == db_path.parent / "archive"


def test_data_plane_paths_support_driver_qualified_sqlite_url(tmp_path, monkeypatch):
    _clear_data_plane_env(monkeypatch)
    db_path = tmp_path / "driver" / "longhouse.db"
    db_url = f"sqlite+pysqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("DATABASE_URL", db_url)

    paths = get_data_plane_paths(get_settings())

    assert paths.hot_database_url == db_url
    assert paths.derived_database_url == _sqlite_url(db_path.parent / "derived.db")
    assert paths.archive_root == db_path.parent / "archive"


def test_data_plane_paths_document_relative_database_url_behavior(monkeypatch):
    _clear_data_plane_env(monkeypatch)
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///longhouse.db")

    paths = get_data_plane_paths(get_settings())

    assert paths.hot_database_url == "sqlite:///longhouse.db"
    assert paths.derived_database_url == "sqlite:///derived.db"
    assert paths.archive_root == Path("archive")


def test_data_plane_root_opts_into_hot_derived_archive_layout(tmp_path, monkeypatch):
    _clear_data_plane_env(monkeypatch)
    root = tmp_path / "longhouse-data"
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("DATABASE_URL", _sqlite_url(tmp_path / "legacy.db"))
    monkeypatch.setenv("LONGHOUSE_DATA_ROOT", str(root))

    settings = get_settings()
    paths = get_data_plane_paths(settings)

    assert settings.longhouse_data_root == str(root)
    assert paths.hot_database_url == _sqlite_url(root / "hot.db")
    assert paths.derived_database_url == _sqlite_url(root / "derived.db")
    assert paths.archive_root == root / "archive"


def test_data_plane_explicit_paths_override_layout_defaults(tmp_path, monkeypatch):
    _clear_data_plane_env(monkeypatch)
    root = tmp_path / "longhouse-data"
    hot_url = _sqlite_url(tmp_path / "custom-hot.db")
    derived_url = _sqlite_url(tmp_path / "custom-derived.db")
    archive_root = tmp_path / "custom-archive"
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("DATABASE_URL", _sqlite_url(tmp_path / "legacy.db"))
    monkeypatch.setenv("LONGHOUSE_DATA_ROOT", str(root))
    monkeypatch.setenv("LONGHOUSE_HOT_DATABASE_URL", hot_url)
    monkeypatch.setenv("LONGHOUSE_DERIVED_DATABASE_URL", derived_url)
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_ROOT", str(archive_root))

    settings = get_settings()
    paths = get_data_plane_paths(settings)
    archive_store = create_archive_store(settings)

    assert paths.hot_database_url == hot_url
    assert paths.derived_database_url == derived_url
    assert paths.archive_root == archive_root
    assert archive_store.root == archive_root


def test_empty_hot_and_derived_store_migrations_are_independent(tmp_path, monkeypatch):
    settings = _configure_store_env(tmp_path, monkeypatch)
    hot = create_hot_store(settings)
    derived = create_derived_store(settings)
    try:
        initialize_hot_database(hot.engine)
        initialize_derived_database(derived.engine)

        assert _meta_value(hot, "role") == "hot"
        assert _meta_value(derived, "role") == "derived"
        assert _migration_exists(hot, "hot:000_empty_store")
        assert _migration_exists(derived, "derived:000_empty_store")
    finally:
        hot.dispose()
        derived.dispose()


def test_derived_store_initialization_creates_event_search_tables(tmp_path, monkeypatch):
    settings = _configure_store_env(tmp_path, monkeypatch)
    derived = create_derived_store(settings)
    try:
        initialize_derived_database(derived.engine)

        with derived.session_factory() as db:
            tables = {
                row[0]
                for row in db.execute(
                    sa_text("SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')")
                ).fetchall()
            }

        assert "derived_events" in tables
        assert "derived_events_fts" in tables
    finally:
        derived.dispose()


def test_derived_store_initialization_rebuilds_legacy_fts_shape(tmp_path, monkeypatch):
    settings = _configure_store_env(tmp_path, monkeypatch)
    derived = create_derived_store(settings)
    try:
        with derived.engine.begin() as conn:
            conn.execute(
                sa_text(
                    """
                    CREATE VIRTUAL TABLE derived_events_fts
                    USING fts5(content_text, tool_output_text, tool_name, role, session_id)
                    """
                )
            )

        initialize_derived_database(derived.engine)

        with derived.session_factory() as db:
            columns = {row[1] for row in db.execute(sa_text("PRAGMA table_info(derived_events_fts)")).fetchall()}
            fts_sql = db.execute(sa_text("SELECT sql FROM sqlite_master WHERE name = 'derived_events_fts'")).scalar()

        assert "parser_revision" in columns
        assert "parser_revision UNINDEXED" in fts_sql
    finally:
        derived.dispose()


def test_empty_store_initialization_is_idempotent(tmp_path, monkeypatch):
    settings = _configure_store_env(tmp_path, monkeypatch)
    hot = create_hot_store(settings)
    try:
        initialize_hot_database(hot.engine)
        initialize_hot_database(hot.engine)

        with hot.session_factory() as db:
            count = db.execute(sa_text("SELECT COUNT(*) FROM data_plane_migration_runs")).scalar()

        assert count == 1
        assert _meta_value(hot, "role") == "hot"
    finally:
        hot.dispose()


def test_settings_resolution_does_not_touch_filesystem(tmp_path, monkeypatch):
    _clear_data_plane_env(monkeypatch)
    db_path = tmp_path / "lazy" / "longhouse.db"
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("DATABASE_URL", _sqlite_url(db_path))

    paths = get_data_plane_paths(get_settings())

    assert paths.derived_database_url == _sqlite_url(db_path.parent / "derived.db")
    assert not db_path.exists()
    assert not (db_path.parent / "derived.db").exists()
    assert not paths.archive_root.exists()


@pytest.mark.asyncio
async def test_hot_write_succeeds_when_derived_db_is_missing(tmp_path, monkeypatch):
    settings = _configure_store_env(tmp_path, monkeypatch)
    hot = create_hot_store(settings)
    derived = create_derived_store(settings)
    try:
        initialize_hot_database(hot.engine)
        initialize_derived_database(derived.engine)
        derived.dispose()
        (tmp_path / "derived.db").unlink()

        await hot.write_serializer.execute(
            lambda db: db.execute(sa_text("INSERT INTO data_plane_store_meta (key, value) VALUES ('hot_write', 'ok')")),
            label="hot-data-plane-test",
        )

        assert _meta_value(hot, "hot_write") == "ok"
        assert not (tmp_path / "derived.db").exists()
    finally:
        hot.dispose()


@pytest.mark.asyncio
async def test_hot_write_succeeds_while_derived_db_is_locked(tmp_path, monkeypatch):
    settings = _configure_store_env(tmp_path, monkeypatch)
    hot = create_hot_store(settings)
    derived = create_derived_store(settings)
    derived_lock = None
    try:
        initialize_hot_database(hot.engine)
        initialize_derived_database(derived.engine)
        derived_lock = derived.engine.connect()
        derived_lock.exec_driver_sql("BEGIN EXCLUSIVE")

        await hot.write_serializer.execute(
            lambda db: db.execute(
                sa_text("INSERT INTO data_plane_store_meta (key, value) VALUES ('derived_locked', 'hot_ok')")
            ),
            label="hot-data-plane-test",
            timeout_seconds=1,
        )

        assert _meta_value(hot, "derived_locked") == "hot_ok"
    finally:
        if derived_lock is not None:
            derived_lock.exec_driver_sql("ROLLBACK")
            derived_lock.close()
        hot.dispose()
        derived.dispose()


def _configure_store_env(tmp_path, monkeypatch):
    _clear_data_plane_env(monkeypatch)
    hot_url = _sqlite_url(tmp_path / "hot.db")
    derived_url = _sqlite_url(tmp_path / "derived.db")
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("DATABASE_URL", hot_url)
    monkeypatch.setenv("LONGHOUSE_HOT_DATABASE_URL", hot_url)
    monkeypatch.setenv("LONGHOUSE_DERIVED_DATABASE_URL", derived_url)
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_ROOT", str(tmp_path / "archive"))
    return get_settings()


def _meta_value(store, key: str) -> str | None:
    with store.session_factory() as db:
        row = db.execute(
            sa_text("SELECT value FROM data_plane_store_meta WHERE key = :key"),
            {"key": key},
        ).fetchone()
    return None if row is None else str(row[0])


def _migration_exists(store, migration_name: str) -> bool:
    with store.session_factory() as db:
        row = db.execute(
            sa_text("SELECT 1 FROM data_plane_migration_runs WHERE migration_name = :name"),
            {"name": migration_name},
        ).fetchone()
    return row is not None
