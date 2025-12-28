# AGENTS.md

## What This Is

**Zerg** is an AI agent orchestration platform. **Jarvis** is a voice/text UI that connects to Zerg. Together they form a "swarm" system where Jarvis handles user interaction and Zerg manages multi-agent workflows.

## Architecture Philosophy (v2.2)

**"Trust the AI"** — Modern LLMs are smart enough to figure things out. Give them context and autonomy, not rigid decision trees.

Key principles:
- **No keyword routing** — Jarvis decides when to delegate, not keyword matching
- **No specialized workers** — Workers are general-purpose agents with SSH access
- **Session Durability** — Runs survive disconnects and timeouts; work continues in background
- **No tool allowlists** — Security via capability boundaries (which hosts can SSH to), not tool restrictions
- **Event-driven** — Workers notify when done, no polling loops

Full spec: `docs/specs/durable-runs-v2.2.md`
Architecture overview (v2.1): `docs/specs/jarvis-supervisor-unification-v2.1.md`
Historical (v2.0): `docs/archive/super-siri-architecture.md`

## For AI Agents

**Assume `make dev` is running** — during coding sessions, the dev stack is typically already up.

When running, you have live access to:
| URL | What |
|-----|------|
| http://localhost:30080 | Main entry (nginx) |
| http://localhost:30080/dashboard | Dashboard UI |
| http://localhost:30080/chat | Jarvis chat |
| http://localhost:30080/api/* | Backend API |

**Debug with:**
- **Playwright MCP** — snapshot pages, click elements, check console errors
- **API calls** — `curl localhost:30080/api/health` or WebFetch tool
- **Logs** — `make logs` to tail all services

**Frontend Logging Modes:**

Control console log verbosity via URL parameter `?log=<level>`:

| Mode | URL | Behavior |
|------|-----|----------|
| **minimal** | `?log=minimal` | Errors/warnings only |
| **normal** | `?log=normal` (default) | Errors/warnings + key events (start, complete) |
| **verbose** | `?log=verbose` | Everything (EventBus events, SSE stream details, all debug logs) |
| **timeline** | `?log=timeline` | Timeline output only (minimal noise + performance timing) |

**Examples:**
- Chat with minimal noise: `http://localhost:30080/chat?log=minimal`
- Chat with timeline profiling: `http://localhost:30080/chat?log=timeline`
- Full debug mode: `http://localhost:30080/chat?log=verbose`

**Implementation:**
- Logger config: `apps/zerg/frontend-web/src/jarvis/core/logger.ts`
- Timeline logger: `apps/zerg/frontend-web/src/jarvis/lib/timeline-logger.ts`
- Phase 5 reduced console noise by 70%+ for normal/timeline modes (chat-observability-eval spec)

**If not running:** `make dev-bg` (background, non-blocking) — wait for containers to become healthy.

## Architecture (Unified SPA)

```
User → http://localhost:30080 (nginx)
  /            → Zerg dashboard (React SPA)
  /dashboard   → Zerg dashboard (alias)
  /chat        → Jarvis chat UI (part of Zerg SPA)
  /api/*       → FastAPI backend (includes Jarvis BFF at /api/jarvis/*)
  /ws/*        → SSE/WS

Internal service ports (dev):
  Zerg backend       47300
  Zerg frontend      47200
```

**Note**: Jarvis chat UI is now integrated into the Zerg frontend SPA at `src/jarvis/`.
There is no separate jarvis-web service.

## Package Managers

- **JavaScript**: Bun only.
  - Monorepo lockfile: `bun.lock` (repo root) — run `bun install` from repo root.
- **Python**: uv only. Run `cd apps/zerg/backend && uv sync`.

Do not use npm, yarn, pip, or poetry.

## Key Commands

