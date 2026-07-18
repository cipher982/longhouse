# Durable Shipping Resilience

**Status:** Decision draft
**Owner:** Longhouse core
**Created:** 2026-07-17
**Related:** `speed-of-light-shipper.md`, `transcript-convergence.md`,
`hosted-archive-restart-control.md`, `storage-failure-isolation.md`

## Decision

The Machine Agent will optimize **useful durable goodput**, not request rate or
host utilization. Live transcript work keeps a latency reservation. Catch-up
work expands toward the safe Runtime Host limit, but a semantic conflict can
occupy at most one attempt and one source-local recovery task. It can never
remain in the live lane and retry indefinitely.

The Runtime Host is the authority for committed source ranges, source-epoch
replacement, and render generations. Conflict responses must carry enough
authority for the Machine Agent to reconcile locally without guessing or
probing a second endpoint for an epoch that may never have been committed.

## Incident Evidence

On 2026-07-17, after network interruption and process cleanup, cinder retained
roughly 600 durable envelopes. Of 211 pending Cursor envelopes, 207 contained
render-generation IDs minted before the Cursor generation-stability fix. The
Runtime Host correctly rejected them with `409`, but the client then requested a
manifest for the never-created proposed epoch, received `404`, classified the
result as retryable, and immediately returned it to the live lane.

The result was a retry storm:

- all eight live worker slots repeatedly executed poisoned Cursor work;
- 979 retryable live errors accumulated in ten minutes;
- successful goodput fell to eight requests in the same window;
- the host remained about 94% CPU idle and local disk traffic stayed below
  1 MB/s because semantic rejection, not hardware, was the bottleneck;
- two previously quarantined sources kept the menu bar red after the storm.

The containment release at `ab9c4ecf` made render-generation conflicts
actionable, adopted the Runtime Host generation without changing raw envelope
identity, and quarantined unresolved `409 -> manifest 404` conflicts. The live
queue then fell from hundreds of Cursor envelopes to zero blocked Cursor
envelopes without dropping source bytes.

The recovery also exposed two follow-on gaps:

1. adopted render authority is currently persisted only in the pending
   envelope, so later appends from the same Cursor store can repeat the
   generation reconciliation;
2. a source epoch may close and be replaced while its old pending envelope is
   in flight. The replacement can already cover that range, but the old epoch
   currently receives only a generic overlap conflict and must be inspected
   manually;
3. Cursor Console canaries reused one provider conversation identity from
   multiple temporary/worktree stores. Those paths built divergent local epoch
   chains for one opaque source and were correctly quarantined, but test traffic
   still made product health red.

## Product Invariants

1. A bad source cannot degrade control, presence, or unrelated transcript
   shipping.
2. Live appends target p95 host acknowledgement under ten seconds.
3. Catch-up consumes all safe leftover capacity, but live latency has priority
   over backlog throughput.
4. Retryable means a later attempt can plausibly succeed without changing the
   request. Semantic conflicts require reconciliation, not delay alone.
5. Raw provider evidence and durable intent survive every retry, restart, and
   quarantine transition.
6. Health describes the current ability to ship. Historical incidents remain
   visible but do not claim that the machine is presently broken.
7. Every destructive operator action is exact-source, evidence-backed, and
   auditable.

## Failure Model

Every failed send is classified once at the protocol boundary:

| Class | Examples | Engine action |
| --- | --- | --- |
| transient transport | connect reset, DNS, timeout before receipt | exponential backoff with jitter |
| explicit pressure | `429`, typed `503`, writer/admission busy | honor `Retry-After` as a floor; reduce the affected lane |
| ambiguous commit | response lost after a possible commit | query by stable envelope ID; never mint a replacement first |
| reconcilable conflict | render generation drift, accepted prefix, epoch replacement | run one typed local reconciliation |
| permanent invalid | malformed range, unsupported revision, corrupt evidence | quarantine exact source |

Unknown `4xx`, generic `409`, and `409` followed by `404` are not transient.
They leave the live lane immediately and retain their evidence in quarantine.

## Runtime Host Conflict Receipt

Every storage-v2 conflict returns a stable code plus authoritative facts. The
minimum receipt is:

```json
{
  "code": "source_epoch_replaced",
  "source_epoch": "old-epoch",
  "accepted_through": 17,
  "current_source_epoch": "new-epoch",
  "current_accepted_through": 38,
  "existing_generation_id": "host-generation",
  "parser_revision": "cursor-store-render-v2",
  "ordering_revision": "cursor-store-order-v1"
}
```

Only applicable fields are present. Required conflict codes are:

- `render_generation_revision_conflict`
- `source_prefix_already_committed`
- `source_epoch_replaced`
- `envelope_id_conflict`
- `invalid_source_range`

The receipt comes from the same transactionally consistent catalog decision as
the rejection. The client must not infer authority by combining a generic `409`
with a later manifest request.

## Machine Agent Recovery

### Render generation

When revisions match and the Runtime Host reports its existing generation, the
engine rewrites only `render.generation_id` in the durable request body. It then
persists that authority in the Cursor root/source state before retrying, so all
future appends use the accepted generation. Raw bytes, media, range, envelope
ID, and source epoch remain unchanged.

### Accepted prefix

When the Runtime Host proves a prefix is already durable, the engine verifies
object/envelope hashes where available, acknowledges the covered prefix
locally, and rebuilds only an uncovered suffix. It never resends an already
proven prefix merely because the original receipt was lost.

### Epoch replacement

When an old epoch points to a replacement:

- if the replacement's authoritative range fully covers the pending old range,
  acknowledge the local pending intent as a proven duplicate;
