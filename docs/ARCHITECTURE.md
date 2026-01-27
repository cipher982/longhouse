# Zerg Architecture

## Overview

Zerg is an AI agent orchestration platform (product name: Swarmlet). This monorepo contains three deployable services that share code and a database.

## Services

| Service | Location | Deploys To | Port | URL |
|---------|----------|------------|------|-----|
| **zerg-api** | `apps/zerg/backend/` | Zerg server | 8000 | api.swarmlet.com |
| **zerg-web** | `apps/zerg/frontend-web/` | Zerg server | 80 (prod) / 5173 (dev) | swarmlet.com |
| **sauron** | `apps/sauron/` | Clifford | 8080 | (internal only) |

### zerg-api (Backend)
- FastAPI service - supervisor/worker AI agent engine
- Handles chat, tools, workers, runners
- Coolify app on zerg server

### zerg-web (Frontend)
- React SPA - Jarvis chat + dashboard
- Same-origin with API via nginx
- Coolify app on zerg server

### sauron (Scheduler)
- APScheduler + FastAPI - background job scheduler
- Loads jobs from `cipher982/sauron-jobs` repo via GitSyncService
- Coolify app on clifford (separate deployment)
- **No public URL** - internal Docker network only

## Deployment

**All three services auto-deploy on push to `main`** via Coolify webhooks.

```
git push origin main
    ↓
Coolify (clifford) receives webhook
    ↓
Builds & deploys each service to its target server
```

### Verification
```bash
# Check deployment logs
./scripts/get-coolify-logs.sh 1

# Full prod verification
make verify-prod
```

### Manual Checks
```bash
# zerg-api/web on zerg server
ssh zerg "docker ps | grep zerg"

# sauron on clifford
ssh clifford "docker ps | grep sauron"
ssh clifford "docker logs sauron --tail 50"
```

## Database

**Single shared database** on clifford (TimescaleDB/PostgreSQL 16):

```
clifford:5433 / life_hub
├── zerg.*     (37 tables) - agent runs, threads, users, workers
├── ops.*      (job queue) - shared between zerg-api and sauron
└── life_hub.* (metrics)   - life-hub platform
```

Both zerg-api and sauron connect to the same database and share the `ops.job_queue` table for durable job execution.

## Code Sharing

Sauron imports from the zerg package:
```python
from zerg.jobs import GitSyncService, register_all_jobs
from zerg.jobs.worker import run_queue_worker
```

This is why they're in the same monorepo - sauron reuses `zerg.jobs` infrastructure.

## Key Files

| Purpose | Location |
|---------|----------|
| Backend code | `apps/zerg/backend/zerg/` |
| Frontend code | `apps/zerg/frontend-web/src/` |
| Sauron code | `apps/sauron/sauron/` |
| Dev compose | `docker/docker-compose.dev.yml` |
| Sauron compose | `apps/sauron/docker-compose.yml` |
| Migrations | `apps/zerg/backend/alembic/` |

## Local Development

```bash
make dev          # Start all services (interactive)
make dev-bg       # Background mode
make stop         # Stop everything
make test         # Unit tests
make test-e2e     # E2E tests
```

Dev stack runs at `localhost:30080` with nginx routing to backend/frontend.

## Servers

| Server | Role | Services |
|--------|------|----------|
| **clifford** | Coolify master, database, sauron | PostgreSQL, Sauron scheduler |
| **zerg** | Application server | zerg-api, zerg-web |
| **cube** | CI runners, GPU workloads | GitHub Actions runners |

## Quick Reference

```bash
# Deploy (just push)
git push origin main

# Check sauron logs
ssh clifford "docker logs sauron -f"

# Check zerg logs
ssh zerg "docker logs zerg-api -f"

# Query job definitions
ssh clifford "docker exec postgres-XXX psql -U life_hub -d life_hub -c 'SELECT job_key, enabled FROM ops.jobs;'"
```
