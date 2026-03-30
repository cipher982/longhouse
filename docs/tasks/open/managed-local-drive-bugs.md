# Managed-Local Drive: Bug Fixes

Managed-local "drive" (injecting messages from web UI into live local sessions) is broken for both Codex and Claude paths. Root causes are distinct but converge on a shared architectural gap.

## Bugs

### Bug 1: Codex — No Context + Invisible to TUI

**Observed:** User asked "capital of france" in TUI, then injected "what about germany" from web UI. AI responded without any awareness of the France question. Message never appeared in the local Codex TUI.

**Root cause:** `codex_bridge.rs:cmd_codex_bridge_send()` opens a **fresh WebSocket** to the Codex app-server and issues `turn/start` with only the injected text. This is completely isolated from the bridge daemon's persistent connection that the TUI uses.

- Fresh connection = no conversation context carried forward
- Bypass of TUI = user never sees the injected message or response locally
- The AI responded because the app-server processed it independently; the bridge daemon forwarded events to Longhouse

**Key file:** `engine/src/codex_bridge.rs` lines 436-473

**Open question:** Does the Codex app-server share state between connected clients? If turns started on one client appear on another, daemon IPC is the right fix. If not, the fix changes shape. Must validate with multi-client canary first.

### Bug 2: Claude — "Completed locally. Transcript syncing..." Dead End

**Observed:** Channel injection worked (Claude answered "-3" in TUI), but Longhouse timeline shows placeholder "Completed locally. Transcript syncing..." that never resolves to the actual response.

**Root cause:** The SSE stream in `_stream_managed_local_output()` closes before events are available. The engine ships the transcript ~500ms-1s after Claude writes it, but the chat route's grace periods expire first. Once the SSE stream returns `sync_status: "pending"`, the frontend shows the placeholder and has **no mechanism** to discover when events actually land.

**Key files:**
- `server/zerg/routers/session_chat.py` lines 1144-1165 (pending return path)
- `web/src/components/SessionChat.tsx` lines 208-220 (dead-end placeholder)

### Bug 3 (Shared): Timeline Uses 5s Polling, Not Event-Driven Push

**Observed:** Even when events ship successfully, the timeline refreshes on a 5-second interval via React Query `refetchInterval`. This adds perceptible latency to everything and is architecturally wrong — the engine already ships events in real-time, but the last mile (API → browser) has no push channel.

**Key file:** `web/src/hooks/useSessionWorkspace.ts` line 19 (`WORKSPACE_RUNTIME_REFRESH_MS = 5_000`)

## Fixes (Revised after review)

### Step 0: Codex Multi-Client Canary

**What:** Before building daemon IPC, validate whether the Codex app-server shares turn state across connected clients. Extend the existing canary/E2E harness to answer:
1. Do turns started on one client (bridge `send`) appear in the other (TUI)?
2. Does the daemon see TUI-originated turns?

**Test seams:** `engine/src/codex_app_server_canary.rs:335`, `scripts/qa/test-codex-bridge-e2e.sh:325`

**Outcome:** If shared → daemon IPC is correct. If isolated → need a different approach (possibly `turn/steer` or single-owner model).

### Step 1: Session-Scoped SSE Stream

**What:** A per-session SSE stream that fires on **any session-visible mutation**, not just ingest. Reuse the existing timeline SSE pattern (`server/zerg/routers/timeline.py:268`) and frontend EventSource stack (`web/src/services/api/agents.ts:410`). Use the existing per-session signature helper (`server/zerg/services/agents_store.py:1814`) for lightweight change detection.

**Signals on:** ingest writes, presence/runtime updates, session mutations (park/archive/loop-mode), continuation creation.

**Not:** A bespoke WebSocket. Not ingest-only. The right primitive is "session-workspace changed."

**Frontend:** `useSessionWorkspace` connects to SSE stream, invalidates React Query on `session_changed` event. Keep 5s polling as reconnect fallback only.

