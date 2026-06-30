"""Hermetic tests for the Cursor transcript decoder.

Builds synthetic store.db fixtures in temp dirs with ephemeral content (no
real secrets). Validates the current cursor-agent format: ordered JSON
message blobs, tool-call/tool-result pairing by toolCallId, reasoning
preservation, and typed unsupported-gap reporting for the legacy chunked
format and missing-root cases.
"""

from __future__ import annotations

import json
import os as _os
import sqlite3
from pathlib import Path

from cryptography.fernet import Fernet

_os.environ.setdefault("DATABASE_URL", "sqlite://")
_os.environ.setdefault("TESTING", "1")
_os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
_os.environ.setdefault("JWT_SECRET", "test-jwt-secret-long-enough")
_os.environ.setdefault("INTERNAL_API_SECRET", "test-internal-secret-long-enough")
_os.environ.setdefault("AUTH_DISABLED", "1")

from zerg.services.cursor_transcript import GAP_EMPTY_SESSION
from zerg.services.cursor_transcript import GAP_LEGACY_CHUNKED
from zerg.services.cursor_transcript import GAP_MISSING_ROOT
from zerg.services.cursor_transcript import decode_store_db
from zerg.services.cursor_transcript import ingest_cursor_store_db
from zerg.services.cursor_transcript import iter_local_cursor_session_summaries
from zerg.services.cursor_transcript import peek_cursor_session


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _field(field_num: int, wire: int, payload: bytes) -> bytes:
    tag = (field_num << 3) | wire
    if wire == 2:
        return _varint(tag) + _varint(len(payload)) + payload
    return _varint(tag) + payload


def _build_root(message_ids: list[bytes], workspace: str | None = None) -> bytes:
    parts = []
    for mid in message_ids:
        parts.append(_field(1, 2, mid))
    if workspace:
        parts.append(_field(9, 2, ("file://" + workspace).encode("utf-8")))
    return b"".join(parts)


def _write_store(
    dir_path: Path,
    *,
    agent_id: str,
    created_at_ms: int,
    updated_at_ms: int,
    messages: list[dict],
    title: str = "Test Cursor Session",
    model: str = "glm-5.2",
    workspace: str | None = "/tmp/test-workspace",
) -> Path:
    """Create a synthetic current-format store.db + meta.json and return its path."""
    dir_path.mkdir(parents=True, exist_ok=True)
    store_path = dir_path / "store.db"
    con = sqlite3.connect(str(store_path))
    con.execute("create table meta (key text primary key, value text)")
    con.execute("create table blobs (id text primary key, data blob)")

    blob_rows: list[tuple[str, bytes]] = []
    message_ids: list[bytes] = []
    for msg in messages:
        data = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        import hashlib

        bid = hashlib.sha256(data).digest()
        blob_rows.append((bid.hex(), data))
        message_ids.append(bid)

    root = _build_root(message_ids, workspace=workspace)
    import hashlib as _h

    root_id = _h.sha256(root).digest()
    blob_rows.append((root_id.hex(), root))

    meta_obj = {
        "agentId": agent_id,
        "latestRootBlobId": root_id.hex(),
        "name": title,
        "mode": "default",
        "isRunEverything": True,
        "approvalMode": "unrestricted",
        "createdAt": created_at_ms,
        "lastUsedModel": model,
    }
    con.execute("insert into meta (key, value) values (?, ?)", ("0", json.dumps(meta_obj).encode("utf-8").hex()))
    for bid, data in blob_rows:
        con.execute("insert into blobs (id, data) values (?, ?)", (bid, data))
    con.commit()
    con.close()

    meta_json = {
        "schemaVersion": 1,
        "createdAtMs": created_at_ms,
        "updatedAtMs": updated_at_ms,
        "title": title,
        "hasConversation": True,
    }
    (dir_path / "meta.json").write_text(json.dumps(meta_json), "utf-8")
    return store_path


