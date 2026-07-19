# Managed Session State Normalization Epic

Status: Proposed implementation epic
Last updated: 2026-07-18
Owner: Longhouse session core + Machine Agent
Related:

- `VISION.md`
- `docs/specs/runtime-display-contract.md`
- `docs/specs/session-identity-kernel.md`
- `docs/specs/managed-provider-session-contract.md`
- `.agents/skills/managed-provider-cli/SKILL.md`

## Executive Summary

Longhouse currently has an accepted canonical session-state contract but does
not yet have one authoritative implementation of it. Provider observations are
reduced through several overlapping paths before the canonical
`SessionStateFacts` projector sees them:

1. provider-specific Rust scanners and bridge observations;
2. managed control leases that also carry provider activity;
3. two Machine Agent phase stores;
4. archive and hot server runtime reducers;
5. Python local-health projection; and
6. separate Desktop, web, and iOS presentation logic.

The result is not merely inconsistent copy. The same session can be `Idle`,
`Ready`, `Activity unknown`, `Inactive`, or `Completed` on different surfaces.
More seriously, heartbeat omission can synthesize `process_gone`, and later
phase or transcript evidence can clear that terminal state without creating a
new run. Control freshness, activity, process lifecycle, transcript convergence,
and client attachment are not reliably independent.

This epic completes the migration already required by
`runtime-display-contract.md`:

- provider adapters emit typed evidence rather than combined statuses;
- one bounded reducer owns current heads for every fact family;
- run termination and session closure are monotonic;
- control leases contain no provider activity;
- process identity survives the machine-to-server boundary;
- command authorization uses current per-action grants;
- Desktop, web, iOS, timeline, detail, SSE, and `/api/agents/*` consume the same
  versioned facts and presentation policy; and
- legacy reducers and aliases are deleted after measured parity.

The product outcome is deliberately simple: a session row stays stable while
its independent activity and access facts change in place. Users never need to
understand app servers, relays, bridge sidecars, hook inboxes, or provider
process topology to decide whether an agent is working, needs them, can be
controlled, can be reattached, or has ended.

## User Problem

The motivating incident combined intermittent network failures with old managed
Codex runtimes. Durable upload recovered, but the menu continued to report
`Remote control unavailable` and placed broken old runtimes under `Ready and
background`. Five current managed sessions and three process residues appeared
as eight peer sessions. The UI was technically exposing implementation facts,
but the user could not tell:

- which five rows corresponded to current terminal sessions;
- whether `background` meant an intentional Console run or an orphan;
- whether a detached row was recoverable, still executing, or merely residue;
- whether remote control was unavailable globally or only for old sessions; or
- whether stopping a row was safe.

The audit showed that this ambiguity is systemic. `background`, `attached`,
`detached`, `finished`, `ready`, `live`, and `idle` are used across different
axes and sometimes mean different things by provider or client.

## Why This Is Not a Menu-Bar Project

Changing menu copy alone would add another presentation mapper over uncertain
facts. The menu currently consumes a local-health model that bypasses
`SessionStateFacts`, merges local phase state with legacy SSE projection, and
infers action availability from provider-specific state. A polished UI over
that model could still:

- call expired activity `Ready`;
- call a stale lease `Connected`;
- expose a stop affordance that the provider cannot perform;
- declare a run ended because one heartbeat omitted it; or
- show different truth than web and iOS.

The menu redesign is therefore a client-cutover phase in this epic. Small copy
guardrails may land earlier, but no new menu-only status model becomes
authoritative.

## Current Implementation Seams

These are the primary code boundaries an implementation session should inspect
before changing behavior:

| Concern | Current seam |
| --- | --- |
| Provider lease building and resolved local sessions | `engine/src/heartbeat.rs` |
| Provider scanners | `engine/src/managed_bridge_scan.rs`, `managed_claude_scan.rs`, `managed_opencode_scan.rs`, `managed_cursor_helm_scan.rs` |
| Machine phase stores/double write | `engine/src/state/session_phase.rs`, `engine/src/state/managed_session_state.rs`, `engine/src/outbox.rs` |
| Managed heartbeat reconciliation | `server/zerg/routers/heartbeat.py` |
| Archive/hot runtime reduction | `server/zerg/services/session_runtime.py`, `live_session_state.py` |
| Kernel capability projection | `server/zerg/services/agents/kernel_capabilities.py` |
| Canonical target projector | `server/zerg/services/session_state_contract.py`, `session_liveness_facts.py` |
| Local-health projection | `server/zerg/services/local_health/` |
| Provider static contracts/readiness | `schemas/managed_providers.yml`, `server/zerg/config/managed_provider_contracts.json`, `engine/src/control_channel.rs` |
| Desktop local reducer and presentation | `desktop/LonghouseMenuBarHarness/Sources/LonghouseMenuBarCore/` |
| Web client projection | `web/src/lib/sessionRuntime.ts`, `web/src/lib/sessionWorkspace/interaction.ts` |
| iOS client projection/action gates | `ios/Sources/Shared/SessionModels.swift`, `SessionAPIAdapters.swift` |

This table is navigation, not authority. The accepted contract and the tests
created by this epic determine semantics.

## Scope

In scope:

- Codex, Claude, OpenCode, Cursor, and Antigravity managed-provider adapters;
- Shadow observations where they share activity, process, transcript, or host
  facts with managed sessions;
- Machine Agent observation storage and heartbeat wire contracts;
- Runtime Host fact storage, reduction, capabilities, presentation, and command
  authorization;
- local-health and Longhouse.app;
- web and iOS state/action projections;
- `/api/agents/*`, timeline, detail, workspace/SSE, and machine-control parity;
- cross-provider and cross-surface conformance tests; and
- deletion of superseded state fields, reducers, and compatibility paths.

Out of scope:

- making provider process topologies identical;
- distributing, pinning, or patching provider binaries;
- moving execution between machines;
- adding model judgment to deterministic liveness or authorization rules;
- replacing SQLite;
- building an unbounded operational event warehouse;
- redesigning transcript storage beyond the revision heads needed for honest
  convergence; and
