from __future__ import annotations

import argparse
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Action = Literal["set_null", "report_only"]


@dataclass(frozen=True)
class GuidColumnSpec:
    table: str
    column: str
    primary_key_columns: tuple[str, ...]
    action: Action


@dataclass(frozen=True)
class GuidFinding:
    db_path: str
    table: str
    column: str
    primary_key: dict[str, object]
    value: str
    action: Action


@dataclass(frozen=True)
class RepairSummary:
    findings: tuple[GuidFinding, ...]
    repaired_count: int
    unsupported_count: int


_DEFAULT_ROOT = Path("/var/app-data/longhouse")

# Keep this explicit so the repair tool stays stdlib-only and runnable without
# booting the full app config. Update it when tenant SQLite schema adds/removes
# GUID-backed columns.
GUID_COLUMN_SPECS: tuple[GuidColumnSpec, ...] = (
    GuidColumnSpec("device_tokens", "id", ("id",), "report_only"),
    GuidColumnSpec("memories", "id", ("id",), "report_only"),
    GuidColumnSpec("runs", "trace_id", ("id",), "set_null"),
    GuidColumnSpec("runs", "assistant_message_id", ("id",), "set_null"),
    GuidColumnSpec("commis_jobs", "trace_id", ("id",), "set_null"),
    GuidColumnSpec("llm_audit_log", "trace_id", ("id",), "set_null"),
    GuidColumnSpec("llm_audit_log", "span_id", ("id",), "set_null"),
    GuidColumnSpec("action_proposals", "id", ("id",), "report_only"),
    GuidColumnSpec("action_proposals", "insight_id", ("id",), "report_only"),
    GuidColumnSpec("action_proposals", "reflection_run_id", ("id",), "set_null"),
    GuidColumnSpec("insights", "id", ("id",), "report_only"),
    GuidColumnSpec("insights", "session_id", ("id",), "set_null"),
    GuidColumnSpec("reflection_runs", "id", ("id",), "report_only"),
    GuidColumnSpec("sessions", "id", ("id",), "report_only"),
    GuidColumnSpec("events", "session_id", ("id",), "report_only"),
    GuidColumnSpec("session_branches", "session_id", ("id",), "report_only"),
    GuidColumnSpec("session_embeddings", "session_id", ("id",), "report_only"),
    GuidColumnSpec("source_lines", "session_id", ("id",), "report_only"),
)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return {str(row[1]) for row in rows}


def _validate_uuid(raw: object) -> bool:
    if raw is None:
        return True
    text = str(raw).strip()
    if not text:
        return False
    try:
        uuid.UUID(text)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def find_db_paths(*, root: str | Path | None = None, db_path: str | Path | None = None, instance: str | None = None) -> list[Path]:
    if db_path:
        path = Path(db_path)
        if not path.is_file():
            raise FileNotFoundError(path)
        return [path]

    root_path = Path(root or _DEFAULT_ROOT)
    if instance:
        candidate = root_path / instance / "longhouse.db"
        if not candidate.is_file():
            raise FileNotFoundError(candidate)
        return [candidate]

    return sorted(path for path in root_path.glob("*/longhouse.db") if path.is_file())


def scan_db(db_path: str | Path) -> list[GuidFinding]:
    conn = sqlite3.connect(str(db_path))
    try:
        existing_tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        findings: list[GuidFinding] = []
        for spec in GUID_COLUMN_SPECS:
            if spec.table not in existing_tables:
                continue
            columns = _table_columns(conn, spec.table)
            if spec.column not in columns:
                continue
            select_cols = [*spec.primary_key_columns, spec.column]
            query = "SELECT " + ", ".join(f'"{name}"' for name in select_cols) + f' FROM "{spec.table}" WHERE "{spec.column}" IS NOT NULL'
            for row in conn.execute(query):
                values = dict(zip(select_cols, row, strict=False))
                raw_value = values.get(spec.column)
                if _validate_uuid(raw_value):
                    continue
                findings.append(
                    GuidFinding(
                        db_path=str(db_path),
                        table=spec.table,
                        column=spec.column,
                        primary_key={name: values.get(name) for name in spec.primary_key_columns},
                        value="" if raw_value is None else str(raw_value),
                        action=spec.action,
                    )
                )
        return findings
    finally:
        conn.close()


def repair_db(db_path: str | Path) -> RepairSummary:
    findings = scan_db(db_path)
    conn = sqlite3.connect(str(db_path))
    repaired_count = 0
    try:
        for finding in findings:
            if finding.action != "set_null":
                continue
            where = " AND ".join(f'"{name}" = ?' for name in finding.primary_key)
            params = [finding.primary_key[name] for name in finding.primary_key]
            conn.execute(
                f'UPDATE "{finding.table}" SET "{finding.column}" = NULL WHERE {where}',
                params,
            )
            repaired_count += 1
        if repaired_count:
            conn.commit()
        return RepairSummary(
            findings=tuple(findings),
            repaired_count=repaired_count,
            unsupported_count=sum(1 for finding in findings if finding.action == "report_only"),
        )
    finally:
        conn.close()


def render_summary(summary: RepairSummary) -> str:
    if not summary.findings:
        return "No malformed GUID values found."

    lines = []
    for finding in summary.findings:
        pk = ", ".join(f"{key}={value}" for key, value in finding.primary_key.items()) or "<no-pk>"
        lines.append(f"{finding.db_path}: {finding.table}.{finding.column} [{pk}] value={finding.value!r} action={finding.action}")
    lines.append(f"Summary: findings={len(summary.findings)} repaired={summary.repaired_count} unsupported={summary.unsupported_count}")
    return "\n".join(lines)


def run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan and repair malformed GUID values in tenant SQLite DBs.")
    parser.add_argument("--root", default=str(_DEFAULT_ROOT), help="Instance root directory (default: /var/app-data/longhouse)")
    parser.add_argument("--db-path", help="Single SQLite DB path to inspect")
    parser.add_argument("--instance", help="Inspect only one instance under --root")
    parser.add_argument("--apply", action="store_true", help="Apply safe repairs in-place")
    args = parser.parse_args(argv)

    db_paths = find_db_paths(root=args.root, db_path=args.db_path, instance=args.instance)
    if not db_paths:
        print("No tenant DBs found.")
        return 0

    exit_code = 0
    for path in db_paths:
        summary = repair_db(path) if args.apply else RepairSummary(tuple(scan_db(path)), repaired_count=0, unsupported_count=0)
        if not args.apply:
            summary = RepairSummary(
                findings=summary.findings,
                repaired_count=0,
                unsupported_count=sum(1 for finding in summary.findings if finding.action == "report_only"),
            )
        print(render_summary(summary))
        if args.apply:
            if summary.unsupported_count:
                exit_code = 1
        elif summary.findings:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(run_cli())
