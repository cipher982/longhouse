# Hot/Cold SQLite Live-Control Closeout

## Alpha Premise

Longhouse has no external users yet. There is no installed base to protect and
no reason to preserve legacy live-control contracts for compatibility. The goal
is to finish the architecture cleanly before launch, not stage a conservative
enterprise rollout.

Dogfooding is not a monitoring plan. David should be able to use the app
normally and only know whether it works or does not work. If we need evidence
after dogfooding, Longhouse must emit logs, counters, health facts, and a simple
check command/reminder that can answer the question mechanically.

## Product Goal

Live/control actions must be hot-first:

- Launch readiness, runtime state, managed control, and user control inputs use
  the Live Store as operational truth.
- Archive SQLite remains historical truth and provenance, but archive writes,
  repair, ingest backlog, WAL/checkpoint pressure, and transcript projection
  must not block live-control ACKs.
- A user sending or queueing text into a managed session should not care whether
  archive SQLite is healthy at that instant.

The short version:

> Live Store is the control plane. Archive SQLite is an append-only provenance
> log. The only bridge is durable, retryable outbox projection.

## Current State

Already complete:

- Live Store exists as a separate SQLite lane.
- Runtime state, live session index, control leases, managed machine-control
  operations, launch readiness, live previews, archive outbox, and live input
  receipts exist in the hot lane.
- `/input auto` "send now" can ACK from a hot live receipt with `input_id:
  null`, then project the archive `SessionInput` later.
- Web and iOS tolerate nullable archive `input_id`.

Still incomplete:

- `queue` and locked `auto` still use archive `SessionInput.id` as queue
  identity.
- Queue list, cancel, drain, retry, and failure chips are archive-first.
- There is no merged live/archive queued-input view.
- There is no 24-hour mechanical check for duplicate sends, stuck live inputs,
  projection lag, or archive pressure effects.

## Target Architecture

### Control Identity

`live_input_id` becomes the primary identity for all user-authored live-control
inputs:

- `auto`
- `queue`
- `steer`
- locked `auto` that becomes queue-next
- retry of a failed text input
- cancel of a queued input
- drain of the next queued input at a turn boundary

`input_id` remains nullable provenance. New web/iOS code must not need it for
live control.

### Live Input Receipt Lifecycle

Live receipt statuses:

- `queued`: accepted as future work
- `delivering`: currently being dispatched
- `delivered`: accepted by the provider/control channel
- `cancelled`: user cancelled before dispatch
- `failed`: dispatch/projection failed in a user-visible way

Required fields:

- `id`
- `owner_id`
- `session_id`
- `thread_id`
- `provider`
- `device_id`
- `client_request_id`
- `intent`
- `status`
- `text`
- `archive_session_input_id`
- `control_command_id`
- `delivery_request_id`
- `error_json`
- `created_at`
- `updated_at`
- `expires_at`

### API Shape

`POST /api/sessions/{session_id}/input`

- Always returns `live_input_id` for JSON text input when Live Store is
  configured.
- May return `input_id` only when an archive projection already exists.
- `auto` with an available lock:
  - create hot receipt as `delivering`
  - dispatch
  - mark hot receipt `delivered`
  - ACK immediately
  - project archive in background
- `queue` or locked `auto`:
  - create hot receipt as `queued`
  - ACK immediately
  - no archive dependency
- `steer`:
  - create hot receipt as `delivering`
  - dispatch
  - mark delivered or failed
  - no silent queue fallback

`GET /api/sessions/{session_id}/inputs`

- Returns live queued/delivering/failed receipts only.
- Response items include both `live_input_id` and nullable `id`.
- The UI must key queued chips by `live_input_id` when present.

`DELETE /api/sessions/{session_id}/inputs/live/{live_input_id}`

- New primary route.
- Cancels only `queued` live receipts.
- Returns conflict if the receipt is already delivering/delivered/cancelled.

### Queue Drain

At terminal turn boundary:

- Claim oldest `queued` live receipt for the session.
- Atomically move it to `delivering`.
- Dispatch exactly once.
- Mark `delivered` on provider/control-channel acceptance.
- Mark `failed` on non-retryable failure.
- Requeue or leave retryable state explicitly.
- Project archive `SessionInput` and link `SessionTurn` in background.

The drain contract must be idempotent by `live_input_id` and
`client_request_id`.

### Archive Projection

Archive projection is outbox-only:

- Never blocks ACK for text input.
- Never blocks cancel.
- Never blocks queue list.
- Never blocks drain.
- Runs through `LiveArchiveOutbox`, not fire-and-forget request tasks.
- Updates `archive_session_input_id` on the live receipt when successful.
- Links projected `SessionInput` to `SessionTurn` for provenance.
- Emits lag/failure metrics.

Attachments are out of scope for this closeout and may remain archive-first
until there is a hot media receipt model.

## Monitoring And Binary Checks

David should not manually monitor. The product/runtime should expose enough
state for an agent to answer:

> Did hot-control inputs work correctly since the last deploy?

Instrumentation to keep:

- Counter: live input receipts created by intent/status.
- Counter: live input dispatch accepted/failed/cancelled.
- Gauge: queued live receipts older than N minutes.
- Gauge: delivering live receipts older than N minutes.
- Gauge: live receipts missing archive projection older than N minutes.
- Counter: archive projection success/failure.
- Counter: duplicate client_request_id dedupe hits.
- Log fields on every control input:
  - `live_input_id`
  - `client_request_id`
  - `session_id`
  - `intent`
  - `status`
  - `archive_session_input_id`
  - `dispatch_request_id`

Do not add repo-local one-off scripts or Makefile targets for this alpha
dogfood check. A future agent should query live runtime state directly
(`db doctor --json`, targeted SQL, and container logs as needed), then produce a
fresh binary summary:

- `PASS`: no stuck queued/delivering receipts, no projection failures, no old
  missing projections, no duplicate dispatch evidence.
- `FAIL`: print the exact sessions/live input ids and the reason.

The reminder belongs outside the repo as user/task state. It should remind
David to ask an agent to inspect hot-control health; it should not rely on a
checked-in black-box script.

## Success Criteria

The epic is complete only when all are true:

1. `auto`, `queue`, `steer`, locked queue-next, cancel, and drain use
   `live_input_id` as control identity.
2. Archive SQLite write pressure cannot prevent text-input ACK, queue ACK,
   cancel, or queue drain.
3. Archive projection is eventual and idempotent.
4. Web and iOS queue chips key by `live_input_id` and do not require
   `SessionInput.id`.
5. Duplicate client submissions do not duplicate provider sends.
6. A cancelled queued live receipt is never drained.
7. A delivering live receipt is not claimed twice.
8. Failed live receipts are visible enough for an agent to inspect directly.
9. A 24-hour reminder exists outside the repo to ask an agent for a fresh
   PASS/FAIL hot-control health read.
10. Focused backend tests, core E2E, exact-SHA ship, hosted demo/canary health,
    and local dogfood refresh all pass.

## Implementation Stages

### Stage 1: Durable Projection Bridge

- Add `session_input_receipt.v1` to `LiveArchiveOutbox`.
- Drain projects `SessionInput` and links `SessionTurn`.
- Projection updates `archive_session_input_id` on the live receipt.
- No request-path `asyncio.create_task` projection.

### Stage 2: Live Receipt Control Model

- Add read helpers for recent live input receipts by session.
- Add atomic status transitions:
  - queued -> cancelled
  - queued -> delivering
  - delivering -> delivered
  - delivering -> failed
- Add idempotent lookup by `(owner_id, session_id, client_request_id)`.

### Stage 3: API Cutover

- Make JSON text input create hot receipts first for all intents.
- Add live cancel route.
- Change queued-input list to return live receipts only.
- Keep attachment route archive-first.

### Stage 4: Drain Cutover

- Move terminal-boundary queue drain to live receipts.
- Preserve old archive drain only as legacy fallback.
- Add duplicate-send and cancelled-before-drain tests.

### Stage 5: Clients

- Web queue chips key by `live_input_id`.
- iOS submitted/queued input identity uses `liveInputId`.
- Archive `inputId` is optional provenance only.

### Stage 6: Observability

- Add metrics/logs/doctor fields.
- Add an out-of-repo 24-hour reminder to request agent inspection.
- Make failures actionable by live input id/session id.

### Stage 7: Validation And Ship

- Focused backend tests for status transitions, dedupe, cancel, drain, and
  projection.
- Core E2E for send/queue/cancel/drain.
- Archive-pressure test: hold or delay archive writer and prove live ACK/cancel
  still works.
- Exact-SHA ship.
- Local dogfood refresh.
- Inspect demo/canary live receipt and outbox fields directly.

## Non-Goals

- Attachments hot-lane migration.
- Full archive compaction/retention policy.
- Public release packaging.
- Multi-user compatibility migration.
- Preserving old archive-first identity as a product contract.
