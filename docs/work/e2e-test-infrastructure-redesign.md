# E2E Test Infrastructure Redesign

**Status**: Ready for Implementation
**Date**: 2026-01-01
**Reviewed by**: Codex (gpt-5.2)

## Executive Summary

The current E2E testing infrastructure has fundamental design issues causing intermittent failures and connection pool exhaustion. The "fix" of reducing Playwright workers from 16 to 2 is a band-aid that made tests 8x slower.

**Key insight from review**: The real structural problem is **engine-per-worker** design. Even with schema-exists checks, you still have N connection pools. The cleanest fix is one engine per uvicorn process with `search_path` set per-request.

---

## Root Causes (Validated)

### 1. Engine-Per-Worker Design (Primary)
Each Playwright worker gets its own SQLAlchemy Engine, creating `O(N)` connection pools instead of `O(1)`.

```python
# database.py - Current problematic pattern
_WORKER_ENGINES: Dict[str, Engine] = {}  # N engines = N pools
```

### 2. Cache Fragmentation (Multi-Uvicorn)
2 uvicorn workers = 2 separate in-memory caches. Both think they need to DROP+CREATE schemas.

### 3. Aggressive Schema Recreation
`recreate_worker_schema()` always DROP+CREATE, even when schema exists:
```python
conn.execute(text(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE"))  # ALWAYS drops!
```

### 4. Connection Pool Math
```
16 Playwright workers × 2 uvicorn × 15 connections/engine = 480 connections
Postgres default max_connections = 100
```

### 5. Stale/Broken Code (from review)
- `test-setup.js` still imports deprecated SQLite cleanup (`zerg.test_db_manager`)
- `ApiClient` helper doesn't set `X-Test-Worker` header
- Background tasks without `current_worker_id` hit default engine

---

## Implementation Plan

### Phase 1: Quick Wins (Reduce Concurrency)

| Task | File | Change |
|------|------|--------|
| 1.1 | `spawn-test-backend.js:97` | Set `uvicornWorkers = 1` |
| 1.2 | `database.py` | Reduce E2E pool size (pool_size=2, max_overflow=3) |
| 1.3 | Verify | Run tests with restored Playwright workers |

### Phase 2: Idempotent Schema Management

| Task | File | Change |
|------|------|--------|
| 2.1 | `e2e_schema_manager.py` | Replace `recreate_worker_schema` with `ensure_worker_schema` (CREATE IF NOT EXISTS + create_all checkfirst=True) |
| 2.2 | `database.py:215` | Call `ensure_worker_schema` instead of `recreate_worker_schema` |
| 2.3 | `test-setup.js` | Add suite-start cleanup: call Python to drop_all_e2e_schemas + pre-create schemas |
| 2.4 | `test-setup.js` | Remove deprecated SQLite cleanup imports |

### Phase 3: Fix Isolation Leaks

| Task | File | Change |
|------|------|--------|
| 3.1 | `tests/helpers/api-client.ts` | Add `X-Test-Worker` header to all requests |
| 3.2 | Audit | Check for background tasks hitting default engine |

### Phase 4: Cleanup & Verification

| Task | File | Change |
|------|------|--------|
| 4.1 | Remove | Delete unused SQLite isolation code |
| 4.2 | Test | Run full E2E suite 3x with CPU-count workers |
| 4.3 | Document | Update AGENTS.md with new test patterns |

---

## Technical Details

### Phase 1.1: Single Uvicorn Worker

```javascript
// spawn-test-backend.js - line 97
const uvicornWorkers = 1;  // Changed from: 2
```

### Phase 1.2: Reduce Pool Size

```python
# database.py - in make_engine()
def make_engine(db_url: str, **kwargs) -> Engine:
    # Reduce pool for E2E to prevent connection exhaustion
    if os.getenv("E2E_USE_POSTGRES_SCHEMAS"):
        kwargs.setdefault("pool_size", 2)
        kwargs.setdefault("max_overflow", 3)

    kwargs.setdefault("pool_pre_ping", True)
    kwargs.setdefault("pool_recycle", 300)
    return create_engine(db_url, **kwargs)
```

