"""Build a slim Longhouse DB from a quiesced source (Phase E reclaim).

CONDITIONAL, owner-aware raw reclaim. Copies all normal tables src -> dst
preserving explicit ids, and for events/source_lines sentinels raw columns ONLY
for rows whose exact bytes are archive-covered UNDER AN OWNER THE RUNTIME WILL
RESOLVE (the row's own session id, with child-subagent alias chunks folded onto
their parent via owner_map — mirrors archive_owning_session_ids). Rows not
provably covered KEEP their raw (active-session tails / residual gaps), so they
stay losslessly recoverable from the monolith. The build ABORTS if any
source_lines row has neither archive coverage nor raw (would be unrecoverable),
or if any sealed chunk is unreadable.

Option B: keeps raw column DEFINITIONS, writes sentinels — SQLite copies no
covered raw cell payloads so the new file reclaims the space. Recreates
events_fts via FTS5 'rebuild', copies indexes/views/triggers/sqlite_sequence.

Source MUST be quiesced (container stopped) — no concurrent writers.
Guarded by REQUIRE_RECLAIM_OK=1 + explicit approval (run via phase-e-reclaim.sh).
Usage: REQUIRE_RECLAIM_OK=1 python phase-e-build-slim.py <src.db> <dst.slim.db>
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


# Carry over page_size + user_version from the source for fidelity. page_size
# MUST be set before the first page write; do it on a brand-new connection
# before anything else touches the file.
src_probe = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
page_size = int(src_probe.execute("PRAGMA page_size").fetchone()[0])
user_version = int(src_probe.execute("PRAGMA user_version").fetchone()[0])
src_probe.close()

db = sqlite3.connect(f"file:{dst_path}?mode=rwc", uri=True)
db.row_factory = sqlite3.Row
db.execute(f"PRAGMA page_size={page_size}")
db.execute("PRAGMA journal_mode=WAL")  # establishes the file with the page_size
db.execute("PRAGMA foreign_keys=OFF")
db.execute("PRAGMA synchronous=OFF")
# Attach src read-only via the URI mode=ro (do NOT use PRAGMA query_only — it is
# connection-wide, not per-attached-db, and would make the dst read-only too).
db.execute("ATTACH DATABASE ? AS src", (f"file:{src_path}?mode=ro",))
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


# ── Conditional coverage (hatch rule, OWNER-AWARE) ───────────────────────────
# Sentinel raw ONLY for rows whose exact bytes are in the archive UNDER AN OWNER
# THE RUNTIME WILL ACTUALLY LOOK UNDER; KEEP raw for any row not provably covered
# (active-session tails, residual gaps). Coverage MUST match runtime resolution
# (archive_owning_session_ids): a row's owners = its own session_id PLUS the
# original child session ids preserved as longhouse_session_id aliases on child
# subagent threads (workflow relink rewrites the row's session_id to the parent
# but leaves chunks under the child id). Global key membership is NOT enough — an
# unrelated session sharing a key must not cause a sentinel that later fails to
# resolve.
import hashlib as _hashlib  # noqa: E402

from zerg.services.archive_store import FilesystemArchiveStore  # noqa: E402
from zerg.services.raw_json_compression import decompress_raw_json  # noqa: E402

archive_root = os.environ.get("LONGHOUSE_ARCHIVE_ROOT") or os.path.join(os.path.dirname(src_path), "archive")
_store = FilesystemArchiveStore(archive_root)

# owner_for[chunk_session_id] -> the runtime session_id that resolves it.
# A chunk session id resolves to itself OR, if it is a child alias, to the parent
# session that carries the longhouse_session_id alias for it.
db.execute("CREATE TEMP TABLE owner_map (chunk_sid TEXT PRIMARY KEY, owner_sid TEXT)")
# every session is its own owner
db.execute("INSERT OR IGNORE INTO owner_map SELECT id, id FROM src.sessions")
# child alias id -> parent session id (the thread's session_id)
db.execute(
    "INSERT OR REPLACE INTO owner_map (chunk_sid, owner_sid) "
    "SELECT a.alias_value, t.session_id FROM src.session_thread_aliases a "
    "JOIN src.session_threads t ON t.id = a.thread_id "
    "WHERE t.branch_kind='subagent' AND a.alias_kind='longhouse_session_id' AND a.alias_value <> ''"
)

# Covered keys tagged with the OWNER session id (resolved through owner_map).
db.execute("CREATE TEMP TABLE covered_sl (owner_sid TEXT, source_path TEXT, source_offset INTEGER, line_hash TEXT, "
           "PRIMARY KEY(owner_sid, source_path, source_offset, line_hash))")
db.execute("CREATE TEMP TABLE covered_ev (owner_sid TEXT, h TEXT, PRIMARY KEY(owner_sid, h))")


def _owner_of(chunk_sid):
    row = db.execute("SELECT owner_sid FROM owner_map WHERE chunk_sid=?", (str(chunk_sid),)).fetchone()
    return row[0] if row else str(chunk_sid)


for stream in ("source_lines", "events"):
    rows = db.execute("SELECT relative_path, session_id FROM src.archive_chunks WHERE stream=? AND state='sealed'", (stream,)).fetchall()
    n = 0
    skipped = 0
    for rp, csid in rows:
        try:
            recs = _store.read_chunk(rp)
        except Exception as e:
            skipped += 1
            print(f"WARN unreadable {stream} chunk {rp}: {e}", flush=True)
            continue
        owner = _owner_of(csid)
        if stream == "source_lines":
            ins = [
                (owner, rec.source_path, int(rec.source_offset), _hashlib.sha256(rec.raw_bytes).hexdigest())
                for rec in recs if rec.source_path is not None and rec.source_offset is not None
            ]
            if ins:
                db.executemany("INSERT OR IGNORE INTO covered_sl VALUES (?,?,?,?)", ins)
        else:
            db.executemany("INSERT OR IGNORE INTO covered_ev VALUES (?,?)",
                           [(owner, _hashlib.sha256(rec.raw_bytes).hexdigest()) for rec in recs])
        n += 1
        if n % 5000 == 0:
            db.commit()
            print(f"coverage {stream}: {n}/{len(rows)} chunks", flush=True)
    db.commit()
    if skipped:
        raise SystemExit(f"ABORT: {skipped} unreadable {stream} chunks — resolve before reclaim")
print(
    f"coverage built: covered_sl={db.execute('SELECT COUNT(*) FROM covered_sl').fetchone()[0]} "
    f"covered_ev={db.execute('SELECT COUNT(*) FROM covered_ev').fetchone()[0]}",
    flush=True,
)


def columns(table):
    return [row["name"] for row in db.execute(f"PRAGMA src.table_info({q(table)})")]


def expr(table, col):
    # Non-raw columns copy verbatim. Raw columns are handled by the conditional
    # copy in copy_table() for events/source_lines, not here.
    return q(col)


CODEC_ZSTD = 1
CODEC_PLAIN = 0
kept_raw = {"source_lines": 0, "events": 0}


def copy_source_lines():
    # Conditional: sentinel raw only where (source_path, source_offset, line_hash)
    # is covered in the archive; otherwise keep raw verbatim. Pure SQL via a LEFT
    # JOIN to covered_sl.
    cols = columns("source_lines")
    main_cols = ", ".join(q(c) for c in cols)
    sel = []
    for c in cols:
        if c == "raw_json":
            sel.append("CASE WHEN cov.line_hash IS NOT NULL THEN '' ELSE s.raw_json END")
        elif c == "raw_json_z":
            sel.append("CASE WHEN cov.line_hash IS NOT NULL THEN NULL ELSE s.raw_json_z END")
        elif c == "raw_json_codec":
            sel.append("CASE WHEN cov.line_hash IS NOT NULL THEN 0 ELSE s.raw_json_codec END")
        else:
            sel.append(f"s.{q(c)}")
    # Coverage join is OWNER-AWARE: a row is covered only if a chunk owned by the
    # row's own session_id carries the same (path, offset, line_hash). owner_map
    # already folded child-alias chunks onto their parent owner, so matching
    # cov.owner_sid = s.session_id mirrors runtime archive_owning_session_ids.
    join = ("LEFT JOIN covered_sl cov ON cov.owner_sid = s.session_id "
            "AND cov.source_path = s.source_path AND cov.source_offset = s.source_offset "
            "AND cov.line_hash = s.line_hash")
    db.execute("BEGIN")
    db.execute(f"INSERT INTO main.source_lines ({main_cols}) SELECT {', '.join(sel)} FROM src.source_lines s {join}")
    kept = db.execute(
        f"SELECT COUNT(*) FROM src.source_lines s {join} "
        "WHERE cov.line_hash IS NULL AND (s.raw_json_z IS NOT NULL OR (s.raw_json IS NOT NULL AND s.raw_json <> ''))"
    ).fetchone()[0]
    kept_raw["source_lines"] = int(kept)
    # SAFETY GATE (#2): a source_lines row that is NOT covered AND has no raw to
    # keep is unrecoverable — fail closed. (Provider source lines are never
    # genuinely empty, so '' + no archive = data loss.)
    orphan = db.execute(
        f"SELECT COUNT(*) FROM src.source_lines s {join} "
        "WHERE cov.line_hash IS NULL AND s.raw_json_z IS NULL AND (s.raw_json IS NULL OR s.raw_json = '')"
    ).fetchone()[0]
    db.commit()
    if orphan:
        raise SystemExit(f"ABORT: {orphan} source_lines rows have no archive coverage AND no raw — would be unrecoverable")


def copy_events():
    # Conditional: events coverage is sha256(raw bytes); raw is zstd in
    # raw_json_z, which SQL cannot hash — so copy row-by-row in Python, sentinel
    # only when the decoded raw's hash is covered, else keep raw verbatim.
    cols = columns("events")
    placeholders = ", ".join("?" for _ in cols)
    col_list = ", ".join(q(c) for c in cols)
    ins = f"INSERT INTO main.events ({col_list}) VALUES ({placeholders})"
    idx = {c: i for i, c in enumerate(cols)}
    cur = db.execute(f"SELECT {col_list} FROM src.events")
    db.execute("BEGIN")
    batch = []
    while True:
        rows = cur.fetchmany(5000)
        if not rows:
            break
        for r in rows:
            r = list(r)
            rj, rz, codec = r[idx["raw_json"]], r[idx["raw_json_z"]], r[idx["raw_json_codec"]] or 0
            sid = str(r[idx["session_id"]])
            raw = None
            if codec == CODEC_ZSTD and rz is not None:
                raw = decompress_raw_json(rz)
            elif rj:
                raw = rj
            if raw:
                h = _hashlib.sha256(raw.encode("utf-8")).hexdigest()
                # Owner-aware: covered only under THIS row's session_id (owner_map
                # already folded child-alias chunks onto the parent owner).
                covered = db.execute("SELECT 1 FROM covered_ev WHERE owner_sid=? AND h=? LIMIT 1", (sid, h)).fetchone() is not None
                if covered:
                    r[idx["raw_json"]] = None
                    r[idx["raw_json_z"]] = None
                    r[idx["raw_json_codec"]] = 0
                else:
                    kept_raw["events"] += 1
            batch.append(r)
            if len(batch) >= 5000:
                db.executemany(ins, batch)
                batch = []
    if batch:
        db.executemany(ins, batch)
    db.commit()


for row in normal_tables:
    table = row["name"]
    start = time.time()
    if table == "source_lines":
        copy_source_lines()
    elif table == "events":
        copy_events()
    else:
        cols = columns(table)
        col_list = ", ".join(q(col) for col in cols)
        db.execute("BEGIN")
        db.execute(f"INSERT INTO main.{q(table)} ({col_list}) SELECT {col_list} FROM src.{q(table)}")
        db.commit()
    print(f"copied {table} in {time.time() - start:.1f}s", flush=True)

print(f"kept raw (uncovered rows): source_lines={kept_raw['source_lines']} events={kept_raw['events']}", flush=True)

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

# Row conservation: every src row must be present in the slim DB (no row lost).
# These MUST be zero.
zero_checks = {
    "events_count_delta": "SELECT (SELECT COUNT(*) FROM src.events) - (SELECT COUNT(*) FROM main.events)",
    "source_lines_count_delta": "SELECT (SELECT COUNT(*) FROM src.source_lines) - (SELECT COUNT(*) FROM main.source_lines)",
}
for label, sql in zero_checks.items():
    value = db.execute(sql).fetchone()[0]
    print(f"{label}={value}", flush=True)
    if value != 0:
        raise SystemExit(f"check failed: {label}={value}")

# Raw-left is NO LONGER required to be zero: the conditional rebuild intentionally
# KEEPS raw for rows not provably archive-covered (active-session tails / residual
# gaps), so they remain losslessly recoverable from the monolith. Report the
# counts and assert they equal the kept_raw counts tracked during copy — i.e. the
# only rows still carrying raw are exactly the ones we deliberately kept.
sl_raw_left = db.execute(
    "SELECT COUNT(*) FROM main.source_lines WHERE raw_json_z IS NOT NULL OR (raw_json IS NOT NULL AND raw_json <> '')"
).fetchone()[0]
ev_raw_left = db.execute(
    "SELECT COUNT(*) FROM main.events WHERE raw_json_z IS NOT NULL OR (raw_json IS NOT NULL AND raw_json <> '')"
).fetchone()[0]
print(f"raw_left source_lines={sl_raw_left} (kept={kept_raw['source_lines']}) events={ev_raw_left} (kept={kept_raw['events']})", flush=True)
if sl_raw_left != kept_raw["source_lines"]:
    raise SystemExit(f"source_lines raw_left {sl_raw_left} != kept {kept_raw['source_lines']}")
if ev_raw_left != kept_raw["events"]:
    raise SystemExit(f"events raw_left {ev_raw_left} != kept {kept_raw['events']}")

fk = db.execute("PRAGMA foreign_key_check").fetchall()
if fk:
    raise SystemExit(f"foreign_key_check failed: {fk[:10]}")

integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
print(f"integrity_check={integrity}", flush=True)
if integrity != "ok":
    raise SystemExit(f"integrity_check failed: {integrity}")

db.execute(f"PRAGMA user_version={user_version}")
db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
db.close()
print("=== SLIM BUILD OK ===", flush=True)
