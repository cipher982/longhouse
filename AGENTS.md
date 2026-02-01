# Longhouse (codename: Zerg)

AI agent orchestration platform. Oikos = voice/text UI. Longhouse = product name.

**Owner**: david010@gmail.com (David Rose)

Skills-Dir: .agents/skills

## Philosophy

- **Read VISION.md first** — It's the guiding light. Understand the strategic direction before diving into implementation.
- **Read docs/LIGHTWEIGHT-OSS-ONBOARDING.md** — Details the SQLite pivot plan. Don't invest in Postgres-specific infrastructure.
- **Progressive disclosure** — AGENTS.md should point to deeper docs/runbooks so agents know what they don’t know.
- Always commit changes as you go (no lingering uncommitted work).

**"Trust the AI"** — Modern LLMs are smart enough to figure things out. Give them context and autonomy, not rigid decision trees. No keyword routing, no specialized commiss.

**Current Direction (2026-01):** Migrating to SQLite-only for OSS. Postgres is control-plane only. See VISION.md § "No Postgres in core."

## Task Tracking

- Master task list: `TODO.md` (update before/after work; use this for agent handoffs and status)

## Quick Reference

**Do not Assume `make dev` is running** during coding sessions.

| URL | What |
|-----|------|
| localhost:47200 | Frontend (dev) |
| localhost:47300 | Backend API (dev) |
| localhost:47300/health | Health check |

## Essential Commands

```bash
make dev              # Start SQLite dev (backend + frontend, no Docker)
make stop             # Stop dev services
make test             # Unit tests (SQLite lite suite)
make test-e2e         # Core E2E + a11y
make test-full        # Full suite (full E2E + evals + visual baselines)
make dev-docker       # Legacy: Docker + Postgres (CI/testing only)
```

## Testing

```bash
make test          # Unit tests (~80s, 16 parallel workers)
make test-e2e      # Core E2E + a11y
make test-full     # Full suite (unit + full E2E + evals + visual baselines)
```

**How it works:** External Postgres on cube (configured in `.env`). pytest-xdist spawns 16 workers with isolated schemas. No Docker needed.

**DO NOT:**
- Set `PYTEST_XDIST_COMMIS=0` — kills parallelism, tests take 10x longer
- Pass extra env vars — `DATABASE_URL` and `CI_TEST_SCHEMA` are already in `.env`
- Run pytest directly — always use Make targets
- Agents: prefer explicit long-named Make aliases (`test-...--`) for tiered runs

**CI Debugging:** Run commands directly, no `&` background, no `|| echo` swallowing. Let it crash, read the first error.

**CI Workflows:** Pushes can trigger multiple workflows; aggregate runs by commit SHA and use `gh run watch` (avoid sleep/poll loops).

## Architecture

```
Dev:  User → Frontend (47200) + Backend API (47300) → SQLite (~/.zerg/dev.db)
Prod: User → nginx → FastAPI backend → SQLite or Postgres
```

**Database:** Longhouse DB is a schema inside Life Hub's Postgres (same server). No separate sync needed for structured data.

**Oikos/Commis Flow:**
User message → `OikosService` → `oikos_react_engine` → (spawn_commis) → `AgentInterrupted` → WAITING → commis runs → `commis_resume` → response

**Key Files:**
| File | Purpose |
|------|---------|
| `services/oikos_react_engine.py` | Core ReAct loop |
| `managers/fiche_runner.py` | `run_thread()` entry point |
| `services/commis_runner.py` | Executes commis jobs |
| `tools/builtin/oikos_tools.py` | `spawn_commis`, `get_commis_evidence` |
| `tools/tool_search.py` | Semantic tool search |

**Lazy Tool Loading:** 65+ tools available, ~12 core tools pre-bound. Others discovered via `search_tools()`.

**Oikos UX ("Human PA" model):** Kick off tasks, move on, don't block. Commiss report back async. Input re-enables on `oikos_complete`, not waiting for commiss.

**Single Brain:** OikosService enforces one `ThreadType.SUPER` thread per user; each message creates a Run tied to that thread.

**System Prompt Injection:** FicheRunner filters DB-stored system messages; prompt is injected fresh from `fiche.system_instructions` every run to prevent staleness. Dynamic context (connector status, RAG) should go late in the message array for cache efficiency.

## Conventions

