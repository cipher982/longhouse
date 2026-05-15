"""Phase 1 of Option D: auto-derive missing column ADDs from SQLAlchemy metadata."""

import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

import json  # noqa: E402

from sqlalchemy import (  # noqa: E402
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    text,
)

from zerg.database import _auto_add_missing_columns, make_engine  # noqa: E402


def _live_columns(engine, table_name: str) -> set[str]:
    with engine.connect() as conn:
        return {row[1] for row in conn.execute(text(f"PRAGMA table_info({table_name})"))}


def _make_engine(tmp_path, name):
    return make_engine(f"sqlite:///{tmp_path / name}")


def test_adds_missing_column_to_existing_table(tmp_path):
    engine = _make_engine(tmp_path, "autocol.db")
    md_v1 = MetaData()
    Table("widgets", md_v1, Column("id", Integer, primary_key=True), Column("name", String(50)))
    md_v1.create_all(engine)

    md_v2 = MetaData()
    Table(
        "widgets",
        md_v2,
        Column("id", Integer, primary_key=True),
        Column("name", String(50)),
        Column("color", String(20)),
        Column("count", Integer, nullable=False, server_default="0"),
    )
    added = _auto_add_missing_columns(engine, md_v2, apply=True)
    assert ("widgets", "color") in added
    assert ("widgets", "count") in added
    assert {"color", "count"}.issubset(_live_columns(engine, "widgets"))


def test_dry_run_does_not_alter(tmp_path):
    engine = _make_engine(tmp_path, "dry.db")
    md = MetaData()
    Table("foo", md, Column("id", Integer, primary_key=True))
    md.create_all(engine)

    md2 = MetaData()
    Table("foo", md2, Column("id", Integer, primary_key=True), Column("extra", String(10)))
    assert _auto_add_missing_columns(engine, md2, apply=False) == [("foo", "extra")]
    assert "extra" not in _live_columns(engine, "foo")


def test_no_op_when_schema_matches(tmp_path):
    engine = _make_engine(tmp_path, "match.db")
    md = MetaData()
    Table("bar", md, Column("id", Integer, primary_key=True), Column("v", String(10)))
    md.create_all(engine)
    assert _auto_add_missing_columns(engine, md, apply=True) == []


def test_skips_pk_and_brand_new_table(tmp_path, caplog):
    """PK adds are illegal under SQLite ALTER; brand-new tables belong to create_all."""
    engine = _make_engine(tmp_path, "skip.db")
    md_v1 = MetaData()
    Table("baz", md_v1, Column("id", Integer, primary_key=True))
    md_v1.create_all(engine)

    md_v2 = MetaData()
    Table(
        "baz",
        md_v2,
        Column("id", Integer, primary_key=True),
        Column("alt_id", Integer, primary_key=True),
        Column("note", String(20)),
    )
    Table("brand_new", md_v2, Column("id", Integer, primary_key=True))

    with caplog.at_level("INFO"):
        added = _auto_add_missing_columns(engine, md_v2, apply=True)
    assert ("baz", "note") in added
    assert ("baz", "alt_id") not in added
    assert all(t != "brand_new" for t, _ in added)
    assert "alt_id" not in _live_columns(engine, "baz")
    assert any("primary_key" in rec.message for rec in caplog.records)


