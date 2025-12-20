# AGENTS.md

## What This Is

**Zerg** is an AI agent orchestration platform. **Jarvis** is a voice/text UI that connects to Zerg. Together they form a "swarm" system where Jarvis handles user interaction and Zerg manages multi-agent workflows.

## Architecture Philosophy (v2.0)

**"Trust the AI"** — Modern LLMs are smart enough to figure things out. Give them context and autonomy, not rigid decision trees.

Key principles:
- **No keyword routing** — Jarvis decides when to delegate, not keyword matching
- **No specialized workers** — Workers are general-purpose agents with SSH access
- **No tool allowlists** — Security via capability boundaries (which hosts can SSH to), not tool restrictions
- **Event-driven** — Workers notify when done, no polling loops

Full spec: `docs/specs/super-siri-architecture.md`

## Architecture (Unified)

```
User → http://localhost:30080 (nginx)
  /            → Zerg dashboard (React)
  /dashboard   → Zerg dashboard (alias)
  /chat        → Jarvis web (PWA)
  /api/*       → FastAPI backend (includes Jarvis BFF at /api/jarvis/*)
  /ws/*        → SSE/WS

Internal service ports (dev):
  Zerg backend       47300
  Zerg frontend      47200
  Jarvis web         8080
```

## Package Managers

- **JavaScript**: Bun only.
  - Monorepo lockfile: `bun.lock` (repo root) — run `bun install` from repo root.
  - Jarvis-only lockfile: `apps/jarvis/bun.lock` — used by the Jarvis Docker workspace; run `cd apps/jarvis && bun install` when working inside the Jarvis workspace directly.
- **Python**: uv only. Run `cd apps/zerg/backend && uv sync`.

Do not use npm, yarn, pip, or poetry.

## Key Commands

```bash
# Start everything (Docker + nginx + services)
make dev           # Full platform with nginx proxy (profile: full)
                   # ⚠️ Interactive - tails logs until Ctrl+C
                   # ⚠️ Auto-rebuilds images (--build flag included)
make dev-bg        # Same but background (for CI/automation)

# Start individual services
make zerg          # Just Zerg with direct ports (profile: zerg)
make jarvis        # Just Jarvis (native Node)

# Environment validation
make env-check     # Validate required env vars before starting

# Stop everything
make stop

# Run tests
make test          # All tests (Unit + E2E)
make test-zerg     # Backend + frontend + e2e
make test-jarvis   # Jarvis unit tests
make test-jarvis-e2e # Jarvis E2E tests (isolated environment)

# Targeted E2E testing
make test-e2e-up     # Start isolated E2E environment
make test-e2e-single TEST=name # Run single test (e.g. TEST=supervisor-progress)
make test-e2e-logs   # View E2E service logs
make test-e2e-down   # Cleanup E2E environment

# Regenerate generated code
make generate-sdk  # OpenAPI types
make regen-ws      # WebSocket contract code

# Seeding (local dev data)
make seed-agents       # Seed Jarvis agents
make seed-credentials  # Seed personal tool credentials (Traccar, WHOOP, Obsidian)

# Validation (CI checks these)
make validate      # All validation
```

## Docker Compose Profiles

Dev compose file (profiles live here): `docker/docker-compose.dev.yml`

Other compose files:
- Jarvis E2E: `apps/jarvis/docker-compose.test.yml`
- Standalone production compose: `docker/docker-compose.prod.yml`

| Profile | Services | Use Case |
|---------|----------|----------|
| `full` | postgres, zerg-*, jarvis-*, reverse-proxy | Full platform via nginx at 30080 |
| `zerg` | postgres, zerg-backend-exposed, zerg-frontend-exposed | Zerg only, direct ports |
| `prod` | postgres, zerg-backend-prod, zerg-frontend-prod | Production hardened |

```bash
# Direct compose usage (prefer make targets)
docker compose -f docker/docker-compose.dev.yml --profile full up
docker compose -f docker/docker-compose.dev.yml --profile zerg up
```

## Testing Infrastructure

Zerg/Jarvis uses a **completely isolated Docker environment** for E2E tests to prevent data corruption in your development database.

### E2E Test Isolation
- **Project Name**: E2E tests run under the `zerg-e2e` Docker project name.
- **Network**: All services communicate via an internal `test` network.
- **Database**: Uses a dedicated `postgres:15-alpine` container with its own volume.
- **Entry Point**: A dedicated `nginx` reverse proxy handles routing for tests.

