from __future__ import annotations

import logging

from sqlalchemy import text

from fastapi import FastAPI

from control_plane.db import Base
from control_plane.db import engine
from control_plane.routers import auth
from control_plane.routers import billing
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


app.include_router(health.router)
app.include_router(ui.router)
app.include_router(auth.router)
app.include_router(billing.router)
app.include_router(webhooks.router)
app.include_router(instances.router)
