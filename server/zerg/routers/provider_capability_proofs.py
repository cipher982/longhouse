"""Authenticated publication and machine reads for trusted provider proofs."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import time
from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import Response
from fastapi import status

from zerg.auth.caller import Caller
from zerg.auth.managed_session_tokens import ManagedSessionToken
from zerg.config import get_settings
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_caller
from zerg.services.managed_provider_contracts import managed_provider_names
from zerg.services.product_assurance_proof_archive import ProductAssuranceProofArchive
from zerg.services.provider_assurance_plan_projection import validate_plan_projection
from zerg.services.provider_capability_blob_resolver import FactoryBlobReference
from zerg.services.provider_capability_blob_resolver import ProviderCapabilityBlobMissing
from zerg.services.provider_capability_blob_resolver import ProviderCapabilityBlobResolver
from zerg.services.provider_capability_blob_resolver import ProviderCapabilityBlobResolverConfigurationError
from zerg.services.provider_capability_blob_resolver import ProviderCapabilityBlobTampered
from zerg.services.provider_capability_blob_resolver import ProviderCapabilityBlobUnavailable
from zerg.services.provider_capability_blob_resolver import resolver_from_settings
from zerg.services.provider_capability_cell_verdicts import REVOKING_CONSECUTIVE_FAILURES
from zerg.services.provider_capability_cell_verdicts import VERDICT_BUNDLE_KIND
from zerg.services.provider_capability_cell_verdicts import VERDICT_SCHEMA_VERSION
from zerg.services.provider_capability_cell_verdicts import CellVerdictStore
from zerg.services.provider_capability_cell_verdicts import verdict_from_mapping
from zerg.services.provider_capability_projection import PROJECTION_VERSION
from zerg.services.provider_capability_projection import project_capabilities
from zerg.services.provider_capability_proof import PROOF_SCHEMA_VERSION
from zerg.services.provider_capability_proof import ProviderCapabilityProofRecord
from zerg.services.provider_capability_proof import proof_record_from_mapping
from zerg.services.provider_capability_proof import v3_provenance_gaps
from zerg.services.provider_capability_proof_store import ProofPublication
from zerg.services.provider_capability_proof_store import ProviderCapabilityProofStore
from zerg.services.provider_capability_schema import load_capability_assertions
from zerg.services.provider_capability_schema import load_chip_edge_assertions
from zerg.services.provider_chip_edges import UNPROVEN
from zerg.services.provider_chip_edges import rollup_state

router = APIRouter(tags=["provider-capability-proofs"])

_BUNDLE_KIND = "provider_capability_proof_bundle"
_TRUSTED_BUNDLE_KIND = "trusted_provider_capability_proof_bundle"
_FACTORY_PRODUCER_CLASS = "release_factory"
_MAX_BODY_BYTES = 2 * 1024 * 1024
_MAX_V4_BUNDLE_BYTES = 1 * 1024 * 1024
_MAX_RECORDS = 512
_MAX_V4_REFS = 512
_MAX_V4_TOTAL_BYTES = 50 * 1024 * 1024


def _proof_store() -> ProviderCapabilityProofStore:
    root = get_settings().data_dir / "provider-capability-proofs" / "trusted-factory"
    return ProviderCapabilityProofStore(root, require_authenticated_publication=True)


def _legacy_proof_store() -> ProviderCapabilityProofStore:
    return ProviderCapabilityProofStore(_proof_store().root.parent / "historical-factory-v2")


def _cell_verdict_store() -> CellVerdictStore:
    return CellVerdictStore(_proof_store().root.parent / "cell-verdicts")


def _blob_resolver() -> ProviderCapabilityBlobResolver | None:
    try:
        return resolver_from_settings(get_settings())
    except ProviderCapabilityBlobResolverConfigurationError:
        return None


def _product_assurance_archive() -> ProductAssuranceProofArchive:
    return ProductAssuranceProofArchive(_proof_store().root.parent / "trusted-product-assurance")


def _verify_factory_token(request: Request) -> None:
    expected = get_settings().provider_capability_factory_token
    if not expected:
        # Publication is an optional hosted/factory surface, not part of the
        # ordinary public or self-hosted Runtime Host contract.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    presented = request.headers.get("X-Provider-Capability-Factory-Token")
    if not presented or not hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Provider capability factory access denied")


def _refuse_evidence_on_public_demo() -> None:
    # The public demo runs auth-disabled, so the agents dependency admits any
    # caller there. It holds mirrored factory proofs only to certify landing
    # chips (`/public/provider-certification`); it never serves the records or
    # the evidence bytes it verified at publication.
    if getattr(get_settings(), "demo_mode", False):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


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
    if payload.get("schema_version") == 4 and total > _MAX_V4_BUNDLE_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="v4 proof metadata is too large")
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
    content_by_digest = dict(blobs)
    for record in records:
        validate_plan_projection(record.canonical_payload(), content_by_digest)
    publication = ProofPublication(
        worker_id=str(worker_id),
        worker_census_digest=str(worker_census_digest),
        auth_mechanism=str(auth_mechanism),
        published_at=str(published_at),
        bundle_digest=str(payload["bundle_digest"]),
    )
    return tuple(records), tuple(blobs), publication


def _reject_v4_inline_content(value: Any) -> None:
    if isinstance(value, dict):
        if {"content_base64", "content", "bytes_base64", "data_base64"} & value.keys():
            raise ValueError("v4 proof bundles cannot carry inline content")
        for child in value.values():
            _reject_v4_inline_content(child)
    elif isinstance(value, list):
        for child in value:
            _reject_v4_inline_content(child)


def _validated_v4_bundle(
    payload: dict[str, Any],
) -> tuple[tuple[ProviderCapabilityProofRecord, ...], tuple[FactoryBlobReference, ...], ProofPublication]:
    expected_keys = {"schema_version", "artifact_kind", "records", "blobs", "publication", "bundle_digest"}
    if set(payload) != expected_keys:
        raise ValueError("v4 proof bundle has an unexpected schema")
    _reject_v4_inline_content(payload)
    if payload.get("schema_version") != 4:
        raise ValueError("v4 proof bundle schema_version must be 4")
    if payload.get("artifact_kind") != _BUNDLE_KIND:
        raise ValueError(f"proof bundle artifact_kind must be {_BUNDLE_KIND}")
    if payload.get("bundle_digest") != _bundle_digest(payload):
        raise ValueError("proof bundle digest does not match canonical content")
    publication_payload = payload.get("publication")
    if not isinstance(publication_payload, dict) or set(publication_payload) != {
        "worker_id",
        "worker_census_digest",
        "auth_mechanism",
        "published_at",
    }:
        raise ValueError("v4 proof publication has an unexpected schema")
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
    if auth_mechanism != "factory_token_v1":
        raise ValueError("proof bundle auth_mechanism is not admitted")

    raw_records = payload.get("records")
    if not isinstance(raw_records, list) or len(raw_records) != 1:
        raise ValueError("v4 proof publication must contain exactly one record")
    records: list[ProviderCapabilityProofRecord] = []
    for raw_record in raw_records:
        if not isinstance(raw_record, dict):
            raise ValueError("proof bundle records must be objects")
        if (raw_record.get("subject_kind") or "provider_release") != "provider_release":
            raise ValueError("v4 proof bundles admit provider_release subjects only")
        record = proof_record_from_mapping(raw_record)
        if raw_record != record.serialize():
            raise ValueError("v4 proof records must use their canonical serialized form")
        if record.provider not in managed_provider_names():
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
    if not isinstance(raw_blobs, list) or not raw_blobs or len(raw_blobs) > _MAX_V4_REFS:
        raise ValueError("v4 proof bundle blobs must contain between 1 and 512 references")
    refs: list[FactoryBlobReference] = []
    seen: set[str] = set()
    for raw_blob in raw_blobs:
        ref = FactoryBlobReference.from_mapping(raw_blob) if isinstance(raw_blob, dict) else None
        if ref is None:
            raise ValueError("v4 proof bundle blob reference is invalid")
        if ref.digest in seen:
            raise ValueError("v4 proof bundle references the same blob twice")
        seen.add(ref.digest)
        refs.append(ref)
    if sum(ref.byte_length for ref in refs) > _MAX_V4_TOTAL_BYTES:
        raise ValueError("v4 proof bundle references exceed the 50 MiB total byte cap")
    referenced = set().union(*(set(record.referenced_content_digests()) for record in records))
    declared = {ref.digest for ref in refs}
    missing = referenced - declared
    unreferenced = declared - referenced
    if missing:
        raise ValueError(f"v4 proof bundle omits referenced content: {sorted(missing)}")
    if unreferenced:
        raise ValueError(f"v4 proof bundle contains unreferenced content: {sorted(unreferenced)}")
    publication = ProofPublication(
        worker_id=str(worker_id),
        worker_census_digest=str(worker_census_digest),
        auth_mechanism=str(auth_mechanism),
        published_at=str(published_at),
        bundle_digest=str(payload["bundle_digest"]),
        bundle_schema_version=4,
    )
    return tuple(records), tuple(refs), publication


def _reference_verification_payloads(
    resolver: ProviderCapabilityBlobResolver,
    refs: tuple[FactoryBlobReference, ...],
    records: tuple[ProviderCapabilityProofRecord, ...],
) -> tuple[tuple[dict[str, Any], ...], dict[str, bytes]]:
    projection_digests = {
        str(record.provenance_extension["plan_projection_digest"])
        for record in records
        if isinstance(record.provenance_extension, dict) and record.provenance_extension.get("plan_projection_digest")
    }
    verification: list[dict[str, Any]] = []
    projection_content: dict[str, bytes] = {}
    deadline = time.monotonic() + 90
    for ref in refs:
        verified = resolver.resolve(ref, collect=ref.digest in projection_digests, deadline=deadline)
        verification.append(dict(verified.verification))
        if verified.content is not None:
            projection_content[ref.digest] = verified.content
    for record in records:
        validate_plan_projection(record.canonical_payload(), projection_content)
    return tuple(verification), projection_content


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


def _accept_cell_verdicts(payload: dict[str, Any]) -> dict[str, Any]:
    """Store the newest failed-execution verdict per factory cell.

    Verdicts carry no evidence and cannot certify anything; they only let the
    chart stop trusting an older pass (`project_capabilities`). A `pass` is a
    proof, so it is refused here.
    """

    if set(payload) != {"schema_version", "artifact_kind", "verdicts"} or payload["schema_version"] != VERDICT_SCHEMA_VERSION:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"verdict bundle must be exactly schema_version {VERDICT_SCHEMA_VERSION}, artifact_kind, verdicts",
        )
    raw = payload["verdicts"]
    if not isinstance(raw, list) or not raw or len(raw) > _MAX_RECORDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"verdicts must be a list of 1 to {_MAX_RECORDS} objects",
        )
    try:
        verdicts = [verdict_from_mapping(item) for item in raw]
        applied = _cell_verdict_store().publish(verdicts)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return {"schema_version": VERDICT_SCHEMA_VERSION, "accepted": len(verdicts), "applied": applied}


@router.post("/internal/provider-capability-proofs", status_code=status.HTTP_201_CREATED)
async def publish_provider_capability_proofs(
    request: Request,
    _factory: None = Depends(_verify_factory_token),
) -> dict[str, Any]:
    payload = await _read_capped_json(request)
    if payload.get("artifact_kind") == VERDICT_BUNDLE_KIND:
        return _accept_cell_verdicts(payload)
    if payload.get("schema_version") == 2:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="historical schema-v2 proofs are read-only and cannot be published",
        )
    raw_records = payload.get("records")
    if isinstance(raw_records, list) and raw_records:
        subject_kinds = {record.get("subject_kind", "provider_release") for record in raw_records if isinstance(record, dict)}
        if "longhouse_product" in subject_kinds:
            if payload.get("schema_version") == 4:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="v4 proof bundles admit provider_release subjects only",
                )
            if subject_kinds != {"longhouse_product"} or len(subject_kinds) != 1:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="proof bundle may not mix provider and Longhouse product subjects",
                )
            try:
                trusted_ids = _product_assurance_archive().accept(payload)
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
            return {
                "schema_version": 2,
                "accepted": len(trusted_ids),
                "trusted_artifact_ids": trusted_ids,
            }
    if payload.get("schema_version") == 4:
        try:
            records, refs, publication = _validated_v4_bundle(payload)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        resolver = _blob_resolver()
        if resolver is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "provider_capability_blob_store_unavailable", "message": "v4 evidence resolver is not configured"},
            )
        try:
            verification, _projection_content = await asyncio.to_thread(_reference_verification_payloads, resolver, refs, records)
        except ProviderCapabilityBlobMissing as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "provider_capability_blob_missing", "message": str(exc)},
            ) from exc
        except ProviderCapabilityBlobTampered as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "provider_capability_blob_tampered", "message": str(exc)},
            ) from exc
        except ProviderCapabilityBlobUnavailable as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "provider_capability_blob_store_unavailable", "message": str(exc)},
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        store = _proof_store()
        ref_payloads = tuple(dict(ref) for ref in payload["blobs"])
        try:
            for record in records:
                metadata = store.build_reference_metadata(
                    record,
                    bundle_digest=publication.bundle_digest,
                    publication=publication,
                    refs=ref_payloads,
                    verification=verification,
                )
                store.write_reference_metadata(record, metadata)
                store.write(record, publication=publication, rebuild_index=False)
                integrity = store.integrity_report(record.provider, records=(record,), available=frozenset())
                if record.artifact_id not in integrity.admissible_artifact_ids:
                    raise ValueError("stored reference proof did not retain its authenticated integrity")
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        return {
            "schema_version": 2,
            "proof_bundle_schema_version": 4,
            "accepted": len(records),
            "trusted_artifact_ids": [record.artifact_id for record in records],
        }
    try:
        records, blobs, publication = _validated_records(payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    store = _proof_store()
    for digest, content in blobs:
        store.write_blob(content, expected_digest=digest)
    for record in records:
        # The append-only record and publication event are the serving
        # authority.  Do not synchronously rebuild the full diagnostic index
        # on every factory receipt; that O(history) work blocks this async
        # application and makes health/API traffic stall during a proof batch.
        store.write(record, publication=publication, rebuild_index=False)
    records_by_provider: dict[str, tuple[ProviderCapabilityProofRecord, ...]] = {}
    for record in records:
        records_by_provider[record.provider] = (*records_by_provider.get(record.provider, ()), record)
    referenced_digests = set().union(*(set(record.referenced_content_digests()) for record in records))
    available = frozenset(digest for digest in referenced_digests if store.has_blob(digest))
    integrity_by_provider = {
        provider: store.integrity_report(provider, records=provider_records, available=available)
        for provider, provider_records in records_by_provider.items()
    }
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


@router.get("/agents/provider-capability-proofs", dependencies=[Depends(_refuse_evidence_on_public_demo)])
def list_provider_capability_proofs(
    _auth: object = Depends(verify_agents_caller),
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
    legacy_ids = {record.artifact_id for record in legacy_records}
    records_by_provider = {
        provider: tuple(record for record in records if record.provider == provider and record.artifact_id not in legacy_ids)
        for provider in managed_provider_names()
    }
    available = store.available_blob_digests(records=tuple(record for record in records if record.artifact_id not in legacy_ids))
    reports = {
        provider: store.integrity_report(provider, records=provider_records, available=available)
        for provider, provider_records in records_by_provider.items()
        if provider_records
    }
    integrity = {item.artifact_id: item for report in reports.values() for item in report.artifacts}
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


_MAX_VERSION_EVIDENCE_RECORDS = 5000


def _required_assertion_ids(provider: str) -> list[str]:
    """The assertion ids the public chip certification counts for one provider."""

    try:
        edges = load_chip_edge_assertions()
    except SystemExit as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
    return sorted({assertion.assertion_id for chip in edges.get(provider, {}).values() if chip for assertion in chip})


@router.get("/agents/provider-version-evidence", dependencies=[Depends(_refuse_evidence_on_public_demo)])
def get_provider_version_evidence(
    provider: str = Query(min_length=1, max_length=64),
    version: str = Query(min_length=1, max_length=128),
    _auth: object = Depends(verify_agents_caller),
    _single: None = Depends(require_single_tenant),
) -> dict[str, Any]:
    """Facts only: the proof records and failing cell verdicts one provider
    version has, plus the assertions the chip certification requires. The
    caller decides what they add up to; nothing here is a verdict."""

    known = provider in managed_provider_names()
    store = _proof_store()
    provider_records = store.records(provider) if known else ()
    records = sorted(
        (record for record in provider_records if record.provider_version == version),
        key=lambda record: (_evidence_moment(record.generated_at), record.artifact_id),
        reverse=True,
    )
    total = len(records)
    shown = tuple(records[:_MAX_VERSION_EVIDENCE_RECORDS])
    integrity = {}
    if shown:
        available = store.available_blob_digests(records=shown)
        report = store.integrity_report(provider, records=shown, available=available)
        integrity = {item.artifact_id: item for item in report.artifacts}
    # Facts, not a fold: a failing verdict is returned with the newest pass of the
    # same cell (any version) and the newest store-admissible one, so a caller can
    # tell a verdict a later pass superseded from a live one. Whether a pass also
    # qualifies for the chart (sha, epoch, age) is the projection's call, not this
    # route's. Verdicts carry no provider version.
    newest_pass: dict[tuple[str, str, str, str | None], ProviderCapabilityProofRecord] = {}
    for record in provider_records:
        if record.outcome.value != "pass":
            continue
        key = (record.provider, record.assertion_id, record.scenario_id, record.assertion_variant)
        current = newest_pass.get(key)
        if current is None or _evidence_moment(record.generated_at) > _evidence_moment(current.generated_at):
            newest_pass[key] = record
    verdicts = [
        verdict
        for verdict in (_cell_verdict_store().verdicts().values() if known else ())
        if verdict.provider == provider and verdict.consecutive_failures >= REVOKING_CONSECUTIVE_FAILURES
    ]
    pass_records = tuple(newest_pass[v.key] for v in verdicts if v.key in newest_pass)
    pass_admissible: dict[str, bool] = {}
    if pass_records:
        report = store.integrity_report(provider, records=pass_records, available=store.available_blob_digests(records=pass_records))
        pass_admissible = {item.artifact_id: bool(item.admissible) for item in report.artifacts}
    failing = []
    for verdict in verdicts:
        latest = newest_pass.get(verdict.key)
        failing.append(
            {
                **verdict.serialize(),
                "newest_pass_at": latest.generated_at if latest else None,
                "newest_pass_admissible": pass_admissible.get(latest.artifact_id) if latest else None,
            }
        )
    return {
        "schema_version": 1,
        "artifact_kind": "provider_version_evidence",
        "provider": provider,
        "version": version,
        "records": [
            {
                "assertion_id": record.assertion_id,
                "scenario_id": record.scenario_id,
                "variant": record.assertion_variant,
                "outcome": record.outcome.value,
                "evidence_class": record.evidence_class.value,
                "longhouse_git_sha": record.longhouse_git_sha,
                "generated_at": record.generated_at,
                "store_integrity": {
                    "admissible": bool(integrity[record.artifact_id].admissible),
                    "reason_codes": list(integrity[record.artifact_id].reason_codes),
                },
            }
            for record in shown
        ],
        "total_records": total,
        "truncated": total > len(shown),
        "failing_verdicts": failing,
        "failing_verdicts_version_attributed": False,
        "required_assertions": _required_assertion_ids(provider) if known else [],
    }


def _evidence_moment(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _require_owner_capable_evidence_caller(caller: Caller = Depends(verify_agents_caller)) -> Caller:
    if isinstance(caller.principal, ManagedSessionToken):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Managed-session tokens cannot read owner-wide provider evidence",
        )
    if caller.owner_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Provider evidence requires an owner-bound caller")
    return caller


@router.get(
    "/agents/provider-capability-proofs/blobs/{sha256}",
    dependencies=[Depends(_refuse_evidence_on_public_demo), Depends(require_single_tenant)],
)
def get_provider_capability_proof_blob(
    sha256: str,
    _caller: Caller = Depends(_require_owner_capable_evidence_caller),
) -> Response:
    if re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="provider capability evidence not found")
    digest = f"sha256:{sha256}"
    found = _proof_store().reference_for_digest(digest)
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="provider capability evidence not found")
    ref_payload, _record = found
    try:
        reference = FactoryBlobReference.from_mapping(ref_payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "provider_capability_reference_tampered", "message": str(exc)},
        ) from exc
    resolver = _blob_resolver()
    if resolver is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "provider_capability_blob_store_unavailable", "message": "v4 evidence resolver is not configured"},
        )
    try:
        verified = resolver.resolve(reference, collect=True)
    except ProviderCapabilityBlobMissing as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "provider_capability_blob_missing", "message": str(exc)},
        ) from exc
    except ProviderCapabilityBlobTampered as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "provider_capability_blob_tampered", "message": str(exc)},
        ) from exc
    except ProviderCapabilityBlobUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "provider_capability_blob_store_unavailable", "message": str(exc)},
        ) from exc
    if verified.content is None:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="provider capability evidence was not collected")
    return Response(
        content=verified.content,
        media_type=reference.media_type,
        headers={
            "Content-Length": str(reference.byte_length),
            "Content-Disposition": f'attachment; filename="provider-capability-{sha256}.bin"',
            "X-Content-Type-Options": "nosniff",
            "X-Provider-Capability-Sha256": reference.digest,
        },
    )


def _published_records() -> tuple[
    list[ProviderCapabilityProofRecord],
    dict[str, tuple[str, ...]],
    Callable[[ProviderCapabilityProofRecord], tuple[str, ...]],
]:
    store = _proof_store()
    providers = sorted(managed_provider_names())
    all_records = [record for provider in providers for record in store.records(provider)]
    integrity_reasons: dict[str, tuple[str, ...]] = {}
    publication_by_provider: dict[str, dict[str, frozenset[tuple[str, str, str, int]]]] = {}

    def read_integrity(record: ProviderCapabilityProofRecord) -> tuple[str, ...]:
        if record.artifact_id not in integrity_reasons:
            if record.provider not in publication_by_provider:
                publication_by_provider[record.provider] = store.publication_facts(record.provider)
            # Only candidates that can support a current result (or the latest
            # rejected attempt) need evidence validation. Historical attempts
            # and unrelated retained blobs remain the full audit's concern.
            report = store.integrity_report(
                record.provider,
                records=(record,),
                available=store.available_blob_digests(records=(record,)),
                publication_facts=publication_by_provider[record.provider],
            )
            integrity_reasons[record.artifact_id] = report.artifacts[0].reason_codes
        return integrity_reasons[record.artifact_id]

    legacy_store = _legacy_proof_store()
    for provider in providers:
        legacy_records = legacy_store.records(provider)
        all_records.extend(legacy_records)
        integrity_reasons.update({record.artifact_id: ("proof_schema_legacy", "historical_schema_v2") for record in legacy_records})
    return all_records, integrity_reasons, read_integrity


def build_capability_projection_payload(
    *,
    expected_longhouse_sha: str | None = None,
    expected_epoch_digest: str | None = None,
) -> dict[str, Any]:
    """Capability projection from the contract, proof status attached
    separately. Every
    declared capability assertion for every managed provider gets exactly
    one row, whether or not it has ever been proven -- the schema is the
    source of truth for what should exist.

    This is the device-token machine diagnostic. The public certification
    response uses the same proof reader and projection rules.
    """
    all_records, integrity_reasons, read_integrity = _published_records()
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
    settings = get_settings()
    expected_longhouse_sha = expected_longhouse_sha or getattr(settings, "provider_capability_expected_longhouse_sha", None)
    expected_epoch_digest = expected_epoch_digest or getattr(settings, "provider_capability_expected_epoch_digest", None)
    projections = project_capabilities(
        assertions,
        all_records,
        integrity_reasons=integrity_reasons,
        expected_longhouse_sha=expected_longhouse_sha,
        expected_epoch_digest=expected_epoch_digest,
        verdicts=_cell_verdict_store().verdicts(),
        integrity_reader=read_integrity,
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
    _auth: object = Depends(verify_agents_caller),
    _single: None = Depends(require_single_tenant),
) -> dict[str, Any]:
    """Capability projection from the contract, proof status attached
    separately. Every
    declared capability assertion for every managed provider gets exactly
    one row, whether or not it has ever been proven -- the schema is the
    source of truth for what should exist."""
    return build_capability_projection_payload()


CHIP_CERTIFICATION_VERSION = "provider-chip-certification-v1"
_CERTIFICATION_TTL_SECONDS = 60.0
_certification_cache: tuple[float, dict[str, Any]] | None = None


def build_chip_certification_payload(*, now: datetime | None = None) -> dict[str, Any]:
    """The public landing layer: each chip's proof edges joined to the
    proof records this Runtime Host holds.

    A chip with no edge is ``unproven``. Otherwise the per-requirement
    projection statuses roll up (`provider_chip_edges.rollup_state`): all
    admissible passes certify it. A cell that has failed twice in a row since
    its last pass (a factory verdict) no longer certifies and reads
    ``unverified``; a single failure does not, so one flaky run never unlights
    a chip. An unreadable verdict store raises, so the route fails and the page
    shows "unavailable" instead of a chart missing a fact. Rows carry the exact
    identity and the Longhouse SHA and provider version the supporting proof ran
    against, so a claim is scoped to what was tested rather than to "latest".
    """

    edges = load_chip_edge_assertions()
    all_records, integrity_reasons, read_integrity = _published_records()
    flat = tuple(assertion for chips in edges.values() for chip in chips.values() if chip for assertion in chip)
    projected = project_capabilities(
        flat,
        all_records,
        now=now,
        integrity_reasons=integrity_reasons,
        verdicts=_cell_verdict_store().verdicts(),
        integrity_reader=read_integrity,
    )
    by_identity = {(p.provider, p.capability, p.scenario_id, p.assertion_id, p.variant): p for p in projected}
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    providers: list[dict[str, Any]] = []
    for provider in sorted(edges):
        chips: dict[str, Any] = {}
        for chip, assertions in edges[provider].items():
            if assertions is None:
                chips[chip] = {"state": UNPROVEN, "requirements": []}
                continue
            rows = []
            for assertion in assertions:
                p = by_identity[
                    (assertion.provider, assertion.capability, assertion.scenario_id, assertion.assertion_id, assertion.variant)
                ]
                rows.append(
                    {
                        "declared_in": p.capability,
                        "scenario_id": p.scenario_id,
                        "assertion_id": p.assertion_id,
                        "variant": p.variant,
                        "proof_status": p.proof_status,
                        "latest_outcome": p.latest_outcome,
                        "proven_at": p.generated_at if p.proof_status == "pass" else None,
                        "longhouse_git_sha": p.longhouse_git_sha,
                        "provider_version": p.provider_version,
                        "accepted_epoch_id": p.accepted_epoch_id,
                        "max_age_seconds": assertion.max_age_seconds,
                    }
                )
            state = rollup_state(row["proof_status"] for row in rows)
            entry: dict[str, Any] = {"state": state, "requirements": rows}
            chips[chip] = entry
        providers.append({"provider": provider, "chips": chips})
    return {
        "schema_version": 1,
        "artifact_kind": "provider_chip_certification",
        "certification_version": CHIP_CERTIFICATION_VERSION,
        "generated_at": moment.isoformat().replace("+00:00", "Z"),
        "providers": providers,
    }


@router.get("/public/provider-certification")
def get_provider_certification(response: Response) -> dict[str, Any]:
    """Unauthenticated: the landing page reads this. It exposes proof
    status and identities only -- never evidence blobs or artifact paths."""

    global _certification_cache
    clock = time.monotonic()
    if _certification_cache is None or clock - _certification_cache[0] > _CERTIFICATION_TTL_SECONDS:
        _certification_cache = (clock, build_chip_certification_payload())
    response.headers["Cache-Control"] = f"public, max-age={int(_CERTIFICATION_TTL_SECONDS)}"
    return _certification_cache[1]
