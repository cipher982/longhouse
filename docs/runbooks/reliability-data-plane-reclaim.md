# Reliability Data Plane — Reclaim Runbook (Phases B–F)

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

Per `archive-decommission-plan.md`. Copy structured + control tables, omit raw
payloads, preserve event ids/order/indexes/triggers/FTS/`sqlite_sequence`.

```bash
# On a target volume with space:
# 1. Build clean slim DB by copying control/structured tables (no raw columns),
#    dropping source_lines.raw_json_z and events.raw_json_z, keeping slim index.
# 2. quick_check + row counts vs source (structured rows must match exactly).
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
