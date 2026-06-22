"""Machine-facing provider capability views."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query

from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.schemas.provider_action_coverage import ProviderActionCoverageResponse
from zerg.services.managed_provider_contracts import managed_provider_names
from zerg.services.provider_action_coverage import ActionCoverageState
from zerg.services.provider_action_coverage import derive_provider_action_coverage
from zerg.services.provider_action_coverage import serialize_provider_action_coverage

router = APIRouter(prefix="/agents/providers", tags=["agents"])


@router.get("/action-coverage", response_model=ProviderActionCoverageResponse)
def list_provider_action_coverage(
    provider: str | None = Query(None, description="Filter to one managed provider"),
    _auth: object = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> ProviderActionCoverageResponse:
    providers = _requested_providers(provider)
    rows = {}
    for provider_name in providers:
        actions = serialize_provider_action_coverage(derive_provider_action_coverage(provider_name))
        rows[provider_name] = {
            "provider": provider_name,
            "actions": actions,
            "summary": {
                state.value: sum(1 for item in actions.values() if item.get("state") == state.value)
                for state in ActionCoverageState  # noqa: E501
            },
        }

    return ProviderActionCoverageResponse(
        schema_version=1,
        source="zerg.services.provider_action_coverage",
        states=[state.value for state in ActionCoverageState],
        providers=rows,
    )


def _requested_providers(provider: str | None) -> list[str]:
    supported = sorted(managed_provider_names())
    if provider is None:
        return supported

    normalized = provider.strip().lower()
    if normalized not in supported:
        raise HTTPException(status_code=404, detail=f"Unknown managed provider: {provider}")
    return [normalized]