- if it covers only a prefix, acknowledge that prefix and rebuild the suffix
  under the replacement epoch;
- if identity, predecessor, or range continuity does not match, quarantine the
  source with both manifests attached to the evidence record.

This recovery is serialized per opaque source ID so two observations cannot
race epoch replacement and pending-envelope creation. Multiple local paths that
claim one opaque source join the same serialization domain; they cannot mint
independent epoch chains. Canaries must mint unique provider source identities
per run unless the test explicitly exercises continuation of the same source.

## Scheduling and Retry Isolation

The scheduler owns three independent budgets:

- live transcript: reserved capacity and strict latency SLO;
- current-session repair: bounded, recent-gap priority;
- historical catch-up: adaptive leftover capacity.

Rules:

- one source has at most one in-flight send or reconciliation;
- a semantic failure consumes no further live retry budget;
- transient retries use exponential backoff with full jitter and a bounded
  attempt budget before demotion to repair;
- server `Retry-After` is a minimum delay, never capped downward;
- a failed source cannot occupy more than one repair slot;
- scheduling is fair across sources, not merely FIFO across envelopes;
- the controller increases catch-up concurrency while live latency, host queue
  wait, and host execution time are healthy, then decreases multiplicatively on
  pressure;
- drain mode seeks the safe service limit, not 100% laptop CPU, disk, or network
  utilization. Low utilization is correct when the Runtime Host or useful work
  is the limiting resource.

The primary controller signal is acknowledged bytes/events per second. Attempt
rate and busy worker count are diagnostics, not success metrics.

## Durable Local State

Pending intent records need explicit delivery state rather than nullable error
fields:

```text
ready -> in_flight -> acknowledged
                  -> retry_scheduled
                  -> reconciling -> ready | acknowledged | quarantined
                  -> quarantined
```

Each record carries:

- failure class and stable error code;
- attempt count and next eligible time;
- last authoritative receipt;
- reconciliation count and result;
- source-local lease owner/expiry;
- quarantine reason and retained evidence pointer.

State transitions are transactional. Restart recovery expires an abandoned
lease; it does not reset retry history or promote a quarantine back to live.

## Health and Operator Surface

Status separates current state from recent history.

Current shipping is `healthy`, `catching_up`, `pressured`, or `blocked` based on
current queues, consecutive outcomes, and active source failures. A rejected
request that was reconciled and followed by successful acknowledgements remains
in incident history but does not keep the machine red for an hour.

Expose:

- acknowledged goodput versus attempt rate and wasted-work ratio;
- live observation-to-ack p50/p95;
- ready, retry-scheduled, reconciling, and quarantined source counts;
- oldest blocked source and stable error code;
- effective retry delay and server-provided floor;
- per-lane ready/in-flight counts, byte counts, and concurrency caps;
- the current limiting resource: live guard, host queue, host execution, local
  CPU/parser, upload, explicit pause, or no eligible work.

Add exact-source operator commands:

```text
longhouse shipping inspect --source-epoch <id>
longhouse shipping reconcile --source-epoch <id>
longhouse shipping retry --source-epoch <id>
longhouse shipping discard --source-epoch <id> --proof <receipt-id>
```

`reconcile` performs the manifest/generation logic and queues the exact source;
it must not require a daemon restart or a broad filesystem scan. `discard`
requires proof that the range is already durable or an explicit user-approved
data-loss acknowledgement.

## Adjacent Managed-Process Recovery

This incident also left Codex app-server children alive after their bridge
parents died. `codex-bridge stop` correctly failed closed because no IPC socket
could acknowledge termination, but there is no safe recovery command.

Add `codex-bridge reap --session-id <id> --force` with all of these checks:

- state file session ID and recorded app-server PID match;
- process start time and executable/cmdline match the recorded launch;
- the bridge parent is absent and the session is detached;
- no current attached session references that PID;
- terminal state and reap evidence are persisted before signaling;
- TERM, bounded wait, then KILL only for the same verified process identity.

No name-wide `pkill`, generic Node cleanup, or implicit cleanup during health
collection is allowed.

## Acceptance Tests

Fault-injection tests must cover:

- network loss before send, during upload, and after durable commit;
- a render generation changing between local preparation and host commit;
- an epoch being replaced while its old envelope is in flight;
- two local paths concurrently claiming one opaque source, plus repeated canary
  runs that must not reuse a production source identity;
- replacement fully covering, partially covering, and not covering the old
  range;
- repeated typed pressure with `Retry-After`;
- generic/unknown `409` and `409 -> 404`;
- one poisoned source alongside at least 100 healthy live sources;
- Machine Agent and Runtime Host restart during every delivery state;
- stale managed bridge with a live app-server child and PID-reuse rejection.

Pass conditions:

- healthy live sources remain under the ten-second p95 SLO;
- a poisoned source makes at most one live attempt;
- no source has two concurrent deliveries;
- ambiguous commit and replacement recovery preserve raw identity and produce
  no duplicate durable range;
- catch-up increases toward safe host capacity and backs off without starving
  live work;
- current health becomes healthy after successful recovery while retaining the
  incident in history;
- every quarantine has an exact operator recovery path.

## Delivery Order

1. Persist adopted render authority and add epoch-replacement conflict receipts.
2. Add the per-source delivery state machine and live retry budget.
3. Split current health from rolling incident history and expose goodput/waste.
4. Add exact-source inspect/reconcile/retry/discard commands.
5. Tune the catch-up controller against fault-injection and mixed live/archive
   load, then add state-verified managed-process reap.