```bash
# Start everything (Docker + nginx + services)
make dev           # Full platform with nginx proxy (profile: dev)
                   # ⚠️ Interactive - tails logs until Ctrl+C
                   # ⚠️ Auto-rebuilds images (--build flag included)
make dev-bg        # Same but background (for CI/automation)

# Environment validation
make env-check     # Validate required env vars before starting

# Stop everything
make stop

# Run tests (ALWAYS use Make targets, never direct pytest/bun commands)
make test          # Unit tests only (backend + frontend)
make test-e2e      # Playwright E2E only
make test-all      # Unit + Playwright E2E
make test-chat-e2e # Chat page E2E (unified SPA)

# Targeted E2E testing
make test-e2e-single TEST=tests/unified-frontend.spec.ts # Or any Playwright args
make test-e2e-grep GREP="test name"
make test-e2e-ui   # Interactive mode

# Regenerate generated code
make generate-sdk  # OpenAPI types
make regen-ws      # WebSocket contract code
make regen-sse     # SSE event contract code

# Seeding (local dev data)
make seed-agents       # Seed Jarvis agents
make seed-credentials  # Seed personal tool credentials (Traccar, WHOOP, Obsidian)

# Validation (CI checks these)
make validate      # All validation

# Performance profiling (GPU, macOS only)
make perf-gpu           # Landing page GPU measurement
make perf-gpu-dashboard # Dashboard GPU measurement
```

## Deploying + Coolify Debugging

**When a Coolify deployment fails, ALWAYS follow this sequence:**

### 1. Get Full Logs First (Required)
```bash
./scripts/get-coolify-logs.sh 1 > /tmp/coolify-error.log
```
- Coolify UI truncates; this gives complete output
- Searches in `/tmp/coolify-error.log` for actual errors
- Never guess or patch without seeing the full error

### 2. Verify Repo State (Most Common Issues)
```bash
# Check if files are actually in git
git ls-files <failing-path>

# Check if .gitignore is excluding source code
git check-ignore -v <failing-path>

# Compare local vs what Coolify clones
git show HEAD:<path>
```

**Common footgun:** `.gitignore` patterns like `data/` or `dist/` excluding source code.
- Use `/data/` for root-level only
- Use `!apps/<workspace>/dist/` (or similar) for exceptions

### 3. Test Build Locally (Same Context as Coolify)
```bash
docker build -f apps/zerg/frontend-web/Dockerfile --target production .
docker compose -f docker/docker-compose.prod.yml build
```

### 4. Categorize the Failure

| Error Pattern | Root Cause | Fix Location |
|--------------|------------|--------------|
| "Cannot find module" in TypeScript | Missing files in git | `.gitignore` + `git add` |
| "Variable not set" warnings | Env vars not at build time | Coolify UI → "Available at Build Time" |
| "Command failed with no error output (255)" | Coolify SSH/exec bug | Check `docker logs coolify` on clifford |
| "exit code 137" | OOM killed | Upgrade server or reduce parallelism |
| "Workspace not found" | Bun monorepo resolution | Check Docker COPY includes all workspaces |

**Never add Docker workarounds before completing steps 1-3.**

**Full guide:** `docs/COOLIFY_DEBUGGING.md`
**Architecture:** `docs/DEPLOYMENT.md`
**Known tech debt:** `docs/DEPLOYMENT_CLEANUP_PLAN.md`

## Docker Compose Profiles

Dev compose file (profiles live here): `docker/docker-compose.dev.yml`

Other compose files:
- Standalone production compose: `docker/docker-compose.prod.yml`

| Compose | Services | Use Case |
|--------|----------|----------|
| `docker/docker-compose.dev.yml` (profile `dev`) | postgres, backend, frontend, reverse-proxy, dev-runner | Local development via nginx at 30080 |
| `docker/docker-compose.prod.yml` | postgres, backend, frontend, reverse-proxy | Production deployment (typically via Coolify) |

```bash
# Direct compose usage (prefer make targets)
docker compose --project-name zerg --env-file .env -f docker/docker-compose.dev.yml --profile dev up
```

## Testing Infrastructure

**CRITICAL:** ALWAYS use Make targets for testing. Never run pytest/bun commands directly (they miss env vars, wrong CWD, no parallelism).

### Test Commands

| Command | What It Runs | Notes |
|---------|--------------|-------|
| `make test` | Unit tests (backend + frontend) | Runs in parallel |
| `make test-e2e` | Playwright E2E tests | Runs in parallel |
| `make test-all` | Unit + E2E combined | Runs both suites |
| `make test-e2e-single TEST=<spec>` | Single E2E test file | Most useful for iteration |
| `make test-e2e-grep GREP="name"` | E2E tests matching name | Narrow by test name |
| `make test-e2e-ui` | Interactive Playwright UI | Local debugging |

**Backend tests use pytest-xdist (`-n auto`)** - parallel execution across all CPU cores
**E2E tests use Playwright workers** - uses all CPU cores locally (4 workers in CI only)

