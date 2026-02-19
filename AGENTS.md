# Longhouse (codename: Zerg)

AI agent orchestration platform. Oikos = voice/text UI. Longhouse = product name.

**Owner**: david010@gmail.com (David Rose)

Skills-Dir: .agents/skills

## Philosophy

- **Read VISION.md first** — It's the guiding light. Understand the strategic direction before diving into implementation.
- **SQLite-only core** — SQLite pivot is complete. Don't invest in Postgres-specific infrastructure. See VISION.md.
- **Progressive disclosure** — AGENTS.md should point to deeper docs/runbooks so agents know what they don't know.
- Always commit changes as you go (no lingering uncommitted work). In swarm mode, **lead commits after each teammate's verified work** — don't batch everything into one mega-commit at the end.
- **Execute autonomously** — When given a plan or task, implement → test → commit → deploy → verify without pausing for approval at each step. Only stop to ask if something is ambiguous or blocked. Don't narrate your progress — deliver results.

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
make test                    # Unit tests (tests_lite/, ~5s, SQLite in-memory)
make test-e2e                # Core E2E + a11y
make test-full               # Full suite (unit + full E2E + evals + visual baselines + visual compare)
make qa-visual-compare       # Visual comparison with Gemini LLM triage
make qa-visual-compare-fast  # Visual comparison (pixelmatch only, no LLM)
```

**Visual compare** (`scripts/visual-compare.ts`): Hybrid pixelmatch + Gemini Flash triage. Catches semantic regressions (color catastrophes, broken layouts) that pixel-perfect baselines miss. Two modes: (1) `--baseline-dir`/`--current-dir` for CI, (2) single-pair for agent use via MCP `visual_compare` tool. Shared page definitions in `apps/zerg/e2e/tests/helpers/page-list.ts`. Requires `GOOGLE_API_KEY` for LLM triage; falls back to hard threshold without it.

**`tests_lite/` convention:** No shared conftest. Each test file creates its own DB via `_make_db(tmp_path)` using `make_engine("sqlite:///...")` + `AgentsBase.metadata.create_all()`. HTTP-level tests use `TestClient` with `dependency_overrides` on `api_app` (not `app`). See `test_job_preflight.py` for a clean example of both ORM-only and HTTP-level patterns.

## Architecture

**Stack:** FastAPI + SQLAlchemy + SQLite (dev/OSS) or Postgres (prod control-plane). React frontend. See `VISION.md` for details.

**API sub-app:** All `/api/*` routes live on `api_app` (mounted on `app` at `/api`). New routers go on `api_app`, not `app`. Test `dependency_overrides` must target `api_app`. Only `/metrics`, `/config.js`, static mounts, and the SPA catch-all stay on `app`.

**LLM model config:** `config/models.json` is the single source of truth — models, tiers, use cases, routing profiles, and embedding config. Set `MODELS_PROFILE` env var to select per-instance overrides (default `oss`). Each model can declare `apiKeyEnvVar` for its required API key. See `models_config.py:get_llm_client_for_use_case()` for LLM factory, `get_embedding_config()` for embeddings (gracefully returns None if no API key).

**MCP server:** `zerg/mcp_server/server.py:create_server()` exposes tools to CLI agents (Claude Code, Codex, Gemini). Tools: session search (FTS + semantic), recall, memory read/write, insights, file reservations, notify_oikos, visual_compare. New tools go inside `create_server()`. API client at `mcp_server/api_client.py` (get/post/delete methods).

## Features (What Exists)

| Feature | API | Frontend | MCP | Notes |
|---------|-----|----------|-----|-------|
| Session timeline | `GET /api/agents/sessions` | SessionsPage | `search_sessions` | FTS search, filters by project/provider/days |
| Session detail | `GET /api/agents/sessions/:id` | SessionDetailPage | — | Full event transcript |
| **Semantic search** | `GET /api/agents/sessions/semantic?query=` | **Not wired up** | `search_sessions` | Requires embeddings (OpenAI text-embedding-3-small, 256d) |
| **Recall** | `GET /api/agents/recall?query=` | **Not wired up** | `recall` | Turn-level knowledge retrieval with context windows |
| Embeddings | `POST /api/agents/backfill-embeddings` | — | — | Auto-generated on ingest; backfill for existing sessions |
| Oikos (chat) | WebSocket `/ws/oikos` | OikosPage | — | Voice/text AI assistant with tool use |
| Insights | `GET/POST /api/agents/insights` | InsightsPage | `log_insight`, `query_insights` | Patterns, failures, learnings |
| Reflection | `GET /api/agents/reflection/briefing` | **Not wired up** | `get_briefing` | Daily/weekly session briefings |
| Jobs/Scheduler | `GET /api/agents/jobs` | JobsPage | — | Cron jobs with enable/disable, secrets |
| Memory | `GET/PUT /api/agents/memory` | — | `read_memory`, `write_memory` | Key-value store for agent state |
| File reservations | `POST /api/agents/reservations` | — | `reserve_file` | Prevent concurrent edits |
| Action proposals | `GET/POST /api/proposals` | ProposalsPage | — | Review queue for reflection insights with approve/decline; approved proposals show in briefings |
| Runner daemon | WebSocket from runner binary | — | — | Remote command execution on user infra |

## Conventions

- **Backend**: FastAPI + Pydantic, `apps/zerg/backend/zerg/`
- **Frontend**: React + React Query, `apps/zerg/frontend-web/`
- **Package managers**: Bun (JS), uv (Python) — never npm/pip
- **Generated code** (don't edit): `backend/zerg/generated/`, `backend/zerg/tools/generated/`, `frontend-web/src/generated/`
- **Tests**: Always use `make test*` targets, never direct pytest/playwright. New backend tests go in `tests_lite/`.
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
8. **Stripe key rotation** — Use `~/git/me/mytech/scripts/update-stripe-key.sh sk_live_...`. It validates against Stripe before touching anything, then updates Coolify and redeploys.
9. **Coolify container names are random hashes** — Don't `docker ps --filter name=X` to find Coolify apps. Use `docker ps` and check labels: `coolify.serviceName` has the logical name (e.g., `longhouse-control-plane`). Or use `coolify app status <name>`.
9. **Pre-commit hooks** — ruff, ruff-format, vulture (dead code), TS type-check, frontend lint. Vulture whitelist: `apps/zerg/backend/vulture-whitelist.py`. New `TYPE_CHECKING` imports need whitelisting or vulture will block commit.
10. **Deploy requires GHCR build** — Push triggers `runtime-image.yml` (path-filtered). Must wait for build before pulling on zerg. Use `gh run watch <id>` to wait. Marketing + control plane pull the same image via Coolify.
11. **Stage only your changes** — Dirty trees are normal (other agents' WIP). When committing, `git add` specific files — never `git add -A`. If new code depends on unstaged changes from other files, include those files or the deploy will break.
12. **LiteLLM proxy metadata required** — All OpenAI SDK calls on instances go through `llm.drose.io`. Must pass `extra_body={"metadata": {"source": "longhouse:component"}}` or get 400. New models must be added to `~/git/litellm-proxy/config.yaml` AND `hooks/model_hints.py`.
13. **DB provider config overrides env vars** — `get_llm_client_with_db_fallback()` checks `LlmProviderConfig` table first. Stale rows with wrong keys cause silent 401s. Check DB before debugging API auth failures.

## Pushing Changes

**Three things get deployed — all on the `zerg` server:**

| What | URL | How |
|------|-----|-----|
| Marketing site | https://longhouse.ai | Coolify app `longhouse-demo` |
| Control plane | https://control.longhouse.ai | Coolify app `longhouse-control-plane` |
| User instances | https://david010.longhouse.ai | Control plane reprovision (pulls latest image) |

**User instances** are provisioned and managed by the control plane. There is no docker-compose path — all instances are containers created by the provisioner. To update an instance to the latest image, reprovision it via the admin API.

**Dev instances:**
- **david010.longhouse.ai** — primary dev instance (david010@gmail.com, persistent data). Use for feature dev and prod debugging.
- **david@drose.io** — ephemeral instance for testing signup flow. Can be nuked/reprovisioned freely.

### Before Push
```bash
make test              # Unit tests (required)
make test-e2e          # Core E2E + a11y — must pass 100%
```

### After Push
Push to main triggers `runtime-image.yml` **if backend/frontend/dockerfile changed** (has path filters — docs-only pushes skip it). Builds `ghcr.io/cipher982/longhouse-runtime:latest`. **Does NOT auto-deploy.** Deploy steps:

**1. Marketing site (Coolify):**
```bash
coolify deploy name longhouse-demo
# Or with wait: ~/git/me/mytech/scripts/coolify-deploy.sh longhouse-demo
```

**2. Control plane (Coolify):**
```bash
coolify deploy name longhouse-control-plane
```

**3. User instances (reprovision via control plane):**
```bash
# Get admin token
ssh zerg 'docker exec <control-plane-container> env | grep ADMIN_TOKEN'

# List instances
curl -s -H "X-Admin-Token: $TOKEN" https://control.longhouse.ai/api/instances

# Reprovision (stops old container, creates new one with latest image + current secrets)
curl -s -X POST -H "X-Admin-Token: $TOKEN" https://control.longhouse.ai/api/instances/<id>/reprovision
```
Note: reprovisioning generates a new password. Data is safe — SQLite lives on a host bind mount, not inside the container.

### Verify Deploy
```bash
make verify-prod                                       # API + browser validation
curl -s https://longhouse.ai | grep '<title>'          # Marketing site content
curl -s https://david010.longhouse.ai/api/health       # User instance health
```

### If Something Breaks
```bash
ssh zerg 'docker logs longhouse-david010 --tail 50'    # User instance
coolify app logs longhouse-demo                        # Marketing site
coolify app logs longhouse-control-plane               # Control plane
```

### Checklist for Agents
1. `make test` + `make test-e2e` pass locally
2. Push to main (triggers GHCR build if code changed)
3. Wait for GHCR build: `gh run watch <id> --exit-status`
4. Deploy marketing + control plane (Coolify)
5. Reprovision user instances via control plane admin API
6. Verify: health checks on all endpoints
7. If deploy fails, check logs, fix, and redeploy — don't ask the user
8. Brief summary only at end (what shipped, what to manually verify if needed)

## apps/sauron - Scheduler (Folded In)

Sauron is not a separate service; scheduled jobs run inside the standard Longhouse instance service (per-user). The `sauron-jobs` repo pattern remains a power-user path (optional jobs pack), but OSS onboarding should not depend on it. If the user is asking about his jobs or sauron, take a look at ~/git/sauron-jobs and they should be running 24/7 on the instance and sending him emails.

## apps/runner - Native Runner Daemon

Bun-compiled binary for command execution on user infrastructure. Connects via WebSocket, validates commands client-side (defense-in-depth), streams output. Install via `ENROLL_TOKEN=xxx curl .../install.sh | bash` → launchd (macOS) or systemd (Linux).

**Entrypoints:** `apps/runner/src/index.ts` (daemon), `apps/runner/src/executor.ts` (command exec), `zerg/services/runner_job_dispatcher.py` (backend dispatch). Release workflow: `.github/workflows/runner-release.yml`.

## Product Surface (Canonical)

Source of truth for product surface and priorities: `VISION.md` section **"Product Surface (2026-02 Decision)"**.

Do not maintain a second feature catalog in this file. Keep AGENTS focused on execution rules and link to canonical docs:
- Oikos tool contract: `apps/zerg/backend/docs/supervisor_tools.md`
- Harness simplification plan: `apps/zerg/backend/docs/specs/unified-memory-bridge.md`
- Agent infra consolidation spec: `docs/specs/agent-infrastructure-consolidation.md`
- Runner daemon docs: `apps/runner/README.md`
- Control plane docs: `apps/control-plane/README.md`
- Shipper internals: `apps/zerg/backend/zerg/services/shipper/` (Python — hooks, install, auth)
- Rust engine (high-perf shipper daemon): `apps/engine/` — `longhouse-engine connect` replaces Python watcher daemon; 27 MB RSS idle vs 835 MB Python
- Session processing: `apps/zerg/backend/zerg/services/session_processing/` (summarize, embeddings, content, tokens)
- Embedding cache: `apps/zerg/backend/zerg/services/embedding_cache.py`
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
- (2026-02-05) [db] Alembic migrations are deprecated for core app work; `apps/zerg/backend/alembic/versions` is intentionally empty. New models use `AgentsBase.metadata.create_all()` auto-creation.
- (2026-02-05) [security] Never store admin/device tokens in AI session notes; rotate immediately if exposed.
- (2026-02-06) [arch] App mode contract is `APP_MODE` > `DEMO_MODE` > `AUTH_DISABLED/TESTING`; frontend reads runtime mode from backend-served `/config.js`.
- (2026-02-12) [arch] Agent infra models use `AgentsBase` (not `Base`), live in `models/agents.py` and `models/work.py`. Schema `agents.` gets translate-mapped to `None` for SQLite.
- (2026-02-12) [frontend] Frontend API errors: `ApiError` class has `status`, `url`, `body` (already-parsed object, not string). FastAPI wraps HTTPException detail in `{detail: ...}`, so structured error data is at `body.detail.field`.
- (2026-02-13) [arch] Reflection produces **action proposals** alongside insights when `action_blurb` is present (high-confidence, concrete actions). Users review at `/proposals`. Approved proposals appear in agent briefings under "Approved actions (pending execution)." Model: `ActionProposal` in `models/work.py`, API: `routers/proposals.py`.
- (2026-02-14) [ops] **Reprovisioning an instance** = stop+remove container, then re-create with current env vars. Data is safe — SQLite lives on a host bind mount (`/var/lib/docker/data/longhouse/<subdomain>`), not inside the container. Use the admin API: `POST /api/instances/{id}/reprovision`. If secrets change on the control plane, instances must be reprovisioned to pick them up.
- (2026-02-14) [security] SSO tokens include `instance` claim binding them to a specific subdomain. `accept_token` validates this (soft — only when both claim and `INSTANCE_ID` env var present). CP's own `jwt_secret` is never sent to instances.
- (2026-02-16) [ops] Instances route LLM calls through LiteLLM proxy (`llm.drose.io`). All OpenAI SDK calls must pass `extra_body={"metadata": {"source": "longhouse:component"}}` or the proxy rejects with 400. Proxy allowlist: `~/git/litellm-proxy/config.yaml` + `hooks/model_hints.py` (keep in sync).
- (2026-02-16) [ops] `get_llm_client_with_db_fallback()` checks DB `LlmProviderConfig` table before env vars. Stale DB rows with wrong API keys silently override env config → 401s. Delete stale rows: `db.query(LlmProviderConfig).filter(...).delete()`.
