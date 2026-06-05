# Reliability Data Plane Tasks

Due: none
Area: longhouse
Workspace: /Users/davidrose/git/_wt/longhouse-reliability-data-plane
Status: Phase 5 legacy exporter in progress

This task file tracks the SDP-1 epic for separating hot product/control state
from raw archive and derived search/detail state.

Spec: `docs/specs/reliability-data-plane.md`

## Phase 0: Spec and Review

- [x] Create worktree `epic/reliability-data-plane`.
- [x] Consult Hatch Expert for architecture refinement.
- [x] Write persistent spec.
- [x] Commit Phase 0 spec.
- [x] Run Hatch Opus review of spec.
- [x] Incorporate review feedback or record why not.
- [x] Pause for maintainer review.

## Phase 1: Hot-Path Guardrails

- [x] Add or verify DB-session release before queued writes across hot routes.
- [x] Add pool checkout/write timing visibility where missing.
- [x] Add real concurrent saturation integration test with health/list/launch/heartbeat.
- [x] Add route-level writer saturation guard tests for health/list/launch/heartbeat.
- [x] Migrate `/api/agents/presence` away from request-session-held serialized writes.
- [x] Gate hot endpoint access to raw archive/search/large event tables.
- [ ] Remove no-query session-list dependency on `events.content_text` after legacy preview backfill.
- [x] Use hot card previews for new/backfilled list rows; keep bounded legacy fallback for NULL previews.
- [x] Verify cheap diagnostics avoid full DB scans; fix `zerg-ops report` empty-archive handling.
- [ ] Centralize request-session-release/post-write helper and stop passing closed request DB handles to dispatch helpers.

## Phase 2: Filesystem Archive Store

- [x] Add `ArchiveStore` interface.
- [x] Add `FilesystemArchiveStore`.
- [x] Add chunk writer/reader/verifier.
- [x] Add orphan recovery.
- [x] Add archive manifest/checkpoint models.
- [x] Add raw-byte, zstd, checksum, corruption, and recovery tests.

## Phase 3: Hot and Derived Store Skeletons

- [x] Add configurable `hot.db`, `derived.db`, and archive root paths.
- [x] Add separate factories/pools/serializers.
- [x] Add empty-store schema plus migration ledger.
- [x] Add derived-unavailable tests.

## Phase 4: Shadow Ingest and Projectors

- [x] Shadow-write new raw ingest to archive behind a flag.
- [x] Project archive chunks to hot cards.
- [x] Project archive chunks to derived events/search.
- [x] Add parser-revision checkpoints.
- [x] Add projector restart, duplicate, out-of-order, and large-session tests.

## Phase 5: Backup Gate and Legacy Exporter

- [x] Satisfy backup gate and record validation evidence in the spec.
- [x] Add read-only resumable legacy exporter.
- [x] Add export ledger.
- [x] Add low-disk pause.
- [x] Add corruption quarantine.
- [x] Add interrupted export and low-disk tests.

## Phase 6: Read Cutover

- [ ] Move session list to hot cards.
- [ ] Move timeline list to hot cards.
- [ ] Keep control/health/launch independent of derived/archive.
- [ ] Add tests proving hot endpoints work with derived DB locked/missing.
- [ ] Add tests proving hot endpoints do not query legacy cold tables.

## Phase 7: Archive-Primary Writes

- [ ] Make archive primary for new raw data behind a flag.
- [ ] Keep legacy raw fallback.
- [ ] Add rollback tests and runbook.

## Phase 8: Decommission Plan

- [ ] Restore from archive to clean stores.
- [ ] Smoke timeline/search/detail/control on restored data.
- [ ] Draft old DB retention/reclaim plan.
- [ ] Require explicit maintainer approval before deletion or compaction.
