from __future__ import annotations

import hashlib
import json
import re
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
from zerg.models.agents import SessionObservation
from zerg.services.archive_shadow import insert_archive_chunk_manifests
from zerg.services.archive_store import ArchiveRecord
from zerg.services.archive_store import FilesystemArchiveStore
from zerg.services.raw_json_compression import CODEC_PLAIN
from zerg.services.raw_json_compression import CODEC_ZSTD
from zerg.services.raw_json_compression import compress_raw_json
from zerg.services.raw_json_compression import decode_raw_json
from zerg.services.tool_result_repair import repair_orphan_tool_results
from zerg.services.tool_result_repair import scan_orphan_tool_results


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", text)


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


def test_scan_does_not_pair_branched_call_with_legacy_null_branch_result(tmp_path):
    factory = _factory(tmp_path)
    session_id = uuid4()
    raw = _tool_result_raw("toolu_branch", "done on branch")

    with factory() as db:
        _seed_session(db, session_id)
        _seed_tool_call(db, session_id, tool_call_id="toolu_branch", branch_id=1)
        db.add(
            AgentEvent(
                session_id=session_id,
                branch_id=None,
                role="tool",
                tool_output_text="legacy branchless result",
                tool_call_id="toolu_branch",
                timestamp=_ts(2),
                source_path=_SOURCE_PATH,
                source_offset=100,
                event_origin="durable",
            )
        )
        _seed_source_line(db, session_id, raw=raw, source_offset=100, branch_id=1)
        db.commit()

        result = scan_orphan_tool_results(db, session_id=session_id)

    assert result.scanned_orphan_calls == 1
    assert result.recoverable == 1
    assert result.findings[0].recovered_tool_output_text == "done on branch"


def test_scan_pairs_legacy_null_branch_call_with_null_branch_result(tmp_path):
    factory = _factory(tmp_path)
    session_id = uuid4()

    with factory() as db:
        _seed_session(db, session_id)
        _seed_tool_call(db, session_id, tool_call_id="toolu_legacy", branch_id=None)
        db.add(
            AgentEvent(
                session_id=session_id,
                branch_id=None,
                role="tool",
                tool_output_text="legacy done",
                tool_call_id="toolu_legacy",
                timestamp=_ts(2),
                source_path=_SOURCE_PATH,
                source_offset=100,
                event_origin="durable",
            )
        )
        db.commit()

        result = scan_orphan_tool_results(db, session_id=session_id)

    assert result.scanned_orphan_calls == 0


def test_scan_explains_unscannable_null_branch_call(tmp_path):
    factory = _factory(tmp_path)
    session_id = uuid4()

    with factory() as db:
        _seed_session(db, session_id)
        _seed_tool_call(db, session_id, tool_call_id="toolu_no_branch", branch_id=None)
        db.commit()

        result = scan_orphan_tool_results(db, session_id=session_id)

    assert result.scanned_orphan_calls == 1
    assert result.no_source_evidence == 1
    assert result.findings[0].reason == "tool call has no branch_id"


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


def test_scan_recovers_empty_success_tool_result(tmp_path):
    factory = _factory(tmp_path)
    session_id = uuid4()
    raw = _tool_results_raw([
        ("toolu_empty", ""),
        ("toolu_null", None),
    ])

    with factory() as db:
        _seed_session(db, session_id)
        _seed_tool_call(db, session_id, tool_call_id="toolu_empty")
        _seed_tool_call(db, session_id, tool_call_id="toolu_null")
        _seed_source_line(db, session_id, raw=raw, source_offset=100)
        db.commit()

        result = scan_orphan_tool_results(db, session_id=session_id)

    assert result.scanned_orphan_calls == 2
    assert result.recoverable == 2
    assert [(finding.tool_call_id, finding.status, finding.recovered_tool_output_text) for finding in result.findings] == [
        ("toolu_empty", "recoverable", "[empty tool result]"),
        ("toolu_null", "recoverable", "[empty tool result]"),
    ]


