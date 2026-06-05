# Reliability Data Plane Tasks

Due: none
Area: longhouse
Workspace: /Users/davidrose/git/_wt/longhouse-reliability-data-plane
Status: Phase 0 draft

This task file tracks the SDP-1 epic for separating hot product/control state
from raw archive and derived search/detail state.

Spec: `docs/specs/reliability-data-plane.md`

## Phase 0: Spec and Review

- [x] Create worktree `epic/reliability-data-plane`.
- [x] Consult Hatch Expert for architecture refinement.
- [x] Write persistent spec.
- [ ] Commit Phase 0 spec.
- [ ] Run Hatch Opus review of spec.
- [ ] Incorporate review feedback or record why not.
- [ ] Pause for maintainer review.

## Phase 1: Hot-Path Guardrails

- [ ] Add or verify DB-session release before queued writes across hot routes.
- [ ] Add pool checkout/write timing visibility where missing.
- [ ] Add tests for writer saturation with health/list/launch/heartbeat.
- [ ] Gate hot endpoint access to raw archive/search/large event tables.
- [ ] Add cheap diagnostics that avoid full DB scans.

## Phase 2: Filesystem Archive Store

- [ ] Add `ArchiveStore` interface.
- [ ] Add `FilesystemArchiveStore`.
- [ ] Add chunk writer/reader/verifier.
- [ ] Add orphan recovery.
- [ ] Add archive manifest/checkpoint models.
- [ ] Add raw-byte, zstd, checksum, corruption, and recovery tests.

## Phase 3: Hot and Derived Store Skeletons

- [ ] Add configurable `hot.db`, `derived.db`, and archive root paths.
- [ ] Add separate factories/pools/serializers.
- [ ] Add empty DB migrations.
- [ ] Add derived-unavailable tests.

## Phase 4: Shadow Ingest and Projectors

- [ ] Shadow-write new raw ingest to archive behind a flag.
- [ ] Project archive chunks to hot cards.
- [ ] Project archive chunks to derived events/search.
- [ ] Add parser-revision checkpoints.
- [ ] Add projector restart, duplicate, out-of-order, and large-session tests.

## Phase 5: Backup Gate and Legacy Exporter

- [ ] Satisfy backup gate and record validation evidence in the spec.
- [ ] Add read-only resumable legacy exporter.
- [ ] Add export ledger.
- [ ] Add low-disk pause.
- [ ] Add corruption quarantine.
- [ ] Add interrupted export and low-disk tests.

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
