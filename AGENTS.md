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

- **JavaScript**: Bun only. Single `bun.lock` at root. Run `bun install` from repo root.
- **Python**: uv only. Run `cd apps/zerg/backend && uv sync`.

Do not use npm, yarn, pip, or poetry.

## Key Commands

```bash
# Start everything (Docker + nginx + services)
make dev           # Full platform with nginx proxy (profile: full)

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

Single unified compose file: `docker/docker-compose.yml`

| Profile | Services | Use Case |
|---------|----------|----------|
| `full` | postgres, zerg-*, jarvis-*, reverse-proxy | Full platform via nginx at 30080 |
| `zerg` | postgres, zerg-backend-exposed, zerg-frontend-exposed | Zerg only, direct ports |
| `prod` | postgres, zerg-backend-prod, zerg-frontend-prod | Production hardened |

```bash
# Direct compose usage (prefer make targets)
docker compose -f docker/docker-compose.yml --profile full up
docker compose -f docker/docker-compose.yml --profile zerg up
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

### Jarvis (TypeScript)
- Web UI: `apps/jarvis/apps/web/`
- Shared code: `apps/jarvis/packages/core/`
- BFF endpoints: Served by zerg-backend at `/api/jarvis/*`

## Generated Code — Do Not Edit

- `apps/zerg/frontend-web/src/generated/` — OpenAPI types
- `apps/zerg/backend/zerg/ws_protocol/generated/` — WebSocket protocol
- `packages/contracts/` — Shared contract definitions

Run `make regen-ws` if WebSocket schema changes. Run `make generate-sdk` if API changes.

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
    "url": "http://REDACTED_IP:5055",
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
