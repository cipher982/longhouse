# Database: Life Hub Integration

Zerg uses Life Hub's Postgres as its database. In production, Zerg is a schema-tenant in the shared `life_hub` database on clifford.

## Schema Ownership

```
┌─────────────────────────────────────────────────────────────┐
│  CLIFFORD:5433 - PostgreSQL + TimescaleDB                  │
│  Database: life_hub                                         │
│                                                             │
│  zerg.*         │  agents.*      │  health.*   │  infra.*  │
│  (THIS REPO)    │  (Life Hub)    │  (Life Hub) │           │
│                 │                │             │           │
│  agent_runs     │  sessions      │  metrics    │           │
│  worker_jobs    │  events        │  workouts   │           │
│  llm_audit_log  │                │  sleep      │           │
│  threads        │                │             │           │
│  workflows      │                │             │           │
│  users          │                │             │           │
│  ...37 tables   │                │             │           │
└─────────────────────────────────────────────────────────────┘
```

## Connection

**Prod connection string:**
```
postgresql://life_hub:***@clifford:5433/life_hub?options=-csearch_path=zerg,public
```

## Key Points

- **Zerg owns `zerg.*` schema only** — 37 tables
- **Life Hub owns other schemas** — `agents.*`, `health.*`, `infra.*`, `work.*`, `location.*`
- **Independent migrations** — This repo runs alembic for `zerg.*`, Life Hub for its schemas

## Don't Confuse

| Table | What |
|-------|------|
| `zerg.agent_runs` / `zerg.worker_jobs` | Swarmlet's supervisor/worker execution (this repo) |
| `agents.sessions` / `agents.events` | Claude Code/Codex/Gemini sessions via life-hub shipper |

## Admin Dashboards

- `/traces` — Debug supervisor runs, workers, LLM calls
- `/reliability` — System health, error analysis, stuck workers

## Cross-Schema Access

Life Hub MCP can query `zerg.*` tables (same database):
```sql
SELECT * FROM zerg.agent_runs WHERE created_at > NOW() - interval '1 day';
```

**See also:** `~/git/life-hub/AGENTS.md`
