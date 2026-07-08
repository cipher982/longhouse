# Renderable Session Launch Pipeline

Status: Draft, refined by Hatch Fable

## Goal

Make session launch boring and instant across web, iOS, CLI, and agent APIs.

Core invariant:

> If Longhouse returns a session id, that session is immediately renderable,
> subscribable, and addressable by every session route.

## Grounding Observation

The durable cold path already mostly has the right shape. In the archive-backed
launch path, `launch_remote_session` synchronously creates:

- `AgentSession`
- primary `SessionThread`
- `SessionLaunchAttempt`
- `SessionRun` for one-shot launches
- `SessionConnection` after successful attach

The bug class comes from the hosted hot path: when Live Store is configured,
`_launch_remote_session_hot` writes live launch readiness plus launch outbox
rows, returns a session id, and waits for archive convergence to materialize the
durable rows. That creates a first-paint race where the returned session id can
404 on archive-backed detail routes.

So this is not a greenfield schema redesign. The work is to make the hot path
converge on the durable shell semantics the cold path already uses, then delete
or demote the launch-specific compensation machinery.

## Product Requirements

- Phone/web create should first-paint in under one second under normal network.
- Provider boot time must not block first paint.
- Every returned session id must load through:
  - timeline detail
  - mobile-tail
  - workspace
  - agent session get
  - stream subscription
- Empty interactive sessions are valid sessions.
- Launch failure is displayed on the same session, not as an orphaned error.
- Retry/idempotency must preserve one session per user action.
- Existing archive/session history must continue to work.
- No user-auth read path should perform a required write.

## Existing Durable Model

Use the existing kernel rows as the launch contract:

### `AgentSession`

The user-visible identity and transcript container. It must be created before a
launch response returns.

### `SessionThread`

The lineage container for the session. A launch shell should have a primary
thread immediately.

### `SessionLaunchAttempt`

The durable launch command row. It carries `client_request_id`, `command_id`,
state, lease expiry, error fields, and execution lifetime.

### `SessionRun`

One execution attempt for a session. One-shot launches already create this before
dispatch; live-control launches may create or attach one during reconciliation.

### `SessionConnection`

The control path for a run. It becomes attached/degraded/detached/ended as the
Machine Agent and provider bridge report state.

## Non-Goals

- Do not introduce a generic command table in this effort.
- Do not rewrite send/interrupt/terminate into a new command lifecycle.
- Do not change the public launch response shape unless a caller requires it.
- Do not make Live Store a required source of launch identity.

`SessionLaunchAttempt` is the launch command row for this project.

## Target Launch Flow

Phone tap hot path:

```text
iOS taps Create
  -> POST /api/sessions/launch
       archive transaction:
         insert durable AgentSession shell
         insert primary SessionThread
         insert SessionLaunchAttempt(state=pending, lease)
         insert SessionRun if needed
         write optional live readiness mirror
       commit
       return {session_id, launch_state: "launching"}
  -> iOS renders empty session immediately
  -> iOS subscribes to session stream
```

Async dispatch:

```text
after durable commit
  -> background task sends session.launch to Machine Agent
  -> Machine Agent starts provider CLI
  -> bridge/control path attaches or fails
  -> Runtime Host reconciles SessionLaunchAttempt/Run/Connection
  -> Runtime Host emits runtime/live signals
```

Provider startup can take several seconds. That affects when the composer becomes
usable, not whether the session screen loads.

The existing launch lease/reaper is the crash safety net for commit-before-dispatch:
if the process dies after creating the shell but before dispatch or ack, the
attempt expires and the same session shows a failed/orphaned launch state.

## Target Read Flow

All session detail surfaces should go through one internal bootstrap builder:

```text
SessionBootstrap {
  session
  thread_or_lineage
  active_run
  connection
  runtime_display
  capabilities
  projection_tail
  revision
}
```

This is an internal service shape, not necessarily a new wire contract. Web,
iOS, and agent APIs may serialize different responses, but they must share the
same truth rules. Mobile can request a smaller tail; it should not have a
different interpretation of "launching empty session."

## Target State Projection

Reuse the existing remote launch lifecycle projector where possible:

- `launching`
- `launching_unknown`
- `live`
- `launch_failed`
- `launch_orphaned`
- `ended`
- `archived`

Runtime phases such as idle/running/thinking are signals layered on `live`, not
separate launch lifecycle states. One projector owns this translation.

## Implementation Plan

