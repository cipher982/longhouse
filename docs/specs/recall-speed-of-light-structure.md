# Recall at Speed of Light: Structure

**Status:** Design, not implemented
**Owner:** Longhouse session core
**Created:** 2026-07-25
**Depends on:** `speed-of-light-recall.md`, `speed-of-light-database.md`

## Purpose

`speed-of-light-recall.md` fixed the algorithm: recall no longer ranks the whole
match set, and broad queries dropped from 3.4s to a few hundred milliseconds.
This document asks the different question — what does recall cost if every step
runs at the limit the hardware allows, and what structure reaches it.

The answer is that recall is now bound by storage physics rather than by
algorithms, that the floor is roughly 100x below where we sit, and that closing
the gap is a layout problem: what is resident in memory, and how many bytes the
hot path is forced to touch.

## Measured Machine

Hosted runtime host, tenant `david010`, 2026-07-25.

| Property | Value |
| --- | --- |
| CPU | 8 vCPU AMD EPYC Rome |
| RAM | 15.2 GiB total, 9.6 GiB page cache serving *all* tenants |
| Volume | network-attached, not local NVMe |
| Sequential read | 278 MB/s |
| Random 4 KiB read | **600 µs** (7 MB/s effective) |
| `search.db` | **16.7 GiB** |
| `events` (archive) | 4.36M rows, 5.9 GB of text |
| `searchable_events` (hot) | 1.385M rows, ~2.1 GB of text |

Measured page allocation inside the file (`dbstat`), in MB:

| Segment | Size | Lane |
| --- | --- | --- |
| `events` | 9,248.8 | archive |
| `events_fts_data` | 2,261.0 | archive |
| `ix_search_events_session_generation_order` | 788.6 | archive |
| `sqlite_autoindex_events_1` | 346.3 | archive |
| `ix_search_events_worklog` | 298.6 | archive |
| `events_fts_docsize` | 49.8 | archive |
| `searchable_events` | 2,683.3 | **hot** |
| `searchable_fts_data` | 1,061.5 | **hot** |
| `ix_searchable_events_session` | 227.3 | **hot** |
| `ix_searchable_events_window` | 25.3 | **hot** |
| `searchable_fts_docsize` | 18.2 | **hot** |

**Hot lane: 4.0 GB. Archive lane: 13.0 GB.** The interactive corpus is 24% of
the file it lives in, and it fits in page cache with room to spare — but only if
something keeps the other 76% from evicting it.

Two numbers govern everything below.

**A random read costs 600 µs.** That is ~6,000x a main-memory reference. Any
design that performs one disk seek per candidate is finished before it starts:
50,000 candidates × 600 µs is 30 seconds. The hot path must not fault.

**The database is larger than the machine's entire page cache.** 16.7 GiB of
file against 9.6 GiB of cache shared with every other tenant. Residency of the
hot slice is therefore not something the current layout can promise — the hot
1.385M rows are interleaved, in one file, with 13.0 GB of archive that worklog
exports and all-history searches sweep through. Every such sweep evicts the
pages interactive recall depends on.

This is the mechanism behind the observed spread: the same query costs 0.23s
warm and 4.8s cold, and a query matching only 165 events took 4.8s cold despite
costing 1 ms of CPU. That is not ranking. That is page faults.

## Speed-of-Light Budget

What "find the best 20 of a 90-day corpus" costs with nothing wasted, per step,
for a candidate window of 2,000:

| Step | Work | Floor |
| --- | --- | --- |
| Compile query | parse, tokenize | 0.05 ms |
| Walk doclist to K candidates | read compact varint postings | 0.5 ms |
| Apply owner/project/window filters | K × ~40 B of fixed-width columns | 0.05 ms |
| Score BM25 | K × ~200 ns | 0.4 ms |
| Select top 20 | heap over K | 0.02 ms |
| Fetch 20 payloads + snippets | 20 rows of real text | 0.2 ms warm / 12 ms cold |
| **Total** | | **~1–2 ms warm, ~15 ms cold** |

Measured today: 23–95 ms warm at K=2,000, 233–425 ms warm at K=50,000, and up to
5 s cold. The product budget in `speed-of-light-database.md` is 500 ms p95 with a
2 s hard bound.

So recall currently passes its budget warm and violates it cold, while sitting
roughly **50–100x above the floor**. The budget is not the interesting number
anymore; the floor is.

## Where The Bytes Go

The current hot path walks the FTS doclist in rowid order and, for each
candidate, joins `searchable_events` to evaluate `owner_id`, `project`,
`environment`, and `order_time_us`. That join fetches the **whole row**, and the
row contains `content_text` and `tool_output_text` — averaging ~1.5 KB and
reaching 99 KB.

Measured, a `searchable_events` row occupies **2,383 bytes** of page space. For a
50,000-candidate window that is ~119 MB of random row fetches to answer a query
that returns 40 rows. The filter columns actually needed total **20 bytes** per
row.

The hot path touches roughly **117x more bytes than the question requires**, and
every one of those bytes is a candidate page fault at 600 µs.

## Structure

Four changes, in dependency order. Each is independently shippable and each one
is measurable on its own.

### 1. Split the hot corpus into its own file

