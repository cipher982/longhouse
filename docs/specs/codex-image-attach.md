# Codex image attach — working spec

**Status:** working draft for the image-attach-feature branch.
**Delete before merge:** yes (doc-sprawl rule).

## Goal

Web AND iOS composers can attach images to messages going to managed-local
Codex sessions. Coffee-shop ergonomics: paste a screenshot, hit send, agent
sees what you're looking at. Same as dragging an image into the local
terminal, but from anywhere.

## Provider scope

- **Codex** (`codex_app_server`, WS relay): full support.
- **Claude / opencode / antigravity**: capability-gated to "image attach not
  available on this provider" via the existing transport gate. Same UX
  pattern as today's text-send guard.

## The size problem (the actual user-journey risk)

Sources of attached images, by frequency:

| Source | Typical raw size | Risk |
|---|---|---|
| iPhone screenshot | 200 KB – 2 MB | Low |
| iPhone photo (HEIC) | 2 – 5 MB | Medium |
| macOS native screenshot (Cmd-Shift-4) | 1 – 3 MB | Low |
| macOS full-screen retina screenshot | **3 – 8 MB** | **HIGH — kills coffee-shop wifi** |

**Solution: client-side compression before upload, on every platform.**
Compression is non-negotiable. A naive web flow that uploads a 6 MB retina
PNG over coffee-shop 4G to a self-hosted home Mac mini is the user-journey
killer.

### Compression policy (web + iOS)

- Target: longest edge ≤ **1568 px** (matches the inner resolution most
  vision models downscale to anyway — anything larger is bandwidth waste).
- Output format: **JPEG quality 0.85** for photos, **PNG** preserved when
  the source has transparency.
- Soft cap: target output ≤ **1 MB**. If first encode exceeds, drop to
  quality 0.75 and re-encode once.
- Hard reject: original > 20 MB before compression (pathological case;
  rare; clearer error than silently OOM-ing).
- EXIF stripped naturally via canvas/Core Graphics re-encode.
- File metadata in upload: original filename, original byte size,
  compressed byte size — surfaced in telemetry so we can tune.

### Backend stays format-agnostic

Backend just stores what the client sent. No server-side compression. Server
limits: ≤ **2 MB per attachment** post-compression (with 100 KB headroom
above the soft client cap), ≤ **4 attachments per input row**.

## Why the engine integration is small

Codex's app-server protocol already accepts image attachments natively:

```rust
pub enum UserInput {
    Text { text, text_elements },
    Image { url },                  // remote http(s)
    LocalImage { path: PathBuf },   // local fs path — what we use
    Skill { ... },
    Mention { ... },
}
```

Both `turn/start` and `turn/steer` take `input: Vec<UserInput>`. The Machine
Agent runs on the same box as the `codex` binary, so we hand it a local
path and the codex binary handles loading. **No base64, no data URIs in
IPC.**

## End-to-end data flow

```
┌─────────────┐  POST multipart  ┌─────────────┐  command stamp  ┌──────────┐
│ Web/iOS     │ ───────────────▶│ Runtime Host│ ──────────────▶│ Machine  │
│ composer    │  text + files[] │             │  + attach refs  │ Agent    │
│ (compressed)│                 └──────┬──────┘                 └────┬─────┘
└─────────────┘                        │ stores blobs                │
                                       │ TTL 24h                     │ HTTP fetch
                                       ▼                             ▼
                              data/attachments/...           /tmp/lh-attach/
                                                             {session}/{uuid}.png
                                                                      │
                                                                      ▼
                                                              codex IPC
                                                              UserInput::LocalImage
```

## Modules (anti-sprawl discipline)

Each piece lives in its own file. SessionChat.tsx is already 1000 lines;
do not grow it.

| Module | Purpose |
|---|---|
| `server/zerg/models/session_input_attachment.py` | SQLAlchemy model |
| `server/zerg/services/session_input_attachments.py` | Lifecycle: create, fetch-by-id, cleanup-stale |
| `server/zerg/routers/session_inputs_attachments.py` | Multipart upload endpoint + machine-token blob fetch endpoint |
| `engine/src/codex_attachments.rs` | Fetch blob → tmpfile, sha256 verify, cleanup, build LocalImage items |
| `web/src/lib/imageCompression.ts` | Pure function: File → compressed Blob + metadata |
| `web/src/components/composer/AttachmentTray.tsx` | Thumbnail strip + remove × |
| `web/src/components/composer/useComposerAttachments.ts` | Hook: paste/drop/picker → state |
| `ios/Sources/Shared/ImageCompression.swift` | UIImage → compressed Data + metadata |
| `ios/Sources/LonghouseApp/Composer/AttachmentTray.swift` | Same role as web tray |

