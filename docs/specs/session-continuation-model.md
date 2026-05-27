# Session Continuation Model

Status: Phase 2 implemented
Owner: Longhouse session core + managed provider CLIs
Created: 2026-05-27
Related:
- `VISION.md`
- `docs/specs/agents-machine-surface.md`
- `docs/specs/session-identity-kernel.md`
- `docs/specs/remote-session-launch.md`
- `docs/specs/codex-subagent-control-model.md`
- `docs/specs/replay-safe-transcript-ingest.md`

## Executive Summary

Users experience a Longhouse session as the durable conversation they can find,
read, and continue later. Provider processes are disposable execution attempts
against that durable conversation. Longhouse already has the kernel nouns for
this split: `SessionThread`, `SessionRun`, `SessionConnection`, thread aliases,
and launch attempts. This spec makes continuation an explicit product capability
on that kernel.

The immediate launch risk is Codex manual `/resume`: the stock Codex TUI can
switch to an older provider thread while Longhouse's bridge still controls the
fresh placeholder thread. The bridge currently ignores foreign-thread
notifications, and hooks can still bind the resumed rollout path to the new
Longhouse session through `LONGHOUSE_MANAGED_SESSION_ID`. That produces a
split-brain control path and can contaminate the durable archive.

The implementation therefore lands in two layers:

1. Stop silent split-brain and wrong transcript binding.
2. Add first-class `Continue` as a new run/connection on an existing durable
   thread, starting with same-provider native Codex continuation.

Cross-provider continuation is a later capability. It starts a fresh provider
thread seeded by Longhouse-generated context; it is not provider-native resume.

## User Model

Product copy should mostly say **Session**.

- **Continue** means keep working from this conversation.
- **Attach** means open or reconnect a UI/control client to a run that already
  exists.
- **Read-only** means Longhouse has the transcript but no current control path.
- **Search-only/imported** means Longhouse can find and inspect the session but
  cannot steer it until a continuation target is chosen.

Do not expose `Thread`, `Run`, `Connection`, `Bridge`, or `managed` as the main
user model. Those are implementation nouns.

## Internal Model

- **Session**: product/display identity, timeline row, title, workspace, user
  state, and primary thread pointer.
- **Thread**: Longhouse-owned causal continuity. It survives provider quit,
  resume, bridge restart, and future same-provider continuation.
- **Transcript/source artifact**: evidence for a thread, not the thread itself.
  A thread can have more than one source path over time.
- **Run**: one provider CLI process invocation against a thread.
- **Connection**: Longhouse's live control/observation relationship to a run.
- **Alias**: provider-native evidence that can resolve a provider thread or
  source artifact to a Longhouse thread.
- **Launch attempt**: durable user/system intent to create a run. Continue uses
  the same attempt table instead of adding a separate intent table.

## Invariants

1. Thread identity is sticky. A process restart or provider-native resume does
   not create a new Longhouse session when the selected provider thread already
   maps to an existing Longhouse thread.
2. `LONGHOUSE_MANAGED_SESSION_ID` is launch context, not absolute truth. Local
   binding code must verify the observed provider thread/path still belongs to
   that managed session before writing path-to-session bindings.
3. One bridge process controls one provider thread at a time. When the attached
   TUI switches to another non-subagent thread, Longhouse must either transfer
   ownership explicitly or degrade/release the current connection. It must not
   keep reporting the original thread as steerable while the TUI is elsewhere.
4. Connection state changes liveness. It must not mutate session or thread
   identity.
5. Capability projection is server-derived from kernel rows. Web, iOS, CLI, and
   agents must not infer continuation or steerability from legacy fields.
6. Sending input now requires a live `SessionConnection` with
   `can_send_input=1`.
7. Continuing later is allowed for any durable session with a resolvable
   continuation target. It does not require the old provider process to still be
   alive.
8. Same-provider native resume and cross-provider context carry-forward are
   different modes. The first can reuse provider thread identity; the second
   creates a new provider-native thread seeded from Longhouse context.

## Decision Log

### Decision: Treat Continue as a Run on the Existing Thread

**Context:** The current remote-launch flow creates a fresh session and the old
web composer only sends to live managed control paths.

