"""Dedicated recall retrieval index stored outside the hot runtime DB."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RetrievalChunk:
    """A projected recall chunk ready to store in retrieval.db."""

    chunk_uid: str
    session_id: str
    chunk_index: int
    chunk_kind: str
    retrieval_role: str
    event_index_start: int
    event_index_end: int
    content: str
    parent_chunk_uid: str | None = None
    parent_session_id: str | None = None
    thread_id: str | None = None
    parent_thread_id: str | None = None
    first_event_id: int | None = None
    last_event_id: int | None = None
    provider: str | None = None
    project: str | None = None
    environment: str | None = None
    device_id: str | None = None
    cwd: str | None = None
    git_repo: str | None = None
    git_branch: str | None = None
    started_at: str | None = None
    last_activity_at: str | None = None
    intent_text: str | None = None
    evidence_text: str | None = None
    structured_text: str | None = None
    content_hash: str | None = None
    token_count: int = 0
    transcript_revision: int = 0
    stale: int = 0


@dataclass(frozen=True)
class RetrievalHit:
    """A lexical recall hit from retrieval.db."""

    chunk_id: int
    chunk_uid: str
    session_id: str
    parent_chunk_id: int | None
    chunk_index: int
    chunk_kind: str
    score: float
    event_index_start: int
    event_index_end: int
    first_event_id: int | None
    last_event_id: int | None
    content: str
    intent_text: str | None
    evidence_text: str | None
    structured_text: str | None


def _strip_env_quotes(value: str) -> str:
    value = value.strip()
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1].strip()
    return value


def _sqlite_path_from_url(database_url: str) -> Path | None:
    database_url = _strip_env_quotes(database_url)
    if not database_url:
        return None
    parsed = urlparse(database_url)
    if not parsed.scheme.startswith("sqlite"):
        return None
    raw_path = parsed.path or ""
    if not raw_path or raw_path in {":memory:", "/:memory:"}:
        return None
    if database_url.startswith(f"{parsed.scheme}:////"):
        return Path("/" + raw_path.lstrip("/"))
    if database_url.startswith(f"{parsed.scheme}:///"):
        return Path(raw_path.lstrip("/"))
    return None


def resolve_retrieval_db_path(database_url: str) -> Path | None:
    """Return the retrieval.db path for a file-backed SQLite archive DB."""

    explicit = _strip_env_quotes(os.getenv("LONGHOUSE_RETRIEVAL_DB_PATH") or "")
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_absolute() else path.resolve()
    archive_path = _sqlite_path_from_url(database_url)
    if archive_path is None:
        return None
    return archive_path.expanduser().resolve().parent / "retrieval.db"


def connect_retrieval_db(path: Path) -> sqlite3.Connection:
    """Open a retrieval.db connection with row dictionaries and WAL pragmas."""

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def initialize_retrieval_db(conn: sqlite3.Connection) -> None:
    """Create retrieval index schema if needed."""

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS recall_chunks (
          id INTEGER PRIMARY KEY,
          chunk_uid TEXT NOT NULL UNIQUE,
          session_id TEXT NOT NULL,
          parent_session_id TEXT,
          thread_id TEXT,
          parent_thread_id TEXT,
          parent_chunk_id INTEGER,
          chunk_index INTEGER NOT NULL,
          chunk_kind TEXT NOT NULL,
          retrieval_role TEXT NOT NULL DEFAULT 'child'
            CHECK (retrieval_role IN ('child', 'parent')),

          event_index_start INTEGER NOT NULL,
          event_index_end INTEGER NOT NULL,
          first_event_id INTEGER,
          last_event_id INTEGER,

          provider TEXT,
          project TEXT,
          environment TEXT,
          device_id TEXT,
          cwd TEXT,
          git_repo TEXT,
          git_branch TEXT,
          started_at TEXT,
          last_activity_at TEXT,

          content TEXT NOT NULL,
          intent_text TEXT,
          evidence_text TEXT,
          structured_text TEXT,
          content_hash TEXT NOT NULL,
          token_count INTEGER NOT NULL DEFAULT 0,

          transcript_revision INTEGER NOT NULL DEFAULT 0,
          indexed_at TEXT NOT NULL,
          stale INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS ix_recall_chunks_session
          ON recall_chunks(session_id, chunk_index);
        CREATE INDEX IF NOT EXISTS ix_recall_chunks_parent
          ON recall_chunks(parent_chunk_id);
        CREATE INDEX IF NOT EXISTS ix_recall_chunks_role_time
          ON recall_chunks(retrieval_role, started_at, id);
        CREATE INDEX IF NOT EXISTS ix_recall_chunks_project_time
          ON recall_chunks(project, started_at, id);
        CREATE INDEX IF NOT EXISTS ix_recall_chunks_provider_time
          ON recall_chunks(provider, started_at, id);
        CREATE INDEX IF NOT EXISTS ix_recall_chunks_env_time
          ON recall_chunks(environment, started_at, id);
        CREATE INDEX IF NOT EXISTS ix_recall_chunks_hash
          ON recall_chunks(content_hash);

        CREATE VIRTUAL TABLE IF NOT EXISTS recall_chunks_fts USING fts5(
          content,
          intent_text,
          evidence_text,
          structured_text,
          cwd,
          git_repo,
          git_branch,
          tokenize='unicode61 tokenchars ''._/-:'''
        );

        CREATE TABLE IF NOT EXISTS recall_index_state (
          key TEXT PRIMARY KEY,
          value_json TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        """
    )
    set_index_state(conn, "schema_version", {"version": SCHEMA_VERSION})
    conn.commit()


