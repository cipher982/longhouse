# Longhouse (codename: Zerg)

AI agent orchestration platform. Oikos = voice/text UI. Longhouse = product name.

**Owner**: david010@gmail.com (David Rose)

Skills-Dir: .agents/skills

## Philosophy

- **Read VISION.md first** — It's the guiding light. Understand the strategic direction before diving into implementation.
- **SQLite-only core** — SQLite pivot is complete. Don't invest in Postgres-specific infrastructure. See VISION.md.
- **Progressive disclosure** — AGENTS.md should point to deeper docs/runbooks so agents know what they don't know.
- Always commit changes as you go (no lingering uncommitted work). In swarm mode, **lead commits after each teammate's verified work** — don't batch everything into one mega-commit at the end.

**"Trust the AI"** — Modern LLMs are smart enough to figure things out. Give them context and autonomy, not rigid decision trees. No keyword routing, no specialized commiss.

**Current Direction:** SQLite-only for OSS (complete). Postgres is control-plane only. See VISION.md § "No Postgres in core."

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
| localhost:47300/api/health | Health check |

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

**API sub-app:** All `/api/*` routes live on `api_app` (mounted on `app` at `/api`). New routers go on `api_app`, not `app`. Test `dependency_overrides` must target `api_app`. Only `/metrics`, `/config.js`, static mounts, and the SPA catch-all stay on `app`.

**LLM model config:** `config/models.json` is the single source of truth — models, tiers, use cases, and routing profiles. Set `MODELS_PROFILE` env var to select per-instance overrides (default `oss`). Each model can declare `apiKeyEnvVar` for its required API key. See `models_config.py:get_llm_client_for_use_case()` for the factory.

## Conventions

