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
from pydantic import Field

import zerg.database as database_module
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.services.catalog_facts import decode_catalog_datetime
from zerg.services.catalog_read_gateway import CatalogReadError
from zerg.services.catalog_read_gateway import active_owner_id
from zerg.services.catalog_read_gateway import shadow_session_state_health
from zerg.services.catalog_read_gateway import shadow_session_state_snapshot
from zerg.services.live_catalog_timeline import canonical_session_detail_enabled
from zerg.services.live_catalog_timeline import project_catalog_session_facts
from zerg.services.managed_provider_contracts import contract_for_provider
from zerg.services.session_state_diagnostics import SessionStateComparison
from zerg.services.session_state_diagnostics import compare_session_state_axes
from zerg.services.session_state_facts_projector import SHADOW_SUPPORTED_FAMILIES
from zerg.services.session_state_facts_projector import SHADOW_UNSUPPORTED_FAMILIES
from zerg.services.session_state_facts_projector import ShadowSessionStateProjection
from zerg.services.session_state_facts_projector import project_shadow_session_state_facts
from zerg.utils.time import UTCBaseModel

router = APIRouter(prefix="/agents/sessions", tags=["agents"])
health_router = APIRouter(prefix="/agents/session-state", tags=["agents"])


def _served_path() -> Literal["legacy_session_state", "canonical_session_detail"]:
    return "canonical_session_detail" if canonical_session_detail_enabled() else "legacy_session_state"


class SessionStateDiagnosticsResponse(UTCBaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: UUID
    provider: str | None
    catalog_commit_seq: int
    observed_at: datetime
    head_count: int
    served_path: Literal["legacy_session_state", "canonical_session_detail"] = Field(default_factory=_served_path)
    authorization_path: Literal["legacy_capabilities"] = "legacy_capabilities"
    cutover_active: bool = Field(default_factory=canonical_session_detail_enabled)
    shadow: ShadowSessionStateProjection
    comparison: SessionStateComparison


class SessionStateReducerStorageResponse(UTCBaseModel):
    head_counts: dict[str, int]
    head_capacity_per_family: int
    receipt_count: int
    conflict_count: int
    parity_delta_count: int


class SessionStateReducerBatchResponse(UTCBaseModel):
    sample_size: int
    sample_limit: int
    window_seconds: int
    truncated: bool
    newest_received_at: datetime | None
    oldest_received_at: datetime | None
    malformed_results: int
    reducer_status_counts: dict[str, int]
    parity_status_counts: dict[str, int]
    identity_binding: dict[str, int] = Field(
        default_factory=lambda: {
            "bound": 0,
            "matched": 0,
            "unbound": 0,
            "mismatched": 0,
        }
    )
    changed_heads: int
    duplicates: int
    stale: int
    conflicts: int
    parity_deltas: int
    parity_missing_heads: int


class SessionStateReducerHealthResponse(UTCBaseModel):
    status: Literal["disabled", "no_samples", "not_reducing", "not_comparable", "observing", "degraded"]
    catalog_commit_seq: int
    observed_at: datetime
    ingest_enabled: bool
    parity_enabled: bool
    served_path: Literal["legacy_session_state", "canonical_session_detail"] = Field(default_factory=_served_path)
    authorization_path: Literal["legacy_capabilities"] = "legacy_capabilities"
    cutover_active: bool = Field(default_factory=canonical_session_detail_enabled)
    projected_families: tuple[str, ...] = SHADOW_SUPPORTED_FAMILIES
    unsupported_families: tuple[str, ...] = SHADOW_UNSUPPORTED_FAMILIES
    storage: SessionStateReducerStorageResponse
    recent_batches: SessionStateReducerBatchResponse


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
    if contract.can_resume:
        operations.add("resume")
    return operations


@health_router.get("/health", response_model=SessionStateReducerHealthResponse)
def get_session_state_reducer_health(
    auth: object | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> SessionStateReducerHealthResponse:
    """Expose bounded reducer health without claiming cutover readiness."""

    if not database_module.live_catalog_enabled():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "canonical_catalog_required",
                "message": "Session state reducer health requires the canonical live catalog.",
            },
        )
    try:
        snapshot = shadow_session_state_health(owner_id=_owner_id(auth))
    except CatalogReadError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    if snapshot.get("found") is not True:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Owner not found")
    try:
        recent = SessionStateReducerBatchResponse.model_validate(snapshot["recent_batches"])
        failed_batches = int(recent.reducer_status_counts.get("failed") or 0) + int(recent.parity_status_counts.get("failed") or 0)
        if not snapshot.get("ingest_enabled"):
            health_status = "disabled"
        elif recent.sample_size == 0:
            health_status = "no_samples"
        elif failed_batches or recent.malformed_results:
            health_status = "degraded"
        elif not recent.reducer_status_counts.get("applied"):
            health_status = "not_reducing"
        elif not snapshot.get("parity_enabled") or not recent.parity_status_counts.get("compared"):
            health_status = "not_comparable"
        else:
            health_status = "observing"
        return SessionStateReducerHealthResponse(
            status=health_status,
            catalog_commit_seq=int(snapshot["commit_seq"]),
            observed_at=decode_catalog_datetime(snapshot["observed_at"]),
            ingest_enabled=bool(snapshot["ingest_enabled"]),
            parity_enabled=bool(snapshot["parity_enabled"]),
            storage=SessionStateReducerStorageResponse.model_validate(snapshot["storage"]),
            recent_batches=recent,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "invalid_catalog_snapshot",
                "message": "Catalog reducer health returned an invalid snapshot.",
            },
        ) from exc


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
            catalog_facts=legacy_facts,
            heads=heads,
            supported_operations=_supported_operations(snapshot.get("provider")),
            now=observed_at,
        )
        comparison = compare_session_state_axes(
            legacy=legacy,
            shadow=shadow,
            legacy_commit_seq=commit_seq,
            shadow_commit_seq=shadow.commit_seq,
            catalog_facts=legacy_facts,
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


__all__ = [
    "SessionStateDiagnosticsResponse",
    "SessionStateReducerHealthResponse",
    "health_router",
    "router",
]
