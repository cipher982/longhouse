# Session Transcript Preview Freshness Contract

## Purpose

Timeline cards can use low-latency managed bridge text while the durable
transcript catches up. That text is now materialized as an active provisional
`AgentEvent`, not as a separate render-only runtime overlay.

The server response exposes this event through `transcript_preview` so clients
can render it without guessing whether it is durable, provisional, or stale.

## Current API Contract

Timeline and session projections may return:

```json
{
  "transcript_preview": {
    "event_id": 12345,
    "text": "partial answer...",
    "event_origin": "live_provisional",
    "timestamp": "2026-05-11T15:00:01Z",
    "is_provisional": true,
    "is_complete": false,
    "content_cursor": "codex_bridge_live:session-uuid:thread-1:turn-1:12",
    "is_stale": false,
    "stale_reason": null
  }
}
```

Visible preview text comes only from `transcript_preview`.

## Freshness Rules

The event ledger owns supersession:

- durable archive ingest reconciles matching provisional rows;
- terminal runtime signals supersede active provisional rows;
- inactive provisional rows are hidden from normal transcript/event queries.

The preview response owns render freshness:

```text
preview_at = transcript_preview.timestamp

if no active provisional event:
  transcript_preview = null
elif preview age exceeds freshness budget:
  transcript_preview.is_stale = true
else:
  transcript_preview.is_stale = false
```

Freshness budgets are owned by `server/zerg/services/session_views.py`:

- `PROVISIONAL_TRANSCRIPT_PARTIAL_FRESHNESS`: incomplete provisional budget.
- `PROVISIONAL_TRANSCRIPT_COMPLETE_FRESHNESS`: complete provisional budget.

## UI Rules

Timeline cards:

- Render preview only when `transcript_preview` exists and
  `transcript_preview.is_stale == false`.
- Use `Live output` for incomplete current provisional previews.
- Use `Latest output` for complete current previews.
- Prefer keyword/semantic search snippets over transcript preview.
- Prefer durable summary when the preview is missing, stale, superseded, or the
  session is closed.

Session detail:

- Durable transcript remains the main content.
- Runtime banners may show current managed state, but bridge preview text should
  not become durable transcript history unless it reconciles through archive
  ingest.
