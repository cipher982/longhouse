# Elastic Onboarding and Durable Storage Epic

**Status:** Phase 1 and Phase 2A accepted in production; Phase 2B closeout in
progress; Phase 3 is the next implementation phase
**Owner:** Longhouse core and hosted operations
**Created:** 2026-07-20
**Scope:** Hosted scale, customer import, storage telemetry, and object-store
durability

**Builds on:**

- `speed-of-light-database.md` for the Runtime Host catalog/raw/render/search
  architecture;
- `immutable-source-outbox.md` for exact retry and acknowledgement;
- `speed-of-light-shipper.md` and `transcript-convergence.md` for lane-aware
  Machine Agent scheduling;
- `media-data-plane.md` for content-addressed media;
- `db-load-observability.md` for the existing low-cost host sampler.

This epic does not replace those contracts. It closes the gap between a working
single-tenant storage-v2 runtime and a hosted product that can safely accept an
unknown customer's existing history without guessing how large or fast that
history will be.

For hosted Longhouse, this intentionally brings remote durability evaluation
forward from the optional late tuning in `speed-of-light-database.md`. It does
not change that spec's core/self-host topology or make remote object storage a
prerequisite for progressive onboarding. The first customer-facing import work
ships against today's filesystem storage-v2.

The burst-signup framing is capacity engineering for dogfood, invites, and a
narrow hosted launch. It does not reopen the broad hosted self-serve surface
that `VISION.md` freezes.

## Executive Decision

Longhouse will make immutable raw objects the durable, replayable history for
hosted customers. The Machine Agent will inventory, prioritize, compress, and
upload provider-native history in bounded resumable envelopes. Hosted object
storage will sit behind the existing raw/render/media object seam; the Runtime
Host catalog will record identities, manifests, progress, and product state but
will not duplicate every raw transcript record into SQL.

Telemetry lands first. We will measure the current product and storage-v2 path,
then use the same measurements to qualify every migration step. We will not
choose a shared database, eliminate per-tenant runtimes, or hard-code a tenant
count based on an imagined average customer. Those are later decisions made
from observed bytes, requests, latency, contention, recovery behavior, and cost.

While hosted Longhouse has zero external users, planned availability is not a
migration constraint. Storage or database cutovers default to a declared offline
maintenance window: pause writes and control, take verified recoverable
snapshots, run the migration with full machine capacity, validate the complete
product, switch authority, and resume. We protect data and rollback, not an
unused uptime number. Prolonged dual-write, shadow-serving, tenant-by-tenant
canaries, and online schema choreography are deferred until external customers
make downtime consequential.

The 80/20 launch architecture is:

```text
provider-native logs on customer machines
  -> local discovery and byte inventory
  -> immutable, compressed, resumable envelopes
  -> immutable raw object store (filesystem first; hosted remote when proven)
  -> small tenant catalog (identity, manifests, progress, current facts)
  -> asynchronous render/search/recall projections
  -> timeline, session detail, search, recall

telemetry measures each boundary and separates real-user from synthetic traffic
```

For self-hosting, the same logical object interface remains filesystem-backed
and SQLite remains the only required database. No vendor cloud dependency enters
the public core.

## Product Problem

A first customer is not an empty account. They may install Longhouse after
eleven months of Claude, Codex, Cursor, OpenCode, and Antigravity use across
several machines. Their first connection can reveal gigabytes of source history
while they are also creating new live sessions.

The current hosted system can ingest and serve this corpus, but it still makes
launch-risking assumptions:

- hosted capacity is provisioned and enlarged manually;
- tenant history, derived search, and object data compete for one host's disk;
- onboarding has no explicit inventory, prioritization, progress, or completion
  contract;
- current telemetry is strong for ingest and host pressure but weak for
  timeline, detail, cold reads, search, recall, and browser paint;
- browser render evidence is not durably retained under storage-v2;
- scheduled probes do not continuously exercise a recent session, an old
  session, deep pagination, search, and recall against a real corpus;
- cost per stored byte, indexed byte, object operation, and customer import is
  not measured as a product signal;
- a sudden archive backlog can be controlled, but a burst of independent new
  customer imports is not modeled as a first-class workload.

The failure is not “SQLite cannot scale.” The failure would be coupling raw
durability, parsing, indexing, query serving, and account provisioning so tightly
that one large import or one broken projection can make the product unavailable.

## Starting Point

This plan starts from implemented storage-v2, not a blank-slate diagram:

- the Runtime Host has a live catalog boundary plus immutable raw, render, and
  media object paths;
- the Machine Agent has a frozen pending-envelope outbox and exact receipt
  protocol;
- search is already a separate derived store;
- hosted tenants are isolated today primarily by per-tenant runtime/container
  and filesystem roots;
- the Zerg host snapshots application and resource telemetry every two minutes,
  but read-path, projector, browser, and onboarding coverage is incomplete;
- legacy archive/cold-store paths and incomplete restore proofs still exist and
  must be named in the Phase 1 baseline;
- no remote object backend is currently part of production authority.

Phase 1 must verify this checkpoint against the deployed build before treating
any stale spec status as operational truth.

## User Outcomes

### A new hosted customer

1. Installs Longhouse and connects one or more machines.
2. Sees which providers and approximate history Longhouse discovered before
   bulk upload begins.
3. Gets recent and active sessions into the timeline first.
4. Can continue normal live work while older history converges in the
   background.
5. Sees byte-based progress, current rate, paused/backpressured state, and a
   useful completion estimate when enough evidence exists.
6. Can close the laptop, reconnect, or restart without beginning the import
   again.
7. Can open, search, recall, export, and ultimately delete imported history.

### An existing customer

- Live append and session control remain responsive during another customer's
  onboarding or during their own historical repair.