def test_scan_recovers_json_object_tool_result(tmp_path):
    factory = _factory(tmp_path)
    session_id = uuid4()
    raw = _tool_result_raw("toolu_object", {"status": "ok", "count": 0})

    with factory() as db:
        _seed_session(db, session_id)
        _seed_tool_call(db, session_id, tool_call_id="toolu_object")
        _seed_source_line(db, session_id, raw=raw, source_offset=100)
        db.commit()

        result = scan_orphan_tool_results(db, session_id=session_id)

    assert result.scanned_orphan_calls == 1
    assert result.recoverable == 1
    assert result.findings[0].status == "recoverable"
    assert result.findings[0].recovered_tool_output_text == '{"status":"ok","count":0}'


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


def test_scan_truncates_recovered_tool_output_preview(tmp_path):
    factory = _factory(tmp_path)
    session_id = uuid4()
    output = "x" * 600
    raw = _tool_result_raw("toolu_long", output)

    with factory() as db:
        _seed_session(db, session_id)
        _seed_tool_call(db, session_id, tool_call_id="toolu_long")
        _seed_source_line(db, session_id, raw=raw, source_offset=100)
        db.commit()

        result = scan_orphan_tool_results(db, session_id=session_id)

    preview = result.findings[0].recovered_tool_output_text
    assert preview is not None
    assert len(preview) == 503
    assert preview.endswith("...")


def test_repair_orphan_tool_results_dry_run_does_not_mutate(tmp_path):
    factory = _factory(tmp_path)
    session_id = uuid4()
    raw = _tool_result_raw("toolu_dry", "dry output")

    with factory() as db:
        _seed_session(db, session_id)
        _seed_tool_call(db, session_id, tool_call_id="toolu_dry")
        _seed_source_line(db, session_id, raw=raw, source_offset=100)
        db.commit()

        before_events = db.query(AgentEvent).count()
        before_observations = db.query(SessionObservation).count()
        result = repair_orphan_tool_results(db, session_id=session_id)
        after_events = db.query(AgentEvent).count()
        after_observations = db.query(SessionObservation).count()

    assert result.dry_run is True
    assert result.scanned_orphan_calls == 1
    assert result.recoverable == 1
    assert result.inserted == 0
    assert before_events == after_events
    assert before_observations == after_observations


def test_repair_orphan_tool_results_apply_inserts_event_and_observation(tmp_path):
    factory = _factory(tmp_path)
    session_id = uuid4()
    raw = _tool_result_raw("toolu_apply", "apply output")

    with factory() as db:
        _seed_session(db, session_id)
        _seed_tool_call(db, session_id, tool_call_id="toolu_apply")
        _seed_source_line(db, session_id, raw=raw, source_offset=100)
        db.commit()

        result = repair_orphan_tool_results(db, session_id=session_id, apply=True)

        repaired = (
            db.query(AgentEvent)
            .filter(AgentEvent.session_id == session_id)
            .filter(AgentEvent.role == "tool")
            .filter(AgentEvent.tool_call_id == "toolu_apply")
            .one()
        )
        observation = (
            db.query(SessionObservation)
            .filter(SessionObservation.session_id == session_id)
            .filter(SessionObservation.source == "tool_result_repair")
            .one()
        )

    assert result.dry_run is False
    assert result.scanned_orphan_calls == 1
    assert result.recoverable == 1
    assert result.inserted == 1
    assert repaired.branch_id == 1
    assert repaired.event_origin == "durable"
    assert repaired.tool_output_text == "apply output"
    assert repaired.source_path == _SOURCE_PATH
    assert repaired.source_offset == 100
    assert repaired.event_uuid == "tool-result-line"
    assert repaired.event_hash == _expected_event_hash(tool_call_id="toolu_apply", tool_output_text="apply output", raw_json=raw)
    assert decode_raw_json(repaired) == raw
    assert observation.kind == "provider_event"
    assert observation.source_path == _SOURCE_PATH
    assert observation.source_offset == 100


