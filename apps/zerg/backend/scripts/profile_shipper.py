#!/usr/bin/env python3
"""Shipper profiling harness for Rust rewrite planning.

Profiles the shipper at 3 scale levels against real data and real API:
  1. Single largest session file (baseline per-file cost)
  2. 10% of session files (amortized costs, SQLite contention)
  3. 100% of session files (sustained load, memory growth, tail latency)

For each level, captures per-phase timing:
  - File discovery (glob + stat)
  - File read + parse (disk I/O + CPU)
  - Metadata extraction (disk I/O)
  - Payload build + JSON serialization (CPU + alloc)
  - Gzip compression (CPU)
  - HTTP POST (network I/O)
  - State update (SQLite write)

Output: JSON report + optional py-spy flame graph + optional memray trace.

Usage:
    # Dry run (no HTTP, just parse + build payload)
    uv run python scripts/profile_shipper.py --dry-run

    # Against real API (will ship to control plane)
    uv run python scripts/profile_shipper.py --api-url https://david010.longhouse.ai --token $TOKEN

    # Single file only
    uv run python scripts/profile_shipper.py --level 1 --dry-run

    # With py-spy flame graph (requires sudo)
    uv run python scripts/profile_shipper.py --dry-run --py-spy

    # With memray allocation trace
    uv run python scripts/profile_shipper.py --dry-run --memray
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import logging
import os
import random
import statistics
import sys
import time
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

try:
    import orjson

    def _dumps(obj: Any) -> bytes:
        return orjson.dumps(obj)

except ImportError:
    def _dumps(obj: Any) -> bytes:  # type: ignore[misc]
        return json.dumps(obj).encode("utf-8")

from zerg.services.shipper.parser import extract_session_metadata
from zerg.services.shipper.parser import parse_session_file_full
from zerg.services.shipper.parser import parse_session_file_with_offset
from zerg.services.shipper.shipper import SessionShipper, ShipperConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Timing infrastructure
# ---------------------------------------------------------------------------

@dataclass
class PhaseTimer:
    """Captures wall-clock and CPU time for a named phase."""

    name: str
    wall_ms: float = 0.0
    cpu_ms: float = 0.0
    bytes_in: int = 0
    bytes_out: int = 0
    items: int = 0  # events, files, etc.

    def as_dict(self) -> dict:
        d = {
            "name": self.name,
            "wall_ms": round(self.wall_ms, 3),
            "cpu_ms": round(self.cpu_ms, 3),
        }
        if self.bytes_in:
            d["bytes_in"] = self.bytes_in
        if self.bytes_out:
            d["bytes_out"] = self.bytes_out
        if self.items:
            d["items"] = self.items
        return d


class Timer:
    """Context manager that measures wall + CPU time."""

    def __init__(self, phase: PhaseTimer):
        self._phase = phase

    def __enter__(self):
        self._wall_start = time.perf_counter()
        self._cpu_start = time.process_time()
        return self

    def __exit__(self, *args):
        self._phase.wall_ms += (time.perf_counter() - self._wall_start) * 1000
        self._phase.cpu_ms += (time.process_time() - self._cpu_start) * 1000


@dataclass
class FileProfile:
    """Profile for a single session file."""

    path: str
    file_size: int
    event_count: int = 0
    phases: list[PhaseTimer] = field(default_factory=list)
    total_wall_ms: float = 0.0
    total_cpu_ms: float = 0.0
    payload_json_bytes: int = 0
    payload_gzip_bytes: int = 0
    error: str | None = None

    def as_dict(self) -> dict:
        d = {
            "path": self.path,
            "file_size": self.file_size,
            "event_count": self.event_count,
            "total_wall_ms": round(self.total_wall_ms, 3),
            "total_cpu_ms": round(self.total_cpu_ms, 3),
            "payload_json_bytes": self.payload_json_bytes,
            "payload_gzip_bytes": self.payload_gzip_bytes,
            "phases": [p.as_dict() for p in self.phases],
        }
        if self.error:
            d["error"] = self.error
        return d


@dataclass
class LevelReport:
    """Aggregate report for a scale level."""

    level: str
    file_count: int
    total_bytes: int
    total_events: int = 0
    total_wall_ms: float = 0.0
    total_cpu_ms: float = 0.0
    files: list[FileProfile] = field(default_factory=list)
    phase_aggregates: dict[str, dict] = field(default_factory=dict)

    def compute_aggregates(self):
        """Compute per-phase aggregate stats across all files."""
        phase_data: dict[str, list[float]] = {}
        for fp in self.files:
            for phase in fp.phases:
                phase_data.setdefault(phase.name, []).append(phase.wall_ms)

        for name, values in phase_data.items():
            if not values:
                continue
            self.phase_aggregates[name] = {
                "count": len(values),
                "total_ms": round(sum(values), 3),
                "mean_ms": round(statistics.mean(values), 3),
                "median_ms": round(statistics.median(values), 3),
                "p90_ms": round(sorted(values)[int(len(values) * 0.9)], 3) if len(values) >= 10 else None,
                "p99_ms": round(sorted(values)[int(len(values) * 0.99)], 3) if len(values) >= 100 else None,
                "max_ms": round(max(values), 3),
                "pct_of_total": round(sum(values) / self.total_wall_ms * 100, 1) if self.total_wall_ms > 0 else 0,
            }

    def as_dict(self) -> dict:
        return {
            "level": self.level,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "total_events": self.total_events,
            "total_wall_ms": round(self.total_wall_ms, 3),
            "total_cpu_ms": round(self.total_cpu_ms, 3),
            "throughput_mb_per_sec": round(
                (self.total_bytes / 1024 / 1024) / (self.total_wall_ms / 1000), 3
            ) if self.total_wall_ms > 0 else 0,
            "throughput_events_per_sec": round(
                self.total_events / (self.total_wall_ms / 1000), 1
            ) if self.total_wall_ms > 0 else 0,
            "phase_aggregates": self.phase_aggregates,
            "files": [f.as_dict() for f in self.files],
        }


# ---------------------------------------------------------------------------
# Core profiling logic
# ---------------------------------------------------------------------------

async def profile_single_file(
    session_file: Path,
    *,
    api_url: str | None = None,
    api_token: str | None = None,
    dry_run: bool = True,
) -> FileProfile:
    """Profile all phases of shipping a single session file."""

    file_size = session_file.stat().st_size
    fp = FileProfile(path=str(session_file), file_size=file_size)

    try:
        # Phase 1: File read + parse + metadata (single pass)
        t_parse = PhaseTimer(name="parse")
        with Timer(t_parse):
            events, last_offset, metadata = parse_session_file_full(session_file, offset=0)
        t_parse.bytes_in = file_size
        t_parse.items = len(events)
        fp.phases.append(t_parse)
        fp.event_count = len(events)

        if not events:
            fp.total_wall_ms = t_parse.wall_ms
            fp.total_cpu_ms = t_parse.cpu_ms
            return fp

        # Phase 3: Payload build (event conversion + dict construction)
        t_build = PhaseTimer(name="payload_build")
        with Timer(t_build):
            event_dicts = [e.to_event_ingest(str(session_file)) for e in events]
            timestamps = [e.timestamp for e in events if e.timestamp]
            started_at = metadata.started_at or (min(timestamps) if timestamps else datetime.now(timezone.utc))
            ended_at = metadata.ended_at or (max(timestamps) if timestamps else None)
            payload = {
                "id": str(uuid4()),
                "provider": "claude",
                "environment": "production",
                "project": metadata.project,
                "device_id": f"shipper-profiler",
                "cwd": metadata.cwd,
                "git_repo": None,
                "git_branch": metadata.git_branch,
                "started_at": started_at.isoformat(),
                "ended_at": ended_at.isoformat() if ended_at else None,
                "provider_session_id": metadata.session_id,
                "events": event_dicts,
            }
        t_build.items = len(event_dicts)
        fp.phases.append(t_build)

        # Phase 4: JSON serialization
        t_json = PhaseTimer(name="json_serialize")
        with Timer(t_json):
            json_bytes = _dumps(payload)
        t_json.bytes_out = len(json_bytes)
        fp.payload_json_bytes = len(json_bytes)
        fp.phases.append(t_json)

        # Phase 5: Gzip compression
        t_gzip = PhaseTimer(name="gzip_compress")
        with Timer(t_gzip):
            gzip_bytes = gzip.compress(json_bytes)
        t_gzip.bytes_in = len(json_bytes)
        t_gzip.bytes_out = len(gzip_bytes)
        fp.payload_gzip_bytes = len(gzip_bytes)
        fp.phases.append(t_gzip)

        # Phase 6: HTTP POST (only if not dry run)
        if not dry_run and api_url:
            t_http = PhaseTimer(name="http_post")
            headers = {
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
            }
            if api_token:
                headers["X-Agents-Token"] = api_token

            with Timer(t_http):
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.post(
                        f"{api_url}/api/agents/ingest",
                        content=gzip_bytes,
                        headers=headers,
                    )
                    response.raise_for_status()
                    result = response.json()

            t_http.bytes_out = len(gzip_bytes)
            t_http.items = result.get("events_inserted", 0)
            fp.phases.append(t_http)

        # Compute totals
        fp.total_wall_ms = sum(p.wall_ms for p in fp.phases)
        fp.total_cpu_ms = sum(p.cpu_ms for p in fp.phases)

    except Exception as e:
        fp.error = str(e)
        fp.total_wall_ms = sum(p.wall_ms for p in fp.phases)
        fp.total_cpu_ms = sum(p.cpu_ms for p in fp.phases)

    return fp


def discover_session_files(claude_dir: Path | None = None) -> list[tuple[Path, int]]:
    """Discover all non-empty JSONL session files, sorted by size desc.

    Returns: list of (path, file_size) tuples.
    """
    if claude_dir is None:
        claude_dir = Path.home() / ".claude"

    projects_dir = claude_dir / "projects"
    if not projects_dir.exists():
        return []

    files = []
    for p in projects_dir.glob("**/*.jsonl"):
        try:
            size = p.stat().st_size
            if size > 0:
                files.append((p, size))
        except OSError:
            continue

    files.sort(key=lambda x: x[1], reverse=True)
    return files


def select_files(
    all_files: list[tuple[Path, int]],
    level: int,
    seed: int = 42,
) -> list[tuple[Path, int]]:
    """Select files for the given profiling level.

    Level 1: Single largest file
    Level 2: Random 10% of files
    Level 3: All files
    """
    if level == 1:
        return [all_files[0]] if all_files else []
    elif level == 2:
        rng = random.Random(seed)
        count = max(1, len(all_files) // 10)
        # Always include the largest file + random sample
        sample = rng.sample(all_files[1:], min(count - 1, len(all_files) - 1))
        return [all_files[0]] + sample
    else:
        return all_files


async def run_level(
    level: int,
    files: list[tuple[Path, int]],
    *,
    api_url: str | None = None,
    api_token: str | None = None,
    dry_run: bool = True,
    max_files: int | None = None,
) -> LevelReport:
    """Run profiling for a single scale level."""

    level_names = {1: "single", 2: "10pct", 3: "100pct"}
    total_bytes = sum(size for _, size in files)

    if max_files:
        files = files[:max_files]

    report = LevelReport(
        level=level_names.get(level, str(level)),
        file_count=len(files),
        total_bytes=total_bytes,
    )

    print(f"\n{'='*60}")
    print(f"Level {level} ({report.level}): {len(files)} files, {total_bytes / 1024 / 1024:.1f} MB")
    print(f"{'='*60}")

    level_start = time.perf_counter()
    level_cpu_start = time.process_time()

    for i, (path, size) in enumerate(files):
        if (i + 1) % 100 == 0 or i == 0 or i == len(files) - 1:
            elapsed = time.perf_counter() - level_start
            pct = (i + 1) / len(files) * 100
            print(f"  [{pct:5.1f}%] {i+1}/{len(files)} files | {elapsed:.1f}s elapsed | {path.name[:40]}... ({size / 1024:.0f} KB)")

        fp = await profile_single_file(
            path,
            api_url=api_url,
            api_token=api_token,
            dry_run=dry_run,
        )
        report.files.append(fp)
        report.total_events += fp.event_count

    report.total_wall_ms = (time.perf_counter() - level_start) * 1000
    report.total_cpu_ms = (time.process_time() - level_cpu_start) * 1000
    report.compute_aggregates()

    # Print summary
    print(f"\n--- Level {level} Summary ---")
    print(f"  Files: {report.file_count}")
    print(f"  Events: {report.total_events:,}")
    print(f"  Total wall: {report.total_wall_ms / 1000:.2f}s")
    print(f"  Total CPU: {report.total_cpu_ms / 1000:.2f}s")
    print(f"  CPU utilization: {report.total_cpu_ms / report.total_wall_ms * 100:.1f}%" if report.total_wall_ms > 0 else "")
    print(f"  Throughput: {report.phase_aggregates.get('parse', {}).get('total_ms', 0) / 1000:.2f}s parsing, "
          f"{report.phase_aggregates.get('json_serialize', {}).get('total_ms', 0) / 1000:.2f}s JSON, "
          f"{report.phase_aggregates.get('gzip_compress', {}).get('total_ms', 0) / 1000:.2f}s gzip")
    if not dry_run:
        http_total = report.phase_aggregates.get("http_post", {}).get("total_ms", 0)
        print(f"  HTTP: {http_total / 1000:.2f}s total")

    print(f"\n  Phase breakdown:")
    for name, agg in report.phase_aggregates.items():
        print(f"    {name:20s}: {agg['total_ms'] / 1000:8.2f}s total | "
              f"mean={agg['mean_ms']:8.2f}ms | "
              f"median={agg['median_ms']:8.2f}ms | "
              f"max={agg['max_ms']:8.2f}ms | "
              f"{agg['pct_of_total']:5.1f}% of wall")

    return report


# ---------------------------------------------------------------------------
# Memory profiling helpers
# ---------------------------------------------------------------------------

def get_rss_mb() -> float:
    """Get current RSS in MB (macOS/Linux)."""
    try:
        import resource
        # maxrss is in bytes on macOS, KB on Linux
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform == "darwin":
            return rss / 1024 / 1024  # bytes -> MB
        return rss / 1024  # KB -> MB
    except ImportError:
        return 0.0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="Profile shipper phases at multiple scale levels")
    parser.add_argument("--level", type=int, choices=[1, 2, 3], default=None,
                        help="Run specific level only (1=single, 2=10%%, 3=100%%)")
    parser.add_argument("--api-url", type=str, default=None,
                        help="API URL for real shipping (e.g. https://david010.longhouse.ai)")
    parser.add_argument("--token", type=str, default=None,
                        help="API token (or set AGENTS_API_TOKEN)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip HTTP POST (profile parse+build+compress only)")
    parser.add_argument("--max-files", type=int, default=None,
                        help="Cap file count per level (for quick iteration)")
    parser.add_argument("--output", type=str, default=None,
                        help="Output JSON report path (default: stdout summary only)")
    parser.add_argument("--claude-dir", type=str, default=None,
                        help="Override ~/.claude directory")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for 10%% sampling")
    parser.add_argument("--no-file-details", action="store_true",
                        help="Omit per-file details in JSON output (smaller report)")
    parser.add_argument("--py-spy", action="store_true",
                        help="Launch py-spy in background (requires sudo)")
    parser.add_argument("--memray", action="store_true",
                        help="Run under memray (generates .bin trace)")
    args = parser.parse_args()

    api_token = args.token or os.getenv("AGENTS_API_TOKEN")
    dry_run = args.dry_run or (args.api_url is None)
    claude_dir = Path(args.claude_dir) if args.claude_dir else None

    if not dry_run and not api_token:
        print("ERROR: --token or AGENTS_API_TOKEN required for non-dry-run mode")
        sys.exit(1)

    # Discover files
    print("Discovering session files...")
    all_files = discover_session_files(claude_dir)
    total_size = sum(s for _, s in all_files)
    print(f"Found {len(all_files)} non-empty session files ({total_size / 1024 / 1024 / 1024:.2f} GB)")

    # Size distribution
    sizes = [s for _, s in all_files]
    if sizes:
        print(f"  median: {statistics.median(sizes) / 1024:.0f} KB")
        print(f"  p90: {sorted(sizes)[int(len(sizes) * 0.9)] / 1024:.0f} KB")
        print(f"  p99: {sorted(sizes)[int(len(sizes) * 0.99)] / 1024 / 1024:.1f} MB")
        print(f"  max: {max(sizes) / 1024 / 1024:.0f} MB")

    print(f"\nMode: {'DRY RUN (no HTTP)' if dry_run else f'LIVE → {args.api_url}'}")
    print(f"RSS at start: {get_rss_mb():.1f} MB")

    # Run levels
    levels = [args.level] if args.level else [1, 2, 3]
    reports = []

    for level in levels:
        files = select_files(all_files, level, seed=args.seed)
        report = await run_level(
            level, files,
            api_url=args.api_url,
            api_token=api_token,
            dry_run=dry_run,
            max_files=args.max_files,
        )
        reports.append(report)
        print(f"\nRSS after level {level}: {get_rss_mb():.1f} MB")

    # Final summary
    print(f"\n{'='*60}")
    print("FINAL SUMMARY")
    print(f"{'='*60}")

    for r in reports:
        print(f"\n  Level {r.level}:")
        print(f"    {r.file_count} files, {r.total_bytes / 1024 / 1024:.1f} MB input")
        print(f"    {r.total_events:,} events")
        print(f"    {r.total_wall_ms / 1000:.2f}s wall, {r.total_cpu_ms / 1000:.2f}s CPU")
        if r.total_wall_ms > 0:
            print(f"    {r.total_bytes / 1024 / 1024 / (r.total_wall_ms / 1000):.1f} MB/s throughput")
            print(f"    {r.total_events / (r.total_wall_ms / 1000):.0f} events/s")

    # Rust rewrite indicators
    if len(reports) >= 2:
        r1 = reports[0]
        r2 = reports[-1]
        if r1.total_wall_ms > 0 and r2.total_wall_ms > 0:
            print(f"\n--- Rust Rewrite Indicators ---")
            cpu_pct = r2.total_cpu_ms / r2.total_wall_ms * 100
            print(f"  CPU utilization (largest run): {cpu_pct:.1f}%")
            if cpu_pct > 60:
                print(f"    → CPU-bound: Rust rewrite would help significantly")
            elif cpu_pct > 30:
                print(f"    → Mixed: Rust helps for CPU phases, redesign needed for I/O")
            else:
                print(f"    → I/O-bound: Rust alone won't help much, need architectural changes")

            # Identify dominant phase
            for name, agg in r2.phase_aggregates.items():
                if agg["pct_of_total"] > 30:
                    print(f"  Dominant phase: {name} ({agg['pct_of_total']:.1f}% of wall)")

    # Write JSON report
    if args.output:
        output_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": "dry_run" if dry_run else "live",
            "api_url": args.api_url,
            "total_files_available": len(all_files),
            "total_bytes_available": total_size,
            "rss_mb": get_rss_mb(),
            "levels": [],
        }
        for r in reports:
            rd = r.as_dict()
            if args.no_file_details:
                rd.pop("files", None)
            output_data["levels"].append(rd)

        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(output_data, f, indent=2)
        print(f"\nJSON report written to: {output_path}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
