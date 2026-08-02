#!/usr/bin/env python3
"""Score a retrieval strategy against labelled recall queries.

The gate is `false_negative_rate`: answer-present queries where retrieval
returned no gold session. That is the postmortem failure expressed as a number.
Recall@k is diagnostic — it bounds how much a reranker could recover, since
reranking cannot retrieve what the first stage never returned.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

QUERIES_PATH = Path(__file__).with_name("queries.jsonl")
DEFAULT_URL = "https://david010.longhouse.ai"
TOKEN_PATH = Path.home() / ".longhouse" / "machine" / "device-token"


@dataclass
class Query:
    id: str
    category: str
    query: str
    gold_sessions: list[str]
    note: str = ""

    @property
    def expects_evidence(self) -> bool:
        return self.category != "absent"


@dataclass
class Result:
    query: Query
    returned: list[str]
    latency_s: float
    error: str | None = None

    def gold_rank(self) -> int | None:
        """1-based rank of the first gold session, or None if absent.

        Labels record the short session prefix people actually read and quote,
        so a gold matches any returned id that starts with it.
        """

        for position, session_id in enumerate(self.returned, start=1):
            if any(session_id.startswith(gold) for gold in self.query.gold_sessions):
                return position
        return None


@dataclass
class Report:
    strategy: str
    results: list[Result] = field(default_factory=list)

    def _answerable(self) -> list[Result]:
        """Answer-present queries, errors included.

        Errors used to be filtered out here, which let a completely broken
        retrieval strategy report a *better* false-negative rate than a working
        one: every failure left the denominator instead of counting against it.
        A query that errored returned no evidence, which is the same outcome for
        the agent as a miss, so it is scored as one.
        """

        return [r for r in self.results if r.query.expects_evidence]

    def recall_at(self, k: int) -> float:
        answerable = self._answerable()
        if not answerable:
            return 0.0
        hits = sum(1 for r in answerable if (rank := r.gold_rank()) is not None and rank <= k)
        return hits / len(answerable)

    def false_negative_rate(self) -> float:
        """Answer-present queries that returned nothing useful. The gate."""

        answerable = self._answerable()
        if not answerable:
            return 0.0
        misses = sum(1 for r in answerable if r.gold_rank() is None)
        return misses / len(answerable)

    def correct_abstention_rate(self) -> float:
        """Absent queries where retrieval returned nothing, rather than something plausible."""

        absent = [r for r in self.results if not r.query.expects_evidence and not r.error]
        if not absent:
            return 0.0
        return sum(1 for r in absent if not r.returned) / len(absent)

    def by_category(self) -> dict[str, tuple[int, int]]:
        buckets: dict[str, list[Result]] = {}
        for result in self.results:
            buckets.setdefault(result.query.category, []).append(result)
        summary = {}
        for category, results in sorted(buckets.items()):
            if category == "absent":
                good = sum(1 for r in results if not r.returned)
            else:
                good = sum(1 for r in results if (rank := r.gold_rank()) is not None and rank <= 5)
            summary[category] = (good, len(results))
        return summary

    def latencies(self) -> tuple[float, float]:
        values = sorted(r.latency_s for r in self.results if not r.error)
        if not values:
            return (0.0, 0.0)
        p95_index = min(len(values) - 1, int(len(values) * 0.95))
        return (statistics.median(values), values[p95_index])


def load_queries() -> tuple[list[Query], set[str]]:
    queries: list[Query] = []
    excluded: set[str] = set()
    for line in QUERIES_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        record = json.loads(line)
        if "excluded_sessions" in record:
            excluded = set(record["excluded_sessions"])
            continue
        queries.append(
            Query(
                id=record["id"],
                category=record["category"],
                query=record["query"],
                gold_sessions=record.get("gold_sessions", []),
                note=record.get("note", ""),
            )
        )
    return queries, excluded


def search_recall(
    query: str, *, base_url: str, token: str, limit: int, days: int, mode: str
) -> list[str]:
    params = urllib.parse.urlencode(
        {"query": query, "max_results": limit, "since_days": days, "context_turns": 0, "mode": mode}
    )
    request = urllib.request.Request(
        f"{base_url}/api/agents/recall?{params}",
        # An explicit User-Agent is required, not cosmetic: the default
        # "Python-urllib/3.x" is rejected with 403 before reaching the API.
        headers={"X-Agents-Token": token, "User-Agent": "longhouse-recall-eval/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    return [str(match.get("session_id") or "") for match in (payload.get("matches") or [])]


def _search_mode(mode: str):
    def search(query: str, *, base_url: str, token: str, limit: int, days: int) -> list[str]:
        return search_recall(query, base_url=base_url, token=token, limit=limit, days=days, mode=mode)

    return search


# "fts" was a misnomer: it sent no mode, so it hit the fused default. Keep the
# lanes explicit so a dead one is visible instead of being averaged away.
STRATEGIES = {
    "lexical": _search_mode("lexical"),
    "semantic": _search_mode("semantic"),
    "auto": _search_mode("auto"),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", default="auto", choices=sorted(STRATEGIES))
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--verbose", action="store_true", help="Show every miss.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable output.")
    args = parser.parse_args()

    base_url = os.environ.get("LONGHOUSE_EVAL_URL", DEFAULT_URL).rstrip("/")
    token = os.environ.get("LONGHOUSE_EVAL_TOKEN") or (
        TOKEN_PATH.read_text().strip() if TOKEN_PATH.exists() else ""
    )
    if not token:
        print(f"No device token. Set LONGHOUSE_EVAL_TOKEN or create {TOKEN_PATH}.")
        return 2

    queries, excluded = load_queries()
    if not queries:
        print("No queries labelled yet.")
        return 2

    search = STRATEGIES[args.strategy]
    report = Report(strategy=args.strategy)
    for query in queries:
        started = time.monotonic()
        try:
            returned = search(
                query.query, base_url=base_url, token=token, limit=args.limit, days=args.days
            )
            # Sessions that produced this work discuss retrieval itself and would
            # match every query about retrieval.
            returned = [s for s in returned if s not in excluded]
            report.results.append(Result(query, returned, time.monotonic() - started))
        except Exception as exc:  # noqa: BLE001 - report, never abort the sweep
            report.results.append(Result(query, [], time.monotonic() - started, error=str(exc)))

    p50, p95 = report.latencies()
    if args.json:
        print(
            json.dumps(
                {
                    "strategy": report.strategy,
                    "recall_at_5": round(report.recall_at(5), 3),
                    "recall_at_10": round(report.recall_at(10), 3),
                    "false_negative_rate": round(report.false_negative_rate(), 3),
                    "correct_abstention_rate": round(report.correct_abstention_rate(), 3),
                    "p50_s": round(p50, 3),
                    "p95_s": round(p95, 3),
                    "by_category": report.by_category(),
                },
                indent=2,
            )
        )
        return 0

    print(f"\nstrategy: {report.strategy}   queries: {len(report.results)}\n")
    print(f"  recall@5                 {report.recall_at(5):.1%}")
    print(f"  recall@10                {report.recall_at(10):.1%}")
    print(f"  false 'nothing found'    {report.false_negative_rate():.1%}   <- gate")
    print(f"  correct abstention       {report.correct_abstention_rate():.1%}")
    print(f"  latency p50/p95          {p50:.2f}s / {p95:.2f}s\n")
    for category, (good, total) in report.by_category().items():
        print(f"    {category:14s} {good}/{total}")

    errors = [r for r in report.results if r.error]
    if errors:
        print(f"\n  {len(errors)} query(s) errored (counted as misses):")
        for result in errors[:5]:
            print(f"    {result.query.id}: {result.error}")

    if errors and len(errors) / max(1, len(report.results)) > 0.05:
        print(
            f"\n  UNRELIABLE: {len(errors)}/{len(report.results)} queries errored. "
            "These score as misses, so the numbers above are a floor, not a measurement. "
            "Fix the errors and re-run before treating any of this as a gate."
        )

    if args.verbose:
        misses = [r for r in report.results if r.query.expects_evidence and r.gold_rank() is None]
        if misses:
            print(f"\n  misses ({len(misses)}):")
            for result in misses:
                print(f"    [{result.query.category}] {result.query.id}: {result.query.query}")
                print(f"      wanted {result.query.gold_sessions} got {result.returned[:5] or 'nothing'}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
