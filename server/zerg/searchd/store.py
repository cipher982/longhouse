"""SQLite-backed derived index operations executed only inside searchd."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID
from uuid import uuid4

from zerg.services.provider_interaction_semantics import classify_provider_interaction

SCHEMA_VERSION = 1
SCHEMA_GENERATION = "searchd-v3-published-semantic-corpus-with-fenced-embeddings"
SEARCHABLE_RETENTION_DAYS = 91
SEARCHABLE_FAST_WINDOW_DAYS = 90
SEARCHABLE_FAST_WINDOW_MARGIN_SECONDS = 300
_OBJECT_SET_DOMAIN = b"longhouse-search-object-set-v1\0"
_WORKLOG_PAGE_BYTES = 700_000
_WORKLOG_EVENT_CONTENT_BYTES = 128 * 1024
_WORKLOG_TRUNCATION_MARKER = "\n\n[Longhouse worklog export truncated oversized message]"
_WORKLOG_SNAPSHOT_BYTES = 64 * 1024 * 1024
_WORKLOG_SNAPSHOT_MAX_PAGES = 200
_WORKLOG_SNAPSHOT_TTL_SECONDS = 120.0
_WORKLOG_SNAPSHOT_LIMIT = 8
_EMBEDDING_SOURCE_PAGE_BYTES = 6 * 1024 * 1024

_PUBLISH_AGGREGATES_SQL = """
    SELECT
        SUM(CASE WHEN e.role = 'user' AND e.title_eligible = 1
                  AND (e.interaction_kind IS NULL OR e.interaction_kind NOT IN ('local_control', 'local_control_output', 'conversation_boundary'))
                 THEN 1 ELSE 0 END) AS user_messages,
        SUM(CASE WHEN e.role = 'assistant' AND e.tool_name IS NULL THEN 1 ELSE 0 END) AS assistant_messages,
        SUM(CASE WHEN e.tool_name IS NOT NULL THEN 1 ELSE 0 END) AS tool_calls,
        MAX(CASE
            WHEN e.branch_kind IS NOT NULL AND e.branch_kind NOT IN ('root', 'primary')
            THEN 1 ELSE 0
        END) AS is_sidechain
    FROM projection_membership m
    JOIN events e ON e.source_object_id = m.object_id
    WHERE m.session_id = ? AND m.generation_id = ? AND m.desired_revision = ?
      AND e.session_id = ? AND e.generation_id = ?
"""

_ARCHIVE_SEARCH_SQL = """
    SELECT e.id AS search_event_id, e.session_id, e.generation_id, e.source_object_id,
           e.record_ordinal, e.event_id, e.order_time_us,
           e.role, e.tool_name,
           snippet(events_fts, 0, '', '', ' … ', 24) AS content_snippet,
           snippet(events_fts, 1, '', '', ' … ', 24) AS tool_output_snippet,
           s.project, s.provider, s.environment, s.indexed_through, s.event_count,
           events_fts.rank AS rank
    FROM events_fts
    JOIN events e ON e.id = events_fts.rowid
    JOIN session_index s ON s.session_id = e.session_id AND s.generation_id = e.generation_id
    JOIN projection_membership m
      ON m.session_id = e.session_id
     AND m.generation_id = e.generation_id
     AND m.desired_revision = s.indexed_through
     AND m.object_id = e.source_object_id
    WHERE events_fts MATCH ? AND s.owner_id = ?
      AND (? IS NULL OR s.project = ?)
      AND (? IS NULL OR s.provider = ?)
      AND (? IS NULL OR s.environment = ?)
      AND (? IS NULL OR e.order_time_us >= ?)
      AND (? IS NULL OR e.order_time_us < ?)
      AND (e.role != 'user' OR (e.title_eligible = 1
           AND (e.interaction_kind IS NULL OR e.interaction_kind NOT IN ('local_control', 'local_control_output', 'conversation_boundary'))))
    ORDER BY events_fts.rank ASC
    LIMIT ?
"""

# Ranking the whole match set costs time linear in matches, not in results: a
# term matching 2.2M rows spent 3.4s scoring rows nobody would ever see. FTS5
# walks a doclist in rowid order with early exit, and `source_event_id` is the
# archive `events.id`, so a reverse-rowid walk yields the most recent matches
# without touching the rest of the doclist. bm25() evaluated during that walk
# scores only the rows actually visited.
#
# Filters live inside the walk rather than after it. Applied afterwards they
# made narrow windows *slower* — SQLite ranked everything, then scanned for
# survivors and often could not fill the limit at all.
#
# The walk stops at _CANDIDATE_CEILING. When it returns fewer rows than that,
# it provably saw every match and the ranking is exact; callers learn which
# happened from `ranking_scope` instead of having to guess.
#
# Snippets are built only for the rows actually returned. Building them for
# every candidate made cost scale with stored text rather than with results —
# real events carry multi-KB tool output, so snippetting a full candidate window
# cost seconds and still blew the deadline on hosted data even after the ranking
# fix. Snippetting the final page instead keeps that cost flat.
_SEARCHABLE_SEARCH_SQL = """
    WITH candidates AS (
        SELECT e.source_event_id AS search_event_id, bm25(searchable_fts) AS rank
        FROM searchable_fts
        JOIN searchable_events e ON e.source_event_id = searchable_fts.rowid
        WHERE searchable_fts MATCH ? AND e.owner_id = ?
          AND (? IS NULL OR e.project = ?)
          AND (? IS NULL OR e.provider = ?)
          AND (? IS NULL OR e.environment = ?)
          AND (? IS NULL OR e.order_time_us >= ?)
          AND (? IS NULL OR e.order_time_us < ?)
          AND (e.role != 'user' OR (e.title_eligible = 1
               AND (e.interaction_kind IS NULL OR e.interaction_kind NOT IN ('local_control', 'local_control_output', 'conversation_boundary'))))
        ORDER BY searchable_fts.rowid DESC
        LIMIT ?
    ), top AS (
        SELECT search_event_id, rank, (SELECT COUNT(*) FROM candidates) AS candidate_count
        FROM candidates
        ORDER BY rank ASC
        LIMIT ?
    )
    SELECT t.search_event_id, e.session_id, e.generation_id, e.source_object_id,
           e.record_ordinal, e.event_id, e.order_time_us,
           e.role, e.tool_name,
           snippet(searchable_fts, 0, '', '', ' … ', 24) AS content_snippet,
           snippet(searchable_fts, 1, '', '', ' … ', 24) AS tool_output_snippet,
           e.project, e.provider, e.environment, e.indexed_through, e.event_count,
           t.rank AS rank, t.candidate_count AS candidate_count
    FROM top t
    JOIN searchable_events e ON e.source_event_id = t.search_event_id
    JOIN searchable_fts ON searchable_fts.rowid = t.search_event_id
    WHERE searchable_fts MATCH ?
    ORDER BY t.rank ASC
"""

# Most recent matching events considered before ranking. Measured on a 5M-row
# corpus, the worst case (a term matching 2.2M events) costs ~346ms against a
# 500ms target, and ranking is exact for any term matching fewer than this many
# events — on a real corpus, every query carrying useful signal.
#
# A broad term combined with a narrow window is the one shape that stays slow
# (~2s, down from ~3.4s): the walk rejects nearly everything it visits, so it
# never fills its quota and cannot exit early. Deriving a rowid floor from the
# window was measured and rejected — MIN(source_event_id) over a time range is
# not index-only, costing ~570ms on every search including the rare-term
# queries that are otherwise sub-millisecond.
_CANDIDATE_CEILING = 50_000

# Focused plan tests and diagnostic tooling use this name for the all-history
# correctness lane. Interactive recent recall uses _SEARCHABLE_SEARCH_SQL.
_SEARCH_SQL = _ARCHIVE_SEARCH_SQL

_CONTEXT_TARGET_SQL = """
    SELECT e.order_time_us, e.event_key, s.event_count
    FROM events e
    JOIN session_index s ON s.session_id = e.session_id AND s.generation_id = e.generation_id
    JOIN projection_membership m
      ON m.session_id = e.session_id
     AND m.generation_id = e.generation_id
     AND m.desired_revision = s.indexed_through
     AND m.object_id = e.source_object_id
    WHERE e.id = ? AND e.session_id = ? AND e.generation_id = ? AND s.owner_id = ?
