"""Classification and guarded repair for orphaned tool results.

The scanner is read-only. The repair path is opt-in and writes recovered rows
through the normal session-observation reducer so historical backfills preserve
the same audit and dedupe semantics as fresh ingest.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy.orm import aliased

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentSourceLine
from zerg.services.archive_store import FilesystemArchiveStore
from zerg.services.archive_transcript import load_session_source_line_bytes
from zerg.services.raw_json_compression import decode_raw_json
from zerg.services.session_observation_reducers import reduce_provider_event_observation
from zerg.services.session_observations import record_provider_event_observation
from zerg.services.shipper.parser import ParsedEvent
from zerg.services.shipper.parser import parse_tool_result_events_from_raw_line

_RECOVERED_OUTPUT_PREVIEW_CHARS = 500

ToolResultFindingStatus = Literal[
    "recoverable",
    "no_source_evidence",
    "no_result_in_source",
    "unparseable_result",
]


@dataclass(frozen=True)
class OrphanToolResultFinding:
    session_id: str
    event_id: int
    tool_call_id: str
    branch_id: int | None
    source_path: str | None
    source_offset: int | None
    status: ToolResultFindingStatus
    reason: str
    recovered_event_uuid: str | None = None
    recovered_tool_output_text: str | None = None
    recovered_source_path: str | None = None
    recovered_source_offset: int | None = None


@dataclass(frozen=True)
class OrphanToolResultScanResult:
    scanned_orphan_calls: int
    last_event_id: int | None
    recoverable: int
    no_source_evidence: int
    no_result_in_source: int
    unparseable_result: int
    findings: list[OrphanToolResultFinding]


@dataclass(frozen=True)
class OrphanToolResultRepairResult:
    dry_run: bool
    scanned_orphan_calls: int
    last_event_id: int | None
    recoverable: int
    inserted: int
    skipped_existing: int
    no_source_evidence: int
    no_result_in_source: int
    unparseable_result: int
    findings: list[OrphanToolResultFinding]


@dataclass(frozen=True)
class _OrphanToolResultEvaluation:
    finding: OrphanToolResultFinding
    parsed_event: ParsedEvent | None = None
    raw_json_for_event: str | None = None


def scan_orphan_tool_results(
    db: Session,
    *,
    session_id: UUID | str | None = None,
    after_event_id: int | None = None,
    limit: int = 500,
    max_source_lines_per_call: int = 500,
    archive_store: FilesystemArchiveStore | None = None,
) -> OrphanToolResultScanResult:
    """Classify orphaned assistant tool calls without mutating the database."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if after_event_id is not None and after_event_id < 0:
        raise ValueError("after_event_id must be non-negative")
    if max_source_lines_per_call < 1:
        raise ValueError("max_source_lines_per_call must be at least 1")

    findings: list[OrphanToolResultFinding] = []
    calls = _orphan_tool_calls(db, session_id=session_id, after_event_id=after_event_id, limit=limit)
    for call in calls:
        try:
            evaluation = _evaluate_orphan_call(
                db,
                call,
                max_source_lines_per_call=max_source_lines_per_call,
                archive_store=archive_store,
            )
            findings.append(evaluation.finding)
        except Exception as exc:
            findings.append(_finding(call, "unparseable_result", f"classification failed: {type(exc).__name__}"))
    counts = _finding_counts(findings)
    return OrphanToolResultScanResult(
        scanned_orphan_calls=len(findings),
        last_event_id=max((int(call.id) for call in calls), default=None),
        recoverable=counts["recoverable"],
        no_source_evidence=counts["no_source_evidence"],
        no_result_in_source=counts["no_result_in_source"],
        unparseable_result=counts["unparseable_result"],
        findings=findings,
    )


