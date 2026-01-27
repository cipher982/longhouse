# Oikos + Run Rename Spec (Post-French Rebrand)

**Date:** 2026-01-26
**Repo:** `/Users/davidrose/git/zerg-rebrand`
**Branch:** `rebrand/french-terminology`

## Purpose
Finalize terminology after the French rebrand by replacing:
- **Concierge -> Oikos** (orchestrator, UI + service)
- **Course -> Run** (execution record)
- **Jarvis -> Oikos** (UI name and user-facing label)

Keep:
- **Fiche** (definition card)
- **Commis** (worker job)

This is a full rename with **no backward compatibility**. All user-facing strings, routes, and UI labels should use the new terms.

## Goals
- Replace every occurrence of **course** (and variants) with **run**.
- Replace every occurrence of **concierge** (and variants) with **oikos**.
- Replace every occurrence of **jarvis** (and variants) with **oikos** where it is a UI name/label or service concept.
- Keep **fiche** and **commis** unchanged.
- Update all layers (DB, API, WS/SSE, frontend, tests, docs, scripts).
- Regenerate contracts/types after schema changes.
- Maintain a clean linear history with clear commit chunks.
- Treat **Oikos** as a singular proper name; avoid pluralization. If plurality is needed, use “Oikos instance(s)” or “operators.”

## Non-Goals
- No architectural refactor beyond what is required for renaming.
- No behavioral changes.
- No compatibility shims or legacy endpoints.

## Terminology Mapping
| Role | Old (current branch) | New (final) |
|------|-----------------------|-------------|
| Orchestrator (UI + service) | Jarvis / Concierge | Oikos |
| Run | Course | Run |
| Worker job | Commis | Commis |
| Definition card | Fiche | Fiche |

## Affected Surfaces
### Backend
- Models, CRUD, services, routers, prompts
- Events, metrics, usage, reliability, ops
- DB schema + Alembic migrations

### API + Contracts
- REST routes (`/courses` -> `/runs`)
- OpenAPI schemas and generated types
- WS/SSE event payload fields and wrappers

### Frontend
- UI labels (Oikos replaces Jarvis; Run replaces Course)
- API client methods + hooks
- Components/stores related to courses/concierge/jarvis

### Tests + Docs + Scripts
- Unit/integration/E2E tests
- Docs/specs/task notes
- Debug/seed/migration scripts

## Migration Strategy (Confirmed)
- The existing full-rename migration **has NOT been applied**.
- Rewrite `apps/zerg/backend/alembic/versions/f1a2b3c4d5e6_full_rename_tables.py`
  to rename `agent_runs -> runs` directly (never create `courses`).
- Update any references to `courses` in migrations to `runs` as needed.

## Validation
- Regenerate WS/SSE types, tool definitions, OpenAPI types.
- Run repo hooks / validations (ruff, TS, contract drift, AsyncAPI).
- Run `make test` only if requested (large suite).

## Rollback
- Since migration is not applied, rollback is a git revert (no DB state to unwind).

## Update Log
- 2026-01-26: Spec created with confirmed goals, scope, and migration strategy.
- 2026-01-26: Clarified that **Oikos replaces Jarvis** everywhere (UI + services) and is treated as a singular proper name.
