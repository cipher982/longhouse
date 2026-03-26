# Managed-Local Turn Ledger

Status: phase 1 shipped; later phases planned
Last updated: 2026-03-26

## Goal

Add a small, explicit per-turn ledger for managed-local continuation so Longhouse
stops reconstructing turn truth from a mix of tmux send success, runtime hooks,
presence, transcript ingest, and review inference.

The first version should be intentionally narrow:

- managed-local sessions only
- shadow mode first
- no user-facing behavior change in phase 1
- one outstanding continuation request per session thread remains the expected path

## Problem

The current system is reliable again, but the source of truth for one turn is
still too distributed:

- `/api/sessions/{id}/chat` knows when a send was attempted
- runtime hooks/presence know when the local agent reached a terminal state
- transcript ingest knows when the turn became durable
- `turn_loop` infers completed turns from transcript + session-wide state

That makes latency debugging and future optimization harder than it should be.

## Decision

Introduce a new agents table:

- `managed_local_turns`

This table is the planned per-turn ledger for managed-local continuation.
Phase 1 ships it in shadow mode; later phases can promote it into the
authoritative read path.

Phase 1 is shadow-only:

- create the row when `/api/sessions/{id}/chat` begins a managed-local send
- mark send acceptance after the tmux/runner dispatch succeeds
- mark terminal completion when the route observes the current turn reach a
  terminal runtime state
- mark durability when ingest can bind the current prompt + assistant/tool
  evidence to the open turn
- attach the review id when `session_turn_reviews` records a review for that turn

Existing route behavior, sync behavior, and review discovery stay intact in
phase 1.

## Non-Goals

Do not build any of this in phase 1:

- generic tracing infrastructure
- provider-agnostic workflow engine
- route renames or API cleanup
- Loop consuming the ledger as its primary source of truth
- direct provider protocol changes

## Data Model

Minimal v1 columns:

- `id`
- `session_id`
- `request_id`
- `created_at`
- `send_accepted_at`
- `baseline_event_id`
- `baseline_runtime_event_id`
- `expected_user_text_hash`
- `terminal_phase`
- `terminal_at`
- `terminal_runtime_event_id`
- `durable_user_event_id`
- `durable_assistant_event_id`
- `durable_at`
- `review_id`
- `error_code`

Important indexes:

- unique `(session_id, request_id)`
- `(session_id, created_at)`
- `(session_id, durable_at, review_id, created_at)`

Important rule:

- treat timestamps and bound event ids as the real state
- avoid extra enum/status columns until the model proves it needs them

## State Model

The ledger is monotonic.

Transitions:

1. row created
2. send accepted
3. terminal observed
4. durable events bound
5. review attached
6. optional failure stamped

No backwards transitions. No reset flow in phase 1.

Derived facts:

- control complete: `terminal_at IS NOT NULL`
- durable: `durable_at IS NOT NULL`
- review attached: `review_id IS NOT NULL`

## Binding Rules

Phase 1 durability binding should stay simple and conservative:

- only consider managed-local sessions
- find the oldest open turn for the session where `durable_at IS NULL`
- look at transcript events after `baseline_event_id`
- require:
  - the expected current user prompt
  - then a later assistant event with non-empty text or a tool event

Use this matching stack:

- `session_id`
- one outstanding request per session thread as the expected path
- `baseline_event_id`
- `expected_user_text_hash`

Do not try to solve arbitrary concurrent sends in phase 1.

## Phase Plan

### Phase 1: Shadow ledger

- add `managed_local_turns`
- add a tiny service module with monotonic update helpers
- create + update rows from existing continuation / ingest / review paths
- add targeted tests
- verify locally and on hosted prod

### Phase 2: Route reads ledger

- `/api/sessions/{id}/chat` completion reads terminal state from the ledger
- `sync_status` reads durability from the ledger
- keep current direct-ship fallbacks, but stamp outcomes into the ledger

### Phase 3: Loop reads ledger

- `turn_loop` consumes pending durable turns instead of transcript-scan-first
- `session_turn_reviews` keeps the user-facing artifact role

### Phase 4: Reduce transcript hot-path dependence

- optimize or replace the current stop-hook/file-ship durability path
- move toward explicit turn durability acks if needed

## Success Criteria

Phase 1 is done when:

- the new table exists and is populated for managed-local continuation turns
- route, ingest, and review shadow updates all land without changing current UX
- targeted tests cover row creation, terminal stamping, durability binding, and
  review attachment
- hosted managed-local Claude continuation still passes end to end on `david010`

## Current State

Phases 1 and 2 are now shipped.

What is implemented:

- `managed_local_turns` exists in the agents DB model
- continuation creates and stamps shadow rows
- ingest can bind durable prompt + assistant evidence
- turn review creation can attach the resulting review id
- shadow writes are best-effort and isolated so ledger failures do not break the live path
- `/api/sessions/{id}/chat` now prefers the ledger for terminal and durability
  reads, with bounded fallback to direct evidence if a ledger read is missing or
  late

What is intentionally not implemented yet:

- Loop consuming the ledger instead of transcript/presence inference
