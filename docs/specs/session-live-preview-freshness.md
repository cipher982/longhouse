# Session Live Preview Freshness Contract

## Purpose

Timeline cards can use low-latency managed bridge text as a preview while the
durable transcript catches up. That preview must never be presented as fresher
truth than the canonical transcript.

This contract makes the distinction explicit:

- **Durable transcript activity** is canonical for history, detail, search,
  replay, export, and memory.
- **Live transcript overlay** is a provisional card preview sourced from a
  managed bridge/runtime signal.
- **Freshness metadata** tells clients whether the overlay is renderable,
  provisional, stale, or superseded by durable activity.

## Goals

- Give browser clients a server-owned freshness decision instead of duplicating
  timestamp heuristics.
- Prevent stale bridge text from replacing durable summaries on timeline cards.
- Keep `live_transcript` scoped to timeline-card projections.
- Preserve existing latency: no extra transcript queries in the card hot path.
- Make test fixtures explain why a preview is visible or hidden.

## Non-Goals

- Do not make bridge overlay text a durable `AgentEvent`.
- Do not collapse the live bridge lane into archive ingest.
- Do not change session detail transcript ordering in this phase.
- Do not require all clients to render hidden/superseded overlay text.

## API Contract

Timeline-card projections may return two related fields:

```json
{
  "live_transcript": {
    "text": "partial answer...",
    "source": "codex_bridge_live",
    "received_at": "2026-05-11T15:00:02Z",
    "occurred_at": "2026-05-11T15:00:01Z",
    "thread_id": "thread-1",
    "turn_id": "turn-1",
    "seq": 12,
    "method": "item/agentMessage/delta",
    "is_complete": false,
    "content_cursor": "codex_bridge_live:session-uuid:thread-1:turn-1:12",
    "overlay_at": "2026-05-11T15:00:01Z",
    "last_durable_at": "2026-05-11T14:59:59Z",
    "freshness": "current",
    "is_provisional": true,
    "is_stale": false,
    "stale_reason": null
  }
}
```

`live_transcript` is present only when the overlay is renderable. Generic
session/detail projections keep it `null` unless they are explicitly being used
as timeline-card compatibility responses.

### Freshness States

| State | Meaning | API behavior | UI behavior |
| --- | --- | --- | --- |
| `current` | Overlay is newer than durable transcript activity and within the card freshness budget. | Return `live_transcript` with `is_stale=false`. | May show as `Live output` if incomplete, `Latest output` if complete. |
| `superseded` | Durable transcript activity is newer than the overlay. | Return `live_transcript=null`; server may expose this only in debug later. | Show durable summary/snippet/status. Never show stale bridge text. |
| `stale` | Overlay is not superseded, but it exceeded the card freshness budget. | Return `live_transcript` with `is_stale=true` only for compatibility; clients must not render it as live. | Hide preview and show durable summary/status. |
| `absent` | No overlay exists or projection did not opt into overlays. | Return `live_transcript=null`. | Render normal durable card content. |

## Freshness Rules

The server computes overlay timestamps as:

```text
overlay_at = occurred_at || received_at
last_durable_at = session.last_activity_at || ended_at || started_at
```

For timeline-card preview rendering:

```text
if no overlay:
  state = absent
elif last_durable_at > overlay_at:
  state = superseded
elif overlay age exceeds freshness budget:
  state = stale
else:
  state = current
```

Freshness budgets are owned by `server/zerg/services/session_views.py`:

- `LIVE_TRANSCRIPT_PARTIAL_FRESHNESS`: partial/in-flight overlay budget.
- `LIVE_TRANSCRIPT_COMPLETE_FRESHNESS`: complete live overlay budget.

The decision belongs to the server response so all clients see the same truth.

## UI Rules

Timeline cards:

- Render preview only when `live_transcript.freshness == "current"` and
  `live_transcript.is_stale == false`.
- Use `Live output` for incomplete current overlays.
- Use `Latest output` for complete current overlays.
- Prefer keyword/semantic search snippets over live preview.
- Prefer durable summary when the overlay is missing, stale, or superseded.
- Do not infer freshness from `received_at` or a client-owned age budget.

Session detail:

- Durable transcript remains the main content.
- Runtime banners may show current managed state, but bridge preview text should
  not appear as transcript history unless it has reconciled into durable events.

## Success Criteria

- Backend tests prove renderable overlays include freshness metadata.
- Backend tests prove durable transcript activity newer than overlay suppresses
  the overlay for timeline cards.
- Frontend tests prove stale/superseded overlays do not replace durable summary.
- Frontend tests prove fresh overlays still replace stale generated summary.
- Generated OpenAPI/types include the freshness fields.
- UI fixture QA shows no visible regression in timeline card layout.
