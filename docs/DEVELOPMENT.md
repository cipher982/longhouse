# Development Guide

## TL;DR - Start & Stop

```bash
# Start everything (RECOMMENDED)
make dev

# Ctrl+C to stop everything properly
```

That's it. One command to start, Ctrl+C to stop. Everything shuts down cleanly.

---

## What `make dev` Does

1. **Starts Docker Compose (profile: `full`)** behind a single nginx entrypoint
2. **Routes everything same-origin** at `http://localhost:30080` (dashboard, chat, API)
3. **Traps Ctrl+C** to shut down everything cleanly
4. **Shows status** of all services

### Services & Ports

With `make dev`, the intended entry point is nginx:

| Service (entrypoint)    | Port  | URL                              |
| ----------------------- | ----- | -------------------------------- |
| **Unified App (nginx)** | 30080 | http://localhost:30080           |
| Chat                    | 30080 | http://localhost:30080/chat      |
| Dashboard               | 30080 | http://localhost:30080/dashboard |

Internal service ports exist, but are not exposed to the host in `make dev`.

---

## Alternative Commands

### Start Individual Services

```bash
# Just Zerg (Docker, direct ports)
make zerg

# Just Jarvis (native mode, no Docker)
make jarvis
make jarvis-stop
```

### Monitoring

```bash
# View Zerg logs
make zerg-logs

# Check service status
cd apps/jarvis && make status

# Check what's running
docker ps
lsof -i :8080  # Jarvis UI
lsof -i :47300 # Zerg backend (includes Jarvis BFF)
```

### Debugging

```bash
# View help
make help

# Check Jarvis status
cd apps/jarvis && make status

# Reset Zerg database (DESTROYS DATA)
make dev-reset-db
```

---

## Common Issues

### "Port already in use"

Something didn't shut down properly:

```bash
# Nuclear option - kill everything
make stop

# Then restart
make dev
```

### "Cannot connect to backend"

The zerg-backend isn't running or the proxy isn't working:

1. Check if it started: `lsof -i :47300` or `docker ps | grep backend`
2. Check Vite config has proxy: `cat apps/jarvis/apps/web/vite.config.ts`
3. Restart: Ctrl+C, then `make dev`

### "SyntaxError: Unexpected token '<'"

You're getting HTML instead of JSON from an API endpoint. Usually means:

1. Backend server isn't running
2. Vite proxy misconfigured
3. Wrong API endpoint

Check the browser console for the full error with URL and response body.

---

## Testing

```bash
# Run all tests
make test

# Just Jarvis tests
make test-jarvis

# Just Zerg tests
make test-zerg

# Jarvis E2E tests (with Playwright UI)
cd apps/jarvis/e2e
bunx playwright test --ui
```

---

## Development Workflow

### Daily Workflow

```bash
# Morning - start everything
make dev

# Work on code...
# (Vite hot-reloads frontend automatically)
# (Backend needs manual restart if you change server code)

# Evening - stop everything
Ctrl+C
```

### Making Changes

**Frontend changes** (Jarvis UI):

- Edit files in `apps/jarvis/apps/web/`
- Browser auto-refreshes (Vite HMR)

**Backend changes** (Zerg backend includes Jarvis BFF):

- Edit files in `apps/zerg/backend/`
- Hot-reloads in dev mode (RELOAD=true)

**Frontend changes** (Zerg or Jarvis):

- Edit files in `apps/zerg/backend/` or `apps/zerg/frontend-web/`
- Typically hot-reloads; if you need a clean rebuild: `make dev-clean && make dev`

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│         Nginx Reverse Proxy (30080)             │
│                                                 │
│  /chat/*      → Jarvis UI (8080)                │
│  /dashboard/* → Zerg Dashboard (5173)           │
│  /api/*       → Zerg Backend (8000)             │
└─────────────────────────────────────────────────┘
                         │
          ┌──────────────┼──────────────┐
          │              │              │
          ▼              ▼              ▼
┌─────────────┐  ┌─────────────┐  ┌─────────────┐
│ Jarvis UI   │  │   Zerg      │  │   Zerg      │
│ (React PWA) │  │ Dashboard   │  │  Backend    │
│             │  │ (React)     │  │  (FastAPI)  │
│ - Voice UI  │  │             │  │             │
│ - Chat UI   │  │ - Agents    │  │ - Jarvis    │
│             │  │ - Runs      │  │   BFF       │
└─────────────┘  └─────────────┘  │ - OpenAI    │
                                  │   Realtime  │
                                  │ - Agents    │
                                  │ - Workers   │
                                  └──────┬──────┘
                                         │
                                         ▼
                                  ┌─────────────┐
                                  │  Postgres   │
                                  └─────────────┘
```

---

## Project Structure

```
zerg/
├── apps/
│   ├── jarvis/              # Voice UI
│   │   ├── apps/
│   │   │   └── web/         # Vite frontend (8080)
│   │   ├── packages/
│   │   │   ├── core/        # Shared models/config
│   │   │   └── data/        # IndexedDB persistence
│   │   └── Makefile
│   └── zerg/                # Agent platform
│       ├── backend/         # FastAPI (8000)
│       │   └── routers/
│       │       └── jarvis.py  # Jarvis BFF endpoints
│       ├── frontend-web/    # React (5173)
│       └── e2e/            # Playwright tests
├── docker/
│   ├── docker-compose.dev.yml  # Dev profiles
│   └── nginx/                  # Reverse proxy configs
├── scripts/
│   └── dev-docker.sh          # Unified dev script
├── Makefile                   # Main commands
└── docs/
    └── DEVELOPMENT.md         # This file
```

---

## Environment Variables

Key variables in `.env`:

```bash
# Proxy & Services
JARPXY_PORT=30080           # Nginx entry point
JARVIS_WEB_PORT=8080        # Jarvis UI (internal)
BACKEND_PORT=47300          # Zerg backend (internal)
FRONTEND_PORT=47200         # Zerg frontend (internal)

# OpenAI & Auth
OPENAI_API_KEY=sk-...
JWT_SECRET=...
FERNET_SECRET=...

# Database
POSTGRES_USER=...
POSTGRES_PASSWORD=...
POSTGRES_DB=...
```

Copy from `.env.example` if missing.

---

## Troubleshooting Checklist

- [ ] Is Docker running? (`docker ps`)
- [ ] Is port 30080 free? (`lsof -i :30080`) - nginx proxy
- [ ] Is port 8080 free? (`lsof -i :8080`) - Jarvis UI
- [ ] Is port 47300 free? (`lsof -i :47300`) - Zerg backend
- [ ] Do you have `.env` configured? (`cat .env`)
- [ ] Are node_modules installed? (`bun install` from repo root)
- [ ] Is Vite proxy configured? (`cat apps/jarvis/apps/web/vite.config.ts`)

---

## Getting Help

1. **Check logs**: Browser console + terminal output
2. **Run diagnostics**: `cd apps/jarvis && make status`
3. **Check Docker**: `docker ps` and `make zerg-logs`
4. **Nuclear restart**: `make dev-reset-db && make dev`

---

**Last Updated**: 2025-11-13
