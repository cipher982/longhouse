"""The tenant's half of the tester funnel, read by the operating control plane.

`GET /api/internal/funnel` answers one JSON document (`longhouse.tenant-funnel.v1`)
of counts and dates: when the first machine connected, sessions shipped per
provider, and the search / steer / phone / web milestones and active days kept
by services/funnel_facts.py. Authenticated with this tenant's own
`X-Internal-Token` (a secret only the control plane can derive). A runtime that
was not launched for a consenting tester answers 404.
"""

from __future__ import annotations

import asyncio
import hmac

from fastapi import APIRouter
from fastapi import Header
from fastapi import HTTPException

from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.client import CatalogUnavailable
from zerg.config import get_settings
from zerg.services import funnel_facts
from zerg.services.catalogd_supervisor import get_catalogd_client

router = APIRouter(prefix="/internal", tags=["internal-funnel"])


def _require_internal_token(token: str | None) -> None:
    expected = str(get_settings().internal_api_secret or "")
    if not token or not expected or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="internal authentication required")


@router.get("/funnel", include_in_schema=False)
async def tenant_funnel(x_internal_token: str | None = Header(None, alias="X-Internal-Token")) -> dict:
    _require_internal_token(x_internal_token)
    if not funnel_facts.enabled():
        raise HTTPException(status_code=404, detail="funnel facts are not enabled on this runtime")
    catalogd = get_catalogd_client()
    if catalogd is None:
        raise HTTPException(status_code=503, detail="catalog unavailable")
    try:
        owner = await catalogd.call("auth.owner.get.v2", {})
        owner_id = owner.get("owner_id") if owner.get("found") is True else None
        catalog_facts = await catalogd.call("tenant.funnel.facts.read.v2", {"owner_id": str(owner_id)}) if owner_id is not None else {}
    except (CatalogUnavailable, CatalogRemoteError) as exc:
        raise HTTPException(status_code=503, detail="catalog unavailable") from exc
    store = funnel_facts.get_store()
    side_facts = await asyncio.to_thread(store.snapshot) if store is not None else {}
    return funnel_facts.build_document(catalog_facts, side_facts)
