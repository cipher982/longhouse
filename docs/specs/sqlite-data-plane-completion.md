# SQLite Data-Plane Completion (Finish-the-Specs Epic)

**Status:** Approved plan, not started; step 1 gated on David's reclaim approval
**Owner:** Longhouse core
**Created:** 2026-07-09
**Related:** `docs/specs/reliability-data-plane.md`,
`docs/specs/hot-cold-runtime-reliability-hardening.md`,
`docs/specs/hosted-archive-restart-control.md`,
`docs/specs/db-load-observability.md`,
`docs/runbooks/reliability-data-plane-reclaim.md`

## Executive Summary

The decision is made: Longhouse stays SQLite-only. Postgres was evaluated
(2026-07-09, external research + code audit) and rejected for now — it would
fix the lock class but fork the product into two storage backends, against the
self-host wedge. The escape-hatch condition is recorded in "Exit Criteria"
below.

The audit found the architecture has already converged on the right shape and
is further along than the specs admit:

- `longhouse-live.db` (~373MB) — separate hot store: launch readiness,
  presence, runtime state, inputs, previews.
- `/data/archive` (~33GB) — immutable compressed raw chunks, 100%
  byte-verified for both streams; legacy raw writes to the monolith disabled
  since 2026-06-07.
- `retrieval.db` (~488MB) split, recall served from an isolated helper
  process; job queue in its own DB.
- Writer watchdogs (interrupt, escalation, aging), WAL-pressure shed (1GiB),
  hosted archive-repair default `paused`, `ready_with_archive_degraded`
  readiness — the July 7 hardening gap list is mostly closed.

What remains is finishing, not designing: run the already-built Phase E
reclaim, tune the resulting file, and make the win permanent. Ordered by
leverage, each step's payoff proven by the measurement layer from
`db-load-observability.md`.

Sequencing in one line: **measure → freeze → reclaim → tune → protect.**

## Current State (verified 2026-07-09 on the largest hosted tenant)

- `longhouse.db` 126GiB, WAL ~300MB (healthy), page_size 4096.
- ~61GB of monolith raw (`events.raw_json_z` + `source_lines.raw_json_z`) is
  archive-covered and droppable.
- `phase-e-build-slim.py` + `phase-e-reclaim.sh` are built, hatch-reviewed,
  and guarded (`REQUIRE_RECLAIM_OK=1`). The June swap blocker
  (workflow-ingest subagent re-parenting) is resolved: the rebuild is now
  conditional and owner-aware (sentinels only provably covered rows under
  `archive_owning_session_ids` owner mapping; keeps raw otherwise; aborts on
  anything unrecoverable).
- FTS is external-content with batched triggers, but there is **no FTS5 merge
  maintenance anywhere** (no `optimize`/`merge` calls, no automerge tuning).
- Read engine runs factory defaults: no `cache_size`, `mmap_size`, or
  `temp_store` pragmas (~2MB page cache against a 126GiB file).
- New ingest no longer writes raw to the monolith, but there is no rolling
  reclaim: without one, any future raw regression or residual growth needs
  another stopped-window rebuild.

## Plan

### Step 1 — Phase E reclaim (gated: David's explicit approval + stopped window)

Run `scripts/ops/phase-e-reclaim.sh` per the runbook after the baseline soak
completes: stop container → checkpoint/quiesce → owner-aware slim build →
validate → atomic swap → start → smoke → rsync old DB to NAS as rollback.
~2h window; irreversible at the swap.

Expected outcome: monolith drops to roughly 50–60GiB; FTS rebuilt into compact
segments; fresh unfragmented file; `dbstat` diagnostics and `VACUUM INTO`
backups become feasible again. The before/after delta from the capture jobs is
the headline number.

### Step 2 — Read-path cache tuning (after reclaim, ~a day of iteration)

Add `cache_size` (hundreds of MB on the read engine), `mmap_size` (several
GB), and `temp_store=MEMORY` as env-tunable pragmas in
`_configure_sqlite_engine`. Tune against the post-reclaim file using retained
metrics (timeline/search latency, IOPS share). Do not tune pre-reclaim — the
steady-state file is the one that matters.

### Step 3 — FTS5 incremental merge maintenance (small, low risk)

Add a bounded `INSERT INTO events_fts(events_fts, rank) VALUES('merge', N)`
pass (small N) to the existing maintenance loop where `PRAGMA optimize`
already runs, plus automerge/usermerge tuning if the baseline shows
trigger-time merge spikes. Purpose: keep the post-rebuild index compact under
continuous inserts. Apply the same treatment to `recall_chunks_fts` in
`retrieval.db` if its metrics warrant it.

### Step 4 — Rolling raw-sentinel job (protect the win)

Periodic job that sentinels raw columns on rows whose archive chunks have
since sealed + verified — the same owner-aware coverage check as the Phase E
build, in small batches through the WriteSerializer, cadence informed by
serializer label data. Outcome: the monolith never needs another
maintenance-window rebuild.

### Step 5 — Hardening-spec leftovers (can interleave; observability-only parts
may ship during the baseline soak)

From `hot-cold-runtime-reliability-hardening.md`, still open:

- live-archive-outbox oldest-pending / row-count alerts and guaranteed
  drain-share semantics;
- live transcript durability still transits the cold archive writer after the
  provisional preview — bound and measure it; only redesign if post-reclaim
  metrics still show it lagging.

## Explicit Non-Goals

- **No `derived.db` split.** Post-reclaim the monolith IS the derived store
  (events + FTS + product state) at a size SQLite handles. The closeout
  decision (slim monolith + archive, no derived.db) stands; another file is
  another writer, backup unit, and failure mode.
- **No Postgres migration, no dual backend.** See Exit Criteria.
- No WAL2 / `BEGIN CONCURRENT` / Turso MVCC experiments — none is a mature
  stock-SQLite production path (verified against upstream docs 2026-07-09).

## Exit Criteria (when to reopen the Postgres question)

Reopen only if, **after** steps 1–4 and with baseline-quality measurement:

1. the hot write rate itself (not archive contention) still produces lock
   incidents on the slim monolith; or
2. hosted multi-tenancy grows enough concurrent whale tenants that per-tenant
   SQLite files stop being an ops win (fleet migrations, backup windows,
   per-file WAL babysitting).

Landing zone if triggered: database-per-tenant Postgres (mirrors current
isolation; tenant export/delete stays one-database), `tsvector`/GIN for FTS,
pgvector for embeddings. The reclaim shrinks what would need migrating from
126GiB to a few GiB of hot state, so finishing this epic makes that path
cheaper, not harder.

## Acceptance

- Reclaim executed; monolith ≤ ~60GiB; qa-live green; before/after metrics
  delta recorded.
- Cache/mmap pragmas shipped and tuned with measured latency improvement (or
  measured null result and reverted).
- FTS merge maintenance running; segment growth bounded over a multi-week
  window.
- Rolling sentinel job running; monolith size flat or declining month over
  month at current usage.
- Both remaining hardening items closed or explicitly re-scoped in their spec.
