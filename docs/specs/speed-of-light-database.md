# Speed-of-Light Database

**Status:** Proposed replacement architecture
**Owner:** Longhouse core
**Created:** 2026-07-11
**Supersedes when implemented:** the cold-monolith serving path in
`storage-failure-isolation.md`; the database portions of
`archive-backlog-repair.md` and `hosted-archive-restart-control.md`

**Shared contract:** `speed-of-light-shipper.md` remains canonical for the
Machine Agent lane model. This spec applies the same lanes to Runtime Host
storage; it does not define a second scheduler vocabulary.

## Decision

Longhouse should stop treating one ever-growing SQLite file as both the raw
archive and the query engine.

The target data plane is:

```text
Machine Agent ──► API/control process (no SQLite)
                       │
                       ├─► persistent raw-object workers ──► immutable raw objects
                       │                                         │
                       └─► catalogd over Unix socket              │
                               │                                 │
                               ├─► catalog.db manifest commit ◄──┘
                               ├─► session/runtime/control truth
                               ├─► coalesced projector state
                               └─► durable receipt + commit_seq

immutable raw objects ──► versioned render objects ──► direct detail reads
                      ├─► search.db / embeddings (rebuildable)
                      ├─► content-addressed media
                      └─► backup/object-store replication
```

There is no cold archive monolith in the final serving architecture.

- `catalog.db` is the only authoritative mutable database. A narrow persistent
  `catalogd` process exclusively owns it; the API/control process never loads
  SQLite. The catalog is small relative to transcript history, grows by sessions
  and immutable objects rather than events, and contains no transcript bodies.
- Immutable, checksummed, compressed raw objects are the source of truth for
  transcript history.
- Parser-independent raw objects and parser-versioned render objects are
  physically separate. Re-rendering never rewrites or duplicates raw truth.
- Search, embeddings, worklog exports, and other broad query structures are
  derived and rebuildable. They may be unavailable without affecting launch,
  control, timeline, or raw transcript durability.
- The API never starts a full Python/ASGI subprocess for one archive request.
- The Machine Agent never performs a cold-database proof request before every
  archive write. Content identity and raw-object manifests make retries
  idempotent; the manifest commit is the receipt.

This preserves the product constraint that SQLite is the only required core
database. It also takes SQLite out of the job it is worst at here: one mutable,
138 GB, write-heavy database simultaneously serving raw history, operational
state, projections, repairs, search, and maintenance.

## Why This Is the Right Rewrite

The current system contains the seeds of the right architecture but stops one
boundary too early.

At the largest dogfood tenant on 2026-07-11:

- `longhouse.db` was about 138 GB;
- `longhouse-live.db` was about 496 MB;
- immutable compressed archive chunks were about 34 GB;
- the cold database had roughly 10 million events, 14 million source lines,
  and 10 million observations;
- the archive directory held about 240,000 sealed chunks;
- a one-row archive manifest request could return a typed 503 after waiting one
  second for the single read slot;
- a successful worklog read took about six seconds and returned only 511 KB;
- during archive drain, more than half of sampled source-line proof requests
  were rejected with 503 while hot readiness remained green.

The hot catalog is already less than one percent of the cold database size and
contains the state required for Longhouse's launch loop. The immutable archive
already stores the raw value. The 138 GB monolith is therefore mostly an
expensive compatibility projection with failure authority it no longer needs.

The current cold-read isolation path makes that mismatch visible. One request:

1. waits on one global semaphore;
2. serializes the HTTP request into JSON and base64;
3. starts a new Python interpreter;
4. imports the complete application and SQLAlchemy model graph;
5. reconstructs the request through an in-process ASGI client;
6. opens the 138 GB SQLite file read-only;
7. serializes the response into JSON and base64;
8. returns it to the parent process; and
9. destroys the process.

This is good emergency crash containment. It is not a database architecture.

The ingest path is similarly overextended. A historical range may first call
the cold database to prove that source lines exist, fall through on pressure,
submit another JSON file to a filesystem job queue, poll for a result file,
write immutable archive chunks, insert manifests and normalized rows into the
cold monolith, update projections, reopen both databases, and finally copy the
session back into the hot catalog. Each layer was locally reasonable. Together
they do too much work and create too many independent states.

The current Sauron alert bundle is not evidence of one database failure. The
`worklog` failure is directly tied to archive pressure: Longhouse returned
`archive_route_unavailable`. The `zerg-extended-ci` failure is a GitHub workflow
dispatch `422`, and `barstudy-b2-marker-probe` reported its own failed result.
They share one aggregate automation-health surface, so unrelated latest-run
failures appear and remain critical together. Phase 0 must fix the database
pressure and the health/recovery semantics without pretending the GitHub and
marker failures have the same root cause. Life Hub and Zerg co-occurrence should
likewise be treated as a lead about shared host/scheduler pressure, not proof
that they share a database failure.

## Product Invariants

These are non-negotiable.

1. **Acknowledged transcript bytes survive a process crash.** The Runtime Host
   acknowledges ingest only after immutable bytes and their catalog receipt are
   durable.
2. **The provider log remains the retry source until acknowledgement.** A server
   crash before acknowledgement does not require a server-side durable request
   queue; the Machine Agent retries the same content identity.
