# Cursor Agent Transcript Format

**Status:** reverse-engineered source-format reference; legacy ingest notes below are superseded
**Owner:** Longhouse
**Last updated:** 2026-06-30

Targeted by `server/zerg/services/cursor_transcript.py` as a local diagnostic
decoder. The former Python upload paths are removed: they discarded unknown
source material and targeted retired ingest. Current implementation status and
the native Rust storage-v2 design live in
`docs/specs/cursor-storage-v2-source-fidelity.md`.

## Scope and non-goals

- In scope: decoding a finished or in-flight Cursor agent session from disk
  into ordered Longhouse events with tool-call/result pairing.
- Out of scope (v1): the older chunked format used by pre-2026 `composer-1`
  sessions (see "Legacy format" below). Managed `longhouse cursor` control.

## Where sessions live

Per session, keyed by a stable `conversationUuid` (Cursor calls it `agentId`):

```
~/.cursor/chats/<workspaceHash>/<conversationUuid>/
    meta.json          # {schemaVersion, createdAtMs, title, updatedAtMs}
    store.db           # SQLite content-addressed blob DAG (the real transcript)
    store.db-wal       # may exist for a live session
    store.db-shm
    prompt_history.json

~/.cursor/projects/<workspaceSlug>/agent-transcripts/<conversationUuid>/
    <conversationUuid>.jsonl   # LOSSY stream; do not use as archive source

~/.cursor/ai-tracking/ai-code-tracking.db
    conversation_summaries      # (conversationId, title, tldr, overview, model, mode, updatedAt)
```

The `agent-transcripts/*.jsonl` is a lossy projection: it contains only
`user`/`assistant` messages with `text` and `tool_use` blocks plus
`{"type":"turn_ended","status":"success"}` markers. It has **no `tool_result`
blocks, no timestamps, no reasoning blocks, no `tool_call_id`.** It is not the
archive source. `store.db` is.

## store.db schema

```sql
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB);   -- id = sha256 hex of data
```

`meta` row `key='0'` holds a **hex-encoded JSON** string (decode `bytes.fromhex(value)`)
with the session root metadata:

```json
{
  "agentId": "<conversationUuid>",
  "latestRootBlobId": "<64-hex sha256>",
  "name": "<session title>",
  "mode": "default" | "auto-run" | ...,
  "isRunEverything": true,
  "approvalMode": "unrestricted" | ...,
  "createdAt": <epoch ms>,
  "lastUsedModel": "glm-5.2" | "claude-opus-4-8" | "composer-2.5" | ...
}
```

Additional optional keys seen: `lastDebugServerPort`. Older sessions may omit
`isRunEverything`/`approvalMode`.

## Blob graph (current format, cursor-agent >= ~2026)

There are two blob kinds in the current format:

### 1. Snapshot node (protobuf)

The root is `latestRootBlobId`. It is a protobuf message with these fields:

| Field | Wire | Meaning |
| --- | --- | --- |
| `1` | 2 (len-delim, 32 bytes) | **repeated**: ordered list of message-blob ids = the transcript order. This is the load-bearing field. |
| `3` | 2 (32 bytes) | repeated ids; observed on longer sessions. Likely branch/subagent or tool-result dedup refs. Preserve, do not drop. |
| `5` | 2 | small metadata blob (session summary / state). |
| `8` | 2 (32 bytes) | repeated ids; parent/branch-head chain. |
| `9` | 2 | workspace. Newer sessions: a 32-byte id/hash; older sessions: `file://<path>` text. |
| `10` | 0 (varint) | flag. |
| `13` | 2 (32 bytes) | repeated ids. |
| `15` | 2 (len-delim) | repeated: per-turn context metadata as nested protobuf (`field 1` = workspace-related file path strings). Not required for ingest. |
| `18` | 2 (1 byte) | flag. |

A naive protobuf wire parser (varint + length-delimited; stop cleanly on
unknown wire types) is sufficient to extract `field 1`. We do **not** need
`protoc` or a `.proto` file.

