"""Read-only classification for orphaned tool-result repair.

This service does not insert repaired rows. It separates orphaned assistant tool
calls whose result is recoverable from archived/source-line evidence from calls
that appear genuinely dropped or cannot be reconstructed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import and_
from sqlalchemy import or_
from sqlalchemy.orm import Session
from sqlalchemy.orm import aliased

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSourceLine
from zerg.services.archive_store import FilesystemArchiveStore
from zerg.services.archive_transcript import load_session_source_line_bytes
from zerg.services.raw_json_compression import decode_raw_json
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
    recoverable: int
    no_source_evidence: int
    no_result_in_source: int
    unparseable_result: int
    findings: list[OrphanToolResultFinding]


def scan_orphan_tool_results(
    db: Session,
    *,
    session_id: UUID | str | None = None,
    limit: int = 500,
    max_source_lines_per_call: int = 500,
    archive_store: FilesystemArchiveStore | None = None,
) -> OrphanToolResultScanResult:
    """Classify orphaned assistant tool calls without mutating the database."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if max_source_lines_per_call < 1:
        raise ValueError("max_source_lines_per_call must be at least 1")

    findings: list[OrphanToolResultFinding] = []
    for call in _orphan_tool_calls(db, session_id=session_id, limit=limit):
        try:
            findings.append(
                _classify_orphan_call(
                    db,
                    call,
                    max_source_lines_per_call=max_source_lines_per_call,
                    archive_store=archive_store,
                )
            )
        except Exception as exc:
            findings.append(_finding(call, "unparseable_result", f"classification failed: {type(exc).__name__}"))
    counts = {
        "recoverable": 0,
        "no_source_evidence": 0,
        "no_result_in_source": 0,
        "unparseable_result": 0,
    }
    for finding in findings:
        counts[finding.status] += 1
    return OrphanToolResultScanResult(
        scanned_orphan_calls=len(findings),
        recoverable=counts["recoverable"],
        no_source_evidence=counts["no_source_evidence"],
        no_result_in_source=counts["no_result_in_source"],
        unparseable_result=counts["unparseable_result"],
        findings=findings,
    )


def _orphan_tool_calls(
    db: Session,
    *,
    session_id: UUID | str | None,
    limit: int,
) -> list[AgentEvent]:
    result = aliased(AgentEvent)
    same_branch = or_(
        result.branch_id == AgentEvent.branch_id,
        and_(result.branch_id.is_(None), AgentEvent.branch_id.is_(None)),
    )
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
    query = query.order_by(AgentEvent.session_id, AgentEvent.timestamp, AgentEvent.id).limit(limit)
    return query.all()


def _classify_orphan_call(
    db: Session,
    call: AgentEvent,
    *,
    max_source_lines_per_call: int,
    archive_store: FilesystemArchiveStore | None,
) -> OrphanToolResultFinding:
    if not call.source_path:
        return _finding(call, "no_source_evidence", "tool call has no source_path")
    if call.branch_id is None:
        return _finding(call, "no_source_evidence", "tool call has no branch_id")

    source_lines = _candidate_source_lines(db, call, limit=max_source_lines_per_call)
    if not source_lines:
        return _finding(call, "no_source_evidence", "no source_lines rows after the tool call")

    archive_bytes: dict[tuple[str, int, str], str] | None = None
    saw_matching_unparsed_result = False
    for row in source_lines:
        raw, archive_bytes = _source_line_raw(db, call.session_id, row, archive_bytes=archive_bytes, archive_store=archive_store)
        if not raw:
            continue
        parsed_results = parse_tool_result_events_from_raw_line(raw, session_id=str(call.session_id), offset=int(row.source_offset))
        for parsed in parsed_results:
            if parsed.tool_call_id == call.tool_call_id:
                return _finding(
                    call,
                    "recoverable",
                    "matching tool_result found in archived source line",
                    recovered_event_uuid=parsed.uuid,
                    recovered_tool_output_text=parsed.tool_output_text,
                    recovered_source_path=row.source_path,
                    recovered_source_offset=int(row.source_offset),
                )
        if _raw_line_mentions_tool_result(raw, str(call.tool_call_id or "")):
            saw_matching_unparsed_result = True

    if saw_matching_unparsed_result:
        return _finding(call, "unparseable_result", "matching tool_result raw line no longer parses to a role=tool event")
    return _finding(call, "no_result_in_source", "no matching tool_result found in source evidence")


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