- Recently used and old sessions have measured, bounded loading behavior.
- Projection lag is described honestly; durable raw history is not confused
  with search or render readiness.

### The operator

- Can see which lane, tenant, object class, projector, or request surface is
  consuming resources.
- Can slow or pause historical imports without stopping live traffic.
- Can recover or replay derived state from raw objects without asking the
  customer to reship source history.
- Has cost and capacity signals that lead a scaling decision before a disk or
  quota emergency.

## First-Principles Model

### What SQL is for

SQL is appropriate for small mutable facts and indexes that require atomic
coordination or flexible queries:

- tenant, machine, session, source-epoch, and object identities;
- immutable-object manifests and durable receipts;
- current runtime/control facts;
- upload/import state and projector progress;
- authorization, deletion tombstones, and restore points;
- derived lexical search when SQLite FTS remains sufficient.

SQL is not required to be the first or only durable representation of every
provider record. Raw transcript bodies are append-heavy, replayable, naturally
chunked, and rarely updated. Immutable object storage fits those properties
better and prevents the query database from becoming the sole recovery source.

### What object storage is for

Object storage holds immutable, checksummed, compressed bytes:

- parser-independent raw envelopes;
- versioned render objects;
- content-addressed media;
- optionally sealed search/index segments and backup artifacts later.

An object is not acknowledged merely because an upload request returned. The
catalog receipt is issued only when the system can prove the expected identity,
size, checksum, tenant namespace, and durable object existence.

### What remains local

The frozen immutable outbox intent is the retry authority for a prepared range.
The provider source is authoritative only for ranges not yet prepared; it is
never reread to reconstruct a network retry after the source may have changed.
The Machine Agent stores cursors, one bounded pending envelope per source epoch,
checksums, and progress until receipt. This is a necessary bounded copy, not a
second unbounded archive.

### What is derived

Render objects, search, recall, embeddings, summaries, worklogs, and analytics
are disposable projections. Their failure can reduce capability, but it cannot
invalidate raw durability, live control, or the session identity catalog.

## Non-Negotiable Invariants

1. **No acknowledged-byte loss inside the named failure domain.** A receipt
   survives API, worker, container, and ordinary host restart with zero-byte
   loss. Before remote authority, simultaneous source-machine and Runtime Host
   destruction is governed by the hosted backup RPO: five-minute target and
   fifteen-minute alert boundary. After remote authority, raw objects follow the
   selected backend's declared durability policy while catalog/tombstone state
   follows its separately tested backup RPO.
2. **No customer reship for server mistakes.** A corrupt or deleted derived
   store is rebuilt from raw objects. The customer machine is not the backup.
3. **Recent/live traffic wins.** Historical onboarding and repair cannot consume
   capacity reserved for control, presence, live transcript, timeline, and
   detail reads.
4. **Raw acknowledgement is parser-independent.** Parse, render, search, recall,
   and enrichment failures do not cause already durable raw bytes to be resent.
5. **Exact retry is ordinary.** Object and envelope identities make ambiguous
   responses, reconnects, and restarts idempotent.
6. **No advertised-session fiction.** A visible session is readable, explicitly
   `syncing`, or explicitly degraded with recoverable evidence; it is not an
   empty or 404 placeholder.
7. **Tenant authorization applies at every boundary.** Object names, upload
   intents, manifests, reads, logs, and metrics cannot turn a hash or URL into a
   cross-tenant bearer token.
8. **Deletion is end-to-end.** Tenant/session deletion reaches catalog,
   raw/render/media objects, derived indexes, replicas, and documented backup
   expiration without leaving an invisible serving copy.
9. **Self-host remains simple.** Filesystem objects plus SQLite implement the
   same logical contract without requiring S3, Postgres, Kafka, Redis, or a
   hosted control plane.
10. **Pressure is measured, not guessed.** Concurrency and byte budgets adapt to
    observed latency, throughput, errors, queue depth, and storage headroom.
11. **Costs are attributable.** Stored bytes, object requests, egress, indexing,
    and import compute can be attributed at least per tenant and workload lane.
12. **No hidden fallback.** A degraded search or object store reports its actual
    state; it never silently queries a legacy monolith or customer source.
13. **Prelaunch cutovers optimize for completion, not availability.** While
    there are zero external users, planned maintenance may stop ingest, control,
    reads, and projectors. Data protection, deterministic validation, and
    rollback remain mandatory; live coexistence machinery does not.

## Target Architecture

### Machine Agent

The Machine Agent owns local provider discovery and upload scheduling:

- discover supported provider sources using provider-specific adapters;
- assign stable source identity and epoch before upload;
- inventory bytes, records when cheaply knowable, modification time, and likely
  session association without fully parsing the corpus;
- preserve pending immutable envelopes across restart;
- compress and checksum bounded envelopes using the existing storage-v2
  contract;
- prioritize live append, current-session gaps, recent history, then older
  history with aging so large sources cannot starve forever;
- adapt concurrency and batch size to server admission, observed throughput,
  battery/network policy, and customer controls;
- never upload four provider archives with independent unbounded concurrency.

The discovery result is metadata, not a promise that every byte is immediately
uploaded. A machine may report 80 GB discovered while only recent sessions are
scheduled initially.

### Runtime Host

The Runtime Host remains the product authority for one logical tenant. It:

- authenticates machines and scopes every upload intent;
- reserves live-lane capacity before admitting historical work;
- verifies/finalizes object uploads and commits catalog receipts;
- exposes import state and typed backpressure;
- serves timeline metadata from the catalog;
- serves detail from render objects, with raw export as a distinct path;
- schedules or reports projection state without making projections part of raw
  acknowledgement.

The launch path proxies bounded storage-v2 envelopes through the Runtime Host's
existing object worker. This keeps one upload/verification/receipt topology for
hosted and self-hosted use. Direct object-store upload is a later optimization
only if measured API ingress cost justifies its extra verification, security,
multipart cleanup, and lost-finalize machinery. Both paths, if the second is
ever added, must finish through the same catalog receipt contract.

