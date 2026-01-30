# Lightweight OSS Pivot

**Status:** Active
**Goal:** `pip install zerg && zerg serve` - working Timeline in under 2 minutes

---

## The Pivot

**From:** Enterprise B2B SaaS (Postgres, Docker, multi-tenant, complex deploy)
**To:** Lightweight OSS tool (SQLite, single process, pip install, zero config)

**Model:** OpenClaw, Datasette, Aider - tools that just work.

---

## Target Experience

```bash
pip install zerg
zerg serve
# → http://localhost:8080 - Timeline UI ready
# → SQLite at ~/.zerg/zerg.db
# → No Postgres, no Docker, no config
```

**Time to value: <2 minutes**

---

## What We Keep

- **Timeline UI** - The product. Session archive, search, filters.
- **Claude Code session sync** - Shipper sends sessions, Timeline displays them.
- **Oikos chat** - Talk to your sessions (requires API key).
- **Single-user mode** - One user per instance. No auth complexity.

## What We Drop (for now)

- Multi-tenant architecture
- Complex job queue (SKIP LOCKED, advisory locks)
- Durable checkpoints (MemorySaver is fine)
- nginx reverse proxy
- Docker as primary install path

## Lite Mode Guardrails (Non-Negotiable)

- **Single worker only** (no multi-process uvicorn/gunicorn)
- **No durable job queue** (sequential/inline only)
- **No advisory locks** (single-process exclusivity instead)
- **Agents API must be SQLite-safe** (see checklist below)

---

## SQLite Migration

### Why It Works

Single-worker mode eliminates most Postgres dependencies:

| Postgres Feature | Why We Needed It | SQLite Alternative |
|------------------|------------------|-------------------|
| Advisory locks | Multi-worker coordination | Single worker = no contention |
| SKIP LOCKED | Job queue concurrency | Single worker = sequential |
| Separate schemas | Test isolation | Separate .db files |
| UUID type | Convenience | String(36) |
| JSONB | Indexed JSON queries | JSON1 (no indexing, acceptable) |

### What Actually Needs Work

**Agents API (Claude sessions)** — this is the main migration + highest risk
because it stores raw JSONL lines and must preserve exact export fidelity:

```
apps/zerg/backend/zerg/
├── models/agents.py      # UUID→String, JSONB→JSON, drop schema="agents"
├── services/agents_store.py  # SQLite upserts instead of postgresql.insert()
├── routers/agents.py     # Remove require_postgres() guard
└── database.py           # Allow sqlite:// URLs
```

**Agents migration checklist (SQLite):**
- UUID columns → `String(36)` (or `with_variant` for SQLite)
- JSONB → JSON (SQLite JSON1)
- Remove `agents` schema; use default schema (SQLite has none)
- Replace `postgresql.insert()` with SQLite‑safe upsert
- Keep `raw_json` as TEXT **or** store JSONL in file + DB pointer (path + offset)

Everything else either works as-is or gets disabled in lite mode.

---

## Implementation Phases

### Phase 1: SQLite Backend (Day 1)

**Files:**
- `database.py` - Accept `sqlite:///` URLs
- `main.py` - Remove Postgres-only guard
- `config/__init__.py` - Add `lite_mode: bool`

**Test:** `DATABASE_URL=sqlite:///test.db python -c "from zerg.database import get_engine; get_engine()"`

### Phase 2: Agents Models (Day 2)

**Files:**
- `models/agents.py`:
  ```python
  # Before
  id = Column(postgresql.UUID(as_uuid=True), primary_key=True)
  raw_json = Column(postgresql.JSONB)

  # After
  id = Column(String(36), primary_key=True, default=lambda: str(uuid4()))
  raw_json = Column(JSON)
  ```
- `services/agents_store.py` - Replace `postgresql.insert()` with dialect-agnostic upsert
- `routers/agents.py` - Remove `require_postgres()` call
- `alembic/` - SQLite-compatible migration

**Test:** Shipper can sync sessions to SQLite DB, Timeline displays them.

