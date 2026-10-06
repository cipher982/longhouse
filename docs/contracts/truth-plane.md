# Longhouse Truth Plane Contracts

This is the launch-critical contract map for session truth. A contract here is
not a framework: it is a named product question, one backend projection that
answers it, and tests that prove the important transitions.

The goal is a user-facing invariant: when a session says live, offline,
read-only, running, closed, steerable, or queued, web and iOS are reading the
same backend-owned truth instead of reconstructing it differently.

## Contract Map

| Contract | Product Question | Canonical Surface | Current Proof | Next Proof |
| --- | --- | --- | --- | --- |
| Session identity and mode | Is this Shadow, Helm, or Console, and does Longhouse own control? | `session_state.mode` plus `session_state.control.ownership`, projected from durable launch/acquisition provenance | `server/tests_lite/test_session_state_contract.py` and kernel capability tests | Project 2 stores these facts directly in the bounded catalog. |
| Activity and presentation | What is the provider doing, and what scoped label should the user see? | `session_state.activity` plus versioned `session_state.presentation`; `runtime_display` is a deprecated facts-only alias | server property/contract tests plus web/iOS state-facts tests | Delete the alias after the compatibility window. |
| Timeline card status | Can I trust this session at a glance? | `session_state.presentation.primary` and independent access/transcript labels | state contract truth table plus client presentation tests | Add only orthogonal fact combinations, never combined statuses. |
| Action availability | Can this exact operation run now, and why not? | `session_state.control.actions`; legacy capability booleans are deprecated facts-only aliases | state contract and command-time exact-grant tests | Carry catalog lease generation through every command audit. |
| Session input lifecycle | What state is a submitted user input in after send, retry, crash, or cancel? | `SessionInput.status` plus typed intent/disposition/outcome and request identity | server input API/idempotency/boot-recovery tests plus `HTTPOutboxUITests/testRealHTTPOutboxRetriesSamePhotoOperationAndSurvivesRelaunch` and web/iOS row reconciliation tests | Add end-to-end queue replay proof if recovered queued rows ever gain a separate dispatcher. |
| Host and transport health | Is the host reachable, is the control transport alive, and are those different? | Remote `session_state.host` and `session_state.control`; local `engine-status.json.host_link` for Runtime Host update claims | `server/tests_lite/test_session_liveness_facts.py`, state-contract tests, and engine `host_link` / `device` tests | Add reason codes only from new raw evidence. |
| Console turn lifecycle | Did a turn start, stream, finish, fail, or get interrupted? | durable Console turn/run facts projected through `session_state` and the turn APIs | Console session/turn route tests plus web/iOS composer fixtures | Extend the shared fixtures when a new turn outcome becomes user-visible. |
| Provisional vs durable transcript | Is this text live preview, durable archive, stale preview, or superseded? | `SessionTranscriptPreview` and durable events | preview freshness tests plus shared web/iOS rendering fixtures | Keep stale/superseded render decisions backend-owned as bridge behavior changes. |
| Clock and freshness | When does a signal expire, and which clock owns that decision? | backend freshness windows near runtime/provisional projections | `server/tests_lite/test_session_freshness_contract.py` pins backend-clock boundaries for runtime sync and provisional previews | Add cases here when a launch-critical projection introduces a new freshness window. |
| Background work | What work continues beside the parent turn? | `session_state.delegation`: provider registry, category counts, named items and its own observation/expiry clock | `server/tests_lite/test_delegation_lifecycle.py` exercises hook ingress, replacement/empty snapshots, run fencing and exact child lineage | Provider-specific lifecycle captures establish which native updates carry the registry. |
| Error taxonomy | Which failures are product states versus logs/debug details? | typed response fields on input/turn/runtime projections | input/send/turn/preview reason codes are typed at projection boundaries | Expand only when web, iOS, or agents branch on a new code. |

## Session input lifecycle

Each send is a durable operation keyed by `client_request_id`. The client saves
the exact text, intent, model override, and attachment bytes before network
dispatch.

