# Recall at Speed of Light: Structure

**Status:** Design. Revised after measurement and review; earlier sequencing was wrong.
**Owner:** Longhouse session core
**Created:** 2026-07-25
**Depends on:** `speed-of-light-recall.md`, `speed-of-light-database.md`

## Terminology

Two distinctions get confused because both sound like temperature. They are
independent, and the design depends on keeping them apart.

**Hot lane vs archive lane — *which data*.** A permanent property of a row. The
hot lane is the published, current-generation, 91-day corpus
(`searchable_events` + `searchable_fts`, 4.0 GB). The archive lane is all
history up to 365 days (`events` + `events_fts`, 13.0 GB). Today **both live in
the same file**, `search.db`. The split is logical, not physical.

**Warm vs cold — *where the bytes are right now*.** Whether a page is resident
or must be read from the volume. Changes minute to minute; not a database.

The failure being fixed is that **the hot lane goes cold**. Cold and archive are
not synonyms.

## What Is Actually Wrong

`speed-of-light-recall.md` fixed the algorithm: recall no longer ranks the whole
match set, and broad queries went from 3.4 s to a few hundred milliseconds warm.
What remains is not an algorithm problem.

Measured on hosted `david010`, page-cache faults per query against latency:

| query | cold | MB faulted | warm | MB faulted |
| --- | --- | --- | --- | --- |
| `projector` | 0.60 s | 7.8 | 0.13 s | 0.0 |
| `deployment` | 2.24 s | 36.4 | 0.19 s | 0.0 |
| `function` | 4.97 s | 93.5 | 0.57 s | 9.3 |
| `database` | 4.95 s | 88.3 | 4.92 s | 78.8 |

**Latency is a linear function of megabytes faulted in**, at ~13–18 MB/s
effective — consistent with the measured 600 µs random 4 KiB read on this
volume. Once resident, every query is 0.13–0.19 s.

`database` is the pathological shape: it faults ~88 MB, hits the 5 s fuse, is
killed, and on retry faults another ~79 MB. It cannot warm itself faster than
the deadline kills it, so it never completes.

### The cause is first-touch, not eviction

The original version of this spec claimed archive sweeps evict the hot lane, and
proposed splitting files to stop it. That was wrong, on two independent grounds.

**Measurement:** PSI memory pressure on the host is `avg10=avg60=avg300=0.00`
with 12.5 GB available and no cgroup limit on the container (`memory.max=max`).
Nothing is being reclaimed. These are first-touch faults on a large per-query
footprint, not eviction of a warmed set.

**Mechanism:** Linux identifies cached pages by inode but reclaims across the
host or cgroup. A separate file does not create a separate eviction domain, so
even under real pressure, scanning `search.db` could evict `searchable.db` pages
just as easily. Separate inodes buy separate SQLite cache policy and operational
ownership — not residency.

Both point the same way: **splitting the file does not reduce bytes touched, so
it cannot fix this.** The sequencing in the first draft was backwards.

## The Real Target: Bytes Touched Per Query

A query returning 5 results faults in up to 93 MB. The walk evaluates
`owner_id`, `project`, `environment`, and `order_time_us` — about 20 bytes per
row — but reaches them through `searchable_events` rows that measure 2,383 bytes
of page space and whose filter columns sit *after* both large text columns in
the record (`store.py:342-359`).

The fix is to let the walk read a covering structure instead of full rows.

Two candidates, and the cheaper one has not been tried:

**A covering index** on `searchable_events(source_event_id, owner_id, project,
environment, provider, order_time_us)`. No new table, no second write path, no
consistency question. If SQLite selects it, the walk never touches the base row.

**A narrow spine table** with interned integer ids for project/environment/
provider. Prototyped at 20 B/row against 2,383 B/row, and 1.7–2.3× faster warm
on a real-text fixture at K=50,000. But it introduces a second table to keep
consistent, and it is not truly fixed-width — SQLite records use varints, and
lookups ordered by sparse archive ids are not a sequential scan.

**Build the covering index first.** If it captures most of the win, the spine's
extra consistency burden is unjustified. Decide on measured cold physical reads,
not on `dbstat` byte ratios — the 117× figure compares row sizes, and does not
by itself prove 119 MB of unique physical reads, since large fields use overflow
pages.

## If The File Is Ever Split, Option A Is Unsafe

The first draft proposed splitting the hot lane into `searchable.db` and
accepting torn writes reconciled by a watermark, on the grounds that the hot lane
is disposable. That is wrong, and the reason is deletion.