### Phase 3: Single-Process Server (Day 3)

**Files:**
- `cli/main.py` - New CLI entry point
- `cli/serve.py` - `zerg serve` command
- `pyproject.toml` - Entry point: `zerg = "zerg.cli.main:app"`

**Behavior:**
```python
# zerg serve
uvicorn.run("zerg.main:app", host="127.0.0.1", port=8080)
```

**Test:** `pip install -e . && zerg serve` starts server.

### Phase 4: Bundle Frontend (Day 4)

**Build step:**
```bash
cd apps/zerg/frontend-web && bun run build
cp -r dist/ ../backend/zerg/static/
```

**Serve from FastAPI:**
```python
app.mount("/", StaticFiles(directory="static", html=True))
```

**Runtime config:** Generate `config.js` at startup with correct API URLs.

**Test:** `zerg serve` serves working UI at http://localhost:8080

### Phase 5: PyPI Publishing (Day 5)

**pyproject.toml:**
```toml
[project]
name = "zerg"
version = "0.1.0"
dependencies = ["fastapi", "sqlalchemy", "uvicorn", ...]

[project.scripts]
zerg = "zerg.cli.main:app"

[tool.hatch.build]
include = ["zerg/", "zerg/static/"]
```

**CI:** Build frontend → Include in wheel → Publish to PyPI

**Test:** `pip install zerg && zerg serve` works from clean virtualenv.

---

## Architecture (After)

```
~/.zerg/
├── zerg.db          # SQLite database
└── config.toml      # Optional config overrides

zerg serve
├── FastAPI app (single process)
├── SQLite connection
├── Static frontend served at /
└── API at /api/*
```

**No nginx. No Docker. No Postgres. No workers.**

---

## Feature Matrix

| Feature | Lite (SQLite) | Full (Postgres) |
|---------|---------------|-----------------|
| Timeline UI | ✅ | ✅ |
| Session sync (Shipper) | ✅ | ✅ |
| Session search | ✅ Basic | ✅ Full-text |
| Oikos chat | ✅ | ✅ |
| Durable checkpoints | ❌ Memory only | ✅ |
| Multi-user | ❌ | ✅ |
| Job queue | ❌ Sequential | ✅ Concurrent |

**Important:** Lite mode is **full isolation** (one process + one SQLite file).
It’s always-on with low resource use because SQLite has no daemon.

**Lite mode is the default.** Postgres is opt-in for power users.

---

## Config

```toml
# ~/.zerg/config.toml (optional - sensible defaults work)

[server]
host = "127.0.0.1"
port = 8080

[database]
# Default: sqlite:///~/.zerg/zerg.db
# Optional: postgresql://user:pass@host/db
url = "sqlite:///~/.zerg/zerg.db"

[llm]
# Required for Oikos chat
anthropic_api_key = "sk-ant-..."
```

**Onboarding wizard (future):** `zerg init` prompts for API key, writes config.

---

## Migration Path for Existing Users

```bash
# Export from Postgres
zerg export --format sqlite --output backup.db

# Or just point to Postgres
zerg serve --database postgresql://...
```

Both paths work. No forced migration.

---

## Success Criteria

1. **Install:** `pip install zerg` (no git clone)
2. **Run:** `zerg serve` (no Docker, no make dev)
3. **Time:** <2 minutes to see Timeline UI
4. **Size:** <50MB installed
5. **RAM:** <100MB runtime

---

## Open Items

- [ ] Package name availability (`zerg` on PyPI?)
- [ ] Frontend bundle size (currently ~5MB?)
- [ ] Windows support (SQLite works, paths need testing)
- [ ] Shipper packaging (separate pip package or bundled?)

---

## References

- [OpenClaw](https://github.com/moltbot/moltbot) - Lightweight agent, npm install
- [Datasette](https://datasette.io/) - SQLite + Python + single binary
- [Aider](https://aider.chat/) - pip install, zero config

---

## Changelog

- **2026-01-30:** Initial draft
- **2026-01-30:** Pivoted from "should we" to "we will" - lightweight OSS is the direction
