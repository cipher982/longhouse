"""Authenticated publication and machine reads for trusted provider proofs."""

from __future__ import annotations

import hmac
import json
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status

from zerg.config import get_settings
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.services.managed_provider_contracts import managed_provider_names
from zerg.services.provider_capability_projection import project_capabilities
from zerg.services.provider_capability_proof import ProviderCapabilityProofRecord
from zerg.services.provider_capability_proof import proof_record_from_mapping
from zerg.services.provider_capability_proof_store import ProviderCapabilityProofStore
from zerg.services.provider_capability_schema import load_capability_assertions

router = APIRouter(tags=["provider-capability-proofs"])

_BUNDLE_KIND = "provider_capability_proof_bundle"
_TRUSTED_BUNDLE_KIND = "trusted_provider_capability_proof_bundle"
_FACTORY_PRODUCER_CLASS = "release_factory"
_MAX_BODY_BYTES = 2 * 1024 * 1024
_MAX_RECORDS = 512


def _proof_store() -> ProviderCapabilityProofStore:
    root = get_settings().data_dir / "provider-capability-proofs" / "trusted-factory"
    return ProviderCapabilityProofStore(root)


def _verify_factory_token(request: Request) -> None:
    expected = get_settings().provider_capability_factory_token
    if not expected:
        # Publication is an optional hosted/factory surface, not part of the
        # ordinary public or self-hosted Runtime Host contract.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    presented = request.headers.get("X-Provider-Capability-Factory-Token")
    if not presented or not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Provider capability factory access denied")


async def _read_capped_json(request: Request) -> dict[str, Any]:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > _MAX_BODY_BYTES:
                raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Proof bundle is too large")
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Content-Length") from exc

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _MAX_BODY_BYTES:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Proof bundle is too large")
        chunks.append(chunk)
    try:
        payload = json.loads(b"".join(chunks))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Proof bundle must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Proof bundle must be an object")
    return payload


def _validated_records(payload: dict[str, Any]) -> tuple[ProviderCapabilityProofRecord, ...]:
    if payload.get("schema_version") != 2:
        raise ValueError("proof bundle schema_version must be 2")
    if payload.get("artifact_kind") != _BUNDLE_KIND:
        raise ValueError(f"proof bundle artifact_kind must be {_BUNDLE_KIND}")
    raw_records = payload.get("records")
    if not isinstance(raw_records, list) or not raw_records:
        raise ValueError("proof bundle records must be a non-empty list")
    if len(raw_records) > _MAX_RECORDS:
        raise ValueError(f"proof bundle may contain at most {_MAX_RECORDS} records")

    supported = managed_provider_names()
    records: list[ProviderCapabilityProofRecord] = []
    for raw_record in raw_records:
        if not isinstance(raw_record, dict):
            raise ValueError("proof bundle records must be objects")
        record = proof_record_from_mapping(raw_record)
        if record.provider not in supported:
            raise ValueError(f"unsupported managed provider: {record.provider}")
        if record.producer_class != _FACTORY_PRODUCER_CLASS:
            raise ValueError(f"proof producer_class must be {_FACTORY_PRODUCER_CLASS}")
        if not record.run_reference:
            raise ValueError("factory proof records must bind a run_reference")
        if not record.raw_reference_digests:
            raise ValueError("factory proof records must bind raw evidence digests")
        records.append(record)

    invocations = {(record.invocation_id, record.run_reference) for record in records}
    if len(invocations) != 1:
        raise ValueError("proof bundle records must share one invocation and run_reference")
    return tuple(records)


def _bounded_records(store: ProviderCapabilityProofStore) -> tuple[tuple[ProviderCapabilityProofRecord, ...], int]:
    """Return a provider-fair newest-first window that always fits the machine contract."""
    queues = {provider: list(reversed(store.records(provider))) for provider in sorted(managed_provider_names())}
    total = sum(len(records) for records in queues.values())
    selected: list[ProviderCapabilityProofRecord] = []
    while len(selected) < _MAX_RECORDS:
        advanced = False
        for provider in queues:
            if queues[provider] and len(selected) < _MAX_RECORDS:
                selected.append(queues[provider].pop(0))
                advanced = True
        if not advanced:
            break
    return tuple(selected), total


@router.post("/internal/provider-capability-proofs", status_code=status.HTTP_201_CREATED)
async def publish_provider_capability_proofs(
    request: Request,
    _factory: None = Depends(_verify_factory_token),
) -> dict[str, Any]:
    payload = await _read_capped_json(request)
    try:
        records = _validated_records(payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    store = _proof_store()
    for record in records:
        store.write(record)
    trusted_ids = list(dict.fromkeys(record.artifact_id for record in records))
    return {
        "schema_version": 1,
        "accepted": len(trusted_ids),
        "trusted_artifact_ids": trusted_ids,
    }


@router.get("/agents/provider-capability-proofs")
def list_provider_capability_proofs(
    _auth: object = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> dict[str, Any]:
    store = _proof_store()
    records, total = _bounded_records(store)
    return {
        "schema_version": 1,
        "artifact_kind": _TRUSTED_BUNDLE_KIND,
        "records": [record.serialize() for record in records],
        "trusted_artifact_ids": [record.artifact_id for record in records],
        "total_records": total,
        "truncated": total > len(records),
    }


def build_capability_projection_payload() -> dict[str, Any]:
    """Capability projection from the contract, proof status attached
    separately (docs/specs/provider-factory-coherence.md, Phase 5). Every
    declared capability assertion for every managed provider gets exactly
    one row, whether or not it has ever been proven -- the schema is the
    source of truth for what should exist.

    Shared by both the device-token machine surface
    (GET /agents/provider-capabilities) and the cookie-authenticated admin
    surface (GET /admin/provider-capabilities) so there is exactly one
    projection code path, not two that can drift.
    """
    store = _proof_store()
    all_records: list[ProviderCapabilityProofRecord] = []
    for provider in sorted(managed_provider_names()):
        all_records.extend(store.records(provider))
    try:
        assertions = load_capability_assertions()
    except SystemExit as exc:
        # provider_capability_schema.py's schema loader predates this endpoint
        # and raises SystemExit -- a BaseException, not caught by normal
        # exception handling -- for a malformed schema. That is the right
        # behavior for the Makefile-driven CLI callers it was written for
        # (abort the script, print the message), and wrong here: this
        # function now also sits behind a live Runtime Host request path
        # (review 2026-07-29), where an uncaught SystemExit can take down
        # the worker instead of returning a 5xx. Translate at this one
        # narrow boundary rather than changing the shared loader's
        # CLI-facing contract for every other caller.
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
    projections = project_capabilities(assertions, all_records)
    return {
        "schema_version": 1,
        "artifact_kind": "provider_capability_projection",
        "capabilities": [
            {
                "provider": p.provider,
                "capability": p.capability,
                "assertion_id": p.assertion_id,
                "scenario_id": p.scenario_id,
                "declared": p.declared,
                "proof_status": p.proof_status,
                "generated_at": p.generated_at,
                "evidence_class": p.evidence_class,
            }
            for p in projections
        ],
    }


@router.get("/agents/provider-capabilities")
def list_provider_capabilities(
    _auth: object = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> dict[str, Any]:
    """Capability projection from the contract, proof status attached
    separately (docs/specs/provider-factory-coherence.md, Phase 5). Every
    declared capability assertion for every managed provider gets exactly
    one row, whether or not it has ever been proven -- the schema is the
    source of truth for what should exist."""
    return build_capability_projection_payload()