### Object store

The object-store interface must support:

- tenant-scoped immutable put/finalize/read/delete;
- conditional create or equivalent exact-retry behavior;
- checksum and size verification independent of client claims;
- bounded range or whole-object reads needed by detail/export;
- lifecycle/versioning policy that does not silently delete live manifests;
- inventory and restore verification;
- backend-neutral tests runnable against filesystem and at least one
  S3-compatible implementation.

Hosted objects live in an authorization-bound tenant namespace such as
`tenants/{opaque_tenant_key}/{raw|render|media}/v2/...`, or an equivalently
isolated per-tenant bucket. The content hash may select an object inside that
namespace but is never authorization. Finalization proves the authenticated
tenant owns the expected envelope and object location. Media remains
tenant-scoped for the same deletion and information-leak reasons as raw data.

The provider is deliberately undecided in this epic. Selection is a measured
Phase 3 decision using durability, request semantics, optional direct-upload support,
latency from the Runtime Host and customer regions, egress, operational limits,
cost, and exit/migration mechanics. Provider marketing is not evidence.

### Catalog and databases

The current storage-v2 catalog remains the initial tenant catalog. Transcript
bodies do not move back into it. Search remains a separate rebuildable SQLite
store until measured query/index contention or fleet operations justify another
engine.

For hosted durability, catalog snapshots and deletion/tombstone state are
co-equal with raw objects. A raw-only restore that loses accepted ranges,
identity links, or tombstones is not successful. Tombstones need an off-host,
replay-safe representation so reconstructing catalog state cannot resurrect
deleted sessions.

Hosted account, billing, provisioning, and fleet scheduling remain control-plane
concerns outside public core. The epic does not require a shared Postgres data
plane. If a later phase evaluates shared catalog infrastructure, it must preserve
the public machine contract and the ability to run one tenant with SQLite.

### Projectors

Projectors consume committed raw manifests and produce versioned render/search/
recall outputs. Every projector exposes:

- desired and completed revision or commit sequence;
- pending objects/bytes and oldest pending age;
- current throughput;
- last success and typed last failure;
- rebuild generation and source object-set hash.

Projector scheduling is coalesced and lane-aware. Repeated source appends update
desired state rather than creating an unbounded job row for every event.

## Onboarding State Machine

```text
not_connected
  -> discovering
  -> inventory_ready
  -> importing_recent
  -> importing_history
  -> current

any importing state -> paused | backpressured | blocked_source | offline
blocked_source -> retrying | quarantined_with_evidence
offline -> prior durable state plus local retry on reconnect
```

These are product states, not inferred UI labels. Each transition has a durable
reason and timestamp. `usable=true` is an orthogonal derived predicate: it
becomes true when at least one useful recent session is durably readable and
stays true while older history imports, pauses, or reconnects.

### Time-to-value ordering

The default scheduler is priority-based, not provider-parallel FIFO:

1. control, presence, and live transcript append;
2. gaps in a session the user is currently viewing or controlling;
3. active and recently modified sessions across all providers;
4. recent closed sessions, interleaved across providers and machines;
5. old history, with aging and bounded large-source slicing;
6. optional enrichment and broad rebuilds.

The exact recent/old boundary is a policy input informed by inventory and
observed cost, not a permanent scalar in code. A user can explicitly request a
session or range, which raises its priority without bypassing safety limits.

### Adaptive admission

Admission responds to measurements:

- live/detail p95 and error rate;
- object put/finalize latency and throttling;
- catalog commit latency and queue wait;
- projector backlog and object-store read amplification;
- available local spool and hosted storage headroom;
- per-tenant and fleet byte/request rates;
- client network and power policy.

The controller increases historical concurrency cautiously while headroom is
healthy and backs off with jitter when user-facing latency, errors, or provider
throttling rise. Explicit product quota or operator pause remains authoritative.
There is no hard-coded “ten tenants per host” scaling rule.

## Telemetry Contract

Telemetry is Phase 1 because every later architecture claim must be observable
before and after cutover. The smallest useful implementation extends the
existing Prometheus endpoint and two-minute NDJSON sampler; it does not begin
with a new observability platform.

### Real-user request and browser signals

Record low-cardinality histograms/counters for:

- timeline request to first row;
- session navigation to first transcript paint;
- session detail first byte and first page;
- previous/next page append;
- catalog lookup;
- render manifest lookup, queue wait, object read, decompress, verify, and bytes;
- search and recall latency, outcome, result count bucket, and indexed-through
  lag;
- raw export latency and bytes;
- HTTP route class, status family, and timeout/error class.

The new journey-beacon contract is distinct from the existing legacy
`client-render` payload, which includes a session identifier. New beacons
contain route class, coarse session-age bucket, navigation source, build
identity, duration, and outcome. They never contain transcript content, query
text, source paths, session UUIDs, object keys, or other high-cardinality
identifiers. Synthetic and real-user samples are distinct. Phase 1 proves the
contract through the synthetic browser; general real-user emission follows in
Phase 2.

### Onboarding and storage signals

- discovered source/session/byte totals by provider;
- scheduled, pending, acknowledged, blocked, and quarantined bytes;
- newest and oldest unacknowledged source age;
- time to inventory, first durable session, first timeline value, recent-set
  completion, and full convergence;
- object put/finalize/read/delete latency and outcomes;
- stored logical/compressed bytes by raw, render, media, search, and backup;
- dedupe/compression ratio without cross-tenant content disclosure;
- catalog commit latency, WAL/checkpoint pressure, and file size;
- projector pending bytes/objects, revision lag, failures, and throughput;
- per-tenant request/byte/compute cost inputs and fleet totals;
- host disk, cgroup memory/OOM, CPU, and I/O.

