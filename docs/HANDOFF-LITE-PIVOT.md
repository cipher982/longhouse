# Zerg SQLite-Lite Pivot — Technical Handoff (2026-01-31)

## TECHNICAL DNA DISCOVERED

- SQLite is currently hard-blocked in `zerg.database.make_engine` and startup enforces Postgres in `zerg.main.lifespan` (now removed; replaced by warning).
- Schemas are hard-coded: `DB_SCHEMA="zerg"` and agents schema `"agents"`; raw SQL references schema-qualified tables (e.g., `ops.job_queue`). SQLite cannot handle schemas.
- Agents models use Postgres-only types (`UUID`, `JSONB`) and schema-scoped metadata, so SQLite table creation fails.
- AgentsStore uses `sqlalchemy.dialects.postgresql.insert` for upsert and is not SQLite-safe.
- Job claiming uses `FOR UPDATE SKIP LOCKED` in both `commis_job_processor` and `jobs.queue`.
- Advisory locks and `with_for_update` appear in `single_tenant`, `fiche_locks`, `commis_resume`, `email_tools`, `sms_tools`.
- Checkpointer: SQLite currently uses `MemorySaver` (non-durable); Postgres uses AsyncPostgresSaver.
- CLI has no `zerg serve`; backend expects `static/` at repo root but frontend dist is at `apps/zerg/frontend-web/dist`.
- Test suite was Postgres-heavy and slow; created a SQLite-lite suite as default (`make test`) for this pivot.

## CURRENT SYSTEM STATE

- **Phase 1 (SQLite boot)** completed:
  - SQLite URLs now allowed in `make_engine`.
  - SQLite pragmas configured: WAL + busy_timeout + foreign_keys + synchronous + optional wal_autocheckpoint.
  - Postgres-only startup guard removed; now warns in SQLite mode.
  - `_apply_search_path` is a no-op on sqlite.
- **Lite test suite** added and default:
  - `make test` → `test-lite` → `apps/zerg/backend/tests_lite`.
  - Legacy suite remains opt-in: `make test-legacy`.
- **Tests added (lite)**:
  - `test_sqlite_engine.py` (passes)
  - `test_initialize_database_sqlite.py` (xfail)
  - `test_agents_models_sqlite.py` (xfail)
  - `test_agents_ingest_sqlite.py` (xfail)
- **Docs updated**:
  - `docs/LIGHTWEIGHT-OSS-ONBOARDING.md` now includes findings, detailed plan, prior art, key learnings.

### Commits to reference
- `d59c1989` feat: allow sqlite boot with pragmas
- `92ef9576` test: add lite suite and make it default
- `9524a32b` test: add sqlite-lite backlog tests (xfail)

## WHAT WE'RE BUILDING & WHY

- **Goal**: `pip install zerg && zerg serve` launches a SQLite-only, single-node agent ops center with no external dependencies.
- **Why**: The OSS onboarding must be <5 minutes with low resources; Postgres/Docker are overkill for 1-user, 5–10 commis workloads.
- **Approach**: Make backend SQLite-safe (schema + types + concurrency patterns), add SQLite-compatible job queue semantics, and package the frontend into the python distribution.

## CONCRETE IMPLEMENTATION PLAN

### Phase 2 — Model Compatibility (Core + Agents)
- **Files**: `apps/zerg/backend/zerg/models/agents.py`, `models/device_token.py`, `models/llm_audit.py`, `models/run.py`, `models/models.py`, `database.py`.
- **Changes**:
  - Replace `UUID` columns with `String(36)` + Python defaults (UUID4).
  - Replace `JSONB` with `JSON().with_variant(JSONB, "postgresql")` or plain `JSON`.
  - Replace `gen_random_uuid()` server defaults with Python defaults on sqlite.
  - Make `agents` metadata schema conditional (no schema on sqlite).
  - Ensure partial indexes include `sqlite_where` or drop for sqlite.
- **Tests to flip**: `test_initialize_database_sqlite.py`, `test_agents_models_sqlite.py`.

### Phase 3 — Agents Ingest SQLite
- **Files**: `apps/zerg/backend/zerg/services/agents_store.py`, `routers/agents.py`, agents migrations.
- **Changes**:
  - Dialect-aware upsert: use sqlite `insert().on_conflict_do_nothing()` or catch `IntegrityError`.
  - Remove `require_postgres()` guard (keep single-tenant guard if desired).
- **Test to flip**: `test_agents_ingest_sqlite.py`.

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

- `make test` (lite) runs fast: 2 pass, 3 xfail.
- Legacy suite still runnable via `make test-legacy` / `run_backend_tests.sh`.
