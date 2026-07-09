# Worklog Day Export API

Status: Implemented
Owner: Longhouse session core / Sauron worklog

## Problem

Sauron worklog needs yesterday's Longhouse session messages for a daily
digest. The current job reaches into the hosted Longhouse container over SSH,
opens `/data/longhouse.db`, and runs private SQLite queries against `events`.
That made the job fresh again, but it coupled Sauron to Longhouse deployment
details and SQLite planner choices.

Large hosted databases make the wrong query plan prohibitively expensive:
the day count is fast when anchored on `events.timestamp`, while the text-message
query can choose `ix_events_role`, scan historical user/assistant rows, and hit
the SSH timeout.

## Decision

Add a narrow machine-facing Longhouse API:

```text
GET /api/agents/worklog/day?date=YYYY-MM-DD&timezone=America/New_York&include_test=false
```

The route uses a full `X-Agents-Token` device/agents token, returns JSON, and
is implemented by Longhouse against its canonical `sessions` and `events`
tables. Sauron consumes this HTTP contract and stops using SSH/container/DB
internals.

## Contract

Response shape:

```json
{
  "date": "2026-07-07",
  "timezone": "America/New_York",
  "window_start": "2026-07-07T00:00:00-04:00",
  "window_end": "2026-07-08T00:00:00-04:00",
  "source": "longhouse-worklog-api-v1",
  "sessions": [
    {
      "id": "uuid",
      "project": "longhouse",
      "provider": "codex",
      "git_repo": "https://github.com/cipher982/longhouse.git",
      "cwd": "/home/example/longhouse",
      "started_at": "2026-07-07T12:00:00Z",
      "user_messages": 4,
      "assistant_messages": 8,
      "tool_calls": 2,
      "is_sidechain": false,
      "first_event_at": "2026-07-07T12:00:00Z",
      "last_event_at": "2026-07-07T12:10:00Z",
      "first_message_at": "2026-07-07T12:00:00Z",
      "message_count": 12,
      "event_count": 30
    }
  ],
  "events": [
    {
      "session_id": "uuid",
      "role": "user",
      "content_text": "message text",
      "timestamp": "2026-07-07T12:00:00Z"
    }
  ],
  "stats": {
    "session_count": 1,
    "message_count": 12,
    "event_count": 30
  }
}
```

`date` is interpreted in the supplied IANA timezone. The default timezone is
`America/New_York`, matching the existing Sauron digest convention. Daylight
saving transitions are represented by the returned absolute window bounds. The
window is half-open: `[window_start, window_end)`.

`include_test=false` excludes `environment IN ('test', 'e2e')`. `true` includes
every environment.

Ordering is stable:

- sessions: `COALESCE(first_message_at, first_event_at), started_at, id`
- events: `session_id, timestamp`

Lifetime session counters (`user_messages`, `assistant_messages`, `tool_calls`)
come from `sessions`. Window-scoped fields (`first_event_at`, `last_event_at`,
`first_message_at`, `message_count`, `event_count`) come from the selected day.
`is_sidechain` is derived from session lineage, not from a stored
`sessions.is_sidechain` column.

## Query Shape

The implementation must narrow by timestamp before applying role/text filters.
On SQLite, use an `INDEXED BY ix_events_timestamp` query for the day-scoped
subqueries so the planner cannot choose `ix_events_role` and scan historical
message rows. Add a query-plan regression test that verifies the message query
uses `ix_events_timestamp`.

The route returns only user/assistant events with non-null `content_text`, while
session `event_count` covers all events in the day window. Sauron already filters
sidechains, trivial sessions, and empty-message sessions after fetch.

Event scope intentionally matches the existing Sauron SSH export: all events in
the time window, without a durable-only or head-branch filter. This route is the
new transport for the current digest input, not a semantic transcript export.
Invalid dates, invalid IANA timezones, and missing auth use normal HTTP status
codes with JSON `detail`.

V1 returns one JSON payload without pagination or streaming. The expected daily
payload is small enough for this consumer; add pagination only if a real day or
backfill case proves otherwise.

## Non-Goals

- Do not add a daily projection table yet.
- Do not move worklog digest generation into Longhouse.
- Do not make Sauron depend on browser routes or timeline payloads.
- Do not query Life Hub mirror tables for canonical session history.

## Later

If this API gains many consumers, wide historical backfills, or repeated
weekly/monthly analytics, add a materialized session-day projection maintained
by ingest/projection. Until then, a timestamp-anchored direct read is simpler
and already fast enough for one daily digest.
