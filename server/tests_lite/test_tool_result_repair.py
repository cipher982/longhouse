from __future__ import annotations

import hashlib
import json
from datetime import datetime
from datetime import timezone
from uuid import UUID
from uuid import uuid4

from typer.testing import CliRunner

from zerg.cli.main import app
from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentSourceLine
from zerg.services.archive_shadow import insert_archive_chunk_manifests
from zerg.services.archive_store import ArchiveRecord
from zerg.services.archive_store import FilesystemArchiveStore
from zerg.services.raw_json_compression import CODEC_PLAIN
from zerg.services.raw_json_compression import CODEC_ZSTD
from zerg.services.raw_json_compression import compress_raw_json
from zerg.services.tool_result_repair import scan_orphan_tool_results


def test_scan_classifies_recoverable_compressed_image_tool_result(tmp_path):
    factory = _factory(tmp_path)
    session_id = uuid4()
    raw = _tool_result_raw("toolu_img", [{"type": "image", "source": {"type": "base64", "data": "abc"}}])

    with factory() as db:
        _seed_session(db, session_id)
        call = _seed_tool_call(db, session_id, tool_call_id="toolu_img")
        _seed_source_line(db, session_id, raw=raw, source_offset=100)
        db.commit()

        before = db.query(AgentEvent).count()
        result = scan_orphan_tool_results(db, session_id=session_id)
        after = db.query(AgentEvent).count()

    assert before == after
    assert result.scanned_orphan_calls == 1
    assert result.recoverable == 1
    finding = result.findings[0]
    assert finding.event_id == call.id
    assert finding.status == "recoverable"
    assert finding.recovered_tool_output_text == "[image result]"
    assert finding.recovered_source_path == _SOURCE_PATH
    assert finding.recovered_source_offset == 100
    assert finding.recovered_event_uuid == "tool-result-line-result-toolu_img"


def test_scan_skips_already_paired_tool_call(tmp_path):
    factory = _factory(tmp_path)
    session_id = uuid4()

    with factory() as db:
        _seed_session(db, session_id)
        _seed_tool_call(db, session_id, tool_call_id="toolu_done")
        db.add(
            AgentEvent(
                session_id=session_id,
                branch_id=1,
                role="tool",
                tool_output_text="done",
                tool_call_id="toolu_done",
                timestamp=_ts(2),
                source_path=_SOURCE_PATH,
                source_offset=100,
            )
        )
        db.commit()

        result = scan_orphan_tool_results(db, session_id=session_id)

    assert result.scanned_orphan_calls == 0
    assert result.recoverable == 0


def test_scan_ignores_non_durable_streaming_tool_calls(tmp_path):
    factory = _factory(tmp_path)
    session_id = uuid4()
    raw = _tool_result_raw("toolu_stream", [{"type": "image", "source": {"type": "base64", "data": "abc"}}])

    with factory() as db:
        _seed_session(db, session_id)
        _seed_tool_call(db, session_id, tool_call_id="toolu_stream", event_origin="stream")
        _seed_source_line(db, session_id, raw=raw, source_offset=100)
        db.commit()

        result = scan_orphan_tool_results(db, session_id=session_id)

    assert result.scanned_orphan_calls == 0


def test_scan_classifies_orphan_without_matching_source_result_as_genuine_gap(tmp_path):
    factory = _factory(tmp_path)
    session_id = uuid4()

    with factory() as db:
        _seed_session(db, session_id)
        _seed_tool_call(db, session_id, tool_call_id="toolu_missing")
        _seed_source_line(db, session_id, raw='{"type":"assistant","uuid":"later"}', source_offset=100)
        db.commit()

        result = scan_orphan_tool_results(db, session_id=session_id)

    assert result.scanned_orphan_calls == 1
    assert result.no_result_in_source == 1
    assert result.findings[0].status == "no_result_in_source"


