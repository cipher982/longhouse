# Longhouse (codename: Zerg)

AI agent orchestration platform. Oikos = voice/text UI. Longhouse = product name.

**Owner**: david010@gmail.com (David Rose)

Skills-Dir: .agents/skills

## Philosophy

- **Read VISION.md first** — It's the guiding light. Understand the strategic direction before diving into implementation.
- **Read VISION.md (SQLite-only OSS Pivot section)** — Details the SQLite pivot plan. Don't invest in Postgres-specific infrastructure.
- **Progressive disclosure** — AGENTS.md should point to deeper docs/runbooks so agents know what they don't know.
- Always commit changes as you go (no lingering uncommitted work).

**"Trust the AI"** — Modern LLMs are smart enough to figure things out. Give them context and autonomy, not rigid decision trees. No keyword routing, no specialized commiss.

**Current Direction (2026-01):** Migrating to SQLite-only for OSS. Postgres is control-plane only. See VISION.md § "No Postgres in core."

## Task Tracking

- **Master task list:** `TODO.md` (update BEFORE starting work and AFTER completing work)
- **Mark progress:** Check off subtasks as you complete them so next agent knows state
- **Document blockers:** If you can't finish, add notes under the task explaining why

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

## Architecture

**Stack:** FastAPI + SQLAlchemy + SQLite (dev/OSS) or Postgres (prod control-plane). React frontend. See `VISION.md` for details.

## Conventions