### Phase 2.1: Idempotent Schema Ensure

```python
# e2e_schema_manager.py - new function
def ensure_worker_schema(engine: Engine, worker_id: str) -> str:
    """
    Idempotent schema creation. Never DROP during test execution.
    Safe for concurrent uvicorn workers and forward-compatible with migrations.
    """
    schema_name = get_schema_name(worker_id)
    lock_id = zlib.crc32(f"ensure_schema_{schema_name}".encode())

    from zerg.database import Base

    with engine.begin() as conn:
        # Advisory lock prevents race conditions
        conn.execute(text(f"SELECT pg_advisory_xact_lock({lock_id})"))

        # Create schema if not exists (idempotent)
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema_name}"))

        # Set search_path for table creation
        conn.execute(text(f"SET search_path TO {schema_name}, public"))

        # Create all tables (checkfirst=True is idempotent and handles migrations)
        Base.metadata.create_all(bind=conn, checkfirst=True)

    logger.info(f"Ensured schema exists: {schema_name}")
    return schema_name
```

### Phase 2.3: Suite-Start Cleanup

```javascript
// test-setup.js
import { spawn } from 'child_process';
import path from 'path';

async function globalSetup(config) {
  const workers = config.workers || 4;
  const backendDir = path.resolve(__dirname, '../backend');

  // Drop all E2E schemas and pre-create fresh ones
  await new Promise((resolve, reject) => {
    const proc = spawn('uv', ['run', 'python', '-c', `
import os
os.environ['E2E_USE_POSTGRES_SCHEMAS'] = '1'
from zerg.database import default_engine
from zerg.e2e_schema_manager import drop_all_e2e_schemas, ensure_worker_schema

# Clean slate
dropped = drop_all_e2e_schemas(default_engine)
print(f"Dropped {dropped} stale E2E schemas")

# Pre-create schemas for all workers
for i in range(${workers}):
    ensure_worker_schema(default_engine, str(i))
    print(f"Pre-created schema e2e_worker_{i}")
`], { cwd: backendDir, stdio: 'inherit' });

    proc.on('close', code => code === 0 ? resolve() : reject(new Error(`Setup failed: ${code}`)));
  });
}

export default globalSetup;
```

### Phase 3.1: Fix ApiClient Header

```typescript
// tests/helpers/api-client.ts
export class ApiClient {
  constructor(private workerId: string) {}

  async fetch(url: string, options: RequestInit = {}) {
    return fetch(url, {
      ...options,
      headers: {
        ...options.headers,
        'X-Test-Worker': this.workerId,  // Always include!
      },
    });
  }
}
```

---

## Success Metrics

| Metric | Before | Target |
|--------|--------|--------|
| Playwright workers | 2 (capped) | CPU cores |
| Test failures from races | 5-19 | 0 |
| Schema recreations per run | 2N | 0 (pre-created) |
| DB connections (peak) | 480 | < 100 |
| E2E suite duration | 8x slower | Normal |

---

## Risk Mitigation

| Risk | Mitigation |
|------|------------|
| Single uvicorn bottleneck | Monitor; most E2E tests are fast |
| checkfirst=True misses columns | Use Alembic for schema migrations in prod |
| Pre-create fails on DB error | globalSetup exits early; fail fast |
| Worker header missing | Audit all fetch/request calls |

---

## Files to Modify

| Phase | File | Action |
|-------|------|--------|
| 1 | `apps/zerg/e2e/spawn-test-backend.js` | Edit line 97 |
| 1 | `apps/zerg/backend/zerg/database.py` | Add pool size reduction |
| 2 | `apps/zerg/backend/zerg/e2e_schema_manager.py` | Add `ensure_worker_schema` |
| 2 | `apps/zerg/backend/zerg/database.py` | Call ensure instead of recreate |
| 2 | `apps/zerg/e2e/test-setup.js` | Rewrite with pre-creation |
| 3 | `apps/zerg/e2e/tests/helpers/api-client.ts` | Add header |
| 4 | Various | Cleanup deprecated code |