def _basic_messages(tool_call_id: str = "toolu_01ABC") -> list[dict]:
    return [
        {"role": "system", "content": "You are a helpful agent."},
        {
            "role": "user",
            "content": [{"type": "text", "text": "hello cursor"}],
            "providerOptions": {"cursor": {"requestId": "req-1"}},
        },
        {
            "role": "assistant",
            "id": "1",
            "content": [
                {"type": "reasoning", "text": "thinking about it",
                 "providerOptions": {"cursor": {"modelName": "glm-5.2"}}},
                {"type": "text", "text": "sure, running a tool"},
                {
                    "type": "tool-call",
                    "toolCallId": tool_call_id,
                    "toolName": "Shell",
                    "args": {"command": "echo hi", "description": "say hi"},
                },
            ],
        },
        {
            "role": "tool",
            "id": tool_call_id,
            "content": [
                {
                    "type": "tool-result",
                    "toolCallId": tool_call_id,
                    "toolName": "Shell",
                    "result": "hi",
                }
            ],
        },
        {
            "role": "assistant",
            "id": "2",
            "content": [{"type": "text", "text": "done"}],
        },
    ]


def test_decode_current_format_basic(tmp_path: Path) -> None:
    store = _write_store(
        tmp_path / "sess",
        agent_id="aaa-111",
        created_at_ms=1_700_000_000_000,
        updated_at_ms=1_700_000_060_000,
        messages=_basic_messages(),
    )
    result = decode_store_db(store)
    assert result.diagnostics.unsupported_gap is None
    session = result.session
    assert session is not None
    assert session.provider == "cursor"
    assert session.provider_session_id == "aaa-111"
    assert session.cwd == "/tmp/test-workspace"
    assert session.project == "test-workspace"
    assert result.diagnostics.model == "glm-5.2"
    assert result.diagnostics.title == "Test Cursor Session"
    assert result.diagnostics.message_count == 5
    # system + user text + reasoning + assistant text + tool-call + tool-result + assistant text
    assert result.diagnostics.event_count == 7


def test_tool_call_result_pairing(tmp_path: Path) -> None:
    store = _write_store(
        tmp_path / "sess",
        agent_id="bbb-222",
        created_at_ms=1_700_000_000_000,
        updated_at_ms=1_700_000_060_000,
        messages=_basic_messages(tool_call_id="toolu_PAIR"),
    )
    result = decode_store_db(store)
    assert result.session is not None
    events = result.session.events
    tool_call = next(e for e in events if e.tool_name and e.role == "assistant")
    tool_result = next(e for e in events if e.role == "tool")
    assert tool_call.tool_call_id == "toolu_PAIR"
    assert tool_result.tool_call_id == "toolu_PAIR"
    assert tool_call.tool_name == "Shell"
    assert tool_result.tool_name == "Shell"
    assert tool_call.tool_input_json == {"command": "echo hi", "description": "say hi"}
    assert tool_result.tool_output_text == "hi"


def test_reasoning_preserved_as_first_class(tmp_path: Path) -> None:
    store = _write_store(
        tmp_path / "sess",
        agent_id="ccc-333",
        created_at_ms=1_700_000_000_000,
        updated_at_ms=1_700_000_060_000,
        messages=_basic_messages(),
    )
    result = decode_store_db(store)
    assert result.session is not None
    reasoning = [e for e in result.session.events if e.content_text == "thinking about it"]
    assert len(reasoning) == 1
    assert reasoning[0].role == "assistant"
    # raw_json carries the full reasoning block so the timeline can distinguish it
    assert '"reasoning"' in (reasoning[0].raw_json or "")


