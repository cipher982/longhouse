"""Append-only local store for provider capability proof records."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from zerg.provider_contract.provider_capability_proof import ProviderCapabilityProofRecord
from zerg.provider_contract.provider_capability_proof import proof_record_from_mapping

_SAFE_PROVIDER = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class ProviderCapabilityProofStore:
    """One JSON file per record, named by its content-derived artifact id."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _provider_root(self, provider: str) -> Path:
        if not _SAFE_PROVIDER.fullmatch(provider):
            raise ValueError(f"invalid provider proof path component: {provider!r}")
        return self.root / provider

    def write(self, record: ProviderCapabilityProofRecord) -> Path:
        destination = self._provider_root(record.provider) / f"{record.artifact_id}.json"
        encoded = (json.dumps(record.serialize(), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        if destination.exists():
            if self.read_path(destination) != record or destination.read_bytes() != encoded:
                raise ValueError(f"proof artifact identity collision at {destination}")
            return destination
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}-", dir=destination.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def read_path(self, path: Path) -> ProviderCapabilityProofRecord:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"provider capability proof must be an object: {path}")
        return proof_record_from_mapping(payload)

    def records(self, provider: str) -> tuple[ProviderCapabilityProofRecord, ...]:
        provider_root = self._provider_root(provider)
        if not provider_root.exists():
            return ()
        records = [self.read_path(path) for path in provider_root.glob("*.json") if not path.name.startswith(".")]
        return tuple(sorted(records, key=lambda record: (record.generated_at, record.artifact_id)))


__all__ = ["ProviderCapabilityProofStore"]
