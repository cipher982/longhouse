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
# Zerg only (Docker, direct ports)
make zerg
```

### Monitoring

```bash
# Tail all service logs
make logs

# Tail app logs (excludes Postgres)
make logs-app

# Check what's running
docker ps
lsof -i :30080 # nginx entrypoint
lsof -i :47300 # backend (direct mode / tests)
```

### Debugging

```bash
# View help
make help

# Quick diagnostics (ports, containers, env)
make doctor

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
2. Check Vite config has proxy: `cat apps/zerg/frontend-web/vite.config.ts`
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
# Unit tests (backend + frontend; no Playwright)
make test

# Playwright E2E (unified SPA)
make test-e2e

# Full suite (unit + E2E)
make test-all

# Just chat (/chat) E2E smoke tests
make test-chat-e2e
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

**Frontend changes** (unified SPA: dashboard + Jarvis chat):

- Edit files in `apps/zerg/frontend-web/src/` (Jarvis lives in `apps/zerg/frontend-web/src/jarvis/`)
- Browser auto-refreshes (Vite HMR)

**Backend changes** (FastAPI, includes `/api/jarvis/*`):

- Edit files in `apps/zerg/backend/`
- Hot-reloads in dev mode (RELOAD=true)

---

## Architecture

```
User → http://localhost:30080 (nginx)
  /            → Unified React SPA (dashboard + /chat)
  /dashboard   → Zerg dashboard (SPA route)
  /chat        → Jarvis chat UI (SPA route)
  /api/*       → FastAPI backend (includes /api/jarvis/*)
  /ws/*        → SSE/WS

Internal service ports (dev):
  Zerg backend       47300
  Zerg frontend      47200
```

---

## Project Structure

```
zerg/
├── apps/
│   ├── jarvis/              # Legacy Jarvis artifacts (no standalone web app)
│   └── zerg/                # Agent platform
│       ├── backend/         # FastAPI (includes /api/jarvis/*)
│       ├── frontend-web/    # Unified React SPA (dashboard + /chat)
│       └── e2e/             # Playwright E2E (unified SPA)
├── docker/
│   ├── docker-compose.dev.yml  # Dev profiles
│   └── nginx/                  # Reverse proxy configs
├── scripts/
│   └── dev-docker.sh          # Unified dev script (legacy)
├── Makefile                   # Main commands
└── docs/
    └── DEVELOPMENT.md         # This file
```

---

## Environment Variables

Key variables in `.env`:

```bash
# Proxy & Services
JARPXY_PORT=30080           # Nginx entry point (reverse-proxy)
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
- [ ] Is port 47300 free? (`lsof -i :47300`) - Zerg backend
- [ ] Do you have `.env` configured? (`cat .env`)
- [ ] Are node_modules installed? (`bun install` from repo root)
- [ ] Is Vite proxy configured? (`cat apps/zerg/frontend-web/vite.config.ts`)

---

## Getting Help

1. **Check logs**: Browser console + terminal output
2. **Run diagnostics**: `make doctor`
3. **Check Docker**: `docker ps` and `make logs-app`
4. **Nuclear restart**: `make dev-reset-db && make dev`

---

**Last Updated**: 2025-12-20