def test_timestamps_monotonic_within_window(tmp_path: Path) -> None:
    start_ms = 1_700_000_000_000
    end_ms = 1_700_000_060_000
    store = _write_store(
        tmp_path / "sess",
        agent_id="ddd-444",
        created_at_ms=start_ms,
        updated_at_ms=end_ms,
        messages=_basic_messages(),
    )
    result = decode_store_db(store)
    assert result.session is not None
    ts = [e.timestamp for e in result.session.events]
    assert ts == sorted(ts)
    assert ts[0].timestamp() == start_ms / 1000.0
    assert abs(ts[-1].timestamp() - end_ms / 1000.0) < 1e-6


def test_unknown_block_type_preserved_not_dropped(tmp_path: Path) -> None:
    messages = [
        {
            "role": "assistant",
            "id": "1",
            "content": [
                {"type": "text", "text": "ok"},
                {"type": "future-block", "payload": {"x": 1}},
            ],
        },
    ]
    store = _write_store(
        tmp_path / "sess",
        agent_id="eee-555",
        created_at_ms=1_700_000_000_000,
        updated_at_ms=1_700_000_060_000,
        messages=messages,
    )
    result = decode_store_db(store)
    assert result.diagnostics.unsupported_gap is None
    assert result.diagnostics.unknown_block_types == {"future-block": 1}
    assert result.session is not None
    # text event + unknown block event both survive
    assert result.diagnostics.event_count == 2


def test_legacy_chunked_format_reported_as_gap(tmp_path: Path) -> None:
    dir_path = tmp_path / "legacy"
    dir_path.mkdir(parents=True)
    store_path = dir_path / "store.db"
    con = sqlite3.connect(str(store_path))
    con.execute("create table meta (key text primary key, value text)")
    con.execute("create table blobs (id text primary key, data blob)")
    # a non-JSON (protobuf-ish) message blob stands in for the legacy chunked format
    legacy_msg = b"\x0a\x10this is not json" + b"\x00\x01\x02"
    import hashlib

    legacy_id = hashlib.sha256(legacy_msg).digest()
    root = _build_root([legacy_id], workspace=None)
    root_id = hashlib.sha256(root).digest()
    meta_obj = {
        "agentId": "fff-666",
        "latestRootBlobId": root_id.hex(),
        "name": "Legacy",
        "createdAt": 1_700_000_000_000,
        "lastUsedModel": "composer-1",
    }
    con.execute("insert into meta (key, value) values (?, ?)", ("0", json.dumps(meta_obj).encode("utf-8").hex()))
    con.execute("insert into blobs (id, data) values (?, ?)", (legacy_id.hex(), legacy_msg))
    con.execute("insert into blobs (id, data) values (?, ?)", (root_id.hex(), root))
    con.commit()
    con.close()
    result = decode_store_db(store_path)
    assert result.session is None
    assert result.diagnostics.unsupported_gap == GAP_LEGACY_CHUNKED


def test_missing_store_reported_as_gap(tmp_path: Path) -> None:
    result = decode_store_db(tmp_path / "nope" / "store.db")
    assert result.session is None
    assert result.diagnostics.unsupported_gap == GAP_MISSING_ROOT


def test_empty_session_reported_as_gap(tmp_path: Path) -> None:
    dir_path = tmp_path / "empty"
    dir_path.mkdir(parents=True)
    store_path = dir_path / "store.db"
    con = sqlite3.connect(str(store_path))
    con.execute("create table meta (key text primary key, value text)")
    con.execute("create table blobs (id text primary key, data blob)")
    root = _build_root([], workspace=None)  # no field-1 ids
    import hashlib

    root_id = hashlib.sha256(root).digest()
    meta_obj = {
        "agentId": "ggg-777",
        "latestRootBlobId": root_id.hex(),
        "name": "Empty",
        "createdAt": 1_700_000_000_000,
        "lastUsedModel": "glm-5.2",
    }
    con.execute("insert into meta (key, value) values (?, ?)", ("0", json.dumps(meta_obj).encode("utf-8").hex()))
    con.execute("insert into blobs (id, data) values (?, ?)", (root_id.hex(), root))
    con.commit()
    con.close()
    result = decode_store_db(store_path)
    assert result.session is None
    assert result.diagnostics.unsupported_gap == GAP_EMPTY_SESSION