### 2. Message blob (pure JSON)

Each id in the snapshot node's `field 1` list resolves to a blob whose bytes
are a **complete UTF-8 JSON message object** (`json.loads` succeeds directly).
The first byte is `{` (0x7b), which is also why a careless protobuf parser
misreads these as `field 15, wire type 3` (start-group) — that is a trap, not
the real encoding.

Message shapes:

```jsonc
// system
{"role":"system","content":"You are a powerful agentic AI coding assistant..."}

// user (one per user turn)
{"role":"user",
 "content":[{"type":"text","text":"<user_query>...</user_query>"}],
 "providerOptions":{"cursor":{"requestId":"<uuid>"}}}

// assistant (one per assistant turn; id is the turn index as a string)
{"role":"assistant","id":"1",
 "content":[
   {"type":"reasoning","text":"...","providerOptions":{"cursor":{"modelName":"claude-opus-4-8-thinking-high"}},"signature":"..."},
   {"type":"text","text":"..."},
   {"type":"tool-call","toolCallId":"toolu_01GLRGMX1w5qQFRLXeHJigGm","toolName":"Shell","args":{"command":"...","description":"..."}}
 ]}

// tool (one per tool result; id == toolCallId)
{"role":"tool","id":"toolu_01GLRGMX1w5qQFRLXeHJigGm",
 "providerOptions":{"cursor":{"highLevelToolCallResult":{"output":{...}}}},
 "content":[{"type":"tool-result","toolCallId":"toolu_01GLRGMX1w5qQFRLXeHJigGm","toolName":"Shell","result":"Exit code: 0\n..."}]}
```

Block types observed: `text`, `reasoning`, `tool-call`, `tool-result`.
Cursor uses **hyphenated** `tool-call` / `tool-result` (not Anthropic's
`tool_use` / `tool_result`). `redacted-reasoning` also appears (reasoning
withheld by the provider); preserve as reasoning with a redacted marker.

### Tool-call / tool-result pairing

Clean and durable. An assistant `tool-call` block carries `toolCallId`
(`toolu_<base32>`). The next `tool` message's `tool-result` block carries the
same `toolCallId`, and the `tool` message's top-level `id` also equals it.
**No synthetic id synthesis is required** for the current format (unlike
Antigravity). Pair on `toolCallId` directly.

Multiple `tool-call` blocks may appear in one assistant message; each produces
its own `tool` message, in the same order, immediately after the assistant
message in the `field 1` list.

## Reconstruction algorithm (current format)

1. Open `store.db` read-only and **WAL-aware** (`file:...?mode=ro`, URI mode) so
   an in-flight cursor-agent's WAL is read (WAL readers don't block the writer).
   Fall back to `immutable=1` only for a cold store whose `-shm` is gone/locked.
   Do not attempt to checkpoint.
2. Read `meta['0']`, hex-decode, `json.loads` → session metadata
   (`agentId`, `createdAt`, `lastUsedModel`, `name`, `mode`,
   `approvalMode`, `isRunEverything`, `latestRootBlobId`).
3. Parse the root blob protobuf; collect `field 1` ids **in order**.
4. For each id, fetch the blob and `json.loads` it. If it is not valid JSON
   with a `role` key, this is the legacy chunked format — emit a typed
   `unsupported_gap` and stop (see Legacy format).
5. Emit canonical Longhouse events in `field 1` order (see mapping below).
6. Title resolution: `meta.json.title` → `meta[0].name` →
   `ai-tracking.conversation_summaries.title`.

## Canonical event mapping

