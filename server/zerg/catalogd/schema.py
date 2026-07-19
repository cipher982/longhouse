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
from sqlalchemy import UniqueConstraint
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

CATALOG_SCHEMA_VERSION = 2
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
    # Additive feature marker. Older v2 binaries ignore this column and the
    # reducer tables, so pre-cutover rollback remains possible.
    Column("fact_reducer_generation", Text, nullable=True),
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
def _hide_empty_human_launch_shells(connection: Connection) -> None:
    human_launch = (
        "(launch_actor IN ('user', 'human_ui', 'human_shell') " "OR launch_surface IN ('web', 'ios', 'console', 'terminal', 'api'))"
    )
    connection.exec_driver_sql(
        "UPDATE live_session_catalog SET hidden_from_default_timeline = 1 "
        "WHERE hidden_from_default_timeline = 0 AND transcript_revision = 0 "
        "AND user_messages = 0 AND assistant_messages = 0 AND tool_calls = 0 AND " + human_launch
    )
    connection.exec_driver_sql(
        "UPDATE live_timeline_cards SET hidden_from_default_timeline = 1 "
        "WHERE hidden_from_default_timeline = 0 AND transcript_revision = 0 "
        "AND user_messages = 0 AND assistant_messages = 0 AND tool_calls = 0 AND " + human_launch
    )
    connection.exec_driver_sql(
        "UPDATE sessions SET hidden_from_default_timeline = 1 "
        "WHERE hidden_from_default_timeline = 0 AND transcript_revision = 0 "
        "AND user_messages = 0 AND assistant_messages = 0 AND tool_calls = 0 AND " + human_launch
    )


_FACT_REDUCER_V1_TABLES = ("fact_heads", "fact_receipts", "fact_conflicts")
_FACT_REDUCER_TABLES = (*_FACT_REDUCER_V1_TABLES, "fact_parity_deltas")
_FACT_REDUCER_V1_GENERATION = "edc85ddb74216aec3eca96e055a821bcd317f6ab8563c02e786f99556b1bea92"
_FACT_REDUCER_V2_GENERATION = "cb7f217f33511609b328bc480d7e63325ecafb95504bd28ffae3eb80b8c142e7"


