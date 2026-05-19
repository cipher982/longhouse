# Transcript Hot Plane

Status: Reviewed for Phase 1 planning
Owner: launch session surfaces
Last updated: 2026-05-19
Related:
- `realtime-truth-plane.md`
- `session-propagation-sla-matrix.md`
- `managed-session-transcript-catchup.md`
- `machine-agent-flight-recorder.md`
- `session-runtime-display-contract.md`
- `session-event-identity-plan.md`
- `replay-safe-transcript-ingest.md`
- `managed-session-stall-recovery.md`

## Purpose

Longhouse should feel like a lightspeed mirror of the provider transcript.

The product contract is simple:

```text
If a managed provider transcript file changes, Longhouse ships the changed bytes
immediately. Runtime phase must never decide whether transcript shipping stays
hot.
```

This spec replaces the phase-dependent transcript catch-up model with a
greenfield-style transcript hot plane:

1. A native file-change trigger wakes transcript shipping.
2. A periodic reconciliation trigger repairs missed file changes.
3. Runtime, process, and control-path signals remain useful overlays, but they
   are not transcript shipping lanes and cannot overwrite newer transcript
   truth.

## Problem

The current Machine Agent has multiple paths that can cause transcript shipment:

- native filesystem watcher events
- Claude hook/outbox phase signals
- active transcript polling while a session is `thinking` or `running`
- short terminal/attention catchups for `blocked`, `needs_user`, and `idle`
- periodic discovery/reconciliation scans

Those paths grew organically to solve real misses, but the current composition
lets runtime phase influence transcript freshness. In a recent managed Claude
session, `blocked AskUserQuestion` stopped active transcript polling. A later
user answer was not shipped by the hot lane; it was found by `discovery_scan`
and reached hosted ingest about 30 seconds after the provider transcript event.

That violates the launch product's realtime mental model.

## Non-Goals

- Do not redesign provider transcript parsers.
- Do not remove durable archive correctness, spool retry, or offset-based
  dedupe.
- Do not require a new pub/sub system.
- Do not infer provider process death from missing transcript changes.
- Do not make broad local-health or process scans part of browser update
  latency.

## Design Principles

### Transcript Shipping Is Mechanical

Shipping changed transcript bytes is a mechanical transport problem. It should
not depend on semantic phase classification.

### One Actor Owns Transcript Offset Advancement

Every known transcript file has a single logical tail actor. All wake sources
feed that actor; wake sources do not implement separate shipping semantics.

Current code has separate archive and live cursors (`file_state` and
`live_file_state`). The target actor may keep distinct live/archive cursors if
that remains necessary for provisional/live behavior, but one path-local actor
must own the decision to read, ship, retry, and advance those cursors. Parallel
lanes must not independently advance offsets for the same file.

### Repair Is Not The Live Path

Reconciliation scans are required for sleep, restart, dropped watcher events,
and path discovery. They are allowed to repair missed updates, but if dogfood
traffic normally ships through reconciliation, the hot lane is broken.

### Runtime Overlays Are Separate Facts

Runtime phase, provider process state, managed control-path state, and machine
state are overlays. They help the UI be honest, but they do not gate transcript
shipping and they do not replace transcript events.

### Newer Observations Win

Reducers compare observation time, not arrival time. A stale heartbeat lease
cannot overwrite a newer hook, transcript-derived phase, or process/control
observation merely because it arrived later.

## Target Architecture

### Transcript Hot Plane

```text
provider transcript append
  -> filesystem watcher wakes TranscriptTailActor(path)
  -> actor stats path and reads acked_offset..current_size
  -> actor parses complete JSONL records
  -> actor posts batch to Runtime Host
  -> server stores durable events and publishes session update
  -> browser/iOS refetch or apply update and paint
```

### Reconciliation Plane

```text
periodic scan finds known transcript paths
  -> wake same TranscriptTailActor(path) with source=reconciliation
  -> actor uses the same offset and POST path
```

Reconciliation is the same actor with a lower-priority wake reason. It is not a
separate shipping implementation.

### Runtime Overlay Plane

```text
provider transcript projection
process/control observation
machine heartbeat
  -> runtime/liveness reducer
  -> separate display axes
```

The runtime overlay may say:

- transcript is advancing
- provider process is running, gone, or unknown
- managed control path is attached, detached, degraded, or unknown
- machine is online, stale, offline, or unknown
- inferred phase is working, running tool, blocked, idle, inactive, or closed