### Phase 0: Characterization Tests

Add tests that capture the invariant before implementation:

- With Live Store configured, launch and immediately load:
  - timeline detail
  - mobile-tail
  - workspace
  - agent session get
  - stream subscription/auth connect where practical
- Confirm the same behavior works in cold mode.
- Add idempotency coverage for a repeated launch `client_request_id`.

Exit criteria:

- Tests demonstrate the current hosted hot-path race.
- Tests are precise enough to remain as regression coverage.

### Phase 1: Durable Shell in the Hot Path

- Extract the cold path's durable shell creation into a shared helper.
- Call the helper from the hot path before machine dispatch.
- Keep live readiness/outbox writes temporarily as an additive cache.
- Move idempotency to `SessionLaunchAttempt.client_request_id` for both hot and
  cold modes.
- Measure archive write latency for the shell transaction in tests or local
  instrumentation.

Exit criteria:

- Phase 0 route-loadability tests are green.
- Repeated hosted launch requests return the same session id.
- `make test` is green.
- The returned session id is durable before any Machine Agent ack arrives.

### Phase 2: Intent-First Response

- Return after durable shell creation and command scheduling, not after Machine
  Agent ack.
- Dispatch `session.launch` in a background task using the existing registry.
- Reconcile ack/failure back onto `SessionLaunchAttempt`, `SessionRun`, and
  `SessionConnection`.
- Audit CLI/smoke callers before changing behavior. If any caller requires
  synchronous ack, add an explicit wait mode rather than preserving implicit
  blocking for all clients.

Exit criteria:

- Fake-registry tests prove slow ack does not delay the HTTP response.
- Transport failure appears as launch failure/orphaned state on the same session.
- Provider failure appears as launch failure on the same session.
- `make test` is green.

### Phase 3: Unify Readers and Delete Launch Compensation

- Consolidate workspace/mobile-tail/detail live-launch fallback logic into one
  bootstrap path over durable rows.
- Remove mobile-tail launch placeholder branches once durable shells make them
  unnecessary.
- Remove or demote launch-specific Live Store dependencies:
  - `live_launch_readiness` as required read truth
  - `remote_launch.v1` outbox rows
  - `remote_launch_outcome.v1` outbox rows
- Keep non-launch live archive outbox uses intact.
- Decide explicitly whether the continue hot path is in scope; if included,
  apply the same durable-first treatment.

Exit criteria:

- Empty launching session, adopted live session, failed launch, and post-archive
  convergence all render through the same bootstrap rules.
- Grep confirms no session read path requires live launch readiness.
- `make test` is green.

### Phase 4: Operational Guardrails

- Keep normal browser/iOS auth dependencies read-only.
- Add or expose health fields for:
  - pending launch attempt age
  - launch ack latency
  - launch archive convergence latency during migration
  - remaining live archive outbox lag
- Ensure launch lag is visible before it becomes a user-facing blank screen.

Exit criteria:

- Health/readiness surfaces expose the new launch lag signals.
- `make test` is green.

## Risks and Unknowns

- **Archive write latency:** hosted first paint now includes one small archive DB
  transaction. If this is slower than expected on the large `david010` DB, that
  is the load-bearing risk.
- **Outbox migration tolerance:** old launch outbox rows may drain after durable
  shells already exist. Drain logic must remain upsert/idempotent.
- **Idempotency semantics:** hot and cold paths currently differ. Unification may
  change in-flight retry behavior unless covered by tests.
- **Crash window:** commit-before-dispatch can leave a session launching until
  lease expiry. That is acceptable if UI copy and reaper behavior are clear.
- **Continue path:** it has similar hot/cold fork structure. Include it only if
  the launch work exposes the same bug class there; otherwise document deferral.

## Testing Strategy

- Unit tests for lifecycle projection and shell creation.
- HTTP-level tests for immediate route loadability.
- Fake machine-control integration tests for slow ack, success, failure,
  timeout, and retry.
- Regression tests for empty interactive launches with no initial prompt.
- `make test` after each meaningful phase.
- Focused E2E only after the backend contract is stable.

## Definition of Done

- Creating an empty Codex session from iOS renders a usable empty chat
  immediately.
- Returned session ids never 404 on first navigation.
- Launch failure appears as state on the created session.
- Slow provider boot does not block first paint.
- Authenticated read requests do not perform required writes.
- Backend tests cover slow ack, success, failure, idempotency, and archive
  convergence.