**Choice:** `Continue` creates a `SessionLaunchAttempt`, then a new `SessionRun`
and `SessionConnection` on the existing session's primary `SessionThread`.

**Rationale:** This preserves the user's mental model: the timeline item they
opened is the conversation that continues. It also reuses the session identity
kernel instead of reintroducing `continued_from_session_id` as product truth.

**Revisit if:** We need copy/fork semantics where a user intentionally branches
the conversation into a separate timeline item.

### Decision: Guardrail Before Adoption

**Context:** Fully supporting manual Codex `/resume` requires mapping arbitrary
provider thread selections back to existing Longhouse sessions and collapsing
placeholder sessions.

**Choice:** Phase 1 marks foreign non-subagent thread activity as divergence and
prevents the bridge/hook path from rebinding that source path to the placeholder
session. Full identity transfer is deferred to Phase 4.

**Rationale:** The archive-corruption risk is launch-critical. Placeholder
collapse is useful, but it has wider API/UI implications and should land after
the safety guardrail is tested.

**Revisit if:** The guardrail creates an unacceptable user dead-end during
dogfood and we need transfer before web Continue.

### Decision: Same-Provider Native First

**Context:** Codex can resume a provider-native thread. Cross-provider
continuation needs summarization and prompt seeding.

**Choice:** Phase 2 implements native same-provider continuation targets first.
Cross-provider continuation remains post-launch.

**Rationale:** Native resume is the closest match to user intent and verifies
the thread/run/connection model without adding context-generation complexity.

**Revisit if:** Provider-native resume becomes too unstable across upstream CLI
changes.

## API Shape

### Continue

Machine-first service and route:

```http
POST /api/agents/sessions/{session_id}/continue
```

Browser/user route may wrap the same service:

```http
POST /api/sessions/{session_id}/continue
```

Request:

```json
{
  "device_id": "this-device",
  "cwd": "/Users/davidrose/git/zerg/longhouse",
  "client_request_id": "uuid"
}
```

Response:

```json
{
  "session_id": "same-session-id",
  "launch_state": "live",
  "launch_error_code": null,
  "launch_error_message": null
}
```

`carry_context` values:

- `native`: use provider-native resume. Requires a provider thread alias and a
  machine that can resolve the provider source.
- `recent`: deferred; fresh provider thread seeded with recent transcript.
- `summary`: deferred; fresh provider thread seeded with summary + recent turns.
- `full`: deferred; explicit operator/debug mode only.

### Capabilities

Extend the kernel capability payload with:

- `can_continue`: true when at least one continuation target exists.
- `continue_targets`: compact list for UI/CLI choice. Each target includes
  provider, host availability, cwd, and whether native provider resume is
  possible.

The composer remains gated by `live_control_available && can_send_input`.
`can_continue` powers a separate Continue button for read-only or stale sessions.

## Phase Plan

### Phase 0: Spec

Acceptance criteria:

- This spec exists and is committed.
- It names the user model, invariants, API shape, phases, and test matrix.
- It documents the manual Codex `/resume` split-brain failure mode.

Tests:

- Documentation phase only.

### Phase 1: Codex Split-Brain Guardrail

Status: implemented in branch `session-continuation`.

Goal: prevent silent archive/control divergence when manual Codex `/resume`
switches the TUI to a different non-subagent provider thread.

Implementation:

- In `engine/src/codex_bridge.rs`, classify foreign non-subagent thread
  notifications separately from subagents.
- When the bridge sees a non-subagent thread id different from the controlled
  thread after the managed thread is locked/subscribed, mark bridge state
  degraded with a precise reason such as `provider_thread_switched`.
- Release or down-gate the current live connection through runtime/heartbeat
  state so server capability projection does not show the placeholder session
  as steerable.
- Stop `sync_thread_binding` from binding a foreign rollout path to the
  placeholder managed session. Existing binding for the original path may stay;
  the foreign path must not be written for the wrong Longhouse session.
- Preserve subagent behavior from `codex-subagent-control-model.md`.

Acceptance criteria:

- A `thread/started` or turn/status notification for a different root Codex
  thread cannot update `thread_path` or `session_binding` for the managed
  placeholder session.