SessionChat.tsx grows by ≤ 50 lines: import the hook + tray, pipe through
multipart args. iOS Composer view: same.

## Pieces, in detail

### Backend (Runtime Host)

- New table `session_input_attachments`:
  - `id` (uuid pk)
  - `session_input_id` (fk, cascade)
  - `mime_type` (text)
  - `byte_size` (int)
  - `sha256` (text)
  - `blob_path` (text — relative to attach blob root)
  - `original_filename` (text, nullable)
  - `original_byte_size` (int, nullable)
  - `created_at`
- Schema migration via the existing `_auto_add_missing_columns()` path.
- `POST /sessions/{id}/inputs` becomes multipart (alongside existing JSON):
  - `text`, `intent`, `client_request_id` as form fields
  - `attachments` as files (zero or more)
  - Validates: file count ≤ 4, each ≤ 2 MB, mime in {png, jpeg, webp, gif}
  - JSON content-type still works (no attachments) — backwards compatible.
- `GET /api/agents/sessions/{sid}/inputs/{iid}/attachments/{aid}/blob`
  - Machine-token auth (`X-Agents-Token`)
  - Streams the blob bytes
  - 404 on missing/wrong session/wrong input — never leaks across sessions
- Blob storage: `data/attachments/{session_id}/{attach_uuid}.bin`. Self-host
  installs already manage `data/`. Hosted instance: this dir is in the
  Coolify volume mount alongside SQLite.
- Cleanup job: hourly, drops blobs whose parent `session_input` is in
  status=delivered/failed AND older than 24h. Foreign-key cascade also
  deletes blobs when a session_input is hard-deleted.
- `QueuedInputSummary` API gains `attachment_count: int`.

### Engine (Rust, longhouse-engine)

- `IpcCommand::TurnStart` and `IpcCommand::Steer` payload extended with
  optional `attachments: Vec<AttachmentRef>` where:
  ```rust
  struct AttachmentRef {
      id: String,         // attachment uuid
      mime_type: String,
      sha256: String,
      blob_url: String,   // RH https URL
  }
  ```
- New module `codex_attachments.rs`:
  - `fetch_attachments_to_tmp(session_id, attachments) -> Vec<PathBuf>`:
    - Authenticates with the existing machine token from shipping client
    - Writes to `/tmp/lh-attach/{session_id}/{uuid}.{ext}` (mode 0600)
    - Validates sha256 against the attachment row before yielding
    - Cleans tmpdir on engine shutdown and on `Stop` IpcCommand
  - `build_user_input_items(text, paths) -> Vec<Value>`:
    - Builds `UserInput::LocalImage { path }` items first, then the
      `UserInput::Text { text }` item — matches CLI's drag-drop ordering.
    - When `text` is empty AND attachments present, sends a single
      `UserInput::Text { text: "" }` text element so codex always has a
      conversational anchor (prevents some app-server edge cases).
- Existing `handle_ipc_turn_start` / `handle_ipc_steer` call the helpers
  before invoking `send_request_with_runtime`.

### Web (composer)

- `useComposerAttachments()` hook: file-picker open, paste handler on the
  composer textarea, drag-drop on the panel container.
- Compression runs in a Web Worker (off the main thread) so a 6 MB retina
  PNG doesn't freeze the composer for 500 ms.
  - `web/src/lib/imageCompression.worker.ts`
  - Falls back to inline compression if Worker init fails (Safari edge
    cases).
- `AttachmentTray` renders thumbnails as data-URLs (small, after
  compression — ≤ 1 MB total in memory worst case).
- Paperclip button: hidden when `session.capabilities.attach_images === false`.
- Send: when attachments present, FormData; otherwise existing JSON POST.

### iOS (composer)

- `ImageCompression.swift`:
  - `compress(image: UIImage, sourceFilename: String?) -> AttachmentPayload`
  - Uses `UIGraphicsImageRenderer` for resize, `jpegData(compressionQuality:)`
    for encode. PNG preserved only if image has alpha.
- Picker:
  - `PHPickerViewController` (multi-select up to 4)
  - "Take photo" via existing camera intent if any (otherwise PHPicker only
    is fine for this iteration)
  - Paste via `UIPasteboard` — supported in the existing composer textarea.
- Multipart upload through the existing session-input mirror.
- iOS does NOT deploy on push. David Xcode-builds when ready. Spec is
  explicit: iOS code lands in worktree, smoke test happens locally on
  David's phone.

## Capability gate

New field on `session_capabilities`:
```python
attach_images: bool  # True iff transport in {codex_app_server, codex_ws_relay}
```