3. **Hot product state never waits on cold history.** Login, timeline, launch,
   send, interrupt, heartbeat, runtime state, and readiness work when archive
   reads, search, indexing, compaction, or enrichment are stopped.
4. **Raw history has one authoritative representation.** Raw provider records
   are stored once in immutable raw objects, not copied into events,
   source-lines, and observation BLOB columns.
5. **Derived data is disposable.** Render objects, search, embeddings, summaries,
   worklog indexes, and analytics can be rebuilt from raw objects plus catalog.
6. **One process owns all catalog access.** `catalogd` owns SQLite reads, writes,
   schema, WAL recovery, and migrations. API and background workers exchange
   typed facts over a local Unix socket; they never open `catalog.db`.
7. **No unbounded work occurs during startup or request handling.** Recovery,
   migration, compaction, indexing, verification, and repair are explicit,
   resumable jobs.
8. **Pressure is lane-specific.** Historical repair can slow or pause itself;
   it cannot consume the capacity reserved for live ingest or user reads.
9. **Every failure is attributable.** A slow request reports whether time was
   spent in admission, decode, object write, catalog commit, object read,
   search, or projection.
10. **Cold restart is ordinary.** A new Runtime Host reconstructs truth from a
    small catalog and immutable files without replaying a giant mutable log
    before becoming useful.
11. **Source replacement is explicit.** File rotation, truncation, rewrite,
    database revision, or path reuse starts a new source epoch; offsets never
    silently reuse an old identity namespace.
12. **Lossless history includes media.** Referenced images/files are
    content-addressed objects with explicit present/missing state, not incidental
    side effects outside the durability contract.

## Speed-of-Light SLOs

These are product budgets, not benchmark aspirations.

| Surface | Target | Hard failure boundary |
| --- | ---: | ---: |
| `/api/readyz` internal p95 | 25 ms | 100 ms |
| hosted hot API external p95 | 250 ms | 1 s |
| provider append to durable host acknowledgement p95 | 2 s | 10 s |
| durable acknowledgement to UI canonical visibility p95 | 250 ms | 1 s |
| timeline first page external p95 | 300 ms | 1 s |
| session detail first byte external p95 | 300 ms | 1 s |
| session detail first page external p95 | 500 ms | 2 s |
| recent lexical search external p95 | 500 ms | 2 s |
| all-history lexical search external p95 | 1 s | 5 s |
| one-day worklog export p95 | 1 s | 5 s |
| cold-read 503 rate during maximum archive repair | 0 | 0.1% |
| acknowledged raw data loss after process/container restart | 0 bytes | 0 bytes |
| hosted off-host disaster RPO | 5 min target | 15 min alert threshold |

The 10-second live-ingest SLA remains the user-visible guardrail. The internal
target is two seconds so ordinary variance does not consume the entire budget.
Latency gates are measured at the largest supported tenant size while the
mixed-load harness runs maximum-safe repair, user reads, live ingest, indexing,
and compaction concurrently.

## Storage Model

### 1. `catalogd` and `catalog.db`: bounded operational truth

`catalogd` is a narrow supervised process reachable only over a local Unix
socket. It owns all SQLite connections, schema migrations, WAL recovery, reads,
and writes. Its RPC surface exposes product operations and typed records, never
SQL or ORM objects.

The catalog contains one row per durable object/identity or current state, not
one row per transcript event. "Bounded" means growth is proportional to
sessions, source epochs, and immutable objects with explicit retention—not that
the file is constant-size.

Core tables:

- `users`, `refresh_sessions`, `device_tokens`;
- `sessions`: the single session identity and timeline-card row;
- `session_threads`, `session_runs`, `session_connections`;
- `runtime_state`, `control_leases`, `launch_attempts`, `input_receipts`;
- `source_epochs`: stable provider source identity and replacement boundaries;
- `raw_objects`: manifest keyed by unique envelope identity; this row is also
  the durable receipt and carries the catalog `commit_seq`;
- `render_generations`: current and retained parser/order generations per
  session with immutable object manifests;
- `media_objects` and `session_media_refs`;
- `projector_state`: one coalescing row per `(projector, session_id)` with
  desired and completed revisions;
- `session_tombstones` and deletion revision;
- `backup_restore_points` and referenced object-set hash;
- a bounded/pruned delivery outbox only for non-rebuildable external side
  effects such as notifications;
- small registration/configuration tables such as APNs state.

The existing `live_session_catalog`, `live_timeline_cards`, and `live_sessions`
tables overlap heavily. The target has one denormalized `sessions` row for the
timeline and session identity. Runtime state remains separate because it has a
different update frequency and lifecycle.

The catalog must not contain:

- raw provider JSON or transcript text;
- compressed transcript BLOBs;
- one row per source line or semantic event;
- one outbox row per transcript event;
- FTS content or embedding vectors;
- historical runtime observations not required for current state.

### 2. Parser-independent immutable raw objects

The existing filesystem archive becomes the primary session record, but the v2
raw object format is independent of parser output.

Every provider source has identity:

```text
(machine_id, provider, opaque_source_id, source_epoch)
```