| Cursor | Longhouse canonical |
| --- | --- |
| session `agentId` | provider session id |
| `meta.createdAt` / `meta.json.updatedAtMs` | session start / last-updated timestamps |
| `meta.lastUsedModel` | session model |
| `meta.name` / `meta.json.title` | session title |
| `field 9` workspace (`file://` or via project slug) | workspace path |
| `user` message `content[].text` | user message event |
| `user.providerOptions.cursor.requestId` | provider turn/request id (preserve) |
| `assistant` message `content[].text` | assistant text event |
| `assistant` `content[].type=reasoning` | reasoning event (typed; do not drop; honor `redacted-reasoning`) |
| `assistant` `content[].type=tool-call` `toolCallId`/`toolName`/`args` | tool_use event (call id = `toolCallId`, name = `toolName`, input = `args`) |
| `tool` message `content[].type=tool-result` `toolCallId`/`result` | tool_result event paired by `toolCallId` |
| `tool.providerOptions.cursor.highLevelToolCallResult` | preserve as provider metadata; do not collapse into result text |

## Timestamps and ordering (reduced fidelity)

**There are no per-message timestamps** in the current format. Ordering comes
entirely from the root `field 1` list order. Absolute time is limited to:

- session start: `meta.createdAt` (epoch ms)
- session last-updated: `meta.json.updatedAtMs` (epoch ms)

### Timestamp-source spike (2026-06-30)

A spike checked every on-disk Cursor surface for a usable per-event clock so
synthesis could be avoided or anchored. Findings:

- **`agent-transcripts/*.jsonl`** — no timestamps anywhere (the only non-message
  line is `{"type":"turn_ended","status":"success"}`). It is also strictly
  lossier than `store.db`: only `user`/`assistant` roles, only `text` +
  `tool_use` blocks, no `tool_result`, no reasoning, no `tool_call_id`, and
  fewer total messages than the corresponding `store.db` (96 lines vs 125
  message blobs in the sample session). **No benefit to merge — reject.**
  `store.db` is a strict superset.
- **`store.db` message blobs** — scanned all JSON keys; no timestamp-like
  field anywhere. Root protobuf snapshot nodes also carry no per-event time.
- **`~/.cursor/ai-tracking/ai-code-tracking.db` → `ai_code_hashes`** — the
  ONLY on-disk source of real per-event epoch-ms timing. It logs every
  AI-generated code hash with `conversationId`, `timestamp`, `createdAt`,
  and a `requestId` of form `fc_<uuid>_0`. Sample session: 4767 rows but
  only **58 distinct timestamps** across a ~14.3h span, and they fire only
  on code-writing turns (many turns produce none). Its `requestId` namespace
  does **not** map to the bare-UUID `providerOptions.cursor.requestId` on
  user messages, so the join to individual messages is fuzzy at best.
- **Live append** — `store.db` runs in WAL mode and `agent-transcripts/*.jsonl`
  is appended during an active session. A live tailer can stamp wall-clock
  observation time per observed event for **interactive TUI** sessions.
  Caveat (verified 2026-06-30): `--print` headless mode **batches the entire
  session to disk at exit** — `store.db` and the JSONL both appear whole at
  session end, so there is nothing to tail mid-run for headless launches.
  Interactive WAL writes are also flush-delayed (per-turn, not per-event), so
  observation time is a coarse lower bound, not the true event time.
- **`stream-json` is the real per-event clock** — `cursor-agent --print
  --output-format stream-json` emits `timestamp_ms` (epoch ms) on every
  `assistant` and `tool_call` event, and `duration_ms` on `result` events
  (result_end = call_ts + duration_ms). This is the only true per-event
  absolute timing Cursor exposes anywhere. It lives on the live stream
  (the managed-control transport), **not on disk**.
- **`store.db` tool blocks carry duration, not timestamps** — tool-result
  blobs include `providerOptions.cursor.highLevelToolCallResult.output.success
  .executionTime` and `.localExecutionTimeMs` (per-tool wall duration in ms).
  No absolute start/end. Usable to tighten synthetic spacing (known-width
  gaps) but not to anchor events in time.

### Decision

