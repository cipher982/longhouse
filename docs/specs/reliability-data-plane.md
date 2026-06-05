# Reliability Data Plane

**Status:** Phase 5 legacy exporter implementation in progress; backup gate satisfied for additive read-only export
**Owner:** Longhouse core
**Created:** 2026-06-05
**Branch:** `epic/reliability-data-plane`

## Executive Summary

Longhouse must preserve raw provider transcript fidelity without letting raw
archive size destabilize day-to-day product behavior.

The current hosted dogfood tenant proves that the monolithic SQLite layout has
crossed a reliability boundary:

- `longhouse.db` is about 116 GB, with a roughly 497 MB WAL.
- The DB contains about 12.6k sessions, 4.1M extracted events, and 3.2M
  source-line archive rows.
- Normal product paths have recently been affected by cold archive/search
  tables: session list timed out on `source_lines`, and hot writes hit pool
  exhaustion when request sessions waited behind serialized writes.
- Live DB diagnostics such as full `dbstat` and aggregate raw-size scans can
  take longer than 60 seconds on the tenant, so debugging itself can become
  production load.

This is not evidence that SQLite is wrong for Longhouse. It is evidence that
the current DB is doing too many jobs:

1. hot product/control state,
2. live runtime state,
3. raw transcript archive,
4. extracted event cache,
5. FTS/search,
6. background job state,
7. operational diagnostics.

The target architecture is:

```text
hot.db        small, authoritative product/control state
archive/      immutable compressed raw source chunks + checksums
derived.db    rebuildable event/search/detail cache
```

Normal launch, heartbeat, local health, timeline list, and control paths must
work using only `hot.db` and live channels. Raw archive and derived search/detail
may lag, rebuild, or be temporarily unavailable without making Longhouse appear
down.

## Product Goals

1. Make session list, timeline cards, launch, control, heartbeat, and local
   health boring and reliable under heavy dogfood usage.
2. Preserve raw source fidelity so parser/schema changes can rebuild derived
   state from original provider data.
3. Keep the core Runtime Host SQLite-only and self-host friendly.
4. Make archive lag visible as archive/indexing state, not as product failure.
5. Make future hosted object storage possible without requiring it for core.

## Non-Goals

- No Postgres requirement for core Runtime Host.
- No Kafka, Redis, or external distributed job system in this epic.
- No destructive migration, compaction, raw deletion, or `VACUUM` of the hosted
  dogfood DB without a separate explicit operator approval.
- No cutover to new reads until the old and new paths run side by side with
  verification.
- No production data export, cutover, compaction, or deletion before the backup
  gate is satisfied and explicitly reviewed.

## Current Evidence

The June 5 hosted dogfood incident had two immediate fixes:

- `e7edb946`: release request DB sessions before queued hot writes.
- `0c2c8619`: avoid a source-line OR scan in Codex continue-target resolution.

Those fixes restored service, but the root pattern is broader:

- hot product paths can reach into cold archive/search state;
- one SQLite file/WAL/pool/writer hosts both live control and raw history;
- list endpoints construct rich cards from many subsystems at request time;
- raw source fidelity is stored inside the hot operational DB as millions of
  rows;
- diagnostics lack cheap precomputed byte/shape telemetry.

Known current hot-path dependencies to eliminate:

- no-query session list currently reads bounded first-user text from
  `events.content_text` through `get_first_message_map`; `timeline_cards` must
  carry `first_user_message_preview` and `last_visible_text_preview` so list
  endpoints do not need `events` or `derived.db`;
- `/api/agents/presence` still needs audit/migration off request-session-held
  `WriteSerializer` execution. Heartbeat and machine-presence were fixed in
  `e7edb946`, but presence remains a Phase 1 target.

## Design Principles

### Hot Paths Stay Small

The following paths must not touch raw archive, `source_lines`, FTS, or large
event bodies:

- `/api/health`
- `/api/readyz`
- `/api/agents/heartbeat`
- `/api/agents/presence`
- `/api/agents/machine-presence`
- `/api/agents/sessions`
- `/api/timeline/sessions`
- managed-local launch/continue/control
- local health endpoints and menu-bar status

