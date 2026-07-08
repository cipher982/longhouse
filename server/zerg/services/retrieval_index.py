"""Dedicated recall retrieval index stored outside the hot runtime DB."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from zerg.services.session_processing.embeddings import CleanTranscriptEvent
from zerg.services.session_processing.embeddings import iter_clean_transcript_events
from zerg.services.session_processing.tokens import truncate

SCHEMA_VERSION = 1
CHILD_TOKEN_BUDGET = 500
PARENT_TOKEN_BUDGET = 2500


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


@dataclass(frozen=True)
class StoredRetrievalChunk:
    """A stored recall chunk hydrated from retrieval.db."""

    chunk_id: int
    chunk_uid: str
    session_id: str
    parent_chunk_id: int | None
    chunk_index: int
    chunk_kind: str
    retrieval_role: str
    event_index_start: int
    event_index_end: int
    first_event_id: int | None
    last_event_id: int | None
    content: str
    intent_text: str | None
    evidence_text: str | None
    structured_text: str | None


@dataclass(frozen=True)
class _Trace:
    index: int
    events: list[CleanTranscriptEvent]


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
          tokenize='unicode61 tokenchars ''_/-:'''
        );

        CREATE TABLE IF NOT EXISTS recall_index_state (
          key TEXT PRIMARY KEY,
          value_json TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS recall_index_jobs (
          id TEXT PRIMARY KEY,
          tenant TEXT NOT NULL DEFAULT 'single',
          status TEXT NOT NULL
            CHECK (status IN ('queued', 'running', 'done', 'error', 'canceled')),

          project TEXT,
          provider TEXT,
          since_days INTEGER NOT NULL,
          limit_count INTEGER NOT NULL,

          progress_total INTEGER NOT NULL DEFAULT 0,
          progress_done INTEGER NOT NULL DEFAULT 0,
          sessions_indexed INTEGER NOT NULL DEFAULT 0,
          chunks_indexed INTEGER NOT NULL DEFAULT 0,
          child_chunk_count INTEGER NOT NULL DEFAULT 0,

          cancel_requested INTEGER NOT NULL DEFAULT 0,
          heartbeat_at TEXT,
          error TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          started_at TEXT,
          finished_at TEXT
        );

        CREATE UNIQUE INDEX IF NOT EXISTS ux_recall_index_jobs_active
          ON recall_index_jobs(tenant)
          WHERE status IN ('queued', 'running');

        CREATE INDEX IF NOT EXISTS ix_recall_index_jobs_status_created
          ON recall_index_jobs(status, created_at, id);
        """
    )
    set_index_state(conn, "schema_version", {"version": SCHEMA_VERSION})
    conn.commit()


