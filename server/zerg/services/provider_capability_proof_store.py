"""Append-only content-addressed store for provider capability proofs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zerg.services.provider_capability_proof import ProviderCapabilityProofRecord
from zerg.services.provider_capability_proof import proof_record_from_mapping

_SAFE_PROVIDER = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class ProofPublication:
    worker_id: str
    worker_census_digest: str
    auth_mechanism: str
    published_at: str
    bundle_digest: str
    authenticated: bool = True


@dataclass(frozen=True)
class ProofArtifactIntegrity:
    artifact_id: str
    admissible: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class ProofStoreIntegrityReport:
    provider: str
    records_scanned: int
    blobs_scanned: int
    artifacts: tuple[ProofArtifactIntegrity, ...]
    orphan_blob_digests: tuple[str, ...]

    @property
    def admissible_artifact_ids(self) -> frozenset[str]:
        return frozenset(item.artifact_id for item in self.artifacts if item.admissible)


class ProviderCapabilityProofStore:
    """Proof envelopes, publication events, epoch roots, and retained blobs.

    ``require_authenticated_publication`` is enabled for the Runtime Host's
    trusted factory store.  Local diagnostic stores keep working without
    pretending their records were published by the factory.
    """

    def __init__(self, root: Path, *, require_authenticated_publication: bool = False) -> None:
        self.root = Path(root)
        self.require_authenticated_publication = require_authenticated_publication

    def _provider_root(self, provider: str) -> Path:
        if not _SAFE_PROVIDER.fullmatch(provider):
            raise ValueError(f"invalid provider proof path component: {provider!r}")
        return self.root / provider

    @property
    def _blob_root(self) -> Path:
        return self.root / "_blobs" / "sha256"

    @property
    def _event_root(self) -> Path:
        return self.root / "_events"

    @property
    def _epoch_root(self) -> Path:
        return self.root / "_epoch_roots"

    @staticmethod
    def _digest_bytes(payload: bytes) -> str:
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"

    @staticmethod
    def _digest_name(digest: str) -> str:
        if not _SHA256.fullmatch(digest):
            raise ValueError(f"invalid content digest: {digest!r}")
        return digest.removeprefix("sha256:")

    def write_blob(self, payload: bytes, *, expected_digest: str | None = None) -> str:
        digest = self._digest_bytes(payload)
        if expected_digest is not None and digest != expected_digest:
            raise ValueError("retained proof blob digest does not match its declared identity")
        destination = self._blob_root / self._digest_name(digest)
        if destination.exists():
            if self._digest_bytes(destination.read_bytes()) != digest:
                raise ValueError(f"retained proof blob was mutated: {digest}")
            return digest
        self._atomic_bytes(destination, payload)
        return digest

    def has_blob(self, digest: str) -> bool:
        path = self._blob_root / self._digest_name(digest)
        return path.is_file() and self._digest_bytes(path.read_bytes()) == digest

    def write_epoch_root(self, *, epoch_id: str, epoch_digest: str, payload: Mapping[str, Any] | None = None) -> Path:
        if not epoch_id or "/" in epoch_id or epoch_id in {".", ".."}:
            raise ValueError("accepted epoch ID is unsafe")
        self._digest_name(epoch_digest)
        root = {
            "schema_version": 1,
            "artifact_kind": "provider_capability_accepted_epoch_root",
            "epoch_id": epoch_id,
            "epoch_digest": epoch_digest,
            "payload": dict(payload or {}),
        }
        destination = self._epoch_root / f"{epoch_id}.json"
        encoded = self._canonical_json(root, pretty=True)
        if destination.exists():
            if destination.read_bytes() != encoded:
                raise ValueError(f"accepted epoch root would be rewritten: {epoch_id}")
            return destination
        self._atomic_bytes(destination, encoded)
        return destination

    def write(
        self,
        record: ProviderCapabilityProofRecord,
        *,
        publication: ProofPublication | None = None,
        rebuild_index: bool = True,
    ) -> Path:
        if self.require_authenticated_publication and (publication is None or not publication.authenticated):
            raise ValueError("trusted proof store requires authenticated factory publication")
        if publication is not None:
            if not publication.authenticated:
                raise ValueError("proof publication is not authenticated")
            if record.worker_id != publication.worker_id:
                raise ValueError("proof worker identity differs from authenticated publication")
            if record.worker_census_digest != publication.worker_census_digest:
                raise ValueError("proof worker census differs from authenticated publication")
            if record.auth_mechanism != publication.auth_mechanism:
                raise ValueError("proof auth mechanism differs from authenticated publication")

        provider_root = self._provider_root(record.provider)
        destination = provider_root / f"{record.artifact_id}.json"
        encoded = self._canonical_json(record.serialize(), pretty=True)
        if destination.exists():
            existing = self.read_path(destination)
            if existing != record or destination.read_bytes() != encoded:
                raise ValueError(f"proof artifact identity collision at {destination}")
        else:
            self._atomic_bytes(destination, encoded)
        if publication is not None:
            self._write_publication_event(record, publication)
            if record.accepted_epoch_id and record.accepted_epoch_digest:
                self.write_epoch_root(
                    epoch_id=record.accepted_epoch_id,
                    epoch_digest=record.accepted_epoch_digest,
                    payload={},
                )
        # The trusted Runtime Host publication route receives one bundle at a
        # time and validates the newly written record itself.  Rebuilding the
        # complete provider index here makes every append scan all retained
        # records (and their referenced blobs), which turns a normal proof
        # batch into an increasingly expensive synchronous request.  Keep the
        # historical default for local callers, while allowing that route to
        # defer the diagnostic index maintenance.
        if rebuild_index:
            self.rebuild_index(record.provider)
        return destination

    def _write_publication_event(self, record: ProviderCapabilityProofRecord, publication: ProofPublication) -> Path:
        payload = {
            "schema_version": 1,
            "artifact_kind": "provider_capability_proof_publication",
            "artifact_id": record.artifact_id,
            "provider": record.provider,
            "worker_id": publication.worker_id,
            "worker_census_digest": publication.worker_census_digest,
            "auth_mechanism": publication.auth_mechanism,
            "published_at": publication.published_at,
            "bundle_digest": publication.bundle_digest,
        }
        event_id = hashlib.sha256(self._canonical_json(payload)).hexdigest()
        payload["event_id"] = event_id
        destination = self._event_root / record.provider / f"{event_id}.json"
        encoded = self._canonical_json(payload, pretty=True)
        if destination.exists():
            if destination.read_bytes() != encoded:
                raise ValueError(f"proof publication event would be rewritten: {event_id}")
            return destination
        self._atomic_bytes(destination, encoded)
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
        records = [
            self.read_path(path) for path in provider_root.glob("*.json") if path.name != "index.json" and not path.name.startswith(".")
        ]
        return tuple(sorted(records, key=lambda record: (record.generated_at, record.artifact_id)))

    def available_blob_digests(self) -> frozenset[str]:
        if not self._blob_root.exists():
            return frozenset()
        values = []
        for path in self._blob_root.iterdir():
            if path.is_file() and re.fullmatch(r"[0-9a-f]{64}", path.name):
                digest = f"sha256:{path.name}"
                if self._digest_bytes(path.read_bytes()) == digest:
                    values.append(digest)
        return frozenset(values)

    def _all_referenced_content_digests(self) -> frozenset[str]:
        if not self.root.exists():
            return frozenset()
        referenced: set[str] = set()
        for path in self.root.iterdir():
            if path.is_dir() and not path.name.startswith("_") and _SAFE_PROVIDER.fullmatch(path.name):
                for record in self.records(path.name):
                    referenced.update(record.referenced_content_digests())
        return frozenset(referenced)

    def integrity_report(self, provider: str) -> ProofStoreIntegrityReport:
        records = self.records(provider)
        available = self.available_blob_digests()
        artifacts: list[ProofArtifactIntegrity] = []
        for record in records:
            reasons: list[str] = []
            refs = set(record.referenced_content_digests())
            if refs - available:
                reasons.append("proof_referenced_content_missing")
            if self.require_authenticated_publication and not self._has_publication_event(record):
                reasons.append("proof_authenticated_publication_missing")
            if self.require_authenticated_publication:
                root_path = self._epoch_root / f"{record.accepted_epoch_id}.json" if record.accepted_epoch_id else None
                if root_path is None or not root_path.is_file():
                    reasons.append("proof_accepted_epoch_root_missing")
                else:
                    try:
                        root = json.loads(root_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        root = {}
                    if (
                        root.get("schema_version") != 1
                        or root.get("artifact_kind") != "provider_capability_accepted_epoch_root"
                        or root.get("epoch_id") != record.accepted_epoch_id
                        or root.get("epoch_digest") != record.accepted_epoch_digest
                    ):
                        reasons.append("proof_accepted_epoch_root_mismatch")
            artifacts.append(ProofArtifactIntegrity(record.artifact_id, not reasons, tuple(dict.fromkeys(reasons))))
        return ProofStoreIntegrityReport(
            provider=provider,
            records_scanned=len(records),
            blobs_scanned=len(available),
            artifacts=tuple(artifacts),
            orphan_blob_digests=tuple(sorted(available - self._all_referenced_content_digests())),
        )

    def _has_publication_event(self, record: ProviderCapabilityProofRecord) -> bool:
        root = self._event_root / record.provider
        if not root.exists():
            return False
        for path in root.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            event_id = payload.get("event_id")
            canonical = {key: value for key, value in payload.items() if key != "event_id"}
            expected_id = hashlib.sha256(self._canonical_json(canonical)).hexdigest()
            if (
                event_id == expected_id
                and path.stem == expected_id
                and payload.get("schema_version") == 1
                and payload.get("artifact_kind") == "provider_capability_proof_publication"
                and payload.get("artifact_id") == record.artifact_id
                and payload.get("provider") == record.provider
                and payload.get("worker_id") == record.worker_id
                and payload.get("worker_census_digest") == record.worker_census_digest
                and payload.get("auth_mechanism") == record.auth_mechanism
                and _SHA256.fullmatch(str(payload.get("bundle_digest") or ""))
            ):
                return True
        return False

    def rebuild_index(self, provider: str) -> Path:
        provider_root = self._provider_root(provider)
        provider_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        records = self.records(provider)
        payload = {
            "schema_version": 2,
            "provider": provider,
            "artifact_ids": [record.artifact_id for record in records],
            "integrity": {
                item.artifact_id: {"admissible": item.admissible, "reason_codes": list(item.reason_codes)}
                for item in self.integrity_report(provider).artifacts
            },
        }
        destination = provider_root / "index.json"
        self._replace_bytes(destination, self._canonical_json(payload, pretty=True))
        return destination

    @staticmethod
    def _canonical_json(payload: object, *, pretty: bool = False) -> bytes:
        if pretty:
            return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()

    @staticmethod
    def _atomic_bytes(destination: Path, payload: bytes) -> None:
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
                    raise ValueError(f"content-addressed artifact would be rewritten: {destination}")
            _fsync_directory(destination.parent)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _replace_bytes(destination: Path, payload: bytes) -> None:
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}-", dir=destination.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            _fsync_directory(destination.parent)
        finally:
            temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


__all__ = [
    "ProofArtifactIntegrity",
    "ProofPublication",
    "ProofStoreIntegrityReport",
    "ProviderCapabilityProofStore",
]