def test_repair_orphan_tool_results_apply_is_idempotent(tmp_path):
    factory = _factory(tmp_path)
    session_id = uuid4()
    raw = _tool_result_raw("toolu_once", "once output")

    with factory() as db:
        _seed_session(db, session_id)
        _seed_tool_call(db, session_id, tool_call_id="toolu_once")
        _seed_source_line(db, session_id, raw=raw, source_offset=100)
        db.commit()

        first = repair_orphan_tool_results(db, session_id=session_id, apply=True)
        second = repair_orphan_tool_results(db, session_id=session_id, apply=True)
        tool_results = (
            db.query(AgentEvent)
            .filter(AgentEvent.session_id == session_id)
            .filter(AgentEvent.role == "tool")
            .filter(AgentEvent.tool_call_id == "toolu_once")
            .count()
        )

    assert first.inserted == 1
    assert second.scanned_orphan_calls == 0
    assert second.inserted == 0
    assert tool_results == 1


def test_repair_orphan_tool_results_handles_multiple_results_in_one_source_line(tmp_path):
    factory = _factory(tmp_path)
    session_id = uuid4()
    raw = _tool_results_raw([
        ("toolu_first", "first output"),
        ("toolu_second", "second output"),
    ])

    with factory() as db:
        _seed_session(db, session_id)
        _seed_tool_call(db, session_id, tool_call_id="toolu_first")
        _seed_tool_call(db, session_id, tool_call_id="toolu_second")
        _seed_source_line(db, session_id, raw=raw, source_offset=100)
        db.commit()

        result = repair_orphan_tool_results(db, session_id=session_id, apply=True)
        repaired = (
            db.query(AgentEvent)
            .filter(AgentEvent.session_id == session_id)
            .filter(AgentEvent.role == "tool")
            .order_by(AgentEvent.tool_call_id.asc())
            .all()
        )

    assert result.scanned_orphan_calls == 2
    assert result.inserted == 2
    assert [event.tool_call_id for event in repaired] == ["toolu_first", "toolu_second"]
    assert repaired[0].event_uuid == "tool-result-line"
    assert decode_raw_json(repaired[0]) == raw
    assert repaired[0].event_hash == _expected_event_hash(tool_call_id="toolu_first", tool_output_text="first output", raw_json=raw)
    assert repaired[1].event_uuid is None
    assert decode_raw_json(repaired[1]) is None
    assert repaired[1].event_hash == _expected_event_hash(tool_call_id="toolu_second", tool_output_text="second output", raw_json=None)


def test_repair_orphan_tool_results_reads_slim_source_line_from_archive(tmp_path):
    factory = _factory(tmp_path)
    session_id = uuid4()
    raw = _tool_result_raw("toolu_apply_archive", [{"type": "tool_reference", "tool_name": "TaskCreate"}])
    line_hash = _line_hash(raw)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")

    with factory() as db:
        _seed_session(db, session_id)
        _seed_tool_call(db, session_id, tool_call_id="toolu_apply_archive")
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

        result = repair_orphan_tool_results(db, session_id=session_id, archive_store=archive_store, apply=True)
        repaired = db.query(AgentEvent).filter(AgentEvent.role == "tool").filter(AgentEvent.tool_call_id == "toolu_apply_archive").one()

    assert result.inserted == 1
    assert repaired.tool_output_text == "[tool references: TaskCreate]"
    assert decode_raw_json(repaired) == raw


def test_repair_orphan_tool_results_does_not_insert_nonrecoverable(tmp_path):
    factory = _factory(tmp_path)
    session_id = uuid4()

    with factory() as db:
        _seed_session(db, session_id)
        _seed_tool_call(db, session_id, tool_call_id="toolu_missing_apply")
        _seed_source_line(db, session_id, raw='{"type":"assistant","uuid":"later"}', source_offset=100)
        db.commit()

        result = repair_orphan_tool_results(db, session_id=session_id, apply=True)
        tool_results = db.query(AgentEvent).filter(AgentEvent.role == "tool").count()

    assert result.scanned_orphan_calls == 1
    assert result.no_result_in_source == 1
    assert result.inserted == 0
    assert tool_results == 0


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
    assert "--session-id must be a valid UUID" in _strip_ansi(result.output)


