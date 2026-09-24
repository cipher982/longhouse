#!/usr/bin/env python3
"""Generate a representative exact-phrase recall set from a frozen search DB."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path


TOKEN = re.compile(r"[A-Za-z0-9_./:=+%-]{3,}")
LONG_IDENTIFIER = re.compile(r"(?:https?://|/)[^\s\"'<>]{65,}")
ESCAPED = re.compile(r"\\(?:[\"/bfnrt]|u[0-9a-fA-F]{4})")
TARGETS = {
    "conversation": 80,
    "json_escaped": 60,
    "large_output_tail": 60,
    "long_identifier": 40,
    "duplicate_output": 20,
    "hidden_or_deleted": 20,
}

# This is intentionally kept identical to transcript_content.redact_secrets. The
# evaluator runs against a bare frozen DB, so importing the application package
# would make the generator depend on its local environment.
REDACTION_PATTERNS = [
    (re.compile(r"\bsk-ant-[\w-]{20,}\b"), "[ANTHROPIC_KEY]"),
    (re.compile(r"\bsk-[\w-]{20,}\b"), "[OPENAI_KEY]"),
    (re.compile(r"""(?i)[\"']api[_-]?key[\"']\s*:\s*[\"'][a-zA-Z0-9_-]{20,}[\"']"""), '"apiKey": "[REDACTED]"'),
    (re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)['\"]?[a-zA-Z0-9_-]{20,}['\"]?"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(bearer\s+)[a-zA-Z0-9_.-]{20,}"), r"\1[BEARER_TOKEN]"),
    (re.compile(r"\bAKIA[A-Z0-9]{16}\b"), "[AWS_ACCESS_KEY]"),
    (re.compile(r"\bASIA[A-Z0-9]{16}\b"), "[AWS_TEMP_KEY]"),
    (re.compile(r"(?i)(aws[_-]?secret[_-]?access[_-]?key\s*[=:]\s*)['\"]?[a-zA-Z0-9/+=]{40}['\"]?"), r"\1[AWS_SECRET]"),
    (re.compile(r"\bgithub_pat_[a-zA-Z0-9_]{20,}\b"), "[GITHUB_PAT]"),
    (re.compile(r"\bgh[pousr]_[a-zA-Z0-9]{36,}\b"), "[GITHUB_TOKEN]"),
    (re.compile(r"\bxox[bpar]-[a-zA-Z0-9-]+\b"), "[SLACK_TOKEN]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "[PRIVATE_KEY]"),
    (re.compile(r"\beyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\b"), "[JWT_TOKEN]"),
    (re.compile(r"(?i)(secret|password|token|credential)[_-]?\s*[=:]\s*['\"]?(?!\[)[^\s'\"]{8,}['\"]?"), r"\1=[REDACTED]"),
]

# searchable_events has no index on source_event_id, so a correlated EXISTS
# rescans millions of rows per candidate. Materialize both sets once.
VISIBILITY_SETUP = """
    CREATE TEMP TABLE eligible_ids(id INTEGER PRIMARY KEY);
    CREATE TEMP TABLE excluded_ids(id INTEGER PRIMARY KEY);
    INSERT OR IGNORE INTO eligible_ids SELECT s.source_event_id FROM searchable_events s
     WHERE coalesce(s.hidden_from_default_timeline, 0) = 0
       AND s.user_hidden_from_timeline = 0 AND s.tombstoned = 0
       AND s.user_state != 'deleted' AND s.test_scope_visible = 0;
    INSERT OR IGNORE INTO excluded_ids SELECT s.source_event_id FROM searchable_events s
     WHERE coalesce(s.hidden_from_default_timeline, 0) = 1
        OR s.user_hidden_from_timeline = 1 OR s.tombstoned = 1
        OR s.user_state = 'deleted' OR s.test_scope_visible = 1;
"""
ELIGIBLE = "e.id IN temp.eligible_ids"
EXCLUDED = "e.id IN temp.excluded_ids"


def redact_secrets(text: str) -> str:
    for pattern, replacement in REDACTION_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def phrase_match_count(connection: sqlite3.Connection, candidate: str, cap: int = 4) -> int:
    query = '"' + candidate.replace('"', '""') + '"'
    # Distinctiveness only asks "at most N?", so stop counting at N+1: common
    # tokens otherwise walk millions of postings per call.
    return int(
        connection.execute(
            "SELECT count(*) FROM (SELECT 1 FROM events_fts WHERE events_fts MATCH ? LIMIT ?)", (query, cap)
        ).fetchone()[0]
    )


# Token counts repeat across rows; one FTS probe per distinct token per run.
_TOKEN_RARITY: dict[str, int] = {}


def distinctive_phrase(connection: sqlite3.Connection, text: str, max_matches: int = 3) -> str | None:
    tokens = [match.group(0) for match in TOKEN.finditer(text)][:300]
    if len(tokens) < 3:
        return None
    # A rare token is a useful first discriminator, then the phrase count is the
    # actual contract. Prefer longer windows around that token.
    rarity = _TOKEN_RARITY
    for token in tokens:
        if token not in rarity:
            rarity[token] = phrase_match_count(connection, token)
    for length in range(8, 2, -1):
        for start in range(len(tokens) - length + 1):
            window = tokens[start : start + length]
            candidate = " ".join(window)
            if redact_secrets(candidate) != candidate:
                continue
            if max_matches <= 3 and min(rarity[token] for token in window) > max_matches:
                continue
            if phrase_match_count(connection, candidate, max_matches + 1) <= max_matches:
                return candidate
    return None


def candidate_rows(connection: sqlite3.Connection, category: str) -> Iterable[sqlite3.Row]:
    if category == "conversation":
        sql = f"""SELECT e.* , e.content_text AS text FROM events e
                 WHERE e.content_text IS NOT NULL AND length(e.content_text) >= 24 AND {ELIGIBLE}
                 ORDER BY (e.id * 2654435761) % 4294967311 LIMIT 50000"""
    elif category == "json_escaped":
        sql = f"""SELECT e.* , e.tool_output_text AS text FROM events e
                 WHERE e.tool_output_text IS NOT NULL AND instr(e.tool_output_text, '\\') > 0
                   AND {ELIGIBLE} ORDER BY (e.id * 2654435761) % 4294967311 LIMIT 50000"""
    elif category == "large_output_tail":
        sql = f"""SELECT e.* , substr(e.tool_output_text, max(65537, cast(length(e.tool_output_text) * .9 AS integer))) AS text
                 FROM events e WHERE length(e.tool_output_text) > 65536 AND {ELIGIBLE}
                 ORDER BY (e.id * 2654435761) % 4294967311 LIMIT 50000"""
    elif category == "long_identifier":
        sql = f"""SELECT e.* , coalesce(e.content_text, '') || ' ' || coalesce(e.tool_output_text, '') AS text
                 FROM events e WHERE (instr(e.content_text, '/') > 0 OR instr(e.tool_output_text, '/') > 0)
                   AND {ELIGIBLE} ORDER BY (e.id * 2654435761) % 4294967311 LIMIT 100000"""
    else:
        sql = f"""SELECT e.* , coalesce(e.content_text, e.tool_output_text) AS text FROM events e
                 WHERE coalesce(e.content_text, e.tool_output_text) IS NOT NULL AND {EXCLUDED}
                 ORDER BY (e.id * 2654435761) % 4294967311 LIMIT 50000"""
    yield from connection.execute(sql)


def make_record(connection: sqlite3.Connection, row: sqlite3.Row, category: str, *, hidden: bool = False) -> dict[str, object] | None:
    text = str(row["text"] or "")
    if category == "json_escaped" and not ESCAPED.search(text):
        return None
    if category == "long_identifier":
        match = LONG_IDENTIFIER.search(text)
        if not match:
            return None
        text = text[max(0, match.start() - 160) : match.end() + 160]
    query = distinctive_phrase(connection, text)
    if not query:
        return None
    result: dict[str, object] = {
        "query": query,
        "gold_event_ids": [row["event_id"]],
        "gold_sessions": [row["session_id"]],
        "category": category,
        "provider": row["provider"],
    }
    if hidden:
        result["must_not_return"] = True
    return result


def duplicate_candidates(connection: sqlite3.Connection) -> Iterable[dict[str, object]]:
    # One bounded streaming pass: hash outputs and keep those recurring in at
    # least three sessions. Grouping or equality-filtering on the full output
    # text rescans an unindexed multi-GB column per group.
    seen: dict[tuple[str, str], set[str]] = defaultdict(set)
    text_by_key: dict[tuple[str, str], str] = {}
    rows = connection.execute(f"""
        SELECT e.provider, e.session_id, e.tool_output_text AS text FROM events e
        WHERE length(e.tool_output_text) BETWEEN 24 AND 16384 AND {ELIGIBLE}
        ORDER BY (e.id * 2654435761) % 4294967311 LIMIT 300000
    """)
    for row in rows:
        key = (str(row["provider"]), hashlib.sha1(str(row["text"]).encode()).hexdigest())
        seen[key].add(str(row["session_id"]))
        text_by_key.setdefault(key, str(row["text"]))
    for key, sessions in seen.items():
        if len(sessions) < 3:
            continue
        # A duplicate recurs by definition, so "distinctive" means bounded, not unique.
        query = distinctive_phrase(connection, text_by_key[key], max_matches=50)
        if not query:
            continue
        # Gold is every eligible occurrence of the chosen phrase, from the index itself.
        matches = list(connection.execute(f"""
            SELECT e.event_id, e.session_id FROM events_fts JOIN events e ON e.id = events_fts.rowid
            WHERE events_fts MATCH ? AND {ELIGIBLE}
        """, ('"' + query.replace('"', '""') + '"',)))
        gold_sessions = list(dict.fromkeys(str(m["session_id"]) for m in matches))
        if len(gold_sessions) < 3:
            continue
        yield {
            "query": query,
            "gold_event_ids": [m["event_id"] for m in matches],
            "gold_sessions": gold_sessions,
            "category": "duplicate_output",
            "provider": key[0],
        }


def select_round_robin(candidates: Iterable[dict[str, object]], target: int) -> list[dict[str, object]]:
    by_provider: dict[str, list[dict[str, object]]] = defaultdict(list)
    seen: set[str] = set()
    for candidate in candidates:
        query = str(candidate["query"])
        if query not in seen:
            seen.add(query)
            by_provider[str(candidate["provider"])].append(candidate)
        # Candidates arrive in a hashed id order, so a 4x pool already carries
        # the provider mix; draining every row costs hours of FTS probes.
        if len(seen) >= target * 4:
            break
    selected: list[dict[str, object]] = []
    while len(selected) < target:
        progressed = False
        for provider in sorted(by_provider):
            if by_provider[provider]:
                selected.append(by_provider[provider].pop(0))
                progressed = True
                if len(selected) == target:
                    break
        if not progressed:
            break
    return selected


def records(connection: sqlite3.Connection) -> Iterable[dict[str, object]]:
    for category, target in TARGETS.items():
        started = time.monotonic()
        if category == "duplicate_output":
            selected = select_round_robin(duplicate_candidates(connection), target)
        else:
            selected = select_round_robin(
                (record for row in candidate_rows(connection, category)
                 if (record := make_record(connection, row, category, hidden=category == "hidden_or_deleted"))),
                target,
            )
        if len(selected) != target:
            raise RuntimeError(f"{category}: found {len(selected)} distinctive labels, need {target}")
        print(f"{category}: {len(selected)} labels in {time.monotonic() - started:.1f}s", file=sys.stderr, flush=True)
        yield from selected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    connection = sqlite3.connect(f"file:{args.database.absolute()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.executescript(VISIBILITY_SETUP)
    try:
        items = list(records(connection))
    finally:
        connection.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(item, sort_keys=True) + "\n" for item in items))
    print(json.dumps({"output": str(args.output), "count": len(items)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