### Synthetic journey

An authenticated scheduled probe exercises a real non-demo corpus without
changing user data:

1. load the timeline;
2. open the newest active/recent session;
3. open a recent closed session;
4. open a cold session older than 30 days;
5. append an older projection page;
6. open one bounded randomly sampled readable session for coverage;
7. run a stable lexical search fixture;
8. run a stable recall fixture;
9. record server timing, browser first paint, result/non-empty state, build
   identity, and synthetic cohort.

Stable fixtures provide comparable trends; the random sample finds corpus
outliers. A failed query records typed evidence and never uploads transcript or
search content as an artifact.

### Retention and use

Phase 1 keeps the existing cheap host snapshots and adds a compact journey
result artifact. Retain at least 30 days of comparable results outside the
Runtime Host process. We add a metrics backend only when querying NDJSON and CI
artifacts becomes the bottleneck or alerting needs require it.

## Service Levels and Decision Gates

Initial product budgets inherit `speed-of-light-database.md` and become
measurable in Phase 1:

| Surface | Initial target | Hard boundary |
| --- | ---: | ---: |
| timeline first page external p95 | 300 ms | 1 s |
| session detail first page external p95 | 500 ms | 2 s |
| cold session first page external p95 | 1 s | 5 s |
| recent lexical search external p95 | 500 ms | 2 s |
| all-history lexical search external p95 | 1 s | 5 s |
| append to durable acknowledgement p95 | 2 s | 10 s |
| acknowledgement to canonical UI visibility p95 | 250 ms | 1 s |
| acknowledged loss after process/container/ordinary host restart | 0 bytes | 0 bytes |
| pre-remote-authority off-host disaster RPO | 5 min | 15 min alert |
| post-remote-authority raw-object loss | backend durability policy | restore failure |

Cold/detail/search targets are evaluated under controlled background onboarding,
not on an idle host. We do not declare a target achieved until enough samples
exist to show a distribution and its error rate.

These service levels apply before and after a cutover, not during an explicitly
declared prelaunch maintenance window. Migration throughput may use the full
host while the product is paused. Reopening requires the post-cutover checks to
pass; keeping the product responsive while bytes are being transformed is not a
gate until external users exist.

Architecture changes require evidence:

- **Choose remote object storage:** backend passes checksum/idempotency/restore
  contracts and mixed-load results; measured cost fits the hosted product model.
- **Direct-upload optimization:** only after proxied ingress is measured as a
  material bottleneck; short-lived upload intents cannot escape tenant,
  size, checksum, or object-prefix scope; exact retry and lost-finalize recovery
  pass crash tests.
- **Replace SQLite search:** only if measured index/query contention violates
  product SLOs after isolation and ordinary tuning.
- **Shared catalog database:** only if per-tenant catalog operations or fleet
  management, not raw bytes, are the measured bottleneck.
- **Remove per-tenant containers:** only with proven resource savings plus
  equivalent fault, auth, noisy-neighbor, backup, and rollback isolation.

## Failure Model

| Failure | Required behavior |
| --- | --- |
| Customer disconnects mid-upload | Persist exact pending intent; retry the same envelope after reconnect. |
| Upload succeeds but finalize response is lost | Exact retry discovers/verifies the object and returns the original receipt. |
| Object store is slow or throttles | Preserve live capacity, reduce historical admission, expose backpressure and retry time. |
| Object store is unavailable | Do not acknowledge new raw durability; keep bounded local retry state; existing cached/catalog surfaces degrade honestly. |
| Catalog commit fails after object put | Leave an attributable orphan; deterministic retry or reconciler commits the same manifest. |
| Catalog is unavailable | Do not issue a false receipt; object bytes remain reconcilable by identity. |
| Parser/render fails | Raw stays durable; detail reports projection failure/syncing; retry from raw. |
| Search/recall fails | Timeline, control, raw durability, and direct detail remain available. |
| One customer uploads a huge corpus | Fair byte/request admission prevents starvation; live and other tenants retain reservations. |
| Several customers onboard together | Fleet controller admits work from measured headroom; queues remain byte-bounded and visible. |
| Client source changes during import | Stable source epoch/ranges preserve uploaded identity; new bytes enter a successor/current range without rewriting acknowledged data. |
| Object is corrupt or missing | Checksum scrub marks explicit degradation; restore from replica/backup or report loss—never silently request a derived rebuild as raw recovery. |
| Runtime version rolls back | Catalog/object protocol compatibility gate rejects unsupported writes; immutable raw objects remain readable/exportable. |
| Tenant deletion races projection | Tombstone/revision prevents new derived outputs and drives object/index cleanup idempotently. |
| Metrics pipeline fails | Product continues; telemetry health reports the gap so missing evidence is not interpreted as success. |
| Maintenance begins with in-flight writes | Stop admission, wait for or reject bounded in-flight work, flush durable state, and only then snapshot. Machine Agents retain unacknowledged envelopes. |
| Offline migration or validation fails | Keep production paused; preserve failed output for diagnosis; restore the verified pre-cutover snapshot or rerun from it. |

## Security and Privacy

- Object authorization is tenant/session scoped; content hashes and presigned
  URLs are not permanent authorization.
- Upload intents are short-lived, single-purpose, size/checksum bound, and
  useless outside their tenant prefix.
- Encryption in transit and provider-managed encryption at rest are minimums;
  customer-managed keys are a later product decision.
- Logs and metrics exclude transcript content, search/recall query text, local
  source paths, and credentials.
- Object inventories and backups have the same access boundary as live data.
- Data export and deletion behavior is tested against raw, render, media,
  search, replicas, and backup expiration.
