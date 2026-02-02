# Sauron

Minimal APScheduler service for running scheduled jobs via `zerg.jobs`.
Deployed on clifford via Coolify.

## Local run

```bash
cd apps/sauron
uv sync
export JOB_QUEUE_DB_URL=sqlite:///./sauron-queue.db
export JOBS_GIT_REPO_URL=https://github.com/cipher982/sauron-jobs.git
export JOBS_GIT_TOKEN=ghp_...
uv run python -m sauron.main
```

## API

- `GET /health`
- `GET /status`
- `GET /jobs`
- `POST /jobs/{id}/trigger`
- `POST /sync`

## Build (from repo root)

```bash
docker build -f apps/sauron/Dockerfile -t sauron:latest ../..
```
