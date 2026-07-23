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
from zerg.services.provider_capability_proof import ProviderCapabilityProofRecord
from zerg.services.provider_capability_proof import proof_record_from_mapping
from zerg.services.provider_capability_proof_store import ProviderCapabilityProofStore

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
    if payload.get("schema_version") != 1:
        raise ValueError("proof bundle schema_version must be 1")
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
    records = tuple(record for provider in sorted(managed_provider_names()) for record in store.records(provider))
    return {
        "schema_version": 1,
        "artifact_kind": _TRUSTED_BUNDLE_KIND,
        "records": [record.serialize() for record in records],
        "trusted_artifact_ids": [record.artifact_id for record in records],
    }
