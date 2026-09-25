"""Give an empty SQLite database its ``create_all`` schema by page copy.

Tests build a private database per test and call ``Base.metadata.create_all``
(directly or through ``initialize_database``) about 1000 times a run. For the
41-table app schema that is ~18 ms each, nearly all SQLAlchemy DDL compilation
of the same ~275 statements. The first call for a given schema shape runs the
real ``create_all`` into an in-memory template; each later call against an
*empty* database copies the template's pages in with SQLite's backup API
(~1 ms). Every test still gets its own database, so isolation is unchanged.

Anything the copy cannot vouch for takes the real path: a non-SQLite or
non-empty target, a Connection (it may be mid-transaction), and metadata or
tables with DDL event listeners (other than SQLAlchemy's own native-type
hooks), whose side effects a copy would replay stale. The template key includes each table's column, index and constraint
counts, so a test that reshapes a metadata never reuses an old template.
"""

import threading

from sqlalchemy import Engine
from sqlalchemy import MetaData
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import SchemaType

_real_create_all = MetaData.create_all
_templates: dict[tuple, Engine] = {}
_lock = threading.Lock()


def _has_ddl_listeners(metadata: MetaData, tables) -> bool:
    # Enum and Boolean register create hooks for dialects with native types
    # (CREATE TYPE on Postgres); on SQLite they emit nothing.
    targets = [metadata, *(tables if tables is not None else metadata.tables.values())]
    return any(
        not isinstance(getattr(listener, "target", None), SchemaType)
        for target in targets
        for listener in (*target.dispatch.before_create, *target.dispatch.after_create)
    )


def _key(metadata: MetaData, tables, translate_map) -> tuple:
    chosen = tables if tables is not None else metadata.sorted_tables
    return (
        id(metadata),
        tables is not None,
        tuple(sorted((translate_map or {}).items(), key=repr)),
        tuple((t.name, len(t.columns), len(t.indexes), len(t.constraints)) for t in chosen),
    )


def _is_empty(bind: Engine) -> bool:
    with bind.connect() as connection:
        return connection.exec_driver_sql("SELECT COUNT(*) FROM sqlite_master").scalar_one() == 0


def _create_all(self, bind, tables=None, checkfirst=True):
    if not isinstance(bind, Engine) or bind.dialect.name != "sqlite" or _has_ddl_listeners(self, tables) or not _is_empty(bind):
        return _real_create_all(self, bind, tables=tables, checkfirst=checkfirst)
    # initialize_database strips the zerg/agents schemas with a translate map;
    # the template must be built through the same one.
    translate_map = bind.get_execution_options().get("schema_translate_map")
    key = _key(self, tables, translate_map)
    with _lock:
        template = _templates.get(key)
        if template is None:
            template = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
            ddl_bind = template.execution_options(schema_translate_map=translate_map) if translate_map else template
            _real_create_all(self, ddl_bind, tables=tables, checkfirst=checkfirst)
            _templates[key] = template
        source = template.raw_connection()
        target = bind.raw_connection()
        try:
            source.driver_connection.backup(target.driver_connection)
        finally:
            target.close()
            source.close()


def pytest_configure(config):
    MetaData.create_all = _create_all


def pytest_unconfigure(config):
    MetaData.create_all = _real_create_all
