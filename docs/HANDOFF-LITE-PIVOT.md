# Zerg SQLite-Lite Pivot — Technical Handoff (2026-01-31)

> **Status: ARCHIVED (2026-02-01)** — This handoff is complete. All phases implemented.
> See `VISION.md` (SQLite-only OSS Pivot section) for current state.

## TECHNICAL DNA DISCOVERED

- SQLite boot is now supported: `make_engine` accepts sqlite URLs and startup no longer blocks SQLite.
- Schema handling is conditional (`DB_SCHEMA` / `AGENTS_SCHEMA` → `None` on SQLite) with schema translate map.
- Agents models are SQLite-compatible (GUID TypeDecorator, JSON with variants, partial indexes include `sqlite_where`).
- AgentsStore uses dialect-aware upsert; `require_postgres()` guard removed.
- SQLite minimum version is enforced at startup: **3.35+** (RETURNING required for job claiming).
- ✅ Job claiming now uses dialect-aware `commis_job_queue.py` (SQLite uses `UPDATE ... RETURNING`).
- ✅ Checkpointer uses `SqliteSaver` for SQLite (durable checkpoints).
- ✅ CLI has `zerg serve` command with lite mode defaults.
- Lite test suite is default (`make test`), expanded to cover SQLite boot + agents + GUID + version checks.

## CURRENT SYSTEM STATE

- **All phases complete** (0-7: SQLite boot, models, agents, job queue, checkpoints, CLI, onboarding).
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

### Phase 4 — Job Queue + Concurrency — ✅ Complete
- **Files**: `services/commis_job_queue.py` (new), `services/commis_job_processor.py`.
- **Implementation**:
  - Dialect-aware job claiming: Postgres uses `FOR UPDATE SKIP LOCKED`, SQLite uses `UPDATE ... RETURNING`.
  - Heartbeat + stale job reclaim implemented for both dialects.
  - Worker ID tracking for job ownership.

### Phase 5 — Durable Checkpoints (SQLite) — ✅ Complete
- **File**: `services/checkpointer.py`.
- **Implementation**: Uses `langgraph-checkpoint-sqlite` (`SqliteSaver`) for SQLite, `AsyncPostgresSaver` for Postgres.

### Phase 6 — CLI + Frontend Bundle — ✅ Complete
- **Files**: `cli/serve.py`, `cli/main.py`.
- **Implementation**: `zerg serve` command with lite mode defaults (SQLite, auth disabled, single tenant).

### Phase 7 — Onboarding Smoke — ✅ Complete
- README quick-start defaults to SQLite.
- `make onboarding-sqlite` validates SQLite boot.

## COST & RISK REALITIES

- **SQLite single-writer**: concurrency is limited; must serialize writes and use busy_timeout to avoid SQLITE_BUSY.
- **WAL checkpoint starvation**: long-lived readers can grow WAL unbounded; need checkpoint policy.
- **Schema removal**: SQLite has no schemas; any schema-qualified SQL must be rewritten.
- **Legacy suite drift**: Postgres tests remain but are heavy; keep running in CI for regression coverage.
- **Packaging**: bundling frontend dist increases package size; ensure entrypoints still work.

## TESTING STATUS

- `make test` (lite) runs fast and passes (no xfails).
- Legacy suite still runnable via `make test-legacy` / `run_backend_tests.sh`.
