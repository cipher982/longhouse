# Per-event media rendering (web + iOS) + inline-media backfill

**Status:** implemented in branch, pending review/backfill decision.
**Branch:** `feat/media-refs-renderer` off `6c0d260b9`.

## Why

`media_refs` is plumbed end-to-end into the API/OpenAPI contract but has **zero
non-generated client consumers** — it exists only in `web/src/generated/openapi-types.ts`
and `ios/Sources/Shared/Generated/SessionAPI.generated.swift`. And the inline-media
backfill has **never run** (`ingest-health` reports `media_repair_refs: 0`), so even if
a renderer existed, historical sessions have no stored media to show.

Decision (David, 2026-06-26): build the client renderers first, then review the
backfill totals before applying writes so historical sessions actually have media.

## Server contract (already shipped — do not change)

### `EventMediaRefResponse` (`server/zerg/services/session_views.py:1016`)

```python
sha256: str            # content address; the only thing needed to fetch bytes
media_state: str       # "pending" | "present" | "failed"  — render gate
mime_type: Optional[str]   # only when bytes stored (MediaObject row exists)
byte_size: Optional[int]   # only when bytes stored
blob_url: str          # always: "/api/media/{sha256}/blob"
thumb_url: Optional[str]   # "/api/media/{sha256}/thumb" only when a thumbnail exists
source_path: Optional[str]
source_offset: Optional[int]
json_pointer: Optional[str]
original_kind: str
```