def test_skips_python_default_without_server_default(tmp_path, caplog):
    """Columns with Python ``default=0`` but no ``server_default`` must be skipped.

    Auto-derive would otherwise emit a plain ``INTEGER`` ALTER (no DEFAULT clause),
    leaving legacy rows NULL — and would also block the imperative migrator's
    ``if col not in columns`` backfill from running. This is the regression class
    that broke ``AgentHeartbeat.spool_dead``/``ship_attempts_1h``.
    """
    engine = _make_engine(tmp_path, "pydefault.db")
    md_v1 = MetaData()
    Table("counters", md_v1, Column("id", Integer, primary_key=True))
    md_v1.create_all(engine)
    # Seed a legacy row so we can verify it stays untouched on the auto path.
    with engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO counters DEFAULT VALUES")

    md_v2 = MetaData()
    Table(
        "counters",
        md_v2,
        Column("id", Integer, primary_key=True),
        # AgentHeartbeat.spool_dead-style: Python default, no server_default.
        Column("spool_dead", Integer, default=0),
        Column("ship_attempts_1h", Integer, default=0),
    )
    with caplog.at_level("INFO"):
        added = _auto_add_missing_columns(engine, md_v2, apply=True)
    # Neither column should have been added by the auto path.
    assert added == []
    assert "spool_dead" not in _live_columns(engine, "counters")
    assert "ship_attempts_1h" not in _live_columns(engine, "counters")
    # Skip log message must mention the imperative migrator handoff.
    skip_msgs = [rec.message for rec in caplog.records if "auto-derive skip" in rec.message]
    assert any("spool_dead" in m and "imperative migrator" in m for m in skip_msgs)
    assert any("ship_attempts_1h" in m and "imperative migrator" in m for m in skip_msgs)


def test_python_default_skip_lets_server_default_columns_through(tmp_path):
    """Mixed model: server_default columns add, Python-default columns skip."""
    engine = _make_engine(tmp_path, "mixed.db")
    md_v1 = MetaData()
    Table("mixed_t", md_v1, Column("id", Integer, primary_key=True))
    md_v1.create_all(engine)

    md_v2 = MetaData()
    Table(
        "mixed_t",
        md_v2,
        Column("id", Integer, primary_key=True),
        Column("py_only", Integer, default=0),  # must skip
        Column("server_only", Integer, nullable=False, server_default="0"),  # must add
    )
    added = _auto_add_missing_columns(engine, md_v2, apply=True)
    assert added == [("mixed_t", "server_only")]
    cols = _live_columns(engine, "mixed_t")
    assert "server_only" in cols
    assert "py_only" not in cols


def test_handles_server_default(tmp_path):
    engine = _make_engine(tmp_path, "def.db")
    md_v1 = MetaData()
    Table("rows", md_v1, Column("id", Integer, primary_key=True))
    md_v1.create_all(engine)

    md_v2 = MetaData()
    Table(
        "rows",
        md_v2,
        Column("id", Integer, primary_key=True),
        Column("flag", Integer, nullable=False, server_default="1"),
    )
    assert ("rows", "flag") in _auto_add_missing_columns(engine, md_v2, apply=True)
    with engine.connect() as conn:
        conn.exec_driver_sql("INSERT INTO rows DEFAULT VALUES")
        conn.commit()
        rows = conn.exec_driver_sql("SELECT flag FROM rows").fetchall()
    assert rows and rows[0][0] == 1


# --- Codex C2 coverage gaps ---------------------------------------------------


def test_adds_json_column_and_accepts_json_payload(tmp_path):
    """JSON columns must compile to SQLite-valid DDL and accept JSON inserts."""
    engine = _make_engine(tmp_path, "json.db")
    md_v1 = MetaData()
    Table("docs", md_v1, Column("id", Integer, primary_key=True))
    md_v1.create_all(engine)

    md_v2 = MetaData()
    Table(
        "docs",
        md_v2,
        Column("id", Integer, primary_key=True),
        Column("payload", JSON),
    )
    added = _auto_add_missing_columns(engine, md_v2, apply=True)
    assert ("docs", "payload") in added
    assert "payload" in _live_columns(engine, "docs")
    # Round-trip a JSON value to confirm the column is usable.
    blob = json.dumps({"k": [1, 2, 3]})
    with engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO docs (payload) VALUES (?)", (blob,))
        rows = conn.exec_driver_sql("SELECT payload FROM docs").fetchall()
    assert rows and json.loads(rows[0][0]) == {"k": [1, 2, 3]}


