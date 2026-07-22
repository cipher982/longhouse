"""Append-only filesystem store for provider capability assertion records."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from zerg.services.provider_capability_proof import ProviderCapabilityProofRecord
from zerg.services.provider_capability_proof import proof_record_from_mapping

_SAFE_PROVIDER = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class ProviderCapabilityProofStore:
    """Content-addressed records with a rebuildable, non-authoritative index."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _provider_root(self, provider: str) -> Path:
        if not _SAFE_PROVIDER.fullmatch(provider):
            raise ValueError(f"invalid provider proof path component: {provider!r}")
        return self.root / provider

    def write(self, record: ProviderCapabilityProofRecord) -> Path:
        provider_root = self._provider_root(record.provider)
        provider_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination = provider_root / f"{record.artifact_id}.json"
        if destination.exists():
            existing = self.read_path(destination)
            if existing != record:
                raise ValueError(f"proof artifact identity collision at {destination}")
            return destination

        fd, temp_name = tempfile.mkstemp(prefix=".proof-", suffix=".tmp", dir=provider_root)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record.serialize(), handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temp_path, destination)
            except FileExistsError:
                existing = self.read_path(destination)
                if existing != record:
                    raise ValueError(f"proof artifact identity collision at {destination}")
            _fsync_directory(provider_root)
            self.rebuild_index(record.provider)
            return destination
        finally:
            temp_path.unlink(missing_ok=True)

    def read_path(self, path: Path) -> ProviderCapabilityProofRecord:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"provider capability proof must be an object: {path}")
        return proof_record_from_mapping(payload)

    def records(self, provider: str) -> tuple[ProviderCapabilityProofRecord, ...]:
        provider_root = self._provider_root(provider)
        if not provider_root.exists():
            return ()
        records = [
            self.read_path(path) for path in provider_root.glob("*.json") if path.name != "index.json" and not path.name.startswith(".")
        ]
        return tuple(sorted(records, key=lambda record: (record.generated_at, record.artifact_id)))

    def rebuild_index(self, provider: str) -> Path:
        provider_root = self._provider_root(provider)
        provider_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        records = self.records(provider)
        payload = {
            "schema_version": 1,
            "provider": provider,
            "artifact_ids": [record.artifact_id for record in records],
        }
        fd, temp_name = tempfile.mkstemp(prefix=".index-", suffix=".tmp", dir=provider_root)
        temp_path = Path(temp_name)
        destination = provider_root / "index.json"
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, destination)
            _fsync_directory(provider_root)
            return destination
        finally:
            temp_path.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
