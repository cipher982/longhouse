#!/usr/bin/env python3
"""Verify real Claude transcript fidelity against ingest/export + rewind semantics.

This script intentionally uses actual Claude Code JSONL sessions from ~/.claude/projects
instead of synthetic fixtures.

Checks:
1. Lossless roundtrip: source transcript lines roundtrip byte-for-byte (normalized newline form)
2. Rewind signal parity: raw parentUuid fan-out implies >1 stored branch after incremental ingest
3. Compaction visibility: summary + compact_boundary line counts are reported from raw transcript
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import defaultdict
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from uuid import UUID
from uuid import uuid4

from sqlalchemy import func
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "apps" / "zerg" / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from zerg.database import make_engine
from zerg.models.agents import AgentSessionBranch
from zerg.models.agents import AgentsBase
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest
from zerg.services.agents_store import SourceLineIngest
from zerg.services.shipper.parser import extract_session_metadata
from zerg.services.shipper.parser import parse_session_file


@dataclass
class RawTranscriptMetrics:
    path: str
    lines_total: int
    summary_lines: int
    compact_boundary_lines: int
    microcompact_boundary_lines: int
    parent_fanout_points: int
    parent_fanout_max_width: int


@dataclass
class IngestFidelityResult:
    transcript_path: str
    session_id: str
    batches: int
    raw: RawTranscriptMetrics
    roundtrip_match: bool
    exported_bytes: int
    expected_bytes: int
    stored_branches: int
    stored_branch_reasons: list[str]
    head_events: int
    forensic_events: int


def _read_source_lines(path: Path) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    with path.open("rb") as fh:
        offset = 0
        for raw in fh:
            rows.append((offset, raw.rstrip(b"\r\n").decode("utf-8", errors="replace")))
            offset += len(raw)
    return rows


def _normalized_bytes(rows: list[tuple[int, str]]) -> bytes:
    return b"".join((line + "\n").encode("utf-8") for _offset, line in rows)


def _raw_metrics(path: Path, rows: list[tuple[int, str]]) -> RawTranscriptMetrics:
    summary_lines = 0
    compact_boundary_lines = 0
    microcompact_boundary_lines = 0
    children_by_parent: dict[str, set[str]] = defaultdict(set)

    for _offset, line in rows:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue

        typ = obj.get("type")
        if typ == "summary":
            summary_lines += 1
        elif typ == "system":
            subtype = obj.get("subtype")
            if subtype == "compact_boundary":
                compact_boundary_lines += 1
            elif subtype == "microcompact_boundary":
                microcompact_boundary_lines += 1

        parent_uuid = obj.get("parentUuid")
        event_uuid = obj.get("uuid")
        if isinstance(parent_uuid, str) and isinstance(event_uuid, str):
            children_by_parent[parent_uuid].add(event_uuid)

    fanout_widths = [len(children) for children in children_by_parent.values() if len(children) > 1]
    return RawTranscriptMetrics(
        path=str(path),
        lines_total=len(rows),
        summary_lines=summary_lines,
        compact_boundary_lines=compact_boundary_lines,
        microcompact_boundary_lines=microcompact_boundary_lines,
        parent_fanout_points=len(fanout_widths),
        parent_fanout_max_width=max(fanout_widths) if fanout_widths else 0,
    )


def _find_transcripts(root: Path, limit: int) -> list[Path]:
    candidates = [p for p in root.rglob("*.jsonl") if p.is_file()]
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if limit > 0:
        candidates = candidates[:limit]
    return candidates


def _pick_default_transcript(root: Path, limit: int) -> Path:
    candidates = _find_transcripts(root, limit)
    if not candidates:
        raise FileNotFoundError(f"No transcript files found under {root}")

    scored: list[tuple[int, RawTranscriptMetrics, Path]] = []
    for path in candidates:
        rows = _read_source_lines(path)
        if not rows:
            continue
        metrics = _raw_metrics(path, rows)
        # Prefer sessions that stress compaction + rewind.
        score = (
            metrics.parent_fanout_points * 100
            + metrics.summary_lines * 10
            + metrics.compact_boundary_lines * 5
            + min(metrics.lines_total, 1000)
        )
        scored.append((score, metrics, path))

    if not scored:
        raise FileNotFoundError(f"No non-empty transcript files found under {root}")

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][2]


def _resolve_transcript_path(args: argparse.Namespace) -> Path:
    if args.transcript:
        path = Path(args.transcript).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Transcript not found: {path}")
        return path

    root = Path(args.claude_projects_dir).expanduser().resolve()
    if args.session_id:
        matches = list(root.rglob(f"{args.session_id}.jsonl"))
        if not matches:
            raise FileNotFoundError(f"No transcript found for session_id={args.session_id} under {root}")
        return matches[0]

    return _pick_default_transcript(root, args.scan_limit)


def _to_event_ingest(parsed_event: Any, source_path: str) -> EventIngest:
    return EventIngest(
        role=parsed_event.role,
        content_text=parsed_event.content_text,
        tool_name=parsed_event.tool_name,
        tool_input_json=parsed_event.tool_input_json,
        tool_output_text=parsed_event.tool_output_text,
        tool_call_id=parsed_event.tool_call_id,
        timestamp=parsed_event.timestamp,
        source_path=source_path,
        source_offset=int(parsed_event.source_offset),
        raw_json=parsed_event.raw_line or None,
    )


def run_fidelity_check(path: Path, *, batch_size: int) -> IngestFidelityResult:
    rows = _read_source_lines(path)
    if not rows:
        raise ValueError(f"Transcript is empty: {path}")

    raw_metrics = _raw_metrics(path, rows)
    expected_bytes = _normalized_bytes(rows)

    parsed_events = list(parse_session_file(path))
    events_by_offset: dict[int, list[Any]] = defaultdict(list)
    for event in parsed_events:
        events_by_offset[int(event.source_offset)].append(event)

    metadata = extract_session_metadata(path)
    started_at = metadata.started_at or datetime.now(timezone.utc)
    ended_at = metadata.ended_at

    db_path = Path(tempfile.mkdtemp(prefix="longhouse-real-transcript-")) / "fidelity.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    AgentsBase.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)

    session_uuid = uuid4()
    source_path = str(path)
    batches = 0

    with SessionLocal() as db:
        store = AgentsStore(db)
        for idx in range(0, len(rows), batch_size):
            batch_rows = rows[idx : idx + batch_size]
            batches += 1

            batch_source_lines = [
                SourceLineIngest(source_path=source_path, source_offset=offset, raw_json=line)
                for offset, line in batch_rows
            ]
            batch_events: list[EventIngest] = []
            for offset, _line in batch_rows:
                for parsed_event in events_by_offset.get(offset, []):
                    batch_events.append(_to_event_ingest(parsed_event, source_path))

            is_final_batch = idx + batch_size >= len(rows)
            store.ingest_session(
                SessionIngest(
                    id=session_uuid,
                    provider="claude",
                    environment="test",
                    project=metadata.project or "real-transcript-fidelity",
                    device_id="real-transcript-fidelity",
                    cwd=metadata.cwd or str(path.parent),
                    git_repo=None,
                    git_branch=metadata.git_branch,
                    started_at=started_at,
                    ended_at=ended_at if is_final_batch else None,
                    provider_session_id=path.stem,
                    events=batch_events,
                    source_lines=batch_source_lines,
                )
            )

        exported = store.export_session_jsonl(session_uuid, branch_mode="all")
        if exported is None:
            raise RuntimeError("Failed to export ingested session")
        exported_bytes, _ = exported

        branch_rows = (
            db.query(AgentSessionBranch)
            .filter(AgentSessionBranch.session_id == session_uuid)
            .order_by(AgentSessionBranch.id.asc())
            .all()
        )
        branch_reasons = [str(row.branch_reason or "") for row in branch_rows if row.branch_reason]

        head_events = store.count_session_events(session_uuid, branch_mode="head")
        forensic_events = store.count_session_events(session_uuid, branch_mode="all")

    return IngestFidelityResult(
        transcript_path=source_path,
        session_id=str(session_uuid),
        batches=batches,
        raw=raw_metrics,
        roundtrip_match=(exported_bytes == expected_bytes),
        exported_bytes=len(exported_bytes),
        expected_bytes=len(expected_bytes),
        stored_branches=len(branch_rows),
        stored_branch_reasons=branch_reasons,
        head_events=head_events,
        forensic_events=forensic_events,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transcript", type=str, default=None, help="Absolute path to a Claude JSONL transcript.")
    parser.add_argument("--session-id", type=str, default=None, help="Claude provider session id (filename stem).")
    parser.add_argument(
        "--claude-projects-dir",
        type=str,
        default="~/.claude/projects",
        help="Root folder to scan for Claude transcripts when --transcript is not set.",
    )
    parser.add_argument("--scan-limit", type=int, default=300, help="Max transcript files to score during auto-pick.")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1,
        help="Lines per ingest batch to simulate incremental shipping behavior.",
    )
    parser.add_argument(
        "--strict-fanout",
        action="store_true",
        help="Fail when raw parent fan-out exists but stored branches remain <= 1.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    path = _resolve_transcript_path(args)
    result = run_fidelity_check(path, batch_size=max(args.batch_size, 1))
    payload = asdict(result)
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))

    failed = False
    if not result.roundtrip_match:
        print("ERROR: Exported transcript does not roundtrip to normalized source bytes.", file=sys.stderr)
        failed = True

    raw_fanout = result.raw.parent_fanout_points
    if args.strict_fanout and raw_fanout > 0 and result.stored_branches <= 1:
        print(
            "ERROR: Raw transcript shows parent fan-out but stored branches did not fork.",
            file=sys.stderr,
        )
        failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
