#!/usr/bin/env python3
"""Score a retrieval strategy against labelled recall queries.

The gate is `false_negative_rate`: answer-present queries where retrieval
returned no gold session. That is the postmortem failure expressed as a number.
Recall@k is diagnostic — it bounds how much a reranker could recover, since
reranking cannot retrieve what the first stage never returned.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from pathlib import Path

QUERIES_PATH = Path(__file__).with_name("queries.jsonl")
DEFAULT_URL = "https://david010.longhouse.ai"
TOKEN_PATH = Path.home() / ".longhouse" / "machine" / "device-token"
DEFAULT_MAX_FALSE_NEGATIVE_RATE = 0.671
DEFAULT_MIN_RECALL_AT_5 = 0.329
# Full-corpus Qwen3-8B @256d baseline measured at k=25. The replacement must
# improve aggregate recall without buying that gain by regressing any one of
# the four answer-present query classes.
DEFAULT_MIN_CATEGORY_HITS_AT_25 = {
    "exact": 11,
    "paraphrase": 16,
    "causal": 7,
    "supersession": 6,
}


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
    lanes: tuple[str, ...] = ()
    embedding_model: str | None = None
    embedding_dims: int | None = None
    embedding_revision: str | None = None
    coverage: dict[str, object] | None = None

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

    def by_category(self, k: int = 5) -> dict[str, tuple[int, int]]:
        buckets: dict[str, list[Result]] = {}
        for result in self.results:
            buckets.setdefault(result.query.category, []).append(result)
        summary = {}
        for category, results in sorted(buckets.items()):
            if category == "absent":
                good = sum(1 for r in results if not r.error and not r.returned)
            else:
                good = sum(1 for r in results if (rank := r.gold_rank()) is not None and rank <= k)
            summary[category] = (good, len(results))
        return summary

    def latencies(self) -> tuple[float, float]:
        values = sorted(r.latency_s for r in self.results if not r.error)
        if not values:
            return (0.0, 0.0)
        p95_index = min(len(values) - 1, int(len(values) * 0.95))
        return (statistics.median(values), values[p95_index])

    def errors(self) -> list[Result]:
        return [result for result in self.results if result.error]

    def gate_failures(
        self,
        *,
        max_false_negative_rate: float,
        min_recall_at_5: float,
        min_category_hits_at_25: dict[str, int] | None = None,
    ) -> list[str]:
        failures = []
        if self.errors():
            failures.append(f"{len(self.errors())} query(s) errored")
        spaces = self._embedding_spaces()
        if self.strategy != "lexical" and len(spaces) != 1:
            failures.append(f"evaluation observed {len(spaces)} embedding spaces instead of one")
        projectors = {str(coverage["projector"]) for coverage in self._coverage_snapshots()}
        if self.strategy != "lexical" and len(projectors) != 1:
            failures.append(f"evaluation observed {len(projectors)} corpus projectors instead of one")
        if self.false_negative_rate() > max_false_negative_rate:
            failures.append(f"false_negative_rate {self.false_negative_rate():.3f} exceeds {max_false_negative_rate:.3f}")
        if self.recall_at(5) < min_recall_at_5:
            failures.append(f"recall_at_5 {self.recall_at(5):.3f} is below {min_recall_at_5:.3f}")
        category_results = self.by_category(25)
        for category, minimum in (min_category_hits_at_25 or {}).items():
            hits, total = category_results.get(category, (0, 0))
            if hits < minimum:
                failures.append(f"{category}_hits_at_25 {hits}/{total} is below {minimum}")
        return failures

    def _embedding_spaces(self) -> set[tuple[str | None, int | None, str | None]]:
        return {
            (result.embedding_model, result.embedding_dims, result.embedding_revision)
            for result in self.results
            if result.embedding_model is not None
        }

    def embedding_metadata(self) -> dict[str, object] | None:
        values = self._embedding_spaces()
        if not values:
            return None
        observed = [{"model": model, "dims": dims, "revision": revision} for model, dims, revision in sorted(values, key=str)]
        if len(observed) == 1:
            return {"consistent": True, **observed[0]}
        return {"consistent": False, "observed": observed}

    def _coverage_snapshots(self) -> list[dict[str, object]]:
        return [result.coverage for result in self.results if result.coverage is not None]

    def corpus_coverage_metadata(self) -> dict[str, object] | None:
        snapshots = self._coverage_snapshots()
        if not snapshots:
            return None

        def numeric_range(field_name: str) -> dict[str, int]:
            values = [int(snapshot[field_name]) for snapshot in snapshots]
            return {"min": min(values), "max": max(values)}

        projectors = sorted({str(snapshot["projector"]) for snapshot in snapshots})
        unique_snapshots = {
            (
                str(snapshot["catalog_commit_seq"]),
                int(snapshot["expected_sessions"]),
                int(snapshot["expected_episodes"]),
            )
            for snapshot in snapshots
        }
        observed_at = sorted(str(snapshot["catalog_observed_at"]) for snapshot in snapshots)
        return {
            "status": "complete",
            "consistent_projector": len(projectors) == 1,
            "projectors": projectors,
            "response_count": len(snapshots),
            "snapshot_count": len(unique_snapshots),
            "catalog_commit_seq": numeric_range("catalog_commit_seq"),
            "expected_sessions": numeric_range("expected_sessions"),
            "expected_episodes": numeric_range("expected_episodes"),
            "observed_at": {"first": observed_at[0], "last": observed_at[-1]},
            "resident_defects": {
                "invalid_vectors": 0,
                "unnormalized_vectors": 0,
                "unlocatable_episodes": 0,
                "episode_count_mismatches": 0,
                "missing_sessions": 0,
            },
        }


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


def search_recall(query: str, *, base_url: str, token: str, limit: int, days: int, mode: str) -> dict:
    params = urllib.parse.urlencode({"query": query, "max_results": limit, "since_days": days, "context_turns": 0, "mode": mode})
    request = urllib.request.Request(
        f"{base_url}/api/agents/recall?{params}",
        # An explicit User-Agent is required, not cosmetic: the default
        # "Python-urllib/3.x" is rejected with 403 before reaching the API.
        headers={"X-Agents-Token": token, "User-Agent": "longhouse-recall-eval/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("recall returned a non-object response")
    expected_lanes = {"lexical": ["lexical"], "semantic": ["dense"], "auto": ["lexical", "dense"]}[mode]
    if payload.get("lanes") != expected_lanes:
        raise ValueError(f"recall lane attribution mismatch: expected {expected_lanes}, got {payload.get('lanes')}")
    matches = payload.get("matches")
    if not isinstance(matches, list) or any(not isinstance(match, dict) for match in matches):
        raise ValueError("recall returned malformed matches")
    coverage = payload.get("coverage")
    if mode == "lexical":
        if coverage is not None:
            raise ValueError("lexical recall claimed dense corpus coverage")
    else:
        if not isinstance(coverage, dict):
            raise ValueError("dense recall omitted corpus coverage")
        required = {
            "ready",
            "projector",
            "catalog_lag_count",
            "catalog_indexed_through",
            "catalog_commit_seq",
            "catalog_observed_at",
            "expected_sessions",
            "published_sessions",
            "expected_episodes",
            "current_episodes",
            "invalid_vectors",
            "unnormalized_vectors",
            "unlocatable_episodes",
            "episode_count_mismatches",
            "missing_session_ids",
        }
        if set(coverage) != required:
            raise ValueError("dense recall returned malformed corpus coverage")
        if (
            coverage["ready"] is not True
            or coverage["catalog_lag_count"] != 0
            or coverage["catalog_indexed_through"] != coverage["catalog_commit_seq"]
            or coverage["expected_sessions"] != coverage["published_sessions"]
            or coverage["expected_episodes"] != coverage["current_episodes"]
            or any(
                coverage[field_name] != 0
                for field_name in (
                    "invalid_vectors",
                    "unnormalized_vectors",
                    "unlocatable_episodes",
                    "episode_count_mismatches",
                )
            )
            or coverage["missing_session_ids"] != []
        ):
            raise ValueError("dense recall corpus coverage is incomplete")
    return payload


def _search_mode(mode: str):
    def search(query: str, *, base_url: str, token: str, limit: int, days: int) -> dict:
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
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--verbose", action="store_true", help="Show every miss.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable output.")
    parser.add_argument("--max-false-negative-rate", type=float, default=DEFAULT_MAX_FALSE_NEGATIVE_RATE)
    parser.add_argument("--min-recall-at-5", type=float, default=DEFAULT_MIN_RECALL_AT_5)
    args = parser.parse_args()

    base_url = os.environ.get("LONGHOUSE_EVAL_URL", DEFAULT_URL).rstrip("/")
    token = os.environ.get("LONGHOUSE_EVAL_TOKEN") or (TOKEN_PATH.read_text().strip() if TOKEN_PATH.exists() else "")
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
            payload = search(query.query, base_url=base_url, token=token, limit=args.limit, days=args.days)
            returned = [str(match.get("session_id") or "") for match in payload["matches"]]
            # Sessions that produced this work discuss retrieval itself and would
            # match every query about retrieval.
            returned = [s for s in returned if s not in excluded]
            report.results.append(
                Result(
                    query,
                    returned,
                    time.monotonic() - started,
                    lanes=tuple(payload["lanes"]),
                    embedding_model=payload.get("embedding_model"),
                    embedding_dims=payload.get("embedding_dims"),
                    embedding_revision=payload.get("embedding_revision"),
                    coverage=payload.get("coverage"),
                )
            )
        except Exception as exc:  # noqa: BLE001 - report, never abort the sweep
            report.results.append(Result(query, [], time.monotonic() - started, error=str(exc)))

    p50, p95 = report.latencies()
    gate_failures = report.gate_failures(
        max_false_negative_rate=args.max_false_negative_rate,
        min_recall_at_5=args.min_recall_at_5,
        min_category_hits_at_25=DEFAULT_MIN_CATEGORY_HITS_AT_25,
    )
    try:
        git_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[2],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        git_sha = "unknown"
    query_set_sha256 = hashlib.sha256(QUERIES_PATH.read_bytes()).hexdigest()
    metadata = {
        "evaluated_at": datetime.now(UTC).isoformat(),
        "base_url": base_url,
        "git_sha": git_sha,
        "query_set_sha256": query_set_sha256,
        "query_count": len(report.results),
        "limit": args.limit,
        "days": args.days,
        "expected_lanes": {"lexical": ["lexical"], "semantic": ["dense"], "auto": ["lexical", "dense"]}[args.strategy],
        "embedding_space": report.embedding_metadata(),
        "corpus_coverage": report.corpus_coverage_metadata(),
        "error_count": len(report.errors()),
        "thresholds": {
            "max_false_negative_rate": args.max_false_negative_rate,
            "min_recall_at_5": args.min_recall_at_5,
            "min_category_hits_at_25": DEFAULT_MIN_CATEGORY_HITS_AT_25,
        },
    }
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
                    "by_category_at_5": report.by_category(5),
                    "by_category_at_25": report.by_category(25),
                    "errors": len(report.errors()),
                    "error_details": [
                        {
                            "query_id": result.query.id,
                            "category": result.query.category,
                            "error": result.error,
                        }
                        for result in report.errors()
                    ],
                    "gate": "pass" if not gate_failures else "fail",
                    "gate_failures": gate_failures,
                    "metadata": metadata,
                },
                indent=2,
            )
        )
        return 0 if not gate_failures else 1

    print(f"\nstrategy: {report.strategy}   queries: {len(report.results)}\n")
    print(f"  recall@5                 {report.recall_at(5):.1%}")
    print(f"  recall@10                {report.recall_at(10):.1%}")
    print(f"  false 'nothing found'    {report.false_negative_rate():.1%}   <- gate")
    print(f"  correct abstention       {report.correct_abstention_rate():.1%}")
    print(f"  latency p50/p95          {p50:.2f}s / {p95:.2f}s\n")
    categories_at_5 = report.by_category(5)
    categories_at_25 = report.by_category(25)
    for category, (good, total) in categories_at_5.items():
        at_25 = categories_at_25[category][0]
        print(f"    {category:14s} @{5} {good}/{total}  @{25} {at_25}/{total}")

    errors = report.errors()
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
    if gate_failures:
        print("  RELEASE GATE FAILED:")
        for failure in gate_failures:
            print(f"    - {failure}")
        return 1
    print("  RELEASE GATE PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