### Database Initialization
- The backend automatically detects missing tables and initializes the schema on startup via `scripts/init_db.py`.
- No manual migration steps are required for fresh test environments.

### Running Tests
- Use `make test-jarvis-e2e` for a full clean run.
- For active development, use `make test-e2e-up` to keep the environment running, then `make test-e2e-single TEST=<filename>` to iterate quickly on a specific test file.
- E2E artifacts (screenshots + `error-context.md`) are written to `apps/jarvis/test-results/` (mounted from the Playwright container).

### Debugging E2E Failures (Quick Checklist)
1. Re-run just the failing spec: `make test-e2e-single TEST=<spec-or-grep>`
2. Open the failure snapshot: `apps/jarvis/test-results/<test-folder>/error-context.md`
3. Open the screenshot/video (same folder) to confirm what the DOM looked like
4. If the UI looks fine but backend events are missing: `make test-e2e-logs` (then focus on `zerg-backend` + `reverse-proxy`)
5. If you need interactive UI debugging: `make test-jarvis-e2e-ui` (host Playwright UI, not Docker)

### Live Integration Testing (Backend Tools)
To verify core supervisor tools (KV memory, Tasks, Web Search) against a running backend:

```bash
# 1. Start backend
make dev

# 2. Run live tests (from apps/zerg/backend)
cd apps/zerg/backend
uv run python -m pytest tests/live --live-token <YOUR_JWT>
```

These tests dispatch real tasks to the Supervisor and verify the SSE stream results.

## Conventions

### Backend (Python)
- Location: `apps/zerg/backend/zerg/`
- Tests: `apps/zerg/backend/tests/` — run with `./run_backend_tests.sh`
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
- **Tokens**: Use `--color-*` and `--space-*` variables from `src/styles/generated/tokens.css`.
- **No Global Classes**: Avoid generic class names like `.back-button` or `.empty-state` at the root level.
- **Tools**: `bun run build:tokens` generates tokens; `bun run build` verifies nesting syntax.

### Jarvis (TypeScript)
- Web UI: `apps/jarvis/apps/web/`
- Shared code: `apps/jarvis/packages/core/`
- BFF endpoints: Served by zerg-backend at `/api/jarvis/*`

## Generated Code — Do Not Edit

- `apps/zerg/frontend-web/src/generated/` — OpenAPI types
- `apps/zerg/backend/zerg/ws_protocol/generated/` — WebSocket protocol
- `apps/zerg/backend/zerg/tools/generated/tool_definitions.py` — Tool registry types (from `schemas/tools.yml`)
- `packages/contracts/` — Shared contract definitions

Run `make regen-ws` if WebSocket schema changes. Run `make generate-sdk` if API changes.

## Pre-commit (AI Agent Safety)

Pre-commit hooks run automatically on every commit. Agents should be aware of what will block commits.

### What Runs (~5s total)

| Hook | Behavior | Blocks? |
|------|----------|---------|
| ruff lint | Auto-fixes Python issues | No (auto-fix) |
| ruff format | Auto-fixes formatting | No (auto-fix) |
| TypeScript types | `tsc --noEmit` | **Yes** |
| ESLint | Frontend lint | Warns only (100 max) |
| WS drift | Contract sync check | **Yes** |
| AsyncAPI | Schema validation | **Yes** |

### Common Failures & Fixes

| Error | Cause | Fix |
|-------|-------|-----|
| `bare except` (E722) | Using `except:` | Use `except Exception:` |
| Unused variable (F841) | Assigned but never used | Prefix with `_` or remove |
| Line too long (E501) | Ignored in this repo | N/A (disabled) |
| TypeScript type error | Actual type mismatch | Fix the types |
| WS drift | Schema out of sync | Run `make regen-ws` |

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
- `apps/zerg/frontend-web/src/generated/openapi-types.ts`

If you edit these, your changes will be overwritten by `make regen-ws` or `make generate-sdk`.

## Gotchas

1. **Docker context**: Jarvis Dockerfiles expect repo root context (paths like `COPY apps/jarvis/...`).

2. **Auth in dev vs prod**: unified compose sets `AUTH_DISABLED=1` backend and `VITE_AUTH_ENABLED=false` frontend for local. Re-enable auth in prod.

3. **CORS/SSE**: same-origin via nginx at 30080; keep run_id on worker tool events for Jarvis ticker.

