# Prelaunch Cleanup Tracker

Audit date: 2026-04-07. Goal: delete dead code, remove non-launch integrations, reduce confusion for fresh AI agents.

## Phase 1: Delete Dead Files — DONE

Commit: `bff3b8b5` (754 lines deleted)

- [x] Remove tracked dead files: `agents_def/`, `libs/`, `forum.css`, `.nvmrc`
- [x] Remove untracked junk: `.env-backups-2025-12-16/`, `.tmp/`, `archive/`, `app.db`, `qa-oss-server.log`
- [x] Remove empty/legacy dirs: `static/avatars/`, `patrol/`, `logs/jarvis-*`, `logs/postgres/`, `docs/reports/`
- [x] Remove dead frontend files: `web/src/lib/fiches.ts`
- [x] Remove legacy data dirs: `data/funnel.db`, `data/swarmlet/`, `data/workers/`

## Phase 2: Remove Traccar Integration — DONE

Commit: `d28c9784` (303 lines deleted, 15 files)

- [x] Remove traccar from connectors (registry, testers, status_builder)
- [x] Remove traccar from schemas (bootstrap.py)
- [x] Remove get_current_location from personal_tools.py
- [x] Remove traccar from frontend (SettingsPage, connectors type, oikos config)
- [x] Remove config/script files
- [x] Remove traccar from eval dataset and seed scripts
- [x] All pre-commit hooks pass, 1291 backend tests pass

## Phase 3: CSS Cleanup — Docketed

Docket item: `no-date--zerg--prelaunch-css-cleanup-and-naming.md`

## Phase 4: Giant File Modularization — Docketed

Docket item: `no-date--zerg--giant-file-modularization-prelaunch.md`
