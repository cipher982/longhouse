# Continuity Memory Boundary Tasks

Status: In progress
Spec: `docs/specs/continuity-memory-boundary.md`
Last updated: 2026-03-17

## Phase 0: Spec

- [x] Add `TODO.md` tracking entry
- [x] Write boundary spec with explicit keep / move / hide decisions
- [x] Write granular task checklist
- [x] Commit Phase 0 artifacts

## Phase 1: Move ops alerts out of insights

- [ ] Add a tenant-local operational incident model plus startup-safe SQLite migration
- [ ] Add a minimal admin/reliability read API for recent incidents
- [ ] Repoint `check_stale_agents` to incidents instead of `Insight`
- [ ] Repoint `ingest_health` to incidents instead of `Insight`
- [ ] Stop backfilling or creating new system-origin insight rows for these jobs
- [ ] Add/adjust focused tests for incident creation, dedup, and reads
- [ ] Commit Phase 1

## Phase 2: Remove planning artifacts from continuity context

- [ ] Stop including approved proposals in briefing assembly
- [ ] Remove or hide the `/proposals` route from the primary browser product surface
- [ ] Tighten any remaining proposal copy so it reads as admin/internal tooling only
- [ ] Add/adjust focused tests for the trimmed briefing composition
- [ ] Commit Phase 2

## Phase 3: Add minimal insight curation

- [ ] Add `archived_at` or equivalent active-state field to `Insight`
- [ ] Exclude archived insights from `query_insights` and briefing gotcha reads by default
- [ ] Add archive/unarchive browser/API actions for insights
- [ ] Add a minimal browser insight-management surface reachable from continuity-adjacent UI
- [ ] Add/adjust focused tests for archive/unarchive behavior
- [ ] Commit Phase 3

## Phase 4: Docs, reflection posture, and verification

- [ ] Update `AGENTS.md` product-surface copy for the final boundary
- [ ] Update `VISION.md` or other user-facing docs if needed
- [ ] Ensure reflection/proposal docs describe them as optional admin tooling, not core product
- [ ] Run local verification for touched backend/frontend paths
- [ ] Commit Phase 4

## Phase 5: Ship and verify

- [ ] Push `main`
- [ ] Wait for required CI/build workflows
- [ ] Deploy hosted surfaces if image paths changed
- [ ] Reprovision `david010`
- [ ] Verify hosted health plus `make qa-live`
- [ ] Update this task doc with final status notes
- [ ] Commit any last status/doc updates if needed

Notes:
- Keep this bounded. Do not redesign reflection into a full new workflow in the same pass.
- The goal is a cleaner product boundary, not a richer feature set.
