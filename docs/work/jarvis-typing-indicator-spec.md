# Jarvis “Typing” Indicator Spec (Option C: Two-Step Confirm)

## Status

- **Implemented:** Yes (as of 2025-12-18)
- **Open items:** None

## Goal

Make the assistant “typing” UI feel instant and trustworthy, without conflating it with worker/progress UI:

- **Assistant typing indicator** belongs **in the transcript** as the assistant’s next message bubble.
- **Worker progress** (delegation/tools/workers) belongs **at the top** (sticky) and should only appear when real delegation begins.

This spec defines a **two-step confirm** flow:

1. **Immediate placeholder** bubble (optimistic, on Enter)
2. **Confirmed typing** dots only after server confirms work started / message accepted

## Non-Goals

- Changing worker progress UX (modal/top panel) beyond ensuring it’s decoupled from typing.
- Designing final visuals (CSS details can evolve independently).

## Definitions

### Assistant Turn (UI object)

Represents “the assistant’s next response” as a single bubble that progresses through states.

Minimum fields (conceptual):

- `id`: stable identifier
- `role`: `"assistant"`
- `content`: string
- `status`: `"queued" | "typing" | "streaming" | "final" | "error" | "canceled"`
- `correlationId`: ties this bubble to the user’s message / backend run (required to avoid duplicates)
- `createdAt`, `updatedAt`

### Worker Progress (secondary surface)

A separate UI surface anchored at the top of the chat that shows:

- delegation started (`worker_spawned`)
- worker/tool lifecycle

Worker progress is **not** the source of truth for whether the assistant is “typing”.

## State Machine

### States

- `queued`: placeholder bubble exists, but no dots yet (subtle/neutral)
- `typing`: dots visible in the assistant bubble
- `streaming`: assistant content streaming into the same bubble
- `final`: assistant content complete
- `error`: terminal error state (bubble shows error + retry affordance)
- `canceled`: terminal user-canceled state

### Events (conceptual)

These are logical events; wire them to actual app events (SSE/stateManager/etc.).

- `USER_SEND`: user pressed Enter / submitted message
- `SEND_ACK`: app confirmed user message accepted (HTTP 200 / SSE ack / run created)
- `RUN_STARTED`: backend started processing (optional if `SEND_ACK` is strong)
- `ASSISTANT_TOKEN`: first token (or first streaming delta) received
- `ASSISTANT_FINAL`: assistant finished message
- `RUN_ERROR`: terminal error for the run
- `USER_CANCEL`: user clicked stop/cancel
- `WORKER_SPAWNED`: delegation began (controls worker progress UI only)

### Transitions

#### On message submission

- `USER_SEND`:
  - Create assistant placeholder bubble in `queued`
  - Start a **max timeout** timer (60s) → `error` if no progress (and abort the stream)

#### Confirmed typing

- `SEND_ACK` or `RUN_STARTED` (pick the earliest reliable signal):
  - `queued` → `typing`
  - Dots visible in the assistant bubble

#### Start streaming

- `ASSISTANT_TOKEN`:
  - `queued|typing` → `streaming`
  - Stop dots immediately
  - Begin rendering streaming text into the same bubble

#### Finish

- `ASSISTANT_FINAL`:
  - `streaming` → `final`
  - Clear any timers

#### Error/cancel

- `RUN_ERROR`:
  - `queued|typing|streaming` → `error`
  - Stop dots/streaming
  - Show retry UI (re-send the user message or re-run)
- `USER_CANCEL`:
  - `queued|typing|streaming` → `canceled`
  - Stop dots/streaming

### Worker progress behavior (orthogonal)

- `WORKER_SPAWNED`:
  - Show worker progress UI (top sticky)
  - This does **not** change assistant bubble state directly

## Key UX Rules

1. **There is always exactly one assistant bubble per assistant response.**
   - No “global typing indicator” separate from the transcript.
2. **The assistant bubble exists immediately** on `USER_SEND` to eliminate uncertainty.
3. **Dots only appear after confirmation** (`SEND_ACK` / `RUN_STARTED`) to avoid “fake typing”.
4. **First token wins**: as soon as streaming begins, dots disappear and content streams in the same bubble.
5. **Worker progress is additive** and only appears when delegation happens (`WORKER_SPAWNED`).