### Raw Fidelity Is Sacred

Raw source data moves out of the hot DB, but fidelity is not weakened.
Archive records must be:

- immutable once sealed,
- compressed,
- checksummed at record and chunk levels,
- replayable from a clean checkout,
- referenced by manifest rows,
- externally backupable before migration/cutover.

### Derived State Is Rebuildable

Extracted events, FTS, embeddings, summaries, and detailed session projections
are caches. They may be large and useful, but they are not the source of truth.
They must be rebuildable from archive chunks.

### Backups Before Risk

No raw-data exporter, cutover, or decommission phase may touch production raw
state until the backup gate is satisfied.

The backup gate requires:

1. an off-volume consistent backup or snapshot of the hosted tenant data,
2. restore validation on another volume/host,
3. `quick_check` or equivalent validation on the restored DB,
4. high-level counts matching the source tenant,
5. documented rollback and pause points.

Existing backups on the same volume are useful evidence, but they do not satisfy
the gate by themselves.

## Target Architecture

### Data Layout

For local/self-host/core:

```text
/data/longhouse/
  hot.db
  derived.db
  archive/
    tenants/
      <tenant_id>/
        sessions/
          <session_id>/
            manifest.jsonl
            chunks/
              source_lines-000000000001-000000005000-<payload_sha256>.jsonl.zst
              source_lines-000000005001-000000010000-<payload_sha256>.jsonl.zst
```

For hosted, the same shape remains tenant-scoped under the tenant data root.
Future object-store support can implement the same logical archive interface.

### `hot.db`

`hot.db` owns small, bounded, latency-sensitive state.

Initial hot tables:

```text
sessions
timeline_cards
session_runtime_state
machines
device_tokens
control_commands
control_command_acks
archive_chunks
archive_export_checkpoints
projector_checkpoints
```

`hot.db` may store previews and summaries, but not large raw event payloads.

`timeline_cards` is first-class. The timeline should not reconstruct cards from
event/source/archive tables on every request.

Suggested card shape:

```text
session_id
provider
project
cwd
device_id
started_at
last_activity_at
display_phase
runtime_status
summary_title
summary_status
first_user_message_preview
last_visible_text_preview
user_messages
assistant_messages
tool_calls
control_label
can_send_input
can_interrupt
can_continue
continue_target_kind
archive_state
archive_lag_bytes
archive_lag_records
derived_state
derived_revision
updated_at
```

### Archive Store

The archive store is the raw source of truth. The first implementation is local
filesystem, with an interface that can later support object storage.

Interface:

```python
class ArchiveStore:
    def write_chunk(...)
    def read_chunk(...)
    def list_chunks(...)
    def verify_chunk(...)
    def recover_orphans(...)
```

Chunk format v1:

```json
{
  "v": 1,
  "tenant_id": "david010",
  "session_id": "...",
  "stream": "source_lines",
  "source_seq": 12345,
  "legacy_ref": {
    "table": "source_lines",
    "rowid": 987654
  },
  "provider": "codex",
  "source_path": "...",
  "source_offset": 123456,
  "received_at": "...",
  "raw_sha256": "...",
  "raw_b64": "..."
}
```

Use `jsonl.zst` for v1. Store raw bytes exactly via base64 unless a provider
format is proven text-only and byte-equivalent. Compression should offset much
of the base64 overhead, and exact bytes keep the rebuild contract simple.

Write discipline:

1. write `*.tmp`,
2. flush/fsync file where practical,
3. rename to final chunk path,
4. fsync containing directory where practical,
5. insert `archive_chunks` manifest row in `hot.db`,
6. projectors consume only sealed manifest rows.

Chunk target:

- start around 32-128 MB uncompressed or 8-32 MB compressed,
- tune after measuring real archive/export throughput.

### `derived.db`

`derived.db` owns rebuildable caches:

```text
events
events_fts
tool_calls
summaries
embeddings
session_detail_projection
```

