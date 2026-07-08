#!/usr/bin/env python3
"""Profile the retrieval.db recall index with a synthetic corpus."""

from __future__ import annotations

import argparse
import random
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_ROOT = REPO_ROOT / "server"
sys.path.insert(0, str(SERVER_ROOT))

from zerg.services.retrieval_index import connect_retrieval_db  # noqa: E402
from zerg.services.retrieval_index import get_chunks_by_ids  # noqa: E402
from zerg.services.retrieval_index import initialize_retrieval_db  # noqa: E402
from zerg.services.retrieval_index import project_session_chunks  # noqa: E402
from zerg.services.retrieval_index import replace_session_chunks  # noqa: E402
from zerg.services.retrieval_index import search_lexical_chunks  # noqa: E402


@dataclass
class Timing:
    label: str
    samples_ms: list[float]

    def p50(self) -> float:
        return _percentile(self.samples_ms, 50)

    def p95(self) -> float:
        return _percentile(self.samples_ms, 95)

    def max(self) -> float:
        return max(self.samples_ms) if self.samples_ms else 0.0


def _percentile(values: list[float], pct: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((pct / 100) * (len(ordered) - 1))))
    return ordered[index]


def _session(index: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"session-{index}",
        provider="codex" if index % 2 else "claude",
        environment="production",
        project="longhouse" if index % 3 else "other",
        device_id=f"device-{index % 8}",
        cwd="/Users/davidrose/git/zerg/longhouse",
        git_repo="cipher982/longhouse",
        git_branch=f"feature/recall-{index % 17}",
        started_at=datetime(2026, 7, 8, tzinfo=timezone.utc),
        last_activity_at=datetime(2026, 7, 8, tzinfo=timezone.utc),
        transcript_revision=1,
    )


def _events(session_index: int, events_per_session: int) -> list[dict]:
    events: list[dict] = []
    special_terms = [
        "infisical cache timeout",
        "source_lines full table scan",
        "codex bridge managed session",
        "iOS tool call dropped",
        "media inline data urls backfill",
        "request timeout middleware",
    ]
    for event_index in range(events_per_session):
        event_id = session_index * 100_000 + event_index + 1
        term = special_terms[(session_index + event_index) % len(special_terms)]
        if event_index % 5 == 0:
            events.append(
                {
                    "id": event_id,
                    "role": "user",
                    "content_text": f"Investigate {term} in server/zerg/routers/agents_search.py",
                    "timestamp": _event_timestamp(event_index),
                }
            )
        elif event_index % 5 == 3:
            events.append(
                {
                    "id": event_id,
                    "role": "tool",
                    "tool_name": "exec_command",
                    "tool_output_text": f"OperationalError around {term} source_lines --no-verify feature/recall-index",
                    "timestamp": _event_timestamp(event_index),
                }
            )
        else:
            events.append(
                {
                    "id": event_id,
                    "role": "assistant",
                    "content_text": f"Conclusion for {term}: use retrieval.db and bounded parent hydration.",
                    "timestamp": _event_timestamp(event_index),
                }
            )
    return events


def _event_timestamp(event_index: int) -> datetime:
    return datetime(2026, 7, 8, tzinfo=timezone.utc) + timedelta(seconds=event_index)


def _time_ms(fn):
    started = time.perf_counter()
    value = fn()
    return value, (time.perf_counter() - started) * 1000


def _profile_queries(conn: sqlite3.Connection, queries: list[str], repetitions: int) -> list[Timing]:
    timings: dict[str, list[float]] = {query: [] for query in queries}
    hydrate_timings: list[float] = []
    for _ in range(repetitions):
        for query in queries:
            hits, elapsed = _time_ms(lambda q=query: search_lexical_chunks(conn, q, limit=5))
            timings[query].append(elapsed)
            _, hydrate_elapsed = _time_ms(lambda h=hits: get_chunks_by_ids(conn, [hit.parent_chunk_id for hit in h if hit.parent_chunk_id]))
            hydrate_timings.append(hydrate_elapsed)
    return [Timing(label=f"query:{query}", samples_ms=samples) for query, samples in timings.items()] + [
        Timing(label="hydrate:parents", samples_ms=hydrate_timings)
    ]