- **Backend**: FastAPI + Pydantic, `apps/zerg/backend/zerg/`
- **Frontend**: React + React Query, `apps/zerg/frontend-web/`
- **Package managers**: Bun (JS), uv (Python) — never npm/pip
- **Generated code** (don't edit): `src/generated/`, `zerg/generated/`
- **Tests**: Always use `make test*` targets, never direct pytest/playwright
- **Tool contracts**: Edit `schemas/tools.yml`, then run `scripts/generate_tool_types.py` — never edit generated files directly
- **Oikos tools**: Registration is centralized in `oikos_tools.py`; `CORE_TOOLS` pulls from `SUPERVISOR_TOOL_NAMES`; tests in `test_core_tools.py` catch drift
- **Git policy**: Work only on `main`, no worktrees; confirm `git status -sb` before changes; no stashing unless explicitly requested
- **Concurrent edits**: Dirty trees are normal; work around existing diffs and avoid overlapping lines. Only pause to coordinate if you must edit an already-modified file.

## UI Components

Import from `../components/ui`. **Check here before building custom UI.**

| Component | Variants/Sizes | Usage |
|-----------|---------------|-------|
| Button | primary, secondary, tertiary, ghost, danger, success / sm, md, lg | `<Button variant="primary" size="md">` |
| Badge | neutral, success, warning, error | `<Badge variant="success">Active</Badge>` |
| Card | glass (default), default | `<Card><Card.Header>...</Card.Header><Card.Body>...</Card.Body></Card>` |
| EmptyState | default, error | `<EmptyState title="..." description="..." action={<Button>}/>` |
| Input | - | `<Input placeholder="..." />` |
| Spinner | sm, md, lg | `<Spinner size="lg" />` |
| Table | - | `<Table><Table.Header>...<Table.Body><Table.Row>...` |
| SectionHeader | - | `<SectionHeader title="..." description="..." actions={...} />` |
| PageShell | narrow, normal, wide, full | `<PageShell size="wide">...</PageShell>` |
| IconButton | - | `<IconButton aria-label="..."><Icon /></IconButton>` |

## Gotchas

1. **`make dev` is interactive** — Use `run_in_background=true` in Bash tool.
2. **.env can override** — DATABASE_URL in .env breaks SQLite dev; comment it out.
3. **AGENTS.md is canonical** — `CLAUDE.md` is a symlink, edit AGENTS.md only.
4. **Auth disabled in dev** — `AUTH_DISABLED=1` set by dev.sh.
5. **Coolify env var changes need redeploy** — restart doesn't pick up new vars.
6. **Master task list:** `TODO.md` — update before/after work.
7. **Backend README required** — pyproject.toml needs it; create minimal if missing.

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

## apps/sauron - Scheduler (Folded In)

Sauron is not a separate service; scheduled jobs run inside the standard Longhouse instance service (per-user). The `sauron-jobs` repo pattern remains a power-user path (optional jobs pack), but OSS onboarding should not depend on it.

## apps/runner - Native Runner Daemon

Bun-compiled binary for command execution on user infrastructure. Connects via WebSocket, validates commands client-side (defense-in-depth), streams output. Install via `ENROLL_TOKEN=xxx curl .../install.sh | bash` → launchd (macOS) or systemd (Linux).

**Entrypoints:** `apps/runner/src/index.ts` (daemon), `apps/runner/src/executor.ts` (command exec), `zerg/services/runner_job_dispatcher.py` (backend dispatch). Release workflow: `.github/workflows/runner-release.yml`.

## Deep Dives

| Topic | Guide |
|-------|-------|
| **Strategic direction** | `VISION.md` — read first! |
| **SQLite pivot plan** | `VISION.md` (SQLite-only OSS Pivot section) |
| Oikos Tools | `apps/zerg/backend/docs/supervisor_tools.md` |
| Sauron scheduler | `apps/sauron/README.md` |
| Sauron job definitions | `~/git/sauron-jobs/` |
| Gmail Pub/Sub architecture | `~/git/life-hub/docs/specs/gmail-pubsub-realtime.md` |
| UI Capture | `/zerg-ui` skill — debug bundles with trace/a11y, `make ui-capture` |

## Jobs: Builtin vs External

Jobs can live in two places:

| Location | When to use |
|----------|-------------|
| **Builtin** (`zerg/jobs/`) | Product functionality - backup-sentinel, QA checks, things OSS users need |
| **External** (`sauron-jobs/`) | David-specific automation - worklog, google-ads-digest, life-hub integrations |

**Rule of thumb:** If an OSS user wouldn't need it, put it in `sauron-jobs/`.

## Demo & Seed Data

Two separate things exist — don't conflate or rebuild:
- **Fast to Fun** (onboarding): `services/demo_sessions.py` — seeds 2 sessions via empty state button
- **Showcase** (demos/videos): `scripts/build_demo_db.py` — builds full DB with scenarios

## Misc
- GH actions use runners on Cube
- if you are manually patching things for coolify you likely did something wrong. Remember infra-as-code.

## Learnings (Recent - Human compacts weekly)

<!-- Agents: append below. Keep last 7 days or 10 entries max. -->
- (2026-02-04) [arch] Single-domain architecture: each user subdomain (alice.longhouse.ai) serves both frontend and API; nginx proxies /api/* to backend via BACKEND_HOST env var. No separate api-alice subdomain needed.
- (2026-02-04) [infra] Control plane uses Coolify's Caddy proxy (caddy-docker-proxy) for routing; wildcard DNS `*.longhouse.ai` now configured in Cloudflare.
- (2026-02-04) [ops] FERNET_SECRET must be urlsafe base64 32-byte key; hex strings cause instance startup failure.
- (2026-02-04) [arch] Runtime image (`docker/runtime.dockerfile`) bundles frontend+backend; backend serves frontend via StaticFiles at `/app/frontend-web/dist`.
- (2026-02-05) [db] Alembic migrations removed (versions dir empty); treat migration tasks as deprecated.
- (2026-02-05) [security] Avoid storing admin tokens in AI session notes; rotate any exposed token immediately.
- (2026-02-05) [ci] Provisioning E2E runs on cube ARC (DIND), builds runtime image, provisions instance, and hits health + timeline smoke checks.
- (2026-02-05) [ops] Instance health uses `/api/health` (readiness) and `/api/livez` (liveness); no root `/health`.
- (2026-02-05) [ops] longhouse.ai and api.longhouse.ai currently return HTTP 525 (Cloudflare origin handshake failure); smoke-after-deploy health checks fail.
- (2026-02-05) [db] SQLite FTS5 index (`events_fts`) now backs session search when available.
- (2026-02-05) [e2e] Chat UI can include existing commis cards; E2E commis UI assertions should target `data-tool-call-id` for the injected tool.
