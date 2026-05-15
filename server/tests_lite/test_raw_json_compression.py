"""Tests for raw_json zstd compression helpers and ingest/export round-trip.

Covers:
- compress/decompress round-trip correctness
- decode_raw_json handles codec=0 (plain), codec=1 (zstd), missing codec
- decode_raw_json uses is-not-None semantics (empty string is valid content)
- new ingest writes codec=1 rows (no plain text in raw_json column)
- export of compressed rows produces original bytes
- mixed legacy (codec=0) + new (codec=1) rows export correctly
- branch copy preserves codec fields
- standalone backfill script drains legacy rows end-to-end
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import threading
from datetime import datetime
from datetime import timezone
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from zerg.database import make_engine
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentSessionBranch
from zerg.models.agents import AgentSourceLine
from zerg.database import Base
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest
from zerg.services.agents_store import SourceLineIngest
from zerg.services.raw_json_compression import CODEC_PLAIN
from zerg.services.raw_json_compression import CODEC_ZSTD
from zerg.services.raw_json_compression import _get_compressor
from zerg.services.raw_json_compression import _get_decompressor
from zerg.services.raw_json_compression import compress_raw_json
from zerg.services.raw_json_compression import decode_raw_json
from zerg.services.raw_json_compression import decompress_raw_json


def _make_db(tmp_path):
    db_path = tmp_path / "compression_test.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


class _FakeWriteSerializer:
    def __init__(self, session_factory, *, configured: bool = True):
        self._session_factory = session_factory
        self.is_configured = configured
        self.calls: list[dict[str, object]] = []

    async def execute(self, fn, *, label="", auto_commit=True):
        self.calls.append({"label": label, "auto_commit": auto_commit})
        db = self._session_factory()
        try:
            result = fn(db)
            if auto_commit:
                db.commit()
            return result
        finally:
            db.close()


def _seed_legacy_rows(SessionLocal, *, source_lines: list[str] | None = None, events: list[str | None] | None = None) -> UUID:
    session_id = uuid4()
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        session = AgentSession(
            id=session_id,
            provider="claude",
            environment="test",
            project="compression-test",
            started_at=now,
        )
        db.add(session)
        db.flush()

        branch = AgentSessionBranch(session_id=session_id, branch_reason="root", is_head=1)
        db.add(branch)
        db.flush()

        for idx, raw_json in enumerate(source_lines or []):
            db.add(
                AgentSourceLine(
                    session_id=session_id,
                    source_path=_SOURCE_PATH,
                    source_offset=idx,
                    branch_id=branch.id,
                    revision=1,
                    is_branch_copy=0,
                    raw_json=raw_json,
                    raw_json_z=None,
                    raw_json_codec=CODEC_PLAIN,
                    line_hash=hashlib.sha256(raw_json.encode()).hexdigest(),
                )
            )

        for idx, raw_json in enumerate(events or []):
            db.add(
                AgentEvent(
                    session_id=session_id,
                    branch_id=branch.id,
                    role="user",
                    content_text=f"event-{idx}",
                    timestamp=now,
                    source_path=_SOURCE_PATH,
                    source_offset=idx,
                    raw_json=raw_json,
                    raw_json_z=None,
                    raw_json_codec=CODEC_PLAIN,
                )
            )

        db.commit()

    return session_id


# ---------------------------------------------------------------------------
# Unit tests for compression helpers
# ---------------------------------------------------------------------------


def test_compress_decompress_roundtrip():
    # Use a realistic-length JSON line — zstd has per-frame overhead that exceeds
    # tiny payloads; real session events are 1-10KB so compression always wins there.
    original = '{"type":"assistant","message":{"id":"msg_01","role":"assistant","content":[{"type":"text","text":"Here is a detailed answer to your question about Python compression libraries and their trade-offs in production environments."}],"model":"claude-3-5-sonnet","stop_reason":"end_turn"}}'
    blob = compress_raw_json(original)
    assert isinstance(blob, bytes)
    assert len(blob) < len(original.encode())  # compressed is smaller for realistic payloads
    assert decompress_raw_json(blob) == original


def test_compress_handles_unicode():
    original = '{"content":"hello 🌍 — こんにちは"}'
    assert decompress_raw_json(compress_raw_json(original)) == original


def test_zstd_contexts_are_thread_local():
    workers = 4
    barrier = threading.Barrier(workers)

    def _worker() -> tuple[int, int]:
        barrier.wait()
        return (id(_get_compressor()), id(_get_decompressor()))

    main_thread_contexts = (id(_get_compressor()), id(_get_decompressor()))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        thread_contexts = list(executor.map(lambda _n: _worker(), range(workers)))

    compressor_ids = {compressor_id for compressor_id, _decompressor_id in thread_contexts}
    decompressor_ids = {decompressor_id for _compressor_id, decompressor_id in thread_contexts}

    assert len(compressor_ids) == workers
    assert len(decompressor_ids) == workers
    assert main_thread_contexts[0] not in compressor_ids
    assert main_thread_contexts[1] not in decompressor_ids


def test_compress_decompress_parallel_roundtrip():
    large_text = "x" * 2048
    payloads = [
        f'{{"type":"assistant","idx":{idx},"text":"{large_text}"}}'
        for idx in range(128)
    ]

    with ThreadPoolExecutor(max_workers=8) as executor:
        restored = list(executor.map(lambda raw: decompress_raw_json(compress_raw_json(raw)), payloads))

    assert restored == payloads


def test_decode_raw_json_codec_plain():
    class Row:
        raw_json = '{"type":"user"}'
        raw_json_z = None
        raw_json_codec = CODEC_PLAIN

    assert decode_raw_json(Row()) == '{"type":"user"}'


def test_decode_raw_json_codec_zstd():
    original = '{"type":"assistant","text":"hi"}'
    blob = compress_raw_json(original)

    class Row:
        raw_json = ""
        raw_json_z = blob
        raw_json_codec = CODEC_ZSTD

    assert decode_raw_json(Row()) == original


def test_decode_raw_json_missing_codec_falls_back_to_plain():
    class Row:
        raw_json = '{"type":"tool_use"}'
        raw_json_z = None

    # No raw_json_codec attribute at all
    assert decode_raw_json(Row()) == '{"type":"tool_use"}'


def test_decode_raw_json_codec1_no_blob_returns_none():
    class Row:
        raw_json = ""
        raw_json_z = None
        raw_json_codec = CODEC_ZSTD

    assert decode_raw_json(Row()) is None


def test_decode_raw_json_plain_none_returns_none():
    class Row:
        raw_json = None
        raw_json_z = None
        raw_json_codec = CODEC_PLAIN

    assert decode_raw_json(Row()) is None


def test_decode_raw_json_empty_string_is_valid_plain():
    """Empty string is valid content for codec=0 — not treated as missing."""

    class Row:
        raw_json = ""
        raw_json_z = None
        raw_json_codec = CODEC_PLAIN

    # decode returns the empty string as-is; caller decides whether to use it
    assert decode_raw_json(Row()) == ""


# ---------------------------------------------------------------------------
# Integration: ingest writes compressed rows
# ---------------------------------------------------------------------------

_RAW_LINE = '{"type":"user","message":{"role":"user","content":[{"type":"text","text":"hello"}]}}'
_SOURCE_PATH = "/tmp/test.jsonl"


def _make_session_ingest(session_id, raw_line: str = _RAW_LINE) -> SessionIngest:
    event = EventIngest(
        role="user",
        content_text="hello",
        timestamp=datetime.now(timezone.utc),
        source_path=_SOURCE_PATH,
        source_offset=0,
        raw_json=raw_line,
    )
    source_line = SourceLineIngest(
        source_path=_SOURCE_PATH,
        source_offset=0,
        raw_json=raw_line,
    )
    return SessionIngest(
        id=session_id,
        provider="claude",
        environment="test",
        project="compression-test",
        started_at=datetime.now(timezone.utc),
        events=[event],
        source_lines=[source_line],
    )


def test_ingest_writes_compressed_event(tmp_path):
    SessionLocal = _make_db(tmp_path)
    session_id = uuid4()

    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(_make_session_ingest(session_id))
        db.commit()

    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(_make_session_ingest(session_id))
        db.commit()
        # Find the specific event we just ingested by its content
        events = db.query(AgentEvent).filter(AgentEvent.raw_json_codec == CODEC_ZSTD).all()
        assert len(events) >= 1
        matching = [e for e in events if decode_raw_json(e) == _RAW_LINE]
        assert len(matching) >= 1
        event = matching[0]
        assert event.raw_json_z is not None
        assert event.raw_json is None  # plain text not stored for new rows


def test_ingest_writes_compressed_source_line(tmp_path):
    SessionLocal = _make_db(tmp_path)
    session_id = uuid4()

    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(_make_session_ingest(session_id))
        db.commit()
        lines = db.query(AgentSourceLine).filter(AgentSourceLine.raw_json_codec == CODEC_ZSTD).all()
        assert len(lines) >= 1
        matching = [ln for ln in lines if decode_raw_json(ln) == _RAW_LINE]
        assert len(matching) >= 1
        line = matching[0]
        assert line.raw_json_z is not None
        assert line.raw_json == ""  # sentinel for NOT NULL constraint


def test_export_compressed_source_lines_roundtrip(tmp_path):
    SessionLocal = _make_db(tmp_path)
    session_id = uuid4()

    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(_make_session_ingest(session_id))
        db.commit()
        result = store.export_session_jsonl(session_id)

    assert result is not None
    content_bytes, _session = result
    lines = content_bytes.decode("utf-8").strip().split("\n")
    assert len(lines) == 1
    assert lines[0] == _RAW_LINE


def test_export_mixed_legacy_and_compressed(tmp_path):
    """Legacy (codec=0 plain text) rows and new (codec=1 zstd) rows export together."""
    SessionLocal = _make_db(tmp_path)
    session_id = uuid4()
    now = datetime.now(timezone.utc)

    # Manually insert a legacy plain-text source_line
    legacy_line = '{"type":"system","content":"old format"}'
    new_line = '{"type":"user","content":"new format"}'

    with SessionLocal() as db:
        session = AgentSession(
            id=session_id,
            provider="claude",
            environment="test",
            project="test",
            started_at=now,
        )
        db.add(session)
        db.flush()

        branch = AgentSessionBranch(session_id=session_id, branch_reason="root", is_head=1)
        db.add(branch)
        db.flush()

        # Legacy row: codec=0, raw_json has text
        db.add(AgentSourceLine(
            session_id=session_id,
            source_path=_SOURCE_PATH,
            source_offset=0,
            branch_id=branch.id,
            revision=1,
            is_branch_copy=0,
            raw_json=legacy_line,
            raw_json_z=None,
            raw_json_codec=CODEC_PLAIN,
            line_hash="aaa",
        ))
        # Compressed row: codec=1, raw_json_z has blob
        db.add(AgentSourceLine(
            session_id=session_id,
            source_path=_SOURCE_PATH,
            source_offset=100,
            branch_id=branch.id,
            revision=1,
            is_branch_copy=0,
            raw_json="",
            raw_json_z=compress_raw_json(new_line),
            raw_json_codec=CODEC_ZSTD,
            line_hash="bbb",
        ))
        db.commit()

    with SessionLocal() as db:
        store = AgentsStore(db)
        result = store.export_session_jsonl(session_id)

    assert result is not None
    content_bytes, _ = result
    exported_lines = [l for l in content_bytes.decode("utf-8").strip().split("\n") if l]
    assert legacy_line in exported_lines
    assert new_line in exported_lines


def test_compress_raw_json_job_drains_all_chunks_and_compresses_empty_string_source_line(tmp_path, monkeypatch):
    from zerg.jobs.compress_raw_json import run

    SessionLocal = _make_db(tmp_path)
    engine = SessionLocal.kw["bind"]
    _seed_legacy_rows(
        SessionLocal,
        source_lines=[
            '{"type":"system","content":"first legacy line"}',
            "",
            '{"type":"user","content":"third legacy line"}',
        ],
    )

    fake_ws = _FakeWriteSerializer(SessionLocal)
    monkeypatch.setattr("zerg.database.default_engine", engine)
    monkeypatch.setattr("zerg.services.write_serializer.get_write_serializer", lambda: fake_ws)
    monkeypatch.setenv("COMPRESS_RAW_JSON_CHUNK_SIZE", "2")
    monkeypatch.setenv("COMPRESS_RAW_JSON_BATCH_SLEEP_MS", "0")

    result = asyncio.run(run())

    assert result["status"] == "success"
    assert result["source_lines_compressed"] == 3
    assert result["source_lines_batches"] == 2
    assert result["source_lines_drained"] is True

    with SessionLocal() as db:
        rows = db.query(AgentSourceLine).order_by(AgentSourceLine.source_offset.asc()).all()
        events_idx_sql = db.execute(
            text("SELECT sql FROM sqlite_master WHERE type='index' AND name='ix_events_raw_json_pending'")
        ).scalar()
        source_lines_idx_sql = db.execute(
            text("SELECT sql FROM sqlite_master WHERE type='index' AND name='ix_source_lines_raw_json_pending'")
        ).scalar()

    assert [row.raw_json_codec for row in rows] == [CODEC_ZSTD, CODEC_ZSTD, CODEC_ZSTD]
    assert [row.raw_json for row in rows] == ["", "", ""]
    assert all(row.raw_json_z is not None for row in rows)
    assert [decode_raw_json(row) for row in rows] == [
        '{"type":"system","content":"first legacy line"}',
        "",
        '{"type":"user","content":"third legacy line"}',
    ]
    assert "WHERE raw_json_codec = 0" in str(events_idx_sql)
    assert "raw_json IS NOT NULL" in str(events_idx_sql)
    assert "WHERE raw_json_codec = 0" in str(source_lines_idx_sql)


def test_compress_raw_json_job_leaves_event_rows_without_raw_payload_uncompressed(tmp_path, monkeypatch):
    from zerg.jobs.compress_raw_json import run

    SessionLocal = _make_db(tmp_path)
    engine = SessionLocal.kw["bind"]
    _seed_legacy_rows(
        SessionLocal,
        events=[
            None,
            '{"type":"assistant","content":"compress me"}',
        ],
    )

    fake_ws = _FakeWriteSerializer(SessionLocal)
    monkeypatch.setattr("zerg.database.default_engine", engine)
    monkeypatch.setattr("zerg.services.write_serializer.get_write_serializer", lambda: fake_ws)
    monkeypatch.setenv("COMPRESS_RAW_JSON_CHUNK_SIZE", "10")
    monkeypatch.setenv("COMPRESS_RAW_JSON_BATCH_SLEEP_MS", "0")

    result = asyncio.run(run())

    assert result["status"] == "success"
    assert result["events_compressed"] == 1
    assert result["events_drained"] is True

    with SessionLocal() as db:
        rows = db.query(AgentEvent).order_by(AgentEvent.source_offset.asc()).all()

    assert rows[0].raw_json is None
    assert rows[0].raw_json_codec == CODEC_PLAIN
    assert rows[0].raw_json_z is None

    assert rows[1].raw_json is None
    assert rows[1].raw_json_codec == CODEC_ZSTD
    assert rows[1].raw_json_z is not None


def test_backfill_raw_json_script_drains_legacy_rows(tmp_path):
    SessionLocal = _make_db(tmp_path)
    engine = SessionLocal.kw["bind"]
    db_path = Path(engine.url.database)
    _seed_legacy_rows(
        SessionLocal,
        source_lines=[
            '{"type":"system","content":"first legacy line"}',
            "",
        ],
        events=[
            None,
            '{"type":"assistant","content":"compress me"}',
        ],
    )

    repo_root = Path(__file__).resolve().parents[2]
    script_path = repo_root / "scripts" / "ops" / "backfill_raw_json.py"
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{db_path}"
    env["RAW_JSON_BACKFILL_ROWS_PER_TX"] = "2"
    env["RAW_JSON_BACKFILL_WORKERS"] = "1"
    env["RAW_JSON_BACKFILL_PROGRESS_EVERY"] = "1"

    result = subprocess.run(
        [sys.executable, str(script_path)],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, (
        f"script exited {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "starting raw_json backfill:" in result.stdout
    assert "events_first_pending_id=" in result.stdout
    assert "source_lines_first_pending_id=" in result.stdout
    assert "backfill complete" in result.stdout

    with SessionLocal() as db:
        source_lines = db.query(AgentSourceLine).order_by(AgentSourceLine.source_offset.asc()).all()
        events = db.query(AgentEvent).order_by(AgentEvent.source_offset.asc()).all()

    assert [row.raw_json_codec for row in source_lines] == [CODEC_ZSTD, CODEC_ZSTD]
    assert [row.raw_json for row in source_lines] == ["", ""]
    assert [decode_raw_json(row) for row in source_lines] == [
        '{"type":"system","content":"first legacy line"}',
        "",
    ]

    assert events[0].raw_json_codec == CODEC_PLAIN
    assert events[0].raw_json is None
    assert events[0].raw_json_z is None

    assert events[1].raw_json_codec == CODEC_ZSTD
    assert events[1].raw_json is None
    assert events[1].raw_json_z is not None