- **Backend**: FastAPI + Pydantic, `apps/zerg/backend/zerg/`
- **Frontend**: React + React Query, `apps/zerg/frontend-web/`
- **Package managers**: Bun (JS), uv (Python) — never npm/pip
- **Generated code** (don't edit): `src/generated/`, `zerg/generated/`
- **Tests**: Always use `make test*` targets, never direct pytest/playwright
- **Tool contracts**: Edit `schemas/tools.yml`, then run `scripts/generate_tool_types.py` — never edit generated files directly
- **Oikos tools**: Registration is centralized in `oikos_tools.py`; `CORE_TOOLS` pulls from `SUPERVISOR_TOOL_NAMES`; tests in `test_core_tools.py` catch drift
- **Git policy**: Work only on `main`, no worktrees; confirm `git status -sb` before changes; no stashing unless explicitly requested

## Gotchas

1. **`make dev` is interactive** — runs backend + frontend, Ctrl+C to stop.
2. **Default is SQLite** — `make dev` uses `~/.zerg/dev.db`, no Docker needed.
3. **Never run tests directly** — `make test` / `make test-e2e` / `make test-full` only.
4. **WebSocket/SSE code must sync** — run `make regen-ws` / `make regen-sse` after schema changes.
5. **Auth disabled in dev** — `AUTH_DISABLED=1` set automatically by dev.sh.
6. **Coolify env var changes need redeploy** — restart doesn't pick up new vars.
7. **AGENTS.md is canonical** — `CLAUDE.md` is a symlink, edit AGENTS.md only.
8. **Runner name+secret collision** — If two owners seed runners with same name and secret, first-created wins. Use unique secrets per environment.
9. **SSE event types** — New types must be added to `EventType` enum or `append_run_event()` won't publish live.
10. **Sauron job source conflict** — If Zerg backend has `JOB_QUEUE_ENABLED=1` AND `JOBS_GIT_*` vars, it schedules sauron-jobs too. Remove those vars when Sauron is the sole scheduler.
11. **Master task list lives in `TODO.md`** — keep AGENTS.md lean; update TODO.md before/after work.
12. **Sauron Docker build** — uses `apps/sauron/pyproject.docker.toml` to avoid editable `../zerg/backend` sources during image builds.
13. **Caddy routing breaks on deploy** — Container names include timestamps; Caddy config at `/data/coolify/proxy/caddy/dynamic/api-longhouse.caddy` needs manual update after deploy. Fix: set FQDN in Coolify UI so it manages routing automatically.

## Pushing Changes

**Prod URLs**: https://longhouse.ai (frontend) | https://api.longhouse.ai (API)

### Before Push
```bash
make test              # Unit tests (required)
make test-e2e          # Core E2E + a11y - must pass 100%
```

### After Push
Coolify auto-deploys `main`. **Always verify your deploy:**
```bash
make verify-prod       # Full validation: API + browser (~80s)
```
This waits for health, runs API checks (auth, LLM, voice, CRUD), then browser tests.

### If Something Breaks
```bash
./scripts/get-coolify-logs.sh 1   # Check deploy logs
```

### Checklist for Agents
1. ✅ `make test` passes locally
2. ✅ `make test-e2e` passes locally
3. ✅ Push to main
4. ✅ Run `make verify-prod` (~80s)
5. ✅ Report result to user

## apps/sauron - Standalone Scheduler

Sauron is the centralized ops scheduler, deployed as a standalone service on clifford VPS. It reuses `zerg.jobs` infrastructure.

**Location:** `apps/sauron/`

**Key files:**
- `main.py` - APScheduler + worker loop
- `api.py` - FastAPI for Jarvis control
- `cli.py` - CLI for manual operations
- `Dockerfile` - Builds from monorepo root
- `docker-compose.yml` - Coolify deployment config

**How it works:**
1. On startup, clones `cipher982/sauron-jobs` repo via `GitSyncService`
2. Loads jobs from `manifest.py` using `zerg.jobs.loader`
3. Schedules jobs with APScheduler
4. Executes via durable queue (same as Zerg backend)

**API endpoints:**
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/health` | GET | Health check |
| `/status` | GET | Scheduler + git sync status |
| `/jobs` | GET | List all jobs |
| `/jobs/{id}/trigger` | POST | Manual trigger |
| `/sync` | POST | Force git sync |

**Deploy:** Coolify on clifford, separate from main Zerg deployment.

## Deep Dives

| Topic | Guide |
|-------|-------|
| **Strategic direction** | `VISION.md` — read first! |
| **SQLite pivot plan** | `docs/LIGHTWEIGHT-OSS-ONBOARDING.md` |
| Oikos Tools | `apps/zerg/backend/docs/supervisor_tools.md` |
| Sauron scheduler | `apps/sauron/README.md` |
| Sauron job definitions | `~/git/sauron-jobs/` |

## Misc
- GH actions use runners on Cube
- **Parallel patrol agents** converge on same ideas unless diversity enforced; use explicit target partitioning + shared dedupe gate

## TODOs (Agent-Tracked)

- [x] **Telegram webhook handler** - Implemented in `routers/channels_webhooks.py` (commit 2dc1ee0b)
- [x] **Parallel spawn_commis interrupt** - Fixed to return `interrupt_value` dict (commit a8264f9d)

---

## Agent Self-Improvement

**Agents: append learnings here.** Human reviews weekly to promote or compact.

### When to Append

- **User corrects you** — Record what you assumed wrong
- **Non-obvious constraint discovered** — Something not in docs that bit you
- **Workaround found** — Errors/failures and what fixed them
- **Pattern that worked well** — Approaches worth repeating

### Format

```
- (YYYY-MM-DD) [category] Specific insight. "X caused Y, fix is Z"
```

Categories: `gotcha`, `pattern`, `design`, `tool`, `test`, `deploy`, `perf`

### Rules

1. **Be specific** — Not "had issues with X" but "X fails when Y, use Z instead"
2. **One line per insight** — Keep atomic, date it
3. **Don't delete others' entries** — Human compacts weekly
4. **Propose promotions** — If something keeps being useful, suggest moving to main docs

---

### Learnings

<!-- Agents: append below this line. Human compacts weekly. -->
- (2026-01-30) [gotcha] Multi-tenant mode disables Agents API via require_single_tenant(); schema routing in commis_db is test-only and blocked in prod.
- (2026-01-31) [test] ScriptedLLM checks for ToolMessage to decide synthesis vs tool-call; multi-run threads accumulate messages, so new scenarios must check "current turn only" (messages after last HumanMessage).
- (2026-01-31) [test] E2E schema mismatch: globalSetup creates 4 schemas, processor polls 16. Fixed: processor catches ProgrammingError for missing schemas.
- (2026-01-31) [test] `make test` now runs the SQLite-lite suite by default; use `make test-legacy` for full Postgres coverage.
- (2026-01-31) [gotcha] config.Settings.db_is_sqlite() only checks startswith("sqlite") and ignores quoted DATABASE_URL; can mis-detect lite_mode vs database._is_sqlite_url. **FIXED:** db_is_sqlite() now delegates to _is_sqlite_url().
- (2026-01-31) [test] Use GUID() TypeDecorator for trace_id columns (not String(36)) so UUID objects auto-convert for SQLite. Passing raw UUID to String column causes "type 'UUID' is not supported" error.
- (2026-01-31) [test] SQLite cascade deletes unreliable in tests—PRAGMA foreign_keys=ON must be set per connection. Use manual delete order in tests or passive_deletes=True on relationships.
- (2026-01-31) [test] SQLite DateTime columns require datetime objects, not ISO strings. Test fixtures passing strings fail with "SQLite DateTime type only accepts Python datetime".
- (2026-01-31) [design] SQLite minimum version enforced at startup: 3.35+ (RETURNING required for job claiming).
- (2026-02-01) [design] Public branding should be a single umbrella name; keep Oikos as the assistant UI and Zerg as internal codename to avoid name sprawl.
- (2026-02-01) [tool] Claude Code hooks emit PreToolUse/PostToolUse/PostToolUseFailure; keep hooks async, exit 0, and truncate responses (~10KB) to avoid breaking commis.
- (2026-02-01) [tool] Commis hook callback expects X-Internal-Token; default callback base is loopback (localhost) unless LONGHOUSE_CALLBACK_URL is set.
- (2026-02-01) [gotcha] zerg.jobs.queue + ops_db are stubbed in SQLite-only mode; Sauron scheduling in queue mode won’t execute jobs unless you run direct mode or restore a Postgres queue.
- (2026-02-01) [design] Durable scheduler queue now uses SQLite via zerg.jobs.queue; set JOB_QUEUE_DB_URL to a dedicated sqlite file to avoid write contention.
