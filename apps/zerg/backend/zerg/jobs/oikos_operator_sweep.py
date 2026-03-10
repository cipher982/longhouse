"""Periodic fallback sweep for proactive Oikos operator mode."""

from __future__ import annotations

import logging
import os
from typing import Any
from uuid import uuid4

from zerg.database import db_session
from zerg.jobs.registry import JobConfig
from zerg.jobs.registry import job_registry
from zerg.models.user import User
from zerg.services.oikos_service import invoke_oikos
from zerg.surfaces.adapters.operator import OperatorSurfaceAdapter

logger = logging.getLogger(__name__)

JOB_ID = "oikos-operator-sweep"


def _operator_mode_enabled() -> bool:
    return os.getenv("OIKOS_OPERATOR_MODE_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def _build_sweep_message() -> str:
    return "\n".join(
        [
            "System/operator wakeup: periodic coding-session sweep.",
            "",
            "Trigger: periodic_sweep",
            "Reason: fallback check for active, paused, or recently changed coding sessions.",
            "",
            "Inspect the relevant recent session history, then decide whether to " "wait, continue work, or escalate to the user.",
            "Do nothing if nothing needs attention.",
        ]
    )


async def run() -> dict[str, Any]:
    """Wake Oikos for a periodic fallback sweep when operator mode is enabled."""
    if not _operator_mode_enabled():
        return {"status": "skipped", "reason": "operator mode disabled"}

    with db_session() as db:
        owner = db.query(User.id).order_by(User.id).first()

    if owner is None:
        logger.info("Skipping operator sweep: no owner user found")
        return {"status": "skipped", "reason": "no owner"}

    owner_id = int(owner[0])
    message_id = f"operator-sweep-{uuid4()}"
    await invoke_oikos(
        owner_id,
        _build_sweep_message(),
        message_id,
        source="operator",
        surface_adapter=OperatorSurfaceAdapter(owner_id=owner_id, conversation_id="operator:sweep"),
        surface_payload={
            "trigger_type": "periodic_sweep",
            "conversation_id": "operator:sweep",
        },
    )
    return {
        "status": "enqueued",
        "owner_id": owner_id,
        "trigger_type": "periodic_sweep",
    }


job_registry.register(
    JobConfig(
        id=JOB_ID,
        cron=os.getenv("OIKOS_OPERATOR_SWEEP_CRON", "*/30 * * * *"),
        func=run,
        enabled=True,
        timeout_seconds=60,
        tags=["oikos", "autonomy", "builtin"],
        description="Periodic fallback sweep for proactive Oikos operator mode",
    )
)