Rotation, truncation, path reuse, replacement inode, rewind, or provider
database revision starts a new epoch. A new epoch never reuses the old offset or
cursor namespace.

The stable envelope identity excludes parser revision:

```text
SHA-256(
  protocol_version,
  tenant_id,
  machine_id,
  provider,
  opaque_source_id,
  source_epoch,
  source_range,
  ordered_raw_record_hashes
)
```

Within one source epoch, accepted ranges are non-overlapping and monotonic apart
from an exact retry. `catalogd` enforces one content identity for a given source
range. The same range with different record hashes, or a conflicting partial
overlap, returns `source_epoch_conflict` and requires the Machine Agent to open a
new epoch or repair the source cursor explicitly; it is never accepted as a
second version of the same append.

A raw object is self-describing and contains:

- format/protocol version;
- tenant, machine, session, provider, source identity, and source epoch;
- source range and immutable raw order;
- exact provider source records, with inline binary fields losslessly
  externalized to typed media-hash references so the bytes are stored once;
- payload and compressed-file SHA-256;
- uncompressed/compressed sizes and record count;
- provenance for legacy fallback records.

Raw order is `(machine_id, provider, opaque_source_id, source_epoch,
source_position, raw_record_hash)`. It is unaffected by retry time, machine
clock, relinking, or parser changes. Session relinking updates catalog
membership/graph edges; it never rewrites raw objects.

Raw object sizing is bounded by the ingest protocol. One envelope may contain
at most 4 MiB uncompressed or 10,000 source records; larger source ranges are
split on record boundaries before upload. The Runtime Host never merges
unacknowledged envelopes merely to fill a larger file. Optional later
compaction preserves each envelope's identity and source-range locator.

Raw objects are sealed as:

1. encode a deterministic self-describing payload;
2. compress to a temporary file;
3. fsync the file;
4. atomically rename to a content-addressed final path;
5. fsync the containing directory;
6. commit the `raw_objects` manifest row through `catalogd`;
7. acknowledge the Machine Agent with that row's envelope identity and
   `commit_seq`.

Filesystem rename and SQLite commit are not one atomic transaction. The contract
is conjunctive durability: acknowledgement occurs only after both are durable.
If the process dies after rename but before manifest commit, deterministic retry
reuses the same object and completes registration. The orphan scanner also
reconciles sealed files on its normal L4 cadence, but acknowledgement correctness
does not depend on scanning before retry.

### 3. Versioned immutable render objects

Render objects are disposable parser caches, physically separate from raw
objects. They contain deterministic fields needed by session detail: role,
timestamp, content text, tool name/input/output, tool-call identity, thread,
branch, and a raw-object/record locator.

Each render generation declares:

- parser revision;
- ordering revision;
- source raw-object set/hash;
- generation id;
- first/last rendered cursor keys;
- render state: `current | building | failed | superseded`.

Rendered semantic order is generation-specific:

```text
(order_time, machine_id, provider, opaque_source_id, source_epoch,
 source_position, event_subordinal)
```

Every detail/search/worklog cursor carries `render_generation`. Parser upgrades
write new render objects and atomically switch the current generation through
`catalogd`; old cursors return `stale_generation` rather than silently changing
meaning. Migration parity compares the same parser and ordering revision.

Raw durability does not depend on rendering. If parsing fails, the Runtime Host
may acknowledge exact raw record content with
`raw_state=durable, render_state=failed` so the Machine Agent does not resend
durable history forever. Rendering can be retried from raw objects by a
versioned parser worker.

Render objects are capped at 4 MiB uncompressed. Their manifests select the
exact object for a cursor, so a page never hashes/decompresses tens of megabytes.
Continuous packing is deferred until measured object-count/read amplification
justifies it. Any later compactor submits a verified manifest-swap fact to
`catalogd`; it never opens the catalog itself.

### 4. Content-addressed media

Images and other referenced bytes use immutable content-addressed media objects.
The catalog records content hash, MIME/type metadata, size, owning sessions, and
`present | missing | corrupt | deleted` state. Raw/render records reference media
by hash. A transcript with missing media is explicitly degraded rather than
reported lossless.

The acknowledgement reports `raw_state`, `render_state`, and `media_state`
separately. `raw_state=durable` stops transcript resend. `media_state=complete`
is returned only after every referenced available media object and manifest is
durable; otherwise the response lists missing hashes and the Machine Agent
retains/retries those bytes independently. A source that never contained the
referenced bytes is durably classified `missing`, not retried forever and not
described as lossless.

### 5. Derived search stores

Lexical search moves to a dedicated SQLite FTS5 store containing only searchable
text and minimal filters:

- `session_id`;
- event timestamp;
- role/tool kind;
- project/provider/environment;
- content text;
- render generation, render object id, and record ordinal.

It contains no raw provider payload and is fully rebuildable from render and raw
objects.

Start with one `search.db` owned and served by one disposable search/indexer
process over local RPC; the API process does not import SQLite to query it. A
search-process crash degrades only search. When measured index maintenance or
read/write contention warrants it, seal immutable time segments:

- small mutable `search-active.db` for recent/live sessions;
- read-only `search-YYYY-MM.db` segments for closed history;
- query the active file plus only relevant time segments;
- replace a segment atomically after rebuild.

