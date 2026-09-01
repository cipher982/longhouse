"""Agents API — ingest health over the canonical catalog facts."""

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status

from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.client import CatalogUnavailable
from zerg.dependencies.agents_auth import owner_id_from_caller
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_caller
from zerg.models.device_token import DeviceToken
from zerg.services.catalogd_supervisor import get_catalogd_client
from zerg.services.session_views import IngestHealthResponse

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("/ingest-health", response_model=IngestHealthResponse)
async def get_ingest_health(
    _auth: DeviceToken | object | None = Depends(verify_agents_caller),
    _single: None = Depends(require_single_tenant),
) -> IngestHealthResponse:
    """Check ingest freshness -- detects if sessions have stopped shipping."""
    from zerg.services.ingest_health import compute_ingest_health_from_catalog_facts

    owner_id = owner_id_from_caller(_auth)
    catalogd = get_catalogd_client()
    if catalogd is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "catalog_unavailable", "message": "Catalog health is temporarily unavailable."},
        )
    try:
        facts = await catalogd.call("storage.health.v2", {"owner_id": str(owner_id)})
    except (CatalogUnavailable, CatalogRemoteError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "catalog_unavailable", "message": "Catalog health is temporarily unavailable."},
        ) from exc
    return IngestHealthResponse(**compute_ingest_health_from_catalog_facts(facts))
