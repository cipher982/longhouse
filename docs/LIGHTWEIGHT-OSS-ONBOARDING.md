# Lightweight OSS Pivot

**Status:** Active
**Goal:** `pip install zerg && zerg serve` — cloud agent ops center in under 5 minutes (SQLite only)
**Reality check:** Current codebase still uses Postgres; this doc defines the SQLite-only target state.

---

## Current State Findings (2026-01-31)

These are the concrete mismatches between today’s codebase and the SQLite-only target. This section is here so we can plan from reality instead of aspiration.

- **SQLite is hard-blocked** in `zerg.database.make_engine()` and startup enforces PostgreSQL only in `zerg.main.lifespan()`.
- **Schemas are hard-coded** (`DB_SCHEMA="zerg"`, agents schema `"agents"`). SQLite does not support schemas, and there are raw SQL references like `ops.job_queue`.
- **Postgres-only column types** exist across models (UUID, JSONB, `gen_random_uuid()` defaults, partial indexes).
- **Agents API is PG-only**: models use UUID/JSONB/schema, `AgentsStore` uses `postgresql.insert`, and endpoints are guarded by `require_postgres()`.
- **Job claiming uses PG locks** (`FOR UPDATE SKIP LOCKED`) in `commis_job_processor` and `jobs.queue`.
- **Advisory locks / row locks** are used in multiple paths (`single_tenant`, `fiche_locks`, `commis_resume`, email/sms tools).
- **Checkpoints are non-durable on SQLite** (MemorySaver only).
- **CLI lacks `zerg serve` and package bundling**: no server command, and backend expects `static/` at repo root but frontend dist is at `apps/zerg/frontend-web/dist`.

If we want `pip install zerg && zerg serve` on SQLite, all of the above must be addressed or intentionally gated off in lite mode.

---

## Decisions to Lock (before implementation)

1. **SQLite schema strategy**: recommended = **flat tables, no schemas** (there are no name collisions with agents tables). Postgres keeps schemas.
2. **Ops job queue scope**: recommended = **disable `ops.job_queue` in lite** (keep it Postgres-only), but make `commis_jobs` SQLite-safe for local concurrency.
3. **Durable checkpoints**: recommended = **use `langgraph-checkpoint-sqlite`** for SQLite so resumes survive restarts.
4. **Static frontend packaging**: recommended = **bundle `apps/zerg/frontend-web/dist` in the python package** and mount via FastAPI.

---

## Detailed Execution Plan (SQLite Lite Mode)

### Phase 0 — Preflight Decisions + Flags

**Goal:** Establish SQLite vs Postgres mode cleanly so the rest of the system can branch safely.

- Add a computed `lite_mode` (or `db_is_sqlite`) flag in `zerg.config.get_settings()` based on `database_url` scheme.
- Decide schema strategy (flat tables) and write it down in this doc.
- Decide job queue scope (disable ops job queue in lite).
- Decide durable checkpoints (sqlite checkpointer).

### Phase 1 — Core DB Boot on SQLite

**Goal:** `zerg serve` boots on SQLite without crashing.

- **database.py**
  - Allow sqlite URLs (remove hard error).
  - Skip `_apply_search_path()` for sqlite.
  - Make `DB_SCHEMA` and `AGENTS_SCHEMA` conditional; use `None` for sqlite.
  - Set SQLite pragmas on connect: `journal_mode=WAL`, `busy_timeout`, `foreign_keys=ON`.
- **main.py**
  - Remove PostgreSQL-only guard in `lifespan()`. Replace with a warning if SQLite (locks/features are degraded).
- **initialize_database()**
  - Skip schema creation for sqlite and avoid schema-qualified introspection.

**Test:** `DATABASE_URL=sqlite:///~/.zerg/zerg.db zerg serve` starts and `/health` works.

### Phase 2 — Model Compatibility (Core + Agents)

**Goal:** All tables can be created on SQLite.

- Replace `UUID` columns with `String(36)` (or `String`) + `uuid4()` defaults.
- Replace `JSONB` with `JSON().with_variant(JSONB, "postgresql")` or plain `JSON`.
- Replace `gen_random_uuid()` defaults with Python-side defaults on sqlite.
- Update partial indexes to include `sqlite_where` or drop them if not supported.
- Make `agents` metadata schema conditional (None for sqlite).

**Files:** `models/agents.py`, `models/device_token.py`, `models/llm_audit.py`, `models/run.py`, `models/models.py`

**Test:** `initialize_database()` succeeds on SQLite; `sqlite3 ~/.zerg/zerg.db .tables` shows all tables.