Do not build segmented FTS before the single derived store proves it is needed.
The architectural requirement is disposability and isolation, not sharding for
its own sake.

Embeddings follow the same rule: separate, optional, rebuildable, and never in
the durable acknowledgement path. Search exposes its indexed-through catalog
`commit_seq`; it never silently falls back to raw history.

## Consistency and Ordering

Longhouse has three deliberately different orders:

1. raw source order is the immutable provider order defined above;
2. catalog `commit_seq` is the total order of durable catalog mutations; and
3. semantic display order is deterministic within one render generation.

`commit_seq` is the serialization point for acknowledgement, SSE replay,
projector watermarks, backup restore points, and read-after-write. It does not
pretend that wall-clock observation time is the provider's semantic order.
Clients may send `minimum_commit_seq` after a write; `catalogd` either serves a
view at or beyond it or returns an explicit bounded retry response. Projectors
publish their completed catalog revision, and generation-qualified cursors keep
pagination stable across parser/order upgrades.

## Write Path

### Live transcript ingest

```text
Machine Agent
  ├─ observe append
  ├─ assign stable source identity + epoch
  ├─ frame exact raw records
  ├─ optionally attach parser-versioned render records
  ├─ compute parser-independent envelope identity
  └─ POST ingest envelope
        │
Runtime Host admission
  ├─ authenticate through catalogd
  ├─ classify live vs repair
  ├─ reject oversized/over-budget work before decode
  └─ reserve lane capacity
        │
Raw object workers                 Render workers
  ├─ validate/encode raw object      ├─ validate/derive render cache
  ├─ compress                        ├─ version parser/order
  └─ fsync + atomic rename           └─ seal object or fail explicitly
        │                                      │
        └──────────────────┬───────────────────┘
                           ▼
catalogd transaction
  ├─ insert raw manifest/receipt
  ├─ attach render generation or render failure
  ├─ attach media manifests/state
  ├─ upsert one session/timeline row
  ├─ advance coalesced projector desired revisions
  ├─ allocate commit_seq
  └─ commit
        │
ACK + canonical UI event
```

The acknowledgement is a typed receipt:

```text
(envelope_id, commit_seq, raw_state=durable,
 render_state=ready|pending|failed,
 media_state=complete|pending|missing, missing_media_hashes[])
```

The raw/render worker pools perform CPU/native compression and filesystem I/O.
They do not import the web application and do not open `catalog.db`. Bounded
persistent process pools give crash isolation without a new interpreter and
full application boot for every request. Rendering gets a short deadline in the
live acknowledgement path; if it is not ready, `catalogd` commits the durable
raw receipt with `render_state=pending` and rendering continues asynchronously.
If a worker exits, the pool replaces it and the client retries unacknowledged
content.

Referenced media seals through the same bounded storage-worker service before
`media_state=complete`. Media can commit before its referring transcript
envelope because content hashes are idempotent; the envelope transaction then
records the session reference and exact media state.

`catalogd` owns the catalog writer queue and serves typed reads/writes over a
Unix socket. Catalog transactions are small and deterministic. There is no
second archive `WriteSerializer` and no filesystem request/result job protocol.

The writer may group several ready ingest commits into one SQLite transaction
to amortize WAL fsync cost. Each request is acknowledged only after the group
transaction commits, and one failed item cannot partially commit the others.

### Catalog crash domain

The API/control process is SQLite-free. `catalogd` is supervised independently,
and its local RPC hop is part of every catalog operation. Longhouse has already
observed a native `_sqlite3` process crash, so reducing DB size is not enough
reason to leave terminal/control relays in the same blast radius.

If `catalogd` exits, existing in-memory terminal/control transports continue.
New auth, metadata reads, and durable writes fail explicitly with
`catalog_unavailable`; they never fall back to another database. The supervisor
restarts `catalogd`, SQLite WAL recovery completes before the socket accepts
requests, and Machine Agents retry unacknowledged envelopes. The RPC latency
budget is sub-millisecond locally and is measured in every hot request trace.

### Idempotency and reconciliation

Every ingest envelope carries the parser-independent identity defined by the raw
object contract. `raw_objects.envelope_id` is unique, and the committed manifest
row is the receipt. Retries return its object hash and `commit_seq`; no duplicate
receipt table can disagree with the manifest.

Source-line proof is not a separate cold request before every write. For broad
reconciliation, the Machine Agent can request a compact source-epoch/range
manifest through `catalogd`, or send a batch of envelope identities to a bounded
`exists` endpoint. Both are small catalog lookups and remain available during
search/index/archive maintenance.

### Runtime and control writes

Runtime state, launch, input, heartbeat, and control are catalog-first through
`catalogd`. Their small transaction is the product truth. If they also require
historical evidence, the transaction advances a coalesced projector revision and
the evidence is appended later. They never wait for archive storage.

### Derived work

After durable acknowledgement, workers claim coalesced `projector_state` rows:

- search indexing;
- summary/title generation;
- embedding generation;
- daily/worklog secondary indexes;
- optional analytics.

