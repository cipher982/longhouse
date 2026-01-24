# External Jobs System

Run your own scheduled jobs on Zerg's infrastructure without modifying the core codebase.

## Overview

External jobs let you:
- **Keep code private**: Store proprietary job logic in your own git repository
- **Hot-reload changes**: Push to git and jobs update automatically (no redeploy needed)
- **Track versions**: Every job run records which git SHA was executing
- **Maintain independently**: Your jobs, your repo, your release schedule

## Quick Start

### 1. Create Your Jobs Repository

```
my-jobs/
├── manifest.py          # Required: registers your jobs
├── jobs/
│   ├── __init__.py
│   └── daily_report.py  # Your job implementations
└── requirements.txt     # Optional: extra dependencies
```

### 2. Write Your First Job

```python
# jobs/daily_report.py
import logging

logger = logging.getLogger(__name__)

async def run() -> dict:
    """Generate daily report. Must be async, must return dict."""
    logger.info("Generating daily report...")

    # Your logic here
    report_data = await generate_report()

    return {
        "status": "success",
        "rows_processed": len(report_data),
    }
```

### 3. Register in manifest.py

```python
# manifest.py
from zerg.jobs import job_registry, JobConfig
from jobs.daily_report import run as daily_report

job_registry.register(JobConfig(
    id="daily-report",
    cron="0 8 * * *",           # 8 AM UTC daily
    func=daily_report,
    timeout_seconds=300,        # 5 minute timeout
    max_attempts=3,             # Retry up to 3 times
    tags=["reports"],
    project="my-project",
    description="Daily performance report",
))
```

### 4. Configure Zerg

Set environment variables:

```bash
JOBS_GIT_REPO_URL=https://github.com/your-org/my-jobs.git
JOBS_GIT_TOKEN=ghp_xxxxxxxxxxxx    # GitHub PAT for private repos
JOBS_GIT_BRANCH=main               # Optional, defaults to main
JOB_QUEUE_ENABLED=1                # Required for external jobs
```

## JobConfig Reference

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | str | required | Unique job identifier |
| `cron` | str | required | Cron expression (UTC) |
| `func` | async callable | required | `async def run() -> dict` |
| `enabled` | bool | `True` | Whether job runs on schedule |
| `timeout_seconds` | int | `300` | Max execution time |
| `max_attempts` | int | `3` | Retry count on failure |
| `tags` | list[str] | `[]` | For filtering/organization |
| `project` | str | `None` | Project association |
| `description` | str | `""` | Human-readable description |

## Cron Expression Examples