If `derived.db` is missing, locked, rebuilding, or corrupt:

- launch/control/health/session list must still work;
- detail/search may show indexing or temporarily unavailable state;
- rebuild should be possible from archive chunks and checkpoints.

### Projectors

Projectors consume sealed archive chunks and update `hot.db` and `derived.db`.

Checkpoint identity:

```text
projector_name
parser_revision
session_id
chunk_id
chunk_payload_sha256
last_record_ordinal
status
updated_at
```

Parser revision is required. Parser changes must trigger explicit reproject
instead of silently mixing old and new derived semantics.

Initial hot-card projection is conservative. A sealed chunk batch is only
allowed to overwrite session/card hot fields when the visible archive coverage
appears complete for that session, currently anchored by source offset `0`.
Partial shadow chunks for old sessions are checkpointed, but they do not regress
existing hot counts or previews. When later legacy export inserts older chunks,
the same parser revision can rebuild from the full sealed session archive.

## Migration Strategy

The migration is additive and reversible until explicit decommission approval.

### Step 1: Schemas and Stores, No Behavior Change

Add archive manifest/checkpoint tables and store abstractions. Do not move
legacy data.

### Step 2: Archive Writer and Verifier

Implement filesystem archive chunk writing, reading, verification, and orphan
recovery against fixtures.

### Step 3: Shadow-Write New Ingest

For new source data:

- keep legacy writes active;
- also write archive chunks;
- project cards/events in shadow;
- compare old/new visible projections.

Initial shadow archive writes are gated by
`LONGHOUSE_ARCHIVE_SHADOW_WRITE_ENABLED`. Source-line archive chunks are
retry-idempotent: dynamic ingest timing is not included in the chunk payload,
so replaying the same source lines writes the same chunk path and manifest row.
The first hot-card projector supports generic normalized event JSON plus the
existing Claude JSONL shape. Unsupported raw formats get terminal
`unsupported` checkpoints for that parser revision instead of retrying forever.
The first derived projector writes a narrow `derived_events` table and
`derived_events_fts` index in `derived.db`; it is still an offline/shadow
projector and is not wired into request-time detail/search reads.

### Step 4: Legacy Raw Exporter

Build a resumable, read-only exporter from legacy monolith to archive.

Requirements:

- keyset pagination, no broad `OFFSET`;
- short read transactions;
- bounded memory;
- per-chunk checksums;
- per-session export ledger;
- low-disk pause guard;
- no destructive writes to legacy raw tables;
- exports both `source_lines.raw_json_z` and `events.raw_json_z` unless
  redundancy is proven later.

### Step 5: Project from Archive

Rebuild timeline cards and derived event/search state from archive chunks.
Compare against legacy behavior for sampled sessions, including heavy Codex
sessions with thousands of tool calls.

### Step 6: Read Cutover

Cut over in order:

1. session list from hot cards,
2. timeline cards from hot cards,
3. control/presence from hot state,
4. detail from derived DB with fallback/indexing UI,
5. search from derived DB.

### Step 7: Archive-Primary Writes

New raw data writes archive-primary. Legacy raw writes stay behind a fallback
flag until confidence is high.

### Step 8: Decommission Legacy Raw Storage

Only after backup, export, replay, comparison, and explicit maintainer approval.
Preferred reclaim path is to build clean stores from archive, not in-place
SQLite surgery on the 116 GB DB.

## Backup Plan

Given the hosted tenant shape:

```text
DB:      ~116 GB
WAL:     ~497 MB
backups: ~28 GB
free:    ~73 GB
```

Do not require a local full duplicate on the same volume. There is not enough
safe headroom.

### Backup Gate

Before Phase 5 exporter against production, satisfy one of:

1. hosted volume snapshot including DB, WAL, and archive path;
2. SQLite online backup to an external volume;
3. SQLite backup API streaming to remote object storage;
4. existing backup restored and validated off-host.

Validation:

- restored DB opens;
- `PRAGMA quick_check` passes or equivalent;
- high-level counts match source:
  - sessions,
  - events,
  - source_lines,
  - archive manifest/checkpoint rows if present;
