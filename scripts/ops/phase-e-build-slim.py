"""Build a slim Longhouse DB from a quiesced source (Phase E reclaim).

⛔ NOT SAFE TO RUN YET. This version UNCONDITIONALLY sentinels raw columns. hatch
flagged that as unsafe while workflow-subagent source_lines coverage is unsettled
(see docs/runbooks/reliability-data-plane-reclaim.md, "SWAP PARKED"). Before this
runs it MUST be made CONDITIONAL: sentinel only rows proven archive-covered by
(session_id, source_path, source_offset, line_hash); KEEP raw for any uncovered
row; FAIL the build on a row that has neither raw nor coverage. That conditional
logic depends on how the in-flight workflow-ingest feature keys subagent
source_lines, so it is deliberately NOT written yet. Requires the REQUIRE_RECLAIM_OK
env guard below + explicit approval.

Copies all normal tables from src -> dst preserving explicit ids. Recreates
events_fts via FTS5 'rebuild', copies indexes/views/triggers/sqlite_sequence,
runs integrity + raw-left + fk checks. SQLite copies no raw cell payloads, so the
new file reclaims the ~61GB.

Source MUST be quiesced (container stopped) — no concurrent writers.
Usage: REQUIRE_RECLAIM_OK=1 python build_slim.py <src.db> <dst.slim.db>
"""
import os
import sqlite3
import sys
import time

if os.environ.get("REQUIRE_RECLAIM_OK") != "1":
    raise SystemExit(
        "REFUSING: phase-e-build-slim is parked (unconditional raw sentinel is unsafe "
        "until workflow-subagent source_lines keying settles and the rebuild is made "
        "conditional). Set REQUIRE_RECLAIM_OK=1 only after the runbook gate is cleared."
    )

src_path, dst_path = sys.argv[1], sys.argv[2]

for path in (dst_path, dst_path + "-wal", dst_path + "-shm"):
    if os.path.exists(path):
        raise SystemExit(f"refusing to overwrite {path}")


def q(name):
    return '"' + name.replace('"', '""') + '"'


db = sqlite3.connect(f"file:{dst_path}?mode=rwc", uri=True)
db.row_factory = sqlite3.Row
db.execute("ATTACH DATABASE ? AS src", (f"file:{src_path}?mode=ro",))
db.execute("PRAGMA src.query_only=ON")

page_size = int(db.execute("PRAGMA src.page_size").fetchone()[0])
user_version = int(db.execute("PRAGMA src.user_version").fetchone()[0])
application_id = int(db.execute("PRAGMA src.application_id").fetchone()[0])

db.execute(f"PRAGMA page_size={page_size}")
db.execute(f"PRAGMA user_version={user_version}")
db.execute(f"PRAGMA application_id={application_id}")
db.execute("PRAGMA foreign_keys=OFF")
db.execute("PRAGMA journal_mode=OFF")
db.execute("PRAGMA synchronous=OFF")
db.execute("PRAGMA temp_store=FILE")

fts = db.execute(
    """
SELECT name, sql FROM src.sqlite_schema
WHERE type='table' AND sql LIKE 'CREATE VIRTUAL TABLE%USING fts5%'
ORDER BY name
"""
).fetchall()
fts_names = {row["name"] for row in fts}
if fts_names != {"events_fts"}:
    raise SystemExit(f"unexpected virtual tables: {sorted(fts_names)}")
fts_shadow = {f"{name}_{suffix}" for name in fts_names for suffix in ("data", "idx", "content", "docsize", "config")}

tables = db.execute(
    """
SELECT name, sql FROM src.sqlite_schema
WHERE type='table' AND sql IS NOT NULL AND name NOT LIKE 'sqlite_%'
ORDER BY name
"""
).fetchall()
normal_tables = [row for row in tables if row["name"] not in fts_names and row["name"] not in fts_shadow]

for row in normal_tables:
    db.execute(row["sql"])
for row in fts:
    db.execute(row["sql"])


def columns(table):
    return [row["name"] for row in db.execute(f"PRAGMA src.table_info({q(table)})")]


def expr(table, col):
    if table == "events" and col in {"raw_json", "raw_json_z"}:
        return "NULL"
    if table == "events" and col == "raw_json_codec":
        return "0"
    if table == "source_lines" and col == "raw_json":
        return "''"
    if table == "source_lines" and col == "raw_json_z":
        return "NULL"
    if table == "source_lines" and col == "raw_json_codec":
        return "0"
    return q(col)


for row in normal_tables:
    table = row["name"]
    cols = columns(table)
    col_list = ", ".join(q(col) for col in cols)
    expr_list = ", ".join(expr(table, col) for col in cols)
    start = time.time()
    db.execute("BEGIN")
    db.execute(f"INSERT INTO main.{q(table)} ({col_list}) SELECT {expr_list} FROM src.{q(table)}")
    db.commit()
    print(f"copied {table} in {time.time() - start:.1f}s", flush=True)

main_seq = db.execute("SELECT 1 FROM main.sqlite_schema WHERE name='sqlite_sequence'").fetchone()
src_seq = db.execute("SELECT 1 FROM src.sqlite_schema WHERE name='sqlite_sequence'").fetchone()
if main_seq and src_seq:
    db.execute("DELETE FROM main.sqlite_sequence")
    db.execute("INSERT INTO main.sqlite_sequence(name, seq) SELECT name, seq FROM src.sqlite_sequence")
    db.commit()

for typ in ("index", "view"):
    rows = db.execute(
        """
    SELECT name, tbl_name, sql FROM src.sqlite_schema
    WHERE type = ? AND sql IS NOT NULL AND name NOT LIKE 'sqlite_%'
    ORDER BY name
    """,
        (typ,),
    ).fetchall()
    for row in rows:
        if row["tbl_name"] in fts_shadow:
            continue
        db.execute(row["sql"])
    db.commit()

db.execute("INSERT INTO events_fts(events_fts) VALUES('rebuild')")
db.commit()

triggers = db.execute(
    "SELECT name, tbl_name, sql FROM src.sqlite_schema WHERE type='trigger' AND sql IS NOT NULL ORDER BY name"
).fetchall()
for row in triggers:
    db.execute(row["sql"])
db.commit()

db.execute("ANALYZE")
db.execute("PRAGMA optimize")
db.commit()

checks = {
    "events_count_delta": "SELECT (SELECT COUNT(*) FROM src.events) - (SELECT COUNT(*) FROM main.events)",
    "source_lines_count_delta": "SELECT (SELECT COUNT(*) FROM src.source_lines) - (SELECT COUNT(*) FROM main.source_lines)",
    "events_raw_left": "SELECT COUNT(*) FROM main.events WHERE raw_json IS NOT NULL OR raw_json_z IS NOT NULL OR raw_json_codec <> 0",
    "source_lines_raw_left": "SELECT COUNT(*) FROM main.source_lines WHERE raw_json <> '' OR raw_json_z IS NOT NULL OR raw_json_codec <> 0",
}
for label, sql in checks.items():
    value = db.execute(sql).fetchone()[0]
    print(f"{label}={value}", flush=True)
    if value != 0:
        raise SystemExit(f"check failed: {label}={value}")

fk = db.execute("PRAGMA foreign_key_check").fetchall()
if fk:
    raise SystemExit(f"foreign_key_check failed: {fk[:10]}")

integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
print(f"integrity_check={integrity}", flush=True)
if integrity != "ok":
    raise SystemExit(f"integrity_check failed: {integrity}")

db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
db.close()
print("=== SLIM BUILD OK ===", flush=True)