def test_ingest_through_agents_store(tmp_path: Path) -> None:
    from sqlalchemy.orm import sessionmaker

    from zerg.database import Base
    from zerg.database import make_engine
    from zerg.models.agents import AgentSession
    from zerg.services.agents import AgentsStore

    db_path = tmp_path / "ingest.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)

    store = _write_store(
        tmp_path / "sess",
        agent_id="hhh-888",
        created_at_ms=1_700_000_000_000,
        updated_at_ms=1_700_000_060_000,
        messages=_basic_messages(tool_call_id="toolu_INGEST"),
    )
    with SessionLocal() as db:
        result = ingest_cursor_store_db(db, store)
        assert result.diagnostics.unsupported_gap is None
        assert result.ingest is not None
        assert result.ingest.events_inserted == result.diagnostics.event_count
        assert result.ingest.session_created is True
        # The session is durable with cursor provider + provider session id.
        rows = db.query(AgentSession).all()
        assert len(rows) == 1
        assert rows[0].provider == "cursor"
        assert rows[0].cwd == "/tmp/test-workspace"
        # Re-ingest is idempotent (dedupe), no new events.
        store_obj = AgentsStore(db)
        again = store_obj.ingest_session(decode_store_db(store).session)
        assert again.events_inserted == 0


def test_ingest_skips_legacy_gap(tmp_path: Path) -> None:
    from sqlalchemy.orm import sessionmaker

    from zerg.database import Base
    from zerg.database import make_engine

    db_path = tmp_path / "ingest.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)

    # Reuse the legacy fixture builder inline.
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir(parents=True)
    legacy_store = legacy_dir / "store.db"
    con = sqlite3.connect(str(legacy_store))
    con.execute("create table meta (key text primary key, value text)")
    con.execute("create table blobs (id text primary key, data blob)")
    import hashlib

    legacy_msg = b"\x0a\x10this is not json\x00\x01\x02"
    legacy_id = hashlib.sha256(legacy_msg).digest()
    root = _build_root([legacy_id], workspace=None)
    root_id = hashlib.sha256(root).digest()
    meta_obj = {
        "agentId": "iii-999",
        "latestRootBlobId": root_id.hex(),
        "name": "Legacy",
        "createdAt": 1_700_000_000_000,
        "lastUsedModel": "composer-1",
    }
    con.execute("insert into meta (key, value) values (?, ?)", ("0", json.dumps(meta_obj).encode("utf-8").hex()))
    con.execute("insert into blobs (id, data) values (?, ?)", (legacy_id.hex(), legacy_msg))
    con.execute("insert into blobs (id, data) values (?, ?)", (root_id.hex(), root))
    con.commit()
    con.close()

    with SessionLocal() as db:
        result = ingest_cursor_store_db(db, legacy_store)
        assert result.ingest is None
        assert result.diagnostics.unsupported_gap == GAP_LEGACY_CHUNKED


def test_peek_cursor_session_summary(tmp_path: Path) -> None:
    store = _write_store(
        tmp_path / "sess",
        agent_id="jjj-000",
        created_at_ms=1_700_000_000_000,
        updated_at_ms=1_700_000_060_000,
        messages=_basic_messages(),
    )
    summary = peek_cursor_session(store)
    assert summary is not None
    assert summary.agent_id == "jjj-000"
    assert summary.title == "Test Cursor Session"
    assert summary.workspace == "/tmp/test-workspace"
    assert summary.model == "glm-5.2"
    assert summary.legacy is False
    assert summary.control_path == "unmanaged"
    assert summary.liveness_model == "transcript"
    assert summary.state == "detached"


