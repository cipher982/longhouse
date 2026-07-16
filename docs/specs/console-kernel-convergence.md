# Console Kernel Convergence

Status: implementation plan locked 2026-07-15
Owner: Runtime Host session kernel
Related:
- `VISION.md`
- `docs/specs/turn-scoped-console-execution.md`
- `docs/specs/console-turn-transcript-convergence.md`
- `docs/specs/session-identity-kernel.md`
- `docs/contracts/truth-plane.md`

> **Follow-up correction (2026-07-16):** The catalog/workspace convergence in
> this document does not by itself guarantee that provider transcript ingest
> binds back to the Console thread. `console-turn-transcript-convergence.md`
> owns that missing binding, live-event, optimistic-input, and terminal-turn
> seam. This plan is not complete in production until that acceptance gate
> passes.

## Problem

Console identity is authoritative in the live catalog, but four production
paths still treat Console as a Helm-shaped session plus exceptions:

1. the machine-auth turn route writes through the cold `AgentSession` store;
2. input routing depends on `origin_kind` surviving a hand-built control DTO;
3. the generic connection-oriented capability projection is followed by a
   14-field Console composer override;
4. workspace construction has separate archive-present and control-only
   response builders.

This incomplete cutover caused three consecutive launch regressions: a new
session could not open before archive projection, then opened read-only, then
the first message followed the Helm connection guard and returned busy.

## Decision

Use one durable catalog fact to choose the session command family, and keep
archive readiness orthogonal:

```text
create -> catalog session + primary thread + execution target
       -> asynchronous archive projection

open   -> catalog identity + capabilities
       -> archived events when available, otherwise an empty event page

send   -> catalog mode
       -> Console: create or queue a turn
       -> Helm: require the applicable live connection grant
       -> Shadow: unavailable
```

Console and Helm remain different command paths. The simplification is one
authoritative mode and one capability projection, not one execution model.

## Invariants

1. Every public Console turn write uses the catalog FIFO in catalog mode.
2. Command routing reads mode from the owner-scoped catalog session fact. A
   copied DTO field is never the authority for choosing Console versus Helm.
3. An open Console thread with a valid execution target and reachable proven
   adapter exposes `can_start_turn`; it does not require an active run or
   `SessionConnection`.
4. Presentation fields (`composer_enabled`, input mode, labels and reasons)
   derive once from kernel action availability. No later helper may promote a
   denied action.
5. Session identity and capabilities are readable immediately after create.
   Archive lag changes only transcript availability.
6. Browser and machine-auth routes are authentication veneers over the same
   Console create/turn services.
7. The cold `SessionTurn` model remains the archive/history projection. It is
   not a second production Console writer.

## Bounded API Contract

Extend the existing server-owned capability response with the Console subset
already specified by `turn-scoped-console-execution.md`:

```text
turn_state: idle | queued | starting | active | draining
can_start_turn: boolean
start_turn_blocked_by: null | session_closed | machine_offline |
                       adapter_unavailable | execution_target_missing
can_interrupt_active_turn: boolean
```

During this convergence, existing presentation fields remain in the response
and are derived from these action facts. Web and iOS do not need to infer
Console behavior from `origin_kind`, process liveness, or raw provider support.

## Implementation Tasks

### Phase 1 — one Console write service

- [x] Route `POST /api/agents/sessions/{session_id}/turns` through
      `enqueue_catalog_console_turn` when catalog mode is enabled.
- [x] Use the shared typed catalog turn service from both auth veneers; each
      veneer maps its own response model.
- [x] Keep the cold writer only behind non-catalog mode for compatibility tests.
- [x] Prove create followed immediately by a machine-auth first turn without
      archive materialization.
- [x] Prove owner isolation and idempotent replay at the service boundary.

### Phase 2 — mode and action truth

- [x] Make owner-scoped catalog session facts the routing authority.
- [x] Add the Console turn-action fields to the canonical kernel/API
      capability projection.
- [x] Derive Console action availability from closed state, execution target,
      current turn ownership and machine adapter reachability.
- [x] Delete `_project_console_composer` and derive presentation once.
- [x] Add a truth table covering idle, queued, active, closed, offline,
      missing-target and unsupported-adapter Console sessions.

### Phase 3 — one workspace shape

- [x] Read catalog identity/capabilities first for every storage-v2 workspace.
- [x] Attach archived events when storage exists; otherwise attach an empty
      event page with `control_only=true`.
- [x] Build the workspace envelope once so archive convergence cannot change
      session identity or action availability.
- [x] Prove the same session/capabilities before and after archive projection.

### Phase 4 — freeze compatibility and shrink routing context

- [x] Mark cold Console enqueue/dispatch as non-catalog compatibility only and
      migrate production-route coverage to catalog tests.
- [x] Replace `LiveControlSession` as a mode-routing authority with an explicit,
      required catalog mode read.
- [x] Shrink the dispatcher context only where that deletes duplicated builders;
      do not rewrite Helm control mechanics in this epic.
- [x] Add contract tests preventing production Console writes through
      the cold store or post-projection capability promotion.

## Acceptance Gate

- Web, iOS and `/api/agents/*` can create, immediately open and first-send to
  the same Console session before archive projection.
- First and later sends use the same catalog FIFO and idempotency semantics.
- Idle Console is sendable with no provider process; active Console queues the
  next normal turn; closed/offline/unsupported states return typed reasons.
- Helm still requires its live connection grant and Shadow remains read-only.
- Archive convergence adds events without changing mode, capabilities or
  ownership.
- No production route selects Console behavior from an optional DTO field.
- Backend unit and core E2E tests pass; generated web/iOS contracts remain in
  sync if the response model changes.

## Explicit Non-Goals

- Do not merge Console and Helm execution semantics.
- Do not remove the asynchronous archive outbox.
- Do not delete archived `SessionTurn` rows or turn inspection APIs.
- Do not add Console steer or answer-pause behavior.
- Do not remove browser versus machine authentication veneers.
- Do not perform a repository-wide removal of `live_catalog_enabled()`.
- Do not redesign provider adapters or Machine Agent process execution.
