from fastapi import APIRouter
from fastapi import Depends

from zerg.config import get_settings
from zerg.dependencies.auth import get_current_user
from zerg.models_config import get_all_models_for_api

router = APIRouter(
    tags=["models"],
)


@router.get("/")
async def get_models(current_user=Depends(get_current_user)):
    """Return available models filtered for non-admins if allowlist set."""
    role = getattr(current_user, "role", "USER")
    if role == "ADMIN":
        return get_all_models_for_api()

    settings = get_settings()
    raw = settings.allowed_models_non_admin or ""
    allow = {m.strip() for m in raw.split(",") if m.strip()}
    if not allow:
        return get_all_models_for_api()

    # Filter list by id against allowlist
    all_models = get_all_models_for_api()
    return [m for m in all_models if m.get("id") in allow]
