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
from zerg.services.oikos_operator_policy import get_operator_policy
from zerg.services.oikos_operator_policy import operator_master_switch_enabled
from zerg.services.oikos_service import invoke_oikos
from zerg.services.oikos_wakeup_ledger import WAKEUP_STATUS_ENQUEUED
from zerg.services.oikos_wakeup_ledger import WAKEUP_STATUS_FAILED
from zerg.services.oikos_wakeup_ledger import WAKEUP_STATUS_SUPPRESSED
from zerg.services.oikos_wakeup_ledger import append_wakeup
from zerg.surfaces.adapters.operator import OperatorSurfaceAdapter

logger = logging.getLogger(__name__)

JOB_ID = "oikos-operator-sweep"
_SWEEP_CONVERSATION_ID = "operator:sweep"
_SWEEP_TRIGGER_TYPE = "periodic_sweep"
_SWEEP_WAKEUP_KEY = "periodic_sweep:operator:sweep"


def _build_sweep_message() -> str:
    return "\n".join(
        [
            "System/operator wakeup: periodic coding-session sweep.",
            "",
            "Trigger: periodic_sweep",
            "Reason: fallback check for active, paused, or recently changed coding sessions.",
            "",
            "Inspect the relevant recent session history, then decide whether to wait, continue, or escalate.",
            "Do nothing if nothing needs attention.",
        ]
    )


async def run() -> dict[str, Any]:
    """Wake Oikos for a periodic fallback sweep when operator mode is enabled."""
    if not operator_master_switch_enabled():
        return {"status": "skipped", "reason": "operator mode disabled"}

    wakeup_payload = {
        "trigger_type": _SWEEP_TRIGGER_TYPE,
        "conversation_id": _SWEEP_CONVERSATION_ID,
    }

    with db_session() as db:
        owner = db.query(User.id).order_by(User.id).first()
        if owner is None:
            append_wakeup(
                db,
                owner_id=None,
                source="periodic_sweep",
                trigger_type=_SWEEP_TRIGGER_TYPE,
                status=WAKEUP_STATUS_SUPPRESSED,
                reason="no_owner",
                conversation_id=_SWEEP_CONVERSATION_ID,
                wakeup_key=_SWEEP_WAKEUP_KEY,
                payload=wakeup_payload,
            )
            logger.info("Skipping operator sweep: no owner user found")
            return {"status": "skipped", "reason": "no owner"}

        owner_id = int(owner[0])
        if not get_operator_policy(db, owner_id).enabled:
            append_wakeup(
                db,
                owner_id=owner_id,
                source="periodic_sweep",
                trigger_type=_SWEEP_TRIGGER_TYPE,
                status=WAKEUP_STATUS_SUPPRESSED,
                reason="user_policy_disabled",
                conversation_id=_SWEEP_CONVERSATION_ID,
                wakeup_key=_SWEEP_WAKEUP_KEY,
                payload=wakeup_payload,
            )
            logger.info("Skipping operator sweep: operator mode disabled for owner %s", owner_id)
            return {"status": "skipped", "reason": "operator mode disabled"}

    message_id = f"operator-sweep-{uuid4()}"
    try:
        run_id = await invoke_oikos(
            owner_id,
            _build_sweep_message(),
            message_id,
            source="operator",
            surface_adapter=OperatorSurfaceAdapter(owner_id=owner_id, conversation_id=_SWEEP_CONVERSATION_ID),
            surface_payload=wakeup_payload,
        )
        with db_session() as db:
            append_wakeup(
                db,
                owner_id=owner_id,
                source="periodic_sweep",
                trigger_type=_SWEEP_TRIGGER_TYPE,
                status=WAKEUP_STATUS_ENQUEUED,
                conversation_id=_SWEEP_CONVERSATION_ID,
                wakeup_key=_SWEEP_WAKEUP_KEY,
                run_id=run_id,
                payload=wakeup_payload,
            )
    except Exception:
        with db_session() as db:
            append_wakeup(
                db,
                owner_id=owner_id,
                source="periodic_sweep",
                trigger_type=_SWEEP_TRIGGER_TYPE,
                status=WAKEUP_STATUS_FAILED,
                reason="invoke_failed",
                conversation_id=_SWEEP_CONVERSATION_ID,
                wakeup_key=_SWEEP_WAKEUP_KEY,
                payload=wakeup_payload,
            )
        logger.exception("Failed to invoke operator sweep wakeup for owner %s", owner_id)
        raise
    return {
        "status": "enqueued",
        "owner_id": owner_id,
        "trigger_type": _SWEEP_TRIGGER_TYPE,
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
