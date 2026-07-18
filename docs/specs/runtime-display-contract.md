# Session State and Runtime Display Contract

Status: Accepted target contract; current implementation is transitional
Last updated: 2026-07-11
Owner: Longhouse session core
Implementation epic: `docs/specs/managed-session-state-normalization-epic.md`

This is the canonical semantic contract for session state, runtime evidence,
control availability, transcript convergence, and their human presentation.
It is the semantic companion to `speed-of-light-database.md` and
`session-identity-kernel.md`.

It supersedes the state semantics in:

- `managed-idle-timeline-status.md`;
- `transient-transcript-placeholder-ux.md`; and
- the former six-axis `runtime_display` contract in this file.

Those documents remain useful incident history, but they are not design
authority.

## Decision

Longhouse has no authoritative combined "session status."

The kernel preserves independent facts about:

1. session disposition and run lifecycle;
2. provider activity;
3. control ownership, connection evidence, and action grants;
4. pending structured interactions;
5. transcript convergence; and
6. host/process observation.

Human labels and action availability are versioned projections over those
facts. A projection may never be written back as evidence or consumed by
another projection.

The immediate product consequence is:

```text
activity unknown + healthy send-capable control
    => Activity unknown · Live control
    != Ready
    != Idle
```

`Ready` is deleted as a session-status word. It confuses control reachability
with provider quiescence. `No live signal` is replaced by the scoped claim
`Activity unknown` when a current run exists but no fresh activity evidence
does.

## Why this is stable

Each canonical dimension has a different authority and clock. Real sessions
exercise them independently:

- a Shadow session can have fresh activity and transcript progress with no
  control ownership;
- a Helm session can retain live control after its activity evidence expires;
- transcript rendering can lag while the provider is idle;
- a host can be online while a particular connection is gone;
- a run can end while its session and searchable history remain open; and
- a durable question can remain pending after activity becomes unknown.

Any enum that tries to flatten those combinations must either lie or grow a new
value for every cross-product. That is the failure mode this contract removes.

## Canonical nouns

The identity nouns remain those from `session-identity-kernel.md`:

- **Session**: durable product/timeline identity.
- **Thread**: causal provider continuity inside a session.
- **Run**: one provider-process invocation. Resume after process exit creates a
  new run; an ended run never reopens.
- **Connection**: Longhouse's relationship to a run.
- **Lease**: expiring evidence that a connection is reachable now.

Shadow, Helm, and Console are provenance/interaction modes. Mode alone never
grants an action or proves liveness.

## Canonical facts

### Session disposition

Stored facts:

```text
closed_at: timestamp | null
close_reason: user_closed | api_closed | null
```

An open session has `closed_at = null`. Closure is explicit and monotonic.
Provider quietness, host expiry, process disappearance, and transcript age do
not close a session.

Deletion is not a runtime disposition. It is a separate catalog tombstone and
retention revision governed by `speed-of-light-database.md`.

### Launch and run lifecycle

Stored facts:

```text
launch_attempt: pending | dispatched | failed | adopted | abandoned
run: run_id, started_at, ended_at?, end_reason?, process identity
```

The API may derive:

```text
starting | running | ended | unknown
```

- `starting` requires a current launch attempt and no adopted run yet.
- `running` requires positive run/process evidence.
- `ended` requires an explicit matching process exit or omission from a newer,
  complete process snapshot for the same process identity.
- heartbeat or lease expiry yields `unknown`, never `ended`.
- work after `ended` creates a new `run_id`.
- reconnecting a control transport for the same live run changes connection
  evidence only.

### Provider activity

One bounded head is retained per `(run_id, source)`:

```text
kind: thinking | executing | quiescent | blocked | stalled
raw_kind: provider value
tool: string | null
detail: string | null
source
source_epoch: string | null
source_seq: integer | null
observed_at
received_at
valid_until
evidence_hash
raw_locator: object/record locator | null
```

Rules:

- `thinking` means the provider is processing without a named tool.
- `executing` means a named tool/action is running.
- `quiescent` is the normalized machine term for an ordinary provider
  `idle` or `needs_user` prompt. Both render **Idle**. A provider's bare
  `needs_user` phase is not durable proof that the user owes an answer.
- `blocked` is accepted only from provider evidence that explicitly describes
  a block. A structured question or approval is modeled separately.
- `stalled` is accepted only when a provider explicitly emits that fact. Lack
  of progress expires to unknown; Longhouse does not synthesize a stall.
- Unknown raw phases remain in `raw_kind` and immutable evidence. They do not
  coerce to `idle`, `quiescent`, or another known value.
- Expiry changes effective presentation eligibility; it never overwrites the
  stored observation.

The API represents absence or expiry explicitly with `activity.state` set to
`unknown`. The database need not store an invented `unknown` observation.

### Control and actions

Stored facts are connection provenance plus leases and grants:

```text
ownership: owned | unowned
connection_id
acquisition_kind
transport
lease_generation
observed_at
valid_until
revoked_at
supported_operations
granted_operations
```

The effective API connection is:

```text
connected | degraded | disconnected | unknown | not_applicable
```

- A valid, non-revoked lease may project `connected`.
- An explicit recoverable transport failure may project `degraded`.
- Explicit close/revocation may project `disconnected`.
- Lease expiry projects `unknown`, not `disconnected`.
- Unowned Shadow control projects `not_applicable`.

`Reattach` is an action, not a connection state. Each operation is projected
independently:

```text
available | unavailable | unknown
reason: unsupported | not_granted | no_active_run | run_ended |
        session_closed | observe_only | control_degraded |
        control_disconnected | control_freshness_unknown | ...
```

These response values are advisory UI facts. A command must revalidate current
lease generation, authorization, provider support, and grants transactionally.
No cached capability response authorizes a write.

### Pending interaction

Structured interaction is durable and explicit:

```text
kind: question | permission | approval
opened_at
resolved_at: timestamp | null
provider_request_id
```

Only an unresolved interaction can render `Needs answer` or `Needs approval`.
An ordinary `needs_user` phase without an interaction row renders `Idle`.
Transcript progress does not silently resolve an interaction; an explicit
resolution event does.

### Transcript convergence

Stored heads:

```text
source_revision
durable_revision
render_revision
last_append_at
```

Convergence is derived as `current | lagging | unknown`. Parser/render/ingest
errors are diagnostics with typed failure codes, not another convergence value.

Transcript lag is never provider activity. In particular,
`syncing_transcript` is deleted from the provider-activity vocabulary and must
never render as `Working`. A transcript placeholder or quiet catching-up badge
may remain while revisions converge.

### Host and process observation

Host heartbeat is machine-scoped truth. Process observations are keyed by host,
boot identity, pid, and process start identity. Session responses may reference
or project those facts, but must not maintain a second authoritative copy.

Host/process facts may influence run lifecycle and diagnostics. They may not
manufacture provider activity or grant control.

## Evidence and reducer rules

The catalog stores bounded current heads, not an unbounded runtime event log.
Exact historical evidence remains in immutable operational/raw objects when it
exists. Ephemeral activity and lease state may honestly restore as unknown;
durable closure, run endings, interactions, inputs, and tombstones must survive
restore as first-class catalog facts.

Every accepted observation carries, where the source can supply them:

```text
subject ids
fact kind and attributes
producer/source identity
source epoch and sequence (optional)
observed_at (optional)
received_at
validity policy/result
content hash/dedupe key
raw evidence locator (optional)
```

Do not invent epoch, sequence, or provider time for a source that lacks it.
Keep one candidate head per source when evidence is incomparable, then choose
the effective fact with a documented authority order and deterministic tie
break. Do not destroy a competing candidate merely to force last-write-wins.

Reducer invariants:

1. Control evidence cannot change provider activity.
2. Activity evidence cannot grant control or close a session.
3. Transcript progress cannot establish provider activity.
4. TTL expiry cannot produce an explicit negative fact.
5. Session closure and run termination are monotonic.
6. Evidence for run N cannot mutate run N+1.
7. Source sequence is compared only within its declared source epoch.
8. Duplicate evidence is idempotent; the same source position with different
   content is a typed conflict.
