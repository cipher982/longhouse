#!/usr/bin/env python3
"""Capture a diffable storage-and-contention baseline for the local Machine Agent.

The 2026-09-17 incident was measured by hand: the agent database had grown to
831 MB with 626 MB of interior free space inside `cursor_store_raw_record`,
write-lock waits reached 2.7 s with five sessions live, and the OMP Helm
launcher's required identity binding lost that race into a permanent degraded
state. `machine-agent-storage-lifetimes.md` changes how payload bytes are
stored, so the same numbers have to be reproducible before and after rather
than re-derived from memory.

A baseline that cannot say "I could not measure this" is worse than no baseline:
an empty database, a build without `dbstat`, and a checkpointed WAL all look
like good news. Every measurement below either lands in `report.json` as a
number, or is named in `degraded` with the reason it was not taken.

Read-only. The write-lock probe runs `BEGIN IMMEDIATE; ROLLBACK`, which takes the
write lock for the duration of a rollback and modifies no row.

Usage:
    scripts/ops/agent-storage-baseline.py [--out DIR] [--samples N] [--date YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

LEDGER_TABLES = (
    "source_epoch_registry",
    "source_epoch_lane_state",
    "cursor_store_raw_record",
    "cursor_store_capture_cursor",
    "cursor_store_root_state",
    "pending_source_envelope",
    "file_state",
    "session_binding",
)
STATUS_FIELDS = (
    "version",
    "last_ship_result",
    "last_ship_latency_ms",
    "spool_pending_count",
    "spool_dead_count",
    "local_database_bytes",
    "ship_attempts_1h",
    "ship_successes_1h",
    "ship_latency_p50_ms_1h",
    "ship_latency_p95_ms_1h",
    "parse_error_count_1h",
    "ship_connect_errors_1h",
)
WARNING_KINDS = re.compile(r"(?:WARN|ERROR) (?P<module>[a-z_:]+): (?P<message>[A-Za-z ]+)")


def collect(db: Path, samples: int, day: str) -> tuple[dict, list[str]]:
    report: dict = {"captured_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    degraded: list[str] = []

    for suffix, key in (("", "main_bytes"), ("-wal", "wal_bytes"), ("-shm", "shm_bytes")):
        candidate = Path(f"{db}{suffix}")
        report[key] = candidate.stat().st_size if candidate.exists() else None

    if db.stat().st_size == 0:
        degraded.append("database_file_empty: every schema measurement was skipped")
        return finish(report, degraded)

    uri = f"file:{db}?mode=ro"
    try:
        conn_ctx = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as error:
        degraded.append(f"database_open_failed: {error}")
        return finish(report, degraded)
    with conn_ctx as conn:
        try:
            report["journal_mode"] = conn.execute("pragma journal_mode").fetchone()[0]
            report["page_size"] = conn.execute("pragma page_size").fetchone()[0]
            report["page_count"] = conn.execute("pragma page_count").fetchone()[0]
            report["freelist_pages"] = conn.execute("pragma freelist_count").fetchone()[0]
        except sqlite3.Error as error:
            degraded.append(f"pragma_read_failed: {error}")
            return finish(report, degraded)

        if report["page_count"] == 0:
            degraded.append("database_uninitialized: page_count is zero")
            return finish(report, degraded)

        try:
            row = conn.execute("select coalesce(sum(unused), 0) from dbstat").fetchone()
            report["interior_unused_bytes"] = int(row[0])
            rows = conn.execute(
                "select name, sum(pgsize) from dbstat group by name order by 2 desc limit 16"
            ).fetchall()
            report["largest_allocations"] = {name: int(size) for name, size in rows}
        except sqlite3.Error as error:
            degraded.append(f"dbstat_unavailable: {error} — interior free space not measured")
            report["interior_unused_bytes"] = None

        tables = [
            name
            for (name,) in conn.execute(
                "select name from sqlite_master where type='table' order by name"
            )
        ]
        report["tables"] = tables
        counts: dict[str, int | None] = {}
        for name in tables:
            if name == "sqlite_sequence":
                continue
            try:
                counts[name] = int(conn.execute(f"select count(*) from {name}").fetchone()[0])
            except sqlite3.Error as error:
                counts[name] = None
                degraded.append(f"row_count_failed:{name}: {error}")
        report["row_counts"] = counts

    report["wal_checkpoint"] = checkpoint_state(db)
    if report["wal_checkpoint"].get("degraded"):
        degraded.append(report["wal_checkpoint"]["degraded"])

    report["write_lock_probe"] = lock_probe(db, samples)
    if report["write_lock_probe"]["degraded"]:
        degraded.append(report["write_lock_probe"]["degraded"])

    openers_result = openers(db)
    if openers_result.get("degraded"):
        degraded.append(openers_result["degraded"])
    report["openers"] = openers_result.get("openers", [])
    report["opener_writers"] = openers_result.get("writers")
    report["status"] = engine_status(db.parent / "engine-status.json", degraded)
    report["warnings"] = warning_kinds(db.parent / "logs", day)
    return finish(report, degraded)


def finish(report: dict, degraded: list[str]) -> tuple[dict, list[str]]:
    """One place decides what a report says about its own completeness."""
    report["degraded"] = degraded
    report["healthy_baseline"] = not degraded
    return report, degraded


def checkpoint_state(db: Path) -> dict:
    """WAL content versus its high-water mark.

    `-wal` bytes are a high-water mark: SQLite does not shrink the file, so an
    apparent 160 MB WAL can hold four frames. Frames reported by a passive
    checkpoint are the real backlog.
    """
    try:
        conn = sqlite3.connect(f"file:{db}", uri=True, timeout=5.0, isolation_level=None)
        try:
            conn.execute("pragma busy_timeout=5000")
            busy, log_frames, checkpointed = conn.execute(
                "pragma wal_checkpoint(PASSIVE)"
            ).fetchone()
            return {
                "busy": bool(busy),
                "log_frames": int(log_frames),
                "checkpointed_frames": int(checkpointed),
                "backlog_frames": max(0, int(log_frames) - int(checkpointed)),
            }
        finally:
            conn.close()
    except sqlite3.Error as error:
        return {"degraded": f"wal_checkpoint_failed: {error}"}


def lock_probe(db: Path, samples: int) -> dict:
    waits: list[float] = []
    failures = 0
    for _ in range(samples):
        start = time.perf_counter()
        try:
            conn = sqlite3.connect(f"file:{db}", uri=True, timeout=5.0, isolation_level=None)
            try:
                conn.execute("pragma busy_timeout=5000")
                conn.execute("begin immediate")
                conn.execute("rollback")
                waits.append((time.perf_counter() - start) * 1000.0)
            finally:
                conn.close()
        except sqlite3.Error as error:
            failures += 1
            if failures == 1:
                first_error = str(error)
        time.sleep(0.25)
    if not waits:
        return {"samples": samples, "failures": failures, "degraded": "lock_probe_unavailable"}
    if failures:
        partial = f"lock_probe_partial: {failures} of {samples} samples could not take the write lock"
    else:
        partial = None
    ordered = sorted(waits)
    return {
        "samples": samples,
        "failures": failures,
        "p50_ms": round(ordered[len(ordered) // 2]),
        "p95_ms": round(ordered[int(len(ordered) * 0.95) - 1]),
        "max_ms": round(ordered[-1]),
        "over_200ms": sum(1 for value in ordered if value > 200),
        "over_1000ms": sum(1 for value in ordered if value > 1000),
        "first_error": first_error if failures else None,
        "degraded": partial,
    }


def openers(db: Path) -> dict:
    """Who holds the database, and in which access mode.

    Only the daemon should hold it for write at rest. Launchers open it
    transiently on the launch path, so presence alone is not proof; the mode
    (`r`, `w`, `u`) is what distinguishes a reader from a writer. lsof's field
    output does not carry the mode -- its FD field does -- so this parses the
    ordinary table, and a failed `lsof` is reported as unmeasured rather than as
    an empty opener set.
    """
    try:
        result = subprocess.run(
            ["lsof", "--", str(db)], capture_output=True, text=True, timeout=20
        )
        if result.returncode not in (0, 1):  # 1 is lsof's "nothing to report"
            return {"degraded": f"lsof_failed: exit {result.returncode}"}
    except (OSError, subprocess.SubprocessError) as error:
        return {"degraded": f"lsof_unavailable: {error}"}

    rows: list[dict] = []
    for line in result.stdout.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 5:
            continue
        descriptor = fields[3]
        rows.append(
            {
                "command": fields[0],
                "pid": fields[1],
                "fd": descriptor[:-1] if descriptor[-1:] in {"r", "w", "u"} else descriptor,
                "mode": descriptor[-1:] if descriptor[-1:] in {"r", "w", "u"} else "unknown",
            }
        )
    return {
        "openers": rows,
        "writers": sum(1 for row in rows if row["mode"] in {"w", "u"}),
    }


def engine_status(path: Path, degraded: list[str]) -> dict:
    if not path.exists():
        degraded.append("engine_status_missing")
        return {}
    try:
        status = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        degraded.append(f"engine_status_unreadable: {error}")
        return {}
    return {
        **{field: status.get(field) for field in STATUS_FIELDS},
        "archive_backlog_state": status.get("archive_backlog", {}).get("state"),
    }


def warning_kinds(log_dir: Path, day: str) -> dict:
    log = log_dir / f"engine.log.{day}"
    if not log.exists():
        return {}
    counts: dict[str, int] = {}
    for line in log.read_text(errors="replace").splitlines():
        if " WARN " not in line and " ERROR " not in line:
            continue
        match = WARNING_KINDS.search(line)
        if not match:
            continue
        key = f"{match.group('module')}: {match.group('message').strip()}"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: item[1], reverse=True)[:20])


def summarize(report: dict) -> str:
    lines: list[str] = [f"healthy_baseline={report.get('healthy_baseline')}"]
    for key in ("captured_at", "main_bytes", "wal_bytes", "shm_bytes", "journal_mode",
                "page_size", "page_count", "freelist_pages", "interior_unused_bytes"):
        lines.append(f"{key}={report.get(key)}")
    wal = report.get("wal_checkpoint") or {}
    lines.append(f"wal_log_frames={wal.get('log_frames')} backlog_frames={wal.get('backlog_frames')}")
    probe = report.get("write_lock_probe") or {}
    lines.append(
        f"lock_p50_ms={probe.get('p50_ms')} lock_p95_ms={probe.get('p95_ms')} "
        f"lock_max_ms={probe.get('max_ms')} failures={probe.get('failures')}"
    )
    lines.append(
        f"opener_writers={report.get('opener_writers')} openers={json.dumps(report.get('openers'))}"
    )
    counts = report.get("row_counts") or {}
    for table in LEDGER_TABLES:
        if table in counts:
            lines.append(f"rows.{table}={counts[table]}")
    status = report.get("status") or {}
    for field in STATUS_FIELDS:
        lines.append(f"status.{field}={status.get(field)}")
    for key, value in (report.get("warnings") or {}).items():
        lines.append(f"warn[{key}]={value}")
    if report.get("degraded"):
        lines.append("DEGRADED MEASUREMENTS (do not read this as a healthy baseline):")
        for item in report["degraded"]:
            lines.append(f"  - {item}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="directory for report.json and summary.txt")
    parser.add_argument("--samples", type=int, default=40, help="write-lock probe samples")
    parser.add_argument(
        "--date",
        default=dt.datetime.now().strftime("%Y-%m-%d"),
        help="local date of the log to summarize",
    )
    parser.add_argument(
        "--longhouse-home",
        type=Path,
        default=Path(os.environ.get("LONGHOUSE_HOME", Path.home() / ".longhouse")),
    )
    args = parser.parse_args()

    db = args.longhouse_home / "agent" / "longhouse-shipper.db"
    if not db.exists():
        print(f"no agent database at {db}", file=sys.stderr)
        return 1

    report, _ = collect(db, args.samples, args.date)
    summary = summarize(report)

    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True))
        (args.out / "summary.txt").write_text(summary + "\n")
        print(f"wrote {args.out / 'report.json'} and {args.out / 'summary.txt'}")
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
