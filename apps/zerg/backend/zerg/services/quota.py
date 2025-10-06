"""Quota helpers for per-user daily run caps.

Centralised, reusable checks to keep routers/services lean.
"""

from __future__ import annotations

import logging
from datetime import datetime
from datetime import timezone

from fastapi import HTTPException
from fastapi import status
from sqlalchemy import func
from sqlalchemy.orm import Session

from zerg.config import get_settings
from zerg.events import EventType
from zerg.events.event_bus import event_bus
from zerg.models.models import Agent as AgentModel
from zerg.models.models import AgentRun as AgentRunModel
from zerg.models.models import User as UserModel
from zerg.services.ops_discord import send_budget_alert


def _is_admin(user: UserModel | None) -> bool:
    return getattr(user, "role", "USER") == "ADMIN"


def assert_can_start_run(db: Session, *, user: UserModel) -> None:
    """Raise 429 when non-admin user exceeds DAILY_RUNS_PER_USER.

    Treat 0 or missing env as "disabled" (no limit).
    """

    settings = get_settings()
    try:
        limit = int(getattr(settings, "daily_runs_per_user", 0))
    except Exception:  # noqa: BLE001
        limit = 0

    # Admins are exempt from all limits/budgets
    if _is_admin(user):
        return

    # Enforce run cap when configured (> 0). If disabled (0), skip this block
    if limit > 0:
        today_utc = datetime.now(timezone.utc).date()
        count_q = (
            db.query(func.count(AgentRunModel.id))
            .join(AgentModel, AgentModel.id == AgentRunModel.agent_id)
            .filter(
                AgentModel.owner_id == user.id,
                AgentRunModel.started_at.isnot(None),
                func.date(AgentRunModel.started_at) == today_utc,
            )
        )
        used = int(count_q.scalar() or 0)

        if used >= limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Daily run limit reached ({used}/{limit}). Try again tomorrow or contact admin.",
            )

    # --------------------------------------------------------------
    # Budget thresholds (user + global) – optional, admins exempt
    # --------------------------------------------------------------
    # Costs are stored in USD on AgentRun.total_cost_usd. Unknown costs
    # are left NULL and ignored by SUM().
    try:
        user_budget_cents = int(getattr(settings, "daily_cost_per_user_cents", 0))
    except Exception:  # noqa: BLE001
        user_budget_cents = 0
    try:
        global_budget_cents = int(getattr(settings, "daily_cost_global_cents", 0))
    except Exception:  # noqa: BLE001
        global_budget_cents = 0

    if user_budget_cents <= 0 and global_budget_cents <= 0:
        return

    logger = logging.getLogger(__name__)
    today_utc = datetime.now(timezone.utc).date()

    # Sum today's user cost
    user_cost_q = (
        db.query(func.coalesce(func.sum(AgentRunModel.total_cost_usd), 0.0))
        .join(AgentModel, AgentModel.id == AgentRunModel.agent_id)
        .filter(
            AgentModel.owner_id == user.id,
            AgentRunModel.finished_at.isnot(None),
            func.date(AgentRunModel.finished_at) == today_utc,
        )
    )
    user_cost_usd = float(user_cost_q.scalar() or 0.0)

    # Sum today's global cost
    global_cost_usd = 0.0
    if global_budget_cents > 0:
        global_cost_q = db.query(func.coalesce(func.sum(AgentRunModel.total_cost_usd), 0.0)).filter(
            AgentRunModel.finished_at.isnot(None),
            func.date(AgentRunModel.finished_at) == today_utc,
        )
        global_cost_usd = float(global_cost_q.scalar() or 0.0)

    # Helper to check a single budget
    def _check_budget(used_usd: float, budget_cents: int, scope: str) -> None:
        if budget_cents <= 0:
            return
        budget_usd = budget_cents / 100.0
        percent = (used_usd / budget_usd) * 100.0 if budget_usd > 0 else 0.0
        if used_usd >= budget_usd:
            msg = (
                f"Daily {scope} budget exhausted "
                f"(${used_usd:.2f}/${budget_usd:.2f}). "
                "Try again tomorrow or contact admin."
            )
            # Fire-and-forget notifications; ignore failures
            try:
                import asyncio

                # Discord alert at 100%
                asyncio.create_task(
                    send_budget_alert(scope, 100.0, used_usd, budget_cents, getattr(user, "email", None))
                )

                # Publish ops ticker event with captured values
                async def _emit(frame):  # pragma: no cover - tiny helper
                    await event_bus.publish(EventType.BUDGET_DENIED, frame)

                frame = {
                    "scope": scope,
                    "percent": percent,
                    "used_usd": used_usd,
                    "limit_cents": budget_cents,
                    "user_email": getattr(user, "email", None),
                }
                asyncio.create_task(_emit(frame))
            except Exception:  # pragma: no cover – robustness
                pass
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=msg)
        # Warn at 80% and above
        warn_threshold = 0.8 * budget_usd
        if used_usd >= warn_threshold:
            logger.warning(
                "Budget nearing limit: scope=%s used=%.4f budget=%.4f (%.1f%%)",
                scope,
                used_usd,
                budget_usd,
                percent,
            )
            # Optional alert at 80% if enabled – non-blocking
            try:
                import asyncio

                asyncio.create_task(
                    send_budget_alert(scope, percent, used_usd, budget_cents, getattr(user, "email", None))
                )
            except Exception:  # pragma: no cover
                pass

    _check_budget(user_cost_usd, user_budget_cents, "user")
    _check_budget(global_cost_usd, global_budget_cents, "global")