9. Signal time outranks write time inside a trusted ordering domain.
10. A projection consumes facts, never labels, tones, or another projection.
11. UI labels never write facts. Explicit commands may append command/receipt
    facts through their normal authority path.
12. Replay reproduces current heads. A time-dependent presentation is required
    to reproduce only for the same facts, evaluation time, and policy version.

## API contract

The canonical `/api/agents/*` session response exposes the independent facts
once. Browser timeline/detail and iOS consume bounded subsets of the same
contract; they do not define another state machine.

Representative shape:

```json
{
  "state_contract_version": 1,
  "presentation_policy_version": 1,
  "mode": "helm",
  "session": {"closed_at": null, "close_reason": null},
  "run": {"id": "...", "lifecycle": "running", "started_at": "..."},
  "activity": {
    "state": "unknown",
    "raw_kind": "idle",
    "tool": null,
    "source": "codex_bridge",
    "observed_at": "...",
    "valid_until": "..."
  },
  "control": {
    "ownership": "owned",
    "connection": "connected",
    "observed_at": "...",
    "valid_until": "...",
    "actions": {
      "send_input": {"state": "available", "reason": null},
      "interrupt": {"state": "available", "reason": null},
      "reattach": {"state": "unavailable", "reason": "already_connected"}
    }
  },
  "pending_interaction": null,
  "transcript": {
    "convergence": "current",
    "source_revision": 42,
    "durable_revision": 42,
    "render_revision": 42
  },
  "presentation": {
    "primary": {"key": "activity_unknown", "label": "Activity unknown", "tone": "quiet"},
    "access": {"key": "live_control", "label": "Live control", "tone": "connected"}
  },
  "commit_seq": 1234
}
```

English labels remain server-owned for current web/iOS parity. Stable keys and
parameters ship beside them so localization does not require changing the fact
contract later. `presentation.primary` is nullable when there is no current run
and therefore no honest runtime claim to make.

The server owns TTL policy and returns `valid_until`. A client may demote an
already-rendered fact when that timestamp passes, or consume a server expiry
event. It may never choose a longer TTL, promote a fact, infer a negative fact,
grant an action, or feed the demotion back to the server.

## Human projection

### Primary label

Ordered precedence:

1. explicit session closure → `Closed`;
2. current launch attempt without a run → `Starting`;
3. unresolved question → `Needs answer`;
4. unresolved permission/approval → `Needs approval`;
5. fresh `thinking` → `Thinking`;
6. fresh `executing(tool)` → `Using <tool>`;
7. fresh explicit `stalled` → `Stalled`;
8. fresh explicit `blocked` → `Blocked`;
9. fresh `quiescent` → `Idle`;
10. explicitly ended current run → `Ended`;
11. otherwise, when a current non-ended run exists → `Activity unknown`;
12. with no current run → no primary runtime label.

There is no `Ready` primary label. Transcript lag never selects a primary
activity label.

### Access label

The access label is a presentation-only summary of independent action and
transcript facts. It is not reducer input and does not authorize commands.

- `Live control`: owned, current connection with at least one live interactive
  action available. Individual buttons still use per-action gates.
- `Reattach`: no live interactive action, but the reattach action is available.
- `Observe only`: no owned control, but live transcript observation is fresh.
- `Search only`: no live control or live transcript observation; durable
  searchable history exists.
- a typed degraded/unavailable label may replace these when the user can act on
  a real control fault.

This preserves the launch vocabulary while keeping its underlying facts
separable. `Observe only` and `Search only` never become stored connection or
activity values.

### Representative truth table

