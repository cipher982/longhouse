#!/usr/bin/env python3
"""Direct session migration: Life Hub Postgres → Longhouse SQLite via SQL dump.

Reads from Life Hub Postgres, generates SQLite-compatible INSERT statements,
then pipes them into the container's sqlite3 CLI.

Usage:
    LIFE_HUB_DB_URL=... python3 scripts/migrate_sessions_direct.py > /tmp/migration.sql
    scp /tmp/migration.sql zerg:/tmp/
    ssh zerg 'sudo cp /tmp/migration.sql /var/lib/docker/data/longhouse/david/migration.sql'
    ssh zerg 'docker exec longhouse-david sqlite3 /data/longhouse.db < /data/migration.sql'
"""

import json
import os
import sys

import psycopg2
import psycopg2.extras

LIFE_HUB_DB_URL = os.environ.get("LIFE_HUB_DB_URL", "")


def escape_sql(val):
    """Escape a value for SQLite INSERT."""
    if val is None:
        return "NULL"
    s = str(val).replace("'", "''")
    return f"'{s}'"


def main():
    if not LIFE_HUB_DB_URL:
        print("-- ERROR: Set LIFE_HUB_DB_URL", file=sys.stderr)
        sys.exit(1)

    pg = psycopg2.connect(LIFE_HUB_DB_URL)
    pg_cur = pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Get cursor/swarmlet sessions
    pg_cur.execute("""
        SELECT id, provider, project, cwd, git_repo, git_branch,
               started_at, ended_at, user_messages, assistant_messages, tool_calls
        FROM agents.sessions
        WHERE provider IN ('cursor', 'swarmlet')
        ORDER BY started_at
    """)
    sessions = pg_cur.fetchall()
    print(f"-- Found {len(sessions)} sessions", file=sys.stderr)

    print("BEGIN;")

    migrated = 0
    for sess in sessions:
        sid = str(sess["id"])

        # Get events
        pg_cur.execute("""
            SELECT role, content_text, tool_name, tool_input_json, tool_output_text, timestamp
            FROM agents.events
            WHERE session_id = %s
            ORDER BY timestamp
        """, (sess["id"],))
        events = pg_cur.fetchall()

        # INSERT OR IGNORE — won't duplicate if ID already exists
        started = sess["started_at"].isoformat() if sess.get("started_at") else "1970-01-01T00:00:00"
        ended = sess["ended_at"].isoformat() if sess.get("ended_at") else None

        print(f"INSERT OR IGNORE INTO sessions "
              f"(id, provider, environment, project, cwd, git_repo, git_branch, "
              f"started_at, ended_at, user_messages, assistant_messages, tool_calls, "
              f"needs_embedding, device_id) VALUES ("
              f"{escape_sql(sid)}, {escape_sql(sess['provider'])}, 'production', "
              f"{escape_sql(sess.get('project'))}, {escape_sql(sess.get('cwd'))}, "
              f"{escape_sql(sess.get('git_repo'))}, {escape_sql(sess.get('git_branch'))}, "
              f"{escape_sql(started)}, {escape_sql(ended)}, "
              f"{sess.get('user_messages') or 0}, {sess.get('assistant_messages') or 0}, "
              f"{sess.get('tool_calls') or 0}, 1, 'lifehub-migration');")

        for ev in events:
            ts = ev["timestamp"].isoformat() if ev.get("timestamp") else started

            # Handle tool_input_json — it may be a dict or string
            tool_input = ev.get("tool_input_json")
            if isinstance(tool_input, dict):
                tool_input = json.dumps(tool_input)

            # id is INTEGER auto-increment — omit it, let SQLite assign
            print(f"INSERT INTO events "
                  f"(session_id, role, content_text, tool_name, "
                  f"tool_input_json, tool_output_text, timestamp) VALUES ("
                  f"{escape_sql(sid)}, "
                  f"{escape_sql(ev.get('role') or 'user')}, "
                  f"{escape_sql(ev.get('content_text'))}, "
                  f"{escape_sql(ev.get('tool_name'))}, "
                  f"{escape_sql(tool_input)}, "
                  f"{escape_sql(ev.get('tool_output_text'))}, "
                  f"{escape_sql(ts)});")

        migrated += 1
        if migrated % 10 == 0:
            print(f"-- Progress: {migrated}/{len(sessions)}", file=sys.stderr)

    print("COMMIT;")
    print(f"-- Done: {migrated} sessions generated", file=sys.stderr)

    pg_cur.close()
    pg.close()


if __name__ == "__main__":
    main()
