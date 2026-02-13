#!/usr/bin/env python3
"""Migrate insights and sessions from Life Hub to Longhouse.

Requires:
- LONGHOUSE_URL: Longhouse API URL (default: https://david.longhouse.ai)
- LONGHOUSE_TOKEN: Device token (default: reads from ~/.claude/longhouse-device-token)
- LIFE_HUB_DB_URL: Life Hub PostgreSQL connection string

Usage:
    python3 scripts/migrate_from_lifehub.py insights   # Migrate insights only
    python3 scripts/migrate_from_lifehub.py sessions    # Migrate cursor/swarmlet sessions
    python3 scripts/migrate_from_lifehub.py all         # Both
"""

import gzip
import json
import os
import sys
import time
from pathlib import Path

import psycopg2
import psycopg2.extras
import requests

LONGHOUSE_URL = os.environ.get("LONGHOUSE_URL", "https://david.longhouse.ai")
LONGHOUSE_TOKEN = os.environ.get("LONGHOUSE_TOKEN", "")
if not LONGHOUSE_TOKEN:
    token_file = Path.home() / ".claude" / "longhouse-device-token"
    if token_file.exists():
        LONGHOUSE_TOKEN = token_file.read_text().strip()

LIFE_HUB_DB_URL = os.environ.get("LIFE_HUB_DB_URL", "")

HEADERS = {
    "X-Agents-Token": LONGHOUSE_TOKEN,
    "Content-Type": "application/json",
}


def get_lifehub_conn():
    if not LIFE_HUB_DB_URL:
        print("ERROR: Set LIFE_HUB_DB_URL (postgresql://...)")
        sys.exit(1)
    return psycopg2.connect(LIFE_HUB_DB_URL)


def migrate_insights():
    """Migrate insights from Life Hub PostgreSQL to Longhouse API."""
    conn = get_lifehub_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT id, insight_type, title, description, project, severity,
               confidence, tags, created_at, session_id
        FROM work.insights
        ORDER BY created_at
    """)
    insights = cur.fetchall()
    cur.close()
    conn.close()

    print(f"Found {len(insights)} insights in Life Hub")

    migrated = 0
    skipped = 0
    errors = 0

    for ins in insights:
        payload = {
            "insight_type": ins["insight_type"],
            "title": ins["title"],
            "description": ins.get("description"),
            "project": ins.get("project"),
            "severity": ins.get("severity", "info"),
            "confidence": ins.get("confidence"),
            "tags": ins.get("tags") or [],
            "session_id": str(ins["session_id"]) if ins.get("session_id") else None,
        }

        try:
            resp = requests.post(
                f"{LONGHOUSE_URL}/api/insights",
                headers=HEADERS,
                json=payload,
                timeout=30,
            )
            if resp.status_code == 200:
                migrated += 1
            elif resp.status_code == 409:
                skipped += 1  # Already exists / deduped
            else:
                print(f"  WARN [{resp.status_code}] {ins['title'][:60]}: {resp.text[:200]}")
                errors += 1
        except Exception as exc:
            print(f"  ERROR {ins['title'][:60]}: {exc}")
            errors += 1

    print(f"Insights: {migrated} migrated, {skipped} skipped, {errors} errors")


def migrate_sessions():
    """Migrate cursor/swarmlet sessions from Life Hub to Longhouse ingest API."""
    conn = get_lifehub_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Get sessions that don't exist in Longhouse (cursor + swarmlet)
    cur.execute("""
        SELECT id, provider, project, cwd, git_repo, git_branch,
               started_at, ended_at, user_messages, assistant_messages, tool_calls
        FROM agents.sessions
        WHERE provider IN ('cursor', 'swarmlet')
        ORDER BY started_at
    """)
    sessions = cur.fetchall()
    print(f"Found {len(sessions)} cursor/swarmlet sessions in Life Hub")

    migrated = 0
    skipped = 0
    errors = 0

    for sess in sessions:
        sid = str(sess["id"])

        # Fetch events for this session
        cur.execute(
            """
            SELECT role, content_text, tool_name, tool_input_json, tool_output_text,
                   timestamp
            FROM agents.events
            WHERE session_id = %s
            ORDER BY timestamp
        """,
            (sess["id"],),
        )
        events = cur.fetchall()

        # Build ingest payload
        event_list = []
        for ev in events:
            event_dict = {
                "role": ev.get("role") or "user",
                "timestamp": ev["timestamp"].isoformat() if ev.get("timestamp") else sess["started_at"].isoformat(),
            }
            if ev.get("content_text"):
                event_dict["content_text"] = ev["content_text"]
            if ev.get("tool_name"):
                event_dict["tool_name"] = ev["tool_name"]
            if ev.get("tool_input_json"):
                event_dict["tool_input_json"] = ev["tool_input_json"]
            if ev.get("tool_output_text"):
                event_dict["tool_output_text"] = ev["tool_output_text"]
            event_list.append(event_dict)

        payload = {
            "id": sid,
            "provider": sess["provider"],
            "environment": "production",
            "project": sess.get("project"),
            "device_id": "lifehub-migration",
            "cwd": sess.get("cwd"),
            "git_repo": sess.get("git_repo"),
            "git_branch": sess.get("git_branch"),
            "started_at": sess["started_at"].isoformat() if sess.get("started_at") else None,
            "ended_at": sess["ended_at"].isoformat() if sess.get("ended_at") else None,
            "events": event_list,
        }

        try:
            # Gzip compress for efficiency
            payload_bytes = json.dumps(payload).encode("utf-8")
            compressed = gzip.compress(payload_bytes)

            # Retry with backoff on rate limit
            resp = None
            for attempt in range(10):
                resp = requests.post(
                    f"{LONGHOUSE_URL}/api/agents/ingest",
                    headers={**HEADERS, "Content-Encoding": "gzip"},
                    data=compressed,
                    timeout=60,
                )
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 30))
                    retry_after = max(retry_after, 10 * (attempt + 1))  # Exponential backoff floor
                    time.sleep(retry_after)
                    continue
                break

            if resp.status_code == 200:
                data = resp.json()
                migrated += 1
                if migrated % 10 == 0:
                    print(f"  [{migrated}/{len(sessions)}] {sess['provider']} {sid[:8]} — {data.get('events_inserted', 0)} events")
            else:
                print(f"  WARN [{resp.status_code}] {sid[:8]}: {resp.text[:200]}")
                errors += 1

            # Rate limit: ~5 events/sec = 300/min (well under 1000 limit)
            # Conservative to account for background shipper also using budget
            event_delay = max(2.0, len(event_list) / 5.0)
            time.sleep(event_delay)

        except Exception as exc:
            print(f"  ERROR {sid[:8]}: {exc}")
            errors += 1

    cur.close()
    conn.close()
    print(f"Sessions: {migrated} migrated, {skipped} skipped, {errors} errors")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 migrate_from_lifehub.py [insights|sessions|all]")
        sys.exit(1)

    mode = sys.argv[1]

    if mode in ("insights", "all"):
        migrate_insights()
    if mode in ("sessions", "all"):
        migrate_sessions()
