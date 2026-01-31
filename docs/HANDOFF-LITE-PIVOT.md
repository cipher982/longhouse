# Zerg SQLite-Lite Pivot — Technical Handoff (2026-01-31)

## TECHNICAL DNA DISCOVERED

- SQLite boot is now supported: `make_engine` accepts sqlite URLs and startup no longer blocks SQLite.
- Schema handling is conditional (`DB_SCHEMA` / `AGENTS_SCHEMA` → `None` on SQLite) with schema translate map.
- Agents models are SQLite-compatible (GUID TypeDecorator, JSON with variants, partial indexes include `sqlite_where`).
- AgentsStore uses dialect-aware upsert; `require_postgres()` guard removed.
- SQLite minimum version is enforced at startup: **3.35+** (RETURNING required for job claiming).
- Job claiming still uses `FOR UPDATE SKIP LOCKED` in `commis_job_processor` and `jobs.queue` (Phase 4).
- Advisory locks and `with_for_update` remain in `single_tenant`, `fiche_locks`, `commis_resume`, `email_tools`, `sms_tools`.
- Checkpointer: SQLite still uses `MemorySaver` (non-durable); Postgres uses AsyncPostgresSaver.
- CLI has no `zerg serve`; backend expects `static/` at repo root but frontend dist is at `apps/zerg/frontend-web/dist`.
- Lite test suite is default (`make test`), expanded to cover SQLite boot + agents + GUID + version checks.

## CURRENT SYSTEM STATE

- **Phases 1–3 complete** (SQLite boot + model compatibility + agents ingest).
- **SQLite pragmas configured** (WAL, busy_timeout, foreign_keys, etc).
- **SQLite min version enforced**: 3.35+ (RETURNING support).
- **Lite test suite** is default:
  - `make test` → `apps/zerg/backend/tests_lite`
  - Legacy suite remains opt-in: `make test-legacy`.
- **Lite tests now pass** (no xfails) and cover:
  - SQLite engine + DB init
  - Agents models + ingest + dedupe
  - GUID round-trips (DeviceToken, Memory)
  - db_is_sqlite parsing (quoted URLs)
  - SQLite version enforcement

## WHAT WE'RE BUILDING & WHY

- **Goal**: `pip install zerg && zerg serve` launches a SQLite-only, single-node agent ops center with no external dependencies.
- **Why**: The OSS onboarding must be <5 minutes with low resources; Postgres/Docker are overkill for 1-user, 5–10 commis workloads.
- **Approach**: Make backend SQLite-safe (schema + types + concurrency patterns), add SQLite-compatible job queue semantics, and package the frontend into the python distribution.

## CONCRETE IMPLEMENTATION PLAN

### Phase 2 — Model Compatibility (Core + Agents) — ✅ Complete
- **Files**: `apps/zerg/backend/zerg/models/agents.py`, `models/device_token.py`, `models/llm_audit.py`, `models/run.py`, `models/models.py`, `database.py`.
- **Changes**:
  - Replace `UUID` columns with `String(36)` + Python defaults (UUID4).
  - Replace `JSONB` with `JSON().with_variant(JSONB, "postgresql")` or plain `JSON`.
  - Replace `gen_random_uuid()` server defaults with Python defaults on sqlite.
  - Make `agents` metadata schema conditional (no schema on sqlite).
  - Ensure partial indexes include `sqlite_where` or drop for sqlite.
- **Tests flipped**: `test_initialize_database_sqlite.py`, `test_agents_models_sqlite.py`.

### Phase 3 — Agents Ingest SQLite — ✅ Complete
- **Files**: `apps/zerg/backend/zerg/services/agents_store.py`, `routers/agents.py`, agents migrations.
- **Changes**:
  - Dialect-aware upsert: use sqlite `insert().on_conflict_do_nothing()` or catch `IntegrityError`.
  - Remove `require_postgres()` guard (keep single-tenant guard if desired).
- **Test flipped**: `test_agents_ingest_sqlite.py`.

### Phase 4 — Job Queue + Concurrency
- **Files**: `services/commis_job_processor.py`, `jobs/queue.py`, `services/commis_resume.py`, `services/single_tenant.py`, `services/fiche_locks.py`, `tools/builtin/email_tools.py`, `tools/builtin/sms_tools.py`.
- **Changes**:
  - Replace `FOR UPDATE SKIP LOCKED` with `BEGIN IMMEDIATE` + atomic UPDATE/RETURNING.
  - Add heartbeat + stale job reclaim logic.
  - Replace advisory locks with file locks or status-guarded updates.
  - Gate `ops.job_queue` to Postgres-only or disable in lite mode.

### Phase 5 — Durable Checkpoints (SQLite)
- **File**: `services/checkpointer.py`.
- **Change**: Use `langgraph-checkpoint-sqlite` instead of MemorySaver in sqlite.

### Phase 6 — CLI + Frontend Bundle
- **Files**: `cli/main.py`, `pyproject.toml`, `main.py` static mount.
- **Change**: Add `zerg serve`; package `apps/zerg/frontend-web/dist` into python package.

### Phase 7 — Onboarding Smoke
- Add `make onboarding-smoke` for SQLite boot + basic API checks (reuse lite tests).
- Update README quick-start to default to SQLite.

## COST & RISK REALITIES

- **SQLite single-writer**: concurrency is limited; must serialize writes and use busy_timeout to avoid SQLITE_BUSY.
- **WAL checkpoint starvation**: long-lived readers can grow WAL unbounded; need checkpoint policy.
- **Schema removal**: SQLite has no schemas; any schema-qualified SQL must be rewritten.
- **Legacy suite drift**: Postgres tests remain but are heavy; keep running in CI for regression coverage.
- **Packaging**: bundling frontend dist increases package size; ensure entrypoints still work.

## TESTING STATUS

- `make test` (lite) runs fast and passes (no xfails).
- Legacy suite still runnable via `make test-legacy` / `run_backend_tests.sh`.