### Phase 3 — Agents API + Ingest

**Goal:** Shipper ingestion + Timeline endpoints work on SQLite.

- Replace `postgresql.insert` with dialect-agnostic upsert:
  - If sqlite: use `sqlalchemy.dialects.sqlite.insert(...).on_conflict_do_nothing()` or catch `IntegrityError`.
  - If postgres: keep current `on_conflict_do_nothing` with partial index.
- Remove `require_postgres()` guard; keep `require_single_tenant()` if needed.
- Ensure dedupe index works without schema-qualified names.

**Files:** `services/agents_store.py`, `routers/agents.py`, `models/agents.py`, `alembic/versions/0002_agents_schema.py` (+ follow-on migrations)

**Test:** Shipper syncs session; sessions appear in Timeline UI on SQLite.

### Phase 4 — Job Queue + Concurrency (SQLite-safe)

**Goal:** Multiple commis can run concurrently without PG locks.

- Replace `FOR UPDATE SKIP LOCKED` with `BEGIN IMMEDIATE` + atomic `UPDATE ... RETURNING`.
- Add heartbeat fields + reclaim logic for stale jobs.
- Replace advisory locks with file locks or status-guarded updates.
- For SQLite: disable `ops.job_queue` paths (or gate behind `job_queue_enabled && not lite_mode`).

**Files:** `services/commis_job_processor.py`, `jobs/queue.py`, `services/commis_resume.py`, `services/single_tenant.py`, `services/fiche_locks.py`, `tools/builtin/email_tools.py`, `tools/builtin/sms_tools.py`

**Test:** Spawn 3 commis jobs, kill server, restart, jobs resume.

### Phase 5 — Durable Checkpoints (SQLite)

**Goal:** Interrupt/resume survives process restart in lite mode.

- Replace MemorySaver for sqlite with `langgraph-checkpoint-sqlite` backed by the same `~/.zerg/zerg.db`.
- Ensure migrations/setup are idempotent for sqlite.

**Files:** `services/checkpointer.py`

**Test:** Interrupt a run, restart server, resume continues correctly.

### Phase 6 — CLI + Frontend Bundle

**Goal:** `pip install zerg && zerg serve` is real.

- Add `zerg serve` command (typer) that runs uvicorn with sane defaults (`0.0.0.0:8080`).
- Bundle frontend `dist` into the python package (hatch config).
- Update FastAPI static mount to use packaged assets when available.

**Files:** `cli/main.py`, `pyproject.toml`, `main.py`, `apps/zerg/frontend-web/dist`

**Test:** fresh venv → `pip install zerg` → `zerg serve` → open `/dashboard` and `/chat`.

### Phase 7 — Onboarding Smoke + Docs

**Goal:** Validate the full OSS onboarding flow.

- Add/extend `make onboarding-smoke` to run SQLite boot + basic API checks.
- Update README quick-start to default to SQLite.

**Test:** `make onboarding-smoke` passes on a clean machine.

---

## The Vision

**The Problem:**
- You have 5-6 Claude Code terminals open
- Context switching is exhausting
- Close laptop = agents pause
- Can't check progress from phone
- Sessions lost if you restart

**The Solution:** Zerg — your always-on agent operations center
**Alignment:** SQLite is the core and only runtime DB; Postgres is control-plane only (if used).

```
┌─────────────────────────────────────────────────────────────┐
│  ZERG (runs 24/7 on VPS / homelab / Mac mini)               │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Oikos (main chat)           Commis Pool (background)       │
│  ┌─────────────────┐         ┌────────────────────────┐     │
│  │ "Convert that   │────────▶│ Commis 1: cloning...   │     │
│  │  repo to Rust"  │         │ Commis 2: writing tests│     │
│  │                 │         │ Commis 3: reviewing PR │     │
│  │ "Status on the  │◀────────│ Commis 4: (idle)       │     │
│  │  PR from earlier"│        │ Commis 5: deploying... │     │
│  └─────────────────┘         └────────────────────────┘     │
│         ▲                            ▲                      │
│    [Phone/Web]                  [Sauron crons]              │
│                                                             │
│  Timeline: searchable archive of all sessions               │
│  Forum: async agent collaboration space                     │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**Key insight:** Users migrate local Claude sessions → cloud commis. Close laptop, agents keep working. Check from phone. Wake up to completed work.

---

## Target Experience

```bash
# On your VPS / homelab / always-on Mac
pip install zerg
zerg serve --host 0.0.0.0 --port 8080