Move `searchable_events` and `searchable_fts` out of `search.db` into
`searchable.db`. The archive `events` / `events_fts` stay where they are.

The hot file is 4.0 GB measured — text plus index — which fits in page cache
with room to spare. More importantly it stops sharing an eviction domain with
the 13.0 GB archive lane, so a worklog export can no longer cost the next
interactive search a cold start. Residency becomes a property the layout
guarantees rather than a coincidence of recent traffic.

Note the two largest archive segments, `events` (9.2 GB) and `events_fts_data`
(2.3 GB), are exactly what all-history search and worklog export sweep. They are
11.5 GB of the 16.7 GiB file and they are the eviction pressure.

This is also the change that makes the remaining three measurable: while the hot
set is randomly evicted, every latency number is dominated by noise.

### 2. Separate the scan-hot columns from the fetch-cold payload

Give the walk a narrow spine table clustered by rowid, holding only what the
filters read:

```text
searchable_spine(
  source_event_id INTEGER PRIMARY KEY,  -- = FTS rowid = archive events.id
  owner_id        INTEGER NOT NULL,
  project_id      INTEGER,              -- interned
  environment_id  INTEGER NOT NULL,     -- interned
  provider_id     INTEGER NOT NULL,     -- interned
  order_time_us   INTEGER NOT NULL
)
```

Prototyped against the real-text fixture, the spine costs **20 B/row** against
**2,383 B/row** for the full `searchable_events` row — a **117x** reduction. At
1.385M rows that is a **~28 MB** spine, small enough to stay resident
permanently and fixed-width so a scan is sequential rather than a pointer chase
through multi-KB rows.

The walk reads doclist + spine only. Text is fetched for the returned page
alone, which recall already does for snippets since `adbf3ab4c`.

Measured on the same fixture with everything already in page cache, the spine
join beats the full-row join 8.7 ms vs 14.5 ms (`the`) and 2.1 ms vs 4.8 ms
(`runtime`) at K=50,000. That 1.7–2.3x is the *floor* of the benefit: it is pure
CPU and page-touch savings with no faults involved. The real win is that a
50,000-candidate window stops needing ~119 MB of row pages and needs ~1 MB
instead, which is the difference between faulting and not faulting on a volume
where a fault costs 600 µs.

Interning `project`/`environment`/`provider` to integers is what keeps the spine
fixed-width. It also makes the filters branch-free integer compares instead of
string comparisons.

### 3. Raise the page size for the hot file

A 600 µs random read costs 600 µs whether it returns 4 KiB or 16 KiB — the cost
is latency, not bytes. SQLite defaults to 4 KiB pages. For a corpus whose access
pattern is "walk a doclist and scan a spine," 16 KiB pages amortize a fault
across 4x the useful data.

This is a one-line pragma at rebuild time and must be measured, not assumed: it
helps sequential locality and hurts if access is genuinely scattered.

### 4. Make the candidate ceiling adaptive

With the spine resident, examining candidates costs ~40 ns each rather than a
potential page fault. The ceiling can then be set by a real time budget instead
of the fixed 50,000 chosen to bound worst-case I/O.

Recall against full BM25 (measured on 13,338 distinct real events) tracks the
ratio of candidates examined to matches: exact above ~1.2, degrading roughly
linearly below it. A resident spine makes a ratio of 1.0 affordable for almost
every real query, which means `ranking_scope: "recent_bounded"` becomes rare
rather than routine for common terms.

## What This Does Not Do

The archive lane stays on disk and stays slower. That is correct: it is the
honest wide lane, it is swept sequentially at 278 MB/s rather than randomly, and
`speed-of-light-recall.md` already treats it as a separate promise.

None of this adds a service, a vector database, or a second source of truth.
`searchable.db` is disposable and rebuildable exactly as `search.db` is today; it
is the same derived data with a physical boundary drawn around it.

## Expected Result

| Path | Today | With structure |
| --- | --- | --- |
| Rare term, warm | 1 ms | ~1 ms |
| Broad term, warm, K=50k | 233–425 ms | ~5–15 ms |
| Any term, cold | up to 5 s (503) | ~15 ms |
| p95 vs 500 ms budget | passes warm, fails cold | ~30x margin |

The point is not the multiple. It is that the cold cliff disappears, because
after step 1 there is no longer a meaningful cold state for the interactive
corpus.

## Open Questions

1. Does `searchable.db` want its own SQLite connection pool and admission lane,
   or does it inherit searchd's? Separate files argue for separate pools.
2. Is `project` low-cardinality enough to intern safely, or does it need a
   dictionary table with eviction?
3. Does the spine want `role` and `tool_name` too? They are small and some
   callers filter on them.
4. What rebuilds the spine — the existing publish path, or a projector?
5. Does raising page size to 16 KiB help or hurt the FTS doclist specifically?

## Sequencing

Step 1 first and alone, because it is the change that makes the others
measurable. Then step 2, which is expected to carry most of the remaining win.
Steps 3 and 4 are tuning against a system whose behavior is finally stable, and
should not be attempted before then.

Independently of all four: `search.db` is 16.7 GiB against 9.6 GiB of cache, and
most of it is archive. The Phase E reclaim already identified for the monolith
applies here for the same reason — a smaller file raises the cache hit ratio for
everything on the box.
