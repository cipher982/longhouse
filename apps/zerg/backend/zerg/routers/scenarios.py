"""Scenario seeding endpoints for demo accounts."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from zerg.config import get_settings
from zerg.database import get_db
from zerg.dependencies.auth import get_current_user
from zerg.scenarios.seed import seed_scenario

router = APIRouter(prefix="/scenarios", tags=["scenarios"])


class ScenarioSeedRequest(BaseModel):
    name: str
    clean: bool = True


def _is_demo_user(user) -> bool:
    prefs = getattr(user, "prefs", None) or {}
    return bool(prefs.get("demo") or prefs.get("is_demo"))


@router.post("/seed")
async def seed_demo_scenario(
    request: ScenarioSeedRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Seed deterministic scenario data for demo users (or admins)."""
    settings = get_settings()
    is_demo = _is_demo_user(current_user)
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"

    if not (is_demo or is_admin):
        raise HTTPException(status_code=403, detail="Scenario seeding is restricted to demo users")

    if settings.environment and settings.environment.lower() == "production" and not is_demo:
        raise HTTPException(status_code=403, detail="Scenario seeding is restricted to demo users in production")

    return seed_scenario(db, request.name, owner_id=current_user.id, clean=request.clean)
