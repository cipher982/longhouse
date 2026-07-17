# Console Speed-of-Light Warm Path

**Status:** Proposed
**Owner:** Longhouse
**Last updated:** 2026-07-16

## Decision

Console should prepare expensive provider machinery before the user presses
Send, while preserving the invariant that a provider process is never session
identity.

For Codex, the Machine Agent owns a small, bounded pool of initialized stock
`codex app-server` workers. The initial implementation stops at
`process_ready`. A worker may prepare a provider thread before Send only after
real-provider proof establishes that preparation is side-effect-safe,
materially reduces latency, and cannot surface an abandoned provider artifact
as a product session. Send should normally perform only the irreducible
operation: `turn/start` with the user's input.

The bound is machine-global, not session-relative: 500 durable Console
sessions still produce at most the configured one or two warm workers. Old
threads are data that any compatible leased worker can resume; they do not own
idle processes.

This replaces the current Codex Console hot path of spawn, initialize,
thread start/resume, turn start, and provider inference after Send. It does not
change Helm, vendor a provider binary, begin inference speculatively, or keep
one process alive for every historical session.

A warm worker is not a Console invocation. An invocation begins only when an
accepted run durably attaches an exclusive lease and Longhouse starts delivery
of `turn/start`. The machine-global limit applies to complete worker process
groups, including provider-owned children.

## Why

The measured `Hi there 3` dogfood turn on 2026-07-16 had this waterfall:

| Stage | Observed |
| --- | ---: |
| Runtime Host accepted input to Codex task start | 2.3s |
| Codex task start to first token | 15.4s |
| Runtime live-preview publication after provider delta | usually 50–100ms |
| Provider completion to durable transcript ship | about 19.5s |
| Accepted input to durable transcript visibility | about 38s |

Codex processed 21,949 input tokens with zero cached input tokens for a 14-token
answer. Warming cannot promise a provider prompt-cache hit, but it can remove
avoidable local startup from the post-Send critical path and create a stable
environment in which cache behavior can be measured rather than guessed.

The live network path is already comparatively fast. The design must optimize
the entire perceived-response waterfall, not add another transport.

## Product Contract

1. **Send is the inference boundary.** Longhouse must not submit user text,
   start a model turn, consume model tokens, or represent the agent as Working
   before the user presses Send.
   Pre-Send work must not execute tools, request approvals, run project hooks,
   publish transcript activity, or establish canonical run ownership.
2. **Warmth is disposable.** Killing every warm worker loses no session data,
   accepted input, or control authority.
3. **Threads remain durable; processes remain replaceable.** A prepared thread
   may outlive its worker. A worker is never the identity of a Console session.
4. **No hidden execution fallback.** A failed warm lease falls back explicitly
   to the same stock-provider cold path and records why.
5. **One execution owner.** Only the Machine Agent that owns the Console target
   may prepare or lease its provider runtime.
6. **Provider-specific mechanics stay behind adapters.** Codex is first because
   app-server exposes a useful split. Other providers earn warm behavior from
   measured, provider-native proof rather than imitation.

## Latency Budgets

Two budgets are reported separately so Longhouse cannot hide provider latency
inside a fast transport number.

### Longhouse-controlled path

| Stage | Target p50 | Target p95 |
| --- | ---: | ---: |
| Input accepted to durable receipt commit | 25ms | 75ms |
| Commit to command frame written | 25ms | 75ms |
| Command frame written to Machine Agent receive | 50ms | 150ms |
| Receive to durable claim | 25ms | 75ms |
| Claim and warm lease to `turn/start` written | 25ms | 75ms |
| Provider delta received to Runtime Host publish | 100ms | 300ms |
| Runtime Host publish to visible iOS/web render | 150ms | 500ms |
| `turn/completed` to durable ship request | 100ms | 300ms |
| Durable append to Runtime Host acknowledgement | 1s | 3s |

Nominal warm Send-to-`turn/start` p95 must be below 500ms. Stage percentiles
are not added to infer an end-to-end percentile; both are measured. First
visible output then becomes overwhelmingly provider-owned and is reported as
such.

### Provider path

Record, but do not relabel or conceal:

- `turn/start` written to provider acknowledgement;
- acknowledgement to first commentary/tool/final delta;
- input, cached-input, and output tokens when supplied;
- model, reasoning effort, provider CLI version, and thread new/resume state.

