## Zerg Agent Platform

Create and orchestrate AI agents with real‑time streaming. This monorepo contains the Zerg backend and UIs plus the Jarvis PWA.

- **Backend**: FastAPI (Python 3.12), WebSockets, LangGraph agents
- **UI (default in Docker)**: React + Vite (`apps/zerg/frontend-web`)
- **UI (dev/debug)**: Rust/WASM (`apps/zerg/frontend`)
- **Monorepo**: also includes `apps/jarvis` (PWA) and shared packages

### Quick start
1) Copy env and set secrets
```bash
cp .env.example .env
# minimally set OPENAI_API_KEY; JWT_SECRET for auth-enabled runs
```

2) Run the platform
- Docker (recommended):
```bash
make zerg-up           # starts Postgres + backend + React UI
# Backend:  http://localhost:${ZERG_BACKEND_PORT:-47300}
# Frontend: http://localhost:${ZERG_FRONTEND_PORT:-47200}
```
- Local dev (no containers):
```bash
make zerg-dev          # uvicorn backend + Rust/WASM UI (ports from .env)
```
- Full monorepo (Jarvis + Zerg):
```bash
make swarm-dev
```

3) Stop / logs / reset (Docker)
```bash
make zerg-logs
make zerg-down
make zerg-reset        # destroys dev data
```

### Tests
```bash
make test           # all tests (Jarvis + Zerg)
make test-zerg      # Zerg backend + frontend + e2e
make test-jarvis    # Jarvis only
```

### Configuration
- Root `.env` is loaded by the Makefile and `uv`.
- **Required**: `OPENAI_API_KEY`
- **Common secrets**: `JWT_SECRET`, `TRIGGER_SIGNING_SECRET`
- **Feature flags**:
  - `AUTH_DISABLED` (dev default `1`): bypass Google/JWT auth in local runs
  - `LLM_TOKEN_STREAM` (`true|1`): emit per‑token chunks over WS
- **Ports (defaults)**: `ZERG_BACKEND_PORT=47300`, `ZERG_FRONTEND_PORT=47200`

### Monorepo layout (essentials)
- `apps/zerg/backend` – FastAPI service, schedulers, WebSocket hub
- `apps/zerg/frontend-web` – React UI served in Docker images
- `apps/zerg/frontend` – Rust/WASM UI for power users and debugging
- `apps/zerg/e2e` – Playwright tests
- `apps/jarvis` – PWA app
- `packages/contracts` – OpenAPI/AsyncAPI clients and types
- `packages/tool-manifest` – tool manifest generator

### Key capabilities
- Authenticated WebSockets (`/api/ws?token=<jwt>`; 4401 on invalid JWT)
- Token‑level streaming when `LLM_TOKEN_STREAM` is enabled
- LangGraph‑based functional agents with run history and scheduling

### Dev tips
- Prefer Make targets and `uv run` over calling `python`/`pytest` directly
- Generate SDK and tool manifest:
```bash
make generate-sdk
# or just the tool manifest
make generate-tools
```

### Docs
- Deployment: `docs/DEPLOYMENT.md`, `DEPLOY.md`
- React migration notes: `docs/react_dashboard_migration.md`
- Contracts and protocols: `asyncapi/*.yml`, `contracts/*.json`

### License
ISC