These facts can be shown together, but they must remain distinguishable in API
payloads and telemetry.

## TranscriptTailActor State Machine

There may be multiple implementation shapes, but the observable behavior should
match this state machine.

### State

```text
TranscriptTailActor {
  path
  provider
  session_id_hint
  transcript_epoch_id
  archive_acked_offset
  live_acked_offset
  captured_offset
  delivered_offset
  in_flight_range
  pending_wake
  file_identity
  last_stat_size
  last_stat_mtime
  last_ship_result
}
```

### Inputs

```text
Wake(source=fsevent, observed_at)
Wake(source=reconciliation, observed_at)
Wake(source=manual_flush, observed_at)
ShipSucceeded(range, server_trace)
ShipFailed(range, retry_class)
FileRotated(new_identity)
FileTruncated(size)
Shutdown
```

`source` is telemetry and scheduling evidence. It must not change transcript
semantics.

### Behavior

On wake:

1. Stat the file.
2. Compare path plus file identity when available: device, inode, creation time,
   and provider-specific transcript id.
3. If file identity changed, resolve the current provider transcript identity.
4. If size is less than the relevant acked offset, handle truncation or
   rotation before reading.
5. If size equals the relevant acked offset, record a no-op wake and stop.
6. Read `acked_offset..size`.
7. Parse complete records only.
8. POST immediately.
9. On success, advance the appropriate delivered cursor only after server
   acceptance is known.
10. If file size advanced while a POST was in flight, loop immediately.
11. On retryable failure, spool the range and retry without losing offset
    ownership.

Cursor names must be explicit:

- `captured_offset`: highest offset read into local durable state or spool.
- `delivered_offset`: highest contiguous offset accepted by the Runtime Host.
- `archive_acked_offset` and `live_acked_offset`: only remain if live preview
  and durable archive intentionally keep separate cursors.

The implementation must define which cursor each API reads and writes. A vague
single "offset" is not acceptable for crash recovery.

### Forbidden Behavior

The actor must not:

- stop tailing because phase is `blocked`, `needs_user`, or `idle`
- require a hook/outbox phase signal to ship transcript bytes
- wait for heartbeat or local-health before shipping transcript bytes
- let reconciliation change parsing or offset semantics
- run broad process scans in the hot transcript path
- advance delivered cursors before the server durably accepts the range
- retire an actor solely because a provider process, bridge, or control path is
  idle, blocked, detached, or gone

## Wake Sources

### Required

`fsevent`

- Primary hot trigger.
- Expected source for normal managed transcript appends.
- Target: file change observation to HTTP send start p95 under 500ms on a
  healthy laptop/runtime network.
- Must be validated against real Claude and Codex write patterns on macOS. If
  native watcher events are coalesced or delayed under provider writes, the
  actor may use a phase-independent settle timer after observed changes. That
  timer is still part of the actor, not a semantic phase lane.

`reconciliation`

- Repair trigger.
- Periodic scan over known provider transcript paths and bindings.
- Expected to find missed changes after sleep, restart, watcher loss, or path
  discovery gaps.
- Target: no unshipped known transcript range survives more than one
  reconciliation interval when the engine is not under sustained live backlog.
  Under sustained live backlog, the hot lane must still drain active files; the
  scan target becomes diagnostic until backlog clears.

### Allowed But Non-Authoritative

`manual_flush`

- Operator or test command that wakes one actor.
- Useful for diagnostics and deterministic QA.

`provider_bridge_hint`

- Optional future optimization. A bridge can say "this transcript likely
  changed" but it must only wake the same actor.
- It cannot carry phase authority for transcript shipping.

`provisional_transcript_delta`

- Existing bridge-live/provisional transcript deltas are transcript-shaped data
  delivered through a runtime endpoint rather than the durable ingest endpoint.
- They must either be explicitly kept as a separate live preview surface or
  folded into the same actor/identity model. The implementation plan must not
  leave two uncoordinated transcript truth planes.

### To Remove Or Demote

`claude_hook_outbox_transcript_catchup`

- Remove as a transcript shipping lane.
- If hooks remain for provider-specific setup, they must not schedule
  transcript catch-up or update transcript offsets.

`active_transcript_poll`

