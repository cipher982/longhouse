# Reliability Data Plane — Reclaim Runbook (Phases B–F)

> ## 🅿️ RECLAIM PARKED (2026-06-09) — architecture migration DONE; disk reclaim deferred
> **The architecture migration is complete and live** (Phase D: archive-primary on,
> legacy raw off — the DB no longer bloats). Phase E (the one-time disk reclaim of
> the EXISTING 117GB file) was attempted and PARKED. Tenant healthy on the original
> DB the whole time; ZERO data lost.
>
> Why parked: the stopped-window clean-store rebuild is **too slow on this Hetzner
> volume** — a single run was ~5h (events copy ~66m, source_lines ~41m, observations
> ~22m, FTS rebuild, then the final check). Three separate attempts each died on a
> different perf/environment wall (multi-hour preflight quick_check; tenant restarted
> mid-build by another agent's reprovision; final `integrity_check` read 989GB on an
> 87GB DB and never converged). None were data-safety bugs — the logic was sound. The
> built 87GB slim was discarded (stale once the tenant restarted) and the scratch dir
> deleted. Reclaim would also only have been ~30GB (117→87) because Phase B exported
> only recent history, not the full corpus.
>
> **To do it right next time (do NOT just re-run this script as-is):**
> 1. Replace `PRAGMA integrity_check` with `PRAGMA quick_check` in phase-e-build-slim
>    (integrity_check is pathological on FTS5+many-index DBs over this volume).
> 2. Build the slim copy ONLINE against a snapshot, not in a multi-hour stopped
>    window — stop only for a short delta + swap. (Removes the restart-collision and
>    the long-outage problem entirely. This is the real fix; the restart-guard only
>    detects the collision, doesn't avoid it.)
> 3. Run Phase B export over the FULL history first, so the reclaim drops the whole
>    ~60-88GB instead of ~30GB.
> The build/swap scripts (phase-e-reclaim.sh, phase-e-build-slim.py) + restart guard
> + conditional owner-aware coverage all work and are committed — reusable once the
> above are addressed. Related storage wins parked in docket: image dedup,
> compress session_observations (~18GB, uncompressed today).


> ## ⛔ SWAP PARKED (2026-06-08) — do not run step 7 until the workflow-ingest feature lands
>
> **hatch codex final ruling (2026-06-08): NO-GO / PARK.** Verbatim: *"Parking is
> the correct terminal state for this work right now. This is not avoidance... The
> Stop hook is wrong to force execution here. The correct deliverable is a parked
> Phase E with a clear dependency on the workflow-ingest keying change."* It also
> ruled out the two shortcuts: (a) conditional-rebuild (keep raw for unproven rows)
> does NOT eliminate the re-parenting race — rows snapshotted "covered" under one
> session_id can be re-parented later and stop resolving; (b) a partial swap of
> only non-workflow sessions adds an operational branch mid-migration for less
> reclaim and isn't worth it. Resume sequence: workflow-ingest re-parenting lands +
> stabilizes → update verifier/rebuild to resolve parent coverage across child
> session_ids (or record the archive-owning child session id) → re-verify against
> the settled model → run the guarded swap.
> Phases B/C/D are SHIPPED and stable (legacy raw off; DB stopped growing). Phase E
> prep is staged: export done + verified, compaction_kind backfilled, fail-closed
> export shipped, Option-B rebuild scripts written+hatch-reviewed
> (`scripts/ops/phase-e-reclaim.sh` + `phase-e-build-slim.py`). **The clean-store
> swap (step 7) has NOT run and must not, because of a hard ordering dependency on
> another agent's in-flight work:**
>
> - The final pre-swap re-verify (step 6) found uncovered `source_lines` rows on
>   **active workflow sessions** — all under `/subagents/workflows/.../agent-*.jsonl`
>   sidechain paths (e.g. session `bbac0f94...` 248 rows, `c48dae2b...` ~31.7k rows;
>   the latter is the g55 workflow run).
> - A separate agent is building **full Claude Code dynamic-workflow ingest support**
>   (`/tmp/goal-workflows-ingest.md`). Its Phase 2 **re-parents orphaned subagent
>   sessions**, which MOVES `source_lines` rows between `session_id`s. My reclaim
>   drops raw permanently with archive recovery keyed by `session_id`. If I drop
>   first and they re-key after, moved rows lose their archive linkage → data loss.
> - Therefore: **hold step 7 until workflow ingest is merged + stable**, then re-run
>   step 6 against the settled keying, and use the conditional-rebuild rule below
>   (sentinel only proven-covered rows; KEEP raw for any uncovered row; FAIL on a row
>   with neither raw nor coverage). hatch confirmed the unconditional drop is unsafe.
> - RESOLVED (definitive cross-session scan, 2026-06-08): NO data hole. All 72
>   sampled "missing" subagent bytes ARE sealed in the archive — under the
>   SUBAGENT's own resolved session_id (e.g. `6333677e-...`), not the parent. The
>   reclaim verifier scopes archive chunks by the parent session_id and so misses
>   subagent-keyed chunks. Mechanism: archive-primary keys subagent source_lines
>   chunks by the subagent session_id; `ingest_session` merges the slim rows under
>   the parent. **Fix the reclaim verifier + rebuild to resolve a parent's subagent
>   source_lines coverage across the child session_ids** — and do it against the
>   POST-workflow-ingest keying, since their Phase 2 re-parenting changes these
>   parent↔child relationships. No raw is at risk; this is a scoping correctness
>   fix, not a loss.

> ## ⏱️ DOWNTIME FINDING (2026-06-08) — swap as-built is a 2-3 HOUR outage, NOT minutes
> First live swap attempt was aborted in read-only preflight (DB untouched, tenant
> healthy) after a full-DB `quick_check` projected to ~2.5h. That heavy preflight
> check is removed. BUT a deeper hatch review found the build itself runs entirely
> DURING downtime and is inherently long for this 117GB tenant: archive coverage
> pass (~30min), 89-table copy, 12.9M source_lines join, **10.5M-event row-by-row
> Python decode+hash (~30-90min)**, FTS rebuild, integrity check → **90min best
> case, 2-3h likely.** Live coding sessions spool/survive, but browser+iOS steering
> is down the whole window. DO NOT run the all-in-stopped-window builder expecting
> a short outage. Right shape (hatch): precompute coverage + event raw-hashes
> WHILE UP (archive is append-only; events immutable) into a sidecar DB; build the
> slim candidate online from a snapshot; stop only for a short delta catch-up +
> swap. Needs a correct catch-up path for mutable tables. Until that's built,
> options are: accept a multi-hour outage in a planned window, or do the rework
> first. (Longer-term: persist events.raw_sha256 at ingest so Phase E never
> decodes historical BLOBs.)

**Status:** DRAFT — requires David's explicit approval before ANY step runs.
**Predecessor:** `docs/specs/reliability-data-plane-closeout.md` (architecture + PRs 1–3, shipped).
**Companion gate:** `docs/runbooks/archive-decommission-plan.md` (the approval-gate doctrine this obeys).
**Target tenant:** `david010` (hosted). Host SSH alias: `zerg`. Container: `longhouse-david010`. DB: `/data/longhouse.db` (116 GB).

> This runbook DROPS production raw data. Every destructive step is gated,
> verified at the row level, and reversible via retained copies. Nothing here
> runs until David approves the specific phase. Phases B–C are read-only and
> safe; D–F are destructive.

## Why

PRs 1–3 (shipped, SHA `0f76d153`) made new ingest reclaim-ready: structured
`compaction_kind`, archive-backed export, slim `source_lines` index always
written, archive lookups keyed by `line_hash`, and a row-level coverage
verifier. The 116 GB monolith is still ~88 GB redundant raw transcript stored up
to 3× (`source_lines.raw_json_z` ~48 GB, `session_observations.payload_json`
~27 GB uncompressed, `events.raw_json_z` ~13 GB) plus the archive copy. This
runbook exports raw to the archive, makes the archive the sole raw source, drops
the redundant raw, and reclaims disk.

## Invariants (never violate)

1. **Row-level byte identity before any drop.** Every `source_lines` row to be
   slimmed must pass `verify_session_archive_coverage` — a matching archive
   record on `(session_id, source_path, source_offset, line_hash)`. Chunk- or
   session-level coverage is NOT sufficient.
2. **`events` is durable serving state.** Never rebuilt from archive (id/FTS
   rowid drift breaks UI refs, tool pairing, active-context). Restored only from
   DB backup.
3. **Three raw copies retained until reclaim is approved & verified:** old
   monolith raw, sealed archive, off-volume NAS backup.
4. **No live `VACUUM` of the 116 GB DB**, no in-place raw-table rebuilds on the
   constrained volume (72 GB free). Reclaim is clean-store rebuild, swapped in.
5. **One phase at a time, with David's go between destructive phases.**

## Pre-flight (read-only, run before Phase B)

```bash
# Live SHA + disk
curl -s https://david010.longhouse.ai/api/health | python3 -m json.tool
ssh zerg 'docker exec longhouse-david010 sh -lc "df -h /data; ls -lah /data/longhouse.db /data/archive"'

# Re-verify the 2026-06-05 NAS backup still exists and counts match.
ssh synology 'ls -la /volume1/homes/drose/longhouse-backups/reliability-data-plane-20260605/david010/'
# Compare sessions/events/source_lines counts backup vs live (cheap max(rowid)).
```

Gate: NAS backup present, restorable, counts within expected growth delta.
**If the backup is stale, take a fresh off-volume backup before proceeding.**

---

## Phase C — Confirm archive is off the constrained volume (FIRST)

`archive_root` defaults beside the DB on `/data` (the 70%-full volume).
Exporting 116 GB there risks filling it.

```bash
ssh zerg 'docker exec longhouse-david010 sh -lc "echo ARCHIVE=\$LONGHOUSE_ARCHIVE_ROOT; df -h /data; du -sh /data/archive 2>/dev/null"'
```

Decision with David: keep archive on `/data` (only if free space comfortably
exceeds compressed-raw estimate ~25–35 GB) OR mount a separate volume and set
`LONGHOUSE_ARCHIVE_ROOT` via control-plane custom-env, then reprovision.

Gate: archive target has headroom for full compressed raw + 30 GB floor.

---

## Phase B — Export 116 GB raw history to the archive (READ-ONLY)

The exporter (`zerg.cli.archive export-legacy`) is read-only, resumable,
keyset-paginated, with a disk floor and corruption quarantine. It does NOT
modify legacy raw tables.

**Dedup gap (from review):** the exporter does not skip records already written
live by archive-primary ingest, so it will re-emit recent rows. The archive
chunk key is idempotent (same bytes → same chunk path), so duplicates collapse
at the chunk layer, but verification must reconcile by `line_hash`, not row
count. Confirm before running whether to make the exporter dedup-aware against
`archive_chunks` (preferred) or accept idempotent overlap.

```bash
# Run inside the container against the live DB, archive-root from env.
# Loop until selected_rows == 0 for each stream, honoring the 30 GB floor.
ssh zerg 'docker exec longhouse-david010 sh -lc "
  longhouse archive export-legacy --source-table source_lines --disk-floor 30gb --json
"'
# Repeat the call until selected_rows==0, then:
ssh zerg 'docker exec longhouse-david010 sh -lc "
  longhouse archive export-legacy --source-table events --disk-floor 30gb --json
"'
```

Verification (read-only):

```bash
# 1. Archive chunk integrity (checksums) — sampled, not full dbstat under load.
# 2. Row-level coverage on a sample of sessions, incl. branched/rewound + heavy Codex:
#    for each, verify_session_archive_coverage(db, session_id).fully_covered == True
# 3. Reconcile unique (session, path, offset, line_hash) keys: monolith vs archive, zero missing.
```

Gate: 100% of unique `source_lines` raw records covered in the archive by
`line_hash`; event-stream replay classifies every record (`legacy_export` /
`live_archive_primary`), zero `unknown`. Monolith still fully intact.

Rollback: none needed — read-only. If aborted, archive has partial chunks;
re-running resumes from the export ledger.

**VERIFIED 2026-06-07 (both streams 100% covered):**
- source_lines: 10,441 sessions, 12,867,874 rows checked, 12,867,874 covered,
  0 incomplete (by `(source_path, source_offset, line_hash)`).
- events: scoped to the only sessions where events raw is the SOLE source —
  5,672 events-only + 571 partial-source-line = 6,243 sessions; 1,703,283 raw
  event rows checked, 1,703,283 covered, 0 incomplete (by `sha256(raw_bytes)`).
  The other ~9,900 source-line-backed sessions need no events-raw proof: their
  transcript is recoverable from the verified `source_lines` archive and
  `export_session_jsonl` never reads events raw for them.
- **Conclusion: both `source_lines.raw_json_z` and `events.raw_json_z` are safe
  to drop in the Option-B rebuild.** Drivers: `lh_export_driver.py`,
  `lh_partial_audit_sql.py`, `lh_events_coverage_scoped.py` (in `/data/` on host).
- Partial-audit method note: detecting partial-source-line sessions via per-
  session ORM iteration STALLS on 600k-event whale sessions (loads raw blobs).
  Use the SQL-only anti-join (`ix_events_dedup` covers the join key); the
  coverage phase then scopes to events-only + partial, skipping the whales.

---

## Phase D — Disable legacy raw writes (new data archive-only)

After the archive holds everything and PRs 1–3 are live (slim row always
written, archive-backed export proven):

```bash
# Set via control-plane custom-env (field name: custom_env), then reprovision.
# LONGHOUSE_DISABLE_LEGACY_RAW_WRITES=1   (or LONGHOUSE_LEGACY_RAW_WRITE_ENABLED=0)
# Requires LONGHOUSE_ARCHIVE_PRIMARY_WRITE_ENABLED=1 (already set).
make reprovision SUBDOMAIN=david010
```

Verify:

```bash
# Fresh ingest still 200s with X-Ingest-Legacy-Raw: disabled.
# New source_lines rows: slim (line_hash present, raw_json_z NULL).
# New events rows: structured cols present, raw_json_z NULL.
# source_lines.raw_json_z stops growing; detail/search/export/resume unaffected.
QA_INSTANCE_SUBDOMAIN=david010 make qa-live
```

Gate: qa-live 12/12; new raw bytes only in archive; resume works (archive-backed).

Rollback: unset the env var, reprovision. New ingest resumes dual-write. No data
lost (old raw still present; this only affects NEW rows).

---

## Phase E — Reclaim (clean-store rebuild + drop raw)

**Gated on David's explicit approval per `archive-decommission-plan.md`,
including exact paths, backup id, and rollback commands.**

### E1 — Make code/schema raw-tolerant (ship BEFORE any drop)

Otherwise startup recreates `source_lines` and `_auto_add_missing_columns`
re-adds raw columns from the model.

- Backfill `events.compaction_kind` on prod (idempotent, resumable):
  ```bash
  ssh zerg 'docker exec longhouse-david010 sh -lc "longhouse archive backfill-compaction-kind --json"'
  ```
  Gate: active-context boundary parity holds (the read no longer needs raw).
- Remove `raw_json`/`raw_json_z`/`raw_json_codec` from the `AgentEvent` model and
  the slim `source_lines` raw columns; update `database.py`/`db_migrations.py`
  recreate/auto-add so the slimmed schema is the new baseline. Ship + verify on
  canary FIRST.

### E2 — Observation ledger → transient buffer

`session_observations` is currently a durable raw ledger competing with the
archive. Demote transcript observations:

- `provider_event` / `provider_source_line`: prune after the row is BOTH
  projected into `events`/`source_lines` AND archive-sealed (verifier passes).
- Retire `session_observation_rebuild` for transcript (replace with
  archive-backed rebuild for repair tooling only).
- KEEP `runtime_signal` / `client_render` / `server_fanout` / bridge rows with
  bounded retention — product paths read them (`session_turns.py`,
  `session_runtime.py`, `client_render_observations.py`, `realtime_propagation.py`).
- Interim option if a fast win is wanted without the prune: compress
  `payload_json` → `payload_json_z` (columns exist, unused).

### E3 — Build clean slim DB and swap (NOT in-place)

**DESIGN (hatch-confirmed, Option B): KEEP the raw column definitions, write
sentinel/NULL values instead of payloads.** Do NOT remove
`raw_json`/`raw_json_z`/`raw_json_codec` from the models for this reclaim —
that's a coupled big-bang change that would break the exporter/verifiers and
every ORM SELECT during the dangerous phase. Instead the rebuild copies rows
but writes `source_lines.raw_json=''`/`raw_json_z=NULL`/`codec=0` and
`events.raw_json=NULL`/`raw_json_z=NULL`/`codec=0`. SQLite only copies real cell
payloads, so a freshly-built DB with NULLed raw reclaims the ~61GB while model +
readers stay intact. Step 5 therefore needs NO code PR — the runtime is already
raw-payload-tolerant (PR4 fail-closed/synthesize + archive-backed reads,
db_diagnostics column-guarded). Physical column removal is a LATER cleanup phase
after the archive-only state survives the retention window.

**Sequencing catch:** the events verifier goes vacuous after the swap
(`decode_raw_json` returns nothing → `rows_with_raw=0`), so events-raw coverage
MUST be proven before the swap, against the current/pre-slim DB.

```bash
# On a target volume with space:
# 1. Build clean slim DB by copying control/structured tables; KEEP raw columns
#    but write sentinels (raw_json=''/NULL, raw_json_z=NULL, codec=0). Keep slim index.
# 2. quick_check + row counts vs source (structured rows + ids must match exactly;
#    preserve events.id, FTS rowids, source_lines.id, manifests, indexes, triggers,
#    sqlite_sequence).
# 3. Stop runtime:        ssh zerg 'docker stop longhouse-david010'
# 4. Move old aside:      mv /data/longhouse.db /data/longhouse.db.preslim-<date>  (retain)
# 5. Place clean DB:      mv /data/longhouse-slim.db /data/longhouse.db
# 6. Start runtime:       make reprovision SUBDOMAIN=david010
# 7. Smoke (below).
```

Smoke gate:

```bash
curl -s https://david010.longhouse.ai/api/readyz
QA_INSTANCE_SUBDOMAIN=david010 make qa-live          # 12/12
# Manual: timeline loads, session detail loads, FTS search returns, a resume
# works (archive-backed transcript), control/launch/heartbeat fine.
ssh zerg 'docker exec longhouse-david010 sh -lc "ls -lah /data/longhouse.db; df -h /data"'  # confirm shrink
```

Rollback (any failure):

```bash
ssh zerg 'docker stop longhouse-david010'
ssh zerg 'mv /data/longhouse.db /data/longhouse.db.slim-failed && mv /data/longhouse.db.preslim-<date> /data/longhouse.db'
make reprovision SUBDOMAIN=david010
curl -s https://david010.longhouse.ai/api/readyz
```

Retention: keep `longhouse.db.preslim-<date>` + NAS backup + archive for a fixed
window (≥ 2 weeks of healthy operation) before deleting the old monolith.

---

## Phase F — Delete switchover scaffolding (one clean path)

Only after E is stable for the retention window. Collapse to a single path —
full inventory in `docs/specs/reliability-data-plane-closeout.md` "Scaffolding
Deletion Inventory". Summary:

ALREADY DONE (shipped — the dead hot/derived split, loose ends #1 and #2, was
never wired at runtime so it was safe to delete immediately, not gated):
- Deleted `data_plane.py` hot/derived store factories (`create_hot_store`,
  `create_derived_store`, `DataPlaneStore`, `initialize_*`), the `derived_events`
  schema, `archive_derived_projector.py`, `archive_hot_projector.py`,
  `archive_restore.py`, and their tests. `data_plane.py` is now just
  `create_archive_store`.
- Removed env split `LONGHOUSE_DATA_ROOT` / `LONGHOUSE_HOT_DATABASE_URL` /
  `LONGHOUSE_DERIVED_DATABASE_URL` and the `hot_database_url`/`derived_database_url`/
  `longhouse_data_root` Settings fields. `archive_root` resolves directly.

STILL GATED (load-bearing dual-write — removing these IS the reclaim, can't go
until raw is exported and legacy writes disabled):
- Remove flags + env: `LONGHOUSE_ARCHIVE_SHADOW_WRITE_ENABLED`,
  `LONGHOUSE_ARCHIVE_PRIMARY_WRITE_ENABLED`, `LONGHOUSE_LEGACY_RAW_WRITE_ENABLED`,
  `LONGHOUSE_DISABLE_LEGACY_RAW_WRITES`, `LONGHOUSE_ARCHIVE_SHADOW_CHUNK_TARGET_BYTES`.
- Collapse ingest shadow-vs-primary branch into one unconditional archive write;
  drop `X-Ingest-Archive-Primary` / `X-Ingest-Legacy-Raw` headers.
- Remove `ingest_session` params `write_legacy_raw` / `raw_source_archived`.
- Retire `legacy_archive_exporter.py` + `archive export-legacy` (one-shot, done).
- Remove tests asserting raw columns / the shadow-primary flag matrix.

Each removal ships behind canary verification; nothing user-facing changes.

## Definition of Done

- Monolith ≪ 116 GB; raw bytes live only in the archive.
- New ingest archive-only; structured detail/search/export/resume all green.
- Scaffolding deleted; one code path; flags gone.
- Old monolith + NAS backup retained through the window, then deleted on approval.