- The bridge state records a degraded/mismatch reason that local-health and logs
  can surface.
- Browser/iOS send paths cannot project the diverged placeholder as live
  steerable after the mismatch signal is ingested.
- Current subagent ignore/reject tests still pass.

Tests:

- Rust bridge unit tests for:
  - foreign root `thread/started` after lock records divergence and does not
    mutate identity.
  - foreign root `turn/started`/`turn/completed` do not mutate active turn or
    session binding.
  - subagent notifications remain ignored, not divergence.
  - bridge state persists the degradation reason.
- Engine/local DB test proving `session_binding` does not upsert the foreign
  rollout path.
- Backend capability test proving a degraded/released connection projects no
  `can_send_input`.

Implementation notes:

- The bridge now writes `provider_thread_switched` into the state file and stops
  reporting the session as `ready` when it observes a different provider root
  thread.
- Codex hook-driven `bind` refuses to move a managed session to a different
  rollout path once the bridge state has a primary `thread_path`.
- Server heartbeat projection maps `provider_thread_switched` to offline control
  for the affected kernel connection so user send paths do not treat the
  placeholder as live.

Review:

- Hatch Opus review before Phase 2.

### Phase 2: Same-Provider Continue Kernel/API

Status: implemented in branch `session-continuation`.

Goal: allow Longhouse to start a new same-provider run on an existing durable
session/thread.

Implementation:

- Add a continuation service that resolves the session primary thread, validates
  provider, host, cwd, and provider-native aliases, and inserts a
  `SessionLaunchAttempt` keyed by `client_request_id`.
- Reuse or extend the existing machine-control `session.launch` command so a
  target Machine Agent can start Codex in native resume mode rather than fresh
  thread-create mode.
- Create the new `SessionRun` and `SessionConnection` against the existing
  `SessionThread` when launch succeeds.
- Record provider thread id and source path aliases for the existing thread.
- Project `can_continue` and continuation targets from existing aliases,
  machines, cwd, and provider support.

Acceptance criteria:

- Continuing a Codex session returns the same `session_id` and primary
  `thread_id`.
- The new run has `launch_origin=longhouse_continued` or another documented
  continuation-specific value.
- Idempotent `client_request_id` returns the existing launch attempt.
- Missing provider alias, offline host, unsupported provider, or missing cwd
  return typed errors without creating a live connection.
- Existing fresh launch behavior remains unchanged.

Tests:

- Backend service tests for success, idempotency, and typed failures.
- API route tests for machine route and browser wrapper.
- Machine-control command serialization tests for native Codex resume payload.
- Capability projection tests for `can_continue` and target selection.

Implementation notes:

- `POST /api/sessions/{session_id}/continue` and
  `POST /api/agents/sessions/{session_id}/continue` reuse
  `SessionLaunchAttempt` with `continue-*` command ids.
- The Machine Agent now advertises `codex.continue`; servers do not send native
  resume payloads to older engines that only know `codex.launch`.
- The control-channel `session.launch` payload accepts `mode=continue` plus a
  `resume.thread_id` / `resume.thread_path` target. Fresh launches still create
  the initial thread.
- `codex-bridge start/run` accepts `--resume-thread-id` and
  `--resume-thread-path`. Resume launches call Codex `thread/resume` during
  bridge startup before Longhouse marks the continuation live.
- Successful continuation keeps the same Longhouse session/thread, records a
  `longhouse_continued` run, attaches a fresh `codex_bridge` connection, and
  refreshes provider thread/source-path aliases.
- Session capability responses now expose `can_continue` and compact native
  `continue_targets` for Codex sessions with resolvable provider thread id and
  source path evidence.

Review:

- Hatch Opus review before Phase 3.

### Phase 3: Web/CLI Continue UX

Goal: expose first-class Continue without confusing it with live Send or Attach.

Implementation:

- Session detail composer states:
  - live + can send: composer enabled.
  - reattach: composer disabled with Attach/Continue affordance.
  - search-only/imported: composer disabled with Continue affordance.
- Add a compact Continue dialog that defaults provider, host, and cwd from the
  selected session and hides unsupported options.