- Remove as a phase-dependent lane.
- If temporary polling is kept for platform watcher validation or continuous
  append settling, it must be actor-internal, phase-independent, and protected
  by metrics proving why it fired. It must not start or stop because a session
  is `thinking`, `running`, `blocked`, `needs_user`, or `idle`.

`terminal_attention_catchup`

- Remove. `blocked`, `needs_user`, and `idle` do not change transcript shipping
  behavior.

`engine_attached_lease_phase_authority`

- Demote to control-path/liveness overlay.
- It may say "the managed session is attached to this machine."
- It must not overwrite a newer runtime phase using heartbeat receive time.

`unmanaged_hook_delay`

- Remove or quarantine the current unmanaged hook catch-up delay as a
  compatibility hack. A fixed 30 second delay is incompatible with the
  transcript-hot-plane model. If unmanaged sessions remain outside the managed
  realtime SLA, say that explicitly rather than embedding a hidden delay in the
  shipping path.

## Runtime Projection

Runtime display should be derived from facts in this order:

1. Explicit terminal fact with observed process/provider truth.
2. Transcript-derived activity and attention facts.
3. Fresh provider/process/control observations.
4. Stale overlays shown as stale or unknown, not as current phase truth.

Transcript-derived facts include:

- assistant/tool-use without matching result means running tool
- AskUserQuestion without a later answer means blocked on user input
- later user/tool result for the same interaction clears that block
- latest assistant response followed by provider prompt means idle/needs user

This projection can be conservative. It does not need to perfectly understand
every provider edge case to preserve the main invariant: transcript events are
the strongest evidence that the conversation advanced.

## Server Reducer Rules

Runtime events must carry the original observation time.

The reducer must reject or ignore stale overlays when:

```text
incoming.observed_at < current.last_runtime_signal_at
```

Heartbeat receive time is not a valid substitute for lease observation time
when comparing phase freshness.

Progress/transcript events may update recency and wake subscribers even when
they do not establish a runtime phase.

If reducer logic receives both provisional/live transcript updates and durable
archive events, their identities must reconcile by provider event id, source
offset, or another stable key. The user should not see duplicate content or a
phase rollback when durable archive catches up to live preview.

`observed_at` is not a universal last-write-wins clock. It is a freshness
attribute within a fact class. Transcript records are ordered by transcript
epoch plus byte offset and record index. Runtime/process/control facts are
ordered by their own observation times and source-specific strength. A later
process heartbeat cannot erase append-only transcript truth.

Server ingest must be idempotent across crash and retry windows. The stable
dedupe key should include transcript epoch identity, byte range or record index,
and record hash/provider event id. If a client times out after the server
commits, retry must not duplicate UI or reducer effects.

If a later byte range reaches the server before an earlier unacked range, the
server must either buffer it, reject it with a retryable gap response, or mark a
visible gap. Reducers must not derive final phase across unknown transcript
gaps.

## Telemetry Contract

Every shipped transcript range should have metadata-only timing:

```text
trace_id
session_id
provider
path_hash_or_redacted_path
source: fsevent | reconciliation | manual_flush | provider_bridge_hint
observed_at_ms
actor_wake_started_at_ms
read_started_at_ms
read_finished_at_ms
parse_finished_at_ms
http_send_started_at_ms
http_finished_at_ms
server_handler_entered_at_ms
server_store_returned_at_ms
sse_published_at_ms
client_rendered_at_ms when available
offset
new_offset
range_bytes
event_count
outcome
```

For coalesced watcher batches, telemetry should preserve both:

- first observed time for "how long has anything been waiting?"
- latest observed time for "how fast did the latest append ship?"

Without both fields, high-frequency append cases can falsely look slow because
many appends collapse into one older observed timestamp.

Client render beacons must be persisted or sampled in a queryable store with
`session_id`, `event_id`, and timing fields. A future report of "this session
felt stale" should be answerable without local log archaeology.

Parser telemetry must distinguish:

- incomplete trailing record, cursor not advanced
- malformed complete record, quarantined or emitted as parse-error evidence
- successful complete record, cursor advanced after acceptance

Silently dropping a malformed complete line is not acceptable for a live mirror
unless the drop is visible in telemetry and cannot wedge later records.

## QA And Invariant Testing

Implementation must not start by deleting code and hoping existing tests catch
regressions. The first implementation phase is a harness and invariant suite.

### Engine Unit Invariants