- restored health/doctor command works without using live tenant.

### Backup Gate Evidence

On 2026-06-05, the hosted dogfood tenant was quiesced, checkpointed, and copied
off-volume to the NAS:

```text
/volume1/homes/drose/longhouse-backups/reliability-data-plane-20260605/david010/20260605T192032Z-consistent/
```

Validation performed:

- tenant service was stopped before copy;
- `PRAGMA wal_checkpoint(TRUNCATE);` returned `0|0|0`;
- restored NAS copy opened read-only with SQLite;
- restored counts matched the live tenant after restart for:
  - `sessions=16261`,
  - `events=9138514`,
  - `source_lines=12844177`,
  - `session_messages=0`,
  - `session_live_previews=63`;
- restored `page_count=30309079`, `freelist_count=0`;
- live tenant returned `{"status":"ok"}` from `/api/readyz` after restart;
- full `PRAGMA quick_check` on the NAS copy emitted no errors while running but
  was interrupted after more than one hour because the scan was trending
  multi-hour on NAS storage.

This is accepted as equivalent validation for Phase 5's additive read-only
exporter work. It is not approval for destructive raw deletion, compaction, or
read cutover; those still require a fresh review gate.

### Export Disk Floor

Exporter must check disk before each chunk and pause below a configured floor.
Initial hosted floor: 30 GB free.

### Prohibited Before Cutover

- live `VACUUM` of the 116 GB DB,
- `VACUUM INTO` on the same volume,
- table rebuilds of raw tables,
- broad `CREATE INDEX` on huge raw tables without a space plan,
- full `dbstat` or raw-size aggregate scans during production load,
- deleting legacy raw rows.

## Implementation Phases

### Phase 0: Spec and Review

Deliverables:

- this spec,
- task checkpoint file,
- Hatch Expert design refinement captured in this spec,
- Hatch Opus spec review,
- maintainer review pause.

Acceptance:

- no product behavior change,
- spec committed on worktree branch,
- no code implementation started.

### Phase 1: Hot-Path Guardrails

Goal: reduce current blast radius before large storage migration.

Scope:

- assert no request handler holds a DB session while awaiting `WriteSerializer`;
- add route-level DB/write timing where missing;
- add pool checkout wait visibility;
- add tests for session-list/launch/heartbeat under writer pressure;
- remove or gate remaining hot endpoint reads from `source_lines`, FTS, or
  large event bodies;
- introduce cheap diagnostics for DB size/WAL/archive backlog without full
  table scans.

Acceptance:

- heartbeat/presence cannot exhaust request DB pool under synthetic writer
  saturation;
- session list remains bounded on a fixture shaped like the hosted tenant;
- diagnostics do not require full DB scans in normal health/debug commands.
- `/api/agents/presence` no longer holds a request DB session while awaiting a
  queued serialized write.

Tests:

- unit tests for DB-session release before serialized writes;
- integration test with saturated write queue and concurrent health/list/launch;
- query-shape regression tests for `source_lines` access;
- smoke test for hosted-debug diagnostics against fixture DB.
- regression test proving no-query session list does not read `events` for
  card previews once hot cards are active.

Review gate: Hatch Opus review.

### Phase 2: Filesystem Archive Store

Goal: implement archive primitives with no production ingest cutover.

Scope:

- `ArchiveStore` interface;
- `FilesystemArchiveStore`;
- chunk writer/reader/verifier;
- orphan recovery;
- archive manifest/checkpoint models;
- CLI or internal verifier command for local fixtures.

Acceptance:

- exact raw byte roundtrip;
- chunk corruption detected;
- stale temp-write crash artifacts are quarantined without racing live writers;
- rename-before-manifest crash can be detected as an untracked sealed chunk;
- duplicate chunk writes are idempotent and duplicate in-chunk source sequences are rejected;
- manifest entries are append-only/sealed.

Tests:

- zstd chunk roundtrip;
- record SHA mismatch;
- file SHA mismatch;
- malformed JSONL record quarantine;
- chunk boundary tests;
- orphan recovery tests.

