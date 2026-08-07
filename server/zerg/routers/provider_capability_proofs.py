"""Authenticated publication and machine reads for trusted provider proofs."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC
from datetime import datetime
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
from zerg.services.provider_capability_projection import PROJECTION_VERSION
from zerg.services.provider_capability_projection import project_capabilities
from zerg.services.provider_capability_proof import PROOF_SCHEMA_VERSION
from zerg.services.provider_capability_proof import ProviderCapabilityProofRecord
from zerg.services.provider_capability_proof import proof_record_from_mapping
from zerg.services.provider_capability_proof import v3_provenance_gaps
from zerg.services.provider_capability_proof_store import ProofPublication
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
    return ProviderCapabilityProofStore(root, require_authenticated_publication=True)


def _legacy_proof_store() -> ProviderCapabilityProofStore:
    return ProviderCapabilityProofStore(_proof_store().root.parent / "historical-factory-v2")


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


def _bundle_digest(payload: dict[str, Any]) -> str:
    canonical = {key: value for key, value in payload.items() if key != "bundle_digest"}
    encoded = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _validated_records(
    payload: dict[str, Any],
) -> tuple[tuple[ProviderCapabilityProofRecord, ...], tuple[tuple[str, bytes], ...], ProofPublication]:
    if payload.get("schema_version") != PROOF_SCHEMA_VERSION:
        raise ValueError(f"proof bundle schema_version must be {PROOF_SCHEMA_VERSION}")
    if payload.get("artifact_kind") != _BUNDLE_KIND:
        raise ValueError(f"proof bundle artifact_kind must be {_BUNDLE_KIND}")
    if payload.get("bundle_digest") != _bundle_digest(payload):
        raise ValueError("proof bundle digest does not match canonical content")
    publication_payload = payload.get("publication")
    if not isinstance(publication_payload, dict):
        raise ValueError("proof bundle publication must be an object")
    worker_id = publication_payload.get("worker_id")
    worker_census_digest = publication_payload.get("worker_census_digest")
    auth_mechanism = publication_payload.get("auth_mechanism")
    published_at = publication_payload.get("published_at")
    if not all(isinstance(value, str) and value.strip() for value in (worker_id, worker_census_digest, auth_mechanism, published_at)):
        raise ValueError("proof bundle publication identity is incomplete")
    try:
        parsed_published_at = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("proof bundle publication timestamp is invalid") from exc
    if parsed_published_at.tzinfo is None:
        raise ValueError("proof bundle publication timestamp must include a timezone")
    parsed_published_at.astimezone(UTC)
    if auth_mechanism != "factory_token_v1":
        raise ValueError("proof bundle auth_mechanism is not admitted")
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
        gaps = v3_provenance_gaps(record)
        if gaps:
            raise ValueError(f"factory proof record has incomplete v3 provenance: {', '.join(gaps)}")
        if record.worker_id != worker_id or record.worker_census_digest != worker_census_digest:
            raise ValueError("factory proof record differs from publication worker identity")
        if record.auth_mechanism != auth_mechanism:
            raise ValueError("factory proof record differs from publication auth mechanism")
        records.append(record)

    invocations = {(record.invocation_id, record.run_reference) for record in records}
    if len(invocations) != 1:
        raise ValueError("proof bundle records must share one invocation and run_reference")
    raw_blobs = payload.get("blobs")
    if not isinstance(raw_blobs, list) or not raw_blobs:
        raise ValueError("proof bundle blobs must be a non-empty list")
    blobs: list[tuple[str, bytes]] = []
    for blob in raw_blobs:
        if not isinstance(blob, dict) or not isinstance(blob.get("digest"), str) or not isinstance(blob.get("content_base64"), str):
            raise ValueError("proof bundle blob is invalid")
        try:
            content = base64.b64decode(blob["content_base64"], validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("proof bundle blob content is not valid base64") from exc
        digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        if digest != blob["digest"]:
            raise ValueError("proof bundle blob digest does not match content")
        blobs.append((digest, content))
    declared = {digest for digest, _ in blobs}
    missing = set().union(*(set(record.referenced_content_digests()) for record in records)) - declared
    if missing:
        raise ValueError(f"proof bundle omits referenced content: {sorted(missing)}")
    publication = ProofPublication(
        worker_id=str(worker_id),
        worker_census_digest=str(worker_census_digest),
        auth_mechanism=str(auth_mechanism),
        published_at=str(published_at),
        bundle_digest=str(payload["bundle_digest"]),
    )
    return tuple(records), tuple(blobs), publication


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
    if payload.get("schema_version") == 2:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="historical schema-v2 proofs are read-only and cannot be published",
        )
    try:
        records, blobs, publication = _validated_records(payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    store = _proof_store()
    for digest, content in blobs:
        store.write_blob(content, expected_digest=digest)
    for record in records:
        store.write(record, publication=publication)
    integrity_by_provider = {provider: store.integrity_report(provider) for provider in {record.provider for record in records}}
    trusted_ids = [
        record.artifact_id for record in records if record.artifact_id in integrity_by_provider[record.provider].admissible_artifact_ids
    ]
    if len(trusted_ids) != len(records):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="published proof failed retained-content integrity validation",
        )
    return {
        "schema_version": 2,
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
    legacy_store = _legacy_proof_store()
    legacy_records, legacy_total = _bounded_records(legacy_store)
    if legacy_records:
        records = tuple(sorted((*records, *legacy_records), key=lambda item: (item.generated_at, item.artifact_id), reverse=True))[
            :_MAX_RECORDS
        ]
    total += legacy_total
    reports = {provider: store.integrity_report(provider) for provider in managed_provider_names()}
    integrity = {item.artifact_id: item for report in reports.values() for item in report.artifacts}
    legacy_ids = {record.artifact_id for record in legacy_records}
    trusted_ids = [
        record.artifact_id
        for record in records
        if record.artifact_id not in legacy_ids and integrity.get(record.artifact_id, None) and integrity[record.artifact_id].admissible
    ]
    return {
        "schema_version": 2,
        "artifact_kind": _TRUSTED_BUNDLE_KIND,
        "records": [
            {
                **record.serialize(),
                "store_integrity": (
                    {"admissible": False, "reason_codes": ["proof_schema_legacy", "historical_schema_v2"]}
                    if record.artifact_id in legacy_ids
                    else {
                        "admissible": integrity[record.artifact_id].admissible,
                        "reason_codes": list(integrity[record.artifact_id].reason_codes),
                    }
                ),
            }
            for record in records
        ],
        "trusted_artifact_ids": trusted_ids,
        "total_records": total,
        "truncated": total > len(records),
    }


def build_capability_projection_payload(
    *,
    expected_longhouse_sha: str | None = None,
    expected_epoch_digest: str | None = None,
) -> dict[str, Any]:
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
    integrity_reasons: dict[str, tuple[str, ...]] = {}
    for provider in sorted(managed_provider_names()):
        all_records.extend(store.records(provider))
        integrity_reasons.update(
            {item.artifact_id: item.reason_codes for item in store.integrity_report(provider).artifacts if not item.admissible}
        )
    legacy_store = _legacy_proof_store()
    for provider in sorted(managed_provider_names()):
        legacy_records = legacy_store.records(provider)
        all_records.extend(legacy_records)
        integrity_reasons.update({record.artifact_id: ("proof_schema_legacy", "historical_schema_v2") for record in legacy_records})
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
    projections = project_capabilities(
        assertions,
        all_records,
        integrity_reasons=integrity_reasons,
        expected_longhouse_sha=expected_longhouse_sha,
        expected_epoch_digest=expected_epoch_digest,
    )
    return {
        "schema_version": 1,
        "artifact_kind": "provider_capability_projection",
        "projection_version": PROJECTION_VERSION,
        "subject_fence": {
            "configured": expected_longhouse_sha is not None or expected_epoch_digest is not None,
            "longhouse_source_sha": expected_longhouse_sha,
            "accepted_epoch_digest": expected_epoch_digest,
        },
        "capabilities": [
            {
                "provider": p.provider,
                "capability": p.capability,
                "assertion_id": p.assertion_id,
                "variant": p.variant,
                "scenario_id": p.scenario_id,
                "declared": p.declared,
                "disposition": p.disposition,
                "proof_status": p.proof_status,
                "generated_at": p.generated_at,
                "evidence_class": p.evidence_class,
                "proof_artifact_id": p.proof_artifact_id,
                "latest_proof_artifact_id": p.latest_proof_artifact_id,
                "latest_outcome": p.latest_outcome,
                "admissibility_reasons": list(p.admissibility_reasons),
                "accepted_epoch_id": p.accepted_epoch_id,
                "accepted_epoch_digest": p.accepted_epoch_digest,
                "plan_digest": p.plan_digest,
                "compile_report_digest": p.compile_report_digest,
                "producer_id": p.producer_id,
                "worker_id": p.worker_id,
                "open_case_id": p.open_case_id,
                "baseline_outcome": p.baseline_outcome,
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
