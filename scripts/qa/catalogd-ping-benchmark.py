#!/usr/bin/env python3
"""Measure persistent catalogd Unix-RPC ping latency against the phase gate."""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server"))

from zerg.catalogd.client import CatalogClient  # noqa: E402


async def run(socket_path: Path, *, iterations: int, warmup: int) -> dict[str, object]:
    client = CatalogClient(socket_path, default_timeout_seconds=1.0)
    samples_ms: list[float] = []
    try:
        for _ in range(warmup):
            await client.call("ping.v2")
        for _ in range(iterations):
            started = time.perf_counter_ns()
            await client.call("ping.v2")
            samples_ms.append((time.perf_counter_ns() - started) / 1_000_000)
    finally:
        await client.close()
    ordered = sorted(samples_ms)
    p95_index = max(0, min(len(ordered) - 1, int(len(ordered) * 0.95) - 1))
    return {
        "iterations": iterations,
        "p50_ms": round(statistics.median(ordered), 4),
        "p95_ms": round(ordered[p95_index], 4),
        "max_ms": round(ordered[-1], 4),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=10_000)
    parser.add_argument("--warmup", type=int, default=100)
    parser.add_argument("--max-p95-ms", type=float, default=1.0)
    args = parser.parse_args()
    if args.iterations < 1 or args.warmup < 0:
        raise SystemExit("iterations must be positive and warmup non-negative")
    result = asyncio.run(run(args.socket, iterations=args.iterations, warmup=args.warmup))
    result["gate_max_p95_ms"] = args.max_p95_ms
    result["passed"] = result["p95_ms"] <= args.max_p95_ms
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