```python
"0 * * * *"      # Every hour
"*/15 * * * *"   # Every 15 minutes
"0 8 * * *"      # Daily at 8 AM UTC
"0 0 * * 1"      # Mondays at midnight UTC
"0 6 1 * *"      # First of month at 6 AM UTC
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `JOBS_GIT_REPO_URL` | Yes* | Git repository URL |
| `JOBS_GIT_TOKEN` | No | GitHub PAT (for private repos) |
| `JOBS_GIT_BRANCH` | No | Branch to use (default: `main`) |
| `JOBS_DIR` | No | Local clone path (default: `/opt/sauron-jobs`) |
| `JOBS_REFRESH_INTERVAL_SECONDS` | No | Sync interval (default: `3600`, 0=disabled) |
| `JOB_QUEUE_ENABLED` | Yes | Must be `1` for external jobs |

*Only required if using external jobs

## How It Works

```
┌─────────────────────────────────────────────────────────┐
│                    YOUR GIT REPO                        │
│  manifest.py + jobs/*.py                                │
└─────────────────────────────────────────────────────────┘
                          │
                          │ git clone/pull
                          ▼
┌─────────────────────────────────────────────────────────┐
│                      ZERG                               │
│                                                         │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐ │
│  │ Git Sync    │───▶│ Manifest    │───▶│ Job         │ │
│  │ Service     │    │ Loader      │    │ Registry    │ │
│  └─────────────┘    └─────────────┘    └─────────────┘ │
│                                               │         │
│                                               ▼         │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐ │
│  │ ops.runs    │◀───│ Queue       │◀───│ APScheduler │ │
│  │ (tracking)  │    │ Worker      │    │ (cron)      │ │
│  └─────────────┘    └─────────────┘    └─────────────┘ │
└─────────────────────────────────────────────────────────┘
```

1. **Git Sync** clones your repo on startup, pulls periodically
2. **Manifest Loader** executes `manifest.py` to register jobs
3. **APScheduler** triggers jobs based on cron expressions
4. **Queue Worker** executes with retries, timeout, heartbeat
5. **ops.runs** records every execution with git SHA

## Job Execution Features

### Durable Queue
Jobs are stored in a database queue. If Zerg restarts mid-execution:
- Incomplete jobs are automatically retried
- No duplicate runs (dedupe by job_id + scheduled_time)
- Missed runs are backfilled on startup

### Automatic Retries
Failed jobs retry with exponential backoff:
- Attempt 1: immediate
- Attempt 2: +1 minute
- Attempt 3: +2 minutes
- ...capped at 1 hour between retries

### Timeout Enforcement
Jobs exceeding `timeout_seconds` are terminated and marked failed.

### Heartbeat
Long-running jobs send periodic heartbeats to prevent false timeouts.

## Metadata Tracking

Every job run records:
```json
{
  "job_id": "daily-report",
  "status": "success",
  "started_at": "2026-01-24T08:00:00Z",
  "ended_at": "2026-01-24T08:00:05Z",
  "duration_ms": 5000,
  "metadata": {
    "script_source": "manifest",
    "git_sha": "abc123def456...",
    "loaded_at": "2026-01-24T07:00:00Z"
  }
}
```

## Best Practices

### Job Design
```python
async def run() -> dict:
    """
    - Always return a dict with status info
    - Raise exceptions for failures (triggers retry)
    - Use logging, not print()
    - Keep jobs idempotent (safe to retry)
    """
    try:
        result = await do_work()
        return {"status": "success", "count": result}
    except Exception as e:
        logger.exception("Job failed: %s", e)
        raise  # Triggers retry
```

### Error Handling
```python
async def run() -> dict:
    # Partial failures - return status, don't raise
    results = []
    errors = []

    for item in items:
        try:
            results.append(await process(item))
        except Exception as e:
            errors.append(str(e))

    return {
        "status": "partial" if errors else "success",
        "processed": len(results),
        "errors": errors,
    }
```

### Secrets Management
```python
import os

async def run() -> dict:
    # Use environment variables, not hardcoded secrets
    api_key = os.environ["MY_API_KEY"]
    db_url = os.environ["DATABASE_URL"]
    ...
```

## Troubleshooting

### Job Not Running
1. Check `JOBS_GIT_REPO_URL` is set
2. Verify `JOB_QUEUE_ENABLED=1`
3. Check logs for manifest load errors
4. Verify job is `enabled=True`

### Git Clone Fails
1. For private repos, set `JOBS_GIT_TOKEN`
2. Verify token has repo read access
3. Check network connectivity to git host

### Job Times Out
1. Increase `timeout_seconds` in JobConfig
2. Add progress logging to identify slow sections
3. Consider breaking into smaller jobs

### Retries Exhausted
1. Check logs for root cause
2. Fix underlying issue
3. Manually trigger via API: `POST /api/jobs/{job_id}/run`

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/jobs/` | List all registered jobs |
| GET | `/api/jobs/{job_id}` | Get job details |
| POST | `/api/jobs/{job_id}/run` | Trigger job manually |
| POST | `/api/jobs/{job_id}/enable` | Enable job |
| POST | `/api/jobs/{job_id}/disable` | Disable job |
| GET | `/api/jobs/queue/state` | View queue status |

## Example: Complete Job

```python
# jobs/backup_database.py
"""Database backup job with error handling and notifications."""

import asyncio
import logging
import os
from datetime import datetime, UTC

import httpx

logger = logging.getLogger(__name__)

async def run() -> dict:
    """Backup database to S3 and notify on completion."""

    db_url = os.environ["DATABASE_URL"]
    s3_bucket = os.environ["BACKUP_S3_BUCKET"]
    slack_webhook = os.environ.get("SLACK_WEBHOOK")

    started = datetime.now(UTC)

    try:
        # Perform backup
        logger.info("Starting database backup...")
        backup_size = await perform_backup(db_url, s3_bucket)

        duration = (datetime.now(UTC) - started).total_seconds()

        # Notify success
        if slack_webhook:
            await notify_slack(
                slack_webhook,
                f"Backup complete: {backup_size}MB in {duration:.1f}s"
            )

        return {
            "status": "success",
            "backup_size_mb": backup_size,
            "duration_seconds": duration,
            "destination": f"s3://{s3_bucket}/",
        }

    except Exception as e:
        logger.exception("Backup failed: %s", e)

        # Notify failure
        if slack_webhook:
            await notify_slack(slack_webhook, f"Backup FAILED: {e}")

        raise  # Trigger retry


async def perform_backup(db_url: str, bucket: str) -> float:
    """Actual backup logic."""
    # Implementation here
    return 42.5  # Size in MB


async def notify_slack(webhook: str, message: str) -> None:
    """Send Slack notification."""
    async with httpx.AsyncClient() as client:
        await client.post(webhook, json={"text": message})
```

```python
# manifest.py
from zerg.jobs import job_registry, JobConfig
from jobs.backup_database import run as backup_run

job_registry.register(JobConfig(
    id="backup-database",
    cron="0 2 * * *",           # 2 AM UTC daily
    func=backup_run,
    timeout_seconds=1800,       # 30 minutes
    max_attempts=3,
    tags=["backup", "database", "critical"],
    project="infrastructure",
    description="Daily database backup to S3",
))
```
