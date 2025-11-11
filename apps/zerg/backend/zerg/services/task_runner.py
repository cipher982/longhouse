"""Task Runner helper – execute an agent's *task_instructions* once.

This module provides :pyfunc:`execute_agent_task` which is the single source
of truth for running a **non-interactive** task run ("▶ Play" button,
cron/scheduler run, future webhook trigger).

The helper:

1. Creates a fresh thread seeded with the agent's *system* prompt.
2. Inserts one *user* message containing ``agent.task_instructions``.
3. Delegates to :class:`zerg.managers.agent_runner.AgentRunner` to produce the
   assistant/tool messages (token streaming disabled).
4. Updates the agent's status / timestamps and broadcasts
   :pydata:`zerg.events.EventType.AGENT_UPDATED` so dashboards refresh in
   real-time.

It unifies logic that previously lived in *two* places (agents router &
SchedulerService) and removes the last dependency on the legacy
``AgentManager.execute_task`` code path.
"""

from __future__ import annotations

import logging
from datetime import datetime
from datetime import timezone

from sqlalchemy.orm import Session

from zerg.callbacks.token_stream import set_current_user_id
from zerg.config import get_settings
from zerg.crud import crud
from zerg.events import EventType
from zerg.events.event_bus import event_bus
from zerg.managers.agent_runner import AgentRunner
from zerg.models.models import Agent as AgentModel
from zerg.models.models import Thread as ThreadModel
from zerg.services.quota import assert_can_start_run
from zerg.services.thread_service import ThreadService

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def execute_agent_task(
    db: Session, agent: AgentModel, *, thread_type: str = "manual", trigger: str | None = None
) -> ThreadModel:
    """Run *agent.task_instructions* exactly once and return the created thread.

    Parameters
    ----------
    db
        Active SQLAlchemy *Session* bound to the current request / job.
    agent
        ORM row of the agent to run.
    thread_type
        One of ``"manual"`` (▶ Play button) or ``"scheduled"`` (cron).  The
        value is persisted on the thread row for analytics.
    trigger
        Optional explicit trigger type. If not provided, inferred from thread_type.
        One of: "manual", "schedule", "chat", "webhook", "api".

    Raises
    ------
    ValueError
        If agent has no task instructions or if agent is already running.
    """

    # ------------------------------------------------------------------
    # Global kill switch – prevent outbound LLM calls when enabled
    # ------------------------------------------------------------------
    settings = get_settings()
    owner = crud.get_user(db, agent.owner_id)
    if settings.llm_disabled:
        owner_role = getattr(owner, "role", "USER") if owner else "USER"
        if owner_role != "ADMIN":
            raise ValueError("LLM is temporarily disabled by the administrator")

    # ------------------------------------------------------------------
    # Per-user daily run cap (non-admins only)
    # ------------------------------------------------------------------
    if owner:
        assert_can_start_run(db, user=owner)

    # ------------------------------------------------------------------
    # Validate pre-conditions
    # ------------------------------------------------------------------
    if not agent.task_instructions or str(agent.task_instructions).strip() == "":
        raise ValueError("Agent has no task_instructions defined")

    # ------------------------------------------------------------------
    # Acquire run lock – prefer PostgreSQL advisory locks; fallback preserves
    # legacy status-based guard for non-Postgres engines.
    # ------------------------------------------------------------------
    use_advisory = bool(getattr(db.bind, "dialect", None) and db.bind.dialect.name == "postgresql")

    if use_advisory:
        from zerg.services.agent_locks import AgentLockManager

        # Hold the advisory lock for the entire run window.
        with AgentLockManager.agent_lock(db, agent.id) as acquired:
            if not acquired:
                raise ValueError("Agent already running")

            # Persist status for UI/telemetry while the advisory lock enforces exclusivity
            crud.update_agent(db, agent.id, status="running")
            db.commit()
            await event_bus.publish(
                EventType.AGENT_UPDATED,
                {"event_type": "agent_updated", "id": agent.id, "status": "running"},
            )

            # Proceed with execution inside the lock scope
            # ------------------------------------------------------------------
            # Create the new thread + seed messages.
            # ------------------------------------------------------------------
            timestamp_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            title = f"Task Run – {timestamp_str}"

            thread = ThreadService.create_thread_with_system_message(
                db,
                agent,
                title=title,
                thread_type=thread_type,
                active=False,  # task runs are not the *active* chat thread
            )

            # Insert the user *task* prompt (unprocessed)
            crud.create_thread_message(
                db=db,
                thread_id=thread.id,
                role="user",
                content=agent.task_instructions,
                processed=False,
            )

            # ------------------------------------------------------------------
            # Persist an *AgentRun* row so dashboards can display progress.
            # ------------------------------------------------------------------
            # Use explicit trigger if provided, otherwise infer from thread_type
            run_trigger = trigger if trigger else (thread_type if thread_type in {"manual", "schedule"} else "api")
            run_row = crud.create_run(
                db,
                agent_id=agent.id,
                thread_id=thread.id,
                trigger=run_trigger,
                status="queued",
            )

            await event_bus.publish(
                EventType.RUN_CREATED,
                {
                    "event_type": "run_created",
                    "agent_id": agent.id,
                    "run_id": run_row.id,
                    "status": "queued",
                    "thread_id": thread.id,
                },
            )

            # Immediately mark as running (no async queue yet)
            start_ts = datetime.now(timezone.utc)
            crud.mark_running(db, run_row.id, started_at=start_ts)
            await event_bus.publish(
                EventType.RUN_UPDATED,
                {
                    "event_type": "run_updated",
                    "agent_id": agent.id,
                    "run_id": run_row.id,
                    "status": "running",
                    "started_at": start_ts.isoformat(),
                    "thread_id": thread.id,
                },
            )

            # ------------------------------------------------------------------
            # Delegate to AgentRunner (no token stream) and capture duration.
            # ------------------------------------------------------------------
            runner = AgentRunner(agent)

            # Set user context for token streaming
            set_current_user_id(agent.owner_id)

            try:
                try:
                    await runner.run_thread(db, thread)

                except Exception as exc:
                    # Persist run failure first
                    end_ts = datetime.now(timezone.utc)
                    duration_ms = int((end_ts - start_ts).total_seconds() * 1000)
                    crud.mark_failed(db, run_row.id, finished_at=end_ts, duration_ms=duration_ms, error=str(exc))

                    await event_bus.publish(
                        EventType.RUN_UPDATED,
                        {
                            "event_type": "run_updated",
                            "agent_id": agent.id,
                            "run_id": run_row.id,
                            "status": "failed",
                            "finished_at": end_ts.isoformat(),
                            "duration_ms": duration_ms,
                            "error": str(exc),
                            "thread_id": thread.id,
                        },
                    )

                    # Persist agent error state & broadcast so dashboards refresh
                    crud.update_agent(db, agent.id, status="error", last_error=str(exc))
                    db.commit()

                    await event_bus.publish(
                        EventType.AGENT_UPDATED,
                        {
                            "event_type": "agent_updated",
                            "id": agent.id,
                            "status": "error",
                            "last_error": str(exc),
                        },
                    )

                    logger.exception("Task run failed for agent %s", agent.id)
                    raise

                # ------------------------------------------------------------------
                # Success – update run + flip agent back to idle.
                # ------------------------------------------------------------------
                end_ts = datetime.now(timezone.utc)
                duration_ms = int((end_ts - start_ts).total_seconds() * 1000)

                # Persist usage + cost if available
                total_tokens = runner.usage_total_tokens
                total_cost_usd = None
                if runner.usage_prompt_tokens is not None and runner.usage_completion_tokens is not None:
                    # Compute cost only when pricing known
                    from zerg.pricing import get_usd_prices_per_1k

                    prices = get_usd_prices_per_1k(agent.model)
                    if prices is not None:
                        in_price, out_price = prices
                        total_cost_usd = (
                            (runner.usage_prompt_tokens * in_price) + (runner.usage_completion_tokens * out_price)
                        ) / 1000.0

                # Mark run as finished (summary auto-extracted if not provided)
                finished_run = crud.mark_finished(
                    db,
                    run_row.id,
                    finished_at=end_ts,
                    duration_ms=duration_ms,
                    total_tokens=total_tokens,
                    total_cost_usd=total_cost_usd,
                )

                # Refresh to get the auto-extracted summary
                if finished_run:
                    db.refresh(finished_run)

                await event_bus.publish(
                    EventType.RUN_UPDATED,
                    {
                        "event_type": "run_updated",
                        "agent_id": agent.id,
                        "run_id": run_row.id,
                        "status": "success",
                        "finished_at": end_ts.isoformat(),
                        "duration_ms": duration_ms,
                        "summary": finished_run.summary if finished_run else None,
                        "thread_id": thread.id,
                    },
                )

                # For scheduled agents, revert to idle (status enum only supports idle/running/error/processing)
                new_status = "idle"

                crud.update_agent(db, agent.id, status=new_status, last_run_at=end_ts, last_error=None)
                db.commit()

                await event_bus.publish(
                    EventType.AGENT_UPDATED,
                    {
                        "event_type": "agent_updated",
                        "id": agent.id,
                        "status": new_status,
                        "last_run_at": end_ts.isoformat(),
                        "thread_id": thread.id,
                        "last_error": None,
                    },
                )

                return thread
            finally:
                # Always clean up user context
                set_current_user_id(None)

    # If we are here, the database is not PostgreSQL; this app requires
    # PostgreSQL for advisory locks. Simplify by failing fast.
    raise ValueError("PostgreSQL is required for agent execution (advisory locks)")