- Add CLI parity for continuing a session through the machine-facing route.
- Keep raw attach command behavior for existing live/reattachable managed runs.

Acceptance criteria:

- A read-only/imported Codex session shows Continue, not a dead composer.
- A live session still sends normally; Continue is not the primary action.
- A reattachable session distinguishes Attach from Continue.
- UI consumes backend capability fields rather than re-inferring liveness.

Tests:

- Frontend unit/fixture tests for session detail states.
- API client tests for the continue call.
- CLI test for request construction and error display.
- Fixture-backed UI capture if layout changes materially.

Review:

- Hatch Opus review before Phase 4.

### Phase 4: Manual Codex `/resume` Adoption

Goal: make the terminal-native Codex `/resume` path first-class instead of only
guarded.

Implementation:

- Resolve the selected Codex thread id/path through `SessionThreadAlias`.
- If it maps to an existing Longhouse session/thread, transfer/adopt the bridge
  connection to that thread and redirect/collapse the placeholder session.
- If no alias exists, adopt the provider thread into the placeholder session and
  record aliases, while preserving existing source offsets.
- Add a small `session_redirects` table only if UI/API consumers need durable
  placeholder-to-real-session navigation.

Acceptance criteria:

- Terminal `/resume` of a known old Codex thread makes Longhouse control and UI
  point at the known old session, not an empty placeholder.
- Terminal `/resume` of an unknown Codex thread creates/imports exactly one
  durable Longhouse session for that provider thread.
- No historical transcript is duplicated unless the local source cursor was
  explicitly reset and a replay-safe ingest path accepts it as canonical delta.

Tests:

- Rust bridge adoption tests.
- Backend alias-resolution tests.
- End-to-end isolated Codex bridge test if the upstream app-server can be
  exercised in CI; otherwise a deterministic fake app-server contract test.

Review:

- Hatch Opus review before final.

### Phase 5: Cross-Provider Continuation

Deferred until after same-provider native continuation is stable.

Goal: continue any Longhouse session with another provider by seeding a fresh
provider-native thread with Longhouse context.

Acceptance criteria:

- Cross-provider continuation is opt-in and labeled as context carry-forward,
  not native resume.
- Context payload is inspectable and bounded.
- It creates a new provider-native thread but remains attached to the same
  Longhouse session/thread unless the user explicitly chooses to branch.

## Integration Test Matrix

The regression suite should cover these product interactions:

| Scenario | Expected Result |
| --- | --- |
| Fresh `longhouse codex` launch | New session, primary thread, run, attached connection, provider aliases |
| Codex TUI emits subagent thread | Child/subagent ignored for control; parent remains steerable |
| Codex TUI manual `/resume` to foreign root thread | Placeholder connection degrades; foreign path is not rebound |
| Web sends to healthy live session | Dispatches to current live connection |
| Web sends after divergence | Rejected/disabled because live send gate is false |
| Continue old Codex session same provider | Same session/thread, new run/connection, native resume target |
| Continue with offline host | Typed failure, no live connection |
| Continue with missing alias | Typed failure or non-native context mode prompt; no silent fallback |
| Replayed transcript path | No duplicate sourced events without canonical source delta |

## Open Risks

- Upstream Codex can change app-server notification shapes. Bridge tests should
  use both snake_case and camelCase shapes where Codex has already varied.
- Manual `/resume` adoption may require a provider-thread resolver that can read
  Codex rollout metadata and local state even when the bridge only sees an id.
- Multiple machines continuing the same provider thread concurrently should be
  surfaced as competing runs. V1 should not attempt automatic merge or remote
  kill.
- `session_binding` remains a local path-to-session hint. The long-term shape is
  to resolve path ownership through aliases and canonical source state, but
  Phase 1 can harden the existing hint safely.

## Done Condition

This project is done when:

- Manual Codex `/resume` cannot silently leave Longhouse controlling one thread
  while archiving another under the same session.
- Same-provider native Continue is exposed through the machine-facing API and
  human UI without creating a new user-visible session.
- The regression suite exercises bridge notifications, local binding safety,
  server capability projection, API continuation, and UI/CLI affordances.
- Hatch Opus has reviewed the implementation at each major phase and all
  blocking findings are fixed or documented in this spec.