Each projector is idempotent on `(projector, session_id, desired_revision)`.
Many event appends update one desired revision instead of creating unbounded
outbox rows. Failure advances no completed revision. Notifications and other
non-rebuildable external effects use the separate bounded delivery outbox.
Projector lag is visible but cannot change ingest acknowledgement or hot
readiness.

## Read Path

### Timeline and session metadata

Timeline, session identity, capabilities, archive state, and current runtime
state are served through `catalogd` from `catalog.db`.

There is no duplicated timeline-card table. The `sessions` row is deliberately
denormalized for the dominant list query and joined only to bounded current
runtime/control state.

### Session detail and raw export

1. Query the current render generation and manifests through `catalogd`.
2. Select only render objects intersecting the generation-qualified cursor.
3. Read and verify immutable render objects directly.
4. Decode in a persistent worker pool.
5. Stream records as soon as the first object is ready.

Raw export follows raw manifests and emits exact records in immutable source
order. It never reconstructs raw bytes from render caches.

No cold SQLite database, SQLAlchemy model graph, ASGI recursion, base64 envelope,
or global one-reader semaphore participates.

Reader capacity is lane-aware:

- user session detail/export has reserved workers;
- background verification and compaction use leftover workers;
- per-request bytes, objects, and CPU time are bounded;
- large exports stream with cursors instead of buffering the whole response;
- cancellation stops queued work and releases the worker immediately.

### Search and recall

Search queries the derived FTS store. Search failure returns a typed search
degradation; it does not affect session detail or timeline. Results carry
generation-qualified object locators so opening a result reads authoritative
history from immutable files.

Recall may add ranking, embeddings, or LLM synthesis after lexical retrieval.
None of those become the archive source of truth.

### Worklog and day-range exports

Worklog queries the search/message projection by timestamp and streams the
selected normalized messages. It does not scan the raw archive database and it
does not need a special six-second subprocess boot. If its derived projection is
behind, it returns the indexed-through `commit_seq` and a typed `projection_lag`
response. Raw gap fill is an explicit operator/debug mode, never an automatic
fallback that changes the worklog data source under pressure.

## Admission, Scheduling, and Backpressure

The Runtime Host has independent lanes:

| Lane | Work | Reservation |
| --- | --- | --- |
| L0 | auth, control, heartbeat, readiness | always available |
| L1 | live transcript raw-object writes | strict priority |
| L2 | current-session gap repair | bounded priority |
| L3 | historical archive repair | leftover byte/IO budget |
| L4 | indexing, summary, embedding, compaction, verification | background only |

Rules:

- L3 and L4 cannot occupy L0/L1 worker reservations.
- Admission budgets bytes and estimated CPU, not just request count.
- Repair receives `Retry-After` plus a stable pressure code before body decode
  when the server is saturated.
- The Machine Agent applies the pressure signal to the whole repair lane with
  jitter, not only one source path.
- User reads never share a semaphore with source-proof or repair traffic.
- Disk pressure lowers repair and compaction budgets before it affects live
  writes.
- A user-requested `drain` means "maximum safe leftover capacity," not "ignore
  product latency."

The effective repair mode after any restart is `paused` unless an explicit,
expiring control record says `trickle` or `drain`. A drain lease has a deadline
and automatically returns to `paused` or `trickle`; persistent accidental drain
is not a valid state.

## Failure Model

| Failure | Expected outcome |
| --- | --- |
| raw/render worker native crash | uncommitted work unacknowledged; worker replaced; API/control remains alive |
| raw worker crash before raw-object rename | temp file recovered/deleted; Machine Agent retries |
| worker/API crash after raw rename before catalog commit | same hash reused on retry; manifest commit completes |
| API crash after dispatch while workers continue | a late commit is safe; retry returns the same manifest receipt |
| `catalogd` crash | existing in-memory control continues; catalog operations return `catalog_unavailable`; WAL recovery then retry |
| catalog commit failure | no acknowledgement; raw object remains harmless orphan until retry/reconcile |
| render worker/parser failure | raw may be acknowledged with explicit failed render state; rebuild from raw |
| search/index corruption | search degraded; rebuild from raw/render objects; timeline and raw export remain available |
| compactor crash | old manifests remain authoritative; incomplete object ignored |
| raw/media object corruption | typed session-range degradation; restore by hash or request re-ship |
| catalog corruption | restore small catalog backup, verify referenced raw objects, reconcile newer sealed objects |
| disk nearly full | stop repair/compaction/indexing first; reserve headroom for catalog WAL and live raw objects |
| backup lag | durability health degraded; local serving continues; never delete source/old objects based on unverified backup |

### Orphan reconciliation

A bounded L4 scanner is part of the correctness contract, not optional cleanup.
It compares sealed raw/render/media object files with catalog manifests and
classifies:

- recently sealed/unmanifested files: retain for client retry;
- old unmanifested files: inspect and either register from their self-describing
  header or delete after the configured grace period;
- manifests whose files are missing: mark the affected session range degraded
  and request restore/re-ship;
- retired compaction inputs: delete only after manifest swap, grace period, and
  backup confirmation.

Scanner work is resumable and byte-bounded. Orphan count, age, and bytes are
health signals.

## Deletion and Retention

