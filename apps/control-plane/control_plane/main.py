from __future__ import annotations

from fastapi import FastAPI

from control_plane.db import Base
from control_plane.db import engine
from control_plane.routers import auth
from control_plane.routers import billing
from control_plane.routers import health
from control_plane.routers import instances
from control_plane.routers import ui
from control_plane.routers import webhooks

app = FastAPI(title="Longhouse Control Plane", version="0.1.0")


@app.on_event("startup")
def _startup():
    Base.metadata.create_all(bind=engine)


app.include_router(health.router)
app.include_router(ui.router)
app.include_router(auth.router)
app.include_router(billing.router)
app.include_router(webhooks.router)
app.include_router(instances.router)
