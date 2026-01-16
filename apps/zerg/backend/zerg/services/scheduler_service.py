"""
Scheduler Service for managing scheduled agent tasks.

This module provides the SchedulerService class that handles:
- Initializing and managing APScheduler
- Loading and scheduling agents from the database
- Running agent tasks on schedule
"""

import logging

# APScheduler is part of the mandatory backend dependencies; import directly.
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# Legacy AgentManager no longer required – all logic goes through TaskRunner
from zerg.crud import crud
from zerg.database import db_session
from zerg.database import default_session_factory

# EventBus remains for UI notifications
from zerg.events import EventType
from zerg.events.event_bus import event_bus

# New unified task runner helper
from zerg.services.task_runner import execute_agent_task

logger = logging.getLogger(__name__)


class SchedulerService:
    """Service for managing scheduled agent tasks."""

    def __init__(self, session_factory=None):
        """Initialize the scheduler service."""
        self.scheduler = AsyncIOScheduler()
        self._initialized = False
        self.session_factory = session_factory or default_session_factory

    async def start(self):
        """Start the scheduler if not already running."""
        if not self._initialized:
            # Load all scheduled agents from DB
            await self.load_scheduled_agents()

            # Subscribe to agent events for dynamic scheduling
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
            name="Barrier Reaper",
            replace_existing=True,
        )
        logger.info("Barrier reaper job scheduled (every 60s)")

    async def _run_barrier_reaper(self):
        """Execute the barrier reaper task."""
        from zerg.services.worker_resume import reap_expired_barriers

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
        """Subscribe to agent-related events for dynamic scheduling updates."""
        # Subscribe to agent created events
        event_bus.subscribe(EventType.AGENT_CREATED, self._handle_agent_created)
        # Subscribe to agent updated events
        event_bus.subscribe(EventType.AGENT_UPDATED, self._handle_agent_updated)
        # Subscribe to agent deleted events
        event_bus.subscribe(EventType.AGENT_DELETED, self._handle_agent_deleted)

        # External triggers
        event_bus.subscribe(EventType.TRIGGER_FIRED, self._handle_trigger_fired)

        logger.info("Scheduler subscribed to agent events")

    async def _handle_agent_created(self, data):
        """Handle agent created events by scheduling if needed."""
        if data.get("schedule"):
            agent_id = data.get("id")
            cron_expression = data.get("schedule")
            logger.info(f"Scheduling newly created agent {agent_id}")
            await self.schedule_agent(agent_id, cron_expression)

    async def _handle_agent_updated(self, data):
        """
        Handle agent updated events by updating scheduling accordingly.
        Re-schedule or unschedule the job when the cron expression changes.
        """
        agent_id = data.get("id")
        schedule = data.get("schedule")

        # If we can't determine schedule, load from DB
        if schedule is None:
            with db_session(self.session_factory) as db:
                agent = crud.get_agent(db, agent_id)
                if agent:
                    schedule = agent.schedule

        # Remove any existing job regardless
        self.remove_agent_job(agent_id)

        # Re-schedule if a cron expression is set
        if schedule:
            logger.info(f"Updating schedule for agent {agent_id}")
            await self.schedule_agent(agent_id, schedule)
        else:
            logger.info(f"Agent {agent_id} now has no schedule – unscheduled.")

    async def _handle_agent_deleted(self, data):
        """Handle agent deleted events by removing any scheduled jobs."""
        agent_id = data.get("id")
        if agent_id:
            logger.info(f"Removing schedule for deleted agent {agent_id}")
            self.remove_agent_job(agent_id)

    async def _handle_trigger_fired(self, data):
        """Run the associated agent immediately when a trigger fires."""

        agent_id = data.get("agent_id")
        if agent_id is None:
            logger.warning("trigger_fired event missing agent_id – ignoring")
            return

        # Extract trigger type from event payload, default to "webhook" for backwards compatibility
        trigger_type = data.get("trigger_type", "webhook")
        logger.info(f"Trigger fired for agent {agent_id} with trigger={trigger_type}; executing run task now")

        # Execute the agent task immediately (await) so tests can observe the
        # call synchronously; the actual work done inside `run_agent_task` is
        # asynchronous and non‑blocking.  If later we need true fire‑and‑forget
        # behaviour we can switch back to `asyncio.create_task`.
        await self.run_agent_task(agent_id, trigger=trigger_type)

    async def load_scheduled_agents(self):
        """Load all agents that define a cron schedule and register them."""

        try:
            with db_session(self.session_factory) as db:
                # Query as plain tuples so ORM instances are never leaked outside
                # this helper – allows us to close the session safely.
                agent_rows: list[tuple[int, str]] = (
                    db.query(crud.Agent.id, crud.Agent.schedule).filter(crud.Agent.schedule.isnot(None)).all()
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("Error loading scheduled agents: %s", exc)
            agent_rows = []

        # Register jobs outside the DB session – schedule_agent queries the
        # DB again if needed but mostly just registers APScheduler jobs.
        for agent_id, cron_expr in agent_rows:
            await self.schedule_agent(agent_id, cron_expr)
            logger.info("Scheduled agent %s with cron: %s", agent_id, cron_expr)

    async def schedule_agent(self, agent_id: int, cron_expression: str):
        """
        Schedule an agent to run according to its cron expression.

        Args:
            agent_id: The ID of the agent to schedule
            cron_expression: The cron expression defining when to run the agent
        """
        try:
            # Remove any existing jobs for this agent
            self.remove_agent_job(agent_id)

            # Add new job with the cron trigger
            self.scheduler.add_job(
                self.run_agent_task,
                CronTrigger.from_crontab(cron_expression),
                args=[agent_id],
                id=f"agent_{agent_id}",
                replace_existing=True,
            )
            logger.info(f"Added schedule for agent {agent_id}: {cron_expression}")

            # Persist next run time in DB
            job = self.scheduler.get_job(f"agent_{agent_id}")
            # Only persist if we have a valid next run AND the scheduler is
            # already running (during test load_scheduled_agents the scheduler
            # has not started yet and persisting here detaches instances that
            # the tests still hold).
            if self.scheduler.running and job and getattr(job, "next_run_time", None):
                next_run = job.next_run_time

                with db_session(self.session_factory) as db:
                    agent = crud.get_agent(db, agent_id)
                    if agent:
                        agent.next_run_at = next_run

        except Exception as e:
            logger.error(f"Error scheduling agent {agent_id}: {e}")

    def remove_agent_job(self, agent_id: int):
        """Remove any existing scheduled jobs for the given agent."""
        job_id = f"agent_{agent_id}"
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)
            logger.info(f"Removed existing schedule for agent {agent_id}")

        # Clear next_run_at in DB as it's no longer scheduled
        with db_session(self.session_factory) as db:
            agent = crud.get_agent(db, agent_id)
            if agent:
                agent.next_run_at = None

    async def run_agent_task(self, agent_id: int, trigger: str = "schedule"):
        """
        Execute an agent's task.

        This is the function that gets called by the scheduler when a job triggers.
        It handles:
        - Getting a DB session
        - Loading the agent
        - Creating a new thread for this run using the execute_task method
        - Running the agent's task instructions

        Parameters
        ----------
        agent_id
            The ID of the agent to run.
        trigger
            The trigger type: "schedule" for cron jobs, "webhook" for webhook triggers.
        """
        try:
            with db_session(self.session_factory) as db:
                agent = crud.get_agent(db, agent_id)
                if agent is None:
                    logger.error("Agent %s not found", agent_id)
                    return

                # ------------------------------------------------------------------
                # Delegate to shared helper (handles status flips & events).
                # Scheduler runs silently skip if agent is already running.
                # ------------------------------------------------------------------
                logger.info("Running task for agent %s with trigger=%s", agent_id, trigger)
                # Pass explicit trigger type to distinguish schedule vs webhook
                try:
                    thread = await execute_agent_task(db, agent, thread_type="schedule", trigger=trigger)
                except ValueError as exc:
                    if "already running" in str(exc).lower():
                        logger.info("Skipping scheduled run for agent %s - already running", agent_id)
                        return
                    raise

                # ------------------------------------------------------------------
                # Update *next_run_at* after successful run so dashboards show when
                # the task will fire next.  We do *not* touch last_run_at – helper
                # already set it.
                # ------------------------------------------------------------------
                job = self.scheduler.get_job(f"agent_{agent_id}")
                next_run_time = getattr(job, "next_run_time", None) if job else None
                if next_run_time:
                    crud.update_agent(db, agent_id, next_run_at=next_run_time)

                    await event_bus.publish(
                        EventType.AGENT_UPDATED,
                        {
                            "id": agent_id,
                            "next_run_at": next_run_time.isoformat(),
                            "thread_id": thread.id,
                        },
                    )

        except Exception as exc:
            # execute_agent_task already flipped status to *error* and
            # broadcasted so here we just log.
            logger.exception("Scheduled task failed for agent %s: %s", agent_id, exc)


# Global instance of the scheduler service
scheduler_service = SchedulerService()
