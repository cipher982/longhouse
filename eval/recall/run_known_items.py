#!/usr/bin/env python3
"""Run exact known-item recall with cold/warm latency and error-as-miss scoring."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path


def percentile(values: list[float], fraction: float) -> float:
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * fraction))] if values else 0.0


def load(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip() and not line.startswith("//")]


def query(url: str, token: str, text: str, mode: str, limit: int, days: int) -> list[str]:
    params = urllib.parse.urlencode({"query": text, "max_results": limit, "since_days": days, "mode": mode})
    request = urllib.request.Request(
        f"{url}/api/agents/recall?{params}",
        headers={"X-Agents-Token": token, "User-Agent": "longhouse-known-items/1.0"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.load(response)
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError("malformed recall response")
    return [str(item.get("session_id") or "") for item in payload["results"]]


def score(items: list[dict[str, object]], url: str, token: str, mode: str, limit: int, days: int) -> dict[str, object]:
    by_bucket: dict[str, list[dict[str, object]]] = defaultdict(list)
    latencies: list[float] = []
    errors = 0
    for item in items:
        started = time.monotonic()
        returned: list[str] = []
        error = False
        try:
            returned = query(url, token, str(item["query"]), mode, limit, days)
        except Exception:  # Requests that fail are retrieval misses by contract.
            error = True
            errors += 1
        latencies.append(time.monotonic() - started)
        gold = [str(value) for value in item["gold_sessions"]]
        hidden = bool(item.get("must_not_return"))
        matched = any(candidate.startswith(session) for candidate in returned for session in gold)
        hit = (not matched if hidden else matched) and not error
        bucket = f"{item['provider']}/{item['category']}"
        by_bucket[bucket].append({"hit": hit, "hidden": hidden})
    summary = {
        bucket: {"hits": sum(row["hit"] for row in rows), "total": len(rows), "recall_at_k": sum(row["hit"] for row in rows) / len(rows)}
        for bucket, rows in sorted(by_bucket.items())
    }
    return {"errors": errors, "by_provider_category": summary, "latency_seconds": {"p50": statistics.median(latencies), "p95": percentile(latencies, .95), "p99": percentile(latencies, .99)}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--strategy", choices=("lexical", "dense"), required=True)
    parser.add_argument("--url", default=os.environ.get("LONGHOUSE_EVAL_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--token", default=os.environ.get("LONGHOUSE_EVAL_TOKEN", "unused-with-auth-disabled"))
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--days", type=int, default=365)
    args = parser.parse_args()
    mode = "semantic" if args.strategy == "dense" else args.strategy
    items = load(args.input)
    report = {"strategy": args.strategy, "query_count": len(items), "cold": score(items, args.url.rstrip("/"), args.token, mode, args.limit, args.days), "warm": score(items, args.url.rstrip("/"), args.token, mode, args.limit, args.days)}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
