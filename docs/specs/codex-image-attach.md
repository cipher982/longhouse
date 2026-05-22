# Codex image attach — working spec

**Status:** working draft for the image-attach-feature branch.
**Delete before merge:** yes (doc-sprawl rule).

## Goal

Let the web composer attach images to messages going to managed-local Codex
sessions, so David can screenshot from a coffee shop and have Codex see what
he's looking at — same as dragging an image into the local terminal.

Codex only this iteration. Claude/opencode/antigravity get capability-gated
"image attach not available on this provider" via the same pattern that
already gates text send.

## Why this is small

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
Agent runs on the same machine as the `codex` binary, so we hand it a local
path and the codex binary handles loading. **No base64, no data URIs in IPC.**

## Data flow

```
┌─────────────┐  POST multipart  ┌─────────────┐  command stamp  ┌──────────┐
│ Web         │ ───────────────▶│ Runtime Host│ ──────────────▶│ Machine  │
│ composer    │  text + files[] │             │  + attach refs  │ Agent    │
└─────────────┘                 └─────────────┘                 └────┬─────┘
                                  stores blobs                        │
                                  TTL 24h                             │ HTTP fetch
                                                                      ▼
                                                              /tmp/lh-attach/
                                                              {session}/{uuid}.png
                                                                      │
                                                                      ▼
                                                              codex IPC
                                                              UserInput::LocalImage
```

## Pieces

### Backend (Runtime Host)

- New table `session_input_attachments`:
  - `id` (uuid pk)
  - `session_input_id` (fk)
  - `mime_type` (text)
  - `byte_size` (int)
  - `sha256` (text)
  - `blob_path` (text — relative to attach blob root)
  - `created_at`
- `POST /sessions/{id}/inputs` becomes multipart:
  - `text` field (required)
  - `intent` (auto/queue/steer)
  - `client_request_id`
  - `attachments` (zero or more files)
  - Validates: file count ≤ 4, each ≤ 5 MB, mime in {png, jpeg, webp, gif}
- New `GET /api/agents/sessions/{id}/inputs/{input_id}/attachments/{attach_id}/blob`
  - Machine-token auth (`X-Agents-Token`)
  - Streams the blob bytes to the engine
- Blob storage: `data/attachments/{session_id}/{attach_uuid}.bin` on the
  Runtime Host filesystem. Self-host installs already manage `data/` for SQLite.
- Cleanup: hourly job drops blobs whose parent `session_input` is in
  status=delivered/failed AND older than 24h. Belt + braces: foreign-key
  cascade on session_input row delete.
- `QueuedInputSummary` API gains `attachment_count: int`.

### Engine (Rust, longhouse-engine)

- `IpcCommand::TurnStart` and `IpcCommand::Steer` payload extended with
  optional `attachments: Vec<AttachmentRef>` where:
  ```rust
  struct AttachmentRef {
      id: String,         // attachment uuid
      mime_type: String,
      blob_url: String,   // signed URL or RH path
  }
  ```
- New helper `fetch_attachments_to_tmp(session_id, attachments) -> Vec<PathBuf>`:
  - Authenticates with the existing machine token already used for shipping
  - Writes to `/tmp/lh-attach/{session_id}/{uuid}.{ext}`
  - Validates sha256 against the attachment row before yielding
  - On success, builds `UserInput::LocalImage { path }` items and prepends them
    to the existing `UserInput::Text { text }` item
- Lifecycle: tmpfile cleanup on `Stop` IpcCommand and on engine shutdown.

### Web (SessionChat.tsx)

- Paperclip button next to the existing dock controls
- Hidden `<input type="file" accept="image/png,image/jpeg,image/webp,image/gif" multiple>`
- Paste handler: intercepts `ClipboardEvent` items with `kind === "file"` and
  `type.startsWith("image/")`
- Drag-drop on the messages area (whole panel becomes drop target while a file
  drag is in progress)
- Thumbnail strip above the textarea (chip-style: 64px square + filename + ×)
- `postSessionInput` becomes multipart when attachments are present
- Capability gate: only show paperclip when
  `session.capabilities.attach_images === true` (new field, true only for
  codex_app_server transport this iteration)

### iOS

Out of scope this iteration. iOS gets capability-gated to "not supported" and
ships when David is ready to do an Xcode build for it.

## Capability gate

New field on `session_capabilities`:
```python
attach_images: bool  # True iff transport is codex_app_server
```

Computed in `session_capabilities.py` from the existing transport check.
Rendered in `display_label` is **not** changed — keep the existing strings.

## Errors and edge cases

- Attachment blob fetch fails on engine → bridge surfaces "attachment unavailable"
  in the IPC reply; UI shows the failed input row with `last_error="attachment_fetch_failed"`,
  same UI pattern as existing failed-input chips.
- User sends 0 attachments → behaves identically to today (no regression risk).
- Sha256 mismatch on engine → treat as fetch failure; do not retry.
- Image too large → 413 from upload endpoint; UI shows toast "Image too large
  (max 5 MB)" before send.

## What we deliberately do NOT do

- Mid-turn image steer. Both providers accept images on user messages, so they
  ride along with whatever text the user is sending. If the user adds an image
  while the agent is mid-turn, that's a queued or steer input that already has
  an explicit text component — the image just rides on the same row.
- Image preview in the timeline beyond what Codex itself renders today (parser
  already handles input_image and emits `[image attached]` placeholder text).
  We can polish the timeline rendering in a follow-up.
- Anti-virus scanning, EXIF stripping. David is the user; he can paste a
  screenshot. Pre-launch.

## Smoke test plan

1. **Codex app_server**: paste a screenshot of an error log. Codex should
   describe the error.
2. **Codex WS relay**: same input, same expected behavior — proves the IPC
   schema reaches the relay path too.
3. **Claude / opencode / antigravity**: verify paperclip is hidden and
   `attach_images=false` on the capability response.
4. **Failure injection**: send with a corrupt blob (manually edit on RH);
   verify failed-input chip surfaces.

## Sequence of commits in the worktree

1. Spec (this file).
2. Backend: schema + multipart endpoint + tests_lite.
3. Engine: IPC schema + LocalImage builder + Rust tests.
4. **Codex review pause.**
5. Web: composer UI + multipart client + vitest.
6. Smoke test (Codex sessions on David's machine).
7. **Final codex review pause.**
8. Delete this spec.
9. PR → main → merge → ship → dogfood-refresh.
