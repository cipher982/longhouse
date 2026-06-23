"""Local test of the Phase E conditional slim-rebuild script.

Builds a tiny source DB + archive where SOME rows are archive-covered and some
are not, runs scripts/ops/phase-e-build-slim.py, and asserts:
  - row counts conserved (no row lost),
  - covered rows have raw sentinel'd (NULL/''),
  - uncovered rows KEEP their raw bytes (active-tail safety),
  - events_fts rebuilt and queryable.
"""

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

import sqlite3

from zerg.database import Base
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSourceLine
from zerg.models.agents import AgentSession
from zerg.services.archive_store import ArchiveRecord
from zerg.services.archive_store import FilesystemArchiveStore
from zerg.services.archive_shadow import insert_archive_chunk_manifests
from zerg.services.raw_json_compression import compress_raw_json, CODEC_ZSTD
from datetime import datetime, timezone

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ops" / "phase-e-build-slim.py"


def test_conditional_rebuild_keeps_uncovered_raw(tmp_path):
    src = tmp_path / "src.db"
    archive = tmp_path / "archive"
    engine = make_engine(f"sqlite:///{src}")
    initialize_database(engine)  # full schema incl events_fts
    SL = make_sessionmaker(engine)

    sid = "aaaaaaaa-0000-0000-0000-000000000001"
    covered_raw = '{"type":"user","content":"COVERED line"}'
    uncovered_raw = '{"type":"user","content":"UNCOVERED active-tail line"}'
    ev_covered = '{"type":"assistant","content":"covered event"}'
    ev_uncovered = '{"type":"assistant","content":"uncovered event"}'

    with SL() as db:
        db.add(AgentSession(id=sid, provider="claude", environment="production", started_at=datetime.now(timezone.utc)))
        db.flush()
        # source_lines: one covered, one not
        for off, raw in ((0, covered_raw), (100, uncovered_raw)):
            db.add(AgentSourceLine(
                session_id=sid, source_path="/t/s.jsonl", source_offset=off, branch_id=1, revision=1,
                is_branch_copy=0, raw_json="", raw_json_z=compress_raw_json(raw), raw_json_codec=CODEC_ZSTD,
                line_hash=hashlib.sha256(raw.encode()).hexdigest(),
            ))
        # events: one covered, one not
        for off, raw in ((0, ev_covered), (200, ev_uncovered)):
            db.add(AgentEvent(
                session_id=sid, role="assistant", content_text="x", timestamp=datetime.now(timezone.utc),
                source_path="/t/s.jsonl", source_offset=off, raw_json=None, raw_json_z=compress_raw_json(raw),
                raw_json_codec=CODEC_ZSTD, event_hash=hashlib.sha256(raw.encode()).hexdigest(),
            ))
        db.flush()
        # Archive ONLY the covered rows.
        store = FilesystemArchiveStore(archive)
        sl_chunks = store.write_record_chunks([
            ArchiveRecord(tenant_id="default", session_id=sid, stream="source_lines", source_seq=1,
                          raw_bytes=covered_raw.encode(), source_path="/t/s.jsonl", source_offset=0)
        ], target_uncompressed_bytes=1 << 20)
        ev_chunks = store.write_record_chunks([
            ArchiveRecord(tenant_id="default", session_id=sid, stream="events", source_seq=1,
                          raw_bytes=ev_covered.encode(), source_path="/t/s.jsonl", source_offset=0)
        ], target_uncompressed_bytes=1 << 20)
        insert_archive_chunk_manifests(db, sl_chunks + ev_chunks)
        db.commit()
    engine.dispose()

    slim = tmp_path / "slim.db"
    env = dict(os.environ, REQUIRE_RECLAIM_OK="1", LONGHOUSE_ARCHIVE_ROOT=str(archive))
    r = subprocess.run([sys.executable, str(SCRIPT), str(src), str(slim)], env=env, capture_output=True, text=True)
    assert r.returncode == 0, f"build failed:\n{r.stdout}\n{r.stderr}"
    assert "quick_check=ok" in r.stdout, r.stdout
    assert "integrity_check=" not in r.stdout, r.stdout
    assert "SLIM BUILD OK" in r.stdout, r.stdout

    c = sqlite3.connect(slim)
    try:
        # Row conservation.
        assert c.execute("SELECT COUNT(*) FROM source_lines").fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
        # Covered source line -> raw dropped; uncovered -> raw kept.
        cov = c.execute("SELECT raw_json, raw_json_z FROM source_lines WHERE source_offset=0").fetchone()
        unc = c.execute("SELECT raw_json, raw_json_z FROM source_lines WHERE source_offset=100").fetchone()
        assert cov[1] is None and (cov[0] or "") == "", f"covered SL should be sentinel'd: {cov}"
        assert unc[1] is not None, f"uncovered SL must keep raw: {unc}"
        # Covered event -> raw dropped; uncovered -> raw kept.
        cove = c.execute("SELECT raw_json_z FROM events WHERE source_offset=0").fetchone()
        unce = c.execute("SELECT raw_json_z FROM events WHERE source_offset=200").fetchone()
        assert cove[0] is None, "covered event should be sentinel'd"
        assert unce[0] is not None, "uncovered event must keep raw"
        # FTS rebuilt.
        c.execute("SELECT COUNT(*) FROM events_fts")
    finally:
        c.close()