A lost or ambiguous acknowledgement is unknown, not a rejection: retain and
retry the same ID and payload so the server can reconcile without creating a
second turn. A definitive rejection retains the payload for editing into a new
operation or explicit discard. Longhouse acceptance does not mean the Console
turn has completed; show it as in progress until a terminal receipt or durable
transcript evidence arrives.

For a remote client without a local outbox entry, the server keeps a delivered
Console receipt discoverable while its turn is nonterminal, beyond the recent
delivered-receipt window. `starting`, `active`, and `draining` turn states are
shown as in progress even when the handoff still reports `queued`.

Terminal Console cancellations remain in the existing failed-receipt window,
so remote list-only clients can observe the `cancelled` outcome.

When no exact provider-authored origin is available, receipt-to-event linking
uses a conservative text/time fallback. Link only when a receipt has exactly
one eligible event and that event has exactly one eligible receipt. Any tie on
either side stays unlinked; never choose by chronology. The client's lightweight
sent row remains until exact identity evidence appears.

Discard releases the client's retained payload; it is not a server cancellation
of an operation that may already have been accepted.

## Background registry semantics

Background work is independent of the parent's activity. A parent turn can
finish while agents, commands, or monitors remain. Counts describe the latest
provider registry, not lifetime launches or a dependency blocking the parent.

An explicit empty registry clears the list. A missing registry leaves the last
observation unchanged; expiry makes its state unknown, not completed. Delivery
retries and unrelated activity never renew the registry's observation clock.
Named task IDs are scoped to the provider session and run. The transport bounds
registries to 256 entries and 256 KiB of canonical fact JSON (including observed
clocks). This collection budget is separate from the 4 KiB scalar fact budget.
An over-budget observation is omitted without discarding parent activity or
renewing previous registry evidence; no partial count is presented as complete.

OMP publishes semantic registry changes immediately and refreshes an unchanged
complete registry every five minutes, below its thirty-minute freshness lease.
Observation timestamps and live elapsed-duration ticks do not create new snapshots.
The first empty registry and a running-to-empty transition are still observations;
terminal task details and final durations are retained. Failed local retention does
not advance the emission gate, so the next provider frame can retry.

Task status and description come from the provider; raw command strings are not
included. `first_observed_at` is not a task start or activity time. Start and
last-activity timestamps, a navigable child session ID, and nullable
`tool_calls`, `assistant_messages`, and `user_messages` archive counters are
supplied only through exact, unambiguous child lineage. Missing counters are
unknown, not zero. Historical child end times do not decide whether a
background task is still active.

For Claude, a parent Stop registry's task ID selects only that parent's exact
`subagents/agent-<id>.meta.json` sidecar. Its provider-authored `toolUseId` joins
the existing child lineage; missing, malformed, or ambiguous evidence leaves
the task visible without a transcript link.

Subagent archive commits notify the parent's existing workspace stream, so child
timing and counters update without a parent turn. Ordinary forks do not trigger
this background-work wake. It does not alter the parent's transcript counters,
activity clock, or registry observation/expiry.

The delegation fact may also carry `recent_items`: tasks the provider reported terminal
(completed, failed, cancelled, aborted) with their native end time. They are history, not
active work: they are never counted in `count` or `kinds`, session detail lists them
separately as recently finished work, and expiry of the active registry leaves them in place
instead of erasing a recorded outcome. A newer complete registry is authoritative over older
lifecycle edges, so a completion callback is never a permanent client-side tombstone.

Web and native timeline work claims use the registry's own expiry, not the
parent's short-lived activity expiry. The named registry is inspectable in the session
evidence disclosure; expired observations show unknown, never an active count
of zero. The single timeline fire includes linked child tool/reply deltas.
Only subagents add agent flame roots; commands and monitors are separate
populations. New children and newly known counters establish their own baselines
without suppressing progress from continuing children. Historical work is not
replayed as a burst. Fire intensity is qualitative, not token throughput or billing.

Native session-detail material and its local expiry timer use the same
presentation-owned work claim as the timeline. A quiet parent can therefore
stay visually working on fresh delegation without inventing parent execution
time. The primary background headline is one navigable disclosure, not a
duplicate headline plus summary. Expiry becomes uncertain; explicit interaction
and closed-session precedence remain independent.

