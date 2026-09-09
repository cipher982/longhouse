"""Append-only content-addressed store for provider capability proofs."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zerg.services.provider_capability_blob_resolver import FactoryBlobReference
from zerg.services.provider_capability_proof import ProviderCapabilityProofRecord
from zerg.services.provider_capability_proof import proof_record_from_mapping

_SAFE_PROVIDER = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_ARTIFACT_ID = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ProofPublication:
    worker_id: str
    worker_census_digest: str
    auth_mechanism: str
    published_at: str
    bundle_digest: str
    authenticated: bool = True
    bundle_schema_version: int = 3


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

    @property
    def _reference_root(self) -> Path:
        return self.root / "_references"

    @property
    def _reference_admission_root(self) -> Path:
        return self.root / "_reference_admissions"

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

    def build_reference_metadata(
        self,
        record: ProviderCapabilityProofRecord,
        *,
        bundle_digest: str,
        publication: ProofPublication,
        refs: tuple[Mapping[str, Any], ...],
        verification: tuple[Mapping[str, Any], ...],
    ) -> dict[str, Any]:
        """Build content-free, immutable evidence verification metadata."""
        payload: dict[str, Any] = {
            "schema_version": 1,
            "artifact_kind": "provider_capability_proof_reference_metadata",
            "bundle_schema_version": 4,
            "artifact_id": record.artifact_id,
            "provider": record.provider,
            "bundle_digest": bundle_digest,
            "publication": {
                "worker_id": publication.worker_id,
                "worker_census_digest": publication.worker_census_digest,
                "auth_mechanism": publication.auth_mechanism,
                "published_at": publication.published_at,
            },
            "refs": [dict(ref) for ref in refs],
            "verification": [dict(item) for item in verification],
        }
        payload["metadata_digest"] = f"sha256:{hashlib.sha256(self._canonical_json(payload)).hexdigest()}"
        self._validate_reference_metadata(record, payload)
        return payload

    def write_reference_metadata(self, record: ProviderCapabilityProofRecord, payload: Mapping[str, Any]) -> Path:
        self._validate_reference_metadata(record, payload)
        record_path = self._provider_root(record.provider) / f"{record.artifact_id}.json"
        if (
            record_path.exists()
            and not self._reference_path(record).exists()
            and not self._reference_admission_path(record).exists()
            and not self._has_matching_reference_publication_event(record, payload)
        ):
            raise ValueError("historical proof artifacts cannot be converted or rebound to reference proofs")
        destination = self._reference_path(record)
        encoded = self._canonical_json(payload, pretty=True)
        if destination.exists():
            if destination.read_bytes() != encoded:
                raise ValueError(f"proof reference metadata would be rewritten: {record.artifact_id}")
        else:
            self._atomic_bytes(destination, encoded)

        admission = {
            "schema_version": 1,
            "artifact_kind": "provider_capability_proof_reference_admission",
            "artifact_id": record.artifact_id,
            "provider": record.provider,
            "metadata_digest": payload["metadata_digest"],
        }
        admission_path = self._reference_admission_path(record)
        admission_encoded = self._canonical_json(admission, pretty=True)
        if admission_path.exists():
            if admission_path.read_bytes() != admission_encoded:
                raise ValueError(f"proof reference admission would be rewritten: {record.artifact_id}")
        else:
            self._atomic_bytes(admission_path, admission_encoded)
        return destination

    def reference_for_digest(self, digest: str) -> tuple[dict[str, Any], ProviderCapabilityProofRecord] | None:
        """Return one admissible v4 ref bound to an authenticated local proof."""
        self._digest_name(digest)
        if not self._reference_root.exists():
            return None
        for provider_root in sorted(self._reference_root.iterdir()):
            if not provider_root.is_dir() or not _SAFE_PROVIDER.fullmatch(provider_root.name):
                continue
            for metadata_path in provider_root.glob("*.json"):
                try:
                    artifact_id = metadata_path.stem
                    if _ARTIFACT_ID.fullmatch(artifact_id) is None:
                        continue
                    record_path = self._provider_root(provider_root.name) / f"{artifact_id}.json"
                    if not record_path.is_file():
                        continue
                    record = self.read_path(record_path)
                    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
                    self._validate_reference_metadata(record, payload)
                    admission_path = self._reference_admission_path(record)
                    if not admission_path.is_file() or not self._valid_reference_admission(record, payload, admission_path):
                        continue
                    report = self.integrity_report(record.provider, records=(record,), available=frozenset())
                    if not report.admissible_artifact_ids.__contains__(record.artifact_id):
                        continue
                    for ref in payload["refs"]:
                        if ref["digest"] == digest:
                            return dict(ref), record
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    continue
        return None

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
        if publication.bundle_schema_version == 4:
            payload["bundle_schema_version"] = 4
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

    def integrity_report(
        self,
        provider: str,
        *,
        records: tuple[ProviderCapabilityProofRecord, ...] | None = None,
        available: frozenset[str] | None = None,
    ) -> ProofStoreIntegrityReport:
        """Check retained records, optionally restricting the scan to a batch.

        The normal diagnostic/reporting path scans all retained records. The
        authenticated publication endpoint only needs to admit the records in
        the request, so it can pass that batch and one shared blob inventory;
        this keeps a one-record publication from re-reading all history.
        """

        full_scan = records is None
        records = self.records(provider) if full_scan else records
        available = self.available_blob_digests() if available is None else available
        # Publication events are content-addressed files, but their filename
        # also includes the publication timestamp and bundle digest. Looking
        # up one event by scanning the whole directory for every record makes
        # this check O(records * events), which turns a normal proof batch into
        # an increasingly slow request. Build the authenticated artifact set
        # once and use constant-time membership checks below.
        published_artifact_facts = self._published_artifact_facts(provider) if self.require_authenticated_publication else {}
        artifacts: list[ProofArtifactIntegrity] = []
        for record in records:
            reasons: list[str] = []
            refs = set(record.referenced_content_digests())
            reference_path = self._reference_path(record)
            admission_path = self._reference_admission_path(record)
            publication = published_artifact_facts.get(record.artifact_id, frozenset())
            is_reference_proof = reference_path.is_file() or admission_path.is_file() or any(fact[3] == 4 for fact in publication)
            if is_reference_proof:
                if not reference_path.is_file():
                    reasons.append("proof_reference_metadata_missing")
                else:
                    try:
                        metadata = json.loads(reference_path.read_text(encoding="utf-8"))
                        self._validate_reference_metadata(record, metadata)
                        if not admission_path.is_file() or not self._valid_reference_admission(record, metadata, admission_path):
                            reasons.append("proof_reference_admission_missing_or_tampered")
                        elif not self._has_matching_reference_publication_event(record, metadata):
                            reasons.append("proof_reference_publication_mismatch")
                    except (OSError, TypeError, ValueError, json.JSONDecodeError):
                        reasons.append("proof_reference_metadata_invalid")
            elif refs - available:
                # This is the unchanged v3 local-blob behavior. A v4
                # admission always leaves the marker above, so missing v4
                # metadata cannot be mistaken for a legacy proof.
                reasons.append("proof_referenced_content_missing")
            if self.require_authenticated_publication and not any(
                fact[:3] == (record.worker_id, record.worker_census_digest, record.auth_mechanism) for fact in publication
            ):
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
            orphan_blob_digests=(tuple(sorted(available - self._all_referenced_content_digests())) if full_scan else ()),
        )

    def _published_artifact_facts(self, provider: str) -> dict[str, frozenset[tuple[str, str, str, int]]]:
        """Return valid authenticated publication identities for one provider.

        The event directory is append-only and each event names its artifact.
        Parse it once per integrity report instead of re-reading every event
        for every retained proof record.
        """

        root = self._event_root / provider
        if not root.exists():
            return {}
        published: dict[str, set[tuple[str, str, str, int]]] = {}
        for path in root.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            event_id = payload.get("event_id")
            canonical = {key: value for key, value in payload.items() if key != "event_id"}
            expected_id = hashlib.sha256(self._canonical_json(canonical)).hexdigest()
            artifact_id = payload.get("artifact_id")
            worker_id = payload.get("worker_id")
            worker_census_digest = payload.get("worker_census_digest")
            auth_mechanism = payload.get("auth_mechanism")
            bundle_schema = payload.get("bundle_schema_version", 3)
            if (
                event_id == expected_id
                and path.stem == expected_id
                and payload.get("schema_version") == 1
                and payload.get("artifact_kind") == "provider_capability_proof_publication"
                and isinstance(artifact_id, str)
                and payload.get("provider") == provider
                and isinstance(worker_id, str)
                and isinstance(worker_census_digest, str)
                and isinstance(auth_mechanism, str)
                and type(bundle_schema) is int
                and bundle_schema in (3, 4)
                and _SHA256.fullmatch(str(payload.get("bundle_digest") or ""))
            ):
                published.setdefault(artifact_id, set()).add((worker_id, worker_census_digest, auth_mechanism, bundle_schema))
        return {artifact_id: frozenset(facts) for artifact_id, facts in published.items()}

    def _has_publication_event(self, record: ProviderCapabilityProofRecord) -> bool:
        expected = (record.worker_id, record.worker_census_digest, record.auth_mechanism)
        return any(fact[:3] == expected for fact in self._published_artifact_facts(record.provider).get(record.artifact_id, frozenset()))

    def _reference_path(self, record: ProviderCapabilityProofRecord) -> Path:
        return self._reference_root / self._provider_root(record.provider).name / f"{record.artifact_id}.json"

    def _reference_admission_path(self, record: ProviderCapabilityProofRecord) -> Path:
        return self._reference_admission_root / self._provider_root(record.provider).name / f"{record.artifact_id}.json"

    def _valid_reference_admission(self, record: ProviderCapabilityProofRecord, metadata: Mapping[str, Any], path: Path) -> bool:
        try:
            admission = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return False
        return admission == {
            "schema_version": 1,
            "artifact_kind": "provider_capability_proof_reference_admission",
            "artifact_id": record.artifact_id,
            "provider": record.provider,
            "metadata_digest": metadata.get("metadata_digest"),
        }

    def _has_matching_reference_publication_event(self, record: ProviderCapabilityProofRecord, metadata: Mapping[str, Any]) -> bool:
        publication = metadata["publication"]
        payload = {
            "schema_version": 1,
            "artifact_kind": "provider_capability_proof_publication",
            "artifact_id": record.artifact_id,
            "provider": record.provider,
            "worker_id": publication["worker_id"],
            "worker_census_digest": publication["worker_census_digest"],
            "auth_mechanism": publication["auth_mechanism"],
            "published_at": publication["published_at"],
            "bundle_digest": metadata["bundle_digest"],
            "bundle_schema_version": 4,
        }
        event_id = hashlib.sha256(self._canonical_json(payload)).hexdigest()
        path = self._event_root / record.provider / f"{event_id}.json"
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            return False
        return event == {**payload, "event_id": event_id}

    def _validate_reference_metadata(self, record: ProviderCapabilityProofRecord, payload: Mapping[str, Any]) -> None:
        expected_keys = {
            "schema_version",
            "artifact_kind",
            "bundle_schema_version",
            "artifact_id",
            "provider",
            "bundle_digest",
            "publication",
            "refs",
            "verification",
            "metadata_digest",
        }
        if not isinstance(payload, Mapping) or set(payload) != expected_keys:
            raise ValueError("proof reference metadata schema is invalid")
        unsigned = {key: value for key, value in payload.items() if key != "metadata_digest"}
        if (
            payload.get("schema_version") != 1
            or payload.get("artifact_kind") != "provider_capability_proof_reference_metadata"
            or payload.get("bundle_schema_version") != 4
            or payload.get("artifact_id") != record.artifact_id
            or payload.get("provider") != record.provider
            or not isinstance(payload.get("bundle_digest"), str)
            or _SHA256.fullmatch(str(payload.get("bundle_digest"))) is None
            or payload.get("metadata_digest") != f"sha256:{hashlib.sha256(self._canonical_json(unsigned)).hexdigest()}"
        ):
            raise ValueError("proof reference metadata identity is invalid")
        publication = payload.get("publication")
        if not isinstance(publication, Mapping) or set(publication) != {
            "worker_id",
            "worker_census_digest",
            "auth_mechanism",
            "published_at",
        }:
            raise ValueError("proof reference metadata publication is invalid")
        if any(publication.get(name) != getattr(record, name) for name in ("worker_id", "worker_census_digest", "auth_mechanism")):
            raise ValueError("proof reference metadata publication differs from its record")
        refs = payload.get("refs")
        if not isinstance(refs, list) or not 1 <= len(refs) <= 512:
            raise ValueError("proof reference metadata refs are invalid")
        ref_digests: set[str] = set()
        for ref in refs:
            if not isinstance(ref, Mapping):
                raise ValueError("proof reference metadata ref shape is invalid")
            reference = FactoryBlobReference.from_mapping(ref)
            digest = reference.digest
            if digest in ref_digests:
                raise ValueError("proof reference metadata refs are duplicated")
            ref_digests.add(digest)
        if ref_digests != set(record.referenced_content_digests()):
            raise ValueError("proof reference metadata refs do not bind the record")
        if sum(ref["byte_length"] for ref in refs) > 50 * 1024 * 1024:
            raise ValueError("proof reference metadata exceeds the total byte cap")
        bundle = {
            "schema_version": 4,
            "artifact_kind": "provider_capability_proof_bundle",
            "records": [record.serialize()],
            "blobs": refs,
            "publication": dict(publication),
        }
        if self._digest_bytes(self._canonical_json(bundle)) != payload["bundle_digest"]:
            raise ValueError("proof reference metadata differs from its authenticated bundle")
        verification = payload.get("verification")
        if not isinstance(verification, list) or len(verification) != len(refs):
            raise ValueError("proof reference metadata verification is invalid")
        by_digest = {item.get("digest"): item for item in verification if isinstance(item, Mapping)}
        if set(by_digest) != ref_digests or len(by_digest) != len(verification):
            raise ValueError("proof reference metadata verification identities are invalid")
        for ref in refs:
            item = by_digest[ref["digest"]]
            expected_raw = ref["digest"].removeprefix("sha256:")
            expected_checksum = base64.b64encode(bytes.fromhex(expected_raw)).decode("ascii")
            if item != {
                "digest": ref["digest"],
                "key": ref["key"],
                "content_length": ref["byte_length"],
                "content_type": ref["media_type"],
                "metadata_sha256": expected_raw,
                "checksum_sha256": expected_checksum,
                "body_sha256": expected_raw,
                "complete": True,
            }:
                raise ValueError("proof reference metadata verification does not bind its ref")

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
