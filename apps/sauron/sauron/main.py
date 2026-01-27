"""Sauron main scheduler entrypoint.

Standalone APScheduler service that reuses zerg.jobs infrastructure:
- GitSyncService to clone/sync sauron-jobs repo
- job_registry + register_all_jobs() to load jobs
- Worker loop to execute queued jobs
- FastAPI for Jarvis control
"""

import asyncio
import logging
import sys
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

from sauron.config import get_settings

# Load .env for local dev
load_dotenv()


def configure_logging() -> None:
    """Configure logging with standard format."""
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )


logger = logging.getLogger(__name__)


async def run_scheduler() -> None:
    """Main scheduler loop.

    1. Initialize GitSyncService for sauron-jobs repo
    2. Clone repo (blocking on startup)
    3. Register all jobs (builtin + manifest)
    4. Backfill missed runs
    5. Start APScheduler
    6. Run worker loop
    """
    # Import zerg.jobs components
    # These are installed as dependencies via pyproject.toml
    from zerg.jobs import GitSyncService, register_all_jobs, run_git_sync_loop, set_git_sync_service
    from zerg.jobs.commis import enqueue_missed_runs, run_queue_commis

    settings = get_settings()

    logger.info("=" * 60)
    logger.info("Starting Sauron scheduler v2.0")
    logger.info(f"Jobs repo: {settings.jobs_git_repo_url}")
    logger.info(f"Jobs dir: {settings.jobs_dir}")
    logger.info(f"Database: {'configured' if settings.database_url else 'NOT CONFIGURED'}")
    logger.info("=" * 60)

    if not settings.database_url:
        logger.error("DATABASE_URL is required for job queue; exiting.")
        raise SystemExit(1)

    # Initialize git sync service if repo URL is configured
    git_service = None
    if settings.jobs_git_repo_url:
        logger.info(f"Initializing GitSyncService for {settings.jobs_git_repo_url}")
        git_service = GitSyncService(
            repo_url=settings.jobs_git_repo_url,
            local_path=Path(settings.jobs_dir),
            branch=settings.jobs_git_branch,
            token=settings.jobs_git_token,
        )

        # BLOCKING: Clone repo on startup
        logger.info("Cloning jobs repo (this may take a moment)...")
        await git_service.ensure_cloned()
        logger.info(f"Jobs repo ready at {settings.jobs_dir}")

        # Set global git service for zerg.jobs.loader
        set_git_sync_service(git_service)

        # Start background git sync loop
        if settings.jobs_refresh_interval_seconds > 0:
            logger.info(f"Starting git sync loop (interval: {settings.jobs_refresh_interval_seconds}s)")
            asyncio.create_task(
                run_git_sync_loop(git_service, settings.jobs_refresh_interval_seconds),
                name="git-sync-loop",
            )
    else:
        logger.warning("JOBS_GIT_REPO_URL not configured - running builtin jobs only")

    # Create APScheduler
    scheduler = AsyncIOScheduler(timezone="UTC")

    # Register all jobs (builtin + manifest from git repo)
    # This imports builtin modules and loads manifest.py
    scheduled_count = await register_all_jobs(scheduler=scheduler, use_queue=True)
    logger.info(f"Registered {scheduled_count} jobs")

    # Publish job definitions to Life-Hub for ops dashboard
    try:
        from sauron.job_definitions import publish_job_definitions

        await asyncio.to_thread(publish_job_definitions)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Failed to publish job definitions: {e}")

    # Backfill any missed runs
    logger.info("Checking for missed runs...")
    await enqueue_missed_runs()

    # Start scheduler
    scheduler.start()

    # Log next run times
    logger.info("Next scheduled runs:")
    for job in scheduler.get_jobs():
        logger.info(f"  {job.id}: {job.next_run_time}")

    # Start queue worker in background
    worker_task = asyncio.create_task(run_queue_commis(), name="queue-commis")

    # Keep running forever
    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down...")
        worker_task.cancel()
        scheduler.shutdown()
        logger.info("Goodbye!")


async def run_api() -> None:
    """Run the FastAPI server for Jarvis control."""
    import uvicorn

    from sauron.api import app

    settings = get_settings()
    config = uvicorn.Config(
        app,
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )
    server = uvicorn.Server(config)
    await server.serve()


async def main() -> None:
    """Main entry point - runs scheduler and API concurrently."""
    configure_logging()

    # Run scheduler and API in parallel
    await asyncio.gather(
        run_scheduler(),
        run_api(),
    )


if __name__ == "__main__":
    asyncio.run(main())