"""

# Same target row, located by transcript position instead of event id. The
# semantic lane knows where an episode starts in the published ordering but not
# which searchd row that is, so it anchors on the first event at or after that
# position. Ordering matches _CONTEXT_ROWS_SQL so the neighbour walk stays
# consistent with a lexical hit's.
_CONTEXT_TARGET_BY_POSITION_SQL = """
    SELECT e.order_time_us, e.event_key, s.event_count
    FROM events e
    JOIN session_index s ON s.session_id = e.session_id AND s.generation_id = e.generation_id
    JOIN projection_membership m
      ON m.session_id = e.session_id
     AND m.generation_id = e.generation_id
     AND m.desired_revision = s.indexed_through
     AND m.object_id = e.source_object_id
    WHERE e.session_id = ? AND e.generation_id = ? AND s.owner_id = ?
      AND e.order_time_us >= ?
    ORDER BY e.order_time_us ASC, e.event_key ASC
    LIMIT 1
"""

# Every field the clean projection reads, in the order the embeddings projector
# assembled its records. That projector sorted catalog records by
# (order_time, machine, provider, source, epoch, position, subordinal) and then
# numbered them, so reproducing this order here reproduces its clean indices.
# `source_position` is stored zero-padded, which sorts identically to the
# projector's integer compare.
_CONTEXT_ROWS_SQL = """
    SELECT e.id AS search_event_id, e.event_id, e.source_object_id, e.record_ordinal,
           e.order_time_us, e.role, e.content_text, e.tool_name
    FROM events e
    JOIN session_index s ON s.session_id = e.session_id AND s.generation_id = e.generation_id
    JOIN projection_membership m
      ON m.session_id = e.session_id
     AND m.generation_id = e.generation_id
     AND m.desired_revision = s.indexed_through
     AND m.object_id = e.source_object_id
    WHERE e.session_id = ? AND e.generation_id = ? AND s.owner_id = ?
      AND e.role IN ('user', 'assistant') AND e.content_text IS NOT NULL
      AND (e.role != 'user' OR (e.title_eligible = 1
           AND (e.interaction_kind IS NULL OR e.interaction_kind NOT IN ('local_control', 'local_control_output', 'conversation_boundary'))))
      AND {position_predicate}
    ORDER BY e.order_time_us {direction}, e.event_key {direction}
    LIMIT ?
"""


class WorklogPageTooLarge(RuntimeError):
    pass


class WorklogSnapshotError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class _WorklogSnapshot:
    owner_id: str
    window_start_us: int
    window_end_us: int
    include_test: bool
    sessions: list[dict[str, Any]]
    events: list[dict[str, Any]]
    expires_mono: float


def open_search_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError("searchd database path must not be a symlink")
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect(path)
        incompatible = _existing_store_is_incompatible(connection)
    except sqlite3.DatabaseError:
        if connection is not None:
            connection.close()
        incompatible = True
    if incompatible:
        if connection is not None:
            connection.close()
        _discard_derived_store(path)
        connection = _connect(path)
    assert connection is not None
    _initialize_schema(connection)
    return connection


def open_search_read_database(path: Path) -> sqlite3.Connection:
    """Open one read-only WAL connection after the writer has initialized schema."""

    if path.is_symlink():
        raise RuntimeError("searchd database path must not be a symlink")
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5.0, isolation_level=None, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=5.0, isolation_level=None, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _existing_store_is_incompatible(connection: sqlite3.Connection) -> bool:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    if not tables:
        return False
    if "search_meta" not in tables:
        return True
    row = connection.execute("SELECT schema_version, schema_generation, store_id FROM search_meta WHERE singleton = 1").fetchone()
    if row is None or (row["schema_version"], row["schema_generation"]) != (SCHEMA_VERSION, SCHEMA_GENERATION):
        return True
    try:
        return str(UUID(str(row["store_id"]))) != row["store_id"]
    except ValueError:
        return True


def _add_missing_episode_columns(connection: sqlite3.Connection) -> None:
    """Add nullable episode columns in place rather than rebuilding the store.

    Discarding is the store's normal answer to a schema change, and it is the
    right answer when existing rows would be wrong. A nullable locator is not
    that: old rows are merely incomplete, and they say so — a match with no
    locator reports unavailable evidence. Rebuilding to avoid a NULL would mean
    re-publishing every session and re-embedding every episode, which on the
    real corpus is a 17GB index and 82k embedding calls to add one derivable
    integer.
    """

    columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(episode_embeddings)").fetchall()}
    if "start_order_time_us" not in columns:
        connection.execute("ALTER TABLE episode_embeddings ADD COLUMN start_order_time_us INTEGER")


def _discard_derived_store(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        candidate.unlink(missing_ok=True)


def _initialize_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS search_meta (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            schema_version INTEGER NOT NULL,
            schema_generation TEXT NOT NULL,
            store_id TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS indexed_objects (
            object_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            generation_id TEXT NOT NULL,
            event_count INTEGER NOT NULL,
            projection_hash TEXT NOT NULL,
            indexed_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_indexed_objects_generation
            ON indexed_objects(session_id, generation_id, object_id);
        CREATE TABLE IF NOT EXISTS projection_membership (
            session_id TEXT NOT NULL,
            generation_id TEXT NOT NULL,
            desired_revision INTEGER NOT NULL,
            object_id TEXT NOT NULL,
            PRIMARY KEY(session_id, generation_id, desired_revision, object_id)
        );
        CREATE INDEX IF NOT EXISTS ix_projection_membership_object
            ON projection_membership(object_id);
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL UNIQUE,
            session_id TEXT NOT NULL,
            generation_id TEXT NOT NULL,
            source_object_id TEXT NOT NULL,
            record_ordinal INTEGER NOT NULL,
            event_id TEXT NOT NULL,
            order_time_us INTEGER NOT NULL,
            opaque_source_id TEXT NOT NULL,
            source_epoch TEXT NOT NULL,
            source_position TEXT NOT NULL,
            event_subordinal INTEGER NOT NULL,
            role TEXT NOT NULL,
            content_text TEXT,
            tool_name TEXT,
            tool_output_text TEXT,
            tool_call_id TEXT,
            thread_id TEXT,
            branch_kind TEXT,
            provider TEXT NOT NULL,
            interaction_kind TEXT NOT NULL DEFAULT 'provider_system',
            title_eligible INTEGER NOT NULL DEFAULT 0,
            machine_id TEXT NOT NULL,
            project TEXT,
            environment TEXT NOT NULL,
            cwd TEXT,
            git_repo TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_search_events_session_generation_order
            ON events(session_id, generation_id, order_time_us, event_key);
        CREATE INDEX IF NOT EXISTS ix_search_events_worklog
            ON events(order_time_us, session_id, role);
        CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
            content_text,
            tool_output_text,
            content='events',
            content_rowid='id',
            tokenize='unicode61 remove_diacritics 2'
        );
        CREATE TRIGGER IF NOT EXISTS events_ai AFTER INSERT ON events BEGIN
            INSERT INTO events_fts(rowid, content_text, tool_output_text)
            VALUES (new.id, new.content_text, new.tool_output_text);
        END;
        CREATE TRIGGER IF NOT EXISTS events_ad AFTER DELETE ON events BEGIN
            INSERT INTO events_fts(events_fts, rowid, content_text, tool_output_text)
            VALUES ('delete', old.id, old.content_text, old.tool_output_text);
        END;
        CREATE TRIGGER IF NOT EXISTS events_au AFTER UPDATE ON events BEGIN
            INSERT INTO events_fts(events_fts, rowid, content_text, tool_output_text)
            VALUES ('delete', old.id, old.content_text, old.tool_output_text);
            INSERT INTO events_fts(rowid, content_text, tool_output_text)
            VALUES (new.id, new.content_text, new.tool_output_text);
        END;
        -- Metadata and text are separate tables on purpose.
        --
        -- The candidate walk reads owner/project/environment/time — about 20
        -- bytes — but when those lived on the same row as the event text it had
        -- to touch the whole record to reach them. Rows average 2.4 KB, so a
        -- broad query faulted 36-93 MB of page cache to answer with 5 results,
        -- and on a volume where a random read costs 600 us that is seconds.
        --
        -- Splitting them keeps the walk on narrow rows. Text is read only for
        -- the page actually returned, through the FTS index that now owns it.
        CREATE TABLE IF NOT EXISTS searchable_events (
            source_event_id INTEGER PRIMARY KEY,
            owner_id TEXT NOT NULL,
            project TEXT,
            provider TEXT NOT NULL,
            environment TEXT NOT NULL,
            order_time_us INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            generation_id TEXT NOT NULL,
            source_object_id TEXT NOT NULL,
            record_ordinal INTEGER NOT NULL,
            event_id TEXT NOT NULL,
            role TEXT NOT NULL,
            tool_name TEXT,
            interaction_kind TEXT NOT NULL DEFAULT 'provider_system',
            title_eligible INTEGER NOT NULL DEFAULT 0,
            indexed_through INTEGER NOT NULL,
            event_count INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS searchable_text (
            source_event_id INTEGER PRIMARY KEY,
            content_text TEXT,
            tool_output_text TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_searchable_events_session
            ON searchable_events(session_id, generation_id, source_object_id);
        CREATE INDEX IF NOT EXISTS ix_searchable_events_window
            ON searchable_events(order_time_us);
        CREATE VIRTUAL TABLE IF NOT EXISTS searchable_fts USING fts5(
            content_text,
            tool_output_text,
            content='searchable_text',
            content_rowid='source_event_id',
            tokenize='unicode61 remove_diacritics 2'
        );
        -- Dropping a metadata row retires its text, which retires its FTS entry.
        -- Keeping the cascade in the schema means the four places that delete
        -- from searchable_events stay correct without knowing about the split.
        CREATE TRIGGER IF NOT EXISTS searchable_events_ad AFTER DELETE ON searchable_events BEGIN
            DELETE FROM searchable_text WHERE source_event_id = old.source_event_id;
        END;
        CREATE TRIGGER IF NOT EXISTS searchable_text_ai AFTER INSERT ON searchable_text BEGIN
            INSERT INTO searchable_fts(rowid, content_text, tool_output_text)
            VALUES (new.source_event_id, new.content_text, new.tool_output_text);
        END;
        CREATE TRIGGER IF NOT EXISTS searchable_text_ad AFTER DELETE ON searchable_text BEGIN
            INSERT INTO searchable_fts(searchable_fts, rowid, content_text, tool_output_text)
            VALUES ('delete', old.source_event_id, old.content_text, old.tool_output_text);
        END;
        CREATE TRIGGER IF NOT EXISTS searchable_text_au AFTER UPDATE ON searchable_text BEGIN
            INSERT INTO searchable_fts(searchable_fts, rowid, content_text, tool_output_text)
            VALUES ('delete', old.source_event_id, old.content_text, old.tool_output_text);
            INSERT INTO searchable_fts(rowid, content_text, tool_output_text)
            VALUES (new.source_event_id, new.content_text, new.tool_output_text);
        END;
        CREATE TABLE IF NOT EXISTS session_index (
            session_id TEXT PRIMARY KEY,
            generation_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            desired_revision INTEGER NOT NULL,
            indexed_through INTEGER NOT NULL,
            object_count INTEGER NOT NULL,
            object_set_hash TEXT NOT NULL,
            event_count INTEGER NOT NULL,
            user_messages INTEGER NOT NULL,
            assistant_messages INTEGER NOT NULL,
            tool_calls INTEGER NOT NULL,
            is_sidechain INTEGER NOT NULL,
            project TEXT,
            provider TEXT NOT NULL,
            environment TEXT NOT NULL,
            cwd TEXT,
            git_repo TEXT,
            started_at TEXT NOT NULL,
            published_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_session_index_owner_revision
            ON session_index(owner_id, indexed_through, session_id);
        CREATE TABLE IF NOT EXISTS episode_embeddings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            generation_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            episode_ordinal INTEGER NOT NULL,
            event_index_start INTEGER,
            event_index_end INTEGER,
            -- Position of the episode's first event in the published generation.
            -- The index columns above are clean-message ordinals produced by the
            -- embedding sanitizer and cannot be resolved back to a transcript
            -- position here; without this an episode match can never carry
            -- evidence.
            start_order_time_us INTEGER,
            model TEXT NOT NULL,
            dims INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            embedding BLOB NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(session_id, episode_ordinal, model)
        );
        CREATE INDEX IF NOT EXISTS ix_episode_embeddings_session
            ON episode_embeddings(session_id);
        CREATE TABLE IF NOT EXISTS embedding_publications (
            session_id TEXT NOT NULL,
            model TEXT NOT NULL,
            dims INTEGER NOT NULL,
            generation_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            expected_episode_count INTEGER NOT NULL,
            completed_at TEXT NOT NULL,
            PRIMARY KEY(session_id, model)
        );
        CREATE INDEX IF NOT EXISTS ix_embedding_publications_space
            ON embedding_publications(model, dims, generation_id, revision);
        """
    )
    _add_missing_episode_columns(connection)
    now = datetime.now(UTC).isoformat()
    existing = connection.execute("SELECT schema_version, schema_generation, store_id FROM search_meta WHERE singleton = 1").fetchone()
    if existing is None:
        connection.execute(
            """
            INSERT INTO search_meta(singleton, schema_version, schema_generation, store_id, updated_at)
            VALUES (1, ?, ?, ?, ?)
            """,
            (SCHEMA_VERSION, SCHEMA_GENERATION, str(uuid4()), now),
        )
    elif (existing["schema_version"], existing["schema_generation"]) != (SCHEMA_VERSION, SCHEMA_GENERATION):
        raise AssertionError("incompatible derived search store survived rebuild")


class SearchStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self._worklog_snapshots: dict[str, _WorklogSnapshot] = {}
        self._last_optimize_mono = 0.0

    def startup_maintenance(self) -> None:
        """Run SQLite's bounded planner refresh outside interactive requests."""

        self.connection.execute("DELETE FROM searchable_events WHERE order_time_us < ?", (_searchable_cutoff_us(),))
        self.connection.execute("PRAGMA optimize=0x10002")
        self._last_optimize_mono = time.monotonic()

    def prune_inactive_embedding_spaces(self, *, active_model: str) -> dict[str, int]:
        """Delete vectors that cannot be read by the single active space."""

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            vectors = self.connection.execute(
                "DELETE FROM episode_embeddings WHERE model != ?",
                (active_model,),
            ).rowcount
            publications = self.connection.execute(
                "DELETE FROM embedding_publications WHERE model != ?",
                (active_model,),
            ).rowcount
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        return {"vectors": max(0, vectors), "publications": max(0, publications)}

    def ping(self) -> dict[str, object]:
        row = self.connection.execute("SELECT COUNT(*) AS count FROM session_index").fetchone()
        metadata = self.connection.execute("SELECT store_id FROM search_meta WHERE singleton = 1").fetchone()
        return {
            "ready": True,
            "schema_version": SCHEMA_VERSION,
            "schema_generation": SCHEMA_GENERATION,
            "store_id": str(metadata["store_id"]),
            "published_sessions": int(row["count"]),
        }

    def index_object(
        self,
        *,
        session_id: str,
        generation_id: str,
        object_id: str,
        desired_revision: int,
        provider: str,
        machine_id: str,
        project: str | None,
        environment: str,
        cwd: str | None,
        git_repo: str | None,
        opaque_source_id: str,
        source_epoch: str,
        records: list[dict[str, Any]],
    ) -> dict[str, object]:
        now = datetime.now(UTC).isoformat()
        projection_hash = _object_projection_hash(
            session_id=session_id,
            generation_id=generation_id,
            object_id=object_id,
            provider=provider,
            machine_id=machine_id,
            opaque_source_id=opaque_source_id,
            source_epoch=source_epoch,
            records=records,
        )
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.connection.execute(
                """
                SELECT session_id, generation_id, projection_hash, event_count
                FROM indexed_objects WHERE object_id = ?
                """,
                (object_id,),
            ).fetchone()
            if existing is not None:
                exact = existing["projection_hash"] == projection_hash and int(existing["event_count"]) == len(records)
                if not exact:
                    same_empty_object = (
                        not records
                        and int(existing["event_count"]) == 0
                        and existing["session_id"] == session_id
                        and existing["generation_id"] == generation_id
                    )
                    stored_hash = self._stored_object_projection_hash(existing=existing, object_id=object_id)
                    if (not same_empty_object and stored_hash != projection_hash) or int(existing["event_count"]) != len(records):
                        raise ValueError("indexed object identity conflicts with existing derived rows")
                # Semantic classification is recoverable from a later raw
                # replay. It is deliberately excluded from the object
                # identity hash, so a control row can be reclassified without
                # duplicating the object or poisoning the derived index.
                self._update_existing_object_semantics(
                    session_id=session_id,
                    generation_id=generation_id,
                    object_id=object_id,
                    provider=provider,
                    records=records,
                )
                self.connection.execute(
                    "UPDATE indexed_objects SET projection_hash = ?, indexed_at = ? WHERE object_id = ?",
                    (projection_hash, now, object_id),
                )
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO projection_membership(
                        session_id, generation_id, desired_revision, object_id
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (session_id, generation_id, desired_revision, object_id),
                )
                self.connection.execute("COMMIT")
                return {
                    "created": False,
                    "exact_replay": True,
                    "identity_upgraded": not exact,
                    "event_count": len(records),
                }
            for record in records:
                preimage = "\0".join(
                    (
                        generation_id,
                        object_id,
                        str(record["event_id"]),
                        str(record["source_position"]),
                        str(record["event_subordinal"]),
                    )
                ).encode()
                event_key = hashlib.sha256(preimage).hexdigest()
                # searchd receives parser-owned semantic facts from the
                # storage projector. It has no complete raw-provider window,
                # so sequence-dependent controls must be resolved upstream;
                # this fallback only preserves the normalized fact (or the
                # ordinary-message default) while indexing.
                interaction = classify_provider_interaction(
                    provider,
                    role=str(record.get("role") or ""),
                    content_text=record.get("content_text"),
                    interaction_kind=record.get("interaction_kind"),
                    source_surface="provider_file",
                )
                self.connection.execute(
                    """
                    INSERT INTO events(
                        event_key, session_id, generation_id, source_object_id,
                        record_ordinal, event_id, order_time_us, opaque_source_id,
                        source_epoch, source_position, event_subordinal,
                        role, content_text, tool_name, tool_output_text,
                        tool_call_id, thread_id, branch_kind,
                        provider, interaction_kind, title_eligible,
                        machine_id, project, environment, cwd, git_repo
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_key,
                        session_id,
                        generation_id,
                        object_id,
                        record["record_ordinal"],
                        record["event_id"],
                        record["order_time_us"],
                        opaque_source_id,
                        source_epoch,
                        f"{record['source_position']:020d}",
                        record["event_subordinal"],
                        record["role"],
                        record.get("content_text"),
                        record.get("tool_name"),
                        record.get("tool_output_text"),
                        record.get("tool_call_id"),
                        record.get("thread_id"),
                        record.get("branch_kind"),
                        provider,
                        interaction["interaction_kind"],
                        1 if interaction["title_eligible"] else 0,
                        machine_id,
                        project,
                        environment,
                        cwd,
                        git_repo,
                    ),
                )
            self.connection.execute(
                """
                INSERT INTO indexed_objects(
                    object_id, session_id, generation_id,
                    event_count, projection_hash, indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (object_id, session_id, generation_id, len(records), projection_hash, now),
            )
            self.connection.execute(
                """
                INSERT INTO projection_membership(session_id, generation_id, desired_revision, object_id)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, generation_id, desired_revision, object_id),
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        return {"created": True, "exact_replay": False, "event_count": len(records)}

    def write_episode_embeddings(
        self,
        *,
        session_id: str,
        generation_id: str,
        model: str,
        dims: int,
        episodes: list[dict[str, Any]],
        owner_id: str = "",
        revision: int = 0,
        complete: bool = False,
        desired_episode_ordinals: list[int] | None = None,
    ) -> dict[str, object]:
        written = 0
        skipped = 0
        now = datetime.now(UTC).isoformat()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            published = self.connection.execute(
                "SELECT generation_id, desired_revision, owner_id FROM session_index WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if published is None:
                raise ValueError("embedding session is not published")
            if (
                str(published["generation_id"]) != generation_id
                or int(published["desired_revision"]) != revision
                or str(published["owner_id"]) != owner_id
            ):
                raise ValueError("embedding write does not match the published session identity")
            for episode in episodes:
                existing = self.connection.execute(
                    "SELECT content_hash, dims, revision, generation_id, owner_id, start_order_time_us"
                    " FROM episode_embeddings WHERE session_id = ? AND episode_ordinal = ? AND model = ?",
                    (session_id, episode["episode_ordinal"], model),
                ).fetchone()
                if existing is not None and int(existing["revision"]) > revision:
                    skipped += 1
                    continue
                if existing is not None and existing["content_hash"] == episode["content_hash"] and int(existing["dims"]) == dims:
                    # Unchanged text may reuse the vector bytes, but not the
                    # provenance. Skipping the whole row left the old
                    # generation, revision, owner and locator in place, so a
                    # republished session kept vectors pointing at a superseded
                    # generation -- they still ranked, then failed to hydrate,
                    # while the current generation's vectors were absent.
                    provenance_matches = (
                        str(existing["generation_id"]) == generation_id
                        and str(existing["owner_id"]) == owner_id
                        and existing["start_order_time_us"] == episode.get("start_order_time_us")
                    )
                    if provenance_matches:
                        skipped += 1
                        continue
                    self.connection.execute(
                        "UPDATE episode_embeddings SET generation_id = ?, revision = ?, owner_id = ?,"
                        " event_index_start = ?, event_index_end = ?, start_order_time_us = ?, updated_at = ?"
                        " WHERE session_id = ? AND episode_ordinal = ? AND model = ?",
                        (
                            generation_id,
                            revision,
                            owner_id,
                            episode["event_index_start"],
                            episode["event_index_end"],
                            episode.get("start_order_time_us"),
                            now,
                            session_id,
                            episode["episode_ordinal"],
                            model,
                        ),
                    )
                    written += 1
                    continue
                self.connection.execute(
                    """
                    INSERT INTO episode_embeddings(
                        session_id, owner_id, generation_id, revision, episode_ordinal, event_index_start, event_index_end,
                        start_order_time_us, model, dims, content_hash, embedding, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id, episode_ordinal, model) DO UPDATE SET
                        owner_id=excluded.owner_id, generation_id=excluded.generation_id, revision=excluded.revision,
                        event_index_start=excluded.event_index_start,
                        event_index_end=excluded.event_index_end,
                        start_order_time_us=excluded.start_order_time_us,
                        dims=excluded.dims,
                        content_hash=excluded.content_hash,
                        embedding=excluded.embedding,
                        updated_at=excluded.updated_at
                    """,
                    (
                        session_id,
                        owner_id,
                        generation_id,
                        revision,
                        episode["episode_ordinal"],
                        episode["event_index_start"],
                        episode["event_index_end"],
                        episode.get("start_order_time_us"),
                        model,
                        dims,
                        episode["content_hash"],
                        episode["embedding"],
                        now,
                    ),
                )
                written += 1
            if complete:
                # `desired_episode_ordinals` is the caller's full current chunk set,
                # not just the ordinals rewritten in this call -- a completion pass
                # can span multiple write_episode_embeddings calls, and chunks whose
                # hash already matched are never sent as `episodes` at all. Without
                # this, a "complete" write for a partial batch (or an unchanged
                # session sending zero episodes) would delete every episode not in
                # that one call, including still-current ones.
                ordinals = (
                    desired_episode_ordinals
                    if desired_episode_ordinals is not None
                    else [episode["episode_ordinal"] for episode in episodes]
                )
                suffix = f" AND episode_ordinal NOT IN ({','.join('?' for _ in ordinals)})" if ordinals else ""
                self.connection.execute(
                    f"DELETE FROM episode_embeddings WHERE session_id = ? AND model = ?{suffix}", (session_id, model, *ordinals)
                )
                self.connection.execute(
                    """
                    INSERT INTO embedding_publications(
                        session_id, model, dims, generation_id, revision,
                        expected_episode_count, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id, model) DO UPDATE SET
                        dims=excluded.dims,
                        generation_id=excluded.generation_id,
                        revision=excluded.revision,
                        expected_episode_count=excluded.expected_episode_count,
                        completed_at=excluded.completed_at
                    """,
                    (session_id, model, dims, generation_id, revision, len(ordinals), now),
                )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        return {"written": written, "skipped": skipped}

    def read_episode_embedding_hashes(self, *, session_id: str, model: str, dims: int | None = None) -> dict[str, object]:
        sql = "SELECT episode_ordinal, content_hash FROM episode_embeddings WHERE session_id = ? AND model = ?"
        params: tuple[object, ...] = (session_id, model)
        if dims is not None:
            sql += " AND dims = ?"
            params += (dims,)
        rows = self.connection.execute(sql, params).fetchall()
        published = self.connection.execute(
            "SELECT generation_id, desired_revision FROM session_index WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        # JSON object keys are strings; the projector converts them back to ordinals.
        return {
            "hashes": {str(row["episode_ordinal"]): str(row["content_hash"]) for row in rows},
            "published_generation_id": str(published["generation_id"]) if published is not None else None,
            "published_revision": str(published["desired_revision"]) if published is not None else None,
        }

    def read_embedding_source(
        self,
        *,
        session_id: str,
        expected_generation_id: str | None,
        expected_revision: int | None,
        offset: int,
        limit: int,
    ) -> dict[str, object]:
        """Read one fenced page of the semantic projection used for embedding."""

        published = self.connection.execute(
            "SELECT generation_id, desired_revision, owner_id, provider, event_count FROM session_index WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if published is None:
            return {"found": False}
        generation_id = str(published["generation_id"])
        revision = int(published["desired_revision"])
        if expected_generation_id is not None and expected_generation_id != generation_id:
            raise ValueError("embedding source generation changed during pagination")
        if expected_revision is not None and expected_revision != revision:
            raise ValueError("embedding source revision changed during pagination")
        rows = self.connection.execute(
            """
            SELECT e.order_time_us, e.machine_id, e.provider,
                   e.opaque_source_id, e.source_epoch, e.source_position,
                   e.event_subordinal, e.record_ordinal, e.role,
                   e.content_text, e.interaction_kind, e.tool_name,
                   e.tool_output_text
              FROM session_index s
              JOIN projection_membership m
                ON m.session_id = s.session_id
               AND m.generation_id = s.generation_id
               AND m.desired_revision = s.desired_revision
              JOIN events e
                ON e.session_id = m.session_id
               AND e.generation_id = m.generation_id
               AND e.source_object_id = m.object_id
             WHERE s.session_id = ?
             ORDER BY e.order_time_us, e.machine_id, e.provider,
                      e.opaque_source_id, e.source_epoch, e.source_position,
                      e.event_subordinal, e.record_ordinal
             LIMIT ? OFFSET ?
            """,
            (session_id, limit, offset),
        ).fetchall()
        total = int(published["event_count"])
        if offset + len(rows) < total and len(rows) < limit:
            raise ValueError("published embedding source event count is inconsistent")
        records: list[dict[str, object]] = []
        payload_bytes = 0
        for row in rows:
            record: dict[str, object] = {
                "timestamp": int(row["order_time_us"]),
                "machine_id": str(row["machine_id"]),
                "provider": str(row["provider"]),
                "opaque_source_id": str(row["opaque_source_id"]),
                "source_epoch": str(row["source_epoch"]),
                "source_position": int(row["source_position"]),
                "event_subordinal": int(row["event_subordinal"]),
                "role": str(row["role"]),
                "content_text": row["content_text"],
                "interaction_kind": str(row["interaction_kind"]),
                "tool_name": row["tool_name"],
                "tool_output_text": row["tool_output_text"],
            }
            # The protocol limit applies after JSON escaping. Measuring raw
            # UTF-8 undercounts quotes, backslashes, newlines and controls,
            # which can otherwise turn a valid database page into an
            # unframeable response that retries forever.
            record_bytes = 512 + len(json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode())
            if records and payload_bytes + record_bytes > _EMBEDDING_SOURCE_PAGE_BYTES:
                break
            records.append(record)
            payload_bytes += record_bytes
        if offset + len(records) > total or (not records and offset < total):
            raise ValueError("published embedding source event count is inconsistent")
        return {
            "found": True,
            "generation_id": generation_id,
            "revision": str(revision),
            "owner_id": str(published["owner_id"]),
            "provider": str(published["provider"]),
            "event_count": total,
            "records": records,
            "has_more": offset + len(records) < total,
        }

    def _update_existing_object_semantics(
        self,
        *,
        session_id: str,
        generation_id: str,
        object_id: str,
        provider: str,
        records: list[dict[str, Any]],
    ) -> None:
        """Apply a semantic correction while preserving immutable event rows."""

        for record in records:
            interaction = classify_provider_interaction(
                provider,
                role=str(record.get("role") or ""),
                content_text=record.get("content_text"),
                interaction_kind=record.get("interaction_kind"),
                source_surface="provider_file",
            )
            self.connection.execute(
                """
                UPDATE events
                   SET interaction_kind = ?, title_eligible = ?
                 WHERE session_id = ?
                   AND generation_id = ?
                   AND source_object_id = ?
                   AND record_ordinal = ?
                """,
                (
                    interaction["interaction_kind"],
                    1 if interaction["title_eligible"] else 0,
                    session_id,
                    generation_id,
                    object_id,
                    int(record["record_ordinal"]),
                ),
            )

    def _stored_object_projection_hash(self, *, existing: sqlite3.Row, object_id: str) -> str | None:
        session_id = str(existing["session_id"])
        generation_id = str(existing["generation_id"])
        event_count = int(existing["event_count"])
        rows = self.connection.execute(
            """
            SELECT event_id, record_ordinal, order_time_us, source_position,
                   event_subordinal, role, content_text, tool_name,
                   tool_output_text, tool_call_id, thread_id, branch_kind,
                   provider, interaction_kind, machine_id, opaque_source_id, source_epoch
            FROM events
            WHERE session_id = ? AND generation_id = ? AND source_object_id = ?
            ORDER BY record_ordinal ASC
            """,
            (session_id, generation_id, object_id),
        ).fetchall()
        if len(rows) != event_count:
            return None
        if not rows:
            return None
        first = rows[0]
        immutable_fields = ("provider", "machine_id", "opaque_source_id", "source_epoch")
        if any(any(row[field] != first[field] for field in immutable_fields) for row in rows[1:]):
            return None
        records = [
            {
                "event_id": str(row["event_id"]),
                "record_ordinal": int(row["record_ordinal"]),
                "order_time_us": int(row["order_time_us"]),
                "source_position": int(row["source_position"]),
                "event_subordinal": int(row["event_subordinal"]),
                "role": str(row["role"]),
                "interaction_kind": row["interaction_kind"],
                "content_text": row["content_text"],
                "tool_name": row["tool_name"],
                "tool_output_text": row["tool_output_text"],
                "tool_call_id": row["tool_call_id"],
                "thread_id": row["thread_id"],
                "branch_kind": row["branch_kind"],
            }
            for row in rows
        ]
        return _object_projection_hash(
            session_id=session_id,
            generation_id=generation_id,
            object_id=object_id,
            provider=str(first["provider"]),
            machine_id=str(first["machine_id"]),
            opaque_source_id=str(first["opaque_source_id"]),
            source_epoch=str(first["source_epoch"]),
            records=records,
        )

    def publish_generation(
        self,
        *,
        session_id: str,
        generation_id: str,
        owner_id: str,
        desired_revision: int,
        object_count: int,
        object_set_hash: str,
        event_count: int,
        project: str | None,
        provider: str,
        environment: str,
        cwd: str | None,
        git_repo: str | None,
        started_at: str,
    ) -> dict[str, object]:
        now = datetime.now(UTC).isoformat()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            objects = self.connection.execute(
                """
                SELECT o.object_id, o.event_count
                FROM projection_membership m
                JOIN indexed_objects o ON o.object_id = m.object_id
                WHERE m.session_id = ? AND m.generation_id = ? AND m.desired_revision = ?
                ORDER BY o.object_id ASC
                """,
                (session_id, generation_id, desired_revision),
            ).fetchall()
            indexed_event_count = sum(int(row["event_count"]) for row in objects)
            indexed_object_set_hash = _object_set_hash([str(row["object_id"]) for row in objects])
            if len(objects) != object_count or indexed_event_count != event_count or indexed_object_set_hash != object_set_hash:
                self.connection.execute("ROLLBACK")
                return {
                    "published": False,
                    "projection_lag": True,
                    "indexed_objects": len(objects),
                    "indexed_events": indexed_event_count,
                    "indexed_object_set_hash": indexed_object_set_hash,
                }
            aggregates = self.connection.execute(
                _PUBLISH_AGGREGATES_SQL,
                (session_id, generation_id, desired_revision, session_id, generation_id),
            ).fetchone()
            self.connection.execute(
                """
                INSERT INTO session_index(
                    session_id, generation_id, owner_id, desired_revision, indexed_through,
                    object_count, object_set_hash, event_count,
                    user_messages, assistant_messages, tool_calls, is_sidechain,
                    project, provider, environment, cwd, git_repo, started_at, published_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    generation_id=excluded.generation_id,
                    owner_id=excluded.owner_id,
                    desired_revision=excluded.desired_revision,
                    indexed_through=excluded.indexed_through,
                    object_count=excluded.object_count,
                    object_set_hash=excluded.object_set_hash,
                    event_count=excluded.event_count,
                    user_messages=excluded.user_messages,
                    assistant_messages=excluded.assistant_messages,
                    tool_calls=excluded.tool_calls,
                    is_sidechain=excluded.is_sidechain,
                    project=excluded.project,
                    provider=excluded.provider,
                    environment=excluded.environment,
                    cwd=excluded.cwd,
                    git_repo=excluded.git_repo,
                    started_at=excluded.started_at,
                    published_at=excluded.published_at
                """,
                (
                    session_id,
                    generation_id,
                    owner_id,
                    desired_revision,
                    desired_revision,
                    object_count,
                    object_set_hash,
                    event_count,
                    int(aggregates["user_messages"] or 0),
                    int(aggregates["assistant_messages"] or 0),
                    int(aggregates["tool_calls"] or 0),
                    int(aggregates["is_sidechain"] or 0),
                    project,
                    provider,
                    environment,
                    cwd,
                    git_repo,
                    started_at,
                    now,
                ),
            )
            self.connection.execute(
                """
                DELETE FROM projection_membership
                WHERE session_id = ? AND (generation_id != ? OR desired_revision != ?)
                """,
                (session_id, generation_id, desired_revision),
            )
            self.connection.execute(
                """
                DELETE FROM embedding_publications
                WHERE session_id = ? AND (generation_id != ? OR revision != ?)
                """,
                (session_id, generation_id, desired_revision),
            )
            self.connection.execute(
                """
                DELETE FROM events
                WHERE session_id = ? AND source_object_id NOT IN (
                    SELECT object_id FROM projection_membership WHERE session_id = ?
                )
                """,
                (session_id, session_id),
            )
            self.connection.execute(
                """
                DELETE FROM indexed_objects
                WHERE session_id = ? AND object_id NOT IN (
                    SELECT object_id FROM projection_membership WHERE session_id = ?
                )
                """,
                (session_id, session_id),
            )
            self._replace_searchable_session(
                session_id=session_id,
                generation_id=generation_id,
                desired_revision=desired_revision,
                owner_id=owner_id,
                project=project,
                provider=provider,
                environment=environment,
                event_count=event_count,
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        maintenance = self._maintain_after_publish()
        return {
            "published": True,
            "projection_lag": False,
            "indexed_through": str(desired_revision),
            "maintenance": maintenance,
        }

    def _replace_searchable_session(
        self,
        *,
        session_id: str,
        generation_id: str,
        desired_revision: int,
        owner_id: str,
        project: str | None,
        provider: str,
        environment: str,
        event_count: int,
    ) -> None:
        """Atomically replace one session's published, recent discovery corpus."""

        self.connection.execute("DELETE FROM searchable_events WHERE session_id = ?", (session_id,))
        self.connection.execute(
            """
            INSERT INTO searchable_events(
                source_event_id, owner_id, project, provider, environment,
                order_time_us, session_id, generation_id, source_object_id,
                record_ordinal, event_id, role, tool_name,
                interaction_kind, title_eligible,
                indexed_through, event_count
            )
            SELECT e.id, ?, ?, ?, ?,
                   e.order_time_us, e.session_id, e.generation_id, e.source_object_id,
                   e.record_ordinal, e.event_id, e.role, e.tool_name,
                   e.interaction_kind, e.title_eligible,
                   ?, ?
            FROM events e
            JOIN projection_membership m ON m.object_id = e.source_object_id
            WHERE m.session_id = ? AND m.generation_id = ? AND m.desired_revision = ?
              AND e.session_id = ? AND e.generation_id = ?
              AND e.order_time_us >= ?
            """,
            (
                owner_id,
                project,
                provider,
                environment,
                desired_revision,
                event_count,
                session_id,
                generation_id,
                desired_revision,
                session_id,
                generation_id,
                _searchable_cutoff_us(),
            ),
        )
        # Text follows the metadata it belongs to, in the same transaction. The
        # insert trigger on searchable_text is what populates the FTS index.
        self.connection.execute(
            """
            INSERT INTO searchable_text(source_event_id, content_text, tool_output_text)
            SELECT e.id, e.content_text, e.tool_output_text
            FROM events e
            JOIN searchable_events s ON s.source_event_id = e.id
            WHERE s.session_id = ? AND e.session_id = ?
            """,
            (session_id, session_id),
        )
        self.connection.execute("DELETE FROM searchable_events WHERE order_time_us < ?", (_searchable_cutoff_us(),))

    def search(
        self,
        *,
        owner_id: str,
        query: str,
        project: str | None,
        provider: str | None,
        environment: str | None,
        window_start_us: int | None,
        window_end_us: int | None,
        limit: int,
    ) -> dict[str, object]:
        fts_query, query_token_count, compiled_token_count = self._compile_fts_query(query)
        if not fts_query:
            return {"results": [], "query_token_count": query_token_count, "compiled_token_count": compiled_token_count}
        use_searchable_corpus = window_start_us is not None and window_start_us >= _fast_scope_cutoff_us()
        filter_params = (
            fts_query,
            owner_id,
            project,
            project,
            provider,
            provider,
            environment,
            environment,
            window_start_us,
            window_start_us,
            window_end_us,
            window_end_us,
        )
        if use_searchable_corpus:
            candidate_ceiling = max(limit, _CANDIDATE_CEILING)
            sql = _SEARCHABLE_SEARCH_SQL
            params = filter_params + (candidate_ceiling, limit, fts_query)
        else:
            candidate_ceiling = None
            sql = _ARCHIVE_SEARCH_SQL
            params = filter_params + (limit,)
        rows = self.connection.execute(sql, params).fetchall()

        # The bounded walk saturates only when more matches existed than it was
        # willing to look at. Short of that it saw the whole match set, so the
        # ranking is exactly what an unbounded scan would have produced. The
        # inner walk reports its own size, so this costs no extra query.
        ranking_scope = "exact"
        if candidate_ceiling is not None and rows:
            if int(rows[0]["candidate_count"]) >= candidate_ceiling:
                ranking_scope = "recent_bounded"
        return {
            "results": [{k: v for k, v in dict(row).items() if k != "candidate_count"} for row in rows],
            "query_token_count": query_token_count,
            "compiled_token_count": compiled_token_count,
            "search_scope": "published_recent" if use_searchable_corpus else "published_archive",
            "ranking_scope": ranking_scope,
        }

    def recall_context(
        self,
        *,
        owner_id: str,
        session_id: str,
        generation_id: str,
        search_event_id: int | None = None,
        start_order_time_us: int | None = None,
        context_turns: int,
    ) -> dict[str, object]:
        """Return bounded clean neighbor evidence from the hit's published generation.

        A hit is located either by its searchd event id (lexical) or by its
        position in the published ordering (semantic episodes, which know where
        they start but not which row that is).
        """

        if search_event_id is not None:
            target = self.connection.execute(
                _CONTEXT_TARGET_SQL,
                (search_event_id, session_id, generation_id, owner_id),
            ).fetchone()
        elif start_order_time_us is not None:
            target = self.connection.execute(
                _CONTEXT_TARGET_BY_POSITION_SQL,
                (session_id, generation_id, owner_id, start_order_time_us),
            ).fetchone()
        else:
            return {
                "evidence_status": "unavailable",
                "evidence_reason": "hit_missing_locator",
                "context": [],
                "total_events": 0,
            }
        if target is None:
            return {
                "evidence_status": "unavailable",
                "evidence_reason": "hit_not_published",
                "context": [],
                "total_events": 0,
            }
        total_events = int(target["event_count"])
        if context_turns == 0:
            return {"evidence_status": "complete", "evidence_reason": None, "context": [], "total_events": total_events}
        before_sql = _CONTEXT_ROWS_SQL.format(
            position_predicate="(e.order_time_us < ? OR (e.order_time_us = ? AND e.event_key <= ?))",
            direction="DESC",
        )
        after_sql = _CONTEXT_ROWS_SQL.format(
            position_predicate="(e.order_time_us > ? OR (e.order_time_us = ? AND e.event_key > ?))",
            direction="ASC",
        )
        position = (int(target["order_time_us"]), int(target["order_time_us"]), str(target["event_key"]))
        before = self.connection.execute(before_sql, (session_id, generation_id, owner_id, *position, context_turns + 1)).fetchall()
        after = self.connection.execute(after_sql, (session_id, generation_id, owner_id, *position, context_turns)).fetchall()
        context = [dict(row) for row in reversed(before)] + [dict(row) for row in after]
        return {
            "evidence_status": "complete",
            "evidence_reason": None,
            "context": context,
            "total_events": total_events,
        }

    def _compile_fts_query(self, raw: str) -> tuple[str, int, int]:
        """Normalize only syntax; every user-supplied search term remains required."""

        fts_query = _fts_query(raw)
        tokens = [token.casefold() for token in re.findall(r"\w+", raw, flags=re.UNICODE)]
        return fts_query, len(tokens), len(tokens)

    def _maintain_after_publish(self) -> dict[str, int]:
        checkpoint = self.connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        if time.monotonic() - self._last_optimize_mono >= 86_400:
            self.connection.execute("PRAGMA optimize")
            self._last_optimize_mono = time.monotonic()
        return {
            "checkpoint_busy": int(checkpoint[0]),
            "checkpoint_log_pages": int(checkpoint[1]),
            "checkpointed_pages": int(checkpoint[2]),
        }

    def worklog_day(
        self,
        *,
        owner_id: str,
        window_start_us: int,
        window_end_us: int,
        include_test: bool,
        section: str,
        snapshot_id: str | None,
        offset: int,
        limit: int,
    ) -> dict[str, object]:
        self._expire_worklog_snapshots()
        if snapshot_id is None:
            if offset != 0:
                raise WorklogSnapshotError("invalid_snapshot", "a new worklog snapshot must start at offset zero")
            if len(self._worklog_snapshots) >= _WORKLOG_SNAPSHOT_LIMIT:
                raise WorklogSnapshotError("snapshot_capacity", "too many worklog snapshots are active")
            snapshot_id = str(uuid4())
            self.connection.execute("BEGIN")
            try:
                snapshot = self._build_worklog_snapshot(
                    owner_id=owner_id,
                    window_start_us=window_start_us,
                    window_end_us=window_end_us,
                    include_test=include_test,
                )
                self.connection.execute("COMMIT")
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise
            self._worklog_snapshots[snapshot_id] = snapshot
        else:
            snapshot = self._worklog_snapshots.get(snapshot_id)
            if snapshot is None:
                raise WorklogSnapshotError("stale_snapshot", "worklog snapshot is missing or expired")
            if (
                snapshot.owner_id != owner_id
                or snapshot.window_start_us != window_start_us
                or snapshot.window_end_us != window_end_us
                or snapshot.include_test != include_test
            ):
                raise WorklogSnapshotError("snapshot_mismatch", "worklog snapshot does not match the request")
        items = snapshot.sessions if section == "sessions" else snapshot.events
        page = _bounded_snapshot_page(items, offset=offset, limit=limit)
        return {**page, "snapshot_id": snapshot_id}

    def release_worklog_snapshot(self, *, snapshot_id: str, owner_id: str) -> dict[str, object]:
        snapshot = self._worklog_snapshots.get(snapshot_id)
        if snapshot is None:
            return {"released": False}
        if snapshot.owner_id != owner_id:
            raise WorklogSnapshotError("snapshot_mismatch", "worklog snapshot does not belong to this owner")
        del self._worklog_snapshots[snapshot_id]
        return {"released": True}

    def _expire_worklog_snapshots(self) -> None:
        now = time.monotonic()
        for snapshot_id in [key for key, snapshot in self._worklog_snapshots.items() if snapshot.expires_mono <= now]:
            del self._worklog_snapshots[snapshot_id]

    def _build_worklog_snapshot(
        self,
        *,
        owner_id: str,
        window_start_us: int,
        window_end_us: int,
        include_test: bool,
    ) -> _WorklogSnapshot:
        sessions = self._collect_worklog_section(
            self._worklog_sessions,
            owner_id=owner_id,
            window_start_us=window_start_us,
            window_end_us=window_end_us,
            include_test=include_test,
        )
        events = self._collect_worklog_section(
            self._worklog_events,
            owner_id=owner_id,
            window_start_us=window_start_us,
            window_end_us=window_end_us,
            include_test=include_test,
        )
        total_bytes = sum(len(canonical_json(item).encode("utf-8")) for item in (*sessions, *events))
        if total_bytes > _WORKLOG_SNAPSHOT_BYTES:
            raise WorklogSnapshotError("export_too_large", "worklog snapshot exceeds the compatibility export budget")
        return _WorklogSnapshot(
            owner_id=owner_id,
            window_start_us=window_start_us,
            window_end_us=window_end_us,
            include_test=include_test,
            sessions=sessions,
            events=events,
            expires_mono=time.monotonic() + _WORKLOG_SNAPSHOT_TTL_SECONDS,
        )

    def _collect_worklog_section(self, query, **params) -> list[dict[str, Any]]:
        cursor: dict[str, Any] | None = None
        items: list[dict[str, Any]] = []
        for _page in range(_WORKLOG_SNAPSHOT_MAX_PAGES):
            page = query(cursor=cursor, limit=500, **params)
            page_items = page["items"]
            assert isinstance(page_items, list)
            items.extend(page_items)
            if page["has_more"] is not True:
                return items
            next_cursor = page["next_cursor"]
            if not isinstance(next_cursor, dict) or not page_items:
                raise WorklogSnapshotError("invalid_snapshot", "worklog snapshot cursor did not advance")
            cursor = next_cursor
        raise WorklogSnapshotError("export_too_large", "worklog snapshot contains too many records")

    def _worklog_sessions(
        self,
        *,
        owner_id: str,
        window_start_us: int,
        window_end_us: int,
        include_test: bool,
        cursor: dict[str, Any] | None,
        limit: int,
    ) -> dict[str, object]:
        first_order_us = cursor["first_order_us"] if cursor is not None else None
        cursor_started_at = cursor["started_at"] if cursor is not None else None
        cursor_session_id = cursor["session_id"] if cursor is not None else None
        rows = self.connection.execute(
            """
            WITH active AS (
                SELECT e.session_id,
                       MIN(e.order_time_us) AS first_event_us,
                       MAX(e.order_time_us) AS last_event_us,
                       MIN(CASE
                           WHEN e.role IN ('user', 'assistant') AND e.content_text IS NOT NULL
                                AND (e.role != 'user' OR (e.title_eligible = 1
                                     AND (e.interaction_kind IS NULL OR e.interaction_kind NOT IN ('local_control', 'local_control_output', 'conversation_boundary'))))
                           THEN e.order_time_us
                       END) AS first_message_us,
                       SUM(CASE
                           WHEN e.role IN ('user', 'assistant') AND e.content_text IS NOT NULL
                                AND (e.role != 'user' OR (e.title_eligible = 1
                                     AND (e.interaction_kind IS NULL OR e.interaction_kind NOT IN ('local_control', 'local_control_output', 'conversation_boundary'))))
                           THEN 1 ELSE 0
                       END) AS message_count,
                       COUNT(*) AS day_event_count
                FROM events e
                JOIN session_index s ON s.session_id = e.session_id AND s.generation_id = e.generation_id
                JOIN projection_membership m
                  ON m.session_id = e.session_id
                 AND m.generation_id = e.generation_id
                 AND m.desired_revision = s.indexed_through
                 AND m.object_id = e.source_object_id
                WHERE s.owner_id = ?
                  AND e.order_time_us >= ? AND e.order_time_us < ?
                  AND (? = 1 OR s.environment NOT IN ('test', 'e2e'))
                GROUP BY e.session_id
            )
            SELECT s.session_id, s.project, s.provider, s.cwd, s.git_repo, s.started_at,
                   s.user_messages, s.assistant_messages, s.tool_calls, s.is_sidechain,
                   s.indexed_through, active.first_event_us, active.last_event_us,
                   active.first_message_us, active.message_count, active.day_event_count,
                   COALESCE(active.first_message_us, active.first_event_us) AS first_order_us
            FROM active
            JOIN session_index s ON s.session_id = active.session_id
            WHERE (? IS NULL OR
                   (COALESCE(active.first_message_us, active.first_event_us), s.started_at, s.session_id)
                   > (?, ?, ?))
            ORDER BY first_order_us ASC, s.started_at ASC, s.session_id ASC
            LIMIT ?
            """,
            (
                owner_id,
                window_start_us,
                window_end_us,
                1 if include_test else 0,
                first_order_us,
                first_order_us,
                cursor_started_at,
                cursor_session_id,
                limit + 1,
            ),
        ).fetchall()
        return _bounded_worklog_page(rows, limit=limit, cursor_builder=_session_cursor)

    def _worklog_events(
        self,
        *,
        owner_id: str,
        window_start_us: int,
        window_end_us: int,
        include_test: bool,
        cursor: dict[str, Any] | None,
        limit: int,
    ) -> dict[str, object]:
        cursor_values = _event_cursor_values(cursor)
        rows = self.connection.execute(
            """
            SELECT e.session_id, e.role, e.content_text, e.order_time_us,
                   e.machine_id, e.provider, e.opaque_source_id, e.source_epoch,
                   e.source_position, e.event_subordinal, e.event_key,
                   s.indexed_through, s.generation_id
            FROM events e
            JOIN session_index s ON s.session_id = e.session_id AND s.generation_id = e.generation_id
            JOIN projection_membership m
              ON m.session_id = e.session_id
             AND m.generation_id = e.generation_id
             AND m.desired_revision = s.indexed_through
             AND m.object_id = e.source_object_id
            WHERE s.owner_id = ?
              AND e.order_time_us >= ? AND e.order_time_us < ?
              AND e.role IN ('user', 'assistant')
              AND e.content_text IS NOT NULL
              AND (e.role != 'user' OR (e.title_eligible = 1
                   AND (e.interaction_kind IS NULL OR e.interaction_kind NOT IN ('local_control', 'local_control_output', 'conversation_boundary'))))
              AND (? = 1 OR s.environment NOT IN ('test', 'e2e'))
              AND (? IS NULL OR
                   (e.session_id, e.order_time_us, e.machine_id, e.provider,
                    e.opaque_source_id, e.source_epoch, e.source_position,
                    e.event_subordinal, e.event_key)
                   > (?, ?, ?, ?, ?, ?, ?, ?, ?))
            ORDER BY e.session_id ASC, e.order_time_us ASC, e.machine_id ASC, e.provider ASC,
                     e.opaque_source_id ASC, e.source_epoch ASC,
                     e.source_position ASC, e.event_subordinal ASC, e.event_key ASC
            LIMIT ?
            """,
            (
                owner_id,
                window_start_us,
                window_end_us,
                1 if include_test else 0,
                cursor_values[0],
                *cursor_values,
                limit + 1,
            ),
        ).fetchall()
        normalized_rows = []
        for row in rows:
            item = dict(row)
            item["content_text"] = _bounded_worklog_content(str(item["content_text"]))
            normalized_rows.append(item)
        return _bounded_worklog_page(normalized_rows, limit=limit, cursor_builder=_event_cursor)

    def delete_session(self, *, session_id: str) -> dict[str, object]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute("DELETE FROM session_index WHERE session_id = ?", (session_id,))
            self.connection.execute("DELETE FROM searchable_events WHERE session_id = ?", (session_id,))
            self.connection.execute("DELETE FROM projection_membership WHERE session_id = ?", (session_id,))
            self.connection.execute("DELETE FROM events WHERE session_id = ?", (session_id,))
            self.connection.execute("DELETE FROM indexed_objects WHERE session_id = ?", (session_id,))
            self.connection.execute("DELETE FROM episode_embeddings WHERE session_id = ?", (session_id,))
            self.connection.execute("DELETE FROM embedding_publications WHERE session_id = ?", (session_id,))
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        return {"deleted": True}


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def object_set_hash(object_ids: list[str]) -> str:
    return _object_set_hash(object_ids)


def _object_set_hash(object_ids: list[str]) -> str:
    return hashlib.sha256(_OBJECT_SET_DOMAIN + "".join(sorted(object_ids)).encode("ascii")).hexdigest()


def _object_projection_hash(**value: object) -> str:
    """Hash immutable render payload, excluding recoverable semantic facts."""

    normalized = dict(value)
    records = normalized.get("records")
    if isinstance(records, list):
        normalized["records"] = [
            {key: field_value for key, field_value in record.items() if key != "interaction_kind"} if isinstance(record, dict) else record
            for record in records
        ]
    return hashlib.sha256(canonical_json(normalized).encode("utf-8")).hexdigest()


def _searchable_cutoff_us() -> int:
    return int((datetime.now(UTC) - timedelta(days=SEARCHABLE_RETENTION_DAYS)).timestamp() * 1_000_000)


def _fast_scope_cutoff_us() -> int:
    return int(
        (datetime.now(UTC) - timedelta(days=SEARCHABLE_FAST_WINDOW_DAYS, seconds=SEARCHABLE_FAST_WINDOW_MARGIN_SECONDS)).timestamp()
        * 1_000_000
    )


def _fts_query(raw: str) -> str:
    tokens = re.findall(r"\w+", raw, flags=re.UNICODE)
    if not tokens:
        return ""
    stripped = raw.strip()
    explicitly_quoted = len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {'"', "'"}
    compact_identifier = not any(character.isspace() for character in stripped)
    normalized = " ".join(tokens)
    if len(tokens) > 1 and (explicitly_quoted or compact_identifier):
        return f'"{normalized}"'
    return normalized


def _bounded_worklog_content(value: str) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= _WORKLOG_EVENT_CONTENT_BYTES:
        return value
    marker = _WORKLOG_TRUNCATION_MARKER.encode("utf-8")
    prefix = encoded[: _WORKLOG_EVENT_CONTENT_BYTES - len(marker)].decode("utf-8", "ignore")
    return prefix + _WORKLOG_TRUNCATION_MARKER


def _bounded_worklog_page(rows: list[sqlite3.Row | dict[str, Any]], *, limit: int, cursor_builder) -> dict[str, object]:
    items: list[dict[str, Any]] = []
    encoded_bytes = 0
    for row in rows[:limit]:
        item = dict(row)
        item_bytes = len(canonical_json(item).encode("utf-8"))
        if encoded_bytes + item_bytes > _WORKLOG_PAGE_BYTES:
            if not items:
                raise WorklogPageTooLarge("one normalized worklog record exceeds the RPC page budget")
            break
        items.append(item)
        encoded_bytes += item_bytes
    has_more = len(items) < len(rows)
    return {
        "items": items,
        "has_more": has_more,
        "next_cursor": cursor_builder(items[-1]) if has_more and items else None,
        "page_bytes": encoded_bytes,
    }


def _bounded_snapshot_page(items: list[dict[str, Any]], *, offset: int, limit: int) -> dict[str, object]:
    if not 0 <= offset <= len(items):
        raise WorklogSnapshotError("invalid_snapshot", "worklog snapshot offset is invalid")
    page: list[dict[str, Any]] = []
    encoded_bytes = 0
    for item in items[offset : offset + limit]:
        item_bytes = len(canonical_json(item).encode("utf-8"))
        if encoded_bytes + item_bytes > _WORKLOG_PAGE_BYTES:
            if not page:
                raise WorklogPageTooLarge("one normalized worklog record exceeds the RPC page budget")
            break
        page.append(item)
        encoded_bytes += item_bytes
    next_offset = offset + len(page)
    return {
        "items": page,
        "has_more": next_offset < len(items),
        "next_offset": next_offset if next_offset < len(items) else None,
        "page_bytes": encoded_bytes,
    }


def _session_cursor(row: dict[str, Any]) -> dict[str, object]:
    return {
        "first_order_us": int(row["first_order_us"]),
        "started_at": str(row["started_at"]),
        "session_id": str(row["session_id"]),
    }


def _event_cursor(row: dict[str, Any]) -> dict[str, object]:
    return {
        "session_id": str(row["session_id"]),
        "order_time_us": int(row["order_time_us"]),
        "machine_id": str(row["machine_id"]),
        "provider": str(row["provider"]),
        "opaque_source_id": str(row["opaque_source_id"]),
        "source_epoch": str(row["source_epoch"]),
        "source_position": str(row["source_position"]),
        "event_subordinal": int(row["event_subordinal"]),
        "event_key": str(row["event_key"]),
    }


def _event_cursor_values(cursor: dict[str, Any] | None) -> tuple[object, ...]:
    if cursor is None:
        return (None,) * 9
    return (
        cursor["session_id"],
        cursor["order_time_us"],
        cursor["machine_id"],
        cursor["provider"],
        cursor["opaque_source_id"],
        cursor["source_epoch"],
        cursor["source_position"],
        cursor["event_subordinal"],
        cursor["event_key"],
    )


__all__ = [
    "SCHEMA_GENERATION",
    "SCHEMA_VERSION",
    "SearchStore",
    "WorklogPageTooLarge",
    "WorklogSnapshotError",
    "canonical_json",
    "object_set_hash",
    "open_search_database",
]