Review gate: Hatch Opus review before any production exporter work.

### Phase 3: Hot and Derived Store Skeletons

Goal: separate store wiring without changing active read paths.

Scope:

- configurable paths for `hot.db`, `derived.db`, and archive root;
- separate session factories/pools/serializers;
- migrations for empty hot/derived DBs;
- derived unavailable/locked behavior tests.

Acceptance:

- derived DB can be deleted/rebuilt without affecting hot DB tests;
- hot DB WAL remains independent under derived load;
- no cross-DB transaction assumption appears in code.

Tests:

- empty DB migration tests;
- derived lock/unavailable integration tests;
- restart/reopen tests;
- config path tests.

Review gate: Hatch Opus review.

### Phase 4: Shadow Ingest and Projectors

Goal: write new raw data to archive in parallel and project from archive without
cutting over reads.

Scope:

- archive shadow-write for new ingest;
- live lane keeps existing hot session state unchanged by default;
- projector from archive to hot cards;
- projector checkpoints with parser revision;
- projector from archive to derived event/search state;
- comparison tooling between legacy and projected views.

Acceptance:

- existing product behavior unchanged by default;
- feature flag enables shadow archive writes;
- source machine can observe archive high-water mark;
- projector lag is visible but not product-fatal;
- restart resumes projector from checkpoint.

Tests:

- fake machine transcript burst;
- process restart mid-ingest;
- 13k-tool-call synthetic session;
- projector restart from checkpoint;
- duplicate/out-of-order record handling;
- detail/search lag does not break session list/control.

Review gate: Hatch Opus review.

### Phase 5: Backup Gate and Legacy Exporter

Goal: export existing raw fidelity to archive after validated backup.

Hard precondition:

- backup gate satisfied and recorded in the spec decision log.

Scope:

- resumable exporter from legacy `source_lines` and raw `events`;
- export ledger;
- low-disk pause;
- per-chunk and per-session verification;
- dry-run mode;
- throttle controls.

Acceptance:

- exporter is read-only against legacy raw tables;
- can resume after kill/restart without duplicates;
- pauses below disk floor;
- verifies chunk checksums and counts;
- reports corrupted legacy rows without silently skipping them.

Tests:

- fixture legacy DB with compressed raw blobs;
- interrupted export resume;
- corrupted row quarantine;
- low-disk simulation;
- keyset pagination regression.

Review gate: Hatch Opus review before running on hosted production tenant.

### Phase 6: Read Cutover

Goal: normal product reads stop depending on cold archive/derived state.

Scope:

- `/api/agents/sessions` from hot cards;
- `/api/timeline/sessions` from hot cards;
- managed-local launch/control from hot/control state;
- detail/search from derived DB with indexing fallback;
- test hooks that fail if hot endpoints access legacy cold tables.

Acceptance:

- lock or remove derived DB and verify session list/control/health still work;
- block archive reads and verify timeline cards still work;
- session list does not query `source_lines`, FTS, or raw event bodies;
- hosted-sized fixture meets latency budget.

Tests:

- derived locked/unavailable;
- archive unavailable;
- no cold-query assertion tests;
- p95/p99 route timing on synthetic tenant;
- browser/API smoke.

Review gate: Hatch Opus review.

### Phase 7: Archive-Primary Writes and Legacy Fallback

Goal: make archive the primary raw source for new data, while preserving rollback.

Scope:

- archive-primary flag;
- legacy raw-write disable flag;
- rollback/fallback runbook;
- continued comparison samples.

Acceptance:

- new raw data can be replayed from archive only;
- disabling legacy raw writes does not affect hot product paths;
- rollback to legacy raw write path is documented and tested.

Tests:

- archive-only replay;
- fallback rollback;
- mixed old/new session replay;
- search/detail rebuild from archive.

Review gate: Hatch Opus review.

### Phase 8: Decommission Plan

Goal: reclaim old monolith storage only after explicit approval.

Scope:

- clean rebuild plan for hot/derived from archive;
- restore drill on a clean machine or volume;
- old DB retention/deletion plan.

