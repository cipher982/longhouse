"""Validate the compact per-cell projection carried by factory proofs.

The factory's complete qualification plan is a multi-megabyte matrix. Copying
that whole plan into every cell proof made a 75-cell run fail its own bounded
bundle limit and would duplicate the same bytes dozens of times. New proofs
therefore carry one small, digest-bound projection of the exact cell while the
record keeps the immutable full-plan digest. Older v3 proofs that embed the
full plan remain valid.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

PLAN_CELL_PROJECTION_KIND = "provider_assurance_plan_cell_projection"
PLAN_CELL_PROJECTION_VERSION = 1


def plan_content_digest(record: Mapping[str, Any]) -> str | None:
    """Return the plan evidence digest used by this proof generation."""

    extension = record.get("provenance_extension")
    projection = extension.get("plan_projection_digest") if isinstance(extension, Mapping) else None
    return projection if isinstance(projection, str) and projection else record.get("plan_digest")


def validate_plan_projection(
    record: Mapping[str, Any],
    content_by_digest: Mapping[str, bytes],
) -> None:
    """Validate a compact plan projection when the record selects one."""

    extension = record.get("provenance_extension")
    if not isinstance(extension, Mapping) or "plan_projection_digest" not in extension:
        return
    digest = extension.get("plan_projection_digest")
    if not isinstance(digest, str) or not digest.startswith("sha256:") or len(digest) != 71:
        raise ValueError("provider assurance plan projection digest is invalid")
    content = content_by_digest.get(digest)
    if content is None or f"sha256:{hashlib.sha256(content).hexdigest()}" != digest:
        raise ValueError("provider assurance plan projection content is missing or invalid")
    try:
        projection = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("provider assurance plan projection is not valid JSON") from exc
    if (
        not isinstance(projection, dict)
        or projection.get("schema_version") != PLAN_CELL_PROJECTION_VERSION
        or projection.get("artifact_kind") != PLAN_CELL_PROJECTION_KIND
        or projection.get("plan_digest") != record.get("plan_digest")
        or projection.get("epoch_digest") != record.get("accepted_epoch_digest")
    ):
        raise ValueError("provider assurance plan projection identity is invalid")
    subject = projection.get("subject")
    command = projection.get("command")
    if not isinstance(subject, dict) or not isinstance(command, dict):
        raise ValueError("provider assurance plan projection is incomplete")
    expected = {
        "subject_kind": record.get("subject_kind") or "provider_release",
        "subject_key": record.get("subject_key"),
        "provider": record.get("provider"),
        "assertion_id": record.get("assertion_id"),
        "variant": record.get("assertion_variant"),
        "scenario_id": record.get("scenario_id"),
        "scenario_revision": record.get("scenario_revision"),
        "provider_contract_digest": record.get("provider_contract_digest"),
        "adapter_digest": record.get("adapter_digest"),
        "oracle_digest": record.get("oracle_digest"),
        "evidence_class": record.get("evidence_class"),
        "mode": record.get("mode"),
        "worker_platform": record.get("platform"),
        "worker_architecture": record.get("architecture"),
        "longhouse_source_sha": record.get("longhouse_git_sha"),
    }
    if any(command.get(key) != value for key, value in expected.items()):
        raise ValueError("provider assurance plan projection cell differs from its proof record")
    producer_id = command.get("producer_id")
    producer_revision = command.get("producer_revision")
    if f"{producer_id}@{producer_revision}" != record.get("producer_version"):
        raise ValueError("provider assurance plan projection producer differs from its proof record")
    if subject.get("longhouse_source_sha") != record.get("longhouse_git_sha"):
        raise ValueError("provider assurance plan projection subject differs from its proof record")


__all__ = [
    "PLAN_CELL_PROJECTION_KIND",
    "PLAN_CELL_PROJECTION_VERSION",
    "plan_content_digest",
    "validate_plan_projection",
]
