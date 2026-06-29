# Managed Session Input Queue Kernel

## Problem

Managed session input has crossed from "send a live command into a provider CLI" into "durably queue ordered user intent for an interactive external process." The current implementation is halfway between those worlds:

- `SessionInput` is durable and ordered.
- `SessionInput.status` already models `queued -> delivering -> delivered | failed`.
- A per-session lock prevents duplicate provider injection.
- Terminal-phase watchers release that lock and opportunistically drain the next queued input.
- Runtime/presence routes also call `_drain_next_queued_input` when they see deliverable states.
- Startup reconciliation rewinds stale `delivering` text rows.

That is enough for the happy path, but it makes queue progress depend on incidental wakeups. The dogfood failure on 2026-06-29 showed the bad edge: the prior turn was durable and the provider was idle, but the terminal watcher timed out before releasing/draining, so an iOS queued input stayed queued until a fresh runtime event manually poked the system.

The refactor goal is not to replace managed provider control. The goal is to put a small explicit queue kernel under it so missed watcher signals become ordinary lease/recovery cases.

## First Principles

A robust queue has four durable facts:

```text
message: user intent, ordered by session
attempt: a leased delivery try with owner, deadline, result, and error
worker gate: whether this session may accept another input now
completion: proof that the attempt reached the correct durable boundary
```

For Longhouse, a provider session is an ordered message group. Only one queued input should be injected into a given provider session at a time. That does not mean the web request, lock watcher, runtime reducer, or iOS poller should be the queue coordinator.

Runtime phase changes, transcript rows, machine-control reconnects, and terminal watchers are wake hints. Queue correctness must also come from durable leases and recovery scans.

## Current Shape

Relevant existing tables:

- `session_inputs`: durable user-originated input, author, intent, status, idempotency key, delivery request id, timestamps, last error.
- `session_turns`: canonical turn timing, request id, linked input id, state, terminal phase, durable timing.
- `session_runtime_state`: reducer-owned live/runtime projection, phase, terminal state, freshness.
- `session_input_attachments`: attachments associated with `SessionInput`.

Relevant current services:

- `zerg.services.session_inputs`: create, claim, mark delivered/failed, requeue stale delivering, boot reconciliation.
- `zerg.services.session_chat_impl._dispatch_managed_local_text`: synchronous managed send path.
- `zerg.services.session_chat_impl._release_managed_local_lock_after_terminal`: watcher-driven turn completion, lock release, and queue drain.
- `zerg.services.session_chat_impl._drain_next_queued_input`: claim oldest queued row and dispatch through the normal send path.
- `presence` and `runtime` routers: after runtime state writes, call `_drain_next_queued_input` when state is deliverable.

This is close to a queue, but the attempt and worker gate are implicit:

- A `delivering` row is both "claimed for send" and "we are waiting for provider/turn outcome."
- `delivery_request_id` identifies the current attempt but does not have a lease deadline or attempt result history.
- The session lock is the active-turn gate, but lock expiry is not itself a durable queue wakeup.
- Watchers perform coordination work that should belong to a worker.

## Target Model

Keep `SessionInput` as the durable user intent row. Add an explicit delivery-attempt layer and a session queue worker/gate.

The durable attempt lease is the coordination authority. The existing in-memory
session lock may remain as a local fast-path guard around provider injection,
but it must not be the only exclusivity mechanism. Hosted/runtime processes can
restart or multiply; the database lease has to be enough to prevent duplicate
provider injection.

### Tables

Add `session_input_delivery_attempts`:

```text
id integer primary key
session_input_id integer not null references session_inputs(id)
session_id uuid not null index
thread_id uuid null index
owner_id integer null
request_id string(64) not null index
attempt_number integer not null
status string(24) not null
  acquired | submitted | accepted | completed | released | failed | expired
lease_owner string(128) not null
lease_expires_at timestamptz not null index
submitted_at timestamptz null
accepted_at timestamptz null
completed_at timestamptz null
released_at timestamptz null
failed_at timestamptz null
error_code string(64) null
error text null
created_at timestamptz not null
updated_at timestamptz not null
```

Indexes:

```text
ix_input_attempts_input_created(session_input_id, created_at)
ix_input_attempts_session_status_lease(session_id, status, lease_expires_at)
ix_input_attempts_request(session_id, request_id) unique
```

Do not add a separate queue table yet. `session_inputs` remains the ordered queue. Add only the minimum columns needed to make queue eligibility cheap and visible:

```text
attempt_count integer not null default 0
next_attempt_at timestamptz null
last_attempt_id integer null
```

Optional but useful after the first migration lands:

```text
claimed_at timestamptz null
terminal_reason string(64) null
```

Do not make `SessionInput.status` more granular in this pass. Keep the public lifecycle stable:

```text
queued -> delivering -> delivered | failed
queued -> cancelled
```

The detailed state lives in attempts.

### Queue Worker Contract

Introduce a small service, tentatively `zerg.services.session_input_queue`, with one public entrypoint:

```python
async def wake_session_input_queue(
    *,
    db_bind,
    session_id: UUID,
    reason: str,
    owner_hint: int | None = None,
) -> QueueWakeResult:
    ...
```

It may be called freely by:

- POST `/api/sessions/{id}/input`
- runtime/presence state writes
- machine-control reconnect
- terminal watcher completion
- startup reconciliation
- periodic recovery task

`wake_session_input_queue` must be idempotent and cheap when no work is ready.

For a given `session_id`, it:

1. Recovers expired attempts whose `lease_expires_at < now`.
2. Repairs any stale `session_inputs.status='delivering'` rows with no live unexpired attempt.
3. Checks whether the session is eligible to accept the next queued input.
4. Atomically claims the oldest eligible queued `SessionInput` only if no unexpired active attempt exists for the session.
5. Creates a `session_input_delivery_attempts` row with a lease.
6. Dispatches through the existing managed control path.
7. Marks attempt/input according to the result.
8. Schedules or performs another wake only when the active turn is complete.

The claim and attempt creation must happen in one write transaction. The status-gated claim must include the session lease condition, conceptually:

```sql
UPDATE session_inputs
SET status = 'delivering', ...
WHERE id = :candidate_id
  AND status = 'queued'
  AND NOT EXISTS (
    SELECT 1
    FROM session_input_delivery_attempts
    WHERE session_id = :session_id
      AND status IN ('acquired', 'submitted', 'accepted')
      AND lease_expires_at > :now
  )
```

Then insert the attempt row before committing. If either step cannot complete,
the worker must leave the input queued and return a not-ready/raced result.

### Readiness Gate

The worker should have one named readiness function:

```text
ready_to_dispatch(session) =
  session exists
  AND session has live managed control
  AND session is not closed for input
  AND no unexpired active delivery attempt for this session
  AND no active non-terminal SessionTurn for this session
  AND latest runtime state is compatible with input
```

Compatible runtime phases for queue drain:

```text
idle
needs_user
blocked
```

`needs_user` requires care: it is often a normal provider idle prompt after assistant output, not necessarily a structured pause. The gate should use the same runtime semantics the UI uses today, and must not reintroduce a parallel presence cache. Add a focused test before trusting `needs_user` as universally drainable; if structured pause requests prove ambiguous, narrow the gate for that provider/transport instead of special-casing the queue worker.

The readiness gate may return "not ready yet" with a next wake hint:

```text
active_turn
runtime_busy
control_unavailable
closed
lease_active
no_queued_input
```

### Delivery Boundaries

Separate these facts:

```text
submitted: command sent to machine-control transport
accepted: provider/control path accepted the input or verified prompt landing
completed: turn reached terminal-ish phase and the session may drain next input
delivered: public SessionInput lifecycle says the user's input reached provider responsibility
```

For `SessionInput.status`, keep current semantics:

- `delivering`: worker owns the current attempt.
- `delivered`: provider/control accepted the input. This does not wait for durable transcript proof, and the response turn may still be active.
- `failed`: no more automatic attempts should run.
- `queued`: waiting for a safe turn boundary or retry time.

The active turn gate should come from `SessionTurn` plus runtime state, not from leaving the input row in `delivering` until the assistant finishes.

This matters because "input delivered" and "session ready for next input" are not the same event.

Attempt `completed` means the turn reached a terminal-ish boundary and the next
input may be considered. Public input `delivered` means provider accepted
responsibility for the user input. Transcript proof remains archive truth and
UI de-duplication input, not queue progress truth.

### Retry And Failure Policy

Default attempt policy:

```text
max_attempts = 5
initial lease = 60s for transport submit
turn lease = 300s after accepted, renewable by fresh runtime progress
backoff = 5s, 30s, 120s, 300s, then manual/fail
```

Lease renewal must be concrete before attempts become authoritative. Treat the
transport lease and turn lease separately:

- Transport lease covers claim through provider/control accept.
- Turn lease covers accepted input through terminal turn boundary.
- Fresh runtime progress for the same session may renew the turn lease, but must
  not cause re-injection. Candidate renewals include runtime phase changes,
  `last_progress_at`, `last_live_at`, or provider transcript events that are
  already reduced into `SessionRuntimeState`.
- If the turn lease expires while the latest runtime state still says the turn
  is active and fresh, renewal wins over retry. Expiry should recover orphaned
  attempts, not punish long-running healthy turns.

Intent-specific behavior:

- `queue`: retry safe text-only input until max attempts, then `failed`.
- `auto`: retry text-only transient transport failures; fail permanent validation/control errors.
- `steer`: never silently retry after the active turn has changed or after restart; fail with a structured reason.
- attachments: do not auto-requeue unless the attempt can prove the original attachment payload will be resent intact. Keep current conservative failure behavior at first.

Transient errors:

```text
control_unavailable
machine_disconnected
transport_timeout
lock_conflict
runtime_busy
```

Permanent errors:

```text
session_closed
not_managed
invalid_payload
attachment_missing
permission_denied
turn_ended_for_steer
```

### Wakes, Not Watchers

Terminal watchers remain useful, but they should stop doing coordination directly.

New watcher role:

```text
observe terminal phase
persist SessionTurn terminal fields
release any transport/session lock if still held
wake_session_input_queue(reason="turn_terminal")
```

If the watcher misses terminal state, other wakes still cover progress:

- runtime/presence idle wake
- lease expiry recovery
- periodic queue recovery
- startup reconciliation
- next user enqueue

### Periodic Recovery

Add one lightweight background task in lifespan:

```text
every 15s:
  find distinct sessions with:
    queued inputs where next_attempt_at <= now
    OR active attempts with lease_expires_at <= now
    OR delivering inputs with no unexpired attempt
  wake each session input queue
```

This is not a polling loop for normal progress. It is the safety net for missed process-local signals and restarts.

SQLite notes:

- Use existing `WriteSerializer` for high-frequency/write-sensitive paths.
- Keep queries indexed by `(session_id, status, created_at)` and `(session_id, status, lease_expires_at)`.
- Per-session ordering plus SQLite's single writer is enough; do not add external Redis/Postgres.

## API And UI Compatibility

No public route changes in the first pass.

Keep:

- `POST /api/sessions/{id}/input`
- `POST /api/sessions/{id}/inputs-multipart`
- `GET /api/sessions/{id}/inputs`
- existing `SessionInputResponse` status/outcome shape

The UI should still see `queued`, `delivering`, `failed`, and POST-delivered acknowledgements. Attempt rows are internal at first. If needed, expose a debug-only attempt history later.

The existing `managed-input-lifecycle.md` UX contract still applies: no bare 422, visible delivery state, transcript de-duplication, and queue chip behavior. This spec supersedes only its "do not add a new lifecycle table" backend decision.

## Implementation Plan

### Phase 0: Guardrails And Fixture

- Keep commit `afd080bf7` as the tactical fix for the current dogfood failure.
- Add this spec and review it before code changes.
- Preserve current `SessionInput` public lifecycle and UI contracts.
- Do not change provider bridge behavior in this refactor unless a test proves it is necessary.

### Phase 1: Attempts Model And Migration

- Add `SessionInputDeliveryAttempt` to `server/zerg/models/agents.py`.
- Add `attempt_count`, `next_attempt_at`, and `last_attempt_id` to `SessionInput`.
- Rely on `_auto_add_missing_columns()` for additive nullable/server-default columns.
- Add imperative index/table creation only if model metadata is insufficient for existing SQLite tenants.
- Add focused model/migration tests if existing test shape supports it.

### Phase 2: Extract Readiness Gate And Queue Service Skeleton

- Create `server/zerg/services/session_input_queue.py`.
- Add the named readiness function before attempts become authoritative.
- Move `claim_next_queued`, transient requeue, and `_drain_next_queued_input` orchestration behind `wake_session_input_queue`.
- Keep `_drain_next_queued_input` as a compatibility shim that calls the new service.
- Existing runtime/presence/startup callers should switch to the new wake function.
- Tests should still pass before attempts become authoritative.
- The dogfood regression should pass in this phase: prior turn durable, missing
  `terminal_at`, fresh runtime `idle`, queued row exists, wake drains.

### Phase 3: Attempts Become Authoritative