Use fake transcript files and a fake ship client.

Required cases:

- append while `thinking` ships through the hot file-change trigger
- append while `running` ships through the hot file-change trigger
- append while `blocked AskUserQuestion` ships through the hot file-change
  trigger
- append while `needs_user` ships through the hot file-change trigger
- append while `idle` ships through the hot file-change trigger
- multiple appends while one POST is in flight coalesce without losing bytes
- POST succeeds then engine crashes before cursor persist
- POST commits server-side but client times out and retries
- cursor persist attempted before server durable accept is rejected by tests
- server sees later offset before earlier offset and handles the gap explicitly
- retryable POST failure spools and eventually advances only after success
- spool pending plus newer live bytes after restart does not skip unacked bytes
- file truncation does not corrupt offsets
- file rotation resolves a new transcript identity
- path-only identity does not double-ship when symlinks or canonical paths
  differ
- actor GC does not depend on provider phase or control-path state
- actor retires only after reconciled quiescence or explicit tombstone with
  cursor at EOF
- reconciliation wakes the same actor and ships a missed range
- reconciliation does not produce a different parse result from file-change
  wake
- watcher batch coalescing records both first and latest observed timestamps

Core invariant:

```text
For every provider transcript append after actor binding, if the file remains
readable and the Runtime Host is reachable, the range is posted once and only
once regardless of runtime phase.
```

### Server Reducer Invariants

Use direct reducer tests.

Required cases:

- stale heartbeat lease cannot overwrite newer hook/transcript-derived phase
- heartbeat lease uses original `observed_at` for ordering
- transcript progress wakes subscribers even when no phase changes
- blocked AskUserQuestion clears when later transcript evidence advances
- process-gone closes lifecycle without pretending transcript changed
- machine offline marks host state stale/offline without closing session
- durable archive catch-up reconciles provisional/live transcript events without
  duplicate display or phase rollback
- duplicate transcript record retry is idempotent
- missing, duplicated, skewed, or non-monotonic provider timestamps do not break
  transcript ordering because byte offset remains causal
- invalid complete JSONL line produces visible parse-error evidence or
  quarantine behavior instead of silent permanent loss

### Provider Fixture Matrix

Build provider transcript fixtures for Claude and Codex.

Claude required fixtures:

- assistant text
- tool use and tool result
- AskUserQuestion tool use
- AskUserQuestion answer append
- provider idle/needs-user end state
- graceful exit
- process killed
- file created after managed session launch
- transcript file rotated or truncated
- final valid JSON object without trailing newline remains pending until
  newline or explicit provider-close handling

Codex required fixtures:

- assistant text
- shell/tool call and result
- user message while managed
- graceful close
- interrupt/cancel if supported by the current managed path
- process killed
- rollout file created after managed session launch
- bridge-live/provisional delta followed by durable archive catch-up
- duplicate retry of the same byte range

Fixture assertions:

- parsed durable events match expected event sequence
- runtime projection matches expected display axes
- transcript append always wakes the hot actor in managed sessions

### Integration Harness

Before broad refactor, add a deterministic local integration harness:

```text
fake provider writer
fake Runtime Host ingest endpoint
Machine Agent under test
browser/SSE observer optional in later phase
```

The harness should write transcript records to real temp files and assert:

- file append to HTTP send start
- HTTP payload offsets
- server ingest order
- SSE publication
- optional browser paint beacon
- process/control overlay changes that happen while transcript shipping is in
  flight
- crash/restart windows around server accept and cursor persistence

It must support both Claude-shaped and Codex-shaped JSONL.

### End-To-End Dogfood Scenarios

Promote to the SLA matrix only after the harness passes.

Required warm managed scenarios:

- managed Claude: blocked AskUserQuestion, user answers after 30 to 60 seconds,
  browser updates under target
- managed Claude: long running tool sequence, browser sees each transcript
  append under target
- managed Claude: graceful close and process kill produce correct separate
  lifecycle/process facts
- managed Codex: live output append, tool result, and close under target
- managed Codex: control-path detach does not stop transcript shipping

Targets:

- file append to HTTP send start p95 under 500ms
- file append to hosted DB commit p95 under 1000ms
- file append to warm browser paint p95 under 1500ms
- reconciliation repaired range visible within one reconciliation interval
- zero stale phase overwrites in reducer invariant tests

## Implementation Phases

