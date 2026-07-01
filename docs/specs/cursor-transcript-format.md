# Cursor Agent Transcript Format

**Status:** Reverse-engineered from on-disk sessions (cursor-agent `2026.06.26`)
**Owner:** Longhouse
**Last updated:** 2026-06-30

Targeted by `server/zerg/services/cursor_transcript.py` (unmanaged ingest).
This documents what Cursor durably stores per agent session and how Longhouse
decodes it into canonical events. It is intentionally narrow: unmanaged
ingest only. Managed control is a separate, later phase.

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

## Provider contract registration

Cursor **is** registered in `schemas/managed_providers.yml` and the generated
`server/zerg/config/managed_provider_contracts.json` for **Console mode only**
(`managed_transport: cursor_acp`, `control_plane: cursor_acp`, `cursor_exec`
retained as a legacy alias). `provider_cli_binary: cursor-agent`,
`provider_cli_env: LONGHOUSE_CURSOR_BIN`. Advertised machine-control supports:
`cursor.run_once`, `cursor.resume_run_once`. `terminate` is a capability
(kill_on_drop + SIGINT exits cursor-agent, verified) but `cursor.terminate` is
not advertised in `machine_control_supports` until a pid-registry terminate
command is wired (same precedent as codex one-shot exec).

The ingest path (`/api/agents/ingest` → `AgentsStore.ingest_session`) does not
call `require_contract_for_provider`, so unmanaged Shadow ingest works
independently of the contract entry. `SessionIngest.provider` accepts
`"cursor"` as a free-form string.

## Discovery and live-binding status

Three layers, landed explicitly:

- **local_health discovery** — DONE. `_collect_cursor_discovery` in
  `server/zerg/services/local_health.py` scans `~/.cursor/chats` via
  `iter_local_cursor_session_summaries` and surfaces every cursor-agent
  session in the local-health JSON as an unmanaged row
  (`control_path=unmanaged`, `liveness_model=transcript`,
  `state=detached`), with a `legacy_format` flag. Read-only; never breaks
  local-health. Skipped on the fast path.
- **Historical backfill import** — DONE. `longhouse cursor import` scans
  `~/.cursor/chats`, decodes each `store.db` through
  `cursor_transcript.decode_store_db`, and POSTs the canonical
  `SessionIngest` to the Runtime Host `/api/agents/ingest` endpoint with the
  stored device token. `--dry-run` decodes without contacting the server.
  Re-runs are idempotent via event-hash dedupe.
- **Engine live unmanaged binding** (`engine/src/unmanaged_bindings.rs`) —
  DEFERRED. Making discovered cursor sessions auto-appear on the timeline
  without a manual import requires a Rust engine change (SQLite read support
  in the engine, or tailing the lossy `agent-transcripts/*.jsonl`). Frozen as
  an explicit gap; `longhouse cursor import` is the path to timeline presence
  until then.

## Managed Cursor — Shadow / Console / Helm

Per the Shadow / Helm / Console vocabulary (see `AGENTS.md`), Cursor ships in
two of the three session modes today; Helm is the open one.

- **Shadow** (unmanaged, live, observe-only): engine tails `~/.cursor/chats`
  `store.db` and ships events via `cursor_transcript.py`. Built.
- **Console** (managed, headless, UI-driven): user launches a Cursor task from
  Longhouse web/iOS; the Runtime Host dispatches `session.run_once` to the
  Machine Agent, which spawns `cursor-agent acp` and drives an ACP JSON-RPC
  turn. **Built (cursor_acp).** See below.
- **Helm** (managed, interactive, remote-steerable `longhouse cursor`): the
  user runs `longhouse cursor` and gets their normal interactive TUI
  invisibly while Longhouse owns a background control channel for send /
  interrupt / terminate from browser/iOS. **Not built — planned via direct
  TTY injection** (de-risked by prior art; see "Helm path" below).

### Console mode — ACP (cursor_acp)

Contract: `schemas/managed_providers.yml` `provider: cursor`,
`managed_transport: cursor_acp`, `control_plane: cursor_acp`,
`launch_local: false`, `launch_remote: true`, `run_once: true`,
`can_resume: true` (via `session/load` + `session/prompt`). Advertises
`cursor.run_once` and `cursor.resume_run_once`. Mid-turn `send_input` /
`interrupt` / `steer_active_turn` are explicit unsupported gaps — Console is
turn-batched, not mid-turn steerable. `session/cancel` is "Method not found"
on cursor, so terminate is cleanup-on-drop (`kill_on_drop`) until a
pid-registry terminate command is wired.

Flow mirrors codex `run_once`:

1. Web/iOS → `POST /api/sessions/launch` `{provider:"cursor",
   execution_lifetime:"one_shot", initial_prompt, cwd, device_id}`.
2. `remote_session_launch.launch_remote_session` pre-allocates the session +
   run, dispatches `session.run_once` over the control WebSocket. The one-shot
   `SessionConnection.control_plane` is `cursor_acp` (derived via
   `ONE_SHOT_CONTROL_PLANE_BY_PROVIDER`).
3. Engine `control_channel::handle_command_frame` `COMMAND_RUN_ONCE` dispatches
   by provider → `cursor_acp::start_cursor_acp_once`.