# That's it. Access from anywhere.
# SQLite at ~/.zerg/zerg.db (default)
# No Postgres in core/runtime; no Docker or external services required
```

| Metric | Target |
|--------|--------|
| Time to deploy | < 5 minutes |
| Idle RAM | < 200 MB |
| With 5 active commis | < 500 MB |
| External dependencies | Zero |

---

## What We're Building

| Feature | Description |
|---------|-------------|
| **Oikos** | Main chat interface — your Super-Siri |
| **Commis Pool** | Background agents working in parallel |
| **Timeline** | Searchable archive of all sessions |
| **Forum** | Async collaboration space for agents |
| **Session Migration** | Move local Claude → cloud commis |
| **Mobile Access** | Check on agents from phone |
| **Sauron Crons** | Scheduled background jobs |

## What "Lightweight" Means

- **Easy deploy** — pip install, not k8s
- **Low resources** — runs on $5 VPS
- **Zero external deps** — no Postgres server, no Redis, no Docker
- **Simple config** — works out of the box

**NOT:**
- Single-user viewer
- Desktop-only tool
- Toy without real concurrency

---

## SQLite + Concurrent Agents

### Why SQLite Works

**The fear:** "SQLite can't handle concurrent agents"

**The reality:** Your agents spend 99% of time waiting on LLM APIs, not writing to DB.

```
Agent 1: [======LLM call (10s)======] [write 5ms] [======LLM call======]
Agent 2: [======LLM call (8s)======] [write 5ms] [======LLM call======]
Agent 3: [======LLM call (12s)======] [write 5ms] [======LLM call======]
```

SQLite with WAL mode handles this trivially. Writes serialize but they're milliseconds.

### Scale Reality Check

| Metric | Enterprise SaaS | Your Use Case |
|--------|-----------------|---------------|
| Concurrent agents | 1000s | 5-10 |
| Writes/second | 10,000s | ~10 |
| Users | 10,000s | 1 |

SQLite is overkill for this, not underkill.

### Postgres Features → SQLite Alternatives

| Postgres Feature | What It Does | SQLite Alternative |
|------------------|--------------|-------------------|
| `FOR UPDATE SKIP LOCKED` | Atomic job claim | `BEGIN IMMEDIATE` + atomic UPDATE |
| Advisory locks | Cross-process coordination | Status column + heartbeat |
| UUID columns | Convenience | String(36) |
| JSONB | Indexed JSON | JSON1 extension |
| Separate schemas | Isolation | Separate .db files or prefixes |

### Job Queue Implementation

```python
# Postgres way
SELECT * FROM jobs WHERE status='pending' FOR UPDATE SKIP LOCKED LIMIT 1

# SQLite way
def claim_job(db, worker_id):
    with db.begin_immediate():  # Acquire write lock
        job = db.execute("""
            UPDATE jobs
            SET status='running', worker_id=?, started_at=NOW()
            WHERE id = (
                SELECT id FROM jobs
                WHERE status='pending'
                ORDER BY priority DESC, created_at ASC
                LIMIT 1
            )
            RETURNING *
        """, [worker_id]).fetchone()
    return job
```

Multiple workers, SQLite, works fine. Celery does this.

### Coordination Without Advisory Locks

```python
# Option A: Status + heartbeat
# Job has: status, worker_id, last_heartbeat
# Worker updates heartbeat every 30s
# Stale jobs (no heartbeat for 2min) get reclaimed

# Option B: File locks for critical sections
import fcntl
with open(f"~/.zerg/locks/{resource}.lock", 'w') as f:
    fcntl.flock(f, fcntl.LOCK_EX)
    # ... exclusive access ...