The initial Codex goal is to remove local cold-start variance, then determine
with real canaries whether stable process/thread reuse improves prefix caching.
There is no SLA claim for an upstream cache Longhouse cannot control.

## Codex Architecture

### Worker lifecycle

The Machine Agent owns a `CodexConsoleWorkerPool`, not the Runtime Host or iOS
client.

```text
engine start / first Console intent
  -> resolve stock codex binary and auth
  -> spawn `codex app-server --listen stdio://`
  -> initialize / initialized
  -> mark worker Ready

Console target becomes concrete
  -> lease Ready worker
  -> initial rollout remains process_ready
  -> experimental rollout may thread/start or thread/resume after proof

user presses Send
  -> atomically claim accepted turn + prepared lease
  -> turn/start
  -> stream raw notifications immediately
  -> turn/completed
  -> wake durable shipper immediately
  -> release or retire worker by policy
```

Pool defaults for the first implementation:

- minimum ready workers: `1` while the Machine Agent has recent Console intent;
- maximum workers: `2` globally per machine;
- session count never changes this maximum;
- unleased ready TTL: `120s`;
- experimental prepared lease TTL: initially tens of seconds, renewed only
  while its Console view/draft intent is fresh;
- idle shutdown is graceful, then force-killed after a bounded drain;
- provider binary version, auth/config fingerprint, sandbox policy, and cwd
  policy changes invalidate incompatible workers;
- memory pressure, laptop sleep, engine shutdown, or provider failure may reap
  all warm workers immediately;
- the bound covers aggregate process groups and their children, RSS, CPU, file
  descriptors, and sockets across all auth/config partitions.

These are measured defaults, not API guarantees. The pool must expose counters
before adaptive sizing is considered.

### Preparation levels

Warmth is an explicit state, never a boolean:

| State | Work already completed | Safe without a Console target? |
| --- | --- | --- |
| `cold` | none | yes |
| `process_ready` | spawn, auth/config load, JSON-RPC initialize | yes |
| `thread_prepared` | target cwd and thread start/resume, binding persisted | no |
| `turn_active` | user input accepted and `turn/start` sent | only after Send |

Opening the Console or focusing its composer emits bounded **Console intent**;
it does not itself create an unbounded process per screen. A real, persisted
Console session with a selected execution target may request
`thread_prepared`. An uncommitted “new session” shell may request only
`process_ready`.

### Delivery fence, lease, and idempotency

The accepted turn receipt remains the at-most-once authority. A shared worker
PID is never run identity. Persist `worker_instance_id`, `lease_id`, and the
provider turn id separately through this delivery fence:

```text
claimed -> lease_attached -> turn_write_started -> turn_write_completed
        -> provider_acknowledged -> terminal
