"""Phase 1 of Option D: auto-derive missing column ADDs from SQLAlchemy metadata."""

from sqlalchemy import Column, Integer, MetaData, String, Table, text

from zerg.database import _auto_add_missing_columns, make_engine


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
