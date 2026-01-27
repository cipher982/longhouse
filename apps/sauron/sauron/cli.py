"""Sauron CLI for manual operations.

Commands:
- run: Execute a job manually
- list: List all registered jobs
- next: Show next scheduled runs
- version: Show version
"""

import asyncio
import logging
import os
from datetime import UTC, datetime

import typer
from dotenv import load_dotenv

# Load .env for local dev
load_dotenv()

# Configure logging
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = typer.Typer(help="Sauron - Centralized ops scheduler CLI")


def _init_jobs() -> int:
    """Initialize job registry (without scheduler).

    Initializes GitSyncService for external jobs and registers all jobs.
    Returns count of registered jobs.
    """
    async def _load():
        from pathlib import Path

        from zerg.jobs import GitSyncService, register_all_jobs, set_git_sync_service

        from sauron.config import get_settings

        settings = get_settings()

        # Initialize git sync for external jobs
        if settings.jobs_git_repo_url:
            git_service = GitSyncService(
                repo_url=settings.jobs_git_repo_url,
                local_path=Path(settings.jobs_dir),
                branch=settings.jobs_git_branch,
                token=settings.jobs_git_token,
            )
            await git_service.ensure_cloned()
            set_git_sync_service(git_service)
            logger.info(f"Git sync initialized: {git_service.current_sha}")

        return await register_all_jobs(scheduler=None, use_queue=False)

    return asyncio.run(_load())


@app.command()
def run(
    job_id: str = typer.Argument(..., help="Job ID to run"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would run without executing"),
    direct: bool = typer.Option(False, "--direct", help="Run directly instead of via queue"),
):
    """Run a specific job manually."""
    from zerg.jobs import job_registry

    # Load jobs
    _init_jobs()

    job = job_registry.get(job_id)
    if not job:
        typer.echo(f"Unknown job: {job_id}", err=True)
        typer.echo("\nAvailable jobs:")
        for j in job_registry.list_jobs():
            typer.echo(f"  {j.id}")
        raise typer.Exit(1)

    typer.echo(f"Job: {job.id}")
    typer.echo(f"Cron: {job.cron}")
    typer.echo(f"Enabled: {job.enabled}")
    typer.echo(f"Timeout: {job.timeout_seconds}s")
    typer.echo("")

    if dry_run:
        typer.echo("[DRY RUN] Would execute job - skipping")
        return

    if direct:
        # Run directly (bypass queue)
        typer.echo("Executing directly...")

        async def _execute():
            result = await job_registry.run_job(job_id)
            return result

        result = asyncio.run(_execute())
        typer.echo(f"Status: {result.status}")
        if result.error:
            typer.echo(f"Error: {result.error}")
            raise typer.Exit(1)
    else:
        # Enqueue to queue
        typer.echo("Enqueuing to job queue...")

        async def _enqueue():
            from zerg.jobs.queue import enqueue_job, make_dedupe_key

            now = datetime.now(UTC)
            dedupe_key = make_dedupe_key(job_id, now)
            queue_id = await enqueue_job(
                job_id=job_id,
                scheduled_for=now,
                dedupe_key=dedupe_key,
                max_attempts=job.max_attempts,
            )
            return queue_id

        queue_id = asyncio.run(_enqueue())
        if queue_id:
            typer.echo(f"Queued as: {queue_id}")
        else:
            typer.echo("Already queued (dedupe)")

    typer.echo("Done!")


@app.command("list")
def list_jobs(
    all_jobs: bool = typer.Option(False, "--all", "-a", help="Include disabled jobs"),
):
    """List all registered jobs."""
    from zerg.jobs import job_registry

    # Load jobs
    _init_jobs()

    jobs = job_registry.list_jobs(enabled_only=not all_jobs)

    typer.echo("Registered jobs:")
    typer.echo("")

    for job in jobs:
        status = "[enabled]" if job.enabled else "[disabled]"
        tags = ", ".join(job.tags) if job.tags else "-"
        typer.echo(f"  {job.id}")
        typer.echo(f"    Schedule: {job.cron}")
        typer.echo(f"    Status:   {status}")
        typer.echo(f"    Tags:     {tags}")
        typer.echo(f"    Project:  {job.project or '-'}")
        typer.echo("")


@app.command("next")
def next_runs(
    count: int = typer.Option(10, "--count", "-n", help="Number of runs to show"),
):
    """Show next scheduled runs."""
    from apscheduler.triggers.cron import CronTrigger

    from zerg.jobs import job_registry

    # Load jobs
    _init_jobs()

    typer.echo("Next scheduled runs:")
    typer.echo("")

    now = datetime.now(UTC)
    runs = []

    for job in job_registry.list_jobs(enabled_only=True):
        trigger = CronTrigger.from_crontab(job.cron, timezone="UTC")
        next_run = trigger.get_next_fire_time(None, now)
        if next_run:
            runs.append((next_run, job.id))

    # Sort by next run time
    runs.sort(key=lambda x: x[0])

    for next_run, job_id in runs[:count]:
        delta = next_run - now
        hours = int(delta.total_seconds() // 3600)
        minutes = int((delta.total_seconds() % 3600) // 60)
        typer.echo(f"  {next_run.strftime('%Y-%m-%d %H:%M')} UTC (+{hours}h {minutes}m) - {job_id}")


@app.command()
def version():
    """Show version."""
    from sauron import __version__

    typer.echo(f"Sauron v{__version__}")


@app.command()
def sync():
    """Force git sync of jobs repo."""
    from zerg.jobs import get_git_sync_service, set_git_sync_service, GitSyncService
    from pathlib import Path
    from sauron.config import get_settings

    settings = get_settings()

    if not settings.jobs_git_repo_url:
        typer.echo("JOBS_GIT_REPO_URL not configured", err=True)
        raise typer.Exit(1)

    async def _sync():
        git_service = GitSyncService(
            repo_url=settings.jobs_git_repo_url,
            local_path=Path(settings.jobs_dir),
            branch=settings.jobs_git_branch,
            token=settings.jobs_git_token,
        )
        await git_service.ensure_cloned()
        result = await git_service.refresh()
        return result, git_service.current_sha

    result, sha = asyncio.run(_sync())
    typer.echo(f"Sync complete: {sha}")
    typer.echo(f"Message: {result.get('message', 'OK')}")


if __name__ == "__main__":
    app()