## Correlation / De-duplication Requirements

To avoid duplicated bubbles, the system needs a correlation strategy:

- Generate a `clientCorrelationId` at `USER_SEND`.
- Send it with the request to backend.
- Backend echoes it on all related events (ack/start/token/final).
- The UI updates the existing placeholder bubble by `clientCorrelationId` instead of appending new assistant messages.

If backend echo isn’t available, a fallback can be used (e.g. “most recent pending assistant bubble”), but it is less robust and can break with parallel sends.

## Timing Defaults

- `queued` should be visually subtle (empty bubble, shimmer, or minimal placeholder).
- Dots should appear only after `SEND_ACK`/`RUN_STARTED`.
- Timeout: 60s recommended before flipping to `error` (tune based on worst-case runs).
- Watchdog should be **“petted”** on any evidence of liveness (e.g. `connected`, progress events, and `heartbeat`).

## Testing Checklist

- Sending a message immediately creates an assistant placeholder bubble.
- Dots do not appear until `SEND_ACK`/`RUN_STARTED`.
- Dots appear in the assistant bubble (not in the top progress UI).
- On first token, dots disappear and text streams into the same bubble.
- Worker progress UI remains top/sticky and only appears after worker spawn.
- Multiple rapid sends create multiple placeholders and reconcile correctly.
- Errors and cancels terminate the bubble and clear dots.

## Mapping to Current Jarvis Events (Implementation Notes)

This section documents suggested wiring to existing Jarvis web events as of December 2025.

### Actual wiring (implemented)

- `USER_SEND` → frontend creates assistant placeholder with `status='queued'` and a `clientCorrelationId`.
- `SEND_ACK` → **SSE `connected` event** (server sends `run_id` + echoes `client_correlation_id`) transitions `queued → typing`.
- `RUN_STARTED` / “still alive” → `supervisor_started`, `supervisor_thinking`, and `heartbeat` pet the watchdog and keep `typing` live.
- `ASSISTANT_TOKEN` → first streaming/content update transitions to `streaming` (same bubble).
- `ASSISTANT_FINAL` → message finalizes into `final` (same bubble).
- `USER_CANCEL` (multi-send abort) → previous bubble is explicitly marked `canceled` before aborting the previous SSE stream.
- `RUN_ERROR` or watchdog timeout → bubble becomes `error` and the stream is aborted.

### Likely event sources

- **Supervisor lifecycle (delegation + run):** `apps/jarvis/apps/web/lib/event-bus.ts`
  - `supervisor:started` → `RUN_STARTED` (good candidate for “confirmed typing” if it fires reliably/quickly)
  - `supervisor:worker_spawned` → `WORKER_SPAWNED` (top progress UI)
  - `supervisor:complete` → terminal completion (can be used as a fallback to stop typing)
  - `supervisor:error` → `RUN_ERROR`

- **Assistant streaming content:** `apps/jarvis/apps/web/lib/state-manager.ts`
  - `STREAMING_TEXT_CHANGED` → treat first non-empty update as `ASSISTANT_TOKEN`
  - `MESSAGE_FINALIZED` → `ASSISTANT_FINAL`

### Recommended “confirm” signal choice

Prefer the earliest reliable signal:

1. `SEND_ACK` (explicit “message accepted” ack from backend) — best if available
2. `supervisor:started` — acceptable if it’s emitted immediately upon accepting the user message

Avoid using `supervisor:worker_spawned` as confirmation; it only fires for delegated tasks.

### Correlation requirement (current gap)

The current event shapes shown above do not carry a client correlation id for:

`USER_SEND` → `RUN_STARTED` → `ASSISTANT_TOKEN` → `ASSISTANT_FINAL`

To implement Option C robustly (especially for multi-send / parallel sends), add a `clientCorrelationId`
that is generated on `USER_SEND`, included in the request payload, and echoed back on all related events.

If correlation cannot be added immediately, a temporary fallback is:

- allow only one “pending assistant bubble” at a time, and update the most recent pending bubble on
  `STREAMING_TEXT_CHANGED`/`MESSAGE_FINALIZED`

This fallback is simpler but will break if the user can send multiple messages before the first completes.
