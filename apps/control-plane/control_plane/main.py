from __future__ import annotations

import logging
from datetime import datetime
from datetime import timezone

import boto3
import httpx
from sqlalchemy import text

from fastapi import FastAPI
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from control_plane.config import settings
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

app.state.limiter = auth.limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)


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

    # Credential health checks — log errors immediately so bad keys don't ship silently
    _check_stripe_credentials()
    _check_ses_credentials()


def _check_stripe_credentials() -> None:
    """Validate Stripe key at startup. Logs an error if key is invalid — does not crash startup."""
    if not settings.stripe_secret_key:
        return
    try:
        resp = httpx.get(
            "https://api.stripe.com/v1/balance",
            headers={"Authorization": f"Bearer {settings.stripe_secret_key}"},
            timeout=5.0,
        )
        if resp.status_code == 200:
            logger.info("Stripe key valid")
        else:
            logger.error(
                "Stripe key invalid or expired (HTTP %s) — billing will fail",
                resp.status_code,
            )
    except Exception as exc:
        logger.error("Stripe credential check failed: %s", exc)


def _check_ses_credentials() -> None:
    """Validate SES credentials at startup. Logs an error if invalid — does not crash startup."""
    if not settings.instance_aws_ses_access_key_id or not settings.instance_aws_ses_secret_access_key:
        return
    try:
        from botocore.config import Config as _BotocoreConfig

        client = boto3.client(
            "ses",
            region_name=settings.instance_aws_ses_region or "us-east-1",
            aws_access_key_id=settings.instance_aws_ses_access_key_id,
            aws_secret_access_key=settings.instance_aws_ses_secret_access_key,
            config=_BotocoreConfig(connect_timeout=5, read_timeout=5, retries={"max_attempts": 1}),
        )
        quota = client.get_send_quota()
        if quota.get("Max24HourSend", 0) > 0:
            logger.info("SES credentials valid")
        else:
            logger.warning("SES: max 24h send quota is 0 — account may still be in sandbox")
    except Exception as exc:
        logger.error("SES credential check failed — email will fail: %s", exc)


def _recover_stale_deploys():
    """Mark instances stuck in 'deploying' as failed and pause interrupted deployments."""
    db = SessionLocal()
    try:
        # Deployments stuck in "in_progress" → mark paused (do this FIRST)
        stale_deploys = (
            db.query(Deployment)
            .filter(Deployment.status == "in_progress")
            .all()
        )
        stale_deploy_ids = set()
        for deploy in stale_deploys:
            deploy.status = "paused"
            stale_deploy_ids.add(deploy.id)
            logger.warning(f"Paused interrupted deployment {deploy.id}")

        # ALL instances in "deploying" state → mark failed (no age check — if the
        # control plane restarted, no worker is advancing them regardless of age)
        stale_instances = (
            db.query(Instance)
            .filter(Instance.deploy_state == "deploying")
            .all()
        )
        for inst in stale_instances:
            inst.deploy_state = "failed"
            inst.deploy_error = "Control plane restarted during deploy"
            logger.warning(f"Marked stale deploying instance {inst.subdomain} as failed")

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
