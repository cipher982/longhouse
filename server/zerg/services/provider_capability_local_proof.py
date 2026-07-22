"""Local diagnostic reader for append-only provider capability proof records."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from zerg.services.longhouse_paths import resolve_longhouse_home
from zerg.services.managed_provider_contracts import all_managed_provider_contracts
from zerg.services.provider_capability_proof import ProviderCapabilityProofRecord
from zerg.services.provider_capability_proof_store import ProviderCapabilityProofStore


def executable_identity(path: str | None) -> str | None:
    if not path:
        return None
    resolved = Path(path).expanduser().resolve(strict=False)
    if not resolved.is_file():
        return None
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def collect_local_capability_proofs(
    base_dir: Path | None = None,
) -> tuple[dict[str, tuple[ProviderCapabilityProofRecord, ...]], dict[str, Any]]:
    root = resolve_longhouse_home(base_dir) / "provider-capability-proofs"
    store = ProviderCapabilityProofStore(root)
    records_by_provider: dict[str, tuple[ProviderCapabilityProofRecord, ...]] = {}
    providers: dict[str, Any] = {}
    for contract in all_managed_provider_contracts():
        try:
            records = store.records(contract.provider)
        except (OSError, ValueError) as exc:
            records = ()
            providers[contract.provider] = {"state": "invalid", "record_count": 0, "error": str(exc)}
        else:
            providers[contract.provider] = {
                "state": "present" if records else "missing",
                "record_count": len(records),
                "artifact_ids": [record.artifact_id for record in records],
            }
        records_by_provider[contract.provider] = records
    return records_by_provider, {"root": str(root), "providers": providers}
