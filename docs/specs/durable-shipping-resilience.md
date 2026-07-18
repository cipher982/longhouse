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

The recovery also exposed three follow-on gaps:

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

The next inspection found that the apparent two-source incident had expanded to
31 blocked sources. Twenty-nine were not new network failures: a July 14
`session_rebind` had created replacement epochs with stale or cross-provider
session identities. For 23 sources, the Runtime Host already proved the full
local pending range durable; six had no corresponding hosted epoch and were
replayed from their native provider identity. The remaining two were abandoned
Cursor Console canary stores. Reconciliation reduced the durable outbox to zero
without deleting provider transcripts.

Three additional files were failing before an envelope existed: two
Antigravity transcripts had valid path UUIDs overridden by legacy `ag-live-*`
bindings, and one Codex JSONL contained a single record larger than the 32 MiB
storage-v2 limit. Preparation errors were put back into the local queue every
few seconds, counted as consecutive shipping failures, and had no durable
quarantine record for the operator to inspect. Healthy Cursor envelopes still
shipped between those failures. This was a second retry storm hidden below the
durable outbox abstraction.

Finally, restarting an aligned Machine Agent reconstructed about 7 GB of legacy
archive ranges even though storage-v2 state was already current. The range and
byte counters collapsed through local retirement with zero archive send
attempts, while health said `Uploading archive backlog`. Reconciliation work
must not be presented as network upload or used to estimate upload throughput.

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
8. Provider-native identity, Longhouse session identity, and control-channel
   identity are distinct typed fields. A binding from one domain cannot silently
   replace another.
9. Every discovered source reaches a durable terminal state even when envelope
   preparation fails; in-memory retry timers are never the sole record.

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

Preparation failures use the same classification before any HTTP request is
made. A stable malformed identity, oversized record, unsupported framing, or
parser invariant failure is permanent for that exact source revision. Retrying
the same bytes cannot repair it.

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

### Identity binding

The engine validates a binding before it can create or replace a source epoch:

- the binding provider must equal the discovered source provider;
- the bound Longhouse session ID must be a UUID;
- provider-native IDs such as `ag-live-*` remain provider session IDs and never
  occupy the Longhouse session-ID field;
- a rebind records old and new typed identities plus its evidence origin;
- an already accepted native epoch is not replaced merely because a stale
  control binding appears later.

Invalid bindings are ignored in favor of the parser's native identity and
recorded as repairable identity diagnostics. They cannot generate a replacement
epoch or enter the send scheduler.

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

### Preparation failure

Source discovery persists a preparation intent before parsing or framing. A
permanent preparation failure transitions that intent to `quarantined` with the
provider, canonical path, file identity, source revision, failing offset, limit,
and evidence hash. It makes one scheduler attempt and no timed retries.

For an oversized LF-delimited record, storage-v2 mirrors the legacy shipper's
existing range dead-letter behavior: retain an exact evidence pointer for the
record, advance only with an explicit gap receipt understood by the Runtime
Host, and continue with later records. If the host contract cannot represent
that gap, quarantine the source revision rather than spin or silently skip it.
Whole-document sources cannot skip a record and remain quarantined until their
framing or limit changes.

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

The state machine begins at discovery, not after envelope serialization, so
identity, parser, framing, and raw-record-limit failures are represented too.

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

Health also separates `discovering`, `reconciling`, and `uploading`. Bytes being
hashed, compared, or retired locally are not upload backlog. Upload rate and ETA
exist only after actual archive send attempts. A component restart must retain
storage-v2 completion and must not materialize a second legacy backlog for the
same accepted ranges.

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
- invalid cross-provider bindings and non-UUID provider control IDs;
- an oversized LF-delimited record with valid tail records;
- Machine Agent and Runtime Host restart during every delivery state;
- restart with storage-v2-current sources and stale legacy cursor state;
- stale managed bridge with a live app-server child and PID-reuse rejection.

Pass conditions:

- healthy live sources remain under the ten-second p95 SLO;
- a poisoned source makes at most one live attempt;
- a permanent preparation failure makes one attempt, is inspectable after
  restart, and cannot increment transport-failure counters indefinitely;
- no source has two concurrent deliveries;
- ambiguous commit and replacement recovery preserve raw identity and produce
  no duplicate durable range;
- catch-up increases toward safe host capacity and backs off without starving
  live work;
- current health becomes healthy after successful recovery while retaining the
  incident in history;
- local reconciliation is never labeled or measured as upload;
- every quarantine has an exact operator recovery path.

## Delivery Order

1. Enforce typed provider/session bindings and repair the historical rebinds.
2. Persist adopted render authority and add epoch-replacement conflict receipts.
3. Start the per-source delivery state machine at discovery, including
   preparation quarantine and oversized-record gap handling.
4. Split current health from rolling incident history and distinguish local
   reconciliation from upload.
5. Add exact-source inspect/reconcile/retry/discard commands.
6. Prevent storage-v2-current ranges from reappearing as legacy restart backlog.
7. Tune the catch-up controller against fault-injection and mixed live/archive
   load, then add state-verified managed-process reap.
