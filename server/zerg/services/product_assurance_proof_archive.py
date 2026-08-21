"""Durable archive for authenticated Longhouse product assurance proofs."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any


class ProductAssuranceProofArchive:
    """Validate and append product proofs without entering provider projections."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def accept(self, bundle: dict[str, Any]) -> list[str]:
        record, publication = _validate_bundle(bundle)
        encoded_bundle = _canonical_json(bundle, pretty=True)
        bundle_digest = str(bundle["bundle_digest"])
        bundle_path = self.root / "bundles" / f"{bundle_digest.removeprefix('sha256:')}.json"
        _write_immutable(bundle_path, encoded_bundle)

        event = {
            "schema_version": 1,
            "artifact_kind": "longhouse_product_assurance_publication",
            "artifact_id": record["artifact_id"],
            "subject_key": record["subject_key"],
            "bundle_digest": bundle_digest,
            "worker_id": publication["worker_id"],
            "worker_census_digest": publication["worker_census_digest"],
            "auth_mechanism": publication["auth_mechanism"],
            "published_at": publication["published_at"],
        }
        event_id = hashlib.sha256(_canonical_json(event)).hexdigest()
        event["event_id"] = event_id
        event_path = self.root / "events" / str(record["artifact_id"]) / f"{event_id}.json"
        _write_immutable(event_path, _canonical_json(event, pretty=True))
        return [str(record["artifact_id"])]


def _validate_bundle(bundle: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if bundle.get("schema_version") != 3:
        raise ValueError("product assurance bundle schema_version must be 3")
    if bundle.get("artifact_kind") != "provider_capability_proof_bundle":
        raise ValueError("product assurance bundle artifact kind is invalid")
    expected_bundle_digest = (
        "sha256:" + hashlib.sha256(_canonical_json({key: value for key, value in bundle.items() if key != "bundle_digest"})).hexdigest()
    )
    if bundle.get("bundle_digest") != expected_bundle_digest:
        raise ValueError("product assurance bundle digest does not match canonical content")

    records = bundle.get("records")
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        raise ValueError("product assurance bundle must contain exactly one record")
    record = records[0]
    required_record_strings = (
        "artifact_id",
        "subject_key",
        "provider_contract_digest",
        "adapter_digest",
        "scenario_id",
        "oracle_digest",
        "assertion_id",
        "generated_at",
        "producer_class",
        "producer_version",
        "invocation_id",
        "evidence_class",
        "outcome",
        "run_reference",
        "longhouse_git_sha",
        "factory_source_sha",
        "accepted_epoch_id",
        "accepted_epoch_digest",
        "verifier_bundle_digest",
        "compile_report_digest",
        "plan_digest",
        "sandbox_receipt_digest",
        "cleanup_receipt_digest",
        "worker_id",
        "worker_census_digest",
        "auth_mechanism",
    )
    if record.get("schema_version") != 3 or record.get("artifact_kind") != "provider_capability_assertion":
        raise ValueError("product assurance record schema is invalid")
    if record.get("subject_kind") != "longhouse_product":
        raise ValueError("product assurance record subject kind is invalid")
    if any(not isinstance(record.get(name), str) or not record.get(name) for name in required_record_strings):
        raise ValueError("product assurance record identity is incomplete")
    provider_fields = (
        "provider",
        "provider_version",
        "provider_executable_identity",
        "provider_build_identity",
        "provider_build_granularity",
    )
    if any(name in record for name in provider_fields):
        raise ValueError("product assurance record carries provider identity")
    if record.get("permission_mode") is not None:
        raise ValueError("product assurance record carries a provider permission mode")
    scenario_revision = record.get("scenario_revision")
    if not isinstance(scenario_revision, int) or isinstance(scenario_revision, bool) or scenario_revision < 1:
        raise ValueError("product assurance scenario revision must be a positive integer")
    if record.get("producer_class") != "release_factory" or record.get("auth_mechanism") != "factory_token_v1":
        raise ValueError("product assurance producer identity is invalid")
    _timestamp(str(record["generated_at"]), "record")
    raw_digests = record.get("raw_reference_digests")
    if not isinstance(raw_digests, list) or not raw_digests or any(not isinstance(value, str) or not value for value in raw_digests):
        raise ValueError("product assurance record has no raw evidence")
    if not isinstance(record.get("observed_activity"), list) or not record["observed_activity"]:
        raise ValueError("product assurance record has no observed activity")
    if not isinstance(record.get("acquisition_provenance"), dict) or not record["acquisition_provenance"]:
        raise ValueError("product assurance acquisition provenance is incomplete")
    if not isinstance(record.get("provenance_extension"), dict):
        raise ValueError("product assurance provenance extension is invalid")
    if record["provenance_extension"].get("subject_kind") != "longhouse_product":
        raise ValueError("product assurance provenance subject kind is invalid")
    if record["provenance_extension"].get("subject_key") != record["subject_key"]:
        raise ValueError("product assurance provenance subject key is invalid")
    expected_artifact_id = hashlib.sha256(
        _canonical_json({key: value for key, value in record.items() if key != "artifact_id"})
    ).hexdigest()
    if record["artifact_id"] != expected_artifact_id:
        raise ValueError("product assurance artifact identity does not match canonical content")

    publication = bundle.get("publication")
    if not isinstance(publication, dict):
        raise ValueError("product assurance publication identity is missing")
    for name in ("worker_id", "worker_census_digest", "auth_mechanism", "published_at"):
        if not isinstance(publication.get(name), str) or not publication.get(name):
            raise ValueError("product assurance publication identity is incomplete")
    if publication["auth_mechanism"] != "factory_token_v1":
        raise ValueError("product assurance publication auth mechanism is invalid")
    _timestamp(str(publication["published_at"]), "publication")
    for name in ("worker_id", "worker_census_digest", "auth_mechanism"):
        if publication[name] != record[name]:
            raise ValueError("product assurance record differs from publication identity")

    blobs = bundle.get("blobs")
    if not isinstance(blobs, list) or not blobs:
        raise ValueError("product assurance bundle has no evidence blobs")
    declared: set[str] = set()
    for blob in blobs:
        if not isinstance(blob, dict) or not isinstance(blob.get("digest"), str) or not isinstance(blob.get("content_base64"), str):
            raise ValueError("product assurance evidence blob is invalid")
        try:
            content = base64.b64decode(blob["content_base64"], validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("product assurance evidence blob is not valid base64") from exc
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        if blob["digest"] != digest or digest in declared:
            raise ValueError("product assurance evidence blob identity is invalid")
        declared.add(digest)
    referenced = {
        *raw_digests,
        record["accepted_epoch_digest"],
        record["verifier_bundle_digest"],
        record["worker_census_digest"],
        record["compile_report_digest"],
        record["plan_digest"],
        record["sandbox_receipt_digest"],
        record["cleanup_receipt_digest"],
    }
    if declared != referenced:
        raise ValueError("product assurance evidence blobs do not match referenced content")
    return record, publication


def _timestamp(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"product assurance {label} timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"product assurance {label} timestamp must include a timezone")
    parsed.astimezone(UTC)


def _canonical_json(payload: object, *, pretty: bool = False) -> bytes:
    if pretty:
        return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _write_immutable(destination: Path, payload: bytes) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}-", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if destination.read_bytes() != payload:
                raise ValueError(f"product assurance artifact would be rewritten: {destination}")
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = ["ProductAssuranceProofArchive"]