- Logical deletion makes data inaccessible immediately. Physical purge has a
  declared SLA covering live objects, noncurrent versions, delete markers,
  abandoned multipart uploads, replicas, and backup expiration; “delete marker
  written” alone is not completion.
- Cross-tenant content dedupe is forbidden initially because it complicates
  authorization, deletion, and information leakage. Compression and tenant-local
  dedupe provide the safe early savings.

## Cost and Capacity Model

We will not estimate one average customer and multiply by a hard-coded host
count. The model records real units:

```text
tenant durable cost
  = compressed raw GB-month
  + render/media/index GB-month
  + object PUT/GET/list/delete operations
  + restore/backup copies
  + network egress
  + parsing/indexing CPU time
  + catalog/runtime baseline
```

Phase 1 establishes the application and host baseline. Phase 3 benchmarks
candidate object backends with representative small/recent and large/historical
objects. Onboarding adds per-tenant inventory and import cost evidence before
pricing or quotas are finalized.

Capacity warnings are based on projected exhaustion from current bytes/rates and
provider quota, not a fixed tenant scalar. Hard safety ceilings still exist for
security, billing exposure, and disk exhaustion, but they are explicit product/
operator controls, configurable, and visible to the admission controller.

## Migration Strategy

### Prelaunch default: offline replacement

Before every destructive or authority-changing operation, verify that hosted
still has zero external users. If that fact changes, stop and redesign the
cutover around customer communication and bounded availability. While it remains
true, use this sequence:

1. **Prove the target offline.** Contract-test the destination, migrate a
   disposable copy, restore it independently, and run timeline/detail/export/
   search/recall checks before touching production.
2. **Capture a short baseline.** Record the deployed build, storage inventory,
   object/session counts, representative warm/cold/read timings, and outstanding
   worker/projector state. This is a comparison point, not a multi-day soak.
3. **Declare maintenance and stop mutation.** Put hosted Longhouse in an
   explicit maintenance state; stop new ingest and control admission, archive
   repair, projectors, indexers, and background cleanup. Drain or reject bounded
   in-flight work and flush durable state. Machine Agents keep unacknowledged
   frozen envelopes for retry after resume.
4. **Take verified recovery snapshots.** Snapshot catalogs, tombstones,
   filesystem objects, configuration, and build identity. Record manifests and
   checksums, copy the recovery set outside the migration target, and prove that
   the snapshot can be opened or sampled before proceeding.
5. **Run the migration at full capacity.** Bulk copy, transform, pack, rebuild,
   or replace without live-traffic throttles. The migration reads only the
   frozen snapshot/source generation and writes a new destination generation;
   it never mutates the sole recovery copy in place.
6. **Validate before authority changes.** Compare manifests, counts, checksums,
   source ranges, sessions, tombstones, and media. Exercise newest, recent, cold,
   paginated, raw export, search, recall, rebuild, and restart paths against the
   candidate destination.
7. **Switch once.** Change configuration/authority to the new store, start a
   clean Runtime Host, and repeat the critical product checks. Do not maintain a
   long-lived old/new serving split.
8. **Resume and reconcile.** Re-enable Machine Agents and admission. Their
   durable outboxes retry everything not acknowledged before maintenance;
   exact-retry receipts prevent duplication. Observe post-cutover telemetry and
   reconcile any attributable orphan or projection lag.
9. **Rollback cleanly if a gate fails.** Pause again, restore the verified
   snapshot and prior build/configuration, then resume. Never patch the old and
   new authorities concurrently to make a failed cutover look green.

This is intentionally a rip-the-bandage-off migration. The rollback unit is the
complete frozen production generation, not a complicated record-by-record
reverse migrator.

Remote backup/mirroring plus tested restore is an explicit launch-quality
stopping point. Remote authority cutover proceeds only when its measured
operational or economic benefit exceeds the additional migration risk.

Legacy cold-monolith retirement remains owned by `speed-of-light-database.md`.
This epic must not reintroduce it as an object-store migration fallback.

## Execution Contract

This epic is executed as a sequence of independently accepted product phases,
not as a stream of individually deployed fixes. The objective is to minimize
wall-clock critical path without weakening data-authority gates.

### Scheduling rules

- Keep one implementation owner and one coherent write stream. Use Hatch for
  independent read-only design, threat-model, and final-diff review; do not
  create competing authors in the same worktree.
- Group code by one phase acceptance invariant. Small fixes within that slice
  ship together unless the currently deployed build can lose data, cross a
  tenant boundary, corrupt authority, or break live control.
- Run formatting, affected unit tests, and the smallest relevant integration
  proof while iterating. Run one broad repository gate after the phase diff is
  stable, not after every correction.
- Run the final targeted tests, independent review, acceptance-query
  preparation, and rollback/runbook preparation in parallel when they do not
  contend for the same local compiler or files.
- Treat the required exact-SHA hosted CI/deploy gate as the broad gate when it
  duplicates local CI. Authority-changing cutovers additionally require one
  complete local gate and offline restore/rollback proof.
- While remote CI or deployment runs, prepare the next phase in a separate
  worktree or perform read-only research. Do not dirty the exact candidate SHA.
- Update this ledger and detailed acceptance evidence once per accepted phase.
  Do not spend a deployment cycle only to update status prose.

### Blocking rule

Work discovered during a phase blocks advancement only when it threatens an
explicit phase gate or one of these boundaries:

- acknowledged bytes, exact retry, or cursor monotonicity;
- tenant authorization, deletion, or object namespace isolation;
- live transcript/control capacity reserved from historical work;
- deterministic restore, migration, or rollback;
- truthful user/operator state required to recover the system.

Cosmetic telemetry, log wording, speculative scale tuning, general cleanup,
and fleet redesign are recorded for their owning later phase. They do not
silently expand the current phase.

### Current phase ledger