Computed in `session_capabilities.py` from the existing transport check.
Surfaces as `session.capabilities.attach_images` on the API. Web and iOS
both check this before showing attach affordances.

## Telemetry

Three layers, lightweight:

1. **Client-side timings** (web + iOS), emitted through existing
   `/api/observability/client-event` (or equivalent — verify endpoint):
   - `image_attach.compress_ms` (per file)
   - `image_attach.compress_ratio` (compressed / original)
   - `image_attach.upload_ms` (multipart total)
   - `image_attach.attachment_count`
2. **Server-side**:
   - Structured log line at upload completion: bytes received, count, sha256.
   - Metric counter `session_input_attachments_uploaded_total{provider}`.
   - Metric histogram `session_input_attachments_blob_size_bytes`.
3. **Engine-side**:
   - Structured log on each `fetch_attachments_to_tmp`: blob URL, bytes
     fetched, fetch_ms, sha256 verify_ms.
   - Counter `engine_attachments_fetched_total{result}` where result is
     `ok|sha256_mismatch|http_error`.

These hook into existing observability paths — no new dashboards required
for v1, just searchable logs and counters.

## Errors and edge cases

| Case | Behavior |
|---|---|
| Pre-compression file > 20 MB | Reject in client with toast: "Image too large (max 20 MB before compression)" |
| Compressed blob > 2 MB after retry | Reject: "Image still too large after compression — try a smaller crop" |
| Network failure during upload | Standard fetch retry once with exponential backoff; on second failure, show inline error on the input chip with retry button |
| Engine fetch fails (network/404/sha mismatch) | IPC reply includes `attachment_fetch_failed`; UI shows failed-input chip with `last_error="attachment_fetch_failed"` (existing pattern) |
| User sends with 0 attachments | Identical to today's JSON path — no regression |
| Image format not in allowlist | Client rejects before upload with toast |
| Backend disk full on blob write | 500 with clear error code; client retries once; if still 500, surface "storage unavailable" (operator alert) |
| RH restarts mid-upload | Multipart fails cleanly; row not created; client retries |
| Engine restarts after blob fetch but before turn/start | tmpdir cleanup on startup wipes orphans; row gets re-fetched on retry |

## What we deliberately do NOT do

- Mid-turn image-only steer. Images ride on text input rows (turn/start or
  turn/steer). Adding a "send image with no text" button is a user-confusion
  trap and a separate provider verb.
- Image preview rendering in the timeline beyond the existing Codex
  `[image attached]` placeholder. Polish in a follow-up if it actually
  matters.
- Anti-virus, EXIF inspection beyond stripping via re-encode. Pre-launch,
  one user.
- A pluggable abstraction for future providers. Codex is the only provider
  with an enum-typed image input. When/if Claude grows one, we extend then.
- Server-side recompression. Trust the client; verify with sha256.
- Image search / "find that screenshot from last week." Not in scope.

## Smoke test plan

End-to-end, on David's machine:

1. **Web → Codex app_server**: paste a screenshot of an error log.
   Expect Codex to describe the error.
2. **Web → Codex WS relay**: same, different transport. Proves the
   IPC schema reaches the relay path.
3. **iOS → Codex**: take a screenshot on phone, attach in app, send.
   Expect same agent response.
4. **macOS retina full-screen**: 6 MB+ source PNG. Verify compression
   keeps it ≤ 1 MB upload, total UX < 3 s on home wifi.
5. **Capability gate**: launch a Claude session, verify paperclip is
   hidden and `attach_images=false` on the capability response. Same
   for opencode and antigravity.
6. **Failure injection**: corrupt the blob on RH between upload and
   engine fetch; verify failed-input chip surfaces cleanly.

Telemetry sanity check after smokes: verify all three layers emitted
events for at least one successful end-to-end flow.

## Sequence of commits in the worktree

1. **DONE** Spec (this file).
2. Backend: schema + multipart endpoint + machine-token blob endpoint + tests_lite.
3. Engine: IPC schema + LocalImage builder + Rust unit tests.
4. **Codex review pause** (hatch_codex on backend+engine diff).
5. Web: imageCompression.worker.ts + AttachmentTray + useComposerAttachments + SessionChat wiring + vitest.
6. iOS: ImageCompression.swift + AttachmentTray.swift + composer wiring.
7. Telemetry hookup across all three layers.
8. Smoke test (David).
9. **Final codex review pause** (hatch_codex on full diff).
10. Delete this spec.
11. PR → main → CI → merge → ship → dogfood-refresh. iOS: explicit
    Xcode-build prompt to David.
