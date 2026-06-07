# Reliability Data Plane — Loose-End Closeout Plan

**Status:** Draft for review (hatch codex) — not started
**Owner:** Longhouse core
**Created:** 2026-06-06
**Predecessor:** `docs/specs/reliability-data-plane.md` (the additive epic that shipped)
**Chosen end state:** Slim monolith + filesystem archive. No `derived.db`.

> This is a working plan, not durable doc surface. Delete it on completion
> (per repo doctrine: code + git are the truth once work ships).

## Why This Exists

The reliability-data-plane epic shipped the *additive* half of a data-plane
migration and closed the goal tracker, but four loose ends mean the original
problem — a 116 GB monolith carrying raw transcript bytes on the hot DB — is
**not actually solved**. Nothing has been reclaimed; the DB still grows.

This plan closes all four, then deletes the migration scaffolding so the repo
has one clean path.

## Ground Truth (verified 2026-06-06 @ `0bb4ba49`)

What is actually live on `david010`:

1. **Archive-primary raw writes are ON** with **legacy raw fallback ON**. New
   ingest writes raw bytes to *both* the filesystem archive (`archive/`) and the
   monolith (`source_lines` + `events.raw_json_z`). Confirmed via live ingest
   headers `X-Ingest-Archive-Primary: written` / `X-Ingest-Legacy-Raw: enabled`
   and `config/__init__.py:557-560`.

2. **The hot/derived physical split is NOT active.** With `LONGHOUSE_DATA_ROOT`
   unset (prod), `hot_database_url` falls back to the monolith `DATABASE_URL`
   (`config/__init__.py:111-112`). `derived_database_url` resolves to a sibling
   `derived.db` path (`:116-117`) but **no engine is ever created for it at
   runtime**.

3. **The projectors are dead code at runtime.** `project_archive_chunks_to_hot_cards`
   and `project_archive_chunks_to_derived_events` have **zero non-test callers** —
   no background loop, no lifespan task, no CLI. Live `timeline_cards` are kept
   fresh by the *legacy* ingest path (`upsert_timeline_card_from_session`,
   `store.py:1835/1938`), not by the archive projector.

4. **Detail/search still read the monolith** via `get_db()` (`agents_search.py`,
   `agents_sessions.py`). They serve almost entirely from **structured event
   columns** (`content_text`, `tool_name`, `tool_input_json`, `tool_output_text`)
   — NOT from raw bytes. **One exception (the landmine):**
   `get_active_context_boundary` (`store.py:3117`) calls `decode_raw_json(event)`
   to parse `type`/`subtype` and find compaction boundaries. It is called at
   request time from session detail (`agents_sessions.py:938,1071`), search
   (`agents_search.py:132,241`), and workspace (`session_workspace.py:377`).
   This is the only request-time dependency on stored raw bytes.

### Size reality

`events` raw payload (`raw_json_z`) + the entire `source_lines` table are the
bulk of the 116 GB. Structured event columns are a small fraction. So: move raw
bytes to archive (done for new data), drop them from the monolith, and the
monolith shrinks to "small authoritative product/control + structured events +
FTS" — exactly the spec's hot-path goal, without a separate `derived.db`.

## End-State Architecture

```text
monolith.db (was longhouse.db, now small)
  sessions, timeline_cards, session_runtime_state, machines, device_tokens,
  control_commands/acks, archive_chunks + checkpoints,
  events (STRUCTURED columns + FTS, NO raw_json/raw_json_z),
  source_lines: DROPPED
archive/                # immutable zstd+checksum raw chunks — source of truth
derived.db              # DELETED, never wired
```

Invariant preserved: hot product/control + list/timeline paths never scan raw
tables (already true via hot-card listing). Detail/search read structured event
rows. Raw fidelity lives in the archive, replayable to rebuild structured rows.

## hatch codex Review Findings (2026-06-06, gpt-5.5 max/high)

Codex reviewed v1 of this plan and confirmed the ground truth, but found
**data-loss / regression holes that block implementation as originally drafted.**
Verified independently before incorporating:

- **CONFIRMED — `source_lines` is a live product dependency, not dead weight.**
  `export_session_jsonl` (`store.py:~3469`) reads `AgentSourceLine` +
  `decode_raw_json` at request time and is called by the export API
  (`agents_sessions.py:1176`), **session resume/continuity**
  (`session_continuity.py:128`), and archive bundles (`session_archive.py:97`).
  Dropping `source_lines` without an archive-backed transcript reconstruction
  breaks **resume**, which is launch-critical. → New prerequisite phase.
