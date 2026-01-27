# Oikos + Run Rename Task Log

**Date:** 2026-01-26
**Repo:** `/Users/davidrose/git/zerg-rebrand`
**Branch:** `rebrand/french-terminology`
**Spec:** `docs/work/2026-01-26-oikos-run-rename-spec.md`

## Status
- **Current phase:** Jarvis → Oikos pass completed; moving into migration + regen

## Progress Log
- 2026-01-26: Task doc created. Goals locked: Concierge -> Oikos, Course -> Run, Jarvis -> Oikos. Keep Fiche + Commis. Migration not applied; will rewrite full-rename migration.
- 2026-01-26: Renamed course/concierge file paths and applied initial text replacements. Paused to clarify Oikos singular usage and Jarvis removal.
- 2026-01-26: Confirmed **Oikos replaces Jarvis everywhere**, no Jarvis remnants. Oikos is singular; avoid pluralization (use “Oikos instance(s)” or “operators” if needed).
- 2026-01-26: Completed Jarvis → Oikos rename (paths + symbols), updated API paths to `/api/oikos/run` + `/api/oikos/thread`, fixed prompt composer/template collisions.
- 2026-01-26: Regenerated WS/SSE, OpenAPI, and tool definitions; OpenAPI required `DATABASE_URL` + `FERNET_SECRET` in env for generation.
- 2026-01-26: Fixed ruff rename fallout, ran pre-commit hooks, committed full rename.

## Checklist
### Phase A — Prep
- [x] Confirm repo + branch
- [x] Lock terminology (Oikos, Run, Commis, Fiche)
- [x] Confirm migration status (not applied)
- [x] Capture baseline status + file inventory

### Phase B — Repo-wide rename (code + routes)
- [x] Replace `course*` -> `run*` (symbols, routes, schemas, UI labels)
- [x] Replace `concierge*` -> `oikos*` (symbols, routes, schemas, UI labels)
- [x] Replace `jarvis*` -> `oikos*` where it is a UI/service name
- [x] Rename files/directories accordingly

### Phase C — DB migration rewrite
- [x] Rewrite `f1a2b3c4d5e6_full_rename_tables.py` to rename `agent_runs -> runs`
- [x] Update migration references from `courses` to `runs`

### Phase D — Regenerate + fix fallout
- [x] Regenerate WS/SSE types
- [x] Regenerate OpenAPI types (manual env vars for `DATABASE_URL` + `FERNET_SECRET`)
- [x] Regenerate tool definitions (from `schemas/tools.yml`)
- [ ] Fix typecheck/lint errors

### Phase E — Validate + Commit
- [x] Run repo hooks / validations (pre-commit)
- [x] Commit backend+schemas+migration
- [x] Commit frontend
- [x] Commit tests/docs

## Notes
- No backward compatibility.
- "Run" is user-facing (labels + routes).
- **Oikos is singular and replaces Jarvis in UI.** Avoid pluralization.
