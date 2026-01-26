"""
Scheduler Service for managing scheduled fiche tasks.

This module provides the SchedulerService class that handles:
- Initializing and managing APScheduler
- Loading and scheduling fiches from the database
- Running fiche tasks on schedule
"""

import logging

# APScheduler is part of the mandatory backend dependencies; import directly.
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# Legacy FicheManager no longer required – all logic goes through TaskRunner
from zerg.crud import crud
from zerg.database import db_session
from zerg.database import default_session_factory

# EventBus remains for UI notifications
from zerg.events import EventType
from zerg.events.event_bus import event_bus

# New unified task runner helper
from zerg.services.task_runner import execute_fiche_task

logger = logging.getLogger(__name__)


class SchedulerService:
    """Service for managing scheduled fiche tasks."""

    def __init__(self, session_factory=None):
        """Initialize the scheduler service."""
        self.scheduler = AsyncIOScheduler()
        self._initialized = False
        self.session_factory = session_factory or default_session_factory

    async def start(self):
        """Start the scheduler if not already running."""
        if not self._initialized:
            # Load all scheduled fiches from DB
            await self.load_scheduled_fiches()

            # Subscribe to fiche events for dynamic scheduling
            await self._subscribe_to_events()

            # Add barrier reaper job (runs every 60 seconds)
            self._add_barrier_reaper_job()

            # Start the scheduler
            self.scheduler.start()
            self._initialized = True
            logger.info("Scheduler service started")

    def _add_barrier_reaper_job(self):
        """Add a periodic job to reap expired barriers."""
        from apscheduler.triggers.interval import IntervalTrigger

        # Run every 60 seconds to catch expired barriers
        self.scheduler.add_job(
            self._run_barrier_reaper,
            trigger=IntervalTrigger(seconds=60),
            id="barrier_reaper",
            replace_existing=True,
        )
        logger.info("Barrier reaper job scheduled (every 60s)")

    async def _run_barrier_reaper(self):
        """Execute the barrier reaper task."""
        from zerg.services.commis_resume import reap_expired_barriers

        try:
            with db_session(self.session_factory) as db:
                result = await reap_expired_barriers(db)
                if result.get("reaped", 0) > 0:
                    logger.info(f"Barrier reaper: reaped {result['reaped']} expired barriers")
        except Exception as e:
            logger.exception(f"Barrier reaper failed: {e}")

    async def stop(self):
        """Shutdown the scheduler gracefully."""
        if self._initialized:
            self.scheduler.shutdown()
            self._initialized = False
            logger.info("Scheduler service stopped")

    async def _subscribe_to_events(self):
        """Subscribe to fiche-related events for dynamic scheduling updates."""
        # Subscribe to fiche created events
        event_bus.subscribe(EventType.FICHE_CREATED, self._handle_fiche_created)
        # Subscribe to fiche updated events
        event_bus.subscribe(EventType.FICHE_UPDATED, self._handle_fiche_updated)
        # Subscribe to fiche deleted events
        event_bus.subscribe(EventType.FICHE_DELETED, self._handle_fiche_deleted)

        # External triggers
        event_bus.subscribe(EventType.TRIGGER_FIRED, self._handle_trigger_fired)

        logger.info("Scheduler subscribed to fiche events")

    async def _handle_fiche_created(self, data):
        """Handle fiche created events by scheduling if needed."""
        if data.get("schedule"):
            fiche_id = data.get("id")
            cron_expression = data.get("schedule")
            logger.info(f"Scheduling newly created fiche {fiche_id}")
            await self.schedule_fiche(fiche_id, cron_expression)

    async def _handle_fiche_updated(self, data):
        """
        Handle fiche updated events by updating scheduling accordingly.
        Re-schedule or unschedule the job when the cron expression changes.
        """
        fiche_id = data.get("id")
        schedule = data.get("schedule")

        # If we can't determine schedule, load from DB
        if schedule is None:
            with db_session(self.session_factory) as db:
                fiche = crud.get_fiche(db, fiche_id)
                if fiche:
                    schedule = fiche.schedule

        # Remove any existing job regardless
        self.remove_fiche_job(fiche_id)

        # Re-schedule if a cron expression is set
        if schedule:
            logger.info(f"Updating schedule for fiche {fiche_id}")
            await self.schedule_fiche(fiche_id, schedule)
        else:
            logger.info(f"Fiche {fiche_id} now has no schedule – unscheduled.")

    async def _handle_fiche_deleted(self, data):
        """Handle fiche deleted events by removing any scheduled jobs."""
        fiche_id = data.get("id")
        if fiche_id:
            logger.info(f"Removing schedule for deleted fiche {fiche_id}")
            self.remove_fiche_job(fiche_id)

    async def _handle_trigger_fired(self, data):
        """Run the associated fiche immediately when a trigger fires."""

        fiche_id = data.get("fiche_id")
        if fiche_id is None:
            logger.warning("trigger_fired event missing fiche_id – ignoring")
            return

        # Extract trigger type from event payload, default to "webhook" for backwards compatibility
        trigger_type = data.get("trigger_type", "webhook")
        logger.info(f"Trigger fired for fiche {fiche_id} with trigger={trigger_type}; executing task now")

        # Execute the fiche task immediately (await) so tests can observe the
        # call synchronously; the actual work done inside `run_fiche_task` is
        # asynchronous and non‑blocking.  If later we need true fire‑and‑forget
        # behaviour we can switch back to `asyncio.create_task`.
        await self.run_fiche_task(fiche_id, trigger=trigger_type)

    async def load_scheduled_fiches(self):
        """Load all fiches that define a cron schedule and register them."""

        try:
            with db_session(self.session_factory) as db:
                # Query as plain tuples so ORM instances are never leaked outside
                # this helper – allows us to close the session safely.
                fiche_rows: list[tuple[int, str]] = (
                    db.query(crud.Fiche.id, crud.Fiche.schedule).filter(crud.Fiche.schedule.isnot(None)).all()
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("Error loading scheduled fiches: %s", exc)
            fiche_rows = []

        # Register jobs outside the DB session – schedule_fiche queries the
        # DB again if needed but mostly just registers APScheduler jobs.
        for fiche_id, cron_expr in fiche_rows:
            await self.schedule_fiche(fiche_id, cron_expr)
            logger.info("Scheduled fiche %s with cron: %s", fiche_id, cron_expr)

    async def schedule_fiche(self, fiche_id: int, cron_expression: str):
        """
        Schedule a fiche to run according to its cron expression.

        Args:
            fiche_id: The ID of the fiche to schedule
            cron_expression: The cron expression defining when to run the fiche
        """
        try:
            # Remove any existing jobs for this fiche
            self.remove_fiche_job(fiche_id)

            # Add new job with the cron trigger
            self.scheduler.add_job(
                self.run_fiche_task,
                CronTrigger.from_crontab(cron_expression),
                args=[fiche_id],
                id=f"fiche_{fiche_id}",
                replace_existing=True,
            )
            logger.info(f"Added schedule for fiche {fiche_id}: {cron_expression}")

            # Persist next run time in DB
            job = self.scheduler.get_job(f"fiche_{fiche_id}")
            # Only persist if we have a valid next run AND the scheduler is
            # already running (during test load_scheduled_fiches the scheduler
            # has not started yet and persisting here detaches instances that
            # the tests still hold).
            if self.scheduler.running and job and getattr(job, "next_run_time", None):
                next_run = job.next_run_time

                with db_session(self.session_factory) as db:
                    fiche = crud.get_fiche(db, fiche_id)
                    if fiche:
                        fiche.next_course_at = next_run

        except Exception as e:
            logger.error(f"Error scheduling fiche {fiche_id}: {e}")

    def remove_fiche_job(self, fiche_id: int):
        """Remove any existing scheduled jobs for the given fiche."""
        job_id = f"fiche_{fiche_id}"
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)
            logger.info(f"Removed existing schedule for fiche {fiche_id}")

        # Clear next_course_at in DB as it's no longer scheduled
        with db_session(self.session_factory) as db:
            fiche = crud.get_fiche(db, fiche_id)
            if fiche:
                fiche.next_course_at = None

    async def run_fiche_task(self, fiche_id: int, trigger: str = "schedule"):
        """
        Execute a fiche's task.

        This is the function that gets called by the scheduler when a job triggers.
        It handles:
        - Getting a DB session
        - Loading the fiche
        - Creating a new thread for this run using the execute_task method
        - Running the fiche's task instructions

        Parameters
        ----------
        fiche_id
            The ID of the fiche to run.
        trigger
            The trigger type: "schedule" for cron jobs, "webhook" for webhook triggers.
        """
        try:
            with db_session(self.session_factory) as db:
                fiche = crud.get_fiche(db, fiche_id)
                if fiche is None:
                    logger.error("Fiche %s not found", fiche_id)
                    return

                # ------------------------------------------------------------------
                # Delegate to shared helper (handles status flips & events).
                # Scheduler runs silently skip if fiche is already running.
                # ------------------------------------------------------------------
                logger.info("Running task for fiche %s with trigger=%s", fiche_id, trigger)
                # Pass explicit trigger type to distinguish schedule vs webhook
                try:
                    thread = await execute_fiche_task(db, fiche, thread_type="schedule", trigger=trigger)
                except ValueError as exc:
                    if "already running" in str(exc).lower():
                        logger.info("Skipping scheduled run for fiche %s - already running", fiche_id)
                        return
                    raise

                # ------------------------------------------------------------------
                # Update *next_course_at* after successful run so dashboards show when
                # the task will fire next.  We do *not* touch last_course_at – helper
                # already set it.
                # ------------------------------------------------------------------
                job = self.scheduler.get_job(f"fiche_{fiche_id}")
                next_run_time = getattr(job, "next_run_time", None) if job else None
                if next_run_time:
                    crud.update_fiche(db, fiche_id, next_course_at=next_run_time)

                    await event_bus.publish(
                        EventType.FICHE_UPDATED,
                        {
                            "id": fiche_id,
                            "next_course_at": next_run_time.isoformat(),
                            "thread_id": thread.id,
                        },
                    )

        except Exception as exc:
            # execute_fiche_task already flipped status to *error* and
            # broadcasted so here we just log.
            logger.exception("Scheduled task failed for fiche %s: %s", fiche_id, exc)


# Global instance of the scheduler service
scheduler_service = SchedulerService()