- **CONFIRMED — third raw-byte carrier: `session_observations.payload_json`.**
  Embeds raw JSON; read at request time (`provisional_events.py:333`,
  `session_observation_reducers.py:165`, `realtime_propagation.py:727`). The
  slim-monolith end state is NOT reached by dropping only `source_lines` +
  `events.raw_json_z`. Must be measured and handled.
- **CONFIRMED — legacy export will duplicate already-archived live data.** The
  exporter selects by row id and does not dedup against existing archive chunks
  written by live archive-primary ingest. Verification must reconcile by stable
  raw-record key, not just classify `legacy_export` vs `live_archive_primary`.
- **CONFIRMED — rewind/branch mechanics use `AgentSourceLine`** (`store.py:949-1007`,
  `1015-1055`, branch-prefix copies `1180-1271`). Dropping the table needs a
  slim source-line index replacement or archive-aware branch logic.
- **CONFIRMED — Phase E/F ordering bug.** Startup recreates `source_lines`
  (`database.py:1454-1516`) and `_auto_add_missing_columns` re-adds raw columns
  from the model (`database.py:1697-1789`). Code/schema must become
  raw-table-tolerant *before* the clean DB omits them. Model change precedes drop.
- **CONFIRMED — Phase C before B.** `archive_root` defaults beside the DB on the
  constrained volume; confirm off-volume archive location *before* exporting 116 GB.
- **NOTED — no direct `raw_json` API exposure** to web/iOS; event responses are
  structured (`session_views.py:913-951`, `build_event_response:1589-1639`). Good.

Verdict: direction right, **not safe as written**. Revised plan below.

## Plan (revised post-review)

Seven phases. The big change vs v1: an **archive-backed transcript-reconstruction
path is a hard prerequisite** before any raw drop, because resume/export depend
on raw `source_lines`. And we must handle three raw carriers, not two.

### Phase A — Kill the request-time raw-bytes reads (two carriers)

Two request-time reads of *stored* raw bytes block a clean drop:

**A1. Compaction-boundary detection** (`get_active_context_boundary`,
`store.py:3117`).

- Add a structured column to `events`: `compaction_kind TEXT NULL`
  (values: `summary`, `compact_boundary`, `microcompact_boundary`, NULL).
  Plain nullable add → `_auto_add_missing_columns()` handles it at startup
  (per CLAUDE.md learnings), no migrator entry.
- Populate it at ingest time from the same parse that already happens in
  `_is_compaction_boundary_raw_json`, computed from `event_data.raw_json`
  *before* the raw bytes are dropped.
- Rewrite `get_active_context_boundary` to filter on
  `compaction_kind IS NOT NULL` and stop calling `decode_raw_json`.
- One-time backfill CLI (`archive backfill-compaction-kind`) to set the column
  for existing system events from their stored raw_json (while it still exists).
- Test: boundary detection returns identical results before/after on a fixture
  with compaction markers; detail/search active-context projection unchanged.
- Populate `compaction_kind` in **every** insert path: direct event projection
  (`store.py:1627-1652`) and the observation reducer path
  (`session_observation_reducers.py:85-161`). When `write_legacy_raw=False`,
  `compaction_kind` must be carried as structured data, not recomputed from raw.

**A2. Observation payloads** (`session_observations.payload_json`, read by
`provisional_events.py:333`, `session_observation_reducers.py:165`,
`realtime_propagation.py:727`). First **measure** how much of the 116 GB is
observation payload. Then ensure the structured fields these readers need are
materialized so `payload_json` raw embedding can be dropped/compacted. If
observation payloads are small and transient (runtime/heartbeat scratch), they
may simply be excluded from the reclaim drop with a documented TTL/cleanup
instead of a schema change — decide after measuring.

Exit: grep proves no request-time path calls `decode_raw_json` on stored
event/source rows and no reader depends on raw embedding in `payload_json`.
(Ingest-time parsing of *incoming* `event_data.raw_json` is fine — that's the
source, not the store.)

### Phase A′ — Archive-backed transcript reconstruction (resume/export)

**Hard prerequisite for any `source_lines` drop.** `export_session_jsonl`
rebuilds the provider transcript from `source_lines` raw rows and feeds resume
(`session_continuity.py:128`), the export API, and archive bundles. Before the
table can go:

