#!/usr/bin/env python3
"""
Profile Longhouse pipeline stages against "Speed of Light" physical minimums.

Measures data distributions (count, sum, p50, p95, p99, max) and dimensionless
amplification ratios across:
  Stage 0: Local provider transcripts and Console RPC stdout.log files
  Stage 1: Local managed state directories, engine-status.json, and engine logs
  Stage 2: Local shipper source_epoch_registry rotation amplification

Usage:
  python3 scripts/ops/pipeline_speed_of_light.py [--home ~/.longhouse] [--json]
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import math
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any


def pct(values: list[float | int], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(math.ceil((q / 100.0) * len(ordered))) - 1))
    return float(ordered[idx])


def dist_summary(values: list[float | int]) -> dict[str, float | int]:
    if not values:
        return {"count": 0, "sum": 0, "min": 0, "p50": 0, "p95": 0, "p99": 0, "max": 0, "mean": 0}
    ordered = sorted(values)
    total = sum(ordered)
    return {
        "count": len(ordered),
        "sum": total,
        "min": ordered[0],
        "p50": pct(ordered, 50),
        "p95": pct(ordered, 95),
        "p99": pct(ordered, 99),
        "max": ordered[-1],
        "mean": round(total / len(ordered), 2),
    }


def profile_console_stdout(longhouse_home: Path) -> dict[str, Any]:
    agent_dir = longhouse_home / "agent"
    console_dirs = glob.glob(str(agent_dir / "*-console/*/*"))
    stdout_sizes: list[int] = []
    by_adapter: dict[str, list[int]] = collections.defaultdict(list)
    total_lines = 0
    non_text_delta_lines = 0

    for d in console_dirs:
        p = Path(d)
        adapter = p.parent.parent.name
        stdout = p / "stdout.log"
        if not stdout.exists():
            continue
        try:
            sz = stdout.stat().st_size
        except OSError:
            continue
        stdout_sizes.append(sz)
        by_adapter[adapter].append(sz)
        if adapter in ("omp-console", "pi-console"):
            try:
                with open(stdout, "rb") as fh:
                    for raw_line in fh:
                        total_lines += 1
                        if b'"message_update"' in raw_line and any(
                            token in raw_line
                            for token in (
                                b'"thinking_delta"',
                                b'"toolcall_delta"',
                                b'"thinking_start"',
                                b'"thinking_end"',
                                b'"toolcall_start"',
                                b'"toolcall_end"',
                            )
                        ):
                            non_text_delta_lines += 1
            except OSError:
                continue

    return {
        "total_console_run_dirs": len(console_dirs),
        "stdout_log_bytes": dist_summary(stdout_sizes),
        "by_adapter": {k: dist_summary(v) for k, v in by_adapter.items()},
        "rpc_total_lines": total_lines,
        "rpc_non_text_delta_lines": non_text_delta_lines,
        "rpc_non_text_delta_ratio": round(non_text_delta_lines / max(1, total_lines), 4),
    }


def profile_local_state(longhouse_home: Path, user_home: Path) -> dict[str, Any]:
    try:
        ps_out = subprocess.check_output(["ps", "-axo", "pid"], text=True)
        live_pids = {int(x.strip()) for x in ps_out.splitlines()[1:] if x.strip().isdigit()}
    except Exception:
        live_pids = set()

    state_dirs = {
        "claude_channel_sessions": str(user_home / ".claude/channels/longhouse/sessions/*.json"),
        "omp_helm": str(longhouse_home / "managed-local/omp-helm/*.json"),
        "pi_helm": str(longhouse_home / "managed-local/pi-helm/*.json"),
        "cursor_helm": str(longhouse_home / "managed-local/cursor-helm/*.json"),
        "turn_claims": str(longhouse_home / "agent/turn-claims/*.json"),
    }
    inventory: dict[str, Any] = {}
    total_alive = 0
    total_dead = 0
    for name, pattern in state_dirs.items():
        files = glob.glob(pattern)
        alive = 0
        dead = 0
        unmarked_dead_claude = 0
        for fpath in files:
            try:
                with open(fpath) as fh:
                    data = json.load(fh)
                if not isinstance(data, dict):
                    continue
                pid = (
                    data.get("pid")
                    or data.get("claude_pid")
                    or data.get("bridge_pid")
                    or data.get("provider_pid")
                )
                if isinstance(pid, int) and pid in live_pids:
                    alive += 1
                else:
                    dead += 1
                    if name == "claude_channel_sessions" and not data.get("exited_at"):
                        unmarked_dead_claude += 1
            except Exception:
                continue
        total_alive += alive
        total_dead += dead
        inventory[name] = {
            "files": len(files),
            "alive": alive,
            "dead": dead,
            "unmarked_dead": unmarked_dead_claude if name == "claude_channel_sessions" else 0,
        }

    return {
        "inventory": inventory,
        "dead_to_live_file_ratio": round(total_dead / max(1, total_alive), 2),
    }


def profile_shipper_epochs(longhouse_home: Path) -> dict[str, Any]:
    db_path = longhouse_home / "agent/longhouse-shipper.db"
    if not db_path.exists():
        return {}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT provider,
                       count(*) AS total_epochs,
                       count(DISTINCT opaque_source_id) AS distinct_sources,
                       sum(CASE WHEN start_reason = 'revision_change' THEN 1 ELSE 0 END) AS revision_change_epochs
                FROM source_epoch_registry
                GROUP BY provider
                ORDER BY total_epochs DESC
                """
            ).fetchall()
        ]
        for row in rows:
            sources = max(1, int(row["distinct_sources"] or 1))
            row["epochs_per_source_ratio"] = round(int(row["total_epochs"] or 0) / sources, 2)
        return {"by_provider": rows}
    except Exception:
        return {}
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home() / ".longhouse")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    args = parser.parse_args()

    started = time.time()
    user_home = Path.home()
    report = {
        "console_stdout": profile_console_stdout(args.home),
        "local_state": profile_local_state(args.home, user_home),
        "shipper_epochs": profile_shipper_epochs(args.home),
        "elapsed_sec": round(time.time() - started, 2),
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