Notes:
- **No `width`/`height`** projected (MediaObject stores them but they aren't exposed) →
  no layout-before-load; size from decoded image or accept reflow.
- `media_refs` defaults to `[]`, never null.
- Render gate: only `media_state == "present"` is fetchable. `pending`/`failed` →
  placeholder.

### Endpoints returning `media_refs`

- Browser (cookie auth, what web uses): `GET /api/timeline/sessions/{id}/projection`
  (`items[].event.media_refs`) and `GET /api/timeline/sessions/{id}/events`.
- Machine (X-Agents-Token): `GET /api/agents/sessions/{id}/events` and `/projection`.

### Browser media read contract (`server/zerg/routers/agents_media.py:35`)

- `GET /api/media/{sha256}/blob`, `GET /api/media/{sha256}/thumb`,
  `HEAD /api/media/{sha256}` (no `/blob` suffix on HEAD).
- Auth: `get_current_browser_route_user` — `longhouse_session` cookie, or a
  `zdt_`-prefixed `?token=` query JWT (for `<img>` tags that can't set headers / SSE).
- Visibility enforced server-side by a join: the sha must be referenced by a
  `SessionMediaRef` on a session the current user owns, else 404. Caller passes no
  session/event — just the sha.
- Streams `StreamingResponse(media_type=mime_type)` with `Content-Length` +
  `X-Media-Sha256`. **No range, no cache headers, no Content-Disposition.**
- Share-token session pages can read the transcript projection, but this media byte
  route does **not** accept `share_token`. The branch suppresses media tiles on
  `?share_token=...` views rather than showing broken images. A future share-aware
  media route can lift that guard.

## The gotcha: hand-written event models dropped `media_refs` on both clients

The timeline does NOT consume generated types directly. Both clients re-map into
hand-written models. The branch adds the missing media fields in these places:

### Web
1. `web/src/services/api/agents.ts` — `interface AgentEvent` now carries
   `media_refs?: EventMediaRefResponse[]`; the projection response
   decodes into `AgentEvent` (`fetchAgentSessionProjection` :885, `fetchAgentSessionEvents` :1024).
2. Pairing/model layer `web/src/lib/sessionWorkspace/timelineModel.ts:248`
   (`buildTimelineModel`) — media rides on already-paired message / call / result
   events; no pairing change needed.
3. Render sites in `web/src/components/session-workspace/TimelinePane.tsx`:
   - `MessageRow` (:186) — user/assistant message media.
   - `ToolRow` / `ActionCard` output area (~:357) — tool-result media.
4. Existing `<img>` idiom to mirror: `web/src/components/AttachmentTray.tsx:36`
   (`session-chat-attachment-tray__thumb`, `web/src/styles/session-chat.css:711`).
   That's the outbound composer; inbound/event media display is net-new.

### iOS
5. `ios/Sources/Shared/SessionModels.swift` — `struct SessionEvent` now has
   `mediaRefs`; its explicit `init(from:)` defaults absent media to `[]` for cached
   payload compatibility.
6. Adapter `ios/Sources/Shared/SessionAPIAdapters.swift:213`
   (`APIEventResponse.sessionEvent`) copies `mediaRefs`.
7. Pairing `ios/Sources/Shared/TimelineBuilder.swift:92` mirrors web `timelineModel.ts`
   — keep in lockstep (memory note), but no pairing change needed; media rides events.
8. **iOS renders the transcript in a WebView, not native rows.**
   `ios/Sources/LonghouseApp/WebTranscriptView.swift`: `TimelineItem` →
   `WebTranscriptPayloadItem` (:899, has no media field — add one), populated in
   `messagePayload` (:217) / `toolPayload` (:332). The embedded JS render functions
   `message()` (:1673) / `toolDetails()` (:1606) emit the HTML — `<img>` tags go there.
   No native `AsyncImage` precedent; staying in the WebView matches current architecture.

## Auth wrinkle for image loading

- **Web:** `<img src="/api/media/{sha}/blob">` sends the `longhouse_session` cookie
  automatically (same-origin). Simplest path. Use `thumb_url` when present, fall back
  to `blob_url`.
- **iOS WebView:** branch decision is cookie-in-WKWebView. `WebTranscriptView`
  loads its HTML document with `baseURL: serverURL` and copies managed auth cookies
  into the `WKHTTPCookieStore` before sending media-bearing payloads. Hosted
  query-token path requires a `zdt_`-prefixed token, so the branch does not try to
  mint tokenized image URLs.

## Backfill (run against hosted david010)

`POST /api/agents/media/backfill-inline-data-urls` — machine token, **query params**:

| param | default | caps |
|---|---|---|
| `dry_run` | true | — |
| `max_rows` | 100 | 1..1000 |
| `max_bytes` | 10 MiB | 1..50 MiB (decoded budget/batch) |
| `after_id` | 0 | cursor: scans source_lines.id > after_id |
| `confirmed_backup_gate` | false | **required true when dry_run=false** |
| `disk_floor_bytes` | 1 GiB | min free to leave on media FS |

Returns counts incl. `last_source_line_id` (feed back as `after_id`). Scans
`source_lines` for `data:image/...;base64,...`, decodes (allow-list png/jpeg/webp/gif),
sha256s, upserts `SessionMediaRef(media_state=pending)` + stores blob (flips to present).

Run plan:
1. Dry-run from `after_id=0`, `max_rows=1000` repeatedly (paginate on
   `last_source_line_id`) to size total `candidate_refs` / `decoded_bytes` across the
   whole corpus. No writes.
2. Review totals. Confirm disk headroom on the media FS.
3. Apply: same pagination with `dry_run=false&confirmed_backup_gate=true`.
4. Verify `ingest-health` `media_repair_refs` rises then refs reach `present`.

## Build order

1. Run the backfill dry-run FIRST — if `candidate_refs` is ~0 across the corpus, there
   is little/no historical media to render and the renderer's value drops sharply.
   Gate the effort on real numbers.
   - 2026-06-26 partial dry-run: first 300,000 `source_lines` rows on hosted
     `david010` found 77 candidate refs, 21,318,153 decoded bytes, and 6
     rejected refs; sweep stopped at the explicit page limit. This is enough
     signal to build renderers before applying any write backfill.
2. Web renderer (thread model → render `<img>` from blob/thumb url, present-state gate).
3. iOS renderer (model → adapter → WebTranscriptPayloadItem → WebView `<img>`, solve
   the WebView auth wrinkle).
4. Apply backfill.

## Testing

- Web: `make test-frontend`; visual QA via the fixture-backed `ui-capture` loop per
  the zerg-ui skill (capture timeline with a media-bearing fixture, inspect each
  viewport).
- iOS: `make test-ios`; `ios/scripts/render-previews.sh` for any SwiftUI preview
  touched (the WebView render is harder to preview — may need a fixture document).
- Backfill: dry-run is its own safety; verify counts before apply.

## Resolved branch decisions

- Render `thumb_url` inline when present, fall back to `blob_url`, and link to
  `blob_url` for expand/open.
- iOS uses cookie-in-WKWebView auth rather than native-fetch-to-data-URL.
- Shared/read-only `share_token` views suppress media tiles until `/api/media/*`
  supports share-token authorization.

## Open decisions for review

- Backfill scope: whole corpus vs recent sessions only.
- Whether to add share-token authorization to `/api/media/*` before exposing media
  on public shared sessions.