4. **WebSocket code must stay in sync**: run `make regen-ws` after touching `schemas/ws-protocol.schema.json`.

5. **Bun workspace filter syntax**: use `bun run --filter @jarvis/web dev` not `--workspace`.

6. **Ports**: external entry 30080; service ports (8080/47200/47300) are internal. Avoid binding conflicts if running pieces standalone.

7. **Python virtual env**: backend uses `apps/zerg/backend/.venv/`; use `uv run` or activate venv.

8. **E2E Test Isolation**: E2E tests run in a separate Docker project (`zerg-e2e`). If tests fail due to "database connection" issues, ensure no other containers are conflicting on the internal network and try `make test-e2e-reset`.

9. **Auth in Tests**: When `AUTH_DISABLED=1` (default for tests), the `/api/auth/verify` endpoint always returns 204. This allows the frontend to proceed without a real login.

10. **E2E runs on an insecure origin**: Playwright hits `http://reverse-proxy` in Docker (service name), which is **not** a secure context — `crypto.randomUUID()` may be unavailable. Use `apps/jarvis/apps/web/lib/uuid.ts` instead of calling `crypto.randomUUID()` directly.

11. **Prefer Make targets for E2E**: `make` loads/export vars from `.env`. Running `docker compose -f apps/jarvis/docker-compose.test.yml ...` directly can fail if `OPENAI_API_KEY` isn't exported in your shell.

12. **Deterministic UI tests**: For progress-indicator/UI behavior tests, prefer emitting events via `window.__jarvis.eventBus` (DEV only) instead of relying on the LLM to spawn workers/tools.

13. **WebRTC tests in Docker**: When `SKIP_WEBRTC_TESTS=true`, WebRTC-based tests should be *describe-level* skipped so `beforeEach` doesn't fail in the Docker E2E environment.

14. **AGENTS vs CLAUDE**: `CLAUDE.md` is a symlink for tool compatibility. Treat `AGENTS.md` as canonical and only edit `AGENTS.md`.

15. **Generated tool types path**: `scripts/generate_tool_types.py` writes to `apps/zerg/backend/zerg/tools/generated/` relative to repo root (safe from any CWD). If you ever see a duplicate tree like `apps/zerg/backend/apps/...`, it's a stray artifact and can be deleted.

16. **CSS Collisions**: Global class names (e.g., `.back-button`, `.empty-state`) can leak across pages if not scoped. Always nest page-specific styles under a root container class (e.g., `.dashboard-container { ... }`).

17. **`make dev` is interactive**: It starts services, then tails logs forever (never exits). Run it in your terminal, not via automated tools expecting completion. To rebuild after Dockerfile changes, just `make stop && make dev` — the `--build` flag is already included.

18. **Never use raw `docker compose` for dev**: Always use Make targets (`make dev`, `make stop`, `make logs`). Raw `docker compose` commands use wrong project names, miss env vars from `.env`, and create containers on isolated networks that can't communicate with the rest of the stack. If you must, use the pattern from Makefile line 17: `docker compose --project-name zerg --env-file .env -f docker/docker-compose.dev.yml ...`

## Environment Setup

Copy `.env.example` to `.env` and fill in:
- `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` — Required for Docker
- `OPENAI_API_KEY` — Required for Jarvis and LLM features

Run `make env-check` to validate your environment before starting.

Dev auth defaults (`AUTH_DISABLED=1`, `VITE_AUTH_ENABLED=false`) are set in compose. For production, set `AUTH_DISABLED=0` and configure Google OAuth credentials.

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

**Deployment**: Coolify on the `zerg` server manages all containers via Docker Compose.

- **Server access**: `ssh zerg` (via Tailscale at 100.120.197.80)
- **Container inspection**: `docker ps`, `docker logs <container>`
- **Auto-deploy**: Push to `main` → Coolify detects → rebuilds containers
- **Health check**: `curl https://api.swarmlet.com/health`

See `docs/DEPLOYMENT.md` for full deployment guide.

### Vite Environment Variables

**CRITICAL:** `VITE_*` variables are BUILD-TIME, not runtime:
- Must be present during `bun run build` (baked into JS bundle)
- Changing env vars in Coolify requires REBUILD, not just restart
- Use Coolify API `/deploy` endpoint, not `/restart` endpoint
- Build takes ~2-3 minutes

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
| `docs/specs/super-siri-architecture.md` | **Main architecture spec** (v2.0) |
| `docs/work/` | Active PRDs (temporary, delete when done) |
