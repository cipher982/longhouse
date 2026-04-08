# Prelaunch Cleanup Tracker

Audit date: 2026-04-07. Goal: delete dead code, remove non-launch integrations, reduce confusion for fresh AI agents.

## Phase 1: Delete Dead Files (no risk)

- [ ] Remove tracked dead files: `agents_def/`, `libs/`, `forum.css`, `.nvmrc`
- [ ] Remove untracked junk: `.env-backups-2025-12-16/`, `.tmp/`, `archive/`, `app.db`, `qa-oss-server.log`
- [ ] Remove empty/legacy dirs: `static/avatars/`, `patrol/reports/`, `logs/jarvis-*`, `logs/postgres/`, `docs/reports/`
- [ ] Remove dead frontend files: `web/src/lib/fiches.ts`
- [ ] Remove legacy data dirs: `data/funnel.db`, `data/swarmlet/`, `data/workers/`
- [ ] Commit

## Phase 2: Remove Traccar Integration

Traccar is a David-specific personal tool, not OSS core. 19 files reference it.

- [ ] Remove traccar from connectors (registry, testers, status_builder)
- [ ] Remove traccar from schemas (bootstrap.py)
- [ ] Remove get_current_location from personal_tools.py
- [ ] Remove traccar from frontend (SettingsPage, connectors type, oikos config)
- [ ] Remove config/script files: `config/.traccar_config.json.example`, `server/scripts/test_traccar.py`
- [ ] Remove traccar from eval dataset and seed scripts
- [ ] Regenerate `web/src/generated/openapi-types.ts`
- [ ] Run `make test` to verify
- [ ] Commit

## Phase 3: CSS Cleanup (docketed)

Deferred to docket. Current CSS has dead files and naming mismatches (forum.css for timeline).

## Phase 4: Giant File Modularization (docketed)

Deferred to docket. Major files: shipper.rs (3496), agents_store.py (2862), session_chat.py (2230), session_turn_reviews.py (1786).