- **Backend**: FastAPI + Pydantic, `apps/zerg/backend/zerg/`
- **Frontend**: React + React Query, `apps/zerg/frontend-web/`
- **Package managers**: Bun (JS), uv (Python) — never npm/pip
- **Generated code** (don't edit): `backend/zerg/generated/`, `backend/zerg/tools/generated/`, `frontend-web/src/generated/`
- **Tests**: Always use `make test*` targets, never direct pytest/playwright
- **Tool contracts**: Edit `schemas/tools.yml`, then run `scripts/generate_tool_types.py` — never edit generated files directly
- **Oikos tools**: Registration is centralized in `oikos_tools.py`; `OIKOS_TOOL_NAMES` + `OIKOS_UTILITY_TOOLS` define the tool subset; `get_oikos_allowed_tools()` is the single source of truth
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
7. **Backend README required** — pyproject.toml needs it; don't delete `apps/zerg/backend/README.md`.
8. **Coolify container names are random hashes** — Don't `docker ps --filter name=X` to find Coolify apps. Use `docker ps` and check labels: `coolify.serviceName` has the logical name (e.g., `longhouse-control-plane`). Or use `coolify app status <name>`.

## Pushing Changes

**Three things get deployed — all on the `zerg` server:**

| What | URL | How |
|------|-----|-----|
| Marketing site | https://longhouse.ai | Coolify app `longhouse-demo` |
| User instance | https://david.longhouse.ai | Docker Compose at `zerg:/opt/longhouse/david/` |
| Control plane | https://control.longhouse.ai | Coolify app `longhouse-control-plane` |

### Before Push
```bash
make test              # Unit tests (required)
make test-e2e          # Core E2E + a11y — must pass 100%
```

### After Push
Push to main triggers `runtime-image.yml` **if backend/frontend/dockerfile changed** (has path filters — docs-only pushes skip it). Builds `ghcr.io/cipher982/longhouse-runtime:latest`. **Does NOT auto-deploy.** Update all three:

**1. Marketing site (Coolify):**
```bash
coolify deploy name longhouse-demo
# Or with wait: ~/git/me/mytech/scripts/coolify-deploy.sh longhouse-demo
```

**2. Control plane (Coolify):**
```bash
coolify deploy name longhouse-control-plane
```

**3. User instance (Docker Compose on zerg:/opt/longhouse/david/):**
```bash
# Config lives at /opt/longhouse/david/ on zerg server:
#   docker-compose.yml  — compose stack definition
#   david.env           — secrets (chmod 600, NOT in git)
# Template for new secrets: docker/david.env.template in this repo.

# Redeploy (pull new image + recreate container)
ssh zerg 'cd /opt/longhouse/david && docker compose pull && docker compose up -d'
```

### Verify Deploy
```bash
make verify-prod                                       # API + browser validation
curl -s https://longhouse.ai | grep '<title>'          # Marketing site content
```

### If Something Breaks
```bash
ssh zerg 'docker logs longhouse-david --tail 50'       # User instance
coolify app logs longhouse-demo                        # Marketing site
coolify app logs longhouse-control-plane               # Control plane
```

### Checklist for Agents
1. `make test` + `make test-e2e` pass locally
2. Push to main (triggers GHCR build if code changed)
3. Deploy marketing site: `coolify deploy name longhouse-demo`
4. Deploy control plane: `coolify deploy name longhouse-control-plane`
5. Deploy user instance: `ssh zerg 'cd /opt/longhouse/david && docker compose pull && docker compose up -d'`
6. Verify: `make verify-prod` + check `longhouse.ai` title
7. Report result to user

## apps/sauron - Scheduler (Folded In)

Sauron is not a separate service; scheduled jobs run inside the standard Longhouse instance service (per-user). The `sauron-jobs` repo pattern remains a power-user path (optional jobs pack), but OSS onboarding should not depend on it.

## apps/runner - Native Runner Daemon

Bun-compiled binary for command execution on user infrastructure. Connects via WebSocket, validates commands client-side (defense-in-depth), streams output. Install via `ENROLL_TOKEN=xxx curl .../install.sh | bash` → launchd (macOS) or systemd (Linux).

**Entrypoints:** `apps/runner/src/index.ts` (daemon), `apps/runner/src/executor.ts` (command exec), `zerg/services/runner_job_dispatcher.py` (backend dispatch). Release workflow: `.github/workflows/runner-release.yml`.

## Product Surface (Canonical)

Source of truth for product surface and priorities: `VISION.md` section **"Product Surface (2026-02 Decision)"**.

Do not maintain a second feature catalog in this file. Keep AGENTS focused on execution rules and link to canonical docs:
- Oikos tool contract: `apps/zerg/backend/docs/supervisor_tools.md`
- Harness simplification plan: `apps/zerg/backend/docs/specs/unified-memory-bridge.md`
- Runner daemon docs: `apps/runner/README.md`
- Control plane docs: `apps/control-plane/README.md`
- Shipper internals: `apps/zerg/backend/zerg/services/shipper/`
- Video production: `apps/video/` (Remotion studio — canonical video pipeline)
- Marketing screenshots: `scripts/capture_marketing.py`, `scripts/screenshots.yaml`

## Jobs: Builtin vs External

Jobs can live in two places:
- **Builtin** (`zerg/jobs/`): Product functionality OSS users need.
- **External** (`sauron-jobs/`): David-specific automation/integrations.

Rule of thumb: if an OSS user wouldn't need it, put it in `sauron-jobs/`.

## Demo & Seed Data

Two separate things exist — don't conflate or rebuild:
- **Fast to Fun** (onboarding): `services/demo_sessions.py` — seeds 2 sessions via empty state button
- **Showcase** (demos/videos): `scripts/build_demo_db.py` — builds full DB with scenarios

## Misc
- GH actions use runners on Cube
- if you are manually patching things for coolify you likely did something wrong. Remember infra-as-code.

## CI Test Runner
- Run: `scripts/ci/run-on-ci.sh <suite> [ref] [--test <path>]` (details: `scripts/ci/README.md`).

## Learnings (High-Signal Only)

<!-- Agents: keep this tight (<=10). Keep durable invariants only. If a learning is code-fixable confusion, add TODO work and remove it after the fix lands. -->
- (2026-02-04) [arch] Runtime image (`docker/runtime.dockerfile`) bundles frontend+backend; backend serves built frontend from `/app/frontend-web/dist`.
- (2026-02-05) [db] Alembic migrations are deprecated for core app work; `apps/zerg/backend/alembic/versions` is intentionally empty.
- (2026-02-05) [security] Never store admin/device tokens in AI session notes; rotate immediately if exposed.
- (2026-02-06) [arch] App mode contract is `APP_MODE` > `DEMO_MODE` > `AUTH_DISABLED/TESTING`; frontend reads runtime mode from backend-served `/config.js`.
- (2026-02-12) [arch] `api_app` sub-app owns all `/api/*` routes; new routers go on `api_app` in `main.py`. Test `dependency_overrides` must target `api_app`, not `app`.