| Phase | State | Next exact gate |
| --- | --- | --- |
| 1 — telemetry/baseline | accepted | Retention continues without blocking later phases. |
| 2A — source inventory | accepted | None. |
| 2B — progressive import/restart safety | closeout | Deploy the legacy cursor seal, then prove two consecutive restarts create no acknowledged-work replay and no durable cursor/epoch regression. |
| 3 — remote backup/provider decision | not started | Contract, benchmark, authorization, deletion, catalog backup, and disposable restore proof behind the existing object seam. |
| 4 — optional remote authority | gated by Phase 3 evidence | Proceed only if remote authority beats tested backup/mirroring for launch. |
| 5 — projection/cold-read economics | not started | Destructive render/search/recall rebuild from raw truth plus current/cold read proof. |
| 6A — dogfood migration | not started | Offline snapshot, cutover, complete product validation, and exercised rollback. |
| 6B — signup burst | not started | Multi-tenant fairness, admission, spending, and pause/resume proof. |
| 7 — fleet simplification decision | deferred | Keep or change containers/SQLite from measured evidence; a rewrite is not presumed. |

## Delivery Plan

### Phase 1 — Telemetry foundation and baseline

**Goal:** Establish the minimum measurements and hard safety controls needed to
change storage or accept a large import safely.

**Implementation checkpoint (2026-07-20):** Slices A–D are implemented on the
epic branch and are not yet deployed. Slice A adds bounded route-class request
outcomes and latency, independently timed read stages, immutable-object
bytes/counts, and exact commit/dirty build identity. Slices B–C add O(1)
transactional raw/render/media and search-projector accounting, an async cached
telemetry snapshot with explicit health/freshness, retained host-disk signals,
isolated configurable storage worker lanes, and disk/byte/stored-cap admission
for reconstructable historical work only. Exact envelope and media retries
bypass ceilings; live storage and legacy live ingest never consume historical
budgets. Unknown recall and oldest-lag evidence cannot appear green.

Slice D adds one fail-closed scheduled journey through the real non-demo
dogfood tenant using an owner-bound device credential. It exercises timeline,
active/recent, recent closed, cold, older-projection append, bounded random,
stable lexical, and stable recall reads. It records exact build identity,
route-class aggregates, typed outcomes, ready time, and Chromium Element Timing
first paint. A real-browser contract test covers both buffered navigation paint
and paint after an append boundary. The retained 30-day artifact rejects UUIDs,
queries, paths, object keys, and fixture values; traces, screenshots, video,
Playwright errors, and raw output are excluded from retained artifacts and CI
logs.

Live pre-deploy probing also found and fixed two blockers that the journey would
otherwise expose: storage-v2 lexical hits were hydrated without the required
owner scope, and a tenant with 1,000 historical revoked device credentials
could no longer create a new credential. Creation now counts active credentials
for admission and transactionally prunes only the oldest revoked history to a
bounded total before inserting. The lite-test harness now disables ambient host
disk watermarks by default so a nearly full developer or CI filesystem cannot
silently turn unrelated archive contract tests red; dedicated admission tests
still set thresholds and mock disk usage explicitly.

Cursor/Grok rejected earlier revisions for misleading read measurements,
periodic full-table telemetry scans, incomplete media retry behavior, inferred
recall health, missing live-lane proofs, and a misplaced lifecycle cancellation.
The first Slice D review additionally rejected a request-animation-frame proxy
for first paint and unsafe default Playwright failure logging; the second found
that Chromium Element Timing requires a buffered `PerformanceObserver` rather
than `performance.getEntriesByType()`. The amended implementation maintains
counters with reconciled SQLite triggers, never refreshes telemetry from a
request, owns the refresh task through startup and shutdown, measures actual
element paint, and has direct failure/isolation/privacy/browser tests.
Deployment, the first retained live journey, and the baseline report were
completed at the production acceptance checkpoint below.