1. **Do not merge the JSONL.** It adds no timing and drops data.
2. **Baseline stays synthetic monotonic** across `[createdAt, updatedAtMs]`,
   clearly marked reduced-fidelity. Acceptable for historical backfill where
   precise inter-event spacing rarely matters.
3. **Upgrade synthesis to burst-aware when `ai_code_hashes` is available**
   (optional, low priority): cluster events around the distinct anchor
   timestamps for the `conversationId` rather than uniform spread — a
   14.3h session is idle-heavy and uniform spread is badly wrong. Treat as
   a future enhancement, not a launch dependency: separate DB, fuzzy join,
   and only helps code-heavy sessions.
4. **The only real per-event clock is `stream-json`, which is headless-only.**
   `cursor-agent --print --output-format stream-json` emits `timestamp_ms` on
   `assistant`/`tool_call` events and `duration_ms` on `result` events — but
   `--print` is one-shot/headless with no interactive TUI, so it cannot serve
   the managed interactive `longhouse cursor` contract (see Phase 4). For
   unmanaged interactive sessions, tailing `store.db-wal` gives coarse
   flush-delayed observation time — a fallback only. Historical backfill
   remains synthetic.

Longhouse must flag cursor sessions as **reduced timestamp fidelity** on the
timeline: inter-event spacing within a session is unknown. Do not fabricate
per-event timestamps. If absolutely required for ordering in the Longhouse
event model, distribute events uniformly across `[createdAt, updatedAtMs]` as
a clearly-marked synthetic monotonic sequence — but prefer carrying
"order-only" through the ingest layer if the model allows.

## Subagents / threads

`field 3` and `field 8` id lists on the snapshot node suggest branch/subagent
heads, and `tool-call` blocks may spawn nested conversations. v1 ingests the
**linear `field 1` transcript only** and treats nested threads as an open
gap; nested subagent transcripts (if any) are a Phase-2 investigation.

## Legacy format (pre-2026, composer-1) — NOT supported in v1

Older sessions (e.g. `composer-1`, late 2025) store message content as
**chunked protobuf group blobs** (`field 15`, wire type 3 start-group) where
the message JSON is split across repeated `field 4` length-delimited fragments
interleaved with varint length markers. This is a custom framing, not
standard protobuf, and the JSON braces collide with group start/end tags.

Detection: a `field 1` message blob that does not `json.loads` to an object
with a `role` key → legacy format.

v1 behavior: emit a typed `unsupported_gap` with reason
`cursor_legacy_chunked_format` and skip the session. Do not attempt partial
decode. Revisit only if David needs historical pre-2026 cursor sessions.

## Schema drift

The protobuf snapshot schema and the JSON message shapes are undocumented and
may change between cursor-agent releases. The decoder must be tolerant:

- Unknown JSON block types: preserve as a typed `unknown_block` event with the
  raw block, surfaced as a yellow review item (universal-harness evidence
  rule). Never crash.
- Unknown snapshot fields: ignore; do not fail the parse.
- Missing `meta['0']` or missing `latestRootBlobId`: emit a typed
  `unsupported_gap` (`cursor_missing_root`) and stop.

## Open questions for Phase 1 implementation

- Confirm `field 3` / `field 8` semantics across sessions with known
  subagent usage. If they are subagent thread heads, decide whether to ingest
  nested transcripts or mark them as separate linked sessions.
- Confirm whether any session has image/media blocks in `tool-result` or
  user `content` (base64 or blob ref). If so, route through the existing
  media-refs path rather than inlining.
- Decide final Longhouse representation for `reasoning` blocks (preserve as
  first-class events vs. fold into assistant text with a marker). Prefer
  first-class to match the universal-harness `raw_evidence` rule.

## Product-mode note

This document only defines Cursor transcript storage and reconstruction. Current
Shadow, Helm, and Console behavior is owned by `AGENTS.md`, the managed-provider
contract, and `docs/specs/turn-scoped-console-execution.md`; old remote-launch
and ACP exploration notes were removed so they cannot be mistaken for a
supported product path.