def test_scan_classifies_matching_unparsed_result_separately(tmp_path):
    factory = _factory(tmp_path)
    session_id = uuid4()
    raw = _tool_result_raw("toolu_empty", "")

    with factory() as db:
        _seed_session(db, session_id)
        _seed_tool_call(db, session_id, tool_call_id="toolu_empty")
        _seed_source_line(db, session_id, raw=raw, source_offset=100)
        db.commit()

        result = scan_orphan_tool_results(db, session_id=session_id)

    assert result.scanned_orphan_calls == 1
    assert result.unparseable_result == 1
    assert result.findings[0].status == "unparseable_result"


def test_scan_contains_corrupt_source_line_without_aborting_batch(tmp_path):
    factory = _factory(tmp_path)
    session_id = uuid4()

    with factory() as db:
        _seed_session(db, session_id)
        _seed_tool_call(db, session_id, tool_call_id="toolu_bad")
        _seed_source_line(db, session_id, raw="", source_offset=100, raw_json_z=b"not-zstd", raw_json_codec=CODEC_ZSTD)
        db.commit()

        result = scan_orphan_tool_results(db, session_id=session_id)

    assert result.scanned_orphan_calls == 1
    assert result.unparseable_result == 1
    assert result.findings[0].reason == "classification failed: ZstdError"


def test_scan_reads_slim_source_line_from_archive(tmp_path):
    factory = _factory(tmp_path)
    session_id = uuid4()
    raw = _tool_result_raw("toolu_refs", [
        {"type": "tool_reference", "tool_name": "TaskCreate"},
        {"type": "tool_reference", "tool_name": "TaskUpdate"},
    ])
    line_hash = _line_hash(raw)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")

    with factory() as db:
        _seed_session(db, session_id)
        _seed_tool_call(db, session_id, tool_call_id="toolu_refs")
        _seed_source_line(
            db,
            session_id,
            raw="",
            source_offset=100,
            raw_json_z=None,
            raw_json_codec=CODEC_PLAIN,
            line_hash=line_hash,
        )
        chunks = archive_store.write_record_chunks(
            [
                ArchiveRecord(
                    tenant_id="default",
                    session_id=str(session_id),
                    stream="source_lines",
                    source_seq=1,
                    raw_bytes=raw.encode(),
                    source_path=_SOURCE_PATH,
                    source_offset=100,
                )
            ],
            target_uncompressed_bytes=1 << 20,
        )
        insert_archive_chunk_manifests(db, chunks)
        db.commit()

        result = scan_orphan_tool_results(db, session_id=session_id, archive_store=archive_store)

    assert result.scanned_orphan_calls == 1
    assert result.recoverable == 1
    assert result.findings[0].recovered_tool_output_text == "[tool references: TaskCreate, TaskUpdate]"