- introducing client presence unless a concrete product requirement survives
  Desktop cutover.

## Product Decisions

### One session row, independent facts

There is no combined authoritative session status. A surface receives one
session identity plus independent facts for:

- disposition;
- launch and current run lifecycle;
- provider activity;
- pending interaction;
- control ownership, connection, and actions;
- transcript convergence;
- host and process observation; and
- versioned presentation.

The user-facing row stays in one stable session list. Activity transitions do
not move the row between `Working`, `Needs you`, and `Ready` sections.

### Provider mechanics remain provider-specific

Providers normalize into shared semantic roles, not shared process counts.

| Semantic role | Codex | Claude | OpenCode | Cursor | Antigravity |
| --- | --- | --- | --- | --- | --- |
| Execution evidence | app-server/process identity | Claude CLI process | `opencode serve` | Helm launcher + Cursor process | provider/hook invocation evidence |
| Control evidence | bridge + relay + subscribed thread | native channel | authenticated server bridge | Helm socket | hook inbox claim/receipt |
| Local terminal client | remote TUI attachment | foreground TUI/PTY | `opencode attach` | Helm PTY | provider CLI process |
| Activity evidence | bridge/hooks/rollout | hooks/channel | plugin/provider events | native hooks | hook events |
| Provider diagnostics | app-server, relay, bridge, thread subscription | channel/process/PTY | server health/auth/attach | launcher/socket/child | inbox, claim, continuation |

These differences affect evidence and supported actions. They do not create
provider-specific meanings for `running`, `connected`, `unknown`, `ended`, or
`available`.

### Raw evidence is preserved before normalization

Deterministic code handles mechanics: process identity, schemas, timestamps,
ordering, TTLs, leases, grants, validation, and exact provider mappings. It
does not pre-collapse unknown raw provider states into `idle`, discard source
identity, or overwrite competing evidence simply to produce a convenient
label.

Provider-specific raw evidence remains inspectable for agents, local repair,
and debugging. Product clients consume canonical facts and do not parse raw
diagnostics.

### Absence and expiry are not negative facts

- lease expiry means control freshness is `unknown`;
- activity expiry means activity is `unknown`;
- heartbeat omission alone does not end a run;
- host expiry alone does not end a run;
- a missing TUI does not end a Codex app-server run;
- transcript quietness does not prove provider idle; and
- missing evidence never grants or revokes an action without the corresponding
  rule and authority.

### Terminal facts are monotonic and run-scoped

Once run N ends, no phase, transcript, lease, or replay for run N may reopen it.
New work creates or identifies run N+1. Explicit session closure dominates all
stale runtime evidence and is also monotonic.

### Every command revalidates

Client action state is advisory presentation. Send, steer, interrupt, answer,
reattach, resume, and terminate must transactionally revalidate:

- session disposition;
- run identity and lifecycle;
- host identity/reachability where required;
- connection/lease generation and freshness;
- provider/adapter support;
- current operation grant; and
- authorization.

No legacy boolean, cached capability payload, presentation label, or provider
binary-presence check authorizes a write.

## Canonical Vocabulary

This epic adopts `runtime-display-contract.md` without adding a competing
taxonomy.

```text
Session mode:          shadow | helm | console | unknown
Session disposition:   open | closed
Launch:                pending | dispatched | failed | adopted | abandoned
Run lifecycle:         starting | running | ended | unknown
Activity:              thinking | executing | quiescent | blocked | stalled | unknown
Control ownership:     owned | unowned
Control connection:    connected | degraded | disconnected | unknown | not_applicable
Action:                available | unavailable | unknown
Transcript:            current | lagging | unknown
Host:                  online | stale | offline | unknown
Interaction:           question | permission | approval
```

Legacy session-state words scheduled for deletion:

- `Ready`;
- `Ready and background`;
- `No live signal`;
- `syncing_transcript` as activity;
- `finished` as activity;
- generic `background` as a session state;
- `managed_attached`, `managed_detached`, and `managed_degraded` as combined
  presentation authority; and
- rolled-up `control_label` as reducer input.

`Idle` remains the presentation of a fresh `quiescent` activity fact. It does
not imply live control. `Activity unknown` remains distinct from `Idle`.

## Target Architecture

```text
Provider CLI / bridge / hooks / process scan
                    |
                    v
       Provider-specific observation adapter
                    |
                    v
          typed ObservationEnvelope records
                    |
                    v
       one bounded fact-head reducer in SQLite
                    |
          +---------+----------+
          |                    |
          v                    v
  SessionStateFacts     provider diagnostics
          |
          v
 versioned presentation + per-action availability
          |
    +-----+------+--------+---------+
    |            |        |         |
 /api/agents   web/iOS  Desktop   command validation
```

The observation adapter is allowed to know that Codex has an app server and a
TUI, that Claude uses a native channel, or that Antigravity is hook-driven. The
bounded reducer is not.

### Authority placement and offline Desktop

The Runtime Host reducer is the only durable authority for effective session
facts and presentation. The Machine Agent owns raw local observations, their
delivery state, and a bounded observation cache; it does not own a second
durable session-state reducer.

When the Runtime Host is reachable, Desktop consumes the same canonical
projection as other clients. When it is unreachable, Desktop may consume an
explicitly scoped `machine_preview` generated from current local observations
and the same machine-readable vocabulary/policy. That preview:

- carries machine/source sequence and policy version, not a Runtime Host
  `commit_seq`;
- omits or marks unknown any fact requiring server-owned durable evidence;
- is visibly local/stale when its inputs expire;
- is never merged field-by-field with a server projection;
- never authorizes a command; and
- is never written back as canonical evidence.

This fallback preserves offline local-health usefulness without quietly
creating a seventh authority. Parity tests cover its honest subset separately
from same-commit Runtime Host client parity.

## Provider Adapter Contract

### Static declaration

The existing managed-provider manifest remains the source for static provider
semantics:

```text
ProviderAdapterDeclaration {
  provider
  provider_binary_name
  control_plane
  launch_modes
  supported_operations
  proof_level: hermetic | live_no_token | live_token | none
}
```

