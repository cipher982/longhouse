# Media Data Plane

Status: Draft
Last updated: 2026-06-22

Related specs:

- `docs/specs/reliability-data-plane.md`
- `docs/specs/speed-of-light-shipper.md`
- `docs/specs/archive-backlog-repair.md`

## Context

Longhouse needs to support screenshots and other image artifacts across every
coding agent and harness we support: Codex, Claude Code, Antigravity, OpenCode,
browser automation, Computer Use, web composer, iOS composer, and future agent
tools.

The current system has two different image stories:

- Longhouse-originated Codex inputs already use a good shape: web/iOS uploads
  image bytes as multipart, the Runtime Host stores blobs with `sha256`, and
  the engine fetches the blob and passes Codex a local file path.
- Provider-imported history can still carry screenshots as inline
  `data:image/...;base64,...` strings inside transcript JSONL. Those giant
  strings are already mostly stripped from extracted event previews, but they
  remain in raw `source_lines` fidelity records and therefore enter the ingest,
  archive, dedupe, SQLite, and retry paths as megabyte-scale source-line text.

The June 22 dogfood incident exposed the mismatch. The macOS app reported:

```text
Live shipping healthy; archive repair draining
```

Local repair had two pending Codex rows:

| session | decoded image bytes | data URL chars | gzip wire bytes | retry state |
| --- | ---: | ---: | ---: | --- |
| zerg | 993,156 | 1,324,230 | 780,231 | server already archived it; local row kept retrying |
| g55 | 1,336,569 | 1,782,114 | 1,083,679 | still not fully durable on hosted |

At 100 Mbps symmetric, those uploads should take about 60-90 ms at line rate.
Even with TLS and HTTP overhead, 1 MB is not a 20-35 second operation. The
problem is not internet speed. The problem is that binary image bytes are being
treated as giant raw source-line text in a path that also gates ingest acks and
archive repair.

Hosted logs showed request timeouts, client disconnects, duplicate retries, and
WriteSerializer queue/exec pressure. One row had already committed durable
archive chunks on the server, but the client timed out before a useful ack and
kept retrying. That is a data-plane design failure: the server had the evidence,
but the local spool could not reconcile it.

## Problem

Images are first-class session evidence, but the current imported-history path
does not model them as first-class media. Inline base64 images create four
separate failures:

1. **Raw source-line inflation.** A 1 MB image becomes a 1.3-1.8 million
   character JSON string that is parsed, hashed, compressed, archived, and
   retried as source-line evidence, even when the extracted event preview is
   already small.
2. **Retry amplification.** If the ack is lost after a successful server
   commit, the local spool retries the same giant payload instead of cheaply
   proving that the host already has it.
3. **Provider inconsistency.** Longhouse-originated images use blob refs, while
   imported Codex screenshots use inline data URLs. Other providers and
   harnesses will each drift unless there is one media contract.
4. **Future UI and agent inefficiency.** Most views and tools need a thumbnail
   or a reference, not full image bytes in every session response.

This spec is not a third data plane. It extends the reliability data plane and
speed-of-light shipper:

- media bytes live behind the archive/media storage interface, not in a
  provider-specific side store;
- media work maps onto the shipper's existing L0-L4 lane model;
- media repair shares the shipper's spool/backpressure vocabulary instead of
  inventing a competing retry system.

## Product Principle

Longhouse must sync and back up images. It must not hide images, drop images,
or move them out of the product contract.

The speed-of-light design is:

> Pixels are durable media objects. Transcript events carry small media
> references.

Raw evidence remains sacred. But "raw evidence" does not mean "every hot
transcript row contains megabytes of base64." It means Longhouse can prove what
provider source line referenced which exact bytes, and can reconstruct or serve
those bytes later by content hash.

For image-bearing source lines, raw fidelity means:

```text
redacted source line with deterministic media placeholders
+ original source line hash
+ media object sha256 and exact bytes
+ source path / offset / provider provenance
= reconstructable source evidence without megabytes in the hot source-line row
```

The original inline data URL does not need to remain in the hot source-line row
as long as Longhouse can reconstruct the original from the redacted line and
the content-addressed bytes.

## Goals

- Preserve original image bytes exactly, with content-addressed integrity.
- Keep live transcript and source-line ingest small and predictable.
- Support all agent providers and harnesses through one `MediaRef` contract.
- Deduplicate repeated images across sessions and providers by `sha256`.
- Make image upload idempotent: retrying an already-present blob is cheap.
- Let UI, MCP, CLI, and future agents fetch thumbnails or full blobs on demand.
- Preserve source provenance: source path, offset, line hash, JSON pointer, and
  provider-specific kind.
- Keep the core Runtime Host SQLite-only and self-host friendly.
- Make missing media visible as repair state, not transcript ingest failure
  once the transcript reference is durable.
- Keep media storage tenant-scoped and authorization-bound in hosted mode.

## Non-Goals

- No cloud object-store requirement for core/self-host.
- No LLM calls in the media extraction or shipping hot path.
- No lossy conversion of originals. Thumbnails/previews may be derived, but the
  original bytes are stored separately.
- No provider-specific media stores.
- No separate media data plane that bypasses the existing archive/data-plane
  strategy.
- No hiding archive lag by dropping media refs.
- No broad searchable OCR/image understanding in this spec. That is downstream
  enrichment.

## Target Shape

```text
provider or harness output
  -> provider-neutral data URL extractor redacts inline image bytes
  -> provider adapter finds structured media-bearing fields
  -> media extractor writes original bytes to local content-addressed store
  -> source line and extracted event carry small MediaRef(s)
  -> transcript/source-line lane ships small JSON
  -> media work claims and uploads missing blobs by sha256
  -> Runtime Host stores media bytes and metadata
  -> UI / CLI / MCP / agents fetch thumbnail or full blob only when needed
```

The transcript/source-line hot path never carries inline image bytes.

## MediaRef Contract

All Longhouse-facing session projections should represent images with this
provider-neutral shape:

```json
{
  "type": "input_image",
  "media": {
    "sha256": "0123...",
    "mime_type": "image/png",
    "byte_size": 993156,
    "width": 768,
    "height": 1696,
    "thumb_url": "/api/media/0123.../thumb",
    "blob_url": "/api/media/0123.../blob",
    "state": "pending"
  }
}
```

Allowed `state` values:

- `present`: Runtime Host has the original bytes.
- `pending`: transcript ref is durable, media upload still pending.
- `missing`: source referenced media, but local extraction failed or the source
  file disappeared before upload.
- `failed`: media upload/extraction failed permanently and needs repair.

`state` is per reference/read projection, not global object truth. A deduped
blob may be present for one session and still referenced by a failed extraction
or repair item elsewhere.

Provider-native detail may live beside the normalized ref:

```json
{
  "provider": "codex",
  "provider_media": {
    "original_kind": "data_url",
    "json_pointer": "/payload/message/content/0/image_url"
  }
}
```

## Storage Model

### `media_objects`

One row per unique original blob.

```text
sha256 primary key
mime_type not null
byte_size not null
width nullable
height nullable
storage_path not null
thumbnail_sha256 nullable
created_at not null
first_seen_session_id nullable
```

For core/self-host, `storage_path` points under the Runtime Host data root:

```text
data/media/objects/sha256/01/23/0123....bin
data/media/thumbs/sha256/01/23/0123....webp
```

This path is resolved through the Runtime Host data/archive root configuration,
not a new independent root. Future hosted object storage can implement the same
logical store behind the existing archive store interface. It is not required
by the core product.

### `session_media_refs`

One row per reference from a transcript/source location to a media object.