- Add an archive-backed reconstruction: `export_session_jsonl` reads the
  session's sealed archive `source_lines` chunks (via `archive_chunks` manifest
  + `FilesystemArchiveStore.read_chunk`) instead of the monolith table, when the
  table row is absent. Keep the monolith path as the source of parity truth
  during validation.
- Parity test: archive-backed reconstruction byte-matches the monolith-backed
  `export_session_jsonl` output for a sample of sessions (incl. branched/rewound
  and heavy Codex sessions). This is the gate.
- **DECIDED (David, 2026-06-06): slim `source_lines` index.** Keep the table with
  `path/offset/branch_id/revision/line_hash` metadata only; raw bytes move to the
  archive. `export_session_jsonl` reconstructs raw from archive chunks joined on
  the slim index. Rewind/branch logic (`store.py:949-1271`) stays intact and
  operates on metadata. This removes the 100+ GB payload without rewriting the
  launch-critical resume/branch paths.

Exit: resume + export + archive-bundle work with `source_lines` raw bytes
absent, proven by parity tests.

### Phase B — Export the 116 GB raw history to the archive

The exporter already exists (`legacy_archive_exporter.py`, CLI
`archive export-legacy`). It is read-only, resumable, keyset-paginated, with
low-disk pause and corruption quarantine.

- **Backup gate (re-confirm):** the 2026-06-05 NAS off-volume backup at
  `.../reliability-data-plane-20260605/david010/...-consistent/` satisfies the
  spec's gate. Re-verify it still exists and counts match before exporting.
  Get explicit David approval to run the exporter against prod.
- Run `archive export-legacy --source-table source_lines` and
  `--source-table events` in a loop until `selected_rows == 0`, honoring the
  30 GB disk floor.
- **Dedup gap (codex):** the exporter does NOT skip rows already written to the
  archive by live archive-primary ingest. So Phase B will re-emit
  already-archived recent records. Before trusting the archive as complete,
  verification must **reconcile by stable raw-record key** (the same
  source_path/offset/sha or event_key the shadow writer uses), not merely
  classify `legacy_export` vs `live_archive_primary`. Either (a) make the
  exporter dedup-aware against `archive_chunks`, or (b) accept duplicate chunks
  and dedup at reconstruction/verification time. Pick (a) — cleaner and matches
  the idempotent chunk-key design already in `archive_shadow.py`.
- Verify: archive chunk integrity pass (checksums) + reconciliation proving
  every unique monolith raw record has a corresponding archive record, zero
  `unknown` legacy_ref shapes.

Exit: 100% of unique `source_lines` and `events` raw payloads exist in the
archive, verified by key reconciliation, with the monolith still fully intact.

### Phase C — Disk safety: archive lands off the monolith volume (BEFORE B)

Sequencing fix (codex): `archive_root` defaults *beside* the DB on the
constrained volume (`config/__init__.py:92-122`). Exporting 116 GB there could
fill the volume. So this is validated **before Phase B**, not after.

- Confirm `archive/` root location and free space; if it's on the same
  constrained volume, choose a target volume with David first.
- The clean-store rebuild (Phase E) is the reclaim path, not in-place VACUUM
  (runbook prohibits live VACUUM of the 116 GB DB).

### Phase D — Stop dual-writing raw bytes to the monolith

Once the archive holds everything and detail/search no longer need stored raw
bytes:

- Flip new ingest to **archive-only** for raw: disable legacy raw writes.
  Today this is gated (`LONGHOUSE_DISABLE_LEGACY_RAW_WRITES` /
  `LONGHOUSE_LEGACY_RAW_WRITE_ENABLED=0`). Set it, verify ingest still 200s with
  `X-Ingest-Legacy-Raw: disabled` and structured events still populate.
- Watch: `source_lines` stops growing; `events.raw_json_z` is NULL for new rows;
  structured columns + archive chunks both present; detail/search unaffected.

Exit: new raw bytes only go to the archive. Monolith stops growing in raw size.

### Phase E — Reclaim: code-tolerant first, then clean-store rebuild

Gated on David's explicit approval per `archive-decommission-plan.md`.

**E1. Make code/schema tolerant of missing raw (BEFORE any drop).** Ordering fix
(codex): otherwise startup recreates `source_lines` (`database.py:1454-1516`) and
`_auto_add_missing_columns` re-adds raw columns from the model
(`database.py:1697-1789`).
- Remove `raw_json`/`raw_json_z`/`raw_json_codec` from `AgentEvent` model and the
  startup recreate/auto-add for them.