def set_index_state(conn: sqlite3.Connection, key: str, value: dict) -> None:
    """Write a small JSON state value."""

    conn.execute(
        """
        INSERT INTO recall_index_state(key, value_json, updated_at)
        VALUES(:key, :value_json, :updated_at)
        ON CONFLICT(key) DO UPDATE SET
          value_json = excluded.value_json,
          updated_at = excluded.updated_at
        """,
        {
            "key": key,
            "value_json": json.dumps(value, sort_keys=True),
            "updated_at": _utc_now_iso(),
        },
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _token_count(text: str) -> int:
    return max(1, len(text) // 3) if text else 0


def _delete_fts_rows(conn: sqlite3.Connection, row_ids: Iterable[int]) -> None:
    for row_id in row_ids:
        conn.execute("DELETE FROM recall_chunks_fts WHERE rowid = ?", (row_id,))


def replace_session_chunks(conn: sqlite3.Connection, session_id: str, chunks: list[RetrievalChunk]) -> int:
    """Replace all projected chunks for one session and keep FTS in sync."""

    existing_ids = [
        int(row["id"])
        for row in conn.execute(
            "SELECT id FROM recall_chunks WHERE session_id = ? AND retrieval_role = 'child'",
            (session_id,),
        ).fetchall()
    ]
    with conn:
        _delete_fts_rows(conn, existing_ids)
        conn.execute("DELETE FROM recall_chunks WHERE session_id = ?", (session_id,))

        uid_to_id: dict[str, int] = {}
        sorted_chunks = sorted(chunks, key=lambda chunk: 0 if chunk.retrieval_role == "parent" else 1)
        for chunk in sorted_chunks:
            parent_chunk_id = uid_to_id.get(chunk.parent_chunk_uid or "")
            content_hash = chunk.content_hash or _content_hash(chunk.content)
            token_count = chunk.token_count or _token_count(chunk.content)
            cursor = conn.execute(
                """
                INSERT INTO recall_chunks(
                  chunk_uid, session_id, parent_session_id, thread_id, parent_thread_id,
                  parent_chunk_id, chunk_index, chunk_kind, retrieval_role,
                  event_index_start, event_index_end, first_event_id, last_event_id,
                  provider, project, environment, device_id, cwd, git_repo, git_branch,
                  started_at, last_activity_at, content, intent_text, evidence_text,
                  structured_text, content_hash, token_count, transcript_revision,
                  indexed_at, stale
                )
                VALUES(
                  :chunk_uid, :session_id, :parent_session_id, :thread_id, :parent_thread_id,
                  :parent_chunk_id, :chunk_index, :chunk_kind, :retrieval_role,
                  :event_index_start, :event_index_end, :first_event_id, :last_event_id,
                  :provider, :project, :environment, :device_id, :cwd, :git_repo, :git_branch,
                  :started_at, :last_activity_at, :content, :intent_text, :evidence_text,
                  :structured_text, :content_hash, :token_count, :transcript_revision,
                  :indexed_at, :stale
                )
                """,
                {
                    **chunk.__dict__,
                    "parent_chunk_id": parent_chunk_id,
                    "content_hash": content_hash,
                    "token_count": token_count,
                    "indexed_at": _utc_now_iso(),
                },
            )
            chunk_id = int(cursor.lastrowid)
            uid_to_id[chunk.chunk_uid] = chunk_id
            if chunk.retrieval_role == "child":
                conn.execute(
                    """
                    INSERT INTO recall_chunks_fts(
                      rowid, content, intent_text, evidence_text, structured_text,
                      cwd, git_repo, git_branch
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk_id,
                        chunk.content,
                        chunk.intent_text,
                        chunk.evidence_text,
                        chunk.structured_text,
                        chunk.cwd,
                        chunk.git_repo,
                        chunk.git_branch,
                    ),
                )
    return len(chunks)


def rebuild_fts(conn: sqlite3.Connection) -> None:
    """Rebuild FTS rows from child chunks."""

    with conn:
        conn.execute("DELETE FROM recall_chunks_fts")
        conn.execute(
            """
            INSERT INTO recall_chunks_fts(
              rowid, content, intent_text, evidence_text, structured_text,
              cwd, git_repo, git_branch
            )
            SELECT id, content, intent_text, evidence_text, structured_text,
                   cwd, git_repo, git_branch
            FROM recall_chunks
            WHERE retrieval_role = 'child'
            """
        )


def check_fts_integrity(conn: sqlite3.Connection) -> bool:
    """Return True when every child chunk has one FTS row."""

    child_count = int(conn.execute("SELECT count(*) FROM recall_chunks WHERE retrieval_role = 'child'").fetchone()[0])
    fts_count = int(conn.execute("SELECT count(*) FROM recall_chunks_fts").fetchone()[0])
    ok = child_count == fts_count
    set_index_state(
        conn,
        "last_integrity_check",
        {
            "ok": ok,
            "child_count": child_count,
            "fts_count": fts_count,
        },
    )
    conn.commit()
    return ok


def build_fts_query(raw: str) -> str:
    """Build a safe FTS5 AND query while preserving code-shaped tokens."""

    terms = []
    for term in re.split(r"\s+", (raw or "").strip()):
        cleaned = term.strip()
        if not cleaned:
            continue
        if not any(ch.isalnum() for ch in cleaned):
            continue
        terms.append(f'"{cleaned.replace(chr(34), chr(34) + chr(34))}"')
    return " ".join(terms)


def search_lexical_chunks(
    conn: sqlite3.Connection,
    query: str,
    *,
    project: str | None = None,
    provider: str | None = None,
    environment: str | None = None,
    since: str | None = None,
    limit: int = 5,
    inner_limit: int | None = None,
) -> list[RetrievalHit]:
    """Search child chunks with FTS5 and return ranked hits."""

    fts_query = build_fts_query(query)
    if not fts_query:
        return []
    max_rows = inner_limit if inner_limit is not None else max(limit * 20, 100)
    rows = conn.execute(
        """
        SELECT
          c.id,
          c.chunk_uid,
          c.session_id,
          c.parent_chunk_id,
          c.chunk_index,
          c.chunk_kind,
          bm25(recall_chunks_fts) AS score,
          c.event_index_start,
          c.event_index_end,
          c.first_event_id,
          c.last_event_id,
          c.content,
          c.intent_text,
          c.evidence_text,
          c.structured_text
        FROM recall_chunks_fts f
        JOIN recall_chunks c ON c.id = f.rowid
        WHERE recall_chunks_fts MATCH :fts_query
          AND c.retrieval_role = 'child'
          AND (:project IS NULL OR c.project = :project)
          AND (:provider IS NULL OR c.provider = :provider)
          AND (:environment IS NULL OR c.environment = :environment)
          AND (:since IS NULL OR c.started_at >= :since)
        ORDER BY score ASC
        LIMIT :limit
        """,
        {
            "fts_query": fts_query,
            "project": project,
            "provider": provider,
            "environment": environment,
            "since": since,
            "limit": max_rows,
        },
    ).fetchall()
    hits = [
        RetrievalHit(
            chunk_id=int(row["id"]),
            chunk_uid=str(row["chunk_uid"]),
            session_id=str(row["session_id"]),
            parent_chunk_id=int(row["parent_chunk_id"]) if row["parent_chunk_id"] is not None else None,
            chunk_index=int(row["chunk_index"]),
            chunk_kind=str(row["chunk_kind"]),
            score=float(row["score"]),
            event_index_start=int(row["event_index_start"]),
            event_index_end=int(row["event_index_end"]),
            first_event_id=int(row["first_event_id"]) if row["first_event_id"] is not None else None,
            last_event_id=int(row["last_event_id"]) if row["last_event_id"] is not None else None,
            content=str(row["content"]),
            intent_text=row["intent_text"],
            evidence_text=row["evidence_text"],
            structured_text=row["structured_text"],
        )
        for row in rows
    ]
    return _diversify_hits_by_session(hits, limit=limit)


def _diversify_hits_by_session(hits: list[RetrievalHit], *, limit: int) -> list[RetrievalHit]:
    seen: set[str] = set()
    diversified: list[RetrievalHit] = []
    for hit in hits:
        if hit.session_id in seen:
            continue
        seen.add(hit.session_id)
        diversified.append(hit)
        if len(diversified) >= limit:
            break
    return diversified