def _profile_main_db_writes_during_retrieval(retrieval_path: Path, main_db_path: Path, duration_s: float) -> Timing:
    stop = threading.Event()

    def reader_loop() -> None:
        with connect_retrieval_db(retrieval_path) as reader_conn:
            queries = ["timeout", "source_lines", "missing-nohit"]
            index = 0
            while not stop.is_set():
                search_lexical_chunks(reader_conn, queries[index % len(queries)], limit=5)
                index += 1

    main = sqlite3.connect(str(main_db_path), timeout=5.0)
    main.execute("PRAGMA journal_mode=WAL")
    main.execute("CREATE TABLE IF NOT EXISTS write_probe(id INTEGER PRIMARY KEY, value TEXT)")
    main.commit()
    reader = threading.Thread(target=reader_loop, daemon=True)
    reader.start()
    samples: list[float] = []
    deadline = time.monotonic() + duration_s
    try:
        while time.monotonic() < deadline:
            def _write_probe() -> None:
                main.execute("INSERT INTO write_probe(value) VALUES('ok')")
                main.commit()

            _, elapsed = _time_ms(_write_probe)
            samples.append(elapsed)
    finally:
        stop.set()
        reader.join(timeout=2)
        main.close()
    return Timing(label="main_db_write_during_retrieval", samples_ms=samples)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=int, default=1000)
    parser.add_argument("--events", type=int, default=20)
    parser.add_argument("--repetitions", type=int, default=20)
    parser.add_argument("--write-probe-seconds", type=float, default=2.0)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    random.seed(args.seed)

    with tempfile.TemporaryDirectory(prefix="longhouse-retrieval-profile-") as tmp:
        tmp_path = Path(tmp)
        retrieval_path = tmp_path / "retrieval.db"
        main_db_path = tmp_path / "main.db"
        with connect_retrieval_db(retrieval_path) as conn:
            initialize_retrieval_db(conn)
            projection_samples: list[float] = []
            indexing_samples: list[float] = []
            for index in range(args.sessions):
                session = _session(index)
                chunks, project_elapsed = _time_ms(lambda s=session, i=index: project_session_chunks(s, _events(i, args.events)))
                projection_samples.append(project_elapsed)
                _, index_elapsed = _time_ms(lambda s=session, c=chunks: replace_session_chunks(conn, s.id, c))
                indexing_samples.append(index_elapsed)

            query_timings = _profile_queries(
                conn,
                [
                    "infisical cache timeout",
                    "server/zerg/routers/agents_search.py",
                    "--no-verify",
                    "missing-nohit",
                    "source_lines full table scan",
                ],
                args.repetitions,
            )
            write_timing = _profile_main_db_writes_during_retrieval(retrieval_path, main_db_path, args.write_probe_seconds)
            chunk_count = conn.execute("SELECT count(*) FROM recall_chunks").fetchone()[0]
            child_count = conn.execute("SELECT count(*) FROM recall_chunks WHERE retrieval_role = 'child'").fetchone()[0]

        timings = [
            Timing(label="project_session_chunks", samples_ms=projection_samples),
            Timing(label="replace_session_chunks", samples_ms=indexing_samples),
            *query_timings,
            write_timing,
        ]
        print(f"retrieval_db={retrieval_path}")
        print(f"sessions={args.sessions} events_per_session={args.events} chunks={chunk_count} child_chunks={child_count}")
        print(f"retrieval_db_bytes={retrieval_path.stat().st_size if retrieval_path.exists() else 0}")
        for timing in timings:
            print(
                f"{timing.label}: "
                f"n={len(timing.samples_ms)} "
                f"p50_ms={timing.p50():.3f} "
                f"p95_ms={timing.p95():.3f} "
                f"max_ms={timing.max():.3f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