```text
id integer primary key
session_id not null indexed
event_id nullable indexed
source_path nullable
source_offset nullable
source_line_hash nullable
json_pointer nullable
provider nullable
original_kind not null       -- data_url | local_file | screenshot | tool_result | attachment
media_sha256 not null indexed
media_state not null         -- pending | present | missing | failed | oversize
last_error nullable
created_at not null
unique(session_id, source_path, source_offset, media_sha256)
```

This row is the bridge between transcript provenance and bytes. It lets repair
ask "does the host already have the image referenced by this source line?"
without replaying the full source line. `json_pointer` is useful provenance,
but launch does not depend on it for uniqueness.

### Archive Integration

Archive chunks still preserve replayable source evidence. For image-bearing
source lines, the canonical archive record should contain a redacted source line
with deterministic media placeholders plus media refs. For forensic fidelity,
Longhouse may also preserve a cold compressed original source line, but that
original must not be required for timeline, detail, search, local health, or
normal transcript/source-line ingest.

If a provider line originally contained a data URL, the canonical archived
source record should include:

- source path
- source offset
- source line hash
- redacted source JSON with `MediaRef`
- media `sha256`
- original payload hash
- optional cold original source line pointer

This is the key raw-fidelity trade: exact pixels are preserved as media bytes;
the raw line is reconstructable from the redacted line and media objects.

## Runtime Host API

### Claim Missing Media

```http
POST /api/agents/media/claims
```

Request:

```json
{
  "items": [
    {
      "sha256": "0123...",
      "mime_type": "image/png",
      "byte_size": 993156,
      "session_id": "..."
    }
  ]
}
```

Response:

```json
{
  "needed": ["0123..."],
  "present": [],
  "rejected": []
}
```

This endpoint must be cheap. It should not parse transcript JSON or open cold
archive chunks.

### Upload Media

```http
PUT /api/agents/media/{sha256}
Content-Type: image/png
X-Agents-Token: ...
X-Media-Bytes: 993156
```

Behavior:

- Verify the request body hash equals `{sha256}`.
- Store atomically.
- Upsert `media_objects`.
- Return 200 if newly stored.
- Return 200 if already present.
- Treat `mime_type`, width, height, and filename as advisory metadata. A sha256
  match is success even when providers disagree about MIME labels.
- Reserve 409 for impossible or corrupt situations, such as a caller claiming
  the hash but sending bytes that hash differently.

### Read Media

Machine/browser routes can share the same service with different auth:

```http
GET /api/media/{sha256}/thumb
GET /api/media/{sha256}/blob
HEAD /api/media/{sha256}
```

Blob responses should stream from disk with `Content-Length`,
`Content-Type`, and `X-Media-Sha256`.

Hosted reads must be tenant- and session-authorized. A sha256 is not a bearer
capability. Browser/API callers can fetch a media object only if they can see at
least one owning `session_media_refs` row. Machine routes use the normal
`X-Agents-Token` and tenant boundary. Hosted dedupe is tenant-scoped unless a
future global store adds an explicit authorization index.

## Engine Local Store

The Machine Agent maintains a local content-addressed media store under the
existing agent data root:

```text
~/.longhouse/agent/media/
  objects/sha256/01/23/0123....bin
  meta/0123....json
```

Extraction writes to the local store before transcript shipping rewrites the
event to `MediaRef`.

Local metadata:

```json
{
  "sha256": "0123...",
  "mime_type": "image/png",
  "byte_size": 993156,
  "source_path": "...jsonl",
  "source_offset": 83552,
  "source_line_hash": "8372...",
  "provider": "codex",
  "original_kind": "data_url"
}
```

The media store is durable enough for retries. If local storage is unavailable,
the transcript event may still ship with `state=missing` plus the source-line
hash, but that must degrade local health as media repair debt.

Original media caps are separate from web/iOS composer ergonomics caps. For
imported/provider evidence, the default self-host cap should be generous enough
for modern full-screen screenshots, with an initial target of 32 MB per original
image. Oversize media is never silently dropped: the source line ships with an
`oversize` media state, byte count, hash if it can be computed, and a local
health repair item explaining what was not backed up.

