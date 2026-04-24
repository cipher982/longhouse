# Replay-Safe Transcript Ingest

Status: Active design
Owner: Longhouse session kernel
Updated: 2026-04-24

## Goal

Make historical replay and startup recovery harmless by construction.

The canonical transcript must be raw source lines plus explicit rewind hints.
Parsed events, managed turns, summaries, embeddings, runtime progress, and other
derived products must only move when canonical source state moves.

## Why This Exists

The April 14, 2026 replay incident exposed two separate problems:

- local shipper state loss triggered a broad historical fallback scan
- hosted ingest still accepted some replayed rows as new because sourced event
  identity depended on parsed event shape, not just canonical source truth

Several mitigations already landed:

- `AgentSession.transcript_revision`, `summary_revision`, and
  `embedding_revision` gate post-ingest work on canonical transcript change
- stale pending summary/embedding rows self-close during recovery instead of
  draining as thousands of no-op tasks
- missing local shipper DB state now reports `shipper_state_missing` and the
  legacy DB path migrates into the new agent home during install
- sourced event hashing is now stable for identical raw lines even when parser
  timestamp normalization drifts

Those fixes reduce spend and queue churn, but the system is still carrying too
much hash-driven derived identity.

## Canonical Truth

`AgentSourceLine` is the canonical transcript ledger.

Canonical identity is:

- session
- branch
- `source_path`
- `source_offset`
- `revision`
- `line_hash`

Canonical mutations are:

- append a new source line at a new offset
- rewrite an existing `(source_path, source_offset)` with a new `line_hash`
- explicit rewind / truncation from the engine

Everything else is derived.

## Derived Truth

`AgentEvent` is not canonical. It is a parser projection of a canonical source
line.

One source line can legitimately produce multiple derived events, so replay-safe
identity cannot be only `(source_path, source_offset)`.

The durable derived identity for sourced events should be:

- `source_line_id`
- `event_slot` (0-based ordinal of the parsed event within that source line)

Equivalent alternate form:

- `(session_id, branch_id, source_path, source_offset, revision, event_slot)`

`event_hash` should remain metadata for forensic diffing or unsourced events, but
it should stop being the uniqueness key for sourced transcript rows.

## Ingest Contract

Normal ingest should run in this order:

1. Normalize incoming source lines and explicit rewind hints.
2. Resolve append vs rewrite vs truncation against the current head branch.
3. Write canonical `AgentSourceLine` rows first.
4. If there is no canonical source-line delta and no rewind, return without
   writing new `AgentEvent` rows.
5. Only derive `AgentEvent` rows from the canonical source rows that are new for
   this branch head.
6. Only after canonical delta exists should transcript-derived work be queued.

This removes the current bad state where duplicate sourced events can be
inserted even though `transcript_changed` is false.

## Branch / Rewind Semantics

Rewind is a canonical source event, not an event-hash heuristic.

Rules:

- append to the current head branch when every incoming source line is either
  new or byte-identical to the latest stored line at that offset
- fork a new branch at the earliest rewritten or explicitly rewound offset
- copy canonical source prefix rows into the new branch before appending the
  rewritten suffix
- copy or re-materialize derived event prefix rows from those copied source
  rows, but never let replay invent new canonical history

The branch decision should be made from `line_hash` plus explicit rewind hints,
not from parsed event timestamp or JSON canonicalization drift.

## Post-Ingest Derived Work

All expensive derived work should be revision-addressed.

Rules:

- `transcript_revision` increments exactly once per canonical source delta
- summary tasks are keyed by `(session_id, transcript_revision, task_kind)`
- embedding tasks are keyed the same way
- workers close stale rows whose requested revision is already behind the
  session's current revision
- runtime progress signals, managed-turn durability, and transcript turn
  materialization only fire when `transcript_revision` changes

This keeps replay harmless even if the queue or worker restarts later.

## Startup Recovery Semantics

State loss on the local machine must not silently cause hosted spend.

Required behavior:

- distinguish `fresh_install`, `migrated_install`, and `shipper_state_missing`
- `shipper_state_missing` is degraded, not "scan everything from disk"
- startup may bind to live sessions and report health, but historical backfill
  requires an explicit operator action
- first install should default to forward-only capture, not automatic deep
  historical import
- any broad rebuild must be an explicit command such as `import-history` or
  `rebuild-projections`, with operator-visible scope

The product rule is simple: startup recovery must preserve correctness, but it
must not create surprise LLM/API spend.

## Parser Upgrade Semantics

Parser upgrades are a projection concern, not a source-truth concern.

Rules:

- replay of old source lines must not implicitly re-derive events just because a
  parser changed
- parser/version backfills are explicit maintenance operations
- backfills should target sessions or time windows intentionally and stamp their
  own progress
- source truth remains stable across parser versions

## Implementation Sequence

### Phase 1: Landed / immediate mitigation

- gate summary and embedding work on `transcript_revision`
- self-clean stale pending derived-work rows
- suppress post-ingest work when sourced replay has no canonical source delta
- stabilize sourced event hashing for identical raw lines

### Phase 2: Replace sourced event identity

- add `source_line_id` to `AgentEvent`
- add `event_slot` to `AgentEvent`
- create a unique index on `(source_line_id, event_slot)` for sourced rows
- keep `event_uuid` uniqueness for provider-native lineage ids
- demote `event_hash` from sourced uniqueness to metadata

### Phase 3: Make canonical-first ingest explicit

- write / resolve `AgentSourceLine` rows before any event inserts
- derive events only from source rows that are new for the target branch
- if no source delta exists, return `events_inserted == 0`

### Phase 4: Make derived work fully revision-addressed

- unique queue rows by `(session_id, task_kind, transcript_revision)`
- make worker completion stamp the revision it satisfied
- keep startup recovery limited to closing stale rows and enqueuing only the
  current revision

### Phase 5: Make startup behavior explicit

- codify the no-implicit-history-import rule in engine startup
- expose `shipper_state_missing` clearly in machine health
- provide explicit operator commands for history import and projection rebuild

### Phase 6: Migrate or supersede legacy sourced event rows

- either backfill `source_line_id` / `event_slot` for existing sourced events
- or leave historical rows readable and only require the new identity for fresh
  ingests after migration

## Done Condition

This work is done when:

- identical historical replay cannot insert sourced event rows or trigger
  summary / embedding work without a real canonical source-line delta
- startup state loss cannot silently cause a broad historical import
- sourced event uniqueness is anchored to canonical source rows plus event slot,
  not parsed event hashes
- parser upgrades are explicit maintenance work rather than an accidental side
  effect of replay