4. `cursor_acp.rs` spawns `cursor-agent acp` in its own process group,
   `kill_on_drop`, env `LONGHOUSE_MANAGED_SESSION_ID`, returns pid/argv
   upstream immediately (server flips connection → `attached`).
5. Background ACP JSON-RPC handshake over stdio:
   - `initialize` `{protocolVersion: 1 (NUMBER — cursor rejects strings),
     clientCapabilities, clientInfo}`.
   - `session/new` `{cwd, mcpServers: []}` → `sessionId` (or `session/load`
     `{sessionId}` for a resume turn).
   - `session/prompt` `{sessionId, prompt: [{type:"text","text":<prompt>}]}` →
     streams `session/update` notifications until the prompt response
     (`{stopReason: "end_turn" | ...}`).
6. `session/update` notifications → `EventIngest` rows → posts `SessionIngest`
   to `/api/agents/ingest` with the managed session id, plus runtime
   phase/progress/terminal signals to `/api/agents/runtime/events/batch`.

### ACP session/update mapping

- `agent_message_chunk` → `EventIngest(role=assistant, content_text =
  update.content.text)`.
- `agent_thought_chunk` → `EventIngest(role=assistant, kind=reasoning,
  content_text)`.
- `tool_call*` start variants → `EventIngest(role=assistant, tool_name,
  tool_input_json, tool_call_id)` (provisional — exact Cursor variant names
  not yet captured by a tool-using live canary).
- `tool_call*` result/complete/end variants → `EventIngest(role=tool,
  tool_name, tool_output_text, tool_call_id)`.
- `available_commands_update` / other variants → progress signal only (no
  transcript event).

**Timestamp fidelity:** ACP notifications carry **no per-event timestamps**
(verified by live probe). Every event uses a monotonic receipt clock. Do not
fabricate per-event timestamps beyond receipt time. (The earlier stream-json
path had real `timestamp_ms` on tool calls; ACP does not — a fidelity
regression on tool-call timing, accepted for the cleaner control surface.)

### Helm path — direct TTY injection (de-risked, planned)

`cursor-agent`'s interactive TUI exposes no steerable control surface (no
`--remote` / `--attach` / `--socket` / `--app-server`; verified against the
installed binary). Unlike Claude (`--dangerously-load-development-channels`),
Codex (`app-server` + `--remote`), and OpenCode (`serve` + `attach`), Cursor
did not build remote-control into its TUI binary. `cursor-agent acp` is a
**headless** stdio mode — a separate process, not a control channel layered
onto the live TUI — so ACP gives Console, not Helm.

The remaining Helm path is **direct TTY injection** (not PTY-wrapping):
`longhouse cursor` spawns `cursor-agent` as a foreground child inheriting the
user's **real terminal** (pristine TUI, no re-render layer), registers
`session_id + tty_device + pid` with the engine/Runtime Host, and the engine
injects input by writing bytes to that TTY device. This is de-risked by prior
art, not assumed:

- **Cross-process TTY write works on macOS without root or Accessibility
  permission** — a separate process opening `/dev/ttysXXX` and writing
  delivers bytes to the foreground process group's stdin (verified by spike:
  `cat` and `cursor-agent` both received injected text).
- **The Ink submit quirk is named and worked around.** Programmatic `\r` /
  `\n` into an Ink text input creates a newline instead of submitting —
  autocomplete intercepts Enter (anthropics/claude-code#15553). The reliable
  fix is `text → sleep 0.3 → Escape → sleep 0.1 → Enter`. The 0.3s gap is
  load-bearing. This is an Ink TUI behavior, so it applies to cursor-agent
  (Ink-based) identically.
- **tmux is the wrong layer; direct PTY/real-TTY is right.** Modern Claude
  Code refuses all submit keystrokes when stdin is a tmux pane (18 encoding
  variants tried, all fail) but `pty.fork() + \r` with no tmux works
  (anthropics/claude-code#52812). This validates inheriting the real TTY
  (or a direct pty.fork) over a tmux wrapper.
- **Interrupt** = SIGINT to the pid (verified: SIGINT exits cursor-agent).
  Ctrl-C *byte* (0x03) does not interrupt — the TUI traps it. **Terminate** =
  SIGKILL. Mid-turn graceful steer is not available (no surface).

Scope of Helm-via-injection: **send / interrupt / terminate**. Not mid-turn
steer. The hard engineering risk is **quiescence detection** — knowing when
the agent is idle (safe to type) vs mid-turn. Shadow's `store.db` tail gives
turn-commit; mid-turn busy detection needs screen-state inference (prior art:
`tui-use` / `headless-terminal` use a VT-emulator + render-debounce;
`claude-interactive-sdk` uses transcript tail + a Stop hook).

Prior art for the architecture: `Finndersen/claude-interactive-sdk` drives
Claude Code's interactive TUI via PTY + `tmux paste-buffer`/`send-keys`,
reads state via JSONL transcript tail + a `Stop` hook, `C-c` for interrupt —
same shape, same motivation (keep usage on the subscription/TUI, not headless
API). They use tmux; we inherit the real TTY (per #52812, tmux breaks on
modern versions).

Helm is **not** a hard stop — it is a planned Phase 2 build gated on a live
interactive send test with David (the one step that needs a human at a real
terminal).