def test_archive_scan_orphan_tool_results_cli_emits_json(tmp_path):
    db_path = tmp_path / "longhouse.db"
    factory = _factory_for_db(db_path)
    session_id = uuid4()
    raw = _tool_result_raw("toolu_cli", [{"type": "image", "source": {"type": "base64", "data": "abc"}}])
    with factory() as db:
        _seed_session(db, session_id)
        _seed_tool_call(db, session_id, tool_call_id="toolu_cli")
        _seed_source_line(db, session_id, raw=raw, source_offset=100)
        db.commit()

    result = CliRunner().invoke(
        app,
        [
            "archive",
            "scan-orphan-tool-results",
            "--database-url",
            f"sqlite:///{db_path}",
            "--session-id",
            str(session_id),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["scanned_orphan_calls"] == 1
    assert payload["recoverable"] == 1
    assert payload["findings"][0]["status"] == "recoverable"
    assert "source_path" not in payload["findings"][0]
    assert "recovered_source_path" not in payload["findings"][0]
    assert "recovered_tool_output_text" not in payload["findings"][0]


def test_archive_scan_orphan_tool_results_cli_can_include_evidence(tmp_path):
    db_path = tmp_path / "longhouse.db"
    factory = _factory_for_db(db_path)
    session_id = uuid4()
    raw = _tool_result_raw("toolu_cli_evidence", [{"type": "image", "source": {"type": "base64", "data": "abc"}}])
    with factory() as db:
        _seed_session(db, session_id)
        _seed_tool_call(db, session_id, tool_call_id="toolu_cli_evidence")
        _seed_source_line(db, session_id, raw=raw, source_offset=100)
        db.commit()

    result = CliRunner().invoke(
        app,
        [
            "archive",
            "scan-orphan-tool-results",
            "--database-url",
            f"sqlite:///{db_path}",
            "--session-id",
            str(session_id),
            "--include-evidence",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["findings"][0]["source_path"] == _SOURCE_PATH
    assert payload["findings"][0]["recovered_tool_output_text"] == "[image result]"


def test_archive_scan_orphan_tool_results_cli_rejects_bad_session_id(tmp_path):
    db_path = tmp_path / "longhouse.db"
    _factory_for_db(db_path)

    result = CliRunner().invoke(
        app,
        [
            "archive",
            "scan-orphan-tool-results",
            "--database-url",
            f"sqlite:///{db_path}",
            "--session-id",
            "not-a-uuid",
            "--json",
        ],
    )

    assert result.exit_code != 0
    assert "--session-id must be a valid UUID" in result.output


_SOURCE_PATH = "/Users/davidrose/.claude/projects/longhouse/session.jsonl"


def _factory(tmp_path):
    return _factory_for_db(tmp_path / "longhouse.db")


def _factory_for_db(db_path):
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _ts(second: int = 0) -> datetime:
    return datetime(2026, 1, 1, 0, 0, second, tzinfo=timezone.utc)


def _seed_session(db, session_id: UUID) -> AgentSession:
    session = AgentSession(
        id=session_id,
        provider="claude",
        environment="test",
        project="longhouse",
        device_id="device-1",
        cwd="/Users/davidrose/git/zerg/longhouse",
        started_at=_ts(),
        last_activity_at=_ts(),
    )
    db.add(session)
    return session


def _seed_tool_call(db, session_id: UUID, *, tool_call_id: str, event_origin: str = "durable") -> AgentEvent:
    call = AgentEvent(
        session_id=session_id,
        branch_id=1,
        role="assistant",
        tool_name="Bash",
        tool_input_json={"command": "echo hi"},
        tool_call_id=tool_call_id,
        timestamp=_ts(1),
        source_path=_SOURCE_PATH,
        source_offset=0,
        event_uuid=f"call-{tool_call_id}",
        event_origin=event_origin,
    )
    db.add(call)
    db.flush()
    return call


def _seed_source_line(
    db,
    session_id: UUID,
    *,
    raw: str,
    source_offset: int,
    raw_json_z: bytes | None = None,
    raw_json_codec: int = CODEC_ZSTD,
    line_hash: str | None = None,
) -> AgentSourceLine:
    row = AgentSourceLine(
        session_id=session_id,
        thread_id=None,
        source_path=_SOURCE_PATH,
        source_offset=source_offset,
        branch_id=1,
        revision=1,
        is_branch_copy=0,
        raw_json="" if raw_json_codec == CODEC_ZSTD else raw,
        raw_json_z=compress_raw_json(raw) if raw_json_z is None and raw_json_codec == CODEC_ZSTD else raw_json_z,
        raw_json_codec=raw_json_codec,
        line_hash=line_hash or _line_hash(raw),
    )
    db.add(row)
    return row


def _tool_result_raw(tool_call_id: str, content) -> str:
    return json.dumps(
        {
            "type": "user",
            "uuid": "tool-result-line",
            "timestamp": "2026-01-01T00:00:02Z",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_call_id,
                        "content": content,
                    }
                ]
            },
        },
        separators=(",", ":"),
    )


def _line_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
