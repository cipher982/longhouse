"""Diagnostic-only visibility into reducer-backed session state."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from pydantic import ConfigDict

import zerg.database as database_module
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.services.catalog_facts import decode_catalog_datetime
from zerg.services.catalog_read_gateway import CatalogReadError
from zerg.services.catalog_read_gateway import active_owner_id
from zerg.services.catalog_read_gateway import shadow_session_state_snapshot
from zerg.services.live_catalog_timeline import project_catalog_session_facts
from zerg.services.managed_provider_contracts import contract_for_provider
from zerg.services.session_state_diagnostics import SessionStateComparison
from zerg.services.session_state_diagnostics import compare_session_state_axes
from zerg.services.session_state_facts_projector import ShadowSessionStateProjection
from zerg.services.session_state_facts_projector import project_shadow_session_state_facts
from zerg.utils.time import UTCBaseModel

router = APIRouter(prefix="/agents/sessions", tags=["agents"])


class SessionStateDiagnosticsResponse(UTCBaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: UUID
    provider: str | None
    catalog_commit_seq: int
    observed_at: datetime
    head_count: int
    served_path: Literal["legacy_session_state"] = "legacy_session_state"
    authorization_path: Literal["legacy_capabilities"] = "legacy_capabilities"
    cutover_active: Literal[False] = False
    shadow: ShadowSessionStateProjection
    comparison: SessionStateComparison


def _owner_id(auth: object | None) -> int:
    value = getattr(auth, "owner_id", None)
    if value is None:
        value = active_owner_id()
    if value is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="An owner-scoped token is required",
        )
    return int(value)


def _supported_operations(provider: str | None) -> set[str]:
    contract = contract_for_provider(provider)
    if contract is None:
        return set()
    operations = {
        operation for operation in ("send_input", "interrupt", "terminate", "tail_output") if bool(getattr(contract, operation, False))
    }
    if contract.can_resume or contract.reattach:
        operations.add("resume")
    return operations


@router.get("/{session_id}/state-diagnostics", response_model=SessionStateDiagnosticsResponse)
def get_session_state_diagnostics(
    session_id: UUID,
    auth: object | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> SessionStateDiagnosticsResponse:
    """Compare canonical reducer axes without changing served or authorized state."""

    if not database_module.live_catalog_enabled():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "canonical_catalog_required",
                "message": "Session state diagnostics require the canonical live catalog.",
            },
        )
    try:
        snapshot = shadow_session_state_snapshot(str(session_id), owner_id=_owner_id(auth))
    except CatalogReadError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    if snapshot.get("found") is not True:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    if snapshot.get("heads_truncated") is True:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "shadow_fact_head_limit_exceeded",
                "message": "Reducer state exceeds the diagnostic comparison bound.",
            },
        )

    try:
        commit_seq = int(snapshot["commit_seq"])
        observed_at = decode_catalog_datetime(snapshot["observed_at"])
        legacy_facts = snapshot["legacy_facts"]
        if not isinstance(observed_at, datetime) or not isinstance(legacy_facts, dict):
            raise ValueError("catalog diagnostic snapshot is incomplete")
        legacy = project_catalog_session_facts(legacy_facts, observed_at=observed_at).session_state
        heads = snapshot["heads"]
        if not isinstance(heads, list):
            raise ValueError("catalog diagnostic fact heads are incomplete")
        shadow = project_shadow_session_state_facts(
            session_id=str(session_id),
            commit_seq=commit_seq,
            heads=heads,
            supported_operations=_supported_operations(snapshot.get("provider")),
            now=observed_at,
        )
        comparison = compare_session_state_axes(
            legacy=legacy,
            shadow=shadow,
            legacy_commit_seq=commit_seq,
            shadow_commit_seq=shadow.commit_seq,
        )
        return SessionStateDiagnosticsResponse(
            session_id=session_id,
            provider=snapshot.get("provider"),
            catalog_commit_seq=commit_seq,
            observed_at=observed_at,
            head_count=int(snapshot.get("head_count") or 0),
            shadow=shadow,
            comparison=comparison,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "invalid_catalog_snapshot",
                "message": "Catalog state diagnostics returned an invalid snapshot.",
            },
        ) from exc


__all__ = ["SessionStateDiagnosticsResponse", "router"]