Static support answers “can this adapter ever perform the operation?” It does
not answer “can this session perform it now?”

### Observation envelope

Each adapter emits zero or more typed evidence records:

```text
ObservationEnvelope {
  schema_version
  subject {
    session_id
    thread_id?
    run_id?
    machine_id?
  }
  provider
  fact_family: run | process | activity | control | interaction | transcript | host
  attributes
  source
  source_epoch?
  source_seq?
  observed_at?
  received_at
  valid_until?
  evidence_hash
  dedupe_key
  raw_locator?
  complete_snapshot? {
    scope
    machine_boot_id
    captured_at
  }
}
```

Rules:

- do not invent provider time, epoch, or sequence;
- compare sequence only within a declared source epoch;
- retain `raw_kind` for activity even when unknown;
- preserve one candidate head per source when evidence is incomparable;
- duplicate evidence is idempotent;
- the same source position with different content is a typed conflict;
- control evidence contains no phase or tool;
- process evidence carries machine, boot, PID, and process-start identity;
- a complete process snapshot names its scope and completeness explicitly; and
- raw locators point to bounded operational evidence rather than copying large
  logs into core state tables.

### Fact-family requirements

Run/process evidence:

```text
run_id
machine_id
machine_boot_id
pid
process_start_identity
started_at?
ended_at?
exit_status?
end_reason?
observation completeness/source
```

Activity evidence:

```text
run_id
kind: thinking | executing | quiescent | blocked | stalled | unknown
raw_kind
tool?
detail?
observed_at?
valid_until
source
```

Control evidence:

```text
run_id
connection_id
ownership
transport
lease_generation
observed_at
valid_until
revoked_at?
supported_operations
granted_operations
degradation_reason?
```

Interaction evidence:

```text
interaction_id
run_id?
kind: question | permission | approval
provider_request_id?
opened_at
resolved_at?
can_respond
```

Transcript evidence:

```text
thread_id
source_revision?
durable_revision?
render_revision?
last_append_at?
searchable
live_observation
```

## Bounded Fact Storage

The Runtime Host stores current candidate heads and durable terminal/action
facts in SQLite. It does not require an unbounded event log in core.

Target logical tables or equivalent catalogd facts:

- `activity_heads(run_id, source, source_epoch, source_seq, ...)`;
- `control_leases(connection_id, lease_generation, ...)`;
- `process_heads(machine_id, boot_id, pid, process_start_identity, ...)`;
- `pending_interactions(...)`;
- `transcript_heads(thread_id, ...)`;
- durable `session_runs` terminal fields;
- durable session closure fields;
- launch attempts;
- action/input receipts; and
- evidence conflicts/diagnostic locators.

The authoritative reducer chooses an effective head per fact family using a
documented source order. It never stores presentation labels as evidence.

### Authority and clocks

| Fact family | Positive authority | Expiry behavior | Forbidden inference |
| --- | --- | --- | --- |
| Session closure | explicit user/API closure | never expires | process or transcript age cannot close session |
| Run start/end | launch adoption, explicit process/provider terminal, complete matching process snapshot | terminal never expires | lease/heartbeat omission cannot end run |
| Activity | provider hook/bridge/native phase evidence | becomes unknown | control/transcript cannot create activity |
| Control | current provider control adapter lease/grants | becomes unknown | activity/process alone cannot grant action |
| Interaction | explicit structured request/resolution | remains pending until resolved | generic `needs_user` cannot create interaction |
| Transcript | source/durable/render revision heads | becomes unknown when heads unavailable | transcript progress cannot create activity |
| Host/process | machine heartbeat and identity-keyed process snapshots | stale/unknown | PID or command shape alone cannot prove identity |

## Canonical Projection

The authoritative reducer serves the existing target shape, extended only when
required to preserve source truth:

```text
SessionStateFacts {
  state_contract_version
  presentation_policy_version
  mode
  disposition
  launch?
  run?
  activity
  control {
    ownership
    connection
    connection_id?
    lease_generation?
    control_plane?
    observed_at?
    valid_until?
    actions
  }
  pending_interaction?
  transcript
  host
  presentation {
    primary?
    access?
    transcript?
  }
  commit_seq
}
```

`raw_kind`, source, timestamps, and validity remain available on activity.
English presentation remains server-owned alongside stable keys. Clients may
demote an already-rendered expiring fact at `valid_until`; they may not extend,
promote, authorize, or write the demotion back.

## Local Client Attachment

Provider TUI attachment is not control attachment, Desktop connectivity, or
provider activity. The current `ui_presence` field conflates these concepts and
must not survive as a canonical session state.

The initial target does not require a new client-presence fact. Desktop first
cuts over to the canonical local session projection. Provider-local attachment
may remain adapter diagnostics and may inform whether `reattach` is available.

If a concrete product requirement remains after cutover, add a separate record:

```text
ClientPresence {
  client_id
  surface: desktop | web | ios | cli
  session_id?
  observed_at
  valid_until
  transport
}
```

Client presence may explain why a projection is unavailable. It cannot grant
provider control, prove provider activity, or end a run.

## Presentation Contract

### Session list

Longhouse.app shows one stable `Sessions` list for current Helm and Console
sessions. A row changes facts and styling in place. It moves only when the user
changes an explicit sort or when a session enters/leaves the list by lifecycle
policy.

Primary presentation follows the accepted ordered policy:

1. Closed
2. Starting
3. Needs answer
4. Needs approval
5. Thinking
6. Using `<tool>`
7. Stalled
8. Blocked
9. Idle
10. Ended
11. Activity unknown
12. no primary label when there is no current run

The working indicator may pulse. Quiet and attention states do not reorder the
row. Access appears independently as Live control, Reattach, Observe only,
Search only, or a typed actionable fault.

### Shadow and unmanaged processes

Shadow sessions remain sessions in the product model and use the same activity
facts, but control is unowned. Raw unjoined process inventory is not promoted
to a session. Longhouse.app may show it in a collapsed `Observed agents`
diagnostic section, clearly separate from Helm/Console session count.

### System incidents