- On claim, create an attempt row and set input `status='delivering'`, `attempt_count += 1`, `last_attempt_id`.
- Make the durable attempt lease the single cross-process injection authority.
- Keep the in-memory session lock only as a local process guard around actual provider injection.
- On transport submit/accept/failure, update the attempt row.
- On transient failure, release attempt and return input to `queued` with `next_attempt_at`.
- On permanent failure/max attempts, mark input `failed`.
- On provider accepted, mark input `delivered` while `SessionTurn` carries turn completion state.
- Ensure request id/idempotency preserves `client_request_id` and `delivery_request_id` behavior.

### Phase 4: Active Turn Gate

- Add a single readiness function that checks runtime state plus active `SessionTurn`.
- Replace lock-held-as-readiness assumptions with the readiness function.
- Keep session lock acquisition only around actual provider injection.
- Terminal watcher persists turn terminal, releases lock, and wakes queue.
- Runtime/presence state writes wake queue after reducer commit.

### Phase 5: Recovery Loop

- Add lifespan recovery task for expired attempts and stale delivering rows.
- Rework startup reconciliation to use the same recovery function instead of a separate one-off policy.
- Add metrics/logging for:
  - queued age
  - attempt age
  - expired attempts recovered
  - wake reason
  - not-ready reason
  - max-attempt failures

### Phase 6: Tests

Backend focused tests:

- Atomic claim: two concurrent wakes dispatch at most one input per session even if they race before either sees the other's attempt.
- In-memory lock failure/restart does not permit duplicate injection when an unexpired attempt exists.
- Old queued input drains when runtime is already idle and watcher missed terminal.
- Expired attempt is reclaimed and retried without a fresh runtime event.
- Delivered input does not block next queued input once turn is terminal.
- Delivered input does block next queued input while active turn is non-terminal.
- Engine/provider bridge reconnect after input `delivered` but before turn terminal does not re-inject the same input.
- Session closed/deleted while input is delivering fails or releases the attempt without retrying into a closed session.
- `needs_user` drain behavior is explicitly tested for the relevant transport state.
- Transient machine-control unavailable requeues with `next_attempt_at`.
- Permanent errors fail and do not retry.
- Stale `steer` fails rather than silently requeueing.
- Attachment interrupted behavior stays conservative.
- Startup recovery and periodic recovery share the same policy.
- Idempotent `client_request_id` does not double-dispatch.
- Concurrent wake calls dispatch at most one input per session.

Regression tests from the dogfood incident:

- Prior turn is durable, `terminal_at` missing, runtime state is fresh `idle`, queued row exists: wake drains.
- Terminal watcher timeout no longer leaves queue progress dependent on an unrelated future runtime event.

### Phase 7: UI And Debug Surfaces

- Keep UI behavior stable first.
- Update `/inputs` only if needed to show clearer failed/retry states.
- Add an operator/debug endpoint or hosted-session-debug section for attempt history only after backend state proves useful.

### Phase 8: Cleanup

- Remove duplicated drain paths once all callers use the queue wake function.
- Rename logs from "lock watcher drained" to "input queue wake/drain" where accurate.
- Update `managed-input-lifecycle.md` if final behavior changes the UX contract.

## Risks

- Duplicate provider injection: highest risk. Mitigate with per-session readiness gate, attempt request uniqueness, existing lock acquisition around injection, and concurrency tests.
- Losing inline structured errors: keep POST synchronous for immediate `auto`/`steer` dispatch where possible; background queue is mainly for queued/deferred/retry.
- Retrying unsafe payloads: keep steer and attachments conservative.
- SQLite write pressure: keep recovery interval modest, use indexed session selection, and avoid scanning all inputs each tick.
- Provider-specific semantics: do not force Claude/Codex/OpenCode/Antigravity into identical transport code. The queue kernel owns ordering and retries; provider adapters own "accepted" proof.

## Open Questions For Review

- Should accepted provider input be `SessionInput.status='delivered'` immediately, or should public `delivered` wait for durable user transcript proof?
- Is an attempt table enough, or do we need a per-session queue cursor/gate row to make readiness and metrics clearer?
- Should the recovery task be always-on in self-hosted runtime, or only when queued/delivering rows exist at startup plus runtime writes?
- What is the correct lease split between transport submission and long-running turn completion?
- Can `SessionTurn.state` be the sole active-turn gate, or do we need runtime phase as a tie-breaker for imported/reconstructed turns?
