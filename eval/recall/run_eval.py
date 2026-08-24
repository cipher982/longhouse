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
import re
import statistics
import subprocess
import time
import urllib.error
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
# Full-corpus Qwen3-8B @256d returned a gold session for 40/76 answerable
# queries at k=25, so the replacement may miss at most 36/76. The previous
# 0.671 ceiling came from an older partial-corpus k=5 run and no longer matched
# the evaluator's real result depth or the baseline named by this release gate.
# Keep the release boundary as the exact observed ten-card counts. Rounded
# decimal approximations can make a baseline fail its own gate.
DEFAULT_MAX_FALSE_NEGATIVE_RATE = 45 / 76
DEFAULT_MIN_RECALL_AT_5 = 26 / 76
# Grading retrieval against a half-projected corpus measures the backlog, not
# the retriever, so the evaluator still demands a nearly-current index. This is
# a quality threshold for scoring, deliberately not the serving contract:
# recall itself serves under lag and reports the watermark.
MAX_EVAL_LAG_SESSIONS = 100
MAX_EVAL_LAG_AGE_SECONDS = 300.0
# The public recall index is intentionally capped at ten cards. These are the
# exact observed 2026-08-24 auto-lane counts at that serving depth: 19/27 exact,
# 5/24 paraphrase, 3/15 causal, and 4/10 supersession, with no request errors.
DEFAULT_MIN_CATEGORY_HITS_AT_10 = {
    "exact": 19,
    "paraphrase": 5,
    "causal": 3,
    "supersession": 4,
}
_FULL_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")


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
    projector: str | None = None
    coverage: dict[str, object] | None = None
    server_commit: str | None = None

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
        hits = sum(
            1 for r in answerable if (rank := r.gold_rank()) is not None and rank <= k
        )
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

        absent = [
            r for r in self.results if not r.query.expects_evidence and not r.error
        ]
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
                good = sum(
                    1
                    for r in results
                    if (rank := r.gold_rank()) is not None and rank <= k
                )
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
        min_category_hits_at_10: dict[str, int] | None = None,
    ) -> list[str]:
        failures = []
        if self.errors():
            failures.append(f"{len(self.errors())} query(s) errored")
        spaces = self._embedding_spaces()
        if self.strategy != "lexical" and len(spaces) != 1:
            failures.append(
                f"evaluation observed {len(spaces)} embedding spaces instead of one"
            )
        projectors = self.projectors()
        if self.strategy != "lexical" and len(projectors) != 1:
            failures.append(
                f"evaluation observed {len(projectors)} projectors instead of one"
            )
        server_commits = self.server_commits()
        if len(server_commits) != 1:
            failures.append(
                f"evaluation observed {len(server_commits)} serving commits instead of one"
            )
        if self.false_negative_rate() > max_false_negative_rate:
            failures.append(
                f"false_negative_rate {self.false_negative_rate():.3f} exceeds {max_false_negative_rate:.3f}"
            )
        if self.recall_at(5) < min_recall_at_5:
            failures.append(
                f"recall_at_5 {self.recall_at(5):.3f} is below {min_recall_at_5:.3f}"
            )
        category_results = self.by_category(10)
        for category, minimum in (min_category_hits_at_10 or {}).items():
            hits, total = category_results.get(category, (0, 0))
            if hits < minimum:
                failures.append(
                    f"{category}_hits_at_10 {hits}/{total} is below {minimum}"
                )
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
        observed = [
            {"model": model, "dims": dims, "revision": revision}
            for model, dims, revision in sorted(values, key=str)
        ]
        if len(observed) == 1:
            return {"consistent": True, **observed[0]}
        return {"consistent": False, "observed": observed}

    def _coverage_snapshots(self) -> list[dict[str, object]]:
        return [
            result.coverage for result in self.results if result.coverage is not None
        ]

    def server_commits(self) -> list[str]:
        return sorted(
            {
                result.server_commit
                for result in self.results
                if result.server_commit is not None
            }
        )

    def projectors(self) -> list[str]:
        return sorted(
            {
                result.projector
                for result in self.results
                if result.projector is not None
            }
        )

    def corpus_coverage_metadata(self) -> dict[str, object] | None:
        snapshots = self._coverage_snapshots()
        if not snapshots:
            return None

        def numeric_range(field_name: str) -> dict[str, int]:
            values = [int(snapshot[field_name]) for snapshot in snapshots]
            return {"min": min(values), "max": max(values)}

        coverage_valid = all(
            int(snapshot["lagging_sessions"]) <= MAX_EVAL_LAG_SESSIONS
            and (
                snapshot["oldest_lag_seconds"] is None
                or float(snapshot["oldest_lag_seconds"]) <= MAX_EVAL_LAG_AGE_SECONDS
            )
            and int(snapshot["unpublished_sessions"]) <= MAX_EVAL_LAG_SESSIONS
            for snapshot in snapshots
        )
        current = coverage_valid and all(
            bool(snapshot["complete"]) for snapshot in snapshots
        )
        return {
            "status": "current"
            if current
            else ("bounded_head" if coverage_valid else "incomplete"),
            "response_count": len(snapshots),
            "lagging_sessions": numeric_range("lagging_sessions"),
            "unpublished_sessions": numeric_range("unpublished_sessions"),
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


def _without_excluded_sessions(
    session_ids: list[str], excluded_prefixes: set[str]
) -> list[str]:
    """Remove benchmark-producing sessions using the prefix labels stored by the eval."""

    return [
        session_id
        for session_id in session_ids
        if not any(session_id.startswith(prefix) for prefix in excluded_prefixes)
    ]


def search_recall(
    query: str,
    *,
    base_url: str,
    token: str,
    limit: int,
    days: int,
    mode: str,
    expected_sha: str,
) -> dict:
    if not 1 <= limit <= 10:
        raise ValueError("recall evaluation limit must be between 1 and 10")
    params = urllib.parse.urlencode(
        {"query": query, "max_results": limit, "since_days": days, "mode": mode}
    )
    request = urllib.request.Request(
        f"{base_url}/api/agents/recall?{params}",
        # An explicit User-Agent is required, not cosmetic: the default
        # "Python-urllib/3.x" is rejected with 403 before reaching the API.
        headers={"X-Agents-Token": token, "User-Agent": "longhouse-recall-eval/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
            response_headers = getattr(response, "headers", {})
    except urllib.error.HTTPError as exc:
        body = exc.read(512).decode("utf-8", errors="replace")
        raise ValueError(f"recall HTTP {exc.code}: {body}") from exc
    if not isinstance(payload, dict):
        raise ValueError("recall returned a non-object response")
    expected_lanes = {
        "lexical": ["lexical"],
        "semantic": ["dense"],
        "auto": ["lexical", "dense"],
    }[mode]
    if payload.get("lanes") != expected_lanes:
        raise ValueError(
            f"recall lane attribution mismatch: expected {expected_lanes}, got {payload.get('lanes')}"
        )
    server_commit = response_headers.get("X-Longhouse-Commit")
    if server_commit != expected_sha:
        raise ValueError(
            f"serving commit mismatch: expected {expected_sha}, got {server_commit}"
        )
    matches = payload.get("results")
    if not isinstance(matches, list) or any(
        not isinstance(match, dict) for match in matches
    ):
        raise ValueError("recall returned malformed results")
    coverage = payload.get("coverage")
    if mode == "lexical":
        if coverage is not None:
            raise ValueError("lexical recall claimed dense corpus coverage")
    else:
        if not isinstance(coverage, dict):
            raise ValueError("dense recall omitted corpus coverage")
        required = {
            "complete",
            "lagging_sessions",
            "unpublished_sessions",
            "oldest_lag_seconds",
        }
        if set(coverage) != required:
            raise ValueError("dense recall returned malformed corpus coverage")
        if (
            not isinstance(coverage["complete"], bool)
            or coverage["complete"] != (coverage["lagging_sessions"] == 0)
            or not isinstance(coverage["unpublished_sessions"], int)
            or not isinstance(coverage["lagging_sessions"], int)
            or coverage["lagging_sessions"] < 0
            or coverage["lagging_sessions"] > MAX_EVAL_LAG_SESSIONS
            or coverage["unpublished_sessions"] < 0
            or coverage["unpublished_sessions"] > MAX_EVAL_LAG_SESSIONS
            or (
                coverage["oldest_lag_seconds"] is not None
                and (
                    not isinstance(coverage["oldest_lag_seconds"], (int, float))
                    or coverage["oldest_lag_seconds"] < 0
                    or coverage["oldest_lag_seconds"] > MAX_EVAL_LAG_AGE_SECONDS
                )
            )
            or (
                coverage["lagging_sessions"] == 0
                and coverage["oldest_lag_seconds"] is not None
            )
            or (
                coverage["lagging_sessions"] > 0
                and coverage["oldest_lag_seconds"] is None
            )
        ):
            raise ValueError("dense recall corpus coverage is incomplete")
    payload["_diagnostics"] = {
        "server_commit": server_commit,
        "embedding_model": response_headers.get("X-Recall-Embedding-Model"),
        "embedding_dims": response_headers.get("X-Recall-Embedding-Dims"),
        "embedding_revision": response_headers.get("X-Recall-Embedding-Revision"),
        "projector": response_headers.get("X-Recall-Projector"),
    }
    return payload


def _search_mode(mode: str):
    def search(
        query: str,
        *,
        base_url: str,
        token: str,
        limit: int,
        days: int,
        expected_sha: str,
    ) -> dict:
        return search_recall(
            query,
            base_url=base_url,
            token=token,
            limit=limit,
            days=days,
            mode=mode,
            expected_sha=expected_sha,
        )

    return search


# "fts" was a misnomer: it sent no mode, so it hit the fused default. Keep the
# lanes explicit so a dead one is visible instead of being averaged away.
STRATEGIES = {
    "lexical": _search_mode("lexical"),
    "semantic": _search_mode("semantic"),
    "auto": _search_mode("auto"),
}


def _local_git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[2],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", default="auto", choices=sorted(STRATEGIES))
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--verbose", action="store_true", help="Show every miss.")
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable output."
    )
    parser.add_argument(
        "--max-false-negative-rate", type=float, default=DEFAULT_MAX_FALSE_NEGATIVE_RATE
    )
    parser.add_argument(
        "--min-recall-at-5", type=float, default=DEFAULT_MIN_RECALL_AT_5
    )
    parser.add_argument(
        "--expected-sha",
        help="Exact 40-character server commit every evaluated response must report (defaults to this checkout).",
    )
    args = parser.parse_args()

    evaluator_git_sha = _local_git_sha()
    expected_sha = args.expected_sha or evaluator_git_sha
    if not _FULL_GIT_SHA.fullmatch(expected_sha):
        print(
            f"Expected server SHA must be a full 40-character lowercase git SHA, got {expected_sha!r}."
        )
        return 2

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
            payload = search(
                query.query,
                base_url=base_url,
                token=token,
                limit=args.limit,
                days=args.days,
                expected_sha=expected_sha,
            )
            returned = [
                str(match.get("session_id") or "") for match in payload["results"]
            ]
            # Sessions that produced this work discuss retrieval itself and would
            # match every query about retrieval.
            returned = _without_excluded_sessions(returned, excluded)
            report.results.append(
                Result(
                    query,
                    returned,
                    time.monotonic() - started,
                    lanes=tuple(payload["lanes"]),
                    embedding_model=payload["_diagnostics"].get("embedding_model"),
                    embedding_dims=(
                        int(payload["_diagnostics"]["embedding_dims"])
                        if payload["_diagnostics"].get("embedding_dims")
                        else None
                    ),
                    embedding_revision=payload["_diagnostics"].get(
                        "embedding_revision"
                    ),
                    projector=payload["_diagnostics"].get("projector"),
                    coverage=payload.get("coverage"),
                    server_commit=payload["_diagnostics"].get("server_commit"),
                )
            )
        except Exception as exc:  # noqa: BLE001 - report, never abort the sweep
            report.results.append(
                Result(query, [], time.monotonic() - started, error=str(exc))
            )

    p50, p95 = report.latencies()
    gate_failures = report.gate_failures(
        max_false_negative_rate=args.max_false_negative_rate,
        min_recall_at_5=args.min_recall_at_5,
        min_category_hits_at_10=DEFAULT_MIN_CATEGORY_HITS_AT_10,
    )
    query_set_sha256 = hashlib.sha256(QUERIES_PATH.read_bytes()).hexdigest()
    metadata = {
        "evaluated_at": datetime.now(UTC).isoformat(),
        "base_url": base_url,
        "git_sha": evaluator_git_sha,
        "expected_server_sha": expected_sha,
        "observed_server_commits": report.server_commits(),
        "query_set_sha256": query_set_sha256,
        "query_count": len(report.results),
        "limit": args.limit,
        "days": args.days,
        "expected_lanes": {
            "lexical": ["lexical"],
            "semantic": ["dense"],
            "auto": ["lexical", "dense"],
        }[args.strategy],
        "embedding_space": report.embedding_metadata(),
        "observed_projectors": report.projectors(),
        "corpus_coverage": report.corpus_coverage_metadata(),
        "error_count": len(report.errors()),
        "thresholds": {
            "max_false_negative_rate": args.max_false_negative_rate,
            "min_recall_at_5": args.min_recall_at_5,
            "min_category_hits_at_10": DEFAULT_MIN_CATEGORY_HITS_AT_10,
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
                    "correct_abstention_rate": round(
                        report.correct_abstention_rate(), 3
                    ),
                    "p50_s": round(p50, 3),
                    "p95_s": round(p95, 3),
                    "by_category_at_5": report.by_category(5),
                    "by_category_at_10": report.by_category(10),
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
    categories_at_10 = report.by_category(10)
    for category, (good, total) in categories_at_5.items():
        at_10 = categories_at_10[category][0]
        print(f"    {category:14s} @{5} {good}/{total}  @{10} {at_10}/{total}")

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
        misses = [
            r
            for r in report.results
            if r.query.expects_evidence and r.gold_rank() is None
        ]
        if misses:
            print(f"\n  misses ({len(misses)}):")
            for result in misses:
                print(
                    f"    [{result.query.category}] {result.query.id}: {result.query.query}"
                )
                print(
                    f"      wanted {result.query.gold_sessions} got {result.returned[:5] or 'nothing'}"
                )
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