Machine offline, durable upload blocked, transport disconnected, update
required, and archive repair remain system-level facts. They do not move or
rewrite individual session activity. A system banner may explain that row facts
are stale.

### Orphans and residues

A provider runtime joined to a known session remains that session row. Examples:

- execution observed + control disconnected → session row with Control lost;
- control observed + local terminal detached → session row with Reattach when
  granted;
- run explicitly ended + leftover bridge → ended session plus diagnostic
  residue requiring cleanup.

Only infrastructure residue that cannot be safely joined to a session appears
in a separate diagnostics/cleanup section.

## Provider-Specific Requirements

### Codex

- Preserve app-server, bridge/relay, thread subscription, and TUI observations
  separately.
- App-server survival after TUI loss means run evidence may remain positive;
  it does not imply control is connected.
- Bridge death does not authorize killing an app server.
- Clean TUI/user exit may terminate the owned run through an explicit terminal
  path.
- Reattach and terminate remain independently granted operations.
- `launch_mode=tui|detached_ui` remains adapter provenance, not activity.

### Claude

- Process and native-channel evidence remain separate.
- No Codex-style detached bridge state is invented.
- Process identity includes start identity; command-shape fallback lowers
  certainty rather than proving liveness.
- Active-turn steer requires fresh active activity and the native steer grant.

### OpenCode

- Server process, authenticated health, and attached TUI are separate evidence.
- A live server without authenticated health may prove a process but not live
  control.
- Active-turn steer and pause answer remain explicitly unsupported until proven.

### Cursor

- Helm launcher, child process, PTY, socket, and readiness are adapter-owned
  evidence.
- Lack of remote launch or active-turn steer is explicit capability asymmetry,
  not degradation.

### Antigravity

- Do not model hook inbox as a generic attached bridge lease.
- Separate static support, hook installation, recent hook observation, message
  enqueue, claim receipt, and response/continuation evidence.
- Advertise `send_input` as currently available only after the required live
  proof for the installed provider version and current adapter is satisfied.
- Reattach, interrupt, terminate, Console, and active-turn steer remain
  unavailable until a stable provider surface proves them.
- Binary presence alone never grants send.

## Implementation Phases

### Phase 0 — Lifecycle Safety Guardrails

Goal: stop false terminal and false-freshness claims before the larger reducer
cutover.

Work:

- stop managed lease omission from emitting `process_gone`;
- make lease expiry project control `unknown`, not disconnected/offline;
- make activity expiry project activity `unknown` in local-health and server;
- prevent phase or transcript progress from clearing terminal fields for the
  same run;
- require a new run identity for post-terminal work;
- preserve process-start and machine-boot identity through the Rust heartbeat
  and Python models; and
- add targeted regression tests before changing presentation.

Acceptance criteria:

- omitting a live managed session from one or more heartbeats never ends its
  run;
- an expired lease yields `control_freshness_unknown` and grants no action;
- expired `thinking`, `running`, or `idle` activity becomes unknown on every
  local/server projection;
- late same-run phase/progress evidence cannot reopen an ended run;
- a recycled PID cannot satisfy a prior process observation; and
- existing explicit clean-exit and explicit-stop paths still end the correct
  run.

Required checks:

- focused heartbeat reconciliation tests;
- runtime reducer terminal-monotonicity tests;
- local-health activity-boundary tests;
- Rust resolved-process serialization tests; and
- existing managed-provider lifecycle suites.

Rollout/backout:

- ship guardrails behind no presentation change;
- log shadow differences between old terminal inference and new unknown state;
- backout is code rollback only because no new durable schema is required;
- do not restore synthetic terminal creation to hide stale rows.

### Phase 1 — Machine-Readable Contract and Conformance Harness

Goal: make the semantic target executable before introducing new storage.

Work:

- encode canonical enums, reason codes, fact-family schemas, and ordered
  presentation policy in one machine-readable source;
- generate or validate Python, Rust, TypeScript, and Swift representations;
- define provider adapter declarations separately from current readiness;
- build cross-provider scenario fixtures from raw observations to canonical
  expected facts;
- add axis-noninterference and expiry-boundary property tests; and
- mark legacy aliases with a fixed deletion gate.

Acceptance criteria:

- one schema/version defines every canonical enum and presentation key;
- adding an unknown raw provider activity preserves it and projects unknown;
- every supported provider runs the same semantic scenarios;
- unsupported operations produce `unavailable/unsupported`, not missing or
  provider-specific labels;
- clients can decode a future unknown key without coercing it to idle; and
- legacy contract tests no longer define new semantics.

Required checks:

- schema generation drift test;
- reducer algebra/property tests;
- cross-language fixture decoding; and
- pairwise presentation decision-table coverage.

### Phase 2 — Typed Machine Evidence

Goal: separate provider observation from activity, control, and lifecycle on the
Machine Agent boundary.

Work:

- add versioned observation envelopes to the Rust heartbeat/machine contract;
- make Codex, Claude, OpenCode, Cursor, and Antigravity adapters emit fact-family
  evidence separately;
- remove phase/tool from the new control lease shape;
- preserve raw activity kind/source/timestamps/validity;
- preserve machine boot, PID, and process-start identity;
- represent complete process-snapshot scope explicitly;
- add Antigravity hook claim/response readiness evidence; and
- retain legacy lease/session rows as read-only compatibility output during
  shadow comparison.

Acceptance criteria:

- changing a control observation cannot change emitted activity evidence;
- changing activity cannot change control grants or run lifecycle;
- every positive run terminal carries run and process/provider authority;
- Antigravity binary present with no hook proof grants no send action;
- provider diagnostics retain enough raw evidence to explain adapter decisions;
  and
- the Machine Agent can cold-restart and restore durable facts while honestly
  returning unknown for expired ephemeral evidence.

Required checks:

- `make test-engine` plus provider-specific scanner/bridge tests;
- heartbeat schema compatibility tests;
- provider observation fixtures for all five providers; and
- secret/argv checks for bridge and hook credentials.

### Phase 2.5 — Reducer-Grade Evidence Identity

