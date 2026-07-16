# Codex Live Transcript Item Preview

Status: Active design note
Last updated: 2026-06-03

## Problem

iOS should read like the user's terminal: a sequence of visible transcript
items. A long Codex turn can contain many assistant commentary messages, and
each one is a separate visible item in the terminal and in the durable archive.

Today the Codex live lane collapses that shape. The bridge accumulates every
`item/agentMessage/delta` in a turn into one `live_text` string, the Runtime
Host stores that string in `session_live_previews`, and iOS renders it as one
synthetic assistant event. During high-frequency commentary this produces
strings like:

```text
Still healthy.Now uploading Contracts file 5.Contracts file 5 finished.
```

The durable transcript is not corrupt. The bug is that the live preview model
uses the wrong unit.

## Product Goal

Live and durable transcript rendering must share one visible primitive:

```text
TranscriptItem {
  role
  text/tool metadata
  stable source identity
  timestamp
  completion/provisional state
}
```

The UI should not care whether an item is live provisional or durable. A live
item appears quickly, updates in place if more text arrives for the same item,
and is replaced by the durable item with the same source identity when ingest
catches up. No duplicate rows, no paragraph blob, no visible jump.

## Desired User Experience

During the upload session, iOS should show:

```text
Contracts file 4 is 70% uploaded. Still healthy.

Now uploading Contracts file 5, overall file 66 of 144.

Contracts file 5 finished; file 67 of 144 started.
```

Each line above is a separate assistant transcript item. Styling may be compact
for short status updates, but item boundaries must be readable.

## Current Gap

### Durable lane

Codex JSONL and Longhouse durable `AgentEvent` rows already preserve the right
shape:

```text
assistant event A
assistant event B
assistant event C
```

### Live lane

The live path currently preserves only one session-level string:

```text
turn live_text = A + B + C
```

That loses item boundaries and can also re-render text that is already durable.

## Target Architecture

```text
Codex app-server notification
        |
        v
Codex bridge derives LiveTranscriptItemDelta
        |
        v
Runtime Host materializes live transcript item projection
        |
        v
Timeline/mobile-tail returns durable items + live provisional items
        |
        v
iOS TimelineBuilder renders all items with the same row model
```

## Contract

Codex already provides the identity we need:

```text
item/started              item.id, item.type = agentMessage
item/agentMessage/delta   itemId, delta
item/completed            item.id
```

The Codex bridge should preserve that provider item identity and emit a live
transcript event with:

```json
{
  "progress_kind": "bridge_live_transcript_delta",
  "thread_id": "thread-id",
  "turn_id": "turn-id",
  "item_id": "provider item id",
  "item_seq": 3,
  "delta": "text chunk",
  "item_text": "current text for this visible item",
  "turn_completed": false
}
```

Rules:

- `item_id` identifies one visible assistant transcript item. Prefer the
  provider `itemId`; synthesize only for older/edge Codex notifications that do
  not include one.
- `item_text` is the current snapshot for that visible item, not the whole turn.
- `item_seq` is monotonic within an `item_id`.
- The server may retain a bounded number of live items per session.
- Durable rows supersede live rows by source identity and timestamp.
- If Codex does not provide a provider item id, the bridge may synthesize one,
  but it must prefer preserving user-visible boundaries over turn-level blobs.

## Implementation Path

1. Bridge: track live assistant item identity separately from active turn
   identity. Replace the turn-sized `live_transcript_text` snapshot with a
   current-item snapshot. Continue emitting `live_text` as a compatibility
   field, but make it equal to the current item text.
2. Bridge coalescing: include `item_id` in the live transcript coalescing key so
   multiple assistant items in one turn are not collapsed before they leave the
   engine.
3. Server: keep `session_live_previews` as one row per session for now. Its job
   is to show the current live tip, not the entire turn. Store/load current
   visible item text and carry `item_id` in the turn key/cursor so replacing an
   item is intentional.
4. API/iOS: keep the existing `transcript_preview` compatibility shape. iOS
   already renders it as one synthetic assistant event and suppresses it when a
   matching durable assistant event arrives. Once the bridge emits one item per
   preview, iOS needs no punctuation repair or alternate renderer.

Longer-term per-item API projection remains possible, but it is not required for
this bug. The durable rows already carry earlier messages; the live preview only
needs to represent the current item that has not caught up durably yet.

## Regression Cases

- Multiple Codex commentary messages in one turn must not concatenate into one
  preview. A new `item_id` starts a fresh current-item preview.
- A live preview must not re-render assistant text already present in the
  durable tail.
- Token streaming for one assistant item should still update one row in place.
- A turn completion snapshot should not overwrite a clean current-item preview
  with a whole-turn cumulative blob.
- Out-of-order or duplicate live observations must keep the newest item seq.
- A command that completes too quickly to emit an output-delta notification
  must still render from `item/completed.item.aggregatedOutput`.
- A failed command is a failed tool item even when the enclosing turn later
  completes successfully.

## Non-Goals

- Do not change durable archive semantics for existing sessions.
- Do not make iOS parse punctuation to recover message boundaries.
- Do not hide the issue with CSS spacing alone.
- Do not introduce a second transcript renderer for iOS.
