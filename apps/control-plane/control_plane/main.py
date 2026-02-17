from __future__ import annotations

import logging
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from sqlalchemy import text

from fastapi import FastAPI

from control_plane.db import Base
from control_plane.db import engine
from control_plane.db import SessionLocal
from control_plane.models import Deployment
from control_plane.models import Instance
from control_plane.routers import auth
from control_plane.routers import billing
from control_plane.routers import deployments
from control_plane.routers import health
from control_plane.routers import instances
from control_plane.routers import ui
from control_plane.routers import webhooks

logger = logging.getLogger(__name__)

app = FastAPI(title="Longhouse Control Plane", version="0.1.0")


@app.on_event("startup")
def _startup():
    Base.metadata.create_all(bind=engine)
    # Migrate: add email_verified column if missing, backfill existing users as verified
    with engine.connect() as conn:
        try:
            conn.execute(text("ALTER TABLE cp_users ADD COLUMN email_verified BOOLEAN DEFAULT 0"))
            # Backfill: existing users were already trusted — mark them verified
            conn.execute(text("UPDATE cp_users SET email_verified = 1"))
            conn.commit()
            logger.info("Added email_verified column to cp_users and backfilled existing users as verified")
        except Exception as exc:
            conn.rollback()
            # "duplicate column" is expected if migration already ran — anything else is worth logging
            if "duplicate" not in str(exc).lower():
                logger.warning(f"email_verified migration skipped: {exc}")

    # Crash recovery: clean up stale deploy states from interrupted deploys
    _recover_stale_deploys()


def _recover_stale_deploys():
    """Mark instances stuck in 'deploying' as failed and pause interrupted deployments."""
    db = SessionLocal()
    try:
        stale_cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)

        # Instances stuck in "deploying" for >5 minutes → mark failed
        stale_instances = (
            db.query(Instance)
            .filter(
                Instance.deploy_state == "deploying",
                Instance.deploy_started_at < stale_cutoff,
            )
            .all()
        )
        for inst in stale_instances:
            inst.deploy_state = "failed"
            inst.deploy_error = "Control plane restarted during deploy"
            logger.warning(f"Marked stale deploying instance {inst.subdomain} as failed")

        # Deployments stuck in "in_progress" → mark paused for manual resume
        stale_deploys = (
            db.query(Deployment)
            .filter(Deployment.status == "in_progress")
            .all()
        )
        for deploy in stale_deploys:
            deploy.status = "paused"
            logger.warning(f"Paused interrupted deployment {deploy.id}")

        if stale_instances or stale_deploys:
            db.commit()
            logger.info(
                f"Crash recovery: {len(stale_instances)} stale instances, "
                f"{len(stale_deploys)} paused deployments"
            )
    except Exception:
        db.rollback()
        logger.exception("Crash recovery failed")
    finally:
        db.close()


app.include_router(health.router)
app.include_router(ui.router)
app.include_router(auth.router)
app.include_router(billing.router)
app.include_router(webhooks.router)
app.include_router(instances.router)
app.include_router(deployments.router)