Immutable storage must support deletion without resurrection.

A session deletion transaction through `catalogd`:

1. writes a durable catalog tombstone with a monotonically increasing deletion
   revision;
2. hides/removes live session, runtime, control, and timeline state;
3. advances derived-store deletion revisions;
4. retires the session's raw/render/media manifests;
5. deletes unreferenced local objects after grace and reference checks;
6. propagates a deletion marker to off-host backups according to the hosted
   retention contract.

The session tombstone rejects every old/new envelope identity for that session.
A retry from a Machine Agent whose provider log still exists gets an explicit
`session_deleted` result and cannot recreate the session. Deliberate re-import
requires a new session identity and explicit user action.

Orphan GC never deletes a recently sealed file merely because its catalog commit
is missing. Deletion is not reported complete until derived stores, off-host
objects, and retained catalog snapshots inside the managed retention window no
longer expose the bytes. Old offline/user-controlled backups are outside the
software's enforceable deletion boundary and are described honestly.

## Backup, Restore, and Scrubbing

The database is not reliable until restore is routine.

### Backup contract

One restore point is an exact manifest, not only a highest receipt watermark:

1. ask `catalogd` for an online catalog snapshot;
2. derive the exact raw/media object hashes referenced by that snapshot;
3. upload/verify every required object by content hash;
4. optionally upload current render objects to reduce rebuild time;
5. publish the restore point only after the required object set is complete;
6. record catalog snapshot hash, schema version, `commit_seq`, and object-set
   hash in the restore manifest.

Search/embedding stores are excluded from the critical backup set. Retain enough
previous published restore points to recover operator mistakes, not only file
corruption.

Core/self-host acknowledgement guarantees process-crash durability on the local
filesystem. Hosted operation additionally targets off-host object replication
within five minutes and alerts at fifteen minutes. Zero-loss claims must name
the failure domain; asynchronous off-host backup cannot honestly promise zero
loss after simultaneous host and source-machine destruction.

### Restore contract

A blank Runtime Host restore is:

1. restore the latest published catalog snapshot and required raw/media objects;
2. verify the complete object set from the restore manifest;
3. start `catalogd` and the API/control process;
4. expose timeline and raw export;
5. restore or rebuild render objects;
6. reconcile newer sealed raw objects present locally or on source machines;
7. rebuild search and embeddings in the background.

Hot readiness must not wait for an all-history search rebuild.

### Continuous proof

- sample raw/render/media object hash verification continuously;
- run a full catalog integrity check on a replica/snapshot, not the live writer;
- perform a scheduled restore drill into a disposable Runtime Host;
- verify that random sessions render byte-for-byte and search results resolve;
- record restore point, duration, missing/corrupt objects, and search rebuild
  watermark.

## Observability

Every request and worker result carries one trace identity across machine and
host.

Required measurements:

- provider append to observation, enqueue, send, durable ack, and UI visibility;
- admission wait and rejection by lane;
- request bytes compressed/uncompressed;
- raw/render object encode, compression, fsync, and rename time;
- catalogd RPC wait, writer queue wait, transaction time, restart, and WAL recovery;
- object read, verify, decompress, and first-record time;
- search query and result-resolution time;
- projection lag by projector and oldest revision;
- orphan object count and age;
- catalog/WAL bytes and checkpoint results;
- raw/render/media bytes, file count, packing ratio, and backup watermark;
- repair request rate, byte rate, retry rate, and live-latency effect.

Health is separated into:

- `hot`: `catalogd`, auth, launch, runtime, live ingest;
- `archive`: immutable raw/render/media integrity;
- `search`: lexical index and indexed-through watermark;
- `enrichment`: summaries/embeddings;
- `backup`: latest verified restore point;
- `repair`: mode, lease expiry, backlog bytes, and pressure.

`/readyz` depends only on `hot`. Other failures appear in health and product UI
without lying that the Runtime Host is entirely healthy.

## Proof Harnesses

The architecture is accepted only with destructive tests.

### Ingest crash matrix

Kill the raw worker, render worker, API, and `catalogd` at every boundary:

- before temp write;
- during compression;
- after file fsync;
- after rename;
- during catalog transaction;
- after catalog commit before response;
- after response.

For every point, prove either no acknowledgement or exactly one durable raw
manifest/receipt after retry. Render failure is allowed only when the receipt
explicitly says `render_state=failed|pending` and rebuild succeeds from raw.

### Mixed-load latency test

Run simultaneously:

- live managed-session appends;
- maximum-safe historical repair;
- session detail reads;
- timeline polling/SSE;
- lexical search;
- indexing and compaction.

Gate on the SLO table, including zero cold-read 503s caused by repair.

### Restore test

Restore a published catalog snapshot plus its exact raw/media object set into an
empty data root, boot `catalogd` and the Runtime Host, render sampled sessions,
then rebuild and query search.

### Corruption test

Corrupt one raw object, one search file, and a disposable catalog snapshot. Verify
that each failure is attributed to the correct subsystem and the documented
recovery works.

### Upgrade test

Run old and new Machine Agents against the new Runtime Host during the supported
upgrade window. Protocol version mismatch must be explicit; no silent raw-data
fallback may recreate the monolith.

