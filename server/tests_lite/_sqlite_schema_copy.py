"""Give an empty SQLite database its ``create_all`` schema from cached DDL.

Tests build a private database per test and call ``Base.metadata.create_all``
(directly or through ``initialize_database``) about 1000 times a run. For the
41-table app schema that is ~18 ms each, nearly all SQLAlchemy DDL compilation
of the same ~275 statements. The first call for a given schema shape runs the
real ``create_all`` into an in-memory template and keeps the CREATE text it
produced; each later call against an *empty* database replays that text
(~5 ms). Every test still gets its own database, so isolation is unchanged.

The replay runs in one ``BEGIN IMMEDIATE`` transaction that first rechecks
the target is empty, so a concurrent writer (another thread or process) that
created anything since is never overwritten; a non-empty recheck falls back to
the real ``create_all``. Anything else the replay cannot vouch for also takes
the real path: a non-SQLite target, a Connection (it may be mid-transaction),
and metadata or tables with DDL event listeners (other than SQLAlchemy's own
native-type hooks), whose side effects a replay would skip.

The template key is each table's full DDL-relevant shape -- column names,
types, nullability, keys and server defaults, indexes, constraints and dialect
options -- so a test that edits a metadata in place never gets a stale schema.
"""

from sqlalchemy import CheckConstraint
from sqlalchemy import Engine
from sqlalchemy import ForeignKeyConstraint
from sqlalchemy import MetaData
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import SchemaType

_real_create_all = MetaData.create_all
_templates: dict[tuple, tuple[str, ...]] = {}


def _has_ddl_listeners(metadata: MetaData, tables) -> bool:
    # Enum and Boolean register create hooks for dialects with native types
    # (CREATE TYPE on Postgres); on SQLite they emit nothing.
    targets = [metadata, *(tables if tables is not None else metadata.tables.values())]
    return any(
        not isinstance(getattr(listener, "target", None), SchemaType)
        for target in targets
        for listener in (*target.dispatch.before_create, *target.dispatch.after_create)
    )


def _clause(value) -> str | None:
    if value is None:
        return None
    return f"{type(value).__name__}:{getattr(value, 'arg', value)}"


def _constraint_shape(constraint) -> tuple:
    shape = (type(constraint).__name__, constraint.name, tuple(column.name for column in constraint.columns))
    if isinstance(constraint, CheckConstraint):
        shape += (str(constraint.sqltext),)
    if isinstance(constraint, ForeignKeyConstraint):
        shape += (tuple(element.target_fullname for element in constraint.elements), constraint.ondelete, constraint.onupdate)
    return shape


def _table_shape(table) -> tuple:
    return (
        table.fullname,
        repr(sorted(table.dialect_kwargs.items())),
        tuple(
            (
                column.name,
                repr(column.type),
                column.nullable,
                column.primary_key,
                column.autoincrement,
                column.unique,
                _clause(column.server_default),
                _clause(column.computed),
            )
            for column in table.columns
        ),
        tuple(
            sorted(
                (index.name, index.unique, tuple(str(expression) for expression in index.expressions), repr(sorted(index.dialect_kwargs.items())))
                for index in table.indexes
            )
        ),
        tuple(sorted((_constraint_shape(constraint) for constraint in table.constraints), key=repr)),
    )


def _key(metadata: MetaData, tables, translate_map) -> tuple:
    chosen = tables if tables is not None else metadata.sorted_tables
    return (
        id(metadata),
        tables is not None,
        tuple(sorted((translate_map or {}).items(), key=repr)),
        tuple(_table_shape(table) for table in chosen),
    )


def _is_empty(bind: Engine) -> bool:
    with bind.connect() as connection:
        return connection.exec_driver_sql("SELECT COUNT(*) FROM sqlite_master").scalar_one() == 0


def _template_statements(metadata: MetaData, tables, checkfirst, translate_map) -> tuple[str, ...]:
    key = _key(metadata, tables, translate_map)
    statements = _templates.get(key)
    if statements is None:
        template = create_engine("sqlite://", poolclass=StaticPool)
        try:
            # initialize_database strips the zerg/agents schemas with a
            # translate map; the template must be built through the same one.
            ddl_bind = template.execution_options(schema_translate_map=translate_map) if translate_map else template
            _real_create_all(metadata, ddl_bind, tables=tables, checkfirst=checkfirst)
            with template.connect() as connection:
                rows = connection.exec_driver_sql(
                    "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY rowid"
                ).all()
        finally:
            template.dispose()
        statements = _templates[key] = tuple(row[0] for row in rows)
    return statements


def _replay_if_empty(bind: Engine, statements: tuple[str, ...]) -> bool:
    raw = bind.raw_connection()
    try:
        driver = raw.driver_connection
        if driver.in_transaction:
            return False
        cursor = driver.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            try:
                if cursor.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] != 0:
                    cursor.execute("ROLLBACK")
                    return False
                for statement in statements:
                    cursor.execute(statement)
                cursor.execute("COMMIT")
                return True
            except BaseException:
                if driver.in_transaction:
                    cursor.execute("ROLLBACK")
                raise
        finally:
            cursor.close()
    finally:
        raw.close()


def _create_all(self, bind, tables=None, checkfirst=True):
    if isinstance(bind, Engine) and bind.dialect.name == "sqlite" and not _has_ddl_listeners(self, tables) and _is_empty(bind):
        translate_map = bind.get_execution_options().get("schema_translate_map")
        if _replay_if_empty(bind, _template_statements(self, tables, checkfirst, translate_map)):
            return None
    return _real_create_all(self, bind, tables=tables, checkfirst=checkfirst)


def pytest_configure(config):
    MetaData.create_all = _create_all


def pytest_unconfigure(config):
    MetaData.create_all = _real_create_all