## Shipping Lanes

Media extends the lane model from `speed-of-light-shipper.md`; it does not
replace it.

| Existing lane | Media behavior |
| --- | --- |
| L0 control | no media payloads |
| L1 live transcript | small normalized events and redacted source lines with `MediaRef`; no inline bytes |
| L2 live gap repair | recent missing transcript/source refs and media refs for active sessions |
| L3 archive repair | historical redacted source lines and historical media blobs, byte-budgeted |
| L4 enrichment | thumbnails, OCR, captions, embeddings, and other derived media work |

Important rules:

- Live transcript does not wait for full image upload if the local extractor has
  a valid hash and `MediaRef`.
- Live media repair is L2: higher priority than archive repair, lower than
  control and live transcript.
- Archive media uploads use byte budgets and host backpressure.
- A media upload timeout never forces replay of the transcript event if the
  transcript ref is already acked.

The local spool remains pointer-based for provider source files. Media upload
work can be represented as a first-class spool kind or a tightly coupled media
queue, but it must share the same lane scheduler, backoff vocabulary, retry
limits, and host pressure signals. The implementation must not create an
independent retry loop that can fight transcript repair.

## Provider Adapter Responsibilities

Adapters should detect media mechanically and leave judgment to clients/agents.

### Provider-Neutral Inline Data URLs

Any provider source line containing `data:image/*;base64,` over a small
threshold goes through the same media transform before provider-specific logic:

- Base64-decode once in the engine.
- Store original bytes by `sha256`.
- Replace the inline bytes in the raw source line with a deterministic
  `longhouse_media_ref` placeholder.
- Preserve source path, offset, line hash, provider, and the structured field
  location when available.

This catches Codex, Claude Code, OpenCode, and any future provider that emits
inline image data, without making Codex a permanent special case.

### Codex

- Detect structured `input_image.image_url` refs and normalize them to
  `MediaRef`.
- Use the provider-neutral data URL transform for inline images.
- Keep Longhouse-originated image inputs on the file-path boundary into Codex.

Longhouse-originated Codex inputs should migrate from `session_input_attachments`
to the shared media store. The Codex bridge should still fetch bytes and pass a
local file path to Codex. That part is already the right provider boundary.

### Claude Code

- Detect provider-native image/file/tool payloads that reference local image
  paths.
- Use the provider-neutral data URL transform for inline images.
- If the provider log references a local path, copy bytes into the local media
  store before the file disappears.
- Store a `MediaRef` plus provider-native provenance.

### Antigravity and OpenCode

- Add provider-specific extractors for screenshot, artifact, or local-file
  fields.
- Use the provider-neutral data URL transform for inline images.
- Normalize to `MediaRef`.
- Preserve the provider-native pointer in `session_media_refs`.

### Browser Harness and Computer Use

- Treat screenshots as media objects from capture time. They should never enter
  a transcript as base64.
- Tool output should include a `MediaRef`, dimensions, and a compact textual
  label. Full bytes should never be embedded into transcript JSON.
- Deduplicate consecutive identical frames by sha256.
- Apply capture-time budgets for repeated screenshots. When a loop exceeds its
  byte budget, keep transcript/tool metadata and surface visible media repair or
  capture budget state instead of silently dropping evidence.
- Generate a small thumbnail at capture/extraction time and upload it as a
  derived media object. The Runtime Host should remain a byte store on the hot
  path; expensive OCR/captioning stays in L4 enrichment.

### Web and iOS Composer

- Replace the delivery-only `session_input_attachments` lifecycle with shared
  `media_objects` rows.
- Keep multipart upload for user input.
- Keep client-side image compression for user ergonomics, but store whatever
  bytes the client actually sends as the original for that Longhouse input.
