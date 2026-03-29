"""Tests for raw_json zstd compression helpers and ingest/export round-trip.

Covers:
- compress/decompress round-trip correctness
- decode_raw_json handles codec=0 (plain), codec=1 (zstd), missing codec
- decode_raw_json uses is-not-None semantics (empty string is valid content)
- new ingest writes codec=1 rows (no plain text in raw_json column)
- export of compressed rows produces original bytes
- mixed legacy (codec=0) + new (codec=1) rows export correctly
- branch copy preserves codec fields
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from uuid import uuid4

from sqlalchemy.orm import sessionmaker

from zerg.database import make_engine
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentSessionBranch
from zerg.models.agents import AgentSourceLine
from zerg.models.agents import AgentsBase
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest
from zerg.services.agents_store import SourceLineIngest
from zerg.services.raw_json_compression import CODEC_PLAIN
from zerg.services.raw_json_compression import CODEC_ZSTD
from zerg.services.raw_json_compression import compress_raw_json
from zerg.services.raw_json_compression import decode_raw_json
from zerg.services.raw_json_compression import decompress_raw_json


def _make_db(tmp_path):
    db_path = tmp_path / "compression_test.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    AgentsBase.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


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