### Phase 0 - Spec And Review

Deliverables:

- This spec reviewed by Hatch Opus.
- A short Hatch Expert pass over the state-machine logic.
- Open questions resolved or explicitly carried into Phase 1.

Success criteria:

- The spec defines transcript triggers, runtime overlays, state machines,
  invariants, QA gates, and rollout order.
- Review does not identify a missing provider lifecycle class.

### Phase 1 - Measurement And Harness

Deliverables:

- Engine-level test harness around fake transcript files and fake ship client.
- Provider watcher validation on macOS for Claude and Codex append behavior,
  including create, append bursts, sleep/wake if feasible, truncation, and
  rotation-like replacement.
- Server reducer tests for stale observed-at ordering.
- Persisted or queryable client render telemetry plan, with at least a minimal
  implementation if needed for E2E validation.
- Flight recorder or equivalent actor trace fields for source, actor timing,
  HTTP timing, server trace, and event count.
- Idempotent ingest and cursor semantics documented for crash windows, including
  timeout-after-server-commit and spool-before-newer-live-byte cases.

Success criteria:

- Current behavior can reproduce the `blocked AskUserQuestion` delayed-shipping
  class in a failing or diagnostic test.
- Invariant tests exist before shipping behavior is changed.
- The spec has a concrete decision on whether bridge-live/provisional deltas
  are folded into this plane or explicitly retained as a separate live-preview
  surface.

### Phase 2 - Transcript Actor Refactor

Deliverables:

- Per-path transcript tail actor or equivalent scheduler simplification.
- FSEvents and reconciliation wake the same actor.
- Phase-dependent transcript catch-up removed or hard-disabled.
- Claude hook/outbox no longer schedules transcript shipping.
- Active transcript polling removed or made phase-independent and
  actor-internal with telemetry proving it is not the normal path.
- Actor owns live/archive cursor advancement or explicitly documents why two
  cursors remain and how they are kept coherent.
- Actor state is keyed by transcript epoch identity, not path alone. Path is an
  address; epoch is the thing whose offsets are meaningful.

Success criteria:

- All Phase 1 invariants pass for Claude and Codex fixtures.
- Managed `blocked`, `needs_user`, and `idle` append cases ship via hot trigger,
  not reconciliation.

### Phase 3 - Runtime Overlay Cleanup

Deliverables:

- Heartbeat leases use lease `observed_at` for reducer ordering.
- Managed attachment/control-path facts are separated from phase authority.
- Transcript-derived phase/progress projection clears stale blocked states.
- API payloads expose transcript recency, runtime phase, process state, control
  path state, and host state as separate facts.

Success criteria:

- Stale lease overwrite tests pass.
- Browser and iOS can distinguish "transcript advanced" from "control path
  attached" and "process state unknown."

### Phase 4 - End-To-End Promotion

Deliverables:

- SLA matrix scenarios for managed Claude and managed Codex warm transcript
  propagation.
- Browser paint telemetry captured for the key warm scenarios.
- Dogfood run artifacts saved with trace IDs and exact commit SHA.

Success criteria:

- Warm managed Claude AskUserQuestion answer paints under target.
- Warm managed Codex live output paints under target.
- Reconciliation repair is proven but not used as the normal live path.

### Phase 5 - Deploy And Verify

Deliverables:

- Runtime and engine changes shipped.
- David's local dogfood engine refreshed.
- Hosted demo/canary verified against exact commit SHA.
- Post-deploy dogfood scenario run.

Success criteria:

- Live hosted surface shows file append to browser paint within target.
- Engine flight recorder shows normal managed transcript appends coming from
  `fsevent` or equivalent hot actor wake, not `reconciliation`.
- No regression in durable archive correctness.

## Open Questions

- Can native macOS FSEvents reliably observe Claude JSONL appends under the
  current provider write pattern, or do we need a phase-independent actor
  settle timer after each observed append?
- Should transcript-derived phase live server-side only, or should clients also
  receive enough raw axes to render conservative local projections?
- What is the minimum persisted render telemetry retention window for dogfood
  debugging without creating a new analytics product surface?
- Do unmanaged imported sessions get the same hot transcript contract when a
  process binding exists, or only managed sessions?
- Should `file_state` and `live_file_state` remain distinct long term, or can
  live preview and durable archive share one cursor plus provisional event
  reconciliation?