- During migration, disable the 24-hour reaper for any attachment promoted to
  durable session media. Delivery-only temporary blobs may still be reaped after
  terminal failure.

## UI and Agent Surfaces

Timeline cards:

- show thumbnails when present;
- show a small pending/missing indicator when media is not yet durable;
- never load full image bytes in the card list.

Session detail:

- render thumbnails inline;
- fetch full blob only when opened or copied;
- expose file metadata for agents.

MCP and `/api/agents/*`:

- return `MediaRef` objects in session tails/details;
- provide a small tool/API path to fetch a blob by hash when the agent decides
  pixels are relevant.

Search/recall:

- index nearby text and media metadata;
- do not embed raw base64;
- OCR/image captions are optional downstream enrichment, not source of truth.

## Retry and Reconciliation

The shipper needs idempotent media and transcript reconciliation.

Transcript reconciliation:

- If the Runtime Host already has `(session_id, source_path, source_offset,
  source_line_hash)`, local spool may mark that transcript range shipped.
- This reconciliation operates against the existing pointer spool: the local row
  points at a provider file byte range, and retry normally re-reads that range.
  Reconciliation lets repair retire the pointer without re-reading or
  re-uploading the megabyte source line when hosted already has the durable
  redacted source record.
- This fixes the observed "server committed, client timed out, local row keeps
  retrying" failure.

Media reconciliation:

- Before uploading, call `media/claims`.
- If hosted already has `sha256`, mark the local media work item complete.
- If a `PUT` times out, the next retry calls `claims` before sending bytes
  again.
- MIME/width/height disagreement does not fail repair when sha256 matches.
- Hash mismatch, missing local bytes, or oversize media becomes a per-reference
  repair state and local-health item.

Local health should distinguish:

```text
transcript repair: 2 ranges, 3.1 MB
media repair: 1 blob, 1.3 MB
last failure: host timeout after commit suspected; reconciliation available
```

"Archive repair draining" is too vague once media is split out.

## Timeout and Backpressure Contract

The previous incident had a bad timeout shape: server ingest could commit while
the client had already decided the request failed. The media design should make
that less harmful, and the transport should still be corrected.

Rules:

- Server request timeout must exceed the relevant client timeout, or the server
  must reject before doing expensive work.
- Media `PUT` should stream bytes and hash incrementally.
- Transcript ingest should reject inline data URLs over a small threshold once
  media extraction is available.
- Host backpressure should be typed and early:
  - `429/503` with `Retry-After`
  - no expensive JSON/archive work before rejecting archive/media bulk
- Responses should reuse the existing `X-Ingest-*` timing/header contract from
  `speed-of-light-shipper.md` and add bytes-accepted/media-lane fields only
  where they are missing.

## Migration Plan

### Phase 0: Stop the Bleeding

- Add spool reconciliation for already-durable source lines.
- Align client/server ingest timeouts.
- Improve local-health wording so stale retry rows do not masquerade as
  unexplained archive drain.

### Phase 1: Shared Media Store and Source-Line Redaction

- Add `media_objects` and `session_media_refs`.
- Add Runtime Host media claim/upload/read APIs.
- Add local engine content-addressed media store.
- Add redacted source-line representation for image-bearing raw lines:
  deterministic placeholder, original line hash, media sha256, and
  reconstructability tests.
- Add focused tests for idempotent upload, duplicate claims, hash mismatch, and
  blob streaming.

### Phase 2: Stop New Inline Base64

- Detect provider-neutral `data:image/*;base64` in engine JSONL parsing.
- Store decoded bytes locally.
- Ship normalized events and redacted source lines with `MediaRef` instead of
  inline base64.
- Add regression fixtures for Codex and OpenCode using 1-2 MB inline
  screenshots and assert the ingest request body stays small.

### Phase 3: Composer Unification

- Move `session_input_attachments` onto shared media objects.
- Keep Codex bridge file-path behavior.
- Remove 24-hour delivery-only cleanup semantics for media that is part of
  durable session history.

