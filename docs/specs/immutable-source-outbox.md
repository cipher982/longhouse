# Immutable Source Outbox

**Status:** Proposed
**Owner:** Longhouse Machine Agent
**Date:** 2026-07-15
**Related:** `VISION.md`, `speed-of-light-database.md`,
`storage-failure-isolation.md`, `cursor-storage-v2-source-fidelity.md`
**Supersedes:** Product Invariant 2 in `speed-of-light-database.md` and the
equivalent assumption in `cursor-storage-v2-source-fidelity.md`: the live
provider log plus a source cursor is not a sufficient retry source. After this
spec, the provider source is selected once when a pending intent commits. It is
never reread as the authority for a network retry.

## Decision

Before its first network attempt, the Machine Agent freezes each outgoing
storage-v2 envelope — exact raw records, range, and envelope identity — into
one immutable local outbox record. Every retry resends those persisted bytes
unchanged until the Runtime Host returns a durable receipt for that exact
identity. Provider content observed after the freeze goes into a *later*
record; nothing can mutate a pending one.

**Non-loss guarantee.** Every provider raw record within Longhouse's claimed
capture boundary is, at every instant, in at least one of three places:

1. the provider source at or beyond the local cursor (not yet prepared);
2. the local immutable outbox (prepared, unacknowledged);
3. the Runtime Host, proven by a validated receipt (acknowledged).

The cursor advances only on transition 2→3, in the same local transaction
that deletes the pending record. No code path discards a record without a
receipt, and no automatic repair advances the cursor past bytes it has not
proven the host holds.

Live preview is a deliberately separate path and is out of scope here:

```text
provider source
  |
  +-- live preview --> WebSocket/SSE --> current UI        (time to visibility)
  |
  `-- exact raw records --> immutable outbox --> Runtime Host --> receipt
                                                            (lossless recovery)
```

The live path may be lossy and fast; the durable path may be slow and must be
lossless. Neither substitutes for the other, and this spec changes only the
durable path.

## Why: the July 2026 incident

The storage-v2 protocol already makes exact replay idempotent: an envelope
identity resent byte-for-byte returns its original receipt. But the Machine
Agent keeps only a source cursor and reconstructs each envelope from the
*current* provider source on every attempt. That breaks exact replay whenever
the source grows after an ambiguous commit:

1. the agent posts source range `[0, A)`;
2. the Runtime Host durably commits it, but the response is lost;
3. the local cursor remains `0`;
4. the provider appends through `B > A`;
5. the agent reconstructs `[0, B)` — a *new* identity overlapping the old;
6. the Runtime Host correctly rejects it, forever.

This happened in dogfood in July 2026. For Codex source epoch
`e8465c52-…`, the host accepted `[0, 264055)` while the local cursor stayed
at `0`. The accepted prefix bytes and envelope identity still match the local
file exactly. The append-only file then grew to `1183514` bytes, so every
reconstructed retry posted `[0, 1183514)` and received
`source_epoch_conflict`. No amount of networking retry can heal it, and the
poisoned source retried rapidly enough to matter for scheduler capacity while
the legacy archive health indicator read clear.

The root cause is not the database or the transport: a distributed write
intent must be immutable before it crosses the network.

## Fidelity contract

This spec does not reduce raw fidelity. Longhouse continues to preserve every
provider message, tool call, result, timestamp, identifier, and attachment the
source exposes; exact JSONL/provider record bytes including meaningful
framing; exact extracted SQLite rows with their provider-native keys,
revisions, and storage classes; and source identity, position, ordering, and
provenance — enough raw evidence to re-render sessions with future parsers.

Physical container state is not product data: inode numbers, permissions,
SQLite page placement, free pages, index layout, checkpoint timing, and WAL
frames never observed as provider records are explicitly out of scope. For
append-only logs the durable raw records reconstruct the observed bytes; for
provider databases, fidelity means exact observed logical rows and revisions,
not a disk image.

## Core invariants

1. **Freeze before send.** No envelope crosses the network before its exact
   retry representation is committed locally.
2. **One pending intent per source epoch**, enforced mechanically by the
   table's primary key. Live and repair scheduling assign priority only; they
   share one cursor and cannot prepare competing ranges.
3. **Exact retry.** Every attempt for a pending intent uses the same range,
   ordered raw bytes, envelope identity, and request body.
4. **Append isolation.** Provider growth after preparation is eligible only
   for the next intent.
5. **Receipt-gated atomic acknowledgement.** The cursor advances and the
   pending record is deleted in one SQLite transaction, gated on a validated
   receipt for the pending identity and on the cursor still equalling
   `range_start`.
6. **Proof before reconciliation.** The client never advances to a hosted
   high-water mark; it advances only across contiguous hosted ranges whose
   identities it recomputes from exact local or persisted bytes.
7. **Raw durability is independent of rendering.** Parser, title,
   session-link, and media changes cannot alter a frozen identity, and render
   success is never a prerequisite for acknowledgement.
8. **Conflicts quarantine.** One structured conflict gets one bounded
   reconciliation; anything unproven quarantines the source with its evidence
   and releases its scheduler slot.

## Local data model

One table in the existing Machine Agent SQLite database:

```text
pending_source_envelope
  source_epoch          PRIMARY KEY   -- enforces invariant 2
  range_start, range_end
  envelope_id
  request_body_zstd                   -- exact serialized request, compressed
  created_at
  attempt_count, last_attempt_at      -- diagnostics only; never affect retry bytes