- Replace `AgentSourceLine` raw table with the slim index from Phase A′ (or drop
  entirely if reconstruction is fully archive-backed). Update
  `database.py`/`db_migrations.py` source-line creation accordingly.
- Handle `session_observations.payload_json` per Phase A2 decision (compact or
  TTL-cleanup).

**E2. Clean-store rebuild (codex: spec it properly).** The runbook restore drill
only creates *minimal* sessions; production reclaim must preserve ALL
control/product state. Build the new slim DB by **copying the existing structured
+ control tables** (sessions, timeline_cards, session_runtime_state, machines,
device_tokens, control_commands/acks, archive_chunks/checkpoints, events
structured columns + FTS, runtime/auth/user tables, `sqlite_sequence`) while
omitting raw payloads — preserving event ids, ordering, indexes, triggers, FTS.
Do NOT rebuild structured events from archive replay (avoids the hash-ordering
tie-break risk entirely); the structured rows already exist and are authoritative.
- Swap via documented stop → move-aside → start → smoke → retain.
- Keep old monolith + NAS backup + archive for the retention window.
- Smoke: timeline, detail, search, FTS, health, launch, heartbeat, control all
  green; resume works (archive-backed); `make qa-live` david010 = 12/12.

Exit: monolith is small; raw bytes live only in the archive; disk reclaimed.

### Phase F — Delete the switchover scaffolding (one clean path)

Once archive-only raw + structured-served detail/search is the *only* path,
remove the migration machinery. Full inventory below.

## Scaffolding Deletion Inventory (Phase F)

Remove all of the following and collapse to a single path.

**`config/__init__.py`**
- Settings fields: `archive_shadow_write_enabled`, `archive_primary_write_enabled`,
  `legacy_raw_write_enabled`, `archive_shadow_chunk_target_bytes`. Keep
  `archive_root`; keep one tenant-id source if archive paths still need it.
- Env resolution lines `557-560`, `562`, and the
  `LONGHOUSE_DATA_ROOT`/`LONGHOUSE_HOT_DATABASE_URL`/`LONGHOUSE_DERIVED_DATABASE_URL`
  split logic in `_resolve_data_plane_settings` (`101-122`) — collapse to a
  single archive-root resolution. Remove `hot_database_url`/`derived_database_url`.

**`routers/agents_ingest.py`**
- The whole shadow-vs-primary branch: `_prepare_archive_primary_before_ingest`,
  `_write_shadow_archive_after_ingest(_locked)`, `_archive_shadow_session_lock`,
  the `archive_primary_state`/`legacy_raw_effective` state machine (`838-992`),
  the `768-773` validation guard, and the `X-Ingest-Archive-Primary` /
  `X-Ingest-Legacy-Raw` headers. Archive write becomes an unconditional step.

**`services/archive_shadow.py`**
- Remove `force_enabled` and the `archive_shadow_write_enabled` gate; this
  becomes the single archive-write helper (or fold into ingest).

**`services/agents/store.py`**
- `ingest_session` params `write_legacy_raw` and `raw_source_archived`: remove.
  Raw bytes are no longer written to the monolith at all → delete the
  `raw_json`/`raw_json_z` write branches (`1620-1663`, `1755-1786`) and the
  branch-copy raw fields (`1223-1224`, `1263-1264`). Keep `synchronous_projections`
  only if still meaningful; otherwise inline.
- After Phase A: `get_active_context_boundary` no longer touches raw_json.

**`models/agents.py`**
- `AgentEvent.raw_json`, `raw_json_z`, `raw_json_codec`: remove after Phase E.
- `AgentSourceLine` model + table: remove after Phase E.

**`services/raw_json_compression.py`**
- `decode_raw_json` / `decompress_raw_json` keep ONLY if the exporter/restore
  still needs them; otherwise delete. (Exporter reads legacy rows — but after
  reclaim there are none, so this can go once export is done.)

**Engine flags to retire**
- `LONGHOUSE_ARCHIVE_SHADOW_WRITE_ENABLED`, `LONGHOUSE_ARCHIVE_PRIMARY_WRITE_ENABLED`,
  `LONGHOUSE_LEGACY_RAW_WRITE_ENABLED`, `LONGHOUSE_DISABLE_LEGACY_RAW_WRITES`,
  `LONGHOUSE_ARCHIVE_SHADOW_CHUNK_TARGET_BYTES`, `LONGHOUSE_DATA_ROOT`,
  `LONGHOUSE_HOT_DATABASE_URL`, `LONGHOUSE_DERIVED_DATABASE_URL`. Remove from any
  deploy/compose/control-plane env and from docs.