Goal: give the reducer stable ordering and subject identity without inventing
authority from server receipt time.

Work:

- version the typed evidence contract with a shared reducer identity containing
  a canonical subject, source, optional source epoch/position, dedupe key, and
  canonical evidence hash;
- require run identity for run-scoped activity and lifecycle evidence;
- require connection identity and generation for control evidence;
- keep process-start identity opaque and include it in process subjects rather
  than coercing it into legacy timestamp columns;
- declare each adapter source sequenced or unsequenced; unsequenced sources may
  be freshness-ranked but cannot claim same-position conflict detection;
- reject evidence that lacks the identity required for its fact family from the
  reducer while retaining it in the compatibility heartbeat for diagnostics;
- bound reducer intake independently of the broader heartbeat validation bound;
  and
- add five-provider identity/conformance fixtures.

Acceptance criteria:

- no reducer subject or ordering position is derived from server receipt time;
- evidence for run N cannot address run N+1;
- a recycled PID cannot address a prior process subject;
- repeated evidence has a stable dedupe key and canonical hash across restart;
- reuse of one sequenced source position with different content is detectable;
- an unsequenced source is explicitly identified and never receives sequenced
  conflict guarantees; and
- insufficiently identified evidence is rejected with an explainable reason,
  not silently reduced under a session-wide key.

Required checks:

- Rust/Python contract compatibility and canonical-hash fixtures;
- adapter conformance fixtures for Codex, Claude, OpenCode, Cursor, and
  Antigravity;
- cross-run, PID-reuse, connection-generation, duplicate, and hash-mismatch
  fixtures; and
- secret and bounded-payload checks.

### Phase 3 — Authoritative Bounded Reducer in Shadow Mode

Goal: establish one catalog-owned SQLite authority within the shadow store
without changing served responses or command authorization.

Work:

- add one candidate head per
  `(fact_family, subject_key, source, source_epoch)` and reducer transactions;
- key activity by run/source and leases by connection/generation;
- preserve durable session/run terminal, interaction, and receipt facts;
- define deterministic candidate authority and conflict handling;
- make catalogd/runtime reads able to project `SessionStateFacts` from the new
  heads at one `commit_seq`;
- reduce current typed observations as a sub-operation of the existing
  catalogd heartbeat transaction rather than through a second queue or replay
  pipeline;
- compare old and new facts by axis rather than comparing only labels; and
- record explainable, bounded parity deltas without dual-writing a second
  authority;
- suppress all head/receipt writes when evidence is semantically unchanged;
- retain only a bounded recent source-position window for duplicate/conflict
  diagnosis; and
- add reducer and parity kill switches that cannot affect legacy serving or
  command authorization.

Acceptance criteria:

- replay produces identical current heads;
- duplicate evidence is idempotent;
- same current or retained source position/different content creates a typed
  conflict, while older positions outside the bounded window are stale;
- Phase 3B does not mutate session closure or run termination; monotonic
  terminal reduction remains a Phase 4 cutover gate once run facts exist;
- evidence for run N cannot mutate run N+1;
- control, activity, transcript, and interaction heads advance independently;
- Phase 3B parity identifies candidate-level control deltas by exact
  source/fact coordinates and hashes; canonical cross-family parity waits for
  the pure `SessionStateFacts` projector; and
- shadow operation adds bounded storage, not unbounded growth.

SQLite health contract:

- one catalogd RPC mutation and one SQLite transaction per heartbeat batch;
- no second shadow writer, asynchronous replay queue, or stored projection;
- no reducer-added `commit_seq` advance for a duplicate or unchanged reducer
  batch (a new durable heartbeat still advances the enclosing transaction once);
- freshness and expiry are derived at read time and never create timer-driven
  writes;
- reducer input is capped at 256 total facts per heartbeat after Machine Agent
  coalescing, with each value payload capped at 4 KiB and locator at 1 KiB;
- storage is capped per fact family at 2,048 current source candidates, with at
  most 16 receipts and 8 conflicts per candidate; head eviction removes its
  child history in the same catalog transaction;
- the API and reducer use one pure identity/hash validator, so retained
  reducer-grade evidence cannot be weaker than reducer intake;
- WAL bytes, checkpoint busy/remaining frames, writer queue wait, transaction
  execution time, changed heads, duplicates, stale evidence, conflicts, and
  cleanup duration are observable;
- steady-state WAL remains below 64 MiB and below 128 MiB during a representative
  soak, with no monotonically growing remaining-frame count for five minutes;
- representative-load p95 catalog writer wait remains below 100 ms, p99 below
  500 ms, and p99 transaction execution below 50 ms; and
- truncate checkpoints run only while idle after a successful passive
  checkpoint, never on the hot writer path.

Required checks:

- reducer replay/property suites;
- reordered/duplicate/conflict fixtures;
- SQLite restart/cold-rebuild tests for durable versus ephemeral facts;
- performance checks for heartbeat and high-frequency writes through
  `WriteSerializer`; and
- a long-lived-reader WAL/checkpoint test plus catalog backup/recovery rehearsal;
- dogfood parity capture on representative Codex, Claude, OpenCode, Cursor, and
  Antigravity sessions.

Cutover gate:

- no unexplained lifecycle, action-grant, or pending-interaction deltas;
- known activity differences are classified as intentional expiry/unknown
  corrections; and
- provider-specific gaps have explicit unsupported/degraded reasons.

Rollout/backout:

- Phase 3A installs and tests the bounded reducer with no live heartbeat intake;
- Phase 3B may add same-transaction shadow intake only after explicit provider
  run/connection identities, cross-language hash vectors, interrupted-migration
  tests, and the WAL pressure policy pass;
- `shadow_reducer_ingest_enabled` stops head mutation while legacy heartbeat
  behavior remains unchanged;
- `shadow_parity_enabled` independently stops parity comparison/persistence;
- one provider/source may be disabled without changing its compatibility
  heartbeat path; and
- served reads and command authorization have no Phase 3 switch and remain on
  the legacy path until the Phase 4 cutover gate passes.

Phase 3B diagnostic limits:

- parity compares only control axes backed by the just-written normalized
  legacy control lease; activity and other families remain explicitly
  unsupported until the canonical projector exists;
- an omitted legacy lease snapshot is `legacy_unavailable`, never evidence of
  absence;
- readiness remains a shadow-only pre-cutover extension; and
- stale/conflict guarantees apply to retained candidates and their bounded
  receipt window. Evicted candidates do not retain an unbounded high-watermark.

### Phase 4 — Canonical Server and Command Cutover

Goal: serve and authorize from the authoritative facts.

Work:

- make timeline, detail, SSE/workspace, `/api/agents/*`, and catalog surfaces
  consume the same `SessionStateFacts` commit;
- derive presentation and compatibility aliases from canonical facts only;
- stop `SessionStateFacts` from consuming rolled-up `control_label`;
- populate independent transcript source/durable/render revisions;
- revalidate every command against current run, lease generation, support, and
  grants;
- make static provider support distinct from current machine readiness;
- gate Antigravity send on current proof; and
- keep old fields as read-only aliases for one fixed compatibility window.

Acceptance criteria:

- all server surfaces return identical facts and presentation policy version at
  the same commit sequence;
- a stale cached capability cannot authorize any write;
- no projection consumes another label or presentation bucket;
- transcript lag never changes activity;
- compatibility aliases are generated from canonical facts and cannot write
  back; and
- current provider operation readiness is explainable per operation.

Required checks:

- API/timeline/detail/SSE parity fixtures;
- command race tests around lease expiry, run end, and grant revocation;
- transcript convergence revision tests;
- machine capability/support tests; and
- hosted/local Runtime Host restart tests.

### Phase 5 — Desktop and Local-Health Cutover

Goal: make Longhouse.app a quiet view of canonical local truth and deliver the
stable session-list design.

Work:

- emit a canonical local session projection from the Machine Agent/local-health
  boundary, scoped as a non-authoritative `machine_preview` when the Runtime
  Host is unavailable;
- make Swift decode `SessionStateFacts` and server-owned presentation;
- prefer the Runtime Host canonical projection whenever reachable and never
  merge it field-by-field with `machine_preview`;
- remove the merge of local managed phase rows with legacy SSE runtime phases;
- remove residual `ready` bucketing and `Ready and background`;
- replace provider-switch stop exposure with canonical terminate action state
  and an adapter registry;
- show one stable Sessions section with in-place status indicators;
- localize control faults to their session rows;
- keep system shipping/transport/update facts separate;
- demote raw unmanaged process inventory to a collapsed diagnostic surface; and
- keep only unjoinable process residue in cleanup diagnostics.

Acceptance criteria:

- a session changing between Thinking, Using tool, Idle, Needs approval,
  Stalled, or Activity unknown does not move rows;
- Desktop never displays Ready;
- `Activity unknown · Live control` and `Idle · Live control` remain distinct;
- control loss on one old session does not headline all current sessions as
  unavailable;
- a stop/terminate affordance appears only when its action is available;
- Console is never labeled background;
- Codex TUI loss, control loss, and run end render as different facts; and
- Desktop matches API presentation for the same fixture and policy version;
- offline `machine_preview` exposes only its documented local subset and cannot
  grant actions or claim server-owned durable facts.

Required checks:

- menu-bar fixture/snapshot matrix;
- harness accessibility labels;
- local-health expiry and canonical decode tests;
- provider action availability fixtures; and
- dogfood screenshots for healthy, mixed, offline, upload-blocked, and orphan
  scenarios.

### Phase 6 — Web and iOS Cutover

Goal: remove independent client state machines and legacy action gates.

Web work:

- preserve `activity_unknown` rather than mapping quiet to Idle;
- consume server-owned presentation keys/labels;
- stop rewriting access semantics;
- use per-action state for controls; and
- remove legacy runtime booleans as semantic authority.

iOS work:

- use canonical `send_input`/`start_turn` action state only;
- remove the `composerEnabled || canonical` authorization path;
- preserve raw activity kind/source/timestamps;
- remove `syncing_transcript` from activity types;
- consume server-owned access presentation; and
- retain fail-closed decoding for unknown action state.

Acceptance criteria:

- API, web, iOS, and Desktop show the same primary/access keys at one commit and
  policy version;
- unknown activity is never announced as Idle;
- no client can enable an action through a legacy boolean;
- unknown future states remain unknown rather than becoming quiet/available;
- unsupported provider operations remain explicit; and
- existing Console start-turn behavior remains independent of live Helm input.

Required checks:

- `make test-frontend`;
- `make test-ios`;
- generated model drift tests;
- cross-client golden fixtures; and
- focused UI tests for action enablement and accessibility labels.

### Phase 7 — Live Proof and Cutover Soak

Goal: prove the canonical path under real interruptions before deleting the
compatibility path.

Work:

- add repeatable fault scenarios for network loss, Runtime Host restart,
  Machine Agent restart, terminal close, bridge death, app-server survival,
  process exit, stale PID/state file, and transcript lag;
- exercise every provider's supported operations against live proof;
- record fact-source, validity, commit sequence, and action-denial reason in
  diagnostics;
- add deep-health checks for cross-surface key/version parity;
- verify cold restart produces durable terminal/interaction truth and honest
  unknown ephemeral state;
- dogfood the stable menu with mixed current and recoverable sessions;
- keep compatibility aliases read-only while the canonical path soaks; and
- classify every parity delta by fact family, source, and intended resolution.

Acceptance criteria:

- the original five-current-plus-three-residue scenario is unambiguous;
- intermittent network loss cannot create a retry storm of terminal/reopen
  transitions;
- Runtime Host or Machine Agent restart cannot grant actions from stale state;
- every live provider action is either proven successful or explicitly
  unsupported/unavailable with a reason;
- deep-health detects contract-version or presentation-key divergence; and
- no unexplained cross-surface fact or action mismatch remains.

Deletion gate:

- every client and command path uses canonical facts/actions;
- live fault scenarios pass on the exact build proposed for deletion;
- the compatibility window is complete; and
- rollback no longer depends on unsafe lifecycle or authorization behavior.