Acceptance:

- no deletion happens in this phase without a separate maintainer command;
- restore drill proves archive can rebuild product-critical state;
- final decision recorded.

Tests:

- restore from archive to clean stores;
- smoke timeline/search/detail/control on restored data.

Review gate: maintainer approval plus Hatch Opus review.

## Decision Log

### Decision: Use Filesystem Archive First

**Context:** Hosted may eventually benefit from object storage, but Longhouse
core must stay self-host friendly and SQLite-only.

**Choice:** Implement a filesystem archive store first, behind an `ArchiveStore`
interface.

**Rationale:** It preserves local topology, avoids new credentials/services,
keeps backups straightforward, and can later map to object storage.

**Revisit if:** Hosted archive size outgrows local volume management or object
storage becomes a product requirement.

### Decision: Keep Derived Events Outside Hot DB

**Context:** Session detail/search need structured events and FTS, but these are
not required for launch/control/health/session list.

**Choice:** Put extracted events/search/detail caches in `derived.db`.

**Rationale:** Derived state can be large and rebuildable. Separating it prevents
search/detail indexing from sharing the hot DB blast radius.

**Revisit if:** Derived DB complexity exceeds value and on-demand archive replay
is fast enough for detail/search.

### Decision: No Production Export Without Backup Gate

**Context:** The hosted tenant DB is 116 GB with only 73 GB free on the data
volume. Full local duplication and in-place SQLite compaction are unsafe.

**Choice:** Legacy exporter cannot run against production until an off-volume
backup or validated restore exists.

**Rationale:** Raw fidelity is the core asset. Export/migration is additive, but
operator mistakes or disk pressure could still corrupt or lose state.

**Revisit if:** Production data is already fully mirrored and validated by a
separate backup system.

### Decision: Hot Cards Are Materialized

**Context:** Current list endpoints compose rich cards at request time from many
subsystems. That lets one cold query degrade the whole product.

**Choice:** Timeline/session cards become materialized hot rows updated by live
ingest and projectors.

**Rationale:** Product list/read paths should be predictable, cheap, and
decoupled from raw archive/search/detail availability.

**Revisit if:** Materialization drift becomes harder to manage than bounded
request-time reads, which this incident suggests is unlikely.

### Decision: Data-Plane Paths Are Explicitly Activated

**Context:** Existing deployments still use `DATABASE_URL` as the active runtime
database. Phase 3 adds future hot/derived/archive wiring but must not silently
move production traffic.

**Choice:** With no new environment variables, `hot_database_url` resolves to
the current `DATABASE_URL`, while `derived.db` and `archive/` derive from the
same directory. Setting `LONGHOUSE_DATA_ROOT` opts into the future
`hot.db`/`derived.db`/`archive` layout; explicit
`LONGHOUSE_HOT_DATABASE_URL`, `LONGHOUSE_DERIVED_DATABASE_URL`, and
`LONGHOUSE_ARCHIVE_ROOT` override both.

Relative `DATABASE_URL` values keep relative derived/archive paths for
compatibility; Phase 4 must either preserve that explicitly or move store
creation to absolute paths before projectors start creating files.

**Rationale:** This gives tests and future projectors separate store handles
without changing active read/write paths during the skeleton phase.

**Revisit if:** The Runtime Host starts with `DATABASE_URL=hot.db` everywhere
and the compatibility default is no longer needed.

## Review Plan

- Phase 0: Hatch Expert refinement complete; Hatch Opus review required before
  maintainer review.
- Phases 1-4: Hatch Opus review after each phase.
- Phase 5: Hatch Opus review before any hosted production exporter run.
- Phase 6: Hatch Opus review before read cutover.
- Phase 8: Hatch Opus plus maintainer approval before any deletion/reclaim.

## Current Pause Point

Local implementation may continue through the Phase 5 exporter. The hosted
production exporter still requires Hatch Opus review before it runs. Any
raw-data cutover, compaction, or deletion remains blocked on explicit
maintainer approval after replay/comparison evidence.