def test_archive_repair_orphan_tool_results_cli_defaults_to_dry_run(tmp_path):
    db_path = tmp_path / "longhouse.db"
    factory = _factory_for_db(db_path)
    session_id = uuid4()
    raw = _tool_result_raw("toolu_cli_dry", "cli dry output")
    with factory() as db:
        _seed_session(db, session_id)
        _seed_tool_call(db, session_id, tool_call_id="toolu_cli_dry")
        _seed_source_line(db, session_id, raw=raw, source_offset=100)
        db.commit()

    result = CliRunner().invoke(
        app,
        [
            "archive",
            "repair-orphan-tool-results",
            "--database-url",
            f"sqlite:///{db_path}",
            "--session-id",
            str(session_id),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["recoverable"] == 1
    assert payload["inserted"] == 0
    with factory() as db:
        assert db.query(AgentEvent).filter(AgentEvent.role == "tool").count() == 0
        assert db.query(SessionObservation).count() == 0


def test_archive_repair_orphan_tool_results_cli_apply_commits(tmp_path):
    db_path = tmp_path / "longhouse.db"
    factory = _factory_for_db(db_path)
    session_id = uuid4()
    raw = _tool_result_raw("toolu_cli_apply", "cli apply output")
    with factory() as db:
        _seed_session(db, session_id)
        _seed_tool_call(db, session_id, tool_call_id="toolu_cli_apply")
        _seed_source_line(db, session_id, raw=raw, source_offset=100)
        db.commit()

    result = CliRunner().invoke(
        app,
        [
            "archive",
            "repair-orphan-tool-results",
            "--database-url",
            f"sqlite:///{db_path}",
            "--session-id",
            str(session_id),
            "--apply",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is False
    assert payload["inserted"] == 1
    with factory() as db:
        repaired = db.query(AgentEvent).filter(AgentEvent.role == "tool").filter(AgentEvent.tool_call_id == "toolu_cli_apply").one()
        assert repaired.tool_output_text == "cli apply output"
        assert db.query(SessionObservation).filter(SessionObservation.source == "tool_result_repair").count() == 1


def test_archive_repair_orphan_tool_results_cli_rejects_bad_session_id(tmp_path):
    db_path = tmp_path / "longhouse.db"
    _factory_for_db(db_path)

    result = CliRunner().invoke(
        app,
        [
            "archive",
            "repair-orphan-tool-results",
            "--database-url",
            f"sqlite:///{db_path}",
            "--session-id",
            "not-a-uuid",
            "--json",
        ],
    )

    assert result.exit_code != 0
    assert "--session-id must be a valid UUID" in _strip_ansi(result.output)


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


def _seed_tool_call(
    db,
    session_id: UUID,
    *,
    tool_call_id: str,
    branch_id: int | None = 1,
    event_origin: str = "durable",
) -> AgentEvent:
    call = AgentEvent(
        session_id=session_id,
        branch_id=branch_id,
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
    branch_id: int | None = 1,
) -> AgentSourceLine:
    row = AgentSourceLine(
        session_id=session_id,
        thread_id=None,
        source_path=_SOURCE_PATH,
        source_offset=source_offset,
        branch_id=branch_id,
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
    return _tool_results_raw([(tool_call_id, content)])


def _tool_results_raw(results) -> str:
    return json.dumps(
        {
            "type": "user",
            "uuid": "tool-result-line",
            "timestamp": "2026-01-01T00:00:02Z",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": tool_call_id, "content": content}
                    for tool_call_id, content in results
                ]
            },
        },
        separators=(",", ":"),
    )


def _line_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _expected_event_hash(*, tool_call_id: str, tool_output_text: str, raw_json: str | None) -> str:
    payload = {
        "role": "tool",
        "content_text": None,
        "tool_name": None,
        "tool_input_json": None,
        "tool_output_text": tool_output_text,
        "tool_call_id": tool_call_id,
    }
    if raw_json:
        payload["source_line_hash"] = _line_hash(raw_json)
    else:
        payload["timestamp"] = _ts(2).isoformat()
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