`delete_session` (`store.py:1179-1191`) removes `session_index`,
`searchable_events`, `projection_membership`, `events`, and `indexed_objects` in
one `BEGIN IMMEDIATE`. SQLite in WAL mode has no atomic commit across `ATTACH`ed
files. **A torn cross-file delete leaves deleted content searchable
indefinitely.** That is a data-deletion failure, not a staleness window.

Supersession has the same shape: publication removes superseded membership and
events atomically (`store.py:685-770`). Committing archive first can leave hot
rows pointing at a generation whose archive events are gone, so search returns a
hit whose context resolves `hit_not_published` (`store.py:898-908`).

The phrase "stale, missing, or half-replaced" in the first draft was also simply
inaccurate: a transactional hot replacement is atomically old or new. The hazard
is *retracted* data, not partial data.

If the split ever happens, it requires a durable projection outbox written inside
the archive transaction, carrying **tombstones**, applied idempotently to hot
storage, with projection lag exposed. Deletions and owner/access changes need
stronger gating than ordinary freshness. That is option C, and it is the only
safe form.

## Corrected Expectations

The first draft predicted "~15 ms cold" and "no meaningful cold state." Both are
unsupported. A genuinely cold query must also read FTS vocabulary, segments,
postings, and B-tree interior pages — not only payload rows. Splitting cannot
eliminate cold starts, host pressure, or first access after a rebuild.

The defensible claim is narrower: **reducing the walk's footprint moves broad
queries below the fault threshold that currently kills them.** `deployment`
faulted 36 MB and took 2.24 s; a covering walk that faults a few MB should behave
like `projector` at 0.60 s cold and 0.13 s warm. `database`, which currently
never completes, should complete at all.

Latency targets should be reported as measured distributions across four states —
warm, after a controlled archive sweep, after process restart with warm kernel,
and genuinely cold host — not as a single number.

## Known Defects Worth Fixing Independently

1. **`ranking_scope` names the wrong thing.** The bounded walk returns the
   highest eligible `source_event_id` values, which is archive *insertion* order.
   Late imports and backfills can violate recency, so `recent_bounded` overclaims.
   Rename to `rowid_bounded` unless insertion-time recency becomes an enforced
   invariant.
2. **Saturation is reported conservatively.** A match set of exactly
   `_CANDIDATE_CEILING` is reported bounded although the walk saw everything
   (`store.py:871-884`). Harmless direction, but the signal is not exact.
3. **`retrieval.db`** (511 MB, `recall_chunks`) has not been written since
   July 8, and live recall returns `chunk_id: null`. Possibly dead weight; no one
   has confirmed nothing reads it.

## Sequencing

1. **Instrument the cold path.** Capture `admit_ms`/`sql_ms`, cgroup I/O and
   fault counters, WAL size and checkpoint state, and per-query resident-set
   delta. The fault-vs-latency table above should be reproducible on demand
   rather than assembled by hand.
2. **Prototype the covering index inside the existing database and
   transaction**, and verify with `EXPLAIN QUERY PLAN` that it is actually
   selected. Compare cold physical reads against the spine. Keep the winner.
3. **Run Phase E reclaim** and repeat the identical benchmark. `search.db` is
   16.7 GiB against 9.6 GB of host cache; a smaller file raises the hit ratio for
   everything, and the owning storage spec already sequences reclaim before cache
   and FTS tuning.
4. **Split files only if** the experiment shows a benefit beyond cache tuning, or
   for operational ownership — and then only via an outbox with tombstones.
5. **Tune page size and candidate policy last**, once the storage path and a
   relevance benchmark are trustworthy.

Cache and hardware tuning (`cache_size`, `mmap_size`, a larger box) are
mitigations, not diagnosis. Note that SQLite page cache is per connection, so
raising `cache_size` multiplies across the reader pool (`server.py:84-94`).

## Open Questions

1. Does the covering index capture most of the spine's benefit? If so the spine
   is unjustified complexity.
2. What is the per-query cold footprint after the covering walk — does it drop
   below the fault threshold that kills `database` today?
3. How much does Phase E reclaim alone move the fault curve?
4. Is `project` low-cardinality enough to intern, if the spine wins anyway?
5. Does a production query-distribution replay confirm the candidate ceiling is
   relevance-safe across projects, providers, narrow windows, and backfills? The
   13,338-event study did not cover those.
