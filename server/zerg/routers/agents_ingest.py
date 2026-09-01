"""Retired v1 transcript-ingest endpoint."""

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status

from zerg.auth.managed_session_tokens import ManagedSessionToken
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_caller
from zerg.models.device_token import DeviceToken
from zerg.services.session_views import IngestResponse

router = APIRouter(prefix="/agents", tags=["agents"])


@router.post("/ingest", response_model=IngestResponse)
async def ingest_session(
    _auth_token: DeviceToken | ManagedSessionToken | None = Depends(verify_agents_caller),
    _single: None = Depends(require_single_tenant),
) -> IngestResponse:
    """Require storage-v2 rather than retaining a second transcript writer."""
    raise HTTPException(
        status_code=status.HTTP_426_UPGRADE_REQUIRED,
        detail={
            "code": "storage_v2_required",
            "message": "This Runtime Host accepts transcript ingest only through storage-v2.",
        },
    )
