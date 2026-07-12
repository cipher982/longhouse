"""SQLite-backed derived index operations executed only inside searchd."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SCHEMA_GENERATION = "searchd-v1-generation-qualified-locators"
_OBJECT_SET_DOMAIN = b"longhouse-search-object-set-v1\0"


def open_search_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5.0, isolation_level=None, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS search_meta (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            schema_version INTEGER NOT NULL,
            schema_generation TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS indexed_objects (
            object_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            generation_id TEXT NOT NULL,
            desired_revision INTEGER NOT NULL,
            event_count INTEGER NOT NULL,
            projection_hash TEXT NOT NULL,
            indexed_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_indexed_objects_generation
            ON indexed_objects(session_id, generation_id, object_id);
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
    existing = connection.execute("SELECT schema_version, schema_generation FROM search_meta WHERE singleton = 1").fetchone()
    if existing is None:
        connection.execute(
            "INSERT INTO search_meta(singleton, schema_version, schema_generation, updated_at) VALUES (1, ?, ?, ?)",
            (SCHEMA_VERSION, SCHEMA_GENERATION, now),
        )
    elif (existing["schema_version"], existing["schema_generation"]) != (SCHEMA_VERSION, SCHEMA_GENERATION):
        raise RuntimeError("searchd schema is incompatible with this build")
    return connection


class SearchStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def ping(self) -> dict[str, object]:
        row = self.connection.execute("SELECT COUNT(*) AS count FROM session_index").fetchone()
        return {
            "ready": True,
            "schema_version": SCHEMA_VERSION,
            "schema_generation": SCHEMA_GENERATION,
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
            desired_revision=desired_revision,
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
                        tool_call_id, provider, machine_id, project, environment, cwd, git_repo
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    object_id, session_id, generation_id, desired_revision,
                    event_count, projection_hash, indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (object_id, session_id, generation_id, desired_revision, len(records), projection_hash, now),
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
                SELECT object_id, event_count FROM indexed_objects
                WHERE session_id = ? AND generation_id = ? AND desired_revision = ?
                ORDER BY object_id ASC
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
            self.connection.execute(
                """
                INSERT INTO session_index(
                    session_id, generation_id, owner_id, desired_revision, indexed_through,
                    object_count, object_set_hash, event_count, project, provider, environment, cwd, git_repo,
                    started_at, published_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    generation_id=excluded.generation_id,
                    owner_id=excluded.owner_id,
                    desired_revision=excluded.desired_revision,
                    indexed_through=excluded.indexed_through,
                    object_count=excluded.object_count,
                    object_set_hash=excluded.object_set_hash,
                    event_count=excluded.event_count,
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
                "DELETE FROM events WHERE session_id = ? AND generation_id != ?",
                (session_id, generation_id),
            )
            self.connection.execute(
                "DELETE FROM indexed_objects WHERE session_id = ? AND generation_id != ?",
                (session_id, generation_id),
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        return {"published": True, "projection_lag": False, "indexed_through": str(desired_revision)}

    def search(self, *, owner_id: str, query: str, limit: int) -> dict[str, object]:
        fts_query = _fts_query(query)
        if not fts_query:
            return {"results": []}
        rows = self.connection.execute(
            """
            SELECT e.session_id, e.generation_id, e.source_object_id,
                   e.record_ordinal, e.event_id, e.order_time_us,
                   e.role, e.content_text, e.tool_name, e.tool_output_text,
                   s.project, s.provider, s.environment, s.indexed_through,
                   bm25(events_fts) AS rank
            FROM events_fts
            JOIN events e ON e.id = events_fts.rowid
            JOIN session_index s ON s.session_id = e.session_id AND s.generation_id = e.generation_id
            WHERE events_fts MATCH ? AND s.owner_id = ?
            ORDER BY rank ASC, e.order_time_us DESC, e.event_key ASC
            LIMIT ?
            """,
            (fts_query, owner_id, limit),
        ).fetchall()
        return {"results": [dict(row) for row in rows]}

    def worklog_day(
        self,
        *,
        owner_id: str,
        window_start_us: int,
        window_end_us: int,
        include_test: bool,
        limit: int,
    ) -> dict[str, object]:
        rows = self.connection.execute(
            """
            SELECT e.session_id, e.role, e.content_text, e.order_time_us,
                   s.project, s.provider, s.environment, s.cwd, s.git_repo,
                   s.started_at, s.indexed_through, s.generation_id
            FROM events e
            JOIN session_index s ON s.session_id = e.session_id AND s.generation_id = e.generation_id
            WHERE s.owner_id = ?
              AND e.order_time_us >= ? AND e.order_time_us < ?
              AND e.role IN ('user', 'assistant')
              AND e.content_text IS NOT NULL
              AND (? = 1 OR s.environment NOT IN ('test', 'e2e'))
            ORDER BY e.order_time_us ASC, e.machine_id ASC, e.provider ASC,
                     e.opaque_source_id ASC, e.source_epoch ASC,
                     e.source_position ASC, e.event_subordinal ASC
            LIMIT ?
            """,
            (owner_id, window_start_us, window_end_us, 1 if include_test else 0, limit + 1),
        ).fetchall()
        truncated = len(rows) > limit
        return {"events": [dict(row) for row in rows[:limit]], "truncated": truncated}

    def delete_session(self, *, session_id: str) -> dict[str, object]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute("DELETE FROM session_index WHERE session_id = ?", (session_id,))
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


__all__ = [
    "SCHEMA_GENERATION",
    "SCHEMA_VERSION",
    "SearchStore",
    "canonical_json",
    "object_set_hash",
    "open_search_database",
]