## Implementation Sequence

### Phase 0: Stop current self-interference

Before the rewrite:

- pause accidental persistent archive drain;
- make drain control expiring and restart-safe;
- reserve cold-read capacity for users;
- remove cold source-line proof from the per-range repair hot loop or route it
  to an existing bounded hot-manifest lookup;
- add retry/backoff to clients of typed archive pressure;
- stop automation health from repeating a transient failure after a later
  successful proof;
- remove retired jobs that can never succeed;
- distinguish summary/enrichment timeouts from archive durability failures.

Gate: maximum repair no longer causes worklog, manifest, or session-detail 503s.

### Phase 1: Freeze the contracts and build the benchmark

- version parser-independent envelope, source identity/epoch, raw-object,
  render-object, manifest/receipt, media, and generation-qualified cursor
  contracts;
- add end-to-end trace identities and the SLO dashboards/reports;
- build the crash matrix and mixed-load harness against the current system;
- capture a baseline from the largest tenant.

Gate: the harness reproduces current failures and produces comparable numbers.

### Phase 2: Establish `catalogd` and raw manifests

- add and supervise the narrow Unix-socket `catalogd` process;
- move all hot catalog reads/writes and migrations behind typed RPC;
- add `source_epochs`, `raw_objects`, media manifests, tombstones,
  `projector_state`, and monotonic `commit_seq`;
- project existing sealed chunk manifests into raw-object manifests in bounded
  batches with explicit legacy provenance;
- expose catalogd-backed batch reconciliation by envelope/source epoch;
- replace one-shot cold readers with a persistent narrow compatibility pool;
- keep the cold monolith read-only for legacy sessions.

Gate: native catalogd kill/restart leaves API/control alive; new duplicate
detection does not query the cold DB; repair causes no user-read 503s.

**Go/no-go checkpoint:** rerun the mixed-load and restore harnesses after this
phase. A credible containment stopping point is now available: catalogd-backed
manifests/receipts and persistent narrow cold readers.
If that smaller system already meets the SLO table, keep it through a soak and
authorize Phases 3–7 with measured remaining restore, maintenance, or latency
cost—not with a rewrite mandate. The final architectural preference remains
monolith deletion because otherwise the 138 GB schema, backups, migrations, and
integrity burden live forever.

### Phase 3: Cut new ingest to v2 raw/render objects

- seal every new parser-independent raw envelope through persistent workers;
- create a separate parser/order-versioned render object when possible;
- commit raw manifest/receipt, optional render generation, one session row, and
  coalesced projector revisions through `catalogd`;
- acknowledge durable raw even when render is explicitly failed/pending;
- stop inserting new transcript bytes into `events`, `source_lines`,
  `session_observations`, or cold archive-manifest rows;
- retain the compatibility reader only for old storage-generation sessions.

Gate: new ingest latency/growth is independent of historical size; all new raw
history and media have coverage; new sessions never open `longhouse.db`.

### Phase 4: Move new-session reads to direct/derived stores

- timeline and metadata: catalogd only;
- session detail: versioned render objects only;
- raw export: raw objects only;
- worklog/day range and search/recall: derived search/message store with explicit
  indexed-through `commit_seq` and no automatic raw fallback;
- source proof: catalogd source-epoch/raw-object manifests;
- compare direct rendering and search against legacy output at identical parser
  and ordering revisions.

Gate: all new-session product and automation routes make zero cold-monolith
connections and pass the mixed-load SLOs.

### Phase 5: Convert legacy history by session

- freeze a legacy high-watermark;
- inventory every source path/opaque identity, replacement epoch/revision,
  offset/range, raw hash, and media reference in a coverage ledger;
- prefer exact `source_lines`; use raw event payloads only as a
  provenance-marked fallback when exact source records do not exist;
- export every uncovered retained raw range from the monolith into v2 raw
  objects before derived rendering;
- convert and verify content-addressed media references;
- generate render objects at a pinned parser/order revision;
- catch up changes after the high-watermark;
- compare transcript, thread, branch, tool pairing, worklog, and search outputs;
- atomically mark the session `storage_generation=v2`, `tombstoned`, or an
  explicit typed degradation through `catalogd`;
- retry/inspect failures without blocking other sessions.

Gate: every retained source epoch and media reference has proven coverage or an
explicit missing classification; no dead/unverified range is hidden.

### Phase 6: Tenant cutover and restore proof

- route every normal endpoint through catalogd, raw/render objects, or derived
  stores according to the read contract;
- run mixed load, kill-point, corruption, deletion, and blank-host restore tests;
- prove the largest tenant and a clean install use the same topology;
- make any accidental v2-to-monolith serving fallback a test failure.

Gate: deleting or corrupting `longhouse.db` has no serving effect.

### Phase 7: Remove the cold monolith serving system

After restore proof and a soak:

- stop mounting `longhouse.db` in the Runtime Host;
- delete archive read proxy/subprocess machinery;
- delete archive `WriteSerializer` and filesystem request/result jobs;
- delete cold event/source-line/observation ORM serving paths;
- delete startup schema convergence and maintenance for the retired schema;
- retain an offline, version-pinned legacy export tool for a bounded support
  window;
