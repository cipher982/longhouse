"""Find legacy-converted sessions whose conversation a native session already holds.

The 2026-07 legacy conversion and the engine's later native re-ship sometimes
produced two sessions for one conversation (the native copy under a different
session id), so the timeline showed it twice. A legacy session qualifies for
retirement only when every check below holds against exactly one native twin:

- same owner, provider and machine; the twin renders, has native raw and no legacy raw;
- the same provider session id when both carry one, else start times within
  ``window_seconds``;
- every distinct assistant text and every distinct user text of the legacy render
  appears with the same role in the twin's thread (the twin plus its native
  subagent sessions, which the legacy conversion had merged into the parent),
  compared after collapsing whitespace and Cursor's ``<timestamp>``/``<user_query>``
  wrappers;
- the legacy raw archive holds chunks for the session, and the thread's native raw
  record count is at least the archive's unique line count.

This module only reads. Retirement goes through catalogd
(``storage.session.legacy_twin.retire.v2``), which re-checks owner, provider,
machine, identity or start window, and provenance under the writer lock.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from pathlib import Path

import zstandard

from zerg.storage_v2.render_objects import read_render_object

DEFAULT_WINDOW_SECONDS = 120
_TIMESTAMP_BLOCK = re.compile(r"<timestamp>.*?</timestamp>", re.S)
_WRAPPER_TAG = re.compile(r"</?(?:timestamp|user_query)>")


@dataclass
class TwinEvidence:
    legacy_session_id: str
    twin_session_id: str
    provider: str
    start_delta_seconds: float
    subagent_sessions: int
    legacy_assistant_texts: int
    missing_assistant_texts: int
    legacy_user_texts: int
    missing_user_texts: int
    native_records: int
    archive_unique_lines: int | None
    reasons: list[str] = field(default_factory=list)
    ambiguous: bool = False

    @property
    def qualifies(self) -> bool:
        return not self.reasons and not self.ambiguous

    def as_dict(self) -> dict[str, object]:
        return {**asdict(self), "qualifies": self.qualifies}


def _normalize(text: str) -> str:
    return " ".join(_WRAPPER_TAG.sub(" ", _TIMESTAMP_BLOCK.sub(" ", text)).split())


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(str(value))


class _Corpus:
    def __init__(self, live_database: Path, object_root: Path, archive_root: Path) -> None:
        self.connection = sqlite3.connect(f"file:{live_database}?mode=ro", uri=True, timeout=60)
        self.connection.row_factory = sqlite3.Row
        self.object_root = object_root
        self.archive_root = archive_root

    def close(self) -> None:
        self.connection.close()

    def session(self, session_id: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()

    def texts(self, session_ids: list[str], roles: set[str]) -> set[str]:
        found: set[str] = set()
        for session_id in session_ids:
            rows = self.connection.execute(
                """
                SELECT o.object_path, o.object_hash
                FROM render_objects o JOIN sessions s
                  ON s.session_id = o.session_id AND s.current_render_generation = o.generation_id
                WHERE o.session_id = ? AND o.retired_at IS NULL
                """,
                (session_id,),
            )
            for object_path, object_hash in rows:
                decoded = read_render_object(self.object_root, str(object_path), expected_object_hash=str(object_hash))
                for record in decoded.spec.records:
                    if record.role in roles and (record.content_text or "").strip():
                        found.add(_normalize(record.content_text))
        return found

    def native_records(self, session_ids: list[str]) -> int:
        placeholders = ",".join("?" for _ in session_ids)
        row = self.connection.execute(
            f"SELECT COALESCE(SUM(record_count), 0) FROM raw_objects "
            f"WHERE session_id IN ({placeholders}) AND retired_at IS NULL AND provenance_kind = 'native'",
            session_ids,
        ).fetchone()
        return int(row[0])

    def archive_unique_lines(self, session_id: str) -> int | None:
        """Unique raw lines the legacy archive holds for the session, or None when it has none."""

        chunks = sorted(self.archive_root.glob(f"tenants/*/sessions/{session_id}/chunks/*.jsonl.zst"))
        if not chunks:
            return None
        hashes: set[str] = set()
        for chunk in chunks:
            payload = zstandard.ZstdDecompressor().decompress(chunk.read_bytes(), max_output_size=1 << 31)
            for line in payload.splitlines():
                if line.strip():
                    value = json.loads(line).get("raw_sha256")
                    if value:
                        hashes.add(str(value))
        return len(hashes)

    def legacy_sessions(self, session_ids: list[str] | None) -> list[str]:
        query = """
            SELECT s.session_id FROM sessions s
            WHERE s.render_state = 'ready'
              AND EXISTS (SELECT 1 FROM raw_objects r WHERE r.session_id = s.session_id
                          AND r.retired_at IS NULL AND r.provenance_kind LIKE 'legacy\\_%' ESCAPE '\\')
              AND NOT EXISTS (SELECT 1 FROM raw_objects r WHERE r.session_id = s.session_id
                              AND r.retired_at IS NULL AND r.provenance_kind NOT LIKE 'legacy\\_%' ESCAPE '\\')
              AND NOT EXISTS (SELECT 1 FROM session_tombstones t WHERE t.session_id = s.session_id)
        """
        found = [str(row[0]) for row in self.connection.execute(query)]
        if session_ids is None:
            return found
        wanted = set(session_ids)
        return [session_id for session_id in found if session_id in wanted]

    def candidates(self, legacy: sqlite3.Row, window_seconds: int) -> list[sqlite3.Row]:
        rows = self.connection.execute(
            """
            SELECT s.* FROM sessions s
            WHERE s.session_id != ? AND s.owner_id IS ? AND s.provider = ? AND s.machine_id IS ? AND s.render_state = 'ready'
              AND EXISTS (SELECT 1 FROM raw_objects r WHERE r.session_id = s.session_id
                          AND r.retired_at IS NULL AND r.provenance_kind = 'native')
              AND NOT EXISTS (SELECT 1 FROM raw_objects r WHERE r.session_id = s.session_id
                              AND r.retired_at IS NULL AND r.provenance_kind LIKE 'legacy\\_%' ESCAPE '\\')
              AND NOT EXISTS (SELECT 1 FROM session_tombstones t WHERE t.session_id = s.session_id)
            """,
            (legacy["session_id"], legacy["owner_id"], legacy["provider"], legacy["machine_id"]),
        ).fetchall()
        legacy_identity = legacy["provider_session_id"]
        legacy_start = _parse_time(legacy["started_at"])
        matched = []
        for row in rows:
            if legacy_identity and row["provider_session_id"]:
                if row["provider_session_id"] == legacy_identity:
                    matched.append(row)
            elif abs((_parse_time(row["started_at"]) - legacy_start).total_seconds()) <= window_seconds:
                matched.append(row)
        return matched

    def subagents(self, session_id: str) -> list[str]:
        rows = self.connection.execute(
            "SELECT session_id FROM sessions WHERE subagent_parent_session_id = ? AND render_state != 'retired' AND raw_state != 'retired'",
            (session_id,),
        )
        return [str(row[0]) for row in rows]


def find_legacy_twins(
    *,
    live_database: Path,
    object_root: Path,
    archive_root: Path,
    session_ids: list[str] | None = None,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
) -> list[TwinEvidence]:
    """Evidence for every (legacy session, native candidate) pair; read-only."""

    corpus = _Corpus(live_database, object_root, archive_root)
    try:
        evidence: list[TwinEvidence] = []
        for legacy_id in corpus.legacy_sessions(session_ids):
            legacy = corpus.session(legacy_id)
            if legacy is None:
                continue
            candidates = corpus.candidates(legacy, window_seconds)
            if not candidates:
                continue
            legacy_assistant = corpus.texts([legacy_id], {"assistant"})
            legacy_user = corpus.texts([legacy_id], {"user"})
            archive_lines = corpus.archive_unique_lines(legacy_id)
            pair_evidence: list[TwinEvidence] = []
            for twin in candidates:
                twin_id = str(twin["session_id"])
                children = corpus.subagents(twin_id)
                thread = [twin_id, *children]
                thread_assistant = corpus.texts(thread, {"assistant"})
                thread_user = corpus.texts(thread, {"user"})
                native_records = corpus.native_records(thread)
                item = TwinEvidence(
                    legacy_session_id=legacy_id,
                    twin_session_id=twin_id,
                    provider=str(legacy["provider"]),
                    start_delta_seconds=round(
                        abs((_parse_time(twin["started_at"]) - _parse_time(legacy["started_at"])).total_seconds()), 3
                    ),
                    subagent_sessions=len(children),
                    legacy_assistant_texts=len(legacy_assistant),
                    missing_assistant_texts=len(legacy_assistant - thread_assistant),
                    legacy_user_texts=len(legacy_user),
                    missing_user_texts=len(legacy_user - thread_user),
                    native_records=native_records,
                    archive_unique_lines=archive_lines,
                )
                if not legacy_assistant:
                    item.reasons.append("legacy_has_no_assistant_text")
                if item.missing_assistant_texts:
                    item.reasons.append("assistant_text_missing_from_twin")
                if item.missing_user_texts:
                    item.reasons.append("user_text_missing_from_twin")
                if archive_lines is None:
                    item.reasons.append("archive_lines_unavailable")
                elif native_records < archive_lines:
                    item.reasons.append("native_records_below_archive_lines")
                pair_evidence.append(item)
            if sum(item.qualifies for item in pair_evidence) > 1:
                for item in pair_evidence:
                    item.ambiguous = True
            evidence.extend(pair_evidence)
        return evidence
    finally:
        corpus.close()


__all__ = ["DEFAULT_WINDOW_SECONDS", "TwinEvidence", "find_legacy_twins"]