```

The lease attaches to `(session_id, turn_id, run_id)` before `turn/start` is
written. Duplicate delivery returns the existing claim and never starts a
second turn. A crash after `turn_write_started` is `delivery_unknown` and is
never blindly replayed.

If a prepared worker dies before `turn/start`, the claim stays recoverable and
the engine starts one cold replacement. If failure occurs after the write but
before provider acknowledgement, the turn becomes `delivery_unknown`; it is
not blindly replayed.

### Completion and durable convergence

The bounded Console adapter must perform the same completion wake as the Codex
Helm bridge. On `turn/completed`, after the rollout path is known, it sends a
`wake_socket` signal with `wake_reason=turn_completed`, session, provider turn,
path, and observed length. Filesystem observation remains repair, not the normal
completion path.

Live output remains provisional until durable source convergence. This change
accelerates canonical evidence; it does not make runtime events canonical.

### Provider event pump

Reading provider stdout must never await Runtime Host HTTP. Each active lease
owns a bounded event pump:

```text
provider reader -> bounded/coalescing channel -> persistent HTTP worker
```

Raw and projected events batch together over a small 8–16ms window. Terminal,
permission, and tool-boundary events are lossless. Cumulative assistant text
may coalesce under pressure. A five-second Runtime Host stall must not stop the
provider reader or delay provider completion.

## Client Delivery

The existing session SSE stream remains the downlink. Runtime event batch HTTP
remains the Machine Agent uplink for the first implementation. Replacing either
transport is out of scope until stage metrics show it is material.

iOS and web should:

1. apply typed transcript patches directly, keyed by run, turn, item, and item
   sequence, including tool state and completion;
2. render no more than once per display-frame/coalescing window;
3. cause zero mobile-tail fetches for a healthy preview burst;
4. perform one coalesced durable refresh on completion or workspace revision;
5. use bounded polling only while SSE is disconnected, a replay gap exists, or
   durable convergence exceeds its budget.

“Starting” is truthful after the Runtime Host accepts and queues the turn.
“Working” is truthful only after Machine Agent claim/provider activity. Warm
preparation is optionally observable in diagnostics but never shown as agent
work.

## Prompt and Cache Investigation

Warm workers follow the immediate hot-path repairs; prompt-cache optimization
is an evidence phase.

For every real Codex canary, capture provider usage plus sizes and locally
keyed HMAC fingerprints—not instruction contents or plain hashes:

- base instruction bytes and HMAC fingerprint;
- developer/user instruction segment bytes and HMAC fingerprints;
- AGENTS/injected context fingerprint;
- new versus resumed thread;
- cached-input and total-input tokens;
- time to first provider event and first visible token.

Randomize and interleave a controlled matrix so time-local upstream caching is
not mistaken for process reuse:

- cold process + new thread;
- warm process + new thread;
- warm process + resumed thread;
- repeated stable cwd/config;
- one controlled prefix mutation.

Do not shrink or omit required agent instructions merely to improve a latency
chart. Remove only proven duplication, unstable ordering, or accidental context
that normal stock Codex would not receive for the same cwd.

## Observability

Every Console turn emits one correlated waterfall keyed by
`client_request_id`, `turn_id`, and `run_id`:

- accepted;
- command dispatched / claimed;
- warm intent received;
- worker spawn start / ready;
- neutral cwd, OS user, environment/config/MCP fingerprint;
- lease hit/miss and miss reason;
- worker instance, lease, target revision, process-tree children/RSS/fds;
- thread start/resume start / complete;
- `turn/start` write / acknowledgement;
- first provider event / first transcript delta;
- Runtime Host publish;
- client receive / render beacon;
- provider completion;
- durable wake / enqueue / HTTP acknowledgement;
- client durable reconciliation.

Report p50/p95 by provider, CLI version, model, new/resumed thread, and warm/cold
path. A single aggregate “response latency” is insufficient.

Required miss reasons include `pool_empty`, `expired`, `worker_died`,
`config_changed`, `target_changed`, `memory_pressure`, `engine_restart`, and
`unsupported_provider`.

## Failure and Resource Policy

- Pool failure never disables Console; it makes the next run an observed cold
  start.
- Warm workers hold no accepted user input.
- Prepared leases are exclusive and cannot cross users, targets, cwd, auth, or
  sandbox/config fingerprints.
- The engine reaps orphans by exact recorded pid/process group only.
- Logs and process argv must not contain auth tokens or prompt contents.
- Sleep/wake invalidates readiness until the app-server is probed again.
- A worker that served a turn is reused only after a clean `turn/completed`,
  empty pending-request set, and successful health probe. Initial rollout may
  retire after each turn while still benefiting from pre-Send preparation.

## Implementation Plan

### Phase 0 — Waterfall and external proof

- Add the correlated stage schema and surface it in the real-provider canary.
- Preserve the `Hi there 3` waterfall as a regression fixture/benchmark target.
- Run the Codex matrix above against the installed stock CLI.
- Prove which work occurs at process initialize, thread start/resume, and turn
  start before choosing the reuse policy.

Gate: every unexplained second is assigned to Longhouse, provider, network, or
durable convergence.

### Phase 1 — Immediate convergence and client de-amplification

- Emit the missing Console `turn_completed` wake socket signal.
- Decouple provider reading from Runtime Host HTTP with the bounded event pump.
- Add typed SSE transcript patches sufficient to render the provisional turn.
- Coalesce iOS/web durable refreshes and disable normal polling while healthy
  SSE is delivering previews.
- Add integration tests for delta bursts, completion, replay gaps, and durable
  arrival ordering.

Gate: provider completion to durable host acknowledgement p95 under 3s on the
dogfood path; a 1,000-delta burst causes zero tail fetches and bounded renders;
a five-second Runtime Host stall does not block provider stdout draining.

### Phase 2 — Dispatch and durable delivery fence

- Return the durable receipt after enqueue/claim commit and dispatch without
  holding the submit request open for provider startup.
- Replace PID-shaped run claims with the lease/write/acknowledgement fence.
- Drive `queued -> command_dispatched -> machine_claimed -> turn_written ->
  active` from explicit events; derive Working from turn write/provider
  activity rather than process spawn.
- Add failpoints at every fence transition and prove no automatic replay from
  `delivery_unknown`.

Gate: duplicate delivery and every crash transition execute at most one turn;
accepted-to-command-frame and command-frame-to-turn-write are independently
measured.

### Phase 3 — Process-ready pool

- Extract app-server child/RPC ownership from one-shot `codex_exec` into a
  supervised worker abstraction.
- Prove a neutral startup cwd, then implement the global one-or-two process-tree
  bound, fingerprints, resource accounting, TTLs, preemption, exact cleanup,
  and health probes.
- Feed Console intent from Runtime Host through the existing Machine Agent
  control channel; do not add a second persistent channel.
- Initially retire workers after a served turn to minimize state-risk while
  moving all cold work before Send.

Gate: opening 500 sessions still produces at most two aggregate worker process
groups; real canary shows accepted-to-`turn/start` p95 below 500ms.

### Phase 4 — Thread-preparation experiment

- Measure `thread/start` and `thread/resume` contribution and side effects with
  the real stock provider.
- Prove project hooks, MCP children, rollout artifacts, recency, and abandoned
  prepared sessions remain safe and invisible.
- Do not ship preparation unless it saves more than 50ms p95 or 10% of warm
  Send latency.

Gate: a written proof decides to delete the idea or proceed; latency alone is
insufficient if preparation has observable pre-Send side effects.

### Phase 5 — Prepared thread and safe post-turn reuse

- If Phase 4 passes, add local preparation identities, target revisions,
  revocation, expiry, and suppression of empty provider artifacts.
- Prove app-server quiescence and reuse using upstream real-provider canaries.
- Reuse a clean worker across turns when fingerprints match.
- Compare cache and TTFT metrics against retire-after-turn behavior.

Gate: no cross-session events, pending requests, identity leakage, or lifecycle
drift across stress/restart tests; measured latency or resource win is material.

### Phase 6 — Provider-by-provider expansion

Investigate Claude, OpenCode, Cursor, and Antigravity independently through the
universal harness. Implement only provider-native preparation levels that are
observable and disposable. Do not make “warm pool” a fake universal interface
when the provider offers no useful split.

## Test Strategy

- Rust unit tests: pool state machine, expiry, fingerprints, lease exclusivity,
  crash paths, exact cleanup, and completion wake payload.
- Hermetic integration: fake app-server records ordering and asserts no
  `turn/start` before Send, duplicate commands start once, burst deltas do not
  trigger burst fetches, and process death selects the correct recovery state.
- Runtime Host integration: Console intent/claim authorization, idempotency,
  SSE preview, completion, and durable reconciliation.
- iOS/web integration: optimistic input appears once; preview streams; refresh
  is coalesced; disconnected SSE activates bounded fallback polling.
- Real Codex canary: cold/warm/new/resume matrix with stage timings and token
  cache evidence, repeated after supported Codex upgrades.
- Soak: pool size stays bounded across many opened/abandoned Console screens,
  sleep/wake, engine restart, config changes, and mixed Helm/Console activity.

## Acceptance Criteria

1. No model turn or token spend occurs before Send.
2. A warm hit writes `turn/start` within 500ms p95 of accepted input.
3. First visible delta appears within 300ms p95 of the provider emitting it.
4. Provider completion reaches durable host acknowledgement within 3s p95.
5. Healthy SSE delta bursts do not produce per-delta tail fetches.
6. Duplicate command delivery starts exactly one provider turn.
7. Warm-worker loss produces an explicit measured cold start without losing or
   duplicating accepted input.
8. Pool and prepared leases remain within configured bounds and expire cleanly.
9. Helm behavior and stock Codex TUI remain unchanged.
10. Real canaries report Longhouse-controlled and provider-controlled latency
    separately, including cache-token evidence.
11. At least 100 randomized warm sends with interleaved cold controls prove
    exact one-turn execution across engine restarts and zero pre-Send turn or
    token activity.

## Explicit Non-Goals

- Starting inference or guessing user input before Send.
- Weakening required Codex/AGENTS instructions for benchmark results.
- Promising OpenAI prompt-cache behavior.
- Keeping one provider process per Console session indefinitely.
- Replacing stock provider binaries or changing Helm's terminal UX.
- Adding WebSocket transport merely because it sounds faster than measured SSE.