```

Provider, opaque source id, protocol version, and range kind are already
inside the serialized request; they need no correctness columns.

**Immediate representation (v1):** the frozen unit is the entire serialized
storage-v2 request body. This is deliberately the compatibility boundary — it
works against the current Runtime Host with no server change and survives
source rewrite, deletion, parser upgrade, engine restart, and ambiguous
commit.

**End state:** once request assembly is provably a pure function of raw
records plus frozen identity (i.e., no render or media metadata leaks into
the bytes), the stored representation shrinks to compressed exact raw records
plus `(range, envelope_id)`, and the body is re-serialized at send time. This
is a representation change under the same invariants and the same table, not
a second system; nothing else in this spec depends on which representation is
stored. Persisting only range metadata is never sufficient, because the
original source may change or disappear.

Preparation is bounded: the engine caps total pending bytes and stops
preparing new *repair* work under pressure, preserving capacity for live
sources. A pending record is never evicted before acknowledgement except by
an explicit, evidence-preserving operator action.

## Lifecycle and crash points

**Prepare** — one short transaction: read the cursor; if a pending record
exists, return it; otherwise select the next complete raw-record boundary,
build the deterministic envelope and request body, and insert it (the primary
key rejects a race). Commit before any network I/O.

**Send** — workers read the persisted body; they never reread the provider
source. Timeout, disconnect, restart, or sleep leaves the record unchanged.

**Acknowledge** — validate the receipt's envelope identity and durable state,
then in one transaction: check `cursor == range_start`, set
`cursor = range_end`, delete the record.

Crash analysis is exhaustive because there are only three boundaries:

- crash before commit of Prepare → nothing sent and no durability claimed; the
  provider source remains authoritative until the next observation;
- crash after Prepare, before or during Send (including a committed server
  write with a lost response) → the identical envelope is resent; the host
  replays the original receipt;
- crash after Acknowledge → the record is gone and the cursor points at the
  next range.

There is no state after Prepare commits in which bytes are both
unacknowledged and unavailable.

**Conflict** — `source_epoch_conflict` is a source-coherence problem, not a
transport error, and is never retried as one. It triggers exactly one bounded
reconciliation, then quarantine.

The only request-body supersession exception is a lineage-only repair after
quarantine. It requires Runtime Host manifest proof that the rejected target
epoch and every skipped empty predecessor are absent, and that the nearest
receipt-backed local ancestor is still the host's open epoch with the same
tenant, machine, provider, source, range kind, and accepted cursor. Local proof
also requires every skipped epoch to have no records, pending request, or
receipt-gated durable progress. The replacement changes only
`predecessor_source_epoch`, uses a compare-and-swap against the persisted body,
and archives both bodies plus the reason and structured proof before clearing
quarantine. Missing or conflicting proof leaves the original immutable intent
blocked.

## Server contract

The Runtime Host keeps exact replay as the normal ambiguous-commit recovery:

- an existing envelope identity returns its original receipt;
- a new envelope must start exactly at the contiguous accepted cursor;
- partial overlap, same range with a different identity, or a forward gap
  returns a structured conflict identifying the epoch state, the contiguous
  cursor, the overlapping range identities, and whether the epoch is closed.

**Known hazard:** today's server permits forward gaps and reports the maximum
accepted end as `accepted_through`. That is a high-water mark, not proof of
coverage — clients must never repair from it. Before contiguity is enforced
for new writes, existing production epochs must be audited. Sparse legacy
epochs remain quarantined unless their gaps can be filled from exact local
evidence; their disposition does not weaken the new write contract.
Reconciliation (below) is built so that even against the current
gap-permitting server it cannot skip data.

## Reconciliation

Advancing the local cursor from `L` to `H` without resending is allowed only
when all of the following hold:

1. machine, provider, opaque source, epoch, and range framing agree;
2. hosted per-range manifests cover `[L, H)` with no gap;
3. every hosted range boundary lands on a local raw-record boundary;
4. each hosted envelope identity recomputes exactly from local or persisted
   bytes;
5. the cursor update commits via compare-and-swap on `L`.

Outcomes:

- **exact retry** — the pending identity already exists remotely; accept its
  receipt (the normal ambiguous-commit path);
- **proven accepted prefix** — advance through exactly the proven contiguous
  end, then prepare the remainder as new ranges;
- **anything unproven** — hosted gap, local bytes unavailable, byte mismatch,
  or hosted cursor behind local — quarantine with the specific evidence.
  Never advance, never skip, never silently open a successor epoch.

Applied to the incident epoch: proof holds through byte `264055`, so recovery
advances exactly there and prepares `[264055, 1183514)` as fresh ranges. No
larger jump is safe.

## Scheduling and isolation

The pending row and cursor update share one SQLite transaction. The
`source_epoch` primary key serializes competing preparers, including a daemon
and a manual ship command using the same state database. Concurrent senders
may post the same persisted body; exact replay makes that harmless. No
separate shipping lease is required.

- A pending exact retry outranks preparing new work for its source.
- Transient network errors get bounded backoff; a structured conflict gets
  one reconciliation, then quarantine.
- Quarantined sources hold no live in-flight slot; repair work cannot consume
  capacity reserved for newly observed live records.
- Total pending bytes and oldest pending age are the two pressure signals.

## Health

Health reports independent facts, not one headline. The minimum set:

```text
network            reachable | unreachable (with cause)
durable_shipping   last_success_at, pending count/bytes/oldest_age,
                   blocked_source_count (each with its evidence)