def test_peek_cursor_session_legacy_flag(tmp_path: Path) -> None:
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir(parents=True)
    legacy_store = legacy_dir / "store.db"
    con = sqlite3.connect(str(legacy_store))
    con.execute("create table meta (key text primary key, value text)")
    con.execute("create table blobs (id text primary key, data blob)")
    import hashlib

    legacy_msg = b"\x0a\x10this is not json\x00\x01\x02"
    legacy_id = hashlib.sha256(legacy_msg).digest()
    root = _build_root([legacy_id], workspace="/tmp/legacy-ws")
    root_id = hashlib.sha256(root).digest()
    meta_obj = {
        "agentId": "kkk-111",
        "latestRootBlobId": root_id.hex(),
        "name": "Legacy Peek",
        "createdAt": 1_700_000_000_000,
        "lastUsedModel": "composer-1",
    }
    con.execute("insert into meta (key, value) values (?, ?)", ("0", json.dumps(meta_obj).encode("utf-8").hex()))
    con.execute("insert into blobs (id, data) values (?, ?)", (legacy_id.hex(), legacy_msg))
    con.execute("insert into blobs (id, data) values (?, ?)", (root_id.hex(), root))
    con.commit()
    con.close()
    summary = peek_cursor_session(legacy_store)
    assert summary is not None
    assert summary.agent_id == "kkk-111"
    assert summary.legacy is True
    assert summary.workspace == "/tmp/legacy-ws"


def test_iter_local_cursor_session_summaries_scans_dir(tmp_path: Path) -> None:
    cursor_root = tmp_path / "chats"
    for i in range(3):
        _write_store(
            cursor_root / f"ws{i}" / f"sess-{i}",
            agent_id=f"agent-{i}",
            created_at_ms=1_700_000_000_000 + i,
            updated_at_ms=1_700_000_060_000 + i,
            messages=_basic_messages(),
            title=f"Session {i}",
        )
    summaries = list(iter_local_cursor_session_summaries(cursor_root))
    assert len(summaries) == 3
    assert {s.agent_id for s in summaries} == {"agent-0", "agent-1", "agent-2"}
    assert all(s.legacy is False for s in summaries)


def test_local_health_cursor_discovery(tmp_path: Path) -> None:
    # Point HOME-style discovery at our temp tree by monkeypatching the
    # iterator's default root via an explicit cursor_root is not supported by
    # _collect_cursor_discovery, so build under a fake cursor chats root and
    # patch iter_local_cursor_session_summaries to read from it.
    import zerg.services.local_health as lh
    from zerg.services.local_health import _collect_cursor_discovery

    cursor_root = tmp_path / "chats"
    _write_store(
        cursor_root / "ws" / "sess",
        agent_id="lll-222",
        created_at_ms=1_700_000_000_000,
        updated_at_ms=1_700_000_060_000,
        messages=_basic_messages(),
        title="Discovered",
    )
    from zerg.services import cursor_transcript as ct

    original = lh.iter_local_cursor_session_summaries
    lh.iter_local_cursor_session_summaries = lambda: ct.iter_local_cursor_session_summaries(cursor_root)
    try:
        deep = _collect_cursor_discovery(fast=False)
        assert deep["status"] == "ok"
        assert deep["session_count"] == 1
        row = deep["sessions"][0]
        assert row["provider"] == "cursor"
        assert row["control_path"] == "unmanaged"
        assert row["liveness_model"] == "transcript"
        assert row["state"] == "detached"
        assert row["provider_session_id"] == "lll-222"
        assert row["legacy_format"] is False
        assert deep["legacy_format_count"] == 0

        fast = _collect_cursor_discovery(fast=True)
        assert fast["status"] == "skipped"
        assert fast["sessions"] == []
    finally:
        lh.iter_local_cursor_session_summaries = original


