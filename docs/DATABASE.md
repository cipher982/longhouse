# Database

Zerg uses PostgreSQL for persistent storage. In production, Zerg runs as a schema-tenant on the shared Postgres instance on clifford.

## Schema Ownership

```
┌─────────────────────────────────────────────────────────────┐
│  CLIFFORD:5433 - PostgreSQL + TimescaleDB                  │
│  Database: life_hub                                         │
│                                                             │
│  zerg.*         │  ops.*                                    │
│  (THIS REPO)    │  (Job queue)                              │
│                 │                                           │
│  agent_runs     │  job_queue                                │
│  worker_jobs    │                                           │
│  llm_audit_log  │                                           │
│  threads        │                                           │
│  workflows      │                                           │
│  users          │                                           │
│  runners        │                                           │
│  connectors     │                                           │
│  ...37 tables   │                                           │
└─────────────────────────────────────────────────────────────┘
```

## Connection

**Prod connection string:**
```
postgresql://life_hub:***@clifford:5433/life_hub?options=-csearch_path=zerg,public
```

**Dev (Docker Compose):**
```
postgresql://zerg:zerg@postgres:5432/zerg
```

## Key Points

- **Zerg owns `zerg.*` schema** — ~37 tables for agent runs, workers, users, connectors, etc.
- **Ops schema for job queue** — `ops.job_queue` for durable background jobs
- **Independent migrations** — This repo runs alembic for `zerg.*` schema only

## Admin Dashboards

- `/traces` — Debug supervisor runs, workers, LLM calls
- `/reliability` — System health, error analysis, stuck workers

## Local Development

Use `make dev` which starts a local Postgres container. Schema is auto-created via alembic migrations in `start-dev.sh`.

## Production

DATABASE_URL is set via Coolify environment variables. Migrations run automatically on container startup.