**Scope:**
- Backend: New SSE endpoint (or extend existing timeline stream with session-scoped mode). Emit from ingest, presence flush, session mutation endpoints.
- Frontend: `useSessionWorkspace.ts` subscribes to SSE, `refetchInterval` becomes fallback.
- Reuse: `connectTimelineSessionsStream()` pattern, `list_session_window_signature()` preflight.

### Step 2: Split Chat Semantics (Managed-Local vs Cloud)

**What:** SessionChat currently uses the same SSE streaming path for both managed-local drive AND cloud continuation. These need different semantics:

- **Managed-local:** POST returns fast ack with `request_id` + local acceptance status (turn started, not just "runner command exited 0"). Response arrives via session SSE stream (Step 1). No inline streaming.
- **Cloud continuation:** Keep existing inline SSE delta streaming — it works and is the right UX for server-side inference.

**"Accepted" means:** The local session started the turn (verified via runtime/presence signal from `managed_local_control.py:503`), not just that the runner dispatch succeeded.

**Scope:**
- `server/zerg/routers/session_chat.py`: Managed-local path returns JSON ack, not SSE stream. Keep `_stream_managed_local_output` verification of turn acceptance, delete everything after (polling, force-ship, grace periods).
- `web/src/components/SessionChat.tsx`: Mode split. Managed-local shows "Sent" + relies on session stream. Cloud keeps inline SSE handler.
- Delete: `_force_managed_local_claude_sync`, `_await_managed_local_turn_events`, sync placeholder logic, `getSyncPendingPlaceholder()`.

### Step 3: Codex Bridge Fix (Pending Step 0 Results)

**If multi-client state is shared (expected):**
- Add Unix socket IPC endpoint on the bridge daemon (`cmd_codex_bridge_run`)
- `cmd_codex_bridge_send()` routes through daemon IPC instead of fresh WebSocket
- Single-owner queue behind the socket: if a turn is active, decide explicitly between queueing and `turn/steer` — don't let concurrent writers race
- Bridge state file gets `daemon_url` field

**If multi-client state is isolated:**
- Different approach needed — TBD based on canary results

### Step 4: Instrument Pipeline Latency (After correctness)

**What:** Before shaving fixed delays, instrument the full pipeline: `submit → runner ack → local phase signal → transcript ship → ingest commit → browser stream → paint`.

**Known fixed delays to audit:**
- Codex 150ms pre-ship sleep (`codex_bridge.rs:1021`, `DEFAULT_SHIP_DELAY_MS`)
- Engine 1s outbox drain poll (`shipper/hooks.py:11`)
- Claude managed-local grace periods (`session_chat.py:109-112`)

Only remove/reduce these after instrumentation confirms they're bottlenecks, not load-bearing.

## Order of Operations

1. **Step 0** — Codex canary (fast, answers a design question)
2. **Step 1** — Session SSE stream (foundation)
3. **Step 2** — Split chat semantics (depends on Step 1)
4. **Step 3** — Codex bridge fix (depends on Step 0 results)
5. **Step 4** — Latency instrumentation (after correctness)

Steps 0 and 1 can run in parallel. Steps 2 and 3 can run in parallel after their dependencies.

## Key Files

| Step | Files |
|------|-------|
| 0 | `engine/src/codex_app_server_canary.rs`, `scripts/qa/test-codex-bridge-e2e.sh` |
| 1 | New SSE endpoint or extension of `routers/timeline.py`. Modified: `services/agents_store.py` (emit on ingest), `routers/agents_sessions.py` (emit on mutations), `hooks/useSessionWorkspace.ts` (SSE subscription). Reuse: `services/api/agents.ts` EventSource pattern. |
| 2 | `routers/session_chat.py` (managed-local → JSON ack), `components/SessionChat.tsx` (mode split), `services/managed_local_control.py` (keep acceptance verification, delete post-ack orchestration) |
| 3 | `engine/src/codex_bridge.rs` (daemon IPC + send reroute) |
| 4 | Instrumentation across pipeline — no structural changes |