def _tool_result_with_duration(tool_call_id: str, execution_ms: int) -> dict:
    return {
        "role": "tool",
        "id": tool_call_id,
        "content": [
            {
                "type": "tool-result",
                "toolCallId": tool_call_id,
                "toolName": "Shell",
                "result": "ok",
                "providerOptions": {
                    "cursor": {
                        "highLevelToolCallResult": {
                            "output": {
                                "success": {
                                    "exitCode": 0,
                                    "stdout": "ok\n",
                                    "stderr": "",
                                    "executionTime": execution_ms,
                                    "localExecutionTimeMs": execution_ms - 5,
                                }
                            }
                        }
                    }
                },
            }
        ],
    }


def _burst_messages(execution_ms: int) -> list[dict]:
    tcid = "toolu_burst"
    return [
        {"role": "user", "content": [{"type": "text", "text": "run it"}]},
        {
            "role": "assistant",
            "id": "1",
            "content": [
                {"type": "tool-call", "toolCallId": tcid, "toolName": "Shell", "args": {"command": "sleep"}}
            ],
        },
        _tool_result_with_duration(tcid, execution_ms),
        {"role": "assistant", "id": "2", "content": [{"type": "text", "text": "done"}]},
    ]


def test_decode_timestamp_fidelity_is_synthetic(tmp_path: Path) -> None:
    store = _write_store(
        tmp_path / "sess",
        agent_id="fff-001",
        created_at_ms=1_700_000_000_000,
        updated_at_ms=1_700_000_060_000,
        messages=_basic_messages(),
    )
    result = decode_store_db(store)
    assert result.diagnostics is not None
    assert result.diagnostics.timestamp_fidelity == "synthetic"


def test_burst_aware_timestamps_cluster_at_start_with_tool_duration(tmp_path: Path) -> None:
    # 60s session window, but the tool took 5s. Burst-aware synthesis clusters
    # events near start using the real tool duration instead of smearing
    # uniformly across the full 60s.
    start_ms = 1_700_000_000_000
    span_ms = 60_000
    store = _write_store(
        tmp_path / "sess",
        agent_id="fff-002",
        created_at_ms=start_ms,
        updated_at_ms=start_ms + span_ms,
        messages=_burst_messages(execution_ms=5_000),
    )
    result = decode_store_db(store)
    assert result.session is not None
    events = result.session.events
    # 4 messages -> user, assistant tool-call, tool result, assistant text
    last_event_ts = events[-1].timestamp
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    start = _dt.fromtimestamp(start_ms / 1000.0, tz=_tz.utc)
    # All events should cluster within ~6s of start (5s tool + small deltas),
    # NOT smear across the 60s window. Uniform spread would put the last event
    # near start+60s.
    cluster_width_ms = (last_event_ts - start).total_seconds() * 1000
    assert cluster_width_ms < 10_000, cluster_width_ms  # < 10s, not 60s
    # The tool-result event should be stamped after the tool-call (monotonic)
    # and within the tool duration window.
    tool_call_ev = next(e for e in events if e.role == "assistant" and e.tool_name == "Shell")
    tool_result_ev = next(e for e in events if e.role == "tool")
    assert tool_result_ev.timestamp >= tool_call_ev.timestamp


def test_no_tool_durations_falls_back_to_uniform_spread(tmp_path: Path) -> None:
    # _basic_messages has no executionTime -> uniform spread across the window.
    start_ms = 1_700_000_000_000
    span_ms = 60_000
    store = _write_store(
        tmp_path / "sess",
        agent_id="fff-003",
        created_at_ms=start_ms,
        updated_at_ms=start_ms + span_ms,
        messages=_basic_messages(),
    )
    result = decode_store_db(store)
    assert result.session is not None
    events = result.session.events
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    end = _dt.fromtimestamp((start_ms + span_ms) / 1000.0, tz=_tz.utc)
    # Last event should be near the end of the window (uniform spread), not
    # clustered at the start.
    last_event_ts = events[-1].timestamp
    gap_to_end_ms = (end - last_event_ts).total_seconds() * 1000
    assert gap_to_end_ms < 10_000, gap_to_end_ms  # last event within 10s of end
