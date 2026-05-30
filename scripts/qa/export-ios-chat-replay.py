#!/usr/bin/env python3
"""Export a local SQLite session as an iOS chat replay fixture.

The output is intentionally transient test data: write it to /tmp, pass it to
LONGHOUSE_UI_TEST_CHAT_REPLAY_PATH, and do not commit it.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


DEFAULT_DB = Path.home() / ".longhouse" / "longhouse.db"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite DB path")
    parser.add_argument("--session-id", help="Session id to export; defaults to the largest transcript")
    parser.add_argument("--limit", type=int, default=800, help="Maximum events to export; 0 means all")
    parser.add_argument("--output", required=True, help="Fixture JSON output path")
    return parser.parse_args()


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def choose_session(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT
            s.id,
            s.provider,
            COALESCE(s.summary_title, s.summary, '') AS title,
            COUNT(e.id) AS event_count,
            COALESCE(SUM(LENGTH(COALESCE(e.content_text, '')) + LENGTH(COALESCE(e.tool_output_text, ''))), 0)
                AS payload_bytes
        FROM sessions s
        JOIN events e ON e.session_id = s.id
        GROUP BY s.id
        ORDER BY event_count DESC, payload_bytes DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise SystemExit("No sessions with events found")
    return row


def load_session(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT
            s.id,
            s.provider,
            COALESCE(s.summary_title, s.summary, '') AS title,
            COUNT(e.id) AS event_count,
            COALESCE(SUM(LENGTH(COALESCE(e.content_text, '')) + LENGTH(COALESCE(e.tool_output_text, ''))), 0)
                AS payload_bytes
        FROM sessions s
        LEFT JOIN events e ON e.session_id = s.id
        WHERE s.id = ?
        GROUP BY s.id
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        raise SystemExit(f"Session not found: {session_id}")
    return row


def _parse_tool_input(raw: Any) -> Any:
    """tool_input_json is stored as a JSON string; embed it as a real object so
    the iOS replay loader can decode it into [String: JSONValue]. Returns None on
    missing/invalid JSON rather than smuggling a string through."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def export_events(conn: sqlite3.Connection, session_id: str, limit: int) -> list[dict[str, Any]]:
    limit_clause = "" if limit <= 0 else "LIMIT ?"
    params: tuple[Any, ...] = (session_id,) if limit <= 0 else (session_id, limit)
    rows = conn.execute(
        f"""
        SELECT
            id,
            role,
            content_text,
            tool_name,
            tool_input_json,
            tool_output_text,
            tool_call_id,
            timestamp
        FROM events
        WHERE session_id = ?
        ORDER BY timestamp, id
        {limit_clause}
        """,
        params,
    ).fetchall()
    # NOTE: tool_call_state (running/completed/dropped) is NOT stored on events —
    # the server derives it at projection time from pairing. So replay can't
    # reproduce running/dropped states; synthetic fixtures cover those instead.
    return [
        {
            "id": int(row["id"]),
            "role": row["role"],
            "contentText": row["content_text"],
            "toolName": row["tool_name"],
            "toolInputJson": _parse_tool_input(row["tool_input_json"]),
            "toolOutputText": row["tool_output_text"],
            "toolCallId": row["tool_call_id"],
            "timestamp": row["timestamp"],
        }
        for row in rows
    ]


def main() -> None:
    args = parse_args()
    db_path = Path(args.db).expanduser()
    output_path = Path(args.output).expanduser()
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    with connect_readonly(db_path) as conn:
        session = load_session(conn, args.session_id) if args.session_id else choose_session(conn)
        events = export_events(conn, str(session["id"]), args.limit)

    payload = {
        "metadata": {
            "sourceDb": str(db_path),
            "sessionId": session["id"],
            "provider": session["provider"],
            "title": session["title"],
            "sourceEventCount": int(session["event_count"]),
            "sourcePayloadBytes": int(session["payload_bytes"]),
            "exportedEventCount": len(events),
        },
        "events": events,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(
        "exported "
        f"{len(events)} events from {session['id']} "
        f"(source_events={session['event_count']} bytes={session['payload_bytes']}) "
        f"to {output_path}"
    )


if __name__ == "__main__":
    main()
