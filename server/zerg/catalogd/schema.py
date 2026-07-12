"""Schema ownership for the isolated catalog process.

This module deliberately does not import :mod:`zerg.database`.  ``catalogd``
is the only process allowed to open the live catalog once the v2 cutover is
complete, so its engine configuration and additive startup migration live at
this boundary rather than inheriting the Runtime Host's database globals.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path

from sqlalchemy import CheckConstraint
from sqlalchemy import Column
from sqlalchemy import Connection
from sqlalchemy import Engine
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import Table
from sqlalchemy import Text
from sqlalchemy import create_engine
from sqlalchemy import event
from sqlalchemy import inspect
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateColumn

from zerg.catalogd.models import CatalogBase
from zerg.models.live_store import LiveBase

CATALOG_SCHEMA_VERSION = 1
DEFAULT_BUSY_TIMEOUT_MS = 5_000


class CatalogSchemaError(RuntimeError):
    """Base class for catalog schema startup failures."""


class CatalogSchemaMismatchError(CatalogSchemaError):
    """The durable schema markers disagree or are not readable by this build."""


class CatalogSchemaMigrationError(CatalogSchemaError):
    """A model change cannot be applied as a safe additive migration."""


@dataclass(frozen=True, slots=True)
class CatalogMeta:
    """The singleton durable identity and ordering state for one catalog."""

    catalog_id: uuid.UUID
    schema_version: int
    commit_seq: int
    created_at: datetime
    updated_at: datetime


_catalog_metadata = MetaData()
catalog_meta = Table(
    "catalog_meta",
    _catalog_metadata,
    Column("singleton", Integer, primary_key=True),
    Column("catalog_id", Text, nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("commit_seq", Integer, nullable=False, server_default=text("0")),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    CheckConstraint("singleton = 1", name="ck_catalog_meta_singleton"),
    CheckConstraint("schema_version > 0", name="ck_catalog_meta_schema_version_positive"),
    CheckConstraint("commit_seq >= 0", name="ck_catalog_meta_commit_seq_nonnegative"),
)


def _schema_generation() -> str:
    """Fingerprint every catalog-owned table shape used for daemon adoption."""

    shape: list[dict[str, object]] = []
    for metadata in (LiveBase.metadata, CatalogBase.metadata, _catalog_metadata):
        for table in metadata.sorted_tables:
            shape.append(
                {
                    "table": table.name,
                    "columns": [
                        {
                            "name": column.name,
                            "type": str(column.type),
                            "nullable": column.nullable,
                            "primary_key": column.primary_key,
                            "server_default": (str(column.server_default.arg) if column.server_default is not None else None),
                            "foreign_keys": sorted(str(key.target_fullname) for key in column.foreign_keys),
                        }
                        for column in table.columns
                    ],
                }
            )
    encoded = json.dumps(
        {"schema_version": CATALOG_SCHEMA_VERSION, "tables": shape},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


CATALOG_SCHEMA_GENERATION = _schema_generation()

# Every version bump must register the transaction that moves the preceding
# durable version forward. An empty registry is intentional for initial v1.
CATALOG_SCHEMA_MIGRATIONS: dict[int, Callable[[Connection], None]] = {}


def create_catalog_engine(database: str | Path, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> Engine:
    """Create the catalogd-owned SQLite engine.

    ``database`` may be a SQLAlchemy SQLite URL or a filesystem path.  The
    latter is the normal production form for ``longhouse-live.db``.
    """

    if busy_timeout_ms < 0:
        raise ValueError("busy_timeout_ms must be non-negative")

    if isinstance(database, Path):
        database_url = f"sqlite:///{database.expanduser().resolve()}"
    else:
        raw = database.strip()
        database_url = raw if raw.startswith("sqlite:") else f"sqlite:///{Path(raw).expanduser().resolve()}"

    parsed = make_url(database_url)
    if not parsed.drivername.startswith("sqlite"):
        raise ValueError("catalogd requires a SQLite database")

    connect_args: dict[str, object] = {
        "check_same_thread": False,
        "timeout": busy_timeout_ms / 1_000,
    }
    kwargs: dict[str, object] = {"connect_args": connect_args}
    if parsed.database in (None, "", ":memory:"):
        kwargs["poolclass"] = StaticPool

    engine = create_engine(database_url, **kwargs)

    @event.listens_for(engine, "connect")
    def _configure_connection(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
            cursor.execute("PRAGMA wal_autocheckpoint=0")
        finally:
            cursor.close()

    return engine


def _safe_additive_columns(engine: Engine, metadata: MetaData) -> list[tuple[str, str]]:
    """Add model columns that SQLite can preserve without a table rebuild.

    Anything requiring a backfill, constraint recreation, or expression
    default is rejected.  Silently starting against a partially migrated
    catalog would be worse than refusing startup.
    """

    existing_tables = set(inspect(engine).get_table_names())
    pending: list[tuple[str, str, str]] = []
    unsafe: list[str] = []
    preparer = engine.dialect.identifier_preparer

    with engine.connect() as connection:
        for table in metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            live_columns = {column["name"] for column in inspect(connection).get_columns(table.name)}
            for column in table.columns:
                if column.name in live_columns:
                    continue
                reason: str | None = None
                if column.primary_key:
                    reason = "primary key"
                elif column.unique:
                    reason = "unique constraint"
                elif column.foreign_keys:
                    reason = "foreign key"
                elif column.default is not None and column.server_default is None:
                    reason = "Python-only default"
                elif not column.nullable and column.server_default is None:
                    reason = "NOT NULL without a server default"
                elif column.server_default is not None:
                    default = getattr(column.server_default, "arg", None)
                    if not isinstance(default, (str, int, float, bool)) and not hasattr(default, "text"):
                        reason = "non-constant server default"
                if reason is not None:
                    unsafe.append(f"{table.name}.{column.name} ({reason})")
                    continue
                try:
                    ddl = str(CreateColumn(column).compile(dialect=engine.dialect))
                except Exception as exc:  # pragma: no cover - dialect failures are model-specific
                    unsafe.append(f"{table.name}.{column.name} (DDL compile failed: {exc})")
                    continue
                pending.append((table.name, column.name, ddl))

    if unsafe:
        joined = ", ".join(sorted(unsafe))
        raise CatalogSchemaMigrationError(f"catalog requires an explicit migration for: {joined}")

    if pending:
        with engine.begin() as connection:
            for table_name, _column_name, ddl in pending:
                quoted_table = preparer.quote(table_name)
                connection.exec_driver_sql(f"ALTER TABLE {quoted_table} ADD COLUMN {ddl}")
    return [(table_name, column_name) for table_name, column_name, _ddl in pending]


def _user_version(connection) -> int:
    return int(connection.exec_driver_sql("PRAGMA user_version").scalar_one())


def _decode_meta(row, *, expected_schema_version: int) -> CatalogMeta:
    try:
        catalog_id = uuid.UUID(str(row.catalog_id))
    except (AttributeError, TypeError, ValueError) as exc:
        raise CatalogSchemaMismatchError("catalog_meta.catalog_id is not a UUID") from exc
    if row.schema_version != expected_schema_version:
        raise CatalogSchemaMismatchError(f"catalog metadata schema_version={row.schema_version}, expected {expected_schema_version}")
    if type(row.commit_seq) is not int or row.commit_seq < 0:
        raise CatalogSchemaMismatchError("catalog_meta.commit_seq must be a non-negative integer")
    try:
        created_at = datetime.fromisoformat(str(row.created_at))
        updated_at = datetime.fromisoformat(str(row.updated_at))
    except ValueError as exc:
        raise CatalogSchemaMismatchError("catalog_meta timestamps are invalid") from exc
    if created_at.tzinfo is None or updated_at.tzinfo is None:
        raise CatalogSchemaMismatchError("catalog_meta timestamps must include a timezone")
    return CatalogMeta(
        catalog_id=catalog_id,
        schema_version=row.schema_version,
        commit_seq=row.commit_seq,
        created_at=created_at,
        updated_at=updated_at,
    )


def read_catalog_meta(engine: Engine, *, expected_schema_version: int | None = None) -> CatalogMeta:
    """Read and validate the singleton catalog metadata row and version marker."""

    if expected_schema_version is None:
        expected_schema_version = CATALOG_SCHEMA_VERSION

    with engine.connect() as connection:
        user_version = _user_version(connection)
        rows = connection.execute(select(catalog_meta)).all()
    if len(rows) != 1:
        raise CatalogSchemaMismatchError(f"catalog_meta must contain exactly one row; found {len(rows)}")
    metadata = _decode_meta(rows[0], expected_schema_version=expected_schema_version)
    if user_version != metadata.schema_version:
        raise CatalogSchemaMismatchError(
            f"PRAGMA user_version={user_version} does not match " f"catalog metadata schema_version={metadata.schema_version}"
        )
    return metadata


def _migrate_catalog_schema(engine: Engine, *, from_version: int) -> None:
    if from_version > CATALOG_SCHEMA_VERSION:
        raise CatalogSchemaMismatchError(f"catalog schema_version={from_version} is newer than reader version={CATALOG_SCHEMA_VERSION}")
    for current_version in range(from_version, CATALOG_SCHEMA_VERSION):
        migration = CATALOG_SCHEMA_MIGRATIONS.get(current_version)
        if migration is None:
            raise CatalogSchemaMigrationError(f"missing catalog schema migration {current_version}->{current_version + 1}")
        next_version = current_version + 1
        now = datetime.now(UTC).isoformat()
        with engine.begin() as connection:
            durable_version = connection.execute(select(catalog_meta.c.schema_version).where(catalog_meta.c.singleton == 1)).scalar_one()
            if durable_version != current_version or _user_version(connection) != current_version:
                raise CatalogSchemaMismatchError("catalog schema markers changed during migration")
            migration(connection)
            connection.execute(
                catalog_meta.update().where(catalog_meta.c.singleton == 1).values(schema_version=next_version, updated_at=now)
            )
            connection.exec_driver_sql(f"PRAGMA user_version={next_version}")


def initialize_catalog_schema(engine: Engine) -> CatalogMeta:
    """Create or idempotently upgrade the v1 live catalog schema."""

    if engine.dialect.name != "sqlite":
        raise ValueError("catalogd requires a SQLite engine")

    table_names = set(inspect(engine).get_table_names())
    has_meta_table = catalog_meta.name in table_names
    with engine.connect() as connection:
        user_version = _user_version(connection)

    if has_meta_table:
        # Validate both markers before making any model-driven schema changes.
        with engine.connect() as connection:
            rows = connection.execute(select(catalog_meta)).all()
        if len(rows) != 1:
            raise CatalogSchemaMismatchError(f"catalog_meta must contain exactly one row; found {len(rows)}")
        durable_version = rows[0].schema_version
        read_catalog_meta(engine, expected_schema_version=durable_version)
        _migrate_catalog_schema(engine, from_version=durable_version)
    elif user_version != 0:
        raise CatalogSchemaMismatchError(f"PRAGMA user_version={user_version} is set but catalog_meta is missing")

    LiveBase.metadata.create_all(bind=engine)
    CatalogBase.metadata.create_all(bind=engine)
    _catalog_metadata.create_all(bind=engine)
    _safe_additive_columns(engine, LiveBase.metadata)
    _safe_additive_columns(engine, CatalogBase.metadata)
    _safe_additive_columns(engine, _catalog_metadata)

    if not has_meta_table:
        now = datetime.now(UTC)
        new_catalog_id = str(uuid.uuid4())
        with engine.begin() as connection:
            connection.execute(
                catalog_meta.insert().values(
                    singleton=1,
                    catalog_id=new_catalog_id,
                    schema_version=CATALOG_SCHEMA_VERSION,
                    commit_seq=0,
                    created_at=now.isoformat(),
                    updated_at=now.isoformat(),
                )
            )
            connection.exec_driver_sql(f"PRAGMA user_version={CATALOG_SCHEMA_VERSION}")

    return read_catalog_meta(engine)


__all__ = [
    "CATALOG_SCHEMA_GENERATION",
    "CATALOG_SCHEMA_MIGRATIONS",
    "CATALOG_SCHEMA_VERSION",
    "CatalogMeta",
    "CatalogSchemaError",
    "CatalogSchemaMigrationError",
    "CatalogSchemaMismatchError",
    "catalog_meta",
    "create_catalog_engine",
    "initialize_catalog_schema",
    "read_catalog_meta",
]
