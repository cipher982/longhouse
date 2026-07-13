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
from pathlib import Path
from typing import Any
from uuid import UUID
from uuid import uuid4

SCHEMA_VERSION = 1
SCHEMA_GENERATION = "searchd-v1-frozen-worklog-snapshots"
_OBJECT_SET_DOMAIN = b"longhouse-search-object-set-v1\0"
_WORKLOG_PAGE_BYTES = 700_000
_WORKLOG_SNAPSHOT_BYTES = 64 * 1024 * 1024
_WORKLOG_SNAPSHOT_MAX_PAGES = 200
_WORKLOG_SNAPSHOT_TTL_SECONDS = 120.0
_WORKLOG_SNAPSHOT_LIMIT = 8

_PUBLISH_AGGREGATES_SQL = """
    SELECT
        SUM(CASE WHEN e.role = 'user' THEN 1 ELSE 0 END) AS user_messages,
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
        """
    )
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
        projection_hash = _projection_hash(
            session_id=session_id,
            generation_id=generation_id,
            object_id=object_id,
            provider=provider,
            machine_id=machine_id,
            project=project,
            environment=environment,
            cwd=cwd,
            git_repo=git_repo,
            opaque_source_id=opaque_source_id,
            source_epoch=source_epoch,
            records=records,
        )
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.connection.execute(
                "SELECT projection_hash, event_count FROM indexed_objects WHERE object_id = ?",
                (object_id,),
            ).fetchone()
            if existing is not None:
                exact = existing["projection_hash"] == projection_hash and int(existing["event_count"]) == len(records)
                if not exact:
                    raise ValueError("indexed object identity conflicts with existing derived rows")
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO projection_membership(
                        session_id, generation_id, desired_revision, object_id
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (session_id, generation_id, desired_revision, object_id),
                )
                self.connection.execute("COMMIT")
                return {"created": False, "exact_replay": True, "event_count": len(records)}
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
                self.connection.execute(
                    """
                    INSERT INTO events(
                        event_key, session_id, generation_id, source_object_id,
                        record_ordinal, event_id, order_time_us, opaque_source_id,
                        source_epoch, source_position, event_subordinal,
                        role, content_text, tool_name, tool_output_text,
                        tool_call_id, thread_id, branch_kind,
                        provider, machine_id, project, environment, cwd, git_repo
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        return {"published": True, "projection_lag": False, "indexed_through": str(desired_revision)}

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
        fts_query = _fts_query(query)
        if not fts_query:
            return {"results": []}
        rows = self.connection.execute(
            """
            SELECT e.session_id, e.generation_id, e.source_object_id,
                   e.record_ordinal, e.event_id, e.order_time_us,
                   e.role, e.tool_name,
                   snippet(events_fts, 0, '', '', ' … ', 24) AS content_snippet,
                   snippet(events_fts, 1, '', '', ' … ', 24) AS tool_output_snippet,
                   s.project, s.provider, s.environment, s.indexed_through,
                   bm25(events_fts) AS rank
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
            ORDER BY rank ASC, e.order_time_us DESC, e.event_key ASC
            LIMIT ?
            """,
            (
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
                limit,
            ),
        ).fetchall()
        return {"results": [dict(row) for row in rows]}

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
                           THEN e.order_time_us
                       END) AS first_message_us,
                       SUM(CASE
                           WHEN e.role IN ('user', 'assistant') AND e.content_text IS NOT NULL
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
        return _bounded_worklog_page(rows, limit=limit, cursor_builder=_event_cursor)

    def delete_session(self, *, session_id: str) -> dict[str, object]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute("DELETE FROM session_index WHERE session_id = ?", (session_id,))
            self.connection.execute("DELETE FROM projection_membership WHERE session_id = ?", (session_id,))
            self.connection.execute("DELETE FROM events WHERE session_id = ?", (session_id,))
            self.connection.execute("DELETE FROM indexed_objects WHERE session_id = ?", (session_id,))
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


def _projection_hash(**value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _fts_query(raw: str) -> str:
    normalized = re.sub(r"[^\w\s]+", " ", raw, flags=re.UNICODE)
    return re.sub(r"\s+", " ", normalized).strip()


def _bounded_worklog_page(rows: list[sqlite3.Row], *, limit: int, cursor_builder) -> dict[str, object]:
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