**Production acceptance checkpoint (2026-07-21):** Phase 1 shipped through
commits `f0a8ff026` and `70916f3ec` and passed the full backend gate (3,872
passed, 13 skipped), exact-SHA deployment verification, and the automatically
dispatched [Hosted Live QA run](https://github.com/cipher982/longhouse/actions/runs/29803744493).
The always-on canary now creates and replays its durable session through
storage-v2, fails closed on a noncanonical receipt, and is discovered through
the live catalog without a legacy-database fallback.

That production run negotiated storage-v2 with cutover enabled, received all
four repair receipts, measured repair p50/p95 of 309.7/369.8 ms and live p50/p95
of 265.0/284.8 ms, and passed the live SLA. The real browser opened the canary
session, captured three independent SSE-to-paint samples, and measured paint
p50/p95 of 4.3/9.5 ms. The privacy-safe retained journey passed timeline,
active/recent, recent closed, cold, random, older projection, lexical search,
and recall cohorts against exact build `70916f3ec`. Host inventory, remaining
signal gaps, and private operational paths are recorded outside this public
repository. Phase 1 is accepted; longer retention accrues in parallel and does
not block Phase 2.

Deliver:

- route-class request latency/error histograms;
- storage read stage timings and byte/object counts;
- cheap projector/object/search/recall lag and failure gauges from current
  state;
- repaired host resource sampler and explicit telemetry-health signal;
- one authenticated scheduled active/recent/cold/random/search/recall synthetic
  journey against the dogfood tenant through normal tenant authorization;
- browser first-paint timings captured by that probe, without first building a
  general real-user beacon pipeline;
- an operator-configured global historical-ingest byte budget, per-tenant
  stored-byte ceiling, live-lane reservation, and disk watermark that pauses
  historical work with typed backpressure;
- build identity on all new signals;
- one baseline report describing distributions, gaps, current storage footprint,
  object counts, current authority/dual-path leftovers, and unreliable signals.

Acceptance:

- metrics survive Runtime Host restart through the existing external sampler;
- no high-cardinality session/object/user labels enter Prometheus;
- real-user and synthetic samples are separable;
- the probe continuously exercises all named read cohorts and emits compact
  results;
- a deliberate endpoint failure and telemetry failure both appear as failures,
  not missing/green data;
- historical ceilings pause only historical work and expose their exact reason;
- Phase 2 receives a measured baseline, and Phase 3 receives a backend benchmark
  workload.

Phase 1 is an instrumentation and control implementation, not a long observation
program. Capture enough representative dogfood samples to verify the signals and
establish a before-cutover comparison; accumulating 30 days of retention happens
in parallel and does not block Phase 2 or an offline migration.

Not in Phase 1: object-store integration, database replacement, per-tenant
container changes, pricing/plan quotas, customer-facing onboarding UI, or a
general real-user browser beacon pipeline.

### Phase 2 — Customer inventory and progressive import

**Goal:** Turn first install with months of multi-provider history into a
controlled, useful flow on the current filesystem storage-v2 backend.

**Phase 2A production acceptance checkpoint (2026-07-21):** Durable source
inventory shipped through commits `efcff5a36`, `498e06c42`, `7739c6bde`,
`4e6e10a37`, and `c269021d3`. The Machine Agent now inventories provider-native
sources independently of whether historical import is paused or live work is
busy, stores a generation and logical content digest across restart, and sends
only provider-level counts, source bytes, SQLite WAL bytes, footprint bytes,
time bounds, scan duration, and error count. Paths and filenames never enter
the heartbeat or API projection. SQLite inventories count the database plus WAL
and exclude SHM; WAL churn does not create a new logical generation.

Production dogfood discovered 9,947 sources across Antigravity, Claude, Codex,
Cursor, and OpenCode in 724 ms: 24,082,968,586 source bytes plus 66,593,864 WAL
bytes, for a 24,149,562,450-byte footprint. The scan completed with zero errors
while live control remained connected and a 245-range, roughly 7 GB archive
backlog remained bounded behind the historical lane.

Live acceptance also exposed two cross-boundary defects that tests alone had
not represented. The catalog heartbeat validator still expected fields removed
from the typed lease wire contract, so the new heartbeat was rejected; the
contract is now derived from the current schema and heartbeat ingestion is 204.
The API process correctly does not own a live SQLite read connection, so machine
health now reads an owner-scoped, latest-row, privacy-allowlisted projection
through catalogd rather than opening the catalog directly. Its raw projection
is fail-closed and capped at 32 KiB per machine, leaving deterministic headroom
under the 8 MiB RPC frame for 100 machines even with outer JSON escaping.

The full backend gate passed with 3,876 tests and 13 skips. Exact build
`c269021d3` deployed successfully, and an owner-bound production request returned
HTTP 200 with the complete inventory above, healthy transport state, five
provider aggregates, and no source-path values. Phase 2A is accepted. Phase 2B
owns provider-fair recent-first scheduling, durable progress receipts, pause and
resume, and restart recovery.

**Phase 2B closeout checkpoint (2026-07-21):** Commits `4e1af6171`,
`a7e96abd6`, `fa17aac90`, `57891b40a`, `9e47484d5`, `5b53f7743`, and
`5802cef49` are deployed. They add provider-fair backlog scheduling with live
reservation, truthful byte progress, durable reconciliation completion, Cursor
capture high-water retention, SQLite watcher-loop suppression, macOS file
identity that survives reboot, and immediate pause-aware rescheduling of the
immutable storage-v2 outbox after restart. Exact dogfood build `5802cef49` is
clean; the storage-v2 outbox, blocked-source count, active spool, and archive
backlog are all zero.

Live restart testing exposed one remaining Phase 2 gate failure: 245 obsolete
v1 `file_state` gaps representing 7,469,578,965 bytes are recreated on each
process start even though storage-v2 proves those sources current and retires
the transient spool rows. The reviewed local closeout changes only the legacy
acked watermark after storage-v2 head proof; it does not advance the
authoritative source-epoch durable cursor. Phase 2B is accepted only after that
change is deployed and two consecutive restarts prove zero regenerated gaps,
zero blocked envelopes, no durable-position regression, and no false source
replacement. No other cleanup may delay Phase 3.

Deliver discovery inventory, the onboarding state machine, recent-first
scheduler, provider fairness, byte-based progress/health surfaces, thin
customer/operator pause and resume, blocked-source evidence, and restart/
reconnect recovery. Add privacy-safe real-user journey beacons only after the
synthetic event contract is stable.

Gate: a multi-provider large-corpus fixture becomes useful before full history
completes, live traffic remains within SLO, safety ceilings work, and no restart
repeats acknowledged work.

### Phase 3 — Object-store contract, provider decision, and remote backup

**Goal:** Prove one hosted backend behind the existing object seam without
changing acknowledgement authority.

Deliver filesystem/S3-compatible contract tests, candidate benchmarks, cost
model, tenant namespace/auth tests, mirror adapter, inventory, scrub, deletion,
catalog/tombstone backup, and disposable restore proof. Benchmark envelope-sized
objects against packing alternatives using storage cost, request cost, restore/
scrub duration, and deletion time; record a packing decision even when the
decision is to keep envelopes unpacked. Versioned backends must prove
noncurrent-version expiration within the declared deletion policy.

Gate: select a provider only after the same workload and failure matrix run
against each serious candidate. Remote objects plus catalog backup restore a
disposable tenant without resurrecting deleted sessions. Record the decision,
rejected alternatives, deletion policy, object-count threshold, and whether
backup/mirroring is sufficient for launch.

### Phase 4 — Optional remote raw authority

**Goal:** If Phase 3 evidence justifies it, make remote immutable raw objects the
authority for new hosted envelopes.

Deliver the proxied upload/finalize/receipt path, exact retry, orphan
reconciliation, lane-aware admission, the offline replacement playbook,
catalog/tombstone backup, destination validation, and complete-generation
rollback proof. Direct upload remains a later measured optimization, not the
launch path. Do not build transitional dual durability or shadow serving while
there are zero external users.

Gate: acknowledged bytes survive crash and off-host restore inside the declared
failure domains; catalog loss cannot resurrect tombstoned data; rollback retains
every acknowledged envelope; and live/detail SLOs hold after the product
resumes. No SLO applies during the declared cutover window.

### Phase 5 — Projection and cold-read economics

**Goal:** Rebuild and serve render/search/recall from immutable raw truth at
predictable latency and cost, whether Phase 4 uses remote authority or Phase 3
remains a backup/mirror.

Deliver projector scheduling/telemetry, remote read/cache policy, cold cohort
proof, search/recall rebuild drills, and measured object packing/caching only
where read amplification justifies it.

Gate: recent and cold read SLOs hold during onboarding, and deleting every
derived store followed by rebuild restores equivalent product results.

### Phase 6A — Existing-tenant migration

**Goal:** Migrate dogfood safely without coupling migration completion to fleet
load proof.

Deliver the maintenance-state control, verified frozen snapshot, full-capacity
manifest migration, restore/cutover/rollback runbook, storage inventory parity,
spending guardrails, and operator controls. Keep production paused until the
candidate passes product validation; do not trickle-migrate dogfood merely to
preserve unused availability.

Gate: the pre-cutover generation is independently restorable; the new generation
passes manifest and end-to-end product proof; rollback is exercised; and storage
growth has an actionable lead signal.

### Phase 6B — Fleet burst proof

**Goal:** Prove that simultaneous signups degrade through visible queueing and
backpressure rather than outage.

Deliver concurrent tenant simulation, fairness proof, capacity projection,
cost/spending alarms, and operator pause/resume exercises. This depends on Phase
2 admission, not on completing existing-tenant migration.

Gate: burst onboarding preserves live reservations, tenant isolation, deletion
boundaries, and predictable per-tenant progress under configured ceilings.

### Phase 7 — Evidence-based fleet simplification

**Goal:** Decide whether per-tenant containers, tenant SQLite catalogs, search
stores, or the current host layout should change.

This is a decision phase, not a promised rewrite. Compare measured baseline and
operational burden against shared-process/shared-database designs. Any accepted
change needs a staged isolation, migration, backup, noisy-neighbor, and rollback
proof. If the measured system is economical and reliable, keep it.

## What We Explicitly Are Not Building

- Kafka, Redis, or a generic distributed job platform;
- a mandatory cloud service for self-hosted Longhouse;
- one shared SQL transcript table containing every provider record;
- cross-tenant raw-object dedupe;
- an event-level analytics warehouse before retained metrics become limiting;
- a bespoke autoscaler based on an arbitrary tenant count;
- a simultaneous object store, database, container, and onboarding rewrite;
- zero-downtime migration machinery before external users require it;
- a new parser-dependent raw format;
- silent deletion of the customer's local source after upload;
- pricing or customer quotas based on unmeasured estimates.

## Epic Acceptance

The epic is complete when:

1. current and cold product paths have retained real-user and synthetic evidence;
2. a new customer can inventory and progressively import multi-provider history
   without overwhelming live work;
3. acknowledged raw history is durable in the hosted object backend and
   independently restorable without the customer machine;
4. timeline/detail/search/recall can be rebuilt and served from the catalog plus
   immutable objects within declared SLOs;
5. concurrent onboarding is bounded by adaptive admission, fair across tenants,
   observable, and recoverable;
6. storage and processing costs are attributable from measured units;
7. deletion, authorization, corruption, rollback, and disaster recovery are
   exercised rather than assumed;
8. legacy duplicate storage is removed only after restore proof; and
9. any fleet/database/container redesign is supported by evidence—or explicitly
   rejected because the simpler system is good enough.
10. every prelaunch authority-changing migration uses a verified offline
    snapshot/cutover/rollback path instead of prolonged live coexistence.

## Open Decisions

- Which hosted object backend wins the Phase 3 contract, latency, cost, and exit
  evaluation?
- After remote backup/mirroring passes restore proof, is remote acknowledgement
  authority worth its added operational surface for launch?
- Does proxied ingress ever become a measured bottleneck large enough to justify
  a second direct-upload topology?
- What recent-history policy produces the best time to first value across real
  customer inventories?
- What remote cache, if any, is needed for current and cold render objects?
- What retained telemetry backend becomes worthwhile after the NDJSON baseline?
- Which customer controls and plan limits are needed once real import/storage
  distributions exist?

These questions are gates, not omissions. Phases 1–3 produce the evidence needed
to answer them without inventing a customer or a workload.

## Independent Review Synthesis

Two independent architecture reviews evaluated the first draft against current
storage-v2 code, `VISION.md`, and the adjacent durability specs. Both approved
the architectural spine and rejected the original phase order.
This revision incorporates their shared conclusions:

- progressive onboarding and admission now precede remote object authority;
- the frozen immutable outbox, not a reread provider log, is retry authority;
- durability claims name restart, host-loss, backup, and remote-backend failure
  domains;
- proxied bounded-envelope upload is the launch default;
- hosted keys are tenant-scoped and hashes never authorize;
- catalog/tombstone backup and deletion-version expiry are remote cutover gates;
- Phase 1 includes crude visible safety ceilings as well as measurements;
- object count/request/deletion economics are decided before remote authority;
  and
- remote backup plus restore proof is a valid launch stopping point.

The subsequent owner refinement is also explicit: while there are zero external
users, storage cutovers use planned downtime and full-capacity offline migration.
The epic does not spend prelaunch engineering effort preserving live SLAs during
the transformation itself.
