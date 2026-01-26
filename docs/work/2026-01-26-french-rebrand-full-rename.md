# French Rebrand Full Rename (Phase 4+) — Task Log

**Date:** 2026-01-26
**Repo:** `/Users/davidrose/git/zerg-rebrand` (branch `rebrand/french-terminology`)

## Goal
Complete full French terminology renaming (no backward compatibility). Rename tables/columns, Python classes, API schemas, endpoints, tests, and frontend to Fiche/Course/Commis naming.

## Decisions
- **No backward compat**: old names removed (no aliases, no old endpoints).
- **DB rename strategy**: forward migration that renames existing tables/columns/indexes/enums.

## Status
- **Current phase:** Phase D/E — Verification and commits

## Progress Log
- 2026-01-26: Task doc created. Full-rename mode confirmed. Repo/branch verified.
- 2026-01-26: Inventory done — remaining Agent/Run/Worker references across backend, tests, tools, frontend, migrations.
- 2026-01-26: Updated `main.py` router imports/includes + jarvis endpoints to fiche/course naming. Adjusted MCP server routes to `/fiches/{fiche_id}/mcp-servers`.
- 2026-01-26: Renamed run history → course history, memory tools → fiche_memory, updated concierge/threads/connectors/websocket metrics to fiche/course terms. Cloud executor now uses run_commis.
- 2026-01-26: Reviewed prior Phase 4 note (backward-compat plan). Confirmed full-rename path only; no aliases or legacy endpoints.
- 2026-01-26: Backend models/services/routers/schemas updated to fiche/course/commis naming; ops/usage/reliability endpoints now return course/fiche metrics.
- 2026-01-26: Added forward-only Alembic migration to rename tables/columns/constraints for full rename.
- 2026-01-26: Regenerated WS/SSE types and tool definitions after schema changes.
- 2026-01-26: Frontend API migration complete (fiches/courses/commis), Reliability UI updated, commis E2E headers standardized.
- 2026-01-26: Renamed `worker_db.py` → `commis_db.py` and aligned X-Test-Commis usage across frontend + docs.
- 2026-01-26: Regenerated WS/SSE contracts + OpenAPI types (manual env for OpenAPI export).
- 2026-01-26: Resumed in worktree, reviewed Phase 4 blocked notes, confirmed full-rename direction (no backward compat) before continuing.
- 2026-01-26: Fixed MCP server payloads to include `transport` and dashboard websocket course `display_type` default; frontend typecheck + contract validation pass.
- 2026-01-26: Pre-commit hooks clean (ruff/format, types, lint, WS/SSE, AsyncAPI). Committed full rename: `3f90397`.

## Checklist
### Phase A — Inventory & plan
- [x] Confirm repo + branch
- [x] Capture current dirty state
- [x] Identify remaining old terminology in backend/frontend

### Phase B — Backend full rename
- [x] Models/tables/columns/relationships align with Fiche/Course/Commis
- [x] CRUD/services/routers updated (jarvis + main entrypoints)
- [x] Schemas updated
- [x] Generated WS/SSE types regenerated
- [x] Alembic migration added for renames

### Phase C — Frontend & client contracts
- [x] API endpoints updated (/fiches, /courses)
- [x] Types + client usage updated
- [x] UI copy updated

### Phase D — Tests & scripts
- [x] Tests updated for new terminology
- [x] Seed/debug scripts updated

### Phase E — Verification
- [ ] `make test`
- [ ] Fix failures
- [x] Commit(s)

## Notes
- Commit after big sets of changes (backend, migrations, frontend, tests).