| Mode | Lifecycle/activity facts | Control/transcript facts | Primary | Access |
| --- | --- | --- | --- | --- |
| Helm | running + fresh thinking | connected, send available | Thinking | Live control |
| Helm | running + fresh quiescent | connected, send available | Idle | Live control |
| Helm | running + expired activity | connected, send available | Activity unknown | Live control |
| Helm | running + expired activity | reattach available | Activity unknown | Reattach |
| Shadow | running + fresh thinking | live transcript, unowned | Thinking | Observe only |
| Shadow | running + fresh quiescent | live transcript, unowned | Idle | Observe only |
| Console | launch pending, no run | no valid control yet | Starting | — |
| Console | running + fresh executing | connected, interrupt available | Using <tool> | Live control |
| Any | unresolved question | control independent | Needs answer | derived independently |
| Any | fresh quiescent + transcript lag | control independent | Idle | derived independently |
| Any | explicitly ended run | searchable history | Ended | Search only |
| Any | no current run | searchable history | — | Search only |
| Any | explicitly closed session | any stale evidence | Closed | — |

Every displayed timestamp belongs to the fact behind that exact label:
activity labels use activity observation time; control labels use lease time;
transcript labels use revision/append time. A timestamp from one axis may never
decorate another axis's claim.

## Storage mapping for `catalogd`

The speed-of-light catalog uses bounded tables/facts:

- `sessions` with explicit closure fields and tombstone relation;
- `session_threads`, `session_runs`, `session_connections`;
- `launch_attempts`;
- `activity_heads`, one per live `(run, source)` candidate;
- `control_leases`, generation-keyed and expiring;
- `pending_interactions`;
- `transcript_heads` with source/durable/render revisions;
- machine-scoped heartbeat/process heads;
- `input_receipts` and other action receipts.

There is no generic mega-status row, duplicated timeline-card state, unbounded
runtime-observation table, or provider activity synthesized from leases.

## Migration and deletion

The migration is a verified cutover, not two competing truth systems:

1. Implement the new facts projector over existing rows and compare its output
   against production scenarios without serving it.
2. Add the bounded catalog tables and one authoritative reducer/projector.
3. Dual-read only for parity verification; never dual-write two reducers.
4. Version the API, migrate web/iOS/CLI consumers, then switch serving to the
   new contract.
5. A short compatibility window may derive old response fields from new facts
   for an older client, but those aliases are read-only, server-owned, and have
   a fixed deletion release.
6. After parity and client cutover, delete the old fields, reducers, and tables.

Target deletions include:

- `SessionRuntimeState` and `LiveRuntimeState`;
- duplicated live/archive runtime winner and timeline-card reducers;
- `runtime_display` truth/signal tiers, activity-recency categories, tones as
  authority, and redundant booleans such as `is_live`, `is_idle`, and
  `has_signal`;
- `_managed_control_ready_for_timeline` and every label/copy handshake;
- `syncing_transcript` as provider activity;
- unknown-phase-to-idle coercion;
- `ManagedSessionLease.phase` and all control-heartbeat phase synthesis;
- read-time mutation that rewrites stored connection state;
- legacy combined `status`, `display_phase`, `confidence`, `presence_state`,
  `control_label`, `observe_only`, and `search_only` response authority after
  clients consume the new contract.

The server presentation projector remains. It consumes canonical facts and
emits stable keys/copy; it does not become evidence.

## Verification

Required tests:

- axis noninterference: changing one fact family leaves the others unchanged;
- duplicate/idempotency and source-epoch ordering;
- run isolation and terminal monotonicity;
- expiry boundaries immediately before, at, and after `valid_until`;
- explicit-negative versus expired-to-unknown behavior;
- control heartbeat cannot alter activity;
- transcript progress cannot alter activity or resolve interaction;
- command-time action gating revalidates lease generation;
- replay reproduces bounded heads;
- pairwise decision-table coverage for all primary/access labels;
- golden parity across `/api/agents/*`, timeline, detail, SSE, web, and iOS at
  the same `commit_seq` and presentation policy version;
- regression fixtures for `Idle · Live control`, `Activity unknown · Live
  control`, `Thinking · Observe only`, `Ended · Search only`, pending
  interaction, transcript lag, and closed-session dominance.

Do not generate the full Cartesian product of every field. Prove reducer
algebra and axis noninterference, then cover the small ordered presentation
decision table pairwise.