# Auto-released on close/crash
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  zerg serve                                             │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  FastAPI (async)                                        │
│  ├── /api/* — REST endpoints                            │
│  ├── /ws/* — WebSocket for real-time                    │
│  └── /* — Static frontend                               │
│                                                         │
│  Commis Pool (concurrent async tasks)                   │
│  ├── Commis 1 ──▶ LLM ──▶ Tools ──▶ DB                  │
│  ├── Commis 2 ──▶ LLM ──▶ Tools ──▶ DB                  │
│  └── Commis N ──▶ LLM ──▶ Tools ──▶ DB                  │
│                                                         │
│  Job Queue (SQLite-backed)                              │
│  └── Durable, survives restarts                         │
│                                                         │
│  SQLite (WAL mode)                                      │
│  └── ~/.zerg/zerg.db                                    │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

**Single process. Multiple concurrent agents. SQLite. Durable jobs.**

---

## Implementation Phases

### Phase 1: SQLite Backend (Day 1-2)

**Goal:** Accept SQLite URLs, prove it works

**Files:**
- `database.py` — Remove Postgres-only guard, accept `sqlite:///`
- `main.py` — Remove startup check
- `config/` — Add `lite_mode` detection (auto from URL scheme)

**Test:**
```bash
DATABASE_URL=sqlite:///~/.zerg/zerg.db zerg serve
# Server starts, basic endpoints work
```

### Phase 2: Agents Models (Day 2-3)

**Goal:** Claude session sync works with SQLite

**Changes:**
```python
# models/agents.py
# Before
id = Column(postgresql.UUID(as_uuid=True), primary_key=True)
raw_json = Column(postgresql.JSONB)

# After
id = Column(String(36), primary_key=True, default=lambda: str(uuid4()))
raw_json = Column(JSON)  # SQLite JSON1
```

**Files:**
- `models/agents.py` — UUID→String, JSONB→JSON, drop schema
- `services/agents_store.py` — Dialect-agnostic upsert
- `routers/agents.py` — Remove `require_postgres()` guard

**Test:** Shipper syncs session → appears in Timeline

### Phase 3: Job Queue (Day 3-4)

**Goal:** Durable job queue with SQLite

**Changes:**
- Replace `FOR UPDATE SKIP LOCKED` with `BEGIN IMMEDIATE` pattern
- Add heartbeat column for stale job detection
- File locks for critical sections (optional)

**Files:**
- `jobs/queue.py` — SQLite-compatible claim logic
- `services/commis_job_processor.py` — Heartbeat updates

**Test:**
```bash
# Start server, spawn 3 commis, kill server, restart
# Jobs resume from where they left off
```

### Phase 4: Single-Process Concurrency (Day 4-5)

**Goal:** Multiple commis in one process

**Architecture:**
```python
# Commis pool as async tasks
class CommisPool:
    def __init__(self, max_workers=10):
        self.semaphore = asyncio.Semaphore(max_workers)

    async def spawn(self, job):
        async with self.semaphore:
            await run_commis(job)
```

**Test:** Spawn 5 concurrent commis, all make progress

### Phase 5: CLI + Frontend Bundle (Day 5-6)

**Goal:** `pip install zerg && zerg serve` works

**CLI:**
```bash
zerg serve              # Start server
zerg serve --port 8080  # Custom port
zerg status             # Show running jobs
zerg logs <job_id>      # Tail job logs
```

**Frontend:** Pre-built React app served from FastAPI static mount

**Test:** Fresh virtualenv, pip install, zerg serve, open browser, see UI

### Phase 6: PyPI Publishing (Day 6-7)

**Goal:** Available on PyPI

```bash
pip install zerg
```

---

## File Structure (After)

```
~/.zerg/
├── zerg.db              # SQLite database (WAL mode)
├── config.toml          # Optional config overrides
├── locks/               # File locks for coordination
└── logs/                # Job logs

$ zerg serve
→ http://0.0.0.0:8080
→ 5 commis slots available
→ SQLite: ~/.zerg/zerg.db
```

---

## Feature Matrix

| Feature | Lite (SQLite) | Full (Postgres) |
|---------|:-------------:|:---------------:|
| Timeline UI | ✅ | ✅ |
| Session sync (Shipper) | ✅ | ✅ |
| Oikos chat | ✅ | ✅ |
| Concurrent commis | ✅ | ✅ |
| Durable job queue | ✅ | ✅ |
| Job queue (multi-node) | ❌ | ✅ |
| Full-text search | Basic | ✅ Advanced |
| Multi-user | ❌ | ✅ |

**Lite = single node, full features. Postgres = horizontal scale.**

---

## Config

```toml
# ~/.zerg/config.toml (optional — sensible defaults work)

[server]
host = "0.0.0.0"
port = 8080

[commis]
max_concurrent = 5      # How many agents can run at once
heartbeat_interval = 30 # Seconds between heartbeats
stale_threshold = 120   # Reclaim jobs with no heartbeat after this

[database]
# Default: sqlite:///~/.zerg/zerg.db
# For scale: postgresql://user:pass@host/db
url = "sqlite:///~/.zerg/zerg.db"

[llm]
anthropic_api_key = "sk-ant-..."
openai_api_key = "sk-..."
```

---

## Success Criteria

1. **Deploy:** `pip install zerg && zerg serve` on fresh VPS
2. **Concurrent:** 5 commis running simultaneously
3. **Durable:** Kill process, restart, jobs resume
4. **Mobile:** Access from phone, see agent progress
5. **Resources:** <500MB RAM with 5 active agents

---

## Open Questions

- [ ] Package name: `zerg` available on PyPI?
- [ ] Frontend bundle size?
- [ ] Shipper: bundled or separate package?
- [ ] Auth for remote access: API key? OAuth?
- [ ] HTTPS: built-in or "use Caddy/nginx"?

---

## The Pitch

**Before:**
```
5 Claude terminals → context switching hell → close laptop = pause → no mobile
```

**After:**
```
pip install zerg
zerg serve

# Spawn agents from phone
# Close laptop, they keep working
# Wake up to completed PRs
```

Your personal cloud agent team. Always on. SQLite simple. Actually works.

---

## Prior Art & SQLite Best Practices (Sources)

Curated sources we can lean on when pushing SQLite to its limits, plus the concrete behaviors that matter for Zerg’s design.

### Concurrency & Locking Reality

- **WAL improves read/write concurrency, but still single-writer.** Readers and writers can run concurrently, but only one writer at a time. WAL also requires shared memory and does not work over network filesystems.
  https://sqlite.org/wal.html
  https://www.sqlite.org/isolation.html
- **Checkpoint starvation is real.** Long-lived readers can prevent WAL checkpoint completion, letting the WAL grow without bound.
  https://sqlite.org/wal.html
  https://wchargin.com/better-sqlite3/performance.html
- **BEGIN IMMEDIATE grabs the write lock up-front.** It can return `SQLITE_BUSY` if another writer is active; DEFERRED upgrades on first write and can also hit `SQLITE_BUSY`.
  https://www.sqlite.org/lang_transaction.html
- **Timeouts matter under contention.** `PRAGMA busy_timeout` / `sqlite3_busy_timeout()` make writes wait instead of failing; high concurrency often needs longer timeouts than you expect.
  https://www.sqlite.org/c3ref/busy_timeout.html
  https://blog.skypilot.co/abusing-sqlite-to-handle-concurrency/

### DML Features We Can Rely On

- **UPSERT (ON CONFLICT)** is supported and designed for unique constraints (SQLite 3.24+).
  https://www.sqlite.org/lang_upsert.html
- **RETURNING** is supported (SQLite 3.35+), but output order is unspecified.
  https://www.sqlite.org/lang_returning.html

### Durability Tuning in WAL

- **`PRAGMA synchronous` tradeoffs**: `FULL` adds durability; `NORMAL` is faster but can reduce durability in WAL mode.
  https://www.sqlite.org/pragma.html
- **Auto-checkpoint defaults**: WAL checkpoints trigger at ~1000 pages by default; disabling checkpoints can let WAL grow unbounded.
  https://sqlite.org/wal.html

### JSON Support

- **JSON1 functions and operators** (`json_*`, `->`, `->>`) exist; JSON5 extensions are supported in newer SQLite builds.
  https://www.sqlite.org/json1.html

### Tooling / Prior Art

- **Litestream** — streaming replication of SQLite (WAL-aware) to object storage for backups/DR.
  https://litestream.io/how-it-works/
  https://litestream.io/reference/replicate/
- **LiteFS** — distributed SQLite via a FUSE filesystem + single-writer leases; production caveats are documented.
  https://fly.io/docs/litefs/
  https://fly.io/blog/introducing-litefs/
- **rqlite** — Raft-based replication of SQLite commands; single leader handles writes.
  https://rqlite.io/docs/design/
- **dqlite** — Canonical’s Raft-based HA SQLite (used in LXD).
  https://canonical.com/dqlite
  https://documentation.ubuntu.com/lxd/latest/reference/dqlite-internals/
- **Datasette + sqlite-utils** — ecosystem for creating/inspecting SQLite DBs; `sqlite-utils` CLI is great for migration/debugging.
  https://datasette.io/tools/sqlite-utils
  https://docs.datasette.io/en/0.56/ecosystem.html

---

## References

- [OpenClaw](https://github.com/moltbot/moltbot) — Lightweight agent platform
- [Datasette](https://datasette.io/) — SQLite-powered data tool
- [Litestream](https://litestream.io/) — SQLite replication (future?)
- [SQLite WAL mode](https://www.sqlite.org/wal.html) — Concurrent reads

---

## Changelog

- **2026-01-30:** Initial draft
- **2026-01-30:** Pivoted from "viewer" to "cloud agent ops center"
- **2026-01-30:** Proved SQLite + concurrent agents works — durable queue stays