def repair_orphan_tool_results(
    db: Session,
    *,
    session_id: UUID | str | None = None,
    after_event_id: int | None = None,
    limit: int = 500,
    max_source_lines_per_call: int = 500,
    archive_store: FilesystemArchiveStore | None = None,
    apply: bool = False,
) -> OrphanToolResultRepairResult:
    """Dry-run or apply recovery for orphaned tool-result events.

    The caller owns transaction commit/rollback. When ``apply`` is false, this
    function performs the same classification work without mutating the DB.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if after_event_id is not None and after_event_id < 0:
        raise ValueError("after_event_id must be non-negative")
    if max_source_lines_per_call < 1:
        raise ValueError("max_source_lines_per_call must be at least 1")

    findings: list[OrphanToolResultFinding] = []
    inserted = 0
    skipped_existing = 0
    calls = _orphan_tool_calls(db, session_id=session_id, after_event_id=after_event_id, limit=limit)
    for call in calls:
        try:
            evaluation = _evaluate_orphan_call(
                db,
                call,
                max_source_lines_per_call=max_source_lines_per_call,
                archive_store=archive_store,
            )
        except Exception as exc:
            findings.append(_finding(call, "unparseable_result", f"classification failed: {type(exc).__name__}"))
            continue

        findings.append(evaluation.finding)
        if not apply or evaluation.finding.status != "recoverable" or evaluation.parsed_event is None:
            continue
        if _has_matching_tool_result(db, call):
            skipped_existing += 1
            continue
        reduction = _insert_recovered_tool_result(db, call, evaluation)
        if reduction is not None and reduction.inserted:
            inserted += 1
        else:
            skipped_existing += 1

    counts = _finding_counts(findings)
    return OrphanToolResultRepairResult(
        dry_run=not apply,
        scanned_orphan_calls=len(findings),
        last_event_id=max((int(call.id) for call in calls), default=None),
        recoverable=counts["recoverable"],
        inserted=inserted,
        skipped_existing=skipped_existing,
        no_source_evidence=counts["no_source_evidence"],
        no_result_in_source=counts["no_result_in_source"],
        unparseable_result=counts["unparseable_result"],
        findings=findings,
    )


def _orphan_tool_calls(
    db: Session,
    *,
    session_id: UUID | str | None,
    after_event_id: int | None,
    limit: int,
) -> list[AgentEvent]:
    result = aliased(AgentEvent)
    same_branch = result.branch_id.is_not_distinct_from(AgentEvent.branch_id)
    has_matching_result = (
        db.query(result.id)
        .filter(result.session_id == AgentEvent.session_id)
        .filter(result.role == "tool")
        .filter(result.tool_call_id == AgentEvent.tool_call_id)
        .filter(result.event_origin == "durable")
        .filter(same_branch)
        .exists()
    )
    query = (
        db.query(AgentEvent)
        .filter(AgentEvent.role == "assistant")
        .filter(AgentEvent.tool_name.isnot(None))
        .filter(AgentEvent.tool_call_id.isnot(None))
        .filter(AgentEvent.event_origin == "durable")
        .filter(~has_matching_result)
    )
    if session_id is not None:
        query = query.filter(AgentEvent.session_id == UUID(str(session_id)))
    if after_event_id is not None:
        query = query.filter(AgentEvent.id > int(after_event_id))
    query = query.order_by(AgentEvent.id.asc()).limit(limit)
    return query.all()


def _evaluate_orphan_call(
    db: Session,
    call: AgentEvent,
    *,
    max_source_lines_per_call: int,
    archive_store: FilesystemArchiveStore | None,
) -> _OrphanToolResultEvaluation:
    if not call.source_path:
        return _OrphanToolResultEvaluation(_finding(call, "no_source_evidence", "tool call has no source_path"))
    if call.branch_id is None:
        return _OrphanToolResultEvaluation(_finding(call, "no_source_evidence", "tool call has no branch_id"))

    source_lines = _candidate_source_lines(db, call, limit=max_source_lines_per_call)
    if not source_lines:
        return _OrphanToolResultEvaluation(_finding(call, "no_source_evidence", "no source_lines rows after the tool call"))

    archive_bytes: dict[tuple[str, int, str], str] | None = None
    saw_matching_unparsed_result = False
    for row in source_lines:
        raw, archive_bytes = _source_line_raw(db, call.session_id, row, archive_bytes=archive_bytes, archive_store=archive_store)
        if not raw:
            continue
        parsed_results = parse_tool_result_events_from_raw_line(raw, session_id=str(call.session_id), offset=int(row.source_offset))
        for parsed in parsed_results:
            if parsed.tool_call_id == call.tool_call_id:
                return _OrphanToolResultEvaluation(
                    _finding(
                        call,
                        "recoverable",
                        "matching tool_result found in archived source line",
                        recovered_event_uuid=parsed.uuid,
                        recovered_tool_output_text=parsed.tool_output_text,
                        recovered_source_path=row.source_path,
                        recovered_source_offset=int(row.source_offset),
                    ),
                    parsed_event=parsed,
                    raw_json_for_event=parsed.raw_line or None,
                )
        if _raw_line_mentions_tool_result(raw, str(call.tool_call_id or "")):
            saw_matching_unparsed_result = True

    if saw_matching_unparsed_result:
        return _OrphanToolResultEvaluation(
            _finding(call, "unparseable_result", "matching tool_result raw line no longer parses to a role=tool event")
        )
    return _OrphanToolResultEvaluation(_finding(call, "no_result_in_source", "no matching tool_result found in source evidence"))


def _insert_recovered_tool_result(db: Session, call: AgentEvent, evaluation: _OrphanToolResultEvaluation):
    parsed = evaluation.parsed_event
    if parsed is None or call.branch_id is None:
        return None
    session = db.get(AgentSession, call.session_id)
    if session is None:
        raise ValueError(f"session {call.session_id} not found")

    raw_json = evaluation.raw_json_for_event
    event_uuid, parent_event_uuid = _extract_event_lineage(raw_json)
    observation_result = record_provider_event_observation(
        db,
        session_id=call.session_id,
        thread_id=call.thread_id,
        provider=session.provider,
        device_id=session.device_id,
        source="tool_result_repair",
        branch_id=int(call.branch_id),
        role=parsed.role,
        content_text=parsed.content_text,
        tool_name=parsed.tool_name,
        tool_input_json=parsed.tool_input_json,
        tool_output_text=parsed.tool_output_text,
        tool_call_id=parsed.tool_call_id,
        timestamp=parsed.timestamp,
        source_path=evaluation.finding.recovered_source_path,
        source_offset=evaluation.finding.recovered_source_offset,
        event_hash=_compute_event_hash(parsed, raw_json=raw_json),
        raw_json=raw_json,
        event_uuid=event_uuid,
        parent_event_uuid=parent_event_uuid,
        load_observation=True,
    )
    if observation_result.observation is None:
        return None
    return reduce_provider_event_observation(db, observation_result.observation)


def _has_matching_tool_result(db: Session, call: AgentEvent) -> bool:
    query = (
        db.query(AgentEvent.id)
        .filter(AgentEvent.session_id == call.session_id)
        .filter(AgentEvent.role == "tool")
        .filter(AgentEvent.tool_call_id == call.tool_call_id)
        .filter(AgentEvent.event_origin == "durable")
    )
    if call.branch_id is None:
        query = query.filter(AgentEvent.branch_id.is_(None))
    else:
        query = query.filter(AgentEvent.branch_id == call.branch_id)
    return db.query(query.exists()).scalar() is True


def _compute_event_hash(parsed: ParsedEvent, *, raw_json: str | None) -> str:
    # Keep in lockstep with AgentStore._compute_event_hash; repaired rows must
    # dedupe exactly like the same source line would during normal ingest.
    payload: dict[str, Any] = {
        "role": parsed.role,
        "content_text": parsed.content_text,
        "tool_name": parsed.tool_name,
        "tool_input_json": parsed.tool_input_json,
        "tool_output_text": parsed.tool_output_text,
        "tool_call_id": parsed.tool_call_id,
    }
    if raw_json:
        payload["source_line_hash"] = hashlib.sha256(raw_json.encode()).hexdigest()
    else:
        payload["timestamp"] = parsed.timestamp.isoformat()
    content = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(content.encode()).hexdigest()


def _extract_event_lineage(raw_json: str | None) -> tuple[str | None, str | None]:
    # Keep in lockstep with AgentStore._extract_event_lineage. ParsedEvent.uuid
    # is synthetic for tool results and must not be stored as event_uuid.
    if not raw_json:
        return None, None
    try:
        obj = json.loads(raw_json)
    except json.JSONDecodeError:
        return None, None
    if not isinstance(obj, dict):
        return None, None
    event_uuid = obj.get("uuid")
    parent_uuid = obj.get("parentUuid")
    return (event_uuid if isinstance(event_uuid, str) else None, parent_uuid if isinstance(parent_uuid, str) else None)


def _candidate_source_lines(db: Session, call: AgentEvent, *, limit: int) -> list[AgentSourceLine]:
    query = (
        db.query(AgentSourceLine)
        .filter(AgentSourceLine.session_id == call.session_id)
        .filter(AgentSourceLine.source_path == call.source_path)
        .filter(AgentSourceLine.branch_id == call.branch_id)
    )
    if call.source_offset is not None:
        query = query.filter(AgentSourceLine.source_offset > int(call.source_offset))
    query = query.order_by(AgentSourceLine.source_offset.asc(), AgentSourceLine.revision.asc(), AgentSourceLine.id.asc()).limit(limit)
    return query.all()


def _source_line_raw(
    db: Session,
    session_id: UUID,
    row: AgentSourceLine,
    *,
    archive_bytes: dict[tuple[str, int, str], str] | None,
    archive_store: FilesystemArchiveStore | None,
) -> tuple[str | None, dict[tuple[str, int, str], str] | None]:
    raw = decode_raw_json(row)
    if raw:
        return raw, archive_bytes
    if archive_bytes is None:
        archive_bytes = load_session_source_line_bytes(db, session_id, archive_store=archive_store)
    return archive_bytes.get((row.source_path, int(row.source_offset), row.line_hash)), archive_bytes


def _raw_line_mentions_tool_result(raw: str, tool_call_id: str) -> bool:
    if not tool_call_id:
        return False
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        return False
    if not isinstance(obj, dict):
        return False
    content = obj.get("message", {}).get("content", [])
    if not isinstance(content, list):
        return False
    return any(
        isinstance(item, dict) and item.get("type") == "tool_result" and str(item.get("tool_use_id") or "") == tool_call_id
        for item in content
    )


def _finding_counts(findings: list[OrphanToolResultFinding]) -> dict[ToolResultFindingStatus, int]:
    counts: dict[ToolResultFindingStatus, int] = {
        "recoverable": 0,
        "no_source_evidence": 0,
        "no_result_in_source": 0,
        "unparseable_result": 0,
    }
    for finding in findings:
        counts[finding.status] += 1
    return counts


def _finding(
    call: AgentEvent,
    status: ToolResultFindingStatus,
    reason: str,
    *,
    recovered_event_uuid: str | None = None,
    recovered_tool_output_text: str | None = None,
    recovered_source_path: str | None = None,
    recovered_source_offset: int | None = None,
) -> OrphanToolResultFinding:
    return OrphanToolResultFinding(
        session_id=str(call.session_id),
        event_id=int(call.id),
        tool_call_id=str(call.tool_call_id or ""),
        branch_id=call.branch_id,
        source_path=call.source_path,
        source_offset=int(call.source_offset) if call.source_offset is not None else None,
        status=status,
        reason=reason,
        recovered_event_uuid=recovered_event_uuid,
        recovered_tool_output_text=_preview(recovered_tool_output_text),
        recovered_source_path=recovered_source_path,
        recovered_source_offset=recovered_source_offset,
    )


def _preview(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) <= _RECOVERED_OUTPUT_PREVIEW_CHARS:
        return value
    return f"{value[:_RECOVERED_OUTPUT_PREVIEW_CHARS]}..."