### Phase 8 — Delete Competing Authorities and Close

Goal: make regression back to multiple state machines structurally difficult,
then repeat the critical live proof without compatibility state.

Delete:

- phase/tool fields on managed control leases;
- the duplicate `session_phase_state`/`managed_session_state` write path;
- synthetic missing-lease `process_gone` reconciliation;
- archive/live runtime winner logic as lifecycle authority;
- `runtime_display` truth/signal tiers and redundant booleans;
- combined `presentation_state`, `managed_attached`, `managed_detached`, and
  `managed_degraded` authority;
- legacy `Ready`, `No live signal`, `finished`, and `syncing_transcript`
  mappings;
- Desktop's local phase reducer and provider switch for stop exposure;
- iOS/web fixture synthesis from legacy runtime fields; and
- old API fields after the fixed compatibility window.

Work after deletion:

- repeat network loss, heartbeat omission, process exit, restart, stale PID,
  action-revalidation, and original mixed-session scenarios;
- verify fresh databases and upgraded databases produce the same canonical
  facts; and
- remove temporary parity telemetry after the post-deletion proof is clean.

Acceptance criteria:

- repository searches find no semantic consumers of deleted aliases;
- no compatibility decoder or schema remains without an explicitly supported
  old client;
- new provider integration requires an adapter declaration, evidence adapter,
  operation matrix, and conformance fixture—not edits across every client;
- all canonical tests pass with legacy tables/fields absent in a fresh DB;
- critical live fault scenarios still pass after deletion; and
- no unexplained cross-surface fact or action mismatch remains.

## Cross-Provider Conformance Matrix

Every semantic scenario runs against every provider. Provider mechanics remain
in adapter tests; normalized facts must obey the same assertions.

| Scenario | Canonical assertion |
| --- | --- |
| Provider binary installed, no adapter proof | static support may exist; current action unavailable/unknown |
| Healthy active run and control | run running; control connected; only granted actions available |
| Fresh thinking | activity thinking until `valid_until` |
| Fresh tool execution | activity executing with tool |
| Ordinary provider `needs_user` | quiescent/Idle unless structured interaction exists |
| Structured question/approval | pending interaction selects Needs answer/approval |
| Activity expires, control remains | Activity unknown · Live control |
| Lease expires, activity remains | activity unchanged; control unknown; no action grant |
| Client/TUI disappears, execution/control survive | run/control unchanged; reattach determined separately |
| Control dies, execution survives | run remains running/unknown; control disconnected/degraded |
| Explicit process exit | current run ends once and never reopens |
| Heartbeat omission | no terminal fact |
| Complete process snapshot omits exact identity | run may end with matching authority |
| Recycled PID | prior process not considered live |
| Transcript advances | transcript head changes; activity/control unchanged |
| Transcript lags | transcript lag label only |
| Session closes | Closed dominates all stale facts and actions unavailable |
| Unsupported action | unavailable/unsupported on all clients |
| Duplicate evidence | no head/version change beyond idempotent receipt |
| Same source position, conflicting content | typed evidence conflict |
| Old-run evidence arrives after new run | new run unchanged |

Minimum golden presentations per provider:

1. `Thinking · Live control`;
2. `Idle · Live control`;
3. `Activity unknown · Live control`;
4. `Activity unknown · Reattach`;
5. `Thinking · Observe only`;
6. `Ended · Search only`;
7. pending question;
8. pending approval;
9. transcript catching up independent of activity;
10. closed session dominance;
11. lease expiry without run end;
12. process disappearance versus heartbeat disappearance; and
13. unsupported versus currently unavailable action.

## Audit Finding Traceability

| Finding | Summary | Primary phase |
| --- | --- | --- |
| F-01 | `SessionStateFacts` is a projector, not fact authority | 3–4 |
| F-02 | control leases synthesize activity/default idle | 2 |
| F-03 | duplicate Rust phase ledgers | 2–3, delete 7 |
| F-04 | archive/live runtime winner state machine | 3–4, delete 7 |
| F-05 | run terminal is not monotonic | 0 |
| F-06 | missing managed lease creates `process_gone` | 0 |
| F-07 | lease expiry becomes offline/disconnected | 0 |
| F-08 | process start identity discarded on wire | 0–2 |
| F-09 | `finished` has incompatible meanings | 1–2 |
| F-10 | Ready remains in local/client presentation | 5 |
| F-11 | local-health activity does not expire | 0, 5 |
| F-12 | Antigravity launch asserts attached without observer | 2, 4 |
| F-13 | Antigravity support/readiness are conflated | 2, 4, 7 |
| F-14 | canonical projector consumes `control_label` | 4 |
| F-15 | transcript convergence copies one legacy revision | 4 |
| F-16 | hot index can resurrect ended/missing session | 3–4 |
| F-17 | Desktop bypasses canonical state | 5 |
| F-18 | Desktop infers stop availability/provider route | 5 |
| F-19 | web maps unknown activity to Idle | 6 |
| F-20 | web/iOS rewrite server access vocabulary | 6 |
| F-21 | iOS legacy composer can enable unavailable send | 6 |
| F-22 | iOS drops raw activity evidence | 6 |
| F-23 | iOS retains `syncing_transcript` activity | 6–8 |
| F-24 | compatibility projection remains user-visible authority | 4–8 |
| F-25 | local-client attachment is conflated/missing | 5; add only if still needed |
| F-26 | tests protect legacy reducers more than canonical facts | 1 onward |

## Phase Dependencies and Parallel Work

The safety guardrails in Phase 0 may proceed independently and should not wait
for the new reducer. Phase 1 establishes the executable contract required by
all later work.

After Phase 1:

- provider evidence adapter work can proceed in parallel by provider;
- reducer storage/replay scaffolding can proceed against synthetic envelopes;
- Desktop fixture design can proceed against canonical golden fixtures; and
- web/iOS can inventory legacy dependencies without changing authorization.

Serving cutover waits for typed evidence plus shadow reducer parity. Client
cutover waits for canonical server/local projection stability. Deletion waits
for every client and command path plus pre-deletion live proof. Final closure
waits for deletion plus repeated critical fault proof.

