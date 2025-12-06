"""Agent configuration endpoints."""

from fastapi import APIRouter
from fastapi import Depends
from pydantic import BaseModel

from zerg.config import get_settings
from zerg.dependencies.auth import get_current_user

router = APIRouter(
    prefix="/config",
    tags=["config"],
    dependencies=[Depends(get_current_user)],
)


class ContainerPolicyResponse(BaseModel):
    """Response model describing container execution policy."""

    enabled: bool
    default_image: str | None
    network_enabled: bool
    user_id: int | None
    memory_limit: str | None
    cpus: str | None
    timeout_secs: int
    seccomp_profile: str | None


@router.get("/container-policy", response_model=ContainerPolicyResponse)
def read_container_policy() -> ContainerPolicyResponse:
    """Return the container execution policy derived from environment settings."""

    settings = get_settings()
    return ContainerPolicyResponse(
        enabled=settings.container_tools_enabled,
        default_image=settings.container_default_image,
        network_enabled=settings.container_network_enabled,
        user_id=int(settings.container_user_id) if settings.container_user_id else None,
        memory_limit=settings.container_memory_limit,
        cpus=settings.container_cpus,
        timeout_secs=settings.container_timeout_secs,
        seccomp_profile=settings.container_seccomp_profile,
    )