def test_owner_aware_does_not_sentinel_on_foreign_session_match(tmp_path):
    """A row must NOT be sentinel'd just because an UNRELATED session has a chunk
    with the same (path, offset, line_hash). Coverage is owner-scoped, so a row
    whose own session has no chunk keeps its raw even if a foreign chunk matches."""
    src = tmp_path / "src.db"
    archive = tmp_path / "archive"
    engine = make_engine(f"sqlite:///{src}")
    initialize_database(engine)
    SL = make_sessionmaker(engine)

    sid = str(uuid4())
    foreign = str(uuid4())
    raw = '{"type":"user","content":"shared bytes"}'
    lh = hashlib.sha256(raw.encode()).hexdigest()

    with SL() as db:
        for s in (sid, foreign):
            db.add(AgentSession(id=s, provider="claude", environment="production", started_at=datetime.now(timezone.utc)))
            db.flush()
        # The row belongs to `sid`...
        db.add(AgentSourceLine(session_id=sid, source_path="/t/s.jsonl", source_offset=0, branch_id=1, revision=1,
                               is_branch_copy=0, raw_json="", raw_json_z=compress_raw_json(raw), raw_json_codec=CODEC_ZSTD,
                               line_hash=lh))
        db.flush()
        # ...but the only archive chunk with these bytes is owned by `foreign`.
        store = FilesystemArchiveStore(archive)
        chunks = store.write_record_chunks([
            ArchiveRecord(tenant_id="default", session_id=foreign, stream="source_lines", source_seq=1,
                          raw_bytes=raw.encode(), source_path="/t/s.jsonl", source_offset=0)
        ], target_uncompressed_bytes=1 << 20)
        insert_archive_chunk_manifests(db, chunks)
        db.commit()
    engine.dispose()

    slim = tmp_path / "slim.db"
    env = dict(os.environ, REQUIRE_RECLAIM_OK="1", LONGHOUSE_ARCHIVE_ROOT=str(archive))
    r = subprocess.run([sys.executable, str(SCRIPT), str(src), str(slim)], env=env, capture_output=True, text=True)
    assert r.returncode == 0, f"build failed:\n{r.stdout}\n{r.stderr}"
    c = sqlite3.connect(slim)
    try:
        row = c.execute("SELECT raw_json_z FROM source_lines WHERE source_offset=0").fetchone()
        assert row[0] is not None, "row must KEEP raw — foreign-session match must not sentinel it"
    finally:
        c.close()