### E2E Test Isolation
- The backend uses a per-worker SQLite DB file (so Playwright workers don't share a single DB).
- Playwright artifacts (screenshots, traces, reports) are written under `apps/zerg/e2e/test-results/` and `apps/zerg/e2e/playwright-report/`.

### Database Initialization
- The backend automatically detects missing tables and initializes the schema on startup via `scripts/init_db.py`.
- No manual migration steps are required for fresh test environments.

### Debugging E2E Failures (Quick Checklist)
1. Re-run just the failing spec: `make test-e2e-single TEST=<spec-or-grep>`
2. Open the Playwright report: `cd apps/zerg/e2e && bunx playwright show-report`
3. If you suspect missing parallelism, use `apps/zerg/e2e/scripts/run_parallelism_probe.sh` and `apps/zerg/e2e/scripts/run_timeline.sh`

### Live Integration Testing (Backend Tools)
To verify core supervisor tools (KV memory, Tasks, Web Search) against a running backend:

```bash
# 1. Start backend
make dev

# 2. Run live tests (requires running backend + auth token)
cd apps/zerg/backend && uv run python -m pytest tests/live --live-token <YOUR_JWT>
```

**Note**: No Make target for live tests (requires manual JWT token). These dispatch real tasks to the Supervisor and verify the SSE stream results.

## Conventions

### Backend (Python)
- Location: `apps/zerg/backend/zerg/`
- Tests: `apps/zerg/backend/tests/` — run with `make test`
- Debug scripts: `scripts/debug_*.py` — helpers for workflow/execution debugging
- Uses FastAPI with Pydantic models
- Agent logic uses LangGraph
- Routers in `routers/`, services in `services/`, models in `models/`

### Frontend (TypeScript/React)
- Zerg dashboard: `apps/zerg/frontend-web/`
- Uses React Query for data fetching
- Generated OpenAPI types in `src/generated/openapi-types.ts` — do not edit
- Run `bun run generate:api` to regenerate types from backend schema

### Frontend Styling Conventions (2025 Refactor)
- **Scope Styles**: CSS must be scoped to a container class (e.g., `.dashboard-container`, `.chat-view-container`) to prevent global collisions.
- **Layers**: Use `@layer` to control specificity (`tokens`, `base`, `legacy`, `components`, `pages`).
- **Tokens**: Use `--color-*` and `--space-*` variables from `src/styles/tokens.css`.
- **No Global Classes**: Avoid generic class names like `.back-button` or `.empty-state` at the root level.
- **Tools**: Tokens are edited directly (no build step); `bun run build` catches invalid CSS nesting in the Vite build pipeline.

### Jarvis (TypeScript - integrated into Zerg frontend)
- Chat UI: `apps/zerg/frontend-web/src/jarvis/` (integrated into Zerg SPA)
- Entry point: `src/pages/JarvisChatPage.tsx` (renders at `/chat`)
- BFF endpoints: Served by zerg-backend at `/api/jarvis/*`

The Jarvis code lives in `src/jarvis/` with this structure:
- `app/` — React components, hooks, context
- `lib/` — Utilities (voice, session, audio controllers)
- `core/` — Core utilities (logger, API client)
- `data/` — IndexedDB storage
- `styles/` — Jarvis-specific CSS

**Reasoning Effort Feature:**
- Chat UI includes reasoning effort selector (none/low/medium/high)
- Controls OpenAI reasoning intensity for gpt-5.2, o1, o3 models
- Displays reasoning tokens badge in UI when >0
- Implementation uses raw AsyncOpenAI (now upgraded to LangChain `usage_metadata` v1.2.5+)

## Generated Code — Do Not Edit

- `apps/zerg/frontend-web/src/generated/` — OpenAPI types
- `apps/zerg/backend/zerg/generated/ws_messages.py` — WebSocket messages (generated)
- `apps/zerg/frontend-web/src/generated/ws-messages.ts` — WebSocket messages (generated)
- `apps/zerg/backend/zerg/generated/sse_events.py` — SSE events (generated)
- `apps/zerg/frontend-web/src/generated/sse-events.ts` — SSE events (generated)
- `apps/zerg/backend/zerg/tools/generated/tool_definitions.py` — Tool registry types (from `schemas/tools.yml`)

Run `make regen-ws` if WebSocket schema changes. Run `make regen-sse` if SSE schema changes. Run `make generate-sdk` if API changes.

## Pre-commit (AI Agent Safety)

Pre-commit hooks run automatically on every commit. Agents should be aware of what will block commits.

### What Runs

| Hook | Behavior | Blocks? |
|------|----------|---------|
| ruff lint | Auto-fixes Python issues | No (auto-fix) |
| ruff format | Auto-fixes formatting | No (auto-fix) |
| TypeScript types | `tsc --noEmit` | **Yes** |
| ESLint | Frontend lint | Warns only (100 max) |
| WS drift | Contract sync check | **Yes** |
| SSE drift | Contract sync check | **Yes** |
| AsyncAPI | Schema validation | **Yes** |

### Common Failures & Fixes

| Error | Cause | Fix |
|-------|-------|-----|
| `bare except` (E722) | Using `except:` | Use `except Exception:` |
| Unused variable (F841) | Assigned but never used | Prefix with `_` or remove |
| Line too long (E501) | Ignored in this repo | N/A (disabled) |
| TypeScript type error | Actual type mismatch | Fix the types |
| WS drift | Schema out of sync | Run `make regen-ws` |
| SSE drift | Schema out of sync | Run `make regen-sse` |

### Ruff Configuration

Located in `apps/zerg/backend/pyproject.toml`:
- **Line length**: 140 (generous for descriptive code)
- **Ignored**: E501 (line length) - prompt templates are intentionally long
- **Scope**: Only `apps/zerg/backend/zerg/` (excludes alembic, scripts, tests from format enforcement)
- **Generated files excluded**: `apps/zerg/backend/zerg/generated/`

### Testing Pre-commit Locally

```bash
# Run all hooks on all files
pre-commit run --all-files

# Run specific hook
pre-commit run ruff --all-files

# Skip hooks (escape hatch for WIP commits)
git commit --no-verify -m "WIP: skip hooks"
```

### Generated Files — Never Edit

These are excluded from linting and regenerated from schemas:
- `apps/zerg/backend/zerg/generated/ws_messages.py`
- `apps/zerg/frontend-web/src/generated/ws-messages.ts`
- `apps/zerg/backend/zerg/generated/sse_events.py`
- `apps/zerg/frontend-web/src/generated/sse-events.ts`
- `apps/zerg/frontend-web/src/generated/openapi-types.ts`

If you edit these, your changes will be overwritten by `make regen-ws`, `make regen-sse`, or `make generate-sdk`.

## Gotchas

1. **Auth in dev vs prod**: unified compose sets `AUTH_DISABLED=1` backend and `VITE_AUTH_ENABLED=false` frontend for local. Re-enable auth in prod.

2. **CORS/SSE**: same-origin via nginx at 30080; keep run_id on worker tool events for Jarvis ticker.

3. **WebSocket code must stay in sync**: run `make regen-ws` after touching `schemas/ws-protocol.schema.json`.

4. **Ports**: external entry 30080; service ports (47200/47300) are internal. Avoid binding conflicts if running pieces standalone.

5. **Python virtual env**: backend uses `apps/zerg/backend/.venv/`; use `uv run` or activate venv.

6. **E2E Test Isolation**: E2E tests run in a separate Docker project (`zerg-e2e`). If tests fail due to "database connection" issues, ensure no other containers are conflicting on the internal network and try `make test-e2e-reset`.

7. **Auth in Tests**: When `AUTH_DISABLED=1` (default for tests), the `/api/auth/verify` endpoint always returns 204. This allows the frontend to proceed without a real login.

8. **E2E runs on an insecure origin**: Playwright hits `http://reverse-proxy` in Docker (service name), which is **not** a secure context — `crypto.randomUUID()` may be unavailable. Use `src/jarvis/lib/uuid.ts` instead of calling `crypto.randomUUID()` directly.

9. **Prefer Make targets for E2E**: `make` loads/export vars from `.env`. Running raw `docker compose` commands directly can fail if environment variables aren't exported in your shell.

10. **Deterministic UI tests**: For progress-indicator/UI behavior tests, prefer emitting events via `window.__jarvis.eventBus` (DEV only) instead of relying on the LLM to spawn workers/tools.

11. **WebRTC tests in Docker**: When `SKIP_WEBRTC_TESTS=true`, WebRTC-based tests should be *describe-level* skipped so `beforeEach` doesn't fail in the Docker E2E environment.

12. **AGENTS vs CLAUDE**: `CLAUDE.md` is a symlink for tool compatibility. Treat `AGENTS.md` as canonical and only edit `AGENTS.md`.

13. **Generated tool types path**: `scripts/generate_tool_types.py` writes to `apps/zerg/backend/zerg/tools/generated/` relative to repo root (safe from any CWD). If you ever see a duplicate tree like `apps/zerg/backend/apps/...`, it's a stray artifact and can be deleted.

14. **CSS Collisions**: Global class names (e.g., `.back-button`, `.empty-state`) can leak across pages if not scoped. Always nest page-specific styles under a root container class (e.g., `.dashboard-container { ... }`).

15. **`make dev` is interactive**: It starts services, then tails logs forever (never exits). Run it in your terminal, not via automated tools expecting completion. To rebuild after Dockerfile changes, just `make stop && make dev` — the `--build` flag is already included.

16. **Never use raw `docker compose` for dev**: Always use Make targets (`make dev`, `make stop`, `make logs`). Raw `docker compose` commands use wrong project names, miss env vars from `.env`, and create containers on isolated networks that can't communicate with the rest of the stack. If you must, use the pattern from Makefile line 17: `docker compose --project-name zerg --env-file .env -f docker/docker-compose.dev.yml ...`

17. **Never run tests directly**: Always use `make test` / `make test-e2e` instead of direct `pytest` / `bun test` commands. Make targets ensure:
   - Correct working directory
   - Environment variables loaded from `.env`
   - Parallel execution enabled (pytest uses `-n auto`, Playwright uses 4 workers)
   - Proper test isolation (separate DB per worker)
   - Comparable results locally and in CI

18. **UI effects toggle**: Dashboard/app pages use static backgrounds + glass panels by default. Use `?effects=off` URL param or `VITE_UI_EFFECTS=off` to disable ambient visuals entirely. Landing page has its own animated effects (hero, particles) controlled separately via `?fx=none`.

## Environment Setup

Copy `.env.example` to `.env` and fill in:
- `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` — Required for Docker
- `OPENAI_API_KEY` — Required for Jarvis and LLM features

Run `make env-check` to validate your environment before starting.

Dev auth defaults (`AUTH_DISABLED=1`, `VITE_AUTH_ENABLED=false`) are set in compose. For production, set `AUTH_DISABLED=0` and configure Google OAuth credentials.

## Auto-Seeding (User Context & Credentials)

On startup, the backend automatically seeds user context and credentials from local config files. This is **idempotent** (safe to run repeatedly).

### Config File Locations (checked in order)
| File | Dev Path (relative to backend) | Prod Path |
|------|----------|-----------|
| User context | `scripts/user_context.local.json` | `~/.config/zerg/user_context.json` |
| Credentials | `scripts/personal_credentials.local.json` | `~/.config/zerg/personal_credentials.json` |

### Setup (First Time)
```bash
# Copy examples and edit with your details
cd apps/zerg/backend
cp scripts/user_context.example.json \
   scripts/user_context.local.json

cp scripts/personal_credentials.example.json \
   scripts/personal_credentials.local.json

# Edit the files with your servers, integrations, and credentials
# Then restart the backend (or it will seed on next `make dev`)
```

The `.local.json` files are git-ignored. User context includes:
- **servers**: Names, IPs, purposes (injected into supervisor/worker prompts)
- **integrations**: Health trackers, notes apps, backup systems
- **custom_instructions**: Personal preferences for AI responses

## Personal Tools & Credentials (v2.1 Phase 4)

Jarvis supports personal integrations for location, health, and notes. These are **Supervisor-owned tools** that use per-user encrypted credentials.

### Available Personal Tools

| Tool | Integration | Purpose |
|------|-------------|---------|
| `get_current_location` | Traccar | GPS location from tracking server |
| `get_whoop_data` | WHOOP | Health metrics (recovery, HRV, sleep, strain) |
| `search_notes` | Obsidian | Search personal notes via Runner |

### Local Development Setup

**1. Create credentials file:**
```bash
cp apps/zerg/backend/scripts/personal_credentials.example.json \
   apps/zerg/backend/scripts/personal_credentials.local.json
```

**2. Fill in your credentials** (file is git-ignored):
```json
{
  "traccar": {
    "url": "http://5.161.97.53:5055",
    "username": "admin",
    "password": "your-password",
    "device_id": "1"
  },
  "whoop": {
    "client_id": "your-oauth-app-client-id",
    "client_secret": "your-oauth-app-client-secret",
    "access_token": "from-oauth-flow",
    "refresh_token": "from-oauth-flow"
  },
  "obsidian": {
    "vault_path": "~/git/obsidian_vault",
    "runner_name": "laptop"
  }
}
```

**3. Start dev** (auto-seeds on startup):
```bash
make dev
```

Or seed manually:
```bash
make seed-credentials
```

### Production Setup

On production server, seed credentials for your user:
```bash
# Via SSH
python scripts/seed_personal_credentials.py --email your@email.com

# Or create ~/.config/zerg/personal_credentials.json
# Then run: python scripts/seed_personal_credentials.py
```

### Avoid compats / legacy / shims
- this app has no users, never launched, and only has one dev. Keep the code simple. If you see anything that is slightly too complex, recommend a complete refactor to start fresh.

### Credential Security

- ✅ **Local config** (`*.local.json`) is git-ignored
- ✅ **Database storage** is Fernet-encrypted (AES-GCM)
- ✅ **No secrets in code** - all credentials come from config files
- ✅ **Per-user** - stored in `account_connector_credentials` table
- ⚠️ **Change defaults** - Traccar default password is `admin`

### Seeding Commands

```bash
make seed-agents       # Seed Jarvis agents (Morning Digest, Health Watch, etc)
make seed-credentials  # Seed personal tool credentials (Traccar, WHOOP, Obsidian)
```

Both are **idempotent** (safe to run multiple times). Use `--force` to overwrite:
```bash
make seed-credentials ARGS="--force"
```

### For More Details

- **Traccar Setup**: See `TRACCAR_QUICKSTART.md` and `apps/zerg/backend/scripts/TRACCAR_SETUP.md`
- **Test Connection**: `uv run scripts/test_traccar.py`
- **WHOOP OAuth**: Register app at https://developer.whoop.com
- **Obsidian**: Requires a Runner on the machine with your vault

## Production Deployment

**Product Name**: Swarmlet (rebranded from Zerg)

| Service | URL | Server |
|---------|-----|--------|
| Frontend (Dashboard + Landing) | https://swarmlet.com | zerg (Hetzner VPS) |
| Backend API | https://api.swarmlet.com | zerg (Hetzner VPS) |
| Jarvis Chat | https://swarmlet.com/chat | zerg (Hetzner VPS) |

### Infrastructure Architecture
```
clifford (Coolify master)      zerg (build/runtime)
├─ Coolify PHP + DB            ├─ Docker containers
├─ API at localhost:8000       ├─ Build artifacts
└─ SSH → zerg for deploys      └─ App runtime
```

- **Coolify runs on clifford** - orchestrates deploys, stores logs in `coolify-db`
- **Containers run on zerg** - target server for Swarmlet
- **Auto-deploy**: Push to `main` → Coolify detects → SSH to zerg → rebuild

### Deployment Commands (Agent-Friendly)
```bash
# Full automated deploy (trigger + poll + smoke tests)
./scripts/deploy-prod.sh

# Or step by step:
git push origin main                    # Triggers auto-deploy
./scripts/get-coolify-logs.sh 1         # Check status
./scripts/smoke-prod.sh                 # Validate endpoints
```

### Server Access
- **ssh zerg** - Container logs, runtime state
- **ssh clifford** - Coolify DB, deployment logs, API

See `docs/COOLIFY_DEBUGGING.md` for full debugging workflow.

### Vite Environment Variables

**CRITICAL:** `VITE_*` variables are BUILD-TIME, not runtime:
- Must be present during `bun run build` (baked into JS bundle)
- Changing env vars in Coolify requires REBUILD, not just restart
- Use Coolify API `/deploy` endpoint, not `/restart` endpoint

Example:
```bash
# After adding VITE_UMAMI_WEBSITE_ID to Coolify:
# WRONG: /restart (uses old build)
# RIGHT: /deploy?uuid=mosksc0ogk0cssokckw0c8sc&force=true (rebuilds with new vars)
```

## Documentation

| Doc | Purpose |
|-----|---------|
| `docs/DEVELOPMENT.md` | Local dev setup, troubleshooting |
| `docs/DEPLOYMENT.md` | Production deployment |
| `docs/specs/durable-runs-v2.2.md` | **Main architecture spec** (v2.2) |
| `docs/specs/jarvis-supervisor-unification-v2.1.md` | Architecture overview (v2.1) |
| `docs/archive/super-siri-architecture.md` | Historical architecture (v2.0) |
| `docs/work/` | Active PRDs (temporary, delete when done) |