No phase declares success merely because its code merged. Its acceptance and
cutover gates must hold on the exact deployed build under test.

## Rollout Strategy

1. Land lifecycle guardrails with focused diagnostics.
2. Add schema/envelopes and new reducer without serving them.
3. Shadow compare independent axes on dogfood sessions.
4. Cut one server read surface at a time behind an explicit contract version,
   while keeping one canonical commit sequence.
5. Cut Desktop, web, and iOS using shared fixtures.
6. Keep old aliases server-derived and read-only for one fixed compatibility
   window.
7. Run real fault proof and deep-health parity on the canonical path.
8. Delete old authorities, then repeat critical proof before closing.

Rollback rules:

- rollback may switch serving back during pre-deletion phases;
- new evidence must remain replayable after rollback;
- rollback must not restore unsafe command authorization or non-monotonic
  terminal behavior;
- after deletion, recovery is a forward fix or schema-compatible deployment,
  not restoration of competing reducers; and
- no hidden provider fallback changes Helm/Console/Shadow mode.

## Observability and Diagnostics

Every canonical fact shown to a user must be explainable by:

- fact family and canonical value;
- provider/raw kind when applicable;
- source and raw locator;
- session/thread/run/connection identity;
- observed, received, and valid-until timestamps;
- reducer commit sequence and presentation policy version;
- winning candidate and competing candidate count;
- conflict/rejection reason; and
- action-unavailable reason.

Diagnostics expose raw evidence to agents and repair tools without requiring
human users to interpret it in the primary menu. Logs must avoid credentials,
tokens, provider secrets, and large transcript duplication.

Required counters/signals:

- activity/control expiry to unknown;
- rejected post-terminal evidence;
- run/process identity mismatch;
- heartbeat omission without terminal;
- evidence conflicts/deduplication;
- old/new shadow fact deltas by family/provider;
- command revalidation denials by reason;
- client/API contract or policy-version mismatch;
- unsupported versus unproven provider operation; and
- orphan/residue join success and failures.

## Success Measures

The epic is successful when:

- all user-facing surfaces show the same stable primary/access keys for the same
  commit and policy version;
- no action is enabled by a legacy hint or presentation label;
- heartbeat/network loss produces unknown freshness rather than false terminal
  or idle state;
- ended runs never reopen;
- new provider integrations require one adapter and conformance suite rather
  than client-specific status work;
- local-health and Desktop no longer maintain independent activity truth;
- `Ready and background` and generic background session state are gone;
- raw evidence remains available for agent-driven diagnosis; and
- retry storms and orphan incidents are explainable by exact facts and safe
  actions.

## Risks and Mitigations

### Migration creates a seventh reducer

Mitigation: the new reducer runs shadow-only, receives the target evidence
shape, and becomes the sole served authority before old writers are deleted.
Do not add another client mapper or dual-write two authoritative stores.

### Unknown appears more often

Mitigation: this is expected honesty, not a regression. Pair Activity unknown
with independent access and timestamps. Improve provider evidence later rather
than inventing Idle.

### Provider capability regression during cutover

Mitigation: operation-by-operation fixtures and command revalidation. Static
support and current proof remain separate, so unsupported/unproven operations
fail explicitly.

### High-frequency reducer contention

Mitigation: bounded heads, idempotent writes, `WriteSerializer`, provider/source
coalescing, and targeted load measurement. Do not retain an unbounded event log
in the hot SQLite core.

### Desktop loses offline usefulness

Mitigation: the Machine Agent emits a scoped, non-authoritative local preview
with the same vocabulary and policy version. Desktop prefers Runtime Host facts
when reachable, never merges the two projections, and cannot authorize actions
from the preview.

### Compatibility aliases become permanent

Mitigation: every alias has an explicit consumer inventory and deletion gate.
No new code may consume it once the canonical equivalent exists.

## Definition of Done

This epic is complete only when all of the following are true:

- Phase 0 safety invariants hold in automated tests and live fault scenarios;
- one authoritative reducer owns bounded current facts;
- every provider emits typed evidence and passes conformance for its supported
  topology;
- every server and client surface consumes the canonical versioned projection;
- every command revalidates current action authority;
- Desktop presents one stable session list and no generic background state;
- legacy reducers, aliases, and client authorization fallbacks are deleted;
- cross-surface deep-health parity is green;
- cold restart and replay preserve durable truth and honestly lose only
  ephemeral freshness; and
- the original degraded-network/orphan-runtime incident renders as localized,
  comprehensible session and system facts with safe recovery actions.

## Suggested Verification Commands

Run the tier that matches each phase rather than running the full matrix after
every small change.

```bash
make test-engine
make test
make test-frontend
make test-ios
make test-e2e
make dogfood-refresh
make dogfood-check
```

Use the macOS menu-bar harness fixtures during Phase 5 and exact-SHA hosted QA
during server/client cutovers. Full CI and live provider proof are phase exit
signals, not substitutes for the focused invariant tests.

## Decision Log

### Decision: complete the existing contract rather than invent another model

`runtime-display-contract.md` already contains the correct semantic target.
This epic specifies implementation, migration, and deletion around it.

### Decision: shared semantics, provider-specific mechanics

A common bridge/process abstraction would lie about Claude and Antigravity and
would underspecify Codex/OpenCode. Providers instead emit typed evidence into a
shared reducer.

### Decision: fix lifecycle before presentation

False terminal/reopen behavior and expired control semantics are correctness and
authorization risks. They land before the larger storage/client migration.

### Decision: no generic background session state

Console, missing terminal, detached control, and orphan residue are different
facts. They must never share one user-facing status.

### Decision: client presence is deferred

Provider TUI attachment remains diagnostics and reattach evidence. A separate
client-presence fact is added only if a product requirement remains after
Desktop consumes canonical session facts.

### Decision: compatibility is fixed and read-only

Old fields may be derived from canonical facts for a bounded migration window.
They cannot remain writable, feed reducers, authorize commands, or acquire new
consumers.