def test_adds_datetime_with_timezone(tmp_path):
    """DateTime(timezone=True) must compile and insertable timestamps round-trip."""
    engine = _make_engine(tmp_path, "dttz.db")
    md_v1 = MetaData()
    Table("events", md_v1, Column("id", Integer, primary_key=True))
    md_v1.create_all(engine)

    md_v2 = MetaData()
    Table(
        "events",
        md_v2,
        Column("id", Integer, primary_key=True),
        Column("ts", DateTime(timezone=True)),
    )
    added = _auto_add_missing_columns(engine, md_v2, apply=True)
    assert ("events", "ts") in added
    # SQLite has no real TZ type; SQLAlchemy compiles it to DATETIME and stores
    # ISO strings — verify the column accepts an ISO timestamp.
    with engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO events (ts) VALUES (?)", ("2026-01-02T03:04:05+00:00",))
        rows = conn.exec_driver_sql("SELECT ts FROM events").fetchall()
    assert rows and "2026-01-02" in rows[0][0]


def test_adds_foreign_key_column_without_inline_constraint(tmp_path):
    """FK columns add successfully — SQLite ALTER cannot enforce inline FK, so
    auto-derive must drop the constraint silently. Document that behavior so we
    notice if it ever changes."""
    engine = _make_engine(tmp_path, "fk.db")
    md_v1 = MetaData()
    Table("parents", md_v1, Column("id", Integer, primary_key=True))
    Table("children", md_v1, Column("id", Integer, primary_key=True))
    md_v1.create_all(engine)

    md_v2 = MetaData()
    Table("parents", md_v2, Column("id", Integer, primary_key=True))
    Table(
        "children",
        md_v2,
        Column("id", Integer, primary_key=True),
        Column("parent_id", Integer, ForeignKey("parents.id")),
    )
    added = _auto_add_missing_columns(engine, md_v2, apply=True)
    assert ("children", "parent_id") in added
    assert "parent_id" in _live_columns(engine, "children")
    # No FK constraint should be present on the child table — SQLAlchemy's
    # CreateColumn compiler emits only the bare type for ALTER ADD COLUMN.
    with engine.connect() as conn:
        fks = list(conn.exec_driver_sql("PRAGMA foreign_key_list(children)").fetchall())
    assert fks == []


def test_adds_indexed_column_but_not_index(tmp_path):
    """``index=True`` columns: ALTER ADD COLUMN succeeds but the index is NOT
    created. Known limitation — index creation belongs to a follow-up CREATE
    INDEX in the imperative migrator."""
    engine = _make_engine(tmp_path, "idx.db")
    md_v1 = MetaData()
    Table("items", md_v1, Column("id", Integer, primary_key=True))
    md_v1.create_all(engine)

    md_v2 = MetaData()
    Table(
        "items",
        md_v2,
        Column("id", Integer, primary_key=True),
        Column("tag", String(20), index=True),
    )
    added = _auto_add_missing_columns(engine, md_v2, apply=True)
    assert ("items", "tag") in added
    assert "tag" in _live_columns(engine, "items")
    # No index should exist — auto-derive only emits ALTER ADD COLUMN.
    with engine.connect() as conn:
        idx_rows = list(conn.exec_driver_sql("PRAGMA index_list(items)").fetchall())
    assert idx_rows == [], f"unexpected indexes after auto-derive: {idx_rows}"


def test_server_default_backfills_legacy_rows(tmp_path):
    """Insert legacy rows BEFORE the ALTER, then assert SQLite reads back the
    server_default for those rows AFTER. This is the contract the imperative
    migrator depended on — must survive auto-derive."""
    engine = _make_engine(tmp_path, "backfill.db")
    md_v1 = MetaData()
    Table("legacy", md_v1, Column("id", Integer, primary_key=True))
    md_v1.create_all(engine)
    # Seed two legacy rows BEFORE the column exists.
    with engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO legacy DEFAULT VALUES")
        conn.exec_driver_sql("INSERT INTO legacy DEFAULT VALUES")

    md_v2 = MetaData()
    Table(
        "legacy",
        md_v2,
        Column("id", Integer, primary_key=True),
        Column("status", String(10), nullable=False, server_default="ready"),
    )
    added = _auto_add_missing_columns(engine, md_v2, apply=True)
    assert ("legacy", "status") in added
    with engine.connect() as conn:
        rows = conn.exec_driver_sql("SELECT id, status FROM legacy ORDER BY id").fetchall()
    # Both legacy rows should now read the server_default value.
    assert [r[1] for r in rows] == ["ready", "ready"]
