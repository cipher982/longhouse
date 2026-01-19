# Jarvis Real-Time Streaming Reliability

## Context + Post‑Mortem (What Happened)

Jarvis delivers “durable runs” progress to the browser as a real-time event stream (heartbeats, tool start/complete, worker complete, supervisor complete). The system largely worked, but refresh/reconnect behavior felt inconsistent:

- Refreshing `/chat` often “reconnected” in ~5–10s (perceived), even when frontend initialization finished quickly.
- Worker progress UI sometimes got “stuck” (e.g., “worker pending details / 1 worker running…”) even after the run completed.
- Logs were noisy (retries + warnings), making it hard to tell signal from noise.

### Root Causes (Primary Contributors)

#### 1) Proxy buffering mismatch (SSE is “just HTTP”)
Nginx does not inherently understand SSE; it applies behaviors based on which `location` matches the URL. Some Jarvis endpoints were explicitly configured as “streaming” (buffering off, long timeouts), but not all.

If a streaming endpoint falls through to a generic `/api/*` proxy block, nginx may buffer response output and delay delivery of the first bytes to the browser. That makes “reconnect” *feel* slow even when the app attaches quickly.

#### 2) Event ordering + incomplete identifiers (orphan workers)
In real-time streams, events can arrive in any order. Tool events often contain `workerId` immediately, while lifecycle events later add `jobId`. If the UI creates a placeholder item based on `workerId` and later completion arrives keyed by `jobId`, the UI must reconcile the two. If it doesn’t, you get stuck progress panels.

#### 3) Duplicate/brittle event delivery
Some events can be duplicated (multi-publish paths) and some payloads can accidentally contain non‑JSON types (e.g., `datetime`). Both lead to noisy or fragile streams (duplicates, or stream termination).

## Goals (From First Principles)

### UX Goals
- Refresh never loses progress; reconnection appears instant and self-healing.
- Progress UI converges to correct state regardless of event order or duplication.
- No manual “refresh until it works.”

### System Goals
- One canonical real-time channel with deterministic semantics.
- No proxy buffering surprises.
- Events are idempotent, schema-valid, and JSON-only.
- Observability for “time to first event”, reconnect rate, replay size, stream termination reasons.

## Non-Goals
- Minimizing work (this doc describes an ideal end state).
- Keeping nginx or SSE as hard requirements.

## Core Design Principle: App-Layer Reliability Beats Transport

Switching transports (SSE → WS → WebTransport) does **not** by itself solve durability. The durable part comes from:

- A per-run **event log** (or replay window)
- Monotonic **`event_id`** and **resume** semantics
- JSON-only, schema-validated payloads
- Client-side idempotent application of events (dedupe/upserts)

Transport choice should be driven by operational simplicity and environment support, not by hopes that it “fixes” reliability.

## Ideal Architecture Options

### Option A — Resumable SSE (Recommended for simplicity)
**Shape:** classic SSE, but “done correctly” with replay and `Last-Event-ID`.

- Canonical streaming route: `GET /api/stream/runs/{run_public_id}`
- SSE frames include `id: <event_id>`
- Client reconnect sends `Last-Event-ID: <event_id>`
- Server replays events from the log and then continues live
- Add `GET /api/runs/{run_public_id}/snapshot` for “state now” fallback

**Why it’s clean:**
- Streaming endpoints live under a single prefix (`/api/stream/*`), so nginx config is trivial (one location, buffering off).
- Works well with HTTP tooling and infra.

### Option B — WebSocket-only with resume (Recommended if you want one pipe)
**Shape:** everything real-time rides a single WS connection with subscribe/resume.

- One WS endpoint: `wss://.../api/ws`
- Protocol:
  - `subscribe_run {run_id, last_event_id}`
  - server: `snapshot` (optional), `events` replay, then live events

**Why it’s clean:**
- A single channel for all real-time (Jarvis + other app signals).
- No SSE proxy buffering pitfalls.

### Option C — WebTransport (HTTP/3 / QUIC) as an experimental fast path
**Reality check:** WebTransport is compelling for future capabilities (multiplexed streams, connection migration), but it does not remove the need for the app-layer protocol above, and it introduces real-world constraints:

- Safari/iOS support is still the gating factor in practice (so fallback is required).
- Nginx proxying/termination for WebTransport is not a given; you may need a different edge/proxy or terminate at the app edge.
- QUIC/UDP can be blocked in some networks; fallback is required.

**Best use of Option C:** additive “fast path” behind capability checks, *sharing the exact same app protocol* as Options A/B, with WS as the canonical fallback.

### Option D — Replace nginx with a streaming-first edge/proxy
If nginx becomes a repeated source of streaming footguns, consider Envoy/Traefik/Caddy or an edge platform that has strong HTTP/3 + streaming support. This is an infra-level choice; it doesn’t remove the need for the app-layer contract.

## The Contract (Applies to All Options)

### IDs
- Keep internal DB PKs as-is, but expose a stable `run_public_id` (UUID/ULID) in URLs and to the client.
- Every event includes:
  - `event_id` (monotonic per run)
  - `run_public_id`
  - `type` (enum)
  - `timestamp` (ISO 8601 string)
  - `payload` (JSON-only)

### Snapshot + Replay
- `GET /api/runs/{run_public_id}/snapshot` returns the authoritative current state.
- Stream resumes from `last_event_id`:
  - if log contains it → replay delta
  - if log pruned → return snapshot + replay from current window

### Idempotency + Ordering
- Client dedupes by `(run_public_id, event_id)`; can safely apply duplicates.
- Client tolerates out-of-order worker/tool lifecycle by keying state primarily on `worker_id` and mapping `job_id → worker_id` when available.

## Observability / SLOs

Track and alert on:
- `time_to_first_event_ms` after (re)subscribe
- reconnect count per session
- replay size on reconnect (`events_replayed`)
- stream termination reasons (server exceptions vs proxy vs client)

## Testing Strategy (Must-Have)

### E2E: “Refresh mid-run” regression
- Start a run that emits tool events.
- Refresh the page while it’s running.
- Assert:
  - stream reconnects
  - progress updates continue
  - worker completes
  - supervisor completes
  - UI converges (no stuck spinner / no phantom “running” worker)

### Chaos tests
- Duplicate events
- Tool events before worker started
- forced disconnect/reconnect loops

### Contract tests
- Validate every emitted payload is JSON-serializable and schema compliant.

## Recommendation (Cleanest Modern Solution)

If optimizing for reliability and clarity:

1) Implement the **event log + snapshot + event_id resume** contract.
2) Choose **Option A (resumable SSE)** or **Option B (WS-only)** as the canonical transport.
3) Make streaming URLs unambiguous (`/api/stream/*`) to eliminate proxy “special casing by regex.”
4) Optionally add **WebTransport as an experimental fast path** later, but only as a transport swap under a shared app-layer protocol.