archive_projection pending_count / oldest_age
```

`archive_projection` clear never implies storage-v2 sources are synchronized;
`network` reachable never implies shipping is caught up; one source's success
never clears another source's conflict. Menu bar example:

```text
Durable upload blocked for 1 source
Network reachable · archive projection clear · last durable upload 3m ago
```

## Rollout

1. **Stop creating poisoned epochs (client-only, compatible with the current
   Runtime Host):** add the pending-envelope table; persist the compressed
   request before POST; retry persisted bodies and acknowledge atomically;
   classify 409 separately from transport failures.
2. **Make reconciliation provable (server):** expose a bounded,
   machine-authenticated per-range source-epoch manifest; return structured
   conflict evidence; audit existing hosted gaps; enforce contiguity for new
   protocol writes.
3. **Recover existing state:** inventory local epochs against manifests;
   auto-repair only proven contiguous prefixes (the dogfood epoch through
   byte `264055`); quarantine everything else with evidence preserved.
4. **Delete the parallel machinery:** the outbox becomes the only Machine
   Agent durable retry path; retire the legacy transcript spool after
   coverage proof; remove archive-backlog fields that imply storage-v2
   health. One cursor, one pending intent, one receipt, one health model.

## High-value proofs

1. Server commits, response is lost, source grows: the persisted retry
   returns the original receipt and the cursor advances exactly once.
2. Engine restart at each of the three crash boundaries yields the same final
   cursor with no loss or duplication.
3. Reconciliation advances only across contiguous hosted ranges whose
   identities recompute from exact bytes; one changed byte refuses repair.
4. The server rejects a forward gap while still replaying the original
   receipt for exact replay.
5. N quarantined conflicts cannot occupy the N live scheduler slots, and
   health reports them independently of network and archive projection.

## Non-goals

- Kafka, Redis, or any external durable service.
- Mirroring provider filesystem or SQLite physical layout.
- Gating raw acknowledgement on parser or render success.
- Skipping unproven bytes to turn a health indicator green.
- Redesigning live preview or remote control.

## Acceptance

Done when: a lost response followed by arbitrary source growth cannot change
a retry; the three-place non-loss guarantee holds at every crash boundary; no
automatic repair advances past proven contiguous evidence; one poisoned
source cannot reduce live capacity; health distinguishes transport, durable
shipping, and archive projection; and the legacy retry path is deleted, not
maintained in parallel.