### Phase 4: Provider/Harness Coverage

- Add extractors for Claude Code, Antigravity, OpenCode, browser harness, and
  Computer Use.
- Each extractor gets fixtures proving the transcript carries refs and the
  media store has bytes.
- Add capture-time screenshot budgets and consecutive-frame dedupe for browser
  harness and Computer Use.

### Phase 5: Archive and UI Cutover

- Ensure archive chunks record normalized refs and media provenance.
- Update timeline/session detail/MCP responses to expose `MediaRef`.
- Reject large inline image data in `/api/agents/ingest` once all supported
  engines can redact and upload media.

### Phase 6: Opportunistic Backfill

- Backfill existing archived inline data URLs into media objects where feasible.
- This is optional, byte-budgeted, and must inherit the backup gate, restore
  validation, and disk-floor guardrails from `reliability-data-plane.md`.
- Backfill must never block live shipping or archive repair for new data.

## Success Criteria

- A 1.5 MB pasted screenshot does not create a >100 KB transcript/source-line
  ingest body.
- Re-uploading an already-present image by `sha256` performs no full-byte
  upload.
- Losing an ack after a successful media or transcript commit resolves on the
  next retry without replaying megabytes.
- Timeline/session detail can render thumbnails for Codex, web/iOS, and at
  least one non-Codex provider through the same `MediaRef` shape.
- Local health separately reports transcript repair and media repair.
- Search/detail endpoints never include base64 image data.
- Existing raw-source archive verification can still prove which source line
  produced which media object and can reconstruct the original image-bearing
  provider line when needed.

## Risks and Decisions

| Risk | Decision |
| --- | --- |
| Provider logs contain local file paths that disappear quickly | Copy into local media store during observation, before shipping |
| Transcript ships before media bytes reach hosted | Allow `state=pending`; surface media repair debt |
| Object store temptation adds deployment complexity | Keep filesystem-backed archive/media store as core; object storage only behind the existing archive interface |
| Dedup by hash hides provenance | Keep `session_media_refs` per reference even when `media_objects` dedupes bytes |
| Thumbnails become another lossy source of truth | Engine-generated thumbnails are derived/cache only; original `sha256` remains authoritative |
| Backfill old base64 is expensive | Backfill opportunistically and byte-budgeted after the reliability backup gate; do not block live product |
| Same sha256 arrives with different MIME labels | Treat sha256 as authority; MIME is advisory metadata |
| Imported media exceeds ordinary composer caps | Use a separate generous original-media cap and surface `oversize` repair state instead of dropping |

## Hatch Opus Review

Reviewed with Hatch Claude Opus on 2026-06-22. Key findings incorporated:

- The large bytes are primarily in raw `source_lines`, not extracted event
  previews. The spec now centers source-line redaction plus reconstructable raw
  fidelity.
- Media must extend `reliability-data-plane.md` and
  `speed-of-light-shipper.md`, not introduce a third store/lane/spool model.
- The current local spool is pointer-based; reconciliation must retire durable
  source ranges without replaying local file bytes.
- Object-level media rows should not own `pending/missing/failed`; those states
  belong on refs or repair work.
- sha256 is the integrity authority. MIME/dimension disagreement is metadata
  drift, not a hard conflict.
- Browser harness and Computer Use screenshots are likely the future volume
  driver and need capture-time media refs, dedupe, and budgets.
- Stop new inline base64 before attempting old archive backfill. Backfill must
  inherit the reliability data-plane backup gate.

## Open Questions

- How much cold original source-line preservation is needed once redacted source
  lines, media refs, source-line hashes, and media bytes are durable?
- Should missing media block "archive repair healthy" or appear only as a
  separate "media repair" health state?
- Should hosted tenants eventually dedupe media globally behind an
  authorization index, or keep dedupe strictly tenant-scoped forever?
