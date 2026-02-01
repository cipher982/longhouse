# Sauron - Centralized Ops Scheduler

> "One scheduler to rule them all"

Sauron is a standalone APScheduler service that runs scheduled jobs. It reuses the `zerg.jobs` infrastructure for job loading, queue management, and telemetry.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Sauron Service                        │
│  ┌─────────────────┐  ┌─────────────────┐               │
│  │  APScheduler    │  │  FastAPI        │               │
│  │  (cron triggers)│  │  (Jarvis API)   │               │
│  └────────┬────────┘  └────────┬────────┘               │
│           │                    │                         │
│  ┌────────▼────────────────────▼────────┐               │
│  │           zerg.jobs                   │               │
│  │  - GitSyncService (clone sauron-jobs) │               │
│  │  - job_registry (builtin + manifest)  │               │
│  │  - Worker (execute from queue)        │               │
│  └──────────────────────────────────────┘               │
└─────────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│                   sauron-jobs repo                       │
│  https://github.com/cipher982/sauron-jobs               │
│  - manifest.py (registers jobs)                          │
│  - jobs/*.py (job implementations)                       │
└─────────────────────────────────────────────────────────┘
```

## Deployment

Sauron is deployed to **clifford VPS** via Coolify. It runs as a standalone Docker container.

### Build Context

The Dockerfile expects to be built from the monorepo root:

```bash
docker build -f apps/sauron/Dockerfile -t sauron:latest ../..
```

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `JOB_QUEUE_DB_URL` | Yes | SQLite URL for durable queue (e.g. `sqlite:////data/sauron-queue.db`) |
| `DATABASE_URL` | No | Life Hub PostgreSQL URL (optional; used by some jobs) |
| `JOBS_GIT_REPO_URL` | Yes | Git URL for sauron-jobs repo |
| `JOBS_GIT_TOKEN` | Yes | GitHub PAT for private repo |
| `SSH_PRIVATE_KEY_B64` | No | Base64-encoded SSH key for SSH jobs |
| `GITHUB_TOKEN` | No | GitHub PAT for worklog job (gh API) |
| `AWS_SES_*` | Yes | AWS SES credentials for email |
| `*_EMAIL` | No | Email addresses for notifications |

See `docker-compose.yml` for full list.

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/status` | GET | Scheduler status + git sync info |
| `/jobs` | GET | List all registered jobs |
| `/jobs/{id}` | GET | Get job details |
| `/jobs/{id}/trigger` | POST | Manually trigger a job |
| `/jobs/{id}/enable` | POST | Enable a job |
| `/jobs/{id}/disable` | POST | Disable a job |
| `/sync` | POST | Force git sync |

## CLI

```bash
# List jobs
python -m sauron.cli list

# Run job manually (via queue)
python -m sauron.cli run llm-bench-health

# Run job directly (bypass queue)
python -m sauron.cli run llm-bench-health --direct

# Show next scheduled runs
python -m sauron.cli next

# Force git sync
python -m sauron.cli sync
```

## Local Development

```bash
cd apps/sauron

# Install dependencies
uv sync

# Set environment variables
export JOB_QUEUE_DB_URL=sqlite:///./sauron-queue.db
export DATABASE_URL=postgresql://...  # optional (jobs that query life-hub)
export JOBS_GIT_REPO_URL=https://github.com/cipher982/sauron-jobs.git
export JOBS_GIT_TOKEN=ghp_...

# Run scheduler
uv run python -m sauron.main

# Run CLI
uv run python -m sauron.cli list
```

## Job Flow

1. **Startup**: Sauron clones `sauron-jobs` repo via `GitSyncService`
2. **Registration**: `register_all_jobs()` loads builtin jobs + manifest.py
3. **Scheduling**: APScheduler sets up cron triggers for each job
4. **Queueing**: When cron fires, job is enqueued to SQLite `job_queue` table
5. **Execution**: Worker claims job, executes, records to `ops.runs`
6. **Git Sync**: Background loop pulls repo updates every 5 minutes
7. **Definitions**: Publish job definitions directly to `ops.jobs` for the ops dashboard (avoids Life Hub API)

## Jarvis Integration

Jarvis can control Sauron via the API:

```python
# Check status
response = await httpx.get("http://sauron:8080/status")

# Trigger job
response = await httpx.post("http://sauron:8080/jobs/llm-bench-health/trigger")

# Force sync after updating sauron-jobs
response = await httpx.post("http://sauron:8080/sync")
```

## Migration from Old Sauron

This is Sauron v2.0, rewritten to use `zerg.jobs` infrastructure. The old standalone Sauron repo (`cipher982/sauron`) is deprecated.

Key changes:
- Jobs now live in `cipher982/sauron-jobs` repo
- Uses `zerg.jobs` for loading, queue, and telemetry
- Deployed from Zerg monorepo instead of standalone
- API added for Jarvis control