## Provider observations and machine liveness

OMP's twenty-second extension keepalive samples the provider's `ctx.isIdle()`.
Its phase signal is fresh provider evidence even when the sampled phase is unchanged.
The Machine Agent's five-second `status_assertion` says only that it can still report
the previously observed state: it renews machine liveness without changing the
provider observation, phase, or runtime revision. It cannot replace provider samples.

The Machine Agent's heartbeat is a liveness exchange, separate from the semantic
session snapshot. It keeps the periodic 60-second heartbeat; meaning changes
trigger at most one immediate heartbeat per second, while observation timestamps
do not. Gzip is used only after the host advertises it; a 400/415/413 refusal is
retried once as identity and disables gzip until the engine process restarts.
`engine-status.json.host_link` records the host's planned-update claim. The
engine derives `updating` before `expected_back_by`, `slow_update` until the
lease `deadline` or attempt `cutoff`, and `unreachable` after expiry without a
renewal. The `host_updating` and `host_update_slow` local-health reasons are
informational and have no suggested action. `heartbeat_post_failed` appears
only when no acknowledged heartbeat falls within the host's freshness horizon
(120 seconds if unknown) and no valid claim remains.

The macOS menu bar decodes `host_link` from local health, keeps an update claim
out of the headline until it reaches two seconds, then uses the copy in
`schemas/host_link.yml`. A slow update stays amber and adds elapsed time since
`claim_started_at`; while a claim is displayed, Status reporting says
`Paused · updating` and no trouble card is shown. Swift tests compare the
rendered copy with the schema to catch drift.


## Realtime canary

The producer commits a canary session through storage-v2 and emits numbered runtime
observations. The accepted runtime write carries that producer sequence and emission
time through the same workspace invalidation path used by session clients.

`GET /api/telemetry/canary-stream?session_id=...` requires both `X-Canary-Token`
and owner-bound `X-Agents-Token` authentication. It accepts only that owner's canary
sessions and emits timing/sequence coordinates, never transcript previews or tools.
Initial invalidations and unrelated workspace changes are not delivery observations.
The observer correlates producer sequences, not the independent pubsub cursor.
Ingest latency is the producer's monotonic committed-acknowledgement round trip;
it does not compare clocks on different hosts. Producer and observer share a host
clock for end-to-end SSE timing in the supervised bundle.

The authenticated `/api/telemetry/selfcheck` requires fresh ingest and SSE observations,
an absolute sequence gap below ten, and actual SSE latency samples with p95 at most 300 ms.
Render observations are optional. Its sample summary is bounded to fifteen minutes;
missing samples are unhealthy, not zero latency.

`bash scripts/canary/deploy_cube.sh` installs the source-owned producer/observer units
using the existing `~/.config/longhouse-canary/env` and preserves the stable session-id
and sequence files. The observer allows sixty seconds for initial bootstrap visibility
and uses a finite stream read timeout so SIGTERM is not trapped in an idle stream.
The canary stream is excluded from the normal finite HTTP request deadline;
the non-streaming selfcheck remains deadline-bound.
Both units supervise failures with a fifteen-second restart interval; an external
watchdog must alert on missing hops, failed/auth-refused checks, and SLA breaches.

## Non-goals

- No generic contract DSL, registry, or proof engine.
- No formal contracts for auth, billing, model selection, tools, connectors,
  distribution, search ranking, or LLM heuristics unless a launch-critical
  session truth flow directly needs them.
- Client compatibility for old payloads must be inert defaults, not alternate
  product truth reconstructed from raw facts.
- No duplicated contract maps in client code. Clients may keep compatibility
  fallbacks, but the backend projection is the product contract.

## Enforcement Standard

Each contract earns its place by having:

1. One backend projection or model field group that owns the answer.
2. Stable reason codes only when clients or agents branch on them.
3. A truth-table or transition test for the states users can observe.
4. At least one client fixture when web/iOS rendering could diverge.
5. Explicit non-goals so the contract does not become a general framework.