def retrieval_schema_ready(conn: sqlite3.Connection) -> bool:
    """Return True when retrieval.db has the expected serving tables."""

    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type IN ('table', 'virtual')
          AND name IN ('recall_chunks', 'recall_chunks_fts', 'recall_index_state')
        """
    ).fetchall()
    return {str(row["name"]) for row in rows} == {"recall_chunks", "recall_chunks_fts", "recall_index_state"}


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


def _bounded_text(text: str, token_budget: int, *, strategy: str = "sandwich") -> str:
    bounded, _, _was_truncated = truncate(text, token_budget, strategy=strategy)
    return bounded.strip()


def _row_value(row: object, key: str) -> object:
    if isinstance(row, Mapping):
        return row.get(key)
    return getattr(row, key, None)


def _event_to_mapping(event: object) -> dict[str, object]:
    if isinstance(event, Mapping):
        return dict(event)
    return {
        "id": _row_value(event, "id"),
        "role": _row_value(event, "role"),
        "content_text": _row_value(event, "content_text"),
        "tool_output_text": _row_value(event, "tool_output_text"),
        "tool_name": _row_value(event, "tool_name"),
        "timestamp": _row_value(event, "timestamp"),
    }


def _session_value(session: object, key: str) -> object:
    return _row_value(session, key)


def _datetime_to_iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _metadata_kwargs(session: object) -> dict[str, str | None]:
    return {
        "provider": _as_str(_session_value(session, "provider")),
        "project": _as_str(_session_value(session, "project")),
        "environment": _as_str(_session_value(session, "environment")),
        "device_id": _as_str(_session_value(session, "device_id")),
        "cwd": _as_str(_session_value(session, "cwd")),
        "git_repo": _as_str(_session_value(session, "git_repo")),
        "git_branch": _as_str(_session_value(session, "git_branch")),
        "started_at": _datetime_to_iso(_session_value(session, "started_at")),
        "last_activity_at": _datetime_to_iso(_session_value(session, "last_activity_at")),
    }


def _as_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _format_event(event: CleanTranscriptEvent) -> str:
    label = event.role
    if event.tool_name:
        label = f"{label}:{event.tool_name}"
    content = event.content.replace("\n", "\\n")
    return f"{label}: {content}"


def _iter_traces(clean_events: list[CleanTranscriptEvent]) -> Iterable[_Trace]:
    current: list[CleanTranscriptEvent] = []
    trace_index = 0
    for event in clean_events:
        if event.role == "user" and current:
            yield _Trace(index=trace_index, events=current)
            trace_index += 1
            current = [event]
            continue
        current.append(event)
    if current:
        yield _Trace(index=trace_index, events=current)


def _trace_text(events: list[CleanTranscriptEvent]) -> str:
    return "\n".join(_format_event(event) for event in events)


def _structured_text(*, session: object, events: list[CleanTranscriptEvent], include_session: bool) -> str | None:
    tokens: list[str] = []
    if include_session:
        cwd = _as_str(_session_value(session, "cwd"))
        branch = _as_str(_session_value(session, "git_branch"))
        repo = _as_str(_session_value(session, "git_repo"))
        if cwd:
            tokens.append(f"cwd:{cwd}")
        if repo:
            tokens.append(f"repo:{repo}")
        if branch:
            tokens.append(f"branch:{branch}")
    for event in events:
        if event.tool_name:
            tokens.append(f"tool:{event.tool_name}")
        for path in _extract_path_tokens(event.content):
            tokens.append(f"file:{path}")
        for error_name in _extract_error_tokens(event.content):
            tokens.append(f"error:{error_name}")
    deduped = list(dict.fromkeys(tokens))
    return " ".join(deduped) if deduped else None


def _extract_path_tokens(text: str) -> list[str]:
    return re.findall(r"(?:[\w.-]+/)+[\w./-]+\b", text)


def _extract_error_tokens(text: str) -> list[str]:
    return re.findall(r"\b[A-Z][A-Za-z0-9_]*(?:Error|Exception)\b", text)


def _chunk_uid(session_id: str, revision: int, chunk_index: int, kind: str, content: str) -> str:
    return f"{session_id}:{revision}:{chunk_index}:{kind}:{_content_hash(content)[:16]}"


def project_session_chunks(
    session: object,
    events: list[object],
    *,
    transcript_revision: int | None = None,
) -> list[RetrievalChunk]:
    """Project durable transcript events into parent/child recall chunks."""

    session_id = str(_session_value(session, "id") or "")
    if not session_id:
        raise ValueError("session.id is required to project retrieval chunks")
    revision = int(transcript_revision or _session_value(session, "transcript_revision") or 0)
    clean_events = list(iter_clean_transcript_events([_event_to_mapping(event) for event in events], include_tool_calls=True))
    if not clean_events:
        return []

    chunks: list[RetrievalChunk] = []
    metadata = _metadata_kwargs(session)
    chunk_index = 0
    for trace in _iter_traces(clean_events):
        parent_content = _bounded_text(_trace_text(trace.events), PARENT_TOKEN_BUDGET)
        if not parent_content:
            continue
        parent_uid = _chunk_uid(session_id, revision, chunk_index, "trace_parent", parent_content)
        chunks.append(
            RetrievalChunk(
                chunk_uid=parent_uid,
                session_id=session_id,
                chunk_index=chunk_index,
                chunk_kind="trace_parent",
                retrieval_role="parent",
                event_index_start=trace.events[0].index,
                event_index_end=trace.events[-1].index,
                first_event_id=trace.events[0].event_id,
                last_event_id=trace.events[-1].event_id,
                content=parent_content,
                structured_text=_structured_text(session=session, events=trace.events, include_session=True),
                transcript_revision=revision,
                **metadata,
            )
        )
        chunk_index += 1

        for child_kind, child_event in _child_events_for_trace(trace.events):
            child_content = _bounded_text(_format_event(child_event), CHILD_TOKEN_BUDGET, strategy="tail")
            if not child_content:
                continue
            child_uid = _chunk_uid(session_id, revision, chunk_index, child_kind, child_content)
            chunks.append(
                RetrievalChunk(
                    chunk_uid=child_uid,
                    session_id=session_id,
                    parent_chunk_uid=parent_uid,
                    chunk_index=chunk_index,
                    chunk_kind=child_kind,
                    retrieval_role="child",
                    event_index_start=child_event.index,
                    event_index_end=child_event.index,
                    first_event_id=child_event.event_id,
                    last_event_id=child_event.event_id,
                    content=child_content,
                    intent_text=child_content if child_kind == "intent" else None,
                    evidence_text=child_content if child_kind != "intent" else None,
                    structured_text=_structured_text(session=session, events=[child_event], include_session=False),
                    transcript_revision=revision,
                    **metadata,
                )
            )
            chunk_index += 1

    return chunks


def _child_events_for_trace(events: list[CleanTranscriptEvent]) -> Iterable[tuple[str, CleanTranscriptEvent]]:
    user_event = next((event for event in events if event.role == "user"), None)
    if user_event is not None:
        yield "intent", user_event
    assistant_events = [event for event in events if event.role == "assistant"]
    if assistant_events:
        yield "assistant_conclusion", assistant_events[-1]
    for event in events:
        if event.role == "tool" or event.tool_name:
            yield "tool_result", event


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
                    "chunk_uid": chunk.chunk_uid,
                    "session_id": chunk.session_id,
                    "parent_session_id": chunk.parent_session_id,
                    "thread_id": chunk.thread_id,
                    "parent_thread_id": chunk.parent_thread_id,
                    "parent_chunk_id": parent_chunk_id,
                    "chunk_index": chunk.chunk_index,
                    "chunk_kind": chunk.chunk_kind,
                    "retrieval_role": chunk.retrieval_role,
                    "event_index_start": chunk.event_index_start,
                    "event_index_end": chunk.event_index_end,
                    "first_event_id": chunk.first_event_id,
                    "last_event_id": chunk.last_event_id,
                    "provider": chunk.provider,
                    "project": chunk.project,
                    "environment": chunk.environment,
                    "device_id": chunk.device_id,
                    "cwd": chunk.cwd,
                    "git_repo": chunk.git_repo,
                    "git_branch": chunk.git_branch,
                    "started_at": chunk.started_at,
                    "last_activity_at": chunk.last_activity_at,
                    "content": chunk.content,
                    "intent_text": chunk.intent_text,
                    "evidence_text": chunk.evidence_text,
                    "structured_text": chunk.structured_text,
                    "content_hash": content_hash,
                    "token_count": token_count,
                    "transcript_revision": chunk.transcript_revision,
                    "indexed_at": _utc_now_iso(),
                    "stale": chunk.stale,
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


def child_chunk_count(conn: sqlite3.Connection) -> int:
    """Return the number of searchable child chunks."""

    row = conn.execute("SELECT count(*) FROM recall_chunks WHERE retrieval_role = 'child'").fetchone()
    return int(row[0] if row else 0)


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
    hide_internal_canary: bool = True,
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
          AND (
            :hide_internal_canary = 0
            OR (
              lower(coalesce(c.provider, '')) NOT IN ('canary', 'cnary')
              AND lower(coalesce(c.project, '')) NOT IN ('canary', 'cnary')
              AND lower(coalesce(c.project, '')) NOT LIKE 'canary-%'
              AND lower(coalesce(c.project, '')) NOT LIKE 'cnary-%'
              AND lower(coalesce(c.device_id, '')) NOT IN ('canary', 'cnary')
              AND lower(coalesce(c.device_id, '')) NOT LIKE '%-canary'
              AND lower(coalesce(c.device_id, '')) NOT LIKE '%-cnary'
            )
          )
        ORDER BY score ASC
        LIMIT :limit
        """,
        {
            "fts_query": fts_query,
            "project": project,
            "provider": provider,
            "environment": environment,
            "since": since,
            "hide_internal_canary": 1 if hide_internal_canary else 0,
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


def get_chunks_by_ids(conn: sqlite3.Connection, chunk_ids: Iterable[int]) -> dict[int, StoredRetrievalChunk]:
    """Hydrate stored chunks by id."""

    ids = list(dict.fromkeys(int(chunk_id) for chunk_id in chunk_ids if chunk_id is not None))
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT
          id, chunk_uid, session_id, parent_chunk_id, chunk_index, chunk_kind,
          retrieval_role, event_index_start, event_index_end, first_event_id,
          last_event_id, content, intent_text, evidence_text, structured_text
        FROM recall_chunks
        WHERE id IN ({placeholders})
        """,
        ids,
    ).fetchall()
    return {int(row["id"]): _stored_chunk_from_row(row) for row in rows}


def _stored_chunk_from_row(row: sqlite3.Row) -> StoredRetrievalChunk:
    return StoredRetrievalChunk(
        chunk_id=int(row["id"]),
        chunk_uid=str(row["chunk_uid"]),
        session_id=str(row["session_id"]),
        parent_chunk_id=int(row["parent_chunk_id"]) if row["parent_chunk_id"] is not None else None,
        chunk_index=int(row["chunk_index"]),
        chunk_kind=str(row["chunk_kind"]),
        retrieval_role=str(row["retrieval_role"]),
        event_index_start=int(row["event_index_start"]),
        event_index_end=int(row["event_index_end"]),
        first_event_id=int(row["first_event_id"]) if row["first_event_id"] is not None else None,
        last_event_id=int(row["last_event_id"]) if row["last_event_id"] is not None else None,
        content=str(row["content"]),
        intent_text=row["intent_text"],
        evidence_text=row["evidence_text"],
        structured_text=row["structured_text"],
    )