**Dead code to delete**
- `data_plane.py` hot/derived store factories + `initialize_hot_database` /
  `initialize_derived_database` / derived schema. Keep only `create_archive_store`.
- `services/archive_derived_projector.py` — delete (never served anything).
- `archive_hot_projector.py`: per E2 we do NOT rebuild from archive (we copy
  structured tables), so this is delete-able unless the restore *drill* test
  still exercises it. Decide during F; lean delete.

**Additional scaffolding codex flagged (don't miss in F)**
- `session_observation_reducers.py` raw reducers + `AgentSourceLine` imports.
- `session_observation_rebuild.py` rebuild path and its CLI dependency.
- `database.py` startup `source_lines` creation + raw-column auto-add.
- `db_migrations.py` source-line rebuild migration.
- `legacy_archive_exporter.py` + `archive export-legacy` CLI: retire AFTER reclaim
  completes (one-shot tool). Decide retention of `ArchiveExportCheckpoint` /
  `ArchiveExportQuarantine` tables.
- `decode_raw_json`/`decompress_raw_json` (`raw_json_compression.py`): the
  exporter and archive-backed reconstruction still need decompress; keep the
  decompress side, drop compress-on-monolith-write usage.
- Tests asserting raw columns exist (incl. `raw_json_z` integration tests).

**Tests**
- `test_archive_shadow.py`: drop shadow/primary/fallback flag tests; keep a
  single "ingest writes archive chunk" test.
- `test_data_plane_stores.py`: delete (hot/derived store resolution gone).
- Remove `X-Ingest-Archive-Primary`/`X-Ingest-Legacy-Raw` assertions and
  `write_legacy_raw`/`synchronous_projections` test params.

## Sequencing rationale (the hash-ordering loose end, #4)

`_stable_source_seq`/`_stable_event_seq` derive `source_seq` from a content
hash. This is fine and **stays** — it only governs chunk *grouping/idempotency*,
not display order. Detail/search order events by `timestamp, id` from the
structured `events` rows (unchanged), never by `source_seq`. Because we are NOT
serving reads from archive replay (slim-monolith choice), there is no
hash-vs-realtime ordering conflict to resolve. The only consumer of `source_seq`
ordering is archive replay during the Phase E rebuild, which reconstructs
structured rows whose own `timestamp` then drives display order. Document this
and add a test asserting replay→structured-rows preserves transcript order on a
fixture; no code change required.

## Risks & Mitigations

- **Dropping raw bytes is irreversible-ish.** Mitigated by: archive holds them
  (verified Phase B), NAS backup retained, old monolith retained for a window.
- **Active-context regression.** Phase A ships and is verified *before* any raw
  drop; boundary detection parity test is the gate.
- **Disk during reclaim.** Clean-store rebuild on a target volume, never live
  VACUUM (runbook prohibition).
- **Shared worktree / parallel agents.** Commit only touched paths; anchor any
  deploy claim to exact SHA.

## First-Principles Architecture Gate (hatch codex xhigh, 2026-06-06)

A from-scratch architecture review (not just plan validation) on the live-measured
116 GB confirmed the end state but found two **production-data-correctness bugs**.
Live-measured raw carriers: `source_lines.raw_json_z` ≈48 GB,
`session_observations.payload_json` ≈27 GB (uncompressed; ~96% `provider_event`),
`events.raw_json_z` ≈13 GB — ~88 GB of redundant raw stored up to 3× plus the
archive.

**Confirmed bugs (verified in code):**

1. **Slim index won't exist under archive-only ingest.** `store.py:1758`
   (`if write_legacy_raw and observation_result.inserted:`) skips the
   `AgentSourceLine` insert entirely when raw writes are off. The slim index the
   whole plan relies on would be absent for new ingest. Fix: always write the
   slim row; gate only the raw payload columns on `write_legacy_raw`.
2. **Archive byte lookup keyed wrong (in committed PR2).**
   `archive_transcript.py` keyed by `(source_path, source_offset)` + highest
   hash-`source_seq`. Multiple revisions share an offset (rewrite/branch), so it
   can return the WRONG raw line. Fix: key by `line_hash` (= sha256 of raw
   bytes); the `source_lines` dedup index already includes `line_hash`.

**Correctness invariants (must hold before any reclaim):**

- `source_lines` stays the authoritative ordering/branch/revision index; archive
  bytes are addressed by `(session_id, source_path, source_offset, line_hash)`,
  never by hash-`source_seq` and never by offset alone.
- `events` is durable *serving* state with stable autoincrement ids/FTS rowids.
  Do NOT rebuild `events` from archive replay (id/identity drift breaks UI refs,
  tool pairing, active-context). Restore `events` from DB backup; archive only
  rebuilds raw transcript + (via slim index) export/resume.
- Backup story changes: a tenant is now `DB + archive/`. Backup/restore must
  snapshot both; restore verification checks `file_sha256` + `payload_sha256` +
  per-record raw hash before any raw SQLite reclaim.

**Observation ledger decision (David-approved, codex-confirmed):** archive becomes
the SOLE durable raw transcript source. `provider_event` / `provider_source_line`
observations demote to a transient projection buffer, pruned only after the row
is both projected AND archive-sealed. `rebuild-from-ledger` for transcript is
retired in favor of archive-backed rebuild. Bridge/runtime/client/server
observations are NOT transcript raw — keep them with bounded retention (several
product paths read them: `session_turns.py`, `session_runtime.py`,
`client_render_observations.py`, `realtime_propagation.py`). All of this is
destructive and lives **behind the approval gate**.

**Re-scoped PR3 = the correctness gate PR (non-destructive, safe now):**
- always write slim `source_lines` rows even when `write_legacy_raw=False`;
- key archive export lookup by `line_hash` (fixes committed PR2);
- add a row-level verifier: every reclaim-candidate `source_lines` row has a
  verified archive record matching `(session_id, source_path, source_offset,
  line_hash)`;
- export parity tests over rewrites, branch copies, duplicate offsets, multiple
  paths, legacy rows.

The single most important thing before touching production data: **prove
byte-identity at the `source_lines` row level via `line_hash`.** Without it,
reclaim can silently produce valid-looking but wrong resumed transcripts.

## Execution Sequence (hatch codex-directed, David-approved)

Decisive, non-bundled, reversible at every step. **STOP before Phase B.**

1. **PR1 — A1 `compaction_kind`.** Add+backfill column, populate on every write
   path, switch `get_active_context_boundary` off raw reads. Raw columns stay
   intact. Smallest high-value reversible increment.
2. **PR2 — A′ archive-backed export read path (non-destructive).** Add
   archive-backed reconstruction in `export_session_jsonl` with parity tests vs
   today's monolith/raw export. Keep monolith fallback. Do NOT change
   `source_lines` shape.
3. **PR3 — A2 measurement + non-destructive observation handling.** Measure
   `payload_json` share of the 116 GB on the host (`zerg` SSH alias →
   runtime-host), then implement the narrowest handling. Design may proceed in
   parallel; final shape gated on measurement.
4. **STOP — human approval gate.** No prod export (B), off-volume archive
   confirm (C), legacy-write disable (D), slim-index conversion / raw-column drop
   / reclaim (E), or scaffolding deletion (F) without explicit David approval.

A′ split clarified: the archive-backed *read path* + parity tests are safe now
(PR2). The slim-index *conversion* (dropping `source_lines` raw columns) is
destructive and belongs after B/E approval — raw bytes must be verified in the
archive first.

## Review & Approval Gates

1. hatch codex review of THIS plan before any code. ← next step
2. David approval before Phase B exporter runs against prod.
3. David approval before Phase E reclaim (exact paths, backup id, rollback cmds).

## Resolved by review

- ~~Keep `archive_hot_projector.py`?~~ → E2 copies structured tables, doesn't
  replay; lean delete.
- ~~Other `source_lines` consumers?~~ → Yes: export/resume/archive-bundle
  (Phase A′) and rewind/branch (slim index). Addressed.
- ~~iOS/web `raw_json` API exposure?~~ → None found; responses are structured.

## Open Questions for Review (round 2)

- Slim `source_lines` index vs fully archive-backed branch reconstruction — which
  does David prefer? Slim index is lower-risk and keeps rewind logic intact.
- How much of the 116 GB is `session_observations.payload_json`? Drives whether
  A2 is a schema change or a TTL-cleanup. Measure on host first.
- Exporter dedup-aware (option a) is the chosen path — confirm before building.