def _fact_reducer_generation() -> str:
    shape: list[dict[str, object]] = []
    for table_name in _FACT_REDUCER_TABLES:
        table = CatalogBase.metadata.tables[table_name]
        shape.append(
            {
                "table": table_name,
                "columns": [
                    {
                        "name": column.name,
                        "type": str(column.type),
                        "nullable": column.nullable,
                        "primary_key": column.primary_key,
                        "server_default": (str(column.server_default.arg) if column.server_default is not None else None),
                    }
                    for column in table.columns
                ],
                "unique_constraints": sorted(
                    (
                        {
                            "name": constraint.name,
                            "columns": [column.name for column in constraint.columns],
                        }
                        for constraint in table.constraints
                        if isinstance(constraint, UniqueConstraint)
                    ),
                    key=lambda item: str(item["name"]),
                ),
                "indexes": sorted(
                    (
                        {
                            "name": index.name,
                            "columns": [column.name for column in index.columns],
                            "unique": bool(index.unique),
                        }
                        for index in table.indexes
                    ),
                    key=lambda item: str(item["name"]),
                ),
            }
        )
    encoded = json.dumps(shape, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


FACT_REDUCER_GENERATION = _fact_reducer_generation()


def _validate_fact_reducer_schema(bind: Engine | Connection) -> None:
    inspector = inspect(bind)
    live_tables = set(inspector.get_table_names())
    problems = [f"table:{name}" for name in sorted(set(_FACT_REDUCER_TABLES) - live_tables)]
    for table_name in set(_FACT_REDUCER_TABLES) & live_tables:
        declared = CatalogBase.metadata.tables[table_name]
        live_columns = {column["name"]: column for column in inspector.get_columns(table_name)}
        declared_columns = {column.name: column for column in declared.columns}
        if set(live_columns) != set(declared_columns):
            problems.append(f"columns:{table_name}")
        for name in set(live_columns) & set(declared_columns):
            live = live_columns[name]
            expected = declared_columns[name]
            if bool(live["nullable"]) != bool(expected.nullable):
                problems.append(f"nullability:{table_name}.{name}")
            if str(live["type"]).upper() != str(expected.type).upper():
                problems.append(f"type:{table_name}.{name}")
            live_default = live.get("default")
            expected_default = str(expected.server_default.arg).strip() if expected.server_default is not None else None
            if (str(live_default).strip() if live_default is not None else None) != expected_default:
                problems.append(f"default:{table_name}.{name}")

        live_pk = tuple(inspector.get_pk_constraint(table_name).get("constrained_columns") or ())
        declared_pk = tuple(column.name for column in declared.primary_key.columns)
        if live_pk != declared_pk:
            problems.append(f"primary-key:{table_name}")

        live_uniques = {
            (constraint.get("name"), tuple(constraint.get("column_names") or ()))
            for constraint in inspector.get_unique_constraints(table_name)
        }
        declared_uniques = {
            (constraint.name, tuple(column.name for column in constraint.columns))
            for constraint in declared.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        if live_uniques != declared_uniques:
            problems.append(f"unique-constraints:{table_name}")

        live_indexes = {
            (index.get("name"), tuple(index.get("column_names") or ()), bool(index.get("unique")))
            for index in inspector.get_indexes(table_name)
        }
        declared_indexes = {(index.name, tuple(column.name for column in index.columns), bool(index.unique)) for index in declared.indexes}
        if live_indexes != declared_indexes:
            problems.append(f"indexes:{table_name}")
    if problems:
        raise CatalogSchemaMismatchError("catalog fact reducer schema is incompatible: " + ", ".join(sorted(set(problems))))


def _initialize_fact_reducer_schema(engine: Engine) -> None:
    """Atomically adopt or validate additive reducer storage on catalog v2."""

    # pysqlite does not begin a transaction for DDL on its own. Force the
    # boundary before ALTER/CREATE so an interrupted adoption cannot leave a
    # partial table set or marker column behind.
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            _initialize_fact_reducer_schema_in_transaction(connection)
        except BaseException:
            connection.rollback()
            raise
        connection.commit()


def _initialize_fact_reducer_schema_in_transaction(connection: Connection) -> None:
    columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(catalog_meta)")}
    if "fact_reducer_generation" not in columns:
        connection.exec_driver_sql("ALTER TABLE catalog_meta ADD COLUMN fact_reducer_generation TEXT")
    marker = connection.exec_driver_sql("SELECT fact_reducer_generation FROM catalog_meta WHERE singleton = 1").scalar_one()
    tables = set(inspect(connection).get_table_names())
    present = set(_FACT_REDUCER_TABLES) & tables
    if marker is not None:
        if marker == _FACT_REDUCER_V1_GENERATION:
            if present != set(_FACT_REDUCER_V1_TABLES):
                raise CatalogSchemaMismatchError("catalog fact reducer v1 schema is partial")
            CatalogBase.metadata.tables["fact_parity_deltas"].create(bind=connection)
            present = set(_FACT_REDUCER_TABLES)
            marker = _FACT_REDUCER_V2_GENERATION
        if marker == _FACT_REDUCER_V2_GENERATION:
            if present != set(_FACT_REDUCER_TABLES):
                raise CatalogSchemaMismatchError("catalog fact reducer v2 schema is partial")
            head_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(fact_heads)")}
            head_indexes = {row[1] for row in connection.exec_driver_sql("PRAGMA index_list(fact_heads)")}
            if "session_id" in head_columns or "ix_fact_heads_session_family_recent" in head_indexes:
                raise CatalogSchemaMismatchError("catalog fact reducer v2 schema is ambiguous")
            connection.exec_driver_sql("ALTER TABLE fact_heads ADD COLUMN session_id VARCHAR(255)")
            connection.exec_driver_sql(
                "CREATE INDEX ix_fact_heads_session_family_recent " "ON fact_heads(session_id, family, updated_commit_seq)"
            )
            # Schema-v2 facts lack authority classes and true launch-scoped run
            # identities. Rebuild shadow-only state instead of projecting mixed
            # generations. The surrounding BEGIN IMMEDIATE makes this atomic.
            for table_name in ("fact_parity_deltas", "fact_conflicts", "fact_receipts", "fact_heads"):
                connection.exec_driver_sql(f"DELETE FROM {table_name}")
            _validate_fact_reducer_schema(connection)
            connection.exec_driver_sql(
                "UPDATE catalog_meta SET fact_reducer_generation = ? WHERE singleton = 1",
                (FACT_REDUCER_GENERATION,),
            )
            return
        if marker != FACT_REDUCER_GENERATION:
            raise CatalogSchemaMismatchError("catalog fact reducer generation does not match this build")
        _validate_fact_reducer_schema(connection)
        return
    if present and present != set(_FACT_REDUCER_TABLES):
        raise CatalogSchemaMismatchError("catalog fact reducer adoption is partial")
    if not present:
        for table_name in _FACT_REDUCER_TABLES:
            CatalogBase.metadata.tables[table_name].create(bind=connection)
    _validate_fact_reducer_schema(connection)
    connection.exec_driver_sql(
        "UPDATE catalog_meta SET fact_reducer_generation = ? WHERE singleton = 1",
        (FACT_REDUCER_GENERATION,),
    )


CATALOG_SCHEMA_MIGRATIONS: dict[int, Callable[[Connection], None]] = {
    1: _hide_empty_human_launch_shells,
}


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


def _create_declared_indexes(engine: Engine, metadata: MetaData) -> None:
    """Create model-declared indexes after additive columns exist.

    SQLAlchemy's ``create_all`` skips indexes when their table already exists.
    Catalog v2 is still pre-cutover, so startup may install these bounded-table
    indexes now rather than leaving production silently unindexed.
    """

    for table in metadata.sorted_tables:
        for index in table.indexes:
            index.create(bind=engine, checkfirst=True)


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
            rows = connection.execute(
                select(
                    catalog_meta.c.singleton,
                    catalog_meta.c.catalog_id,
                    catalog_meta.c.schema_version,
                    catalog_meta.c.commit_seq,
                    catalog_meta.c.created_at,
                    catalog_meta.c.updated_at,
                )
            ).all()
        if len(rows) != 1:
            raise CatalogSchemaMismatchError(f"catalog_meta must contain exactly one row; found {len(rows)}")
        durable_version = rows[0].schema_version
        _decode_meta(rows[0], expected_schema_version=durable_version)
        if user_version != durable_version:
            raise CatalogSchemaMismatchError(
                f"PRAGMA user_version={user_version} does not match catalog metadata schema_version={durable_version}"
            )
        _migrate_catalog_schema(engine, from_version=durable_version)
        _initialize_fact_reducer_schema(engine)
    elif user_version != 0:
        raise CatalogSchemaMismatchError(f"PRAGMA user_version={user_version} is set but catalog_meta is missing")

    LiveBase.metadata.create_all(bind=engine)
    CatalogBase.metadata.create_all(bind=engine)
    _catalog_metadata.create_all(bind=engine)
    _safe_additive_columns(engine, LiveBase.metadata)
    _safe_additive_columns(engine, CatalogBase.metadata)
    _safe_additive_columns(engine, _catalog_metadata)
    _create_declared_indexes(engine, LiveBase.metadata)
    _create_declared_indexes(engine, CatalogBase.metadata)
    _create_declared_indexes(engine, _catalog_metadata)
    _validate_fact_reducer_schema(engine)

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
                    fact_reducer_generation=FACT_REDUCER_GENERATION,
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