- archive the old DB only after backup/restore approval.

Gate: a clean install and the migrated largest tenant run the same target
topology; deleting or corrupting the retired DB has no product effect.

### Phase 8: Pack and tune only after the architecture is stable

- compact small raw/render objects only after measured object-count or
  read-amplification thresholds justify it;
- tune search segmentation based on measured index cost;
- tune compression level and worker counts against CPU/IO evidence;
- add optional object-store archive backend without changing the catalog or
  protocol contracts.

Gate: tuning improves measured latency/throughput without changing correctness
or adding a second product topology.

## Deletion Budget

This rewrite earns its keep only if it removes more machinery than it adds.

Expected deletions include:

- one-shot archive ASGI subprocess proxying;
- global cold-reader semaphore and pressure response path;
- cold archive `WriteSerializer` ownership;
- filesystem archive worker request/running/result protocol;
- event/source-line raw BLOB compatibility columns and readers;
- `session_observations` as a permanent raw duplicate;
- cold startup migrations, FTS verification, WAL maintenance, and DB doctor
  branches for the retired monolith;
- duplicate live session/catalog/timeline tables;
- source-line claim-before-ingest request loop;
- most monolith-specific repair/export code.

The target should have fewer durable states:

1. provider log until acknowledged;
2. sealed immutable raw/media object;
3. catalogd manifest/current state/tombstone;
4. optional render/search/embedding projection.

If implementation adds another authoritative queue, database, or shadow state,
it is moving away from this design.

## Options Rejected

### Stop after containment and keep the cold monolith

A credible smaller alternative implements Phases 0–2, replaces one-shot ASGI
readers with a persistent narrow pool, stops new raw BLOB growth, and keeps the
existing monolith only for legacy reads. This addresses the measured 503 and
proof-loop failures with much less migration work.

It is an explicit checkpoint, not the preferred permanent topology. It leaves a
138 GB mutable database to back up, integrity-check, migrate, checkpoint, and
support indefinitely. If the post-Phase-2 measurements show those costs are
negligible and all SLOs hold, delay the rest. If they remain material, continue
to deletion with evidence.

### Tune the 138 GB SQLite monolith

Indexes, cache size, mmap, checkpoints, `PRAGMA optimize`, and compaction can
improve a healthy shape. They cannot make raw history, hot control state,
repair, search, and enrichment one failure domain without paying that coupling.

### Keep the small catalog embedded in the API

Embedding a much smaller SQLite catalog in the API would remove a local RPC and
is the simplest credible implementation. It does not remove the observed native
SQLite crash from the terminal/control blast radius. The sub-millisecond local
hop and one narrow supervisor boundary are justified here because remote control
must survive a storage-engine process failure. If measurement disproves the RPC
budget, optimize the typed transport before collapsing the failure domains.

### Move hosted tenants to PostgreSQL now

PostgreSQL would improve concurrent mutable writes but would create two core
persistence topologies and leave raw-history duplication and request-path
complexity intact. Reconsider a hosted catalog adapter only when multiple API
replicas or measured catalog write contention require it. Immutable archive and
derived search contracts should not change.

### Seal history into SQLite archive shards

Read-only per-time or per-session SQLite shards would keep familiar SQL and
transactional row storage. They also introduce shard routing, query fan-out,
active-shard mutation, parser/schema migrations inside history, FTS duplication,
and a larger native corruption surface. Immutable raw objects plus disposable
render/search projections have fewer lifecycle states and preserve exact source
bytes without turning parser revisions into database migrations.

### Add Kafka, Redis, or a generic job system

The Machine Agent already retains unacknowledged source ranges. Content-addressed
raw objects, catalogd manifests, and coalesced projector state provide the
required durability. Another queue adds coordination and recovery states without
removing a current source of truth.

### Keep per-request subprocess isolation

It contains native crashes but imposes interpreter startup, application import,
serialization, and single-slot queuing on every read. Persistent narrow workers
over immutable files provide the containment without rebuilding the world.

### Make object storage mandatory

Filesystem objects preserve self-hosting and are fast on one box. An S3-compatible
backend can implement the same immutable object interface for hosted durability
later; it is not a prerequisite for a correct core.

## Final Acceptance

The speed-of-light database is complete when:

- the API/control process is SQLite-free; one supervised `catalogd` owns one
  metadata-only SQLite catalog and no serving path depends on a cold monolith;
- raw transcript and referenced media bytes exist once in verified immutable
  objects with source-epoch coverage;
- parser-versioned render objects are disposable and physically separate from
  raw truth;
- new ingest acknowledgement is crash-safe, idempotent, and p95 under two
  seconds;
- timeline and control stay fast under maximum archive repair;
- session detail and worklog have no global reader slot and no repair-induced
  503s;
- search and enrichment can be deleted and rebuilt without losing history;
- published restore manifests prove an exact catalog snapshot plus raw/media
  object set, and blank-host restore is routine;
- deletion cannot resurrect from Machine Agent retry and is not reported
  complete before managed backup pruning proof;
- the largest migrated tenant behaves like a clean install;
- the old monolith, one-shot archive subprocesses, and duplicate raw rows are
  deleted rather than left as a second supported topology.
