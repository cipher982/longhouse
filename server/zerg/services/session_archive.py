"""Longhouse session archive bundle export helpers."""

from __future__ import annotations

import base64
import gzip
import hashlib
from datetime import datetime
from datetime import timezone
from typing import Optional
from uuid import UUID

from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session

from zerg.catalogd.client import CatalogUnavailable
from zerg.services.agents import AgentsStore
from zerg.services.catalog_facts import decode_catalog_datetime
from zerg.services.catalogd_supervisor import get_catalogd_client
from zerg.services.live_catalog_timeline import project_catalog_session_facts
from zerg.services.raw_object_workers import RawObjectWorkerError
from zerg.services.raw_object_workers import get_raw_object_worker_pool
from zerg.services.session_kernel_projection import project_session_kernel_fields
from zerg.services.session_kernel_projection import project_session_lineage_fields
from zerg.storage_v2.raw_objects import RawObjectCorruptError
from zerg.utils.time import UTCBaseModel

BUNDLE_VERSION = 1


class SessionArchivePayloadResponse(BaseModel):
    format: str = Field(..., description="Raw archive payload format")
    branch_mode: str = Field(..., description="Branch projection mode used to build the archive")
    sha256: str = Field(..., description="SHA-256 of the raw JSONL payload")
    bytes: int = Field(..., description="Raw JSONL payload size in bytes")
    jsonl_b64_gzip: str = Field(..., description="Gzip-compressed raw JSONL payload, base64-encoded")


class SessionArchiveSessionResponse(UTCBaseModel):
    id: str = Field(..., description="Session UUID")
    provider: str = Field(..., description="Session provider")
    provider_session_id: Optional[str] = Field(None, description="Provider-native session identifier")
    project: Optional[str] = Field(None, description="Project name")
    device_id: Optional[str] = Field(None, description="Machine identifier")
    device_name: Optional[str] = Field(None, description="Human-friendly machine name")
    cwd: Optional[str] = Field(None, description="Working directory")
    git_repo: Optional[str] = Field(None, description="Git remote URL")
    git_branch: Optional[str] = Field(None, description="Git branch")
    started_at: datetime = Field(..., description="Session start time")
    ended_at: Optional[datetime] = Field(None, description="Session end time")
    last_activity_at: Optional[datetime] = Field(None, description="Latest transcript activity timestamp")
    thread_root_session_id: str = Field(..., description="Logical thread root session UUID")
    continued_from_session_id: Optional[str] = Field(None, description="Parent continuation session UUID")
    continuation_kind: Optional[str] = Field(
        None,
        description="Kernel branch kind for non-root threads; null for root threads",
    )
    origin_label: Optional[str] = Field(None, description="User-facing execution origin label")
    execution_home: Optional[str] = Field(None, description="Execution home classification")
    managed_transport: Optional[str] = Field(None, description="Managed transport identifier")
    summary_title: Optional[str] = Field(None, description="Short session title")
    summary: Optional[str] = Field(None, description="Session summary")
    transcript_revision: int = Field(..., description="Current transcript revision")
    summary_revision: int = Field(..., description="Current summary revision")
    embedding_revision: int = Field(..., description="Current embedding revision")
    is_sidechain: bool = Field(False, description="True when session is a task/sub-agent session")


class SessionArchiveManifestItemResponse(UTCBaseModel):
    id: str = Field(..., description="Session UUID")
    started_at: datetime = Field(..., description="Session start time")
    last_activity_at: Optional[datetime] = Field(None, description="Latest transcript activity timestamp")
    transcript_revision: int = Field(..., description="Current transcript revision")
    provider: str = Field(..., description="Session provider")
    project: Optional[str] = Field(None, description="Project name")
    is_sidechain: bool = Field(False, description="True when session is a task/sub-agent session")


class SessionArchiveManifestResponse(BaseModel):
    sessions: list[SessionArchiveManifestItemResponse] = Field(..., description="Archive-eligible sessions")
    total: int = Field(..., ge=0, description="Total matching sessions")


class SessionArchiveBundleResponse(UTCBaseModel):
    bundle_version: int = Field(..., description="Archive bundle schema version")
    exported_at: datetime = Field(..., description="When the bundle was generated")
    session: SessionArchiveSessionResponse = Field(..., description="Session metadata")
    archive: SessionArchivePayloadResponse = Field(..., description="Encoded raw archive payload")


def _encode_jsonl_payload(jsonl_bytes: bytes) -> tuple[str, str]:
    raw_sha = hashlib.sha256(jsonl_bytes).hexdigest()
    compressed = gzip.compress(jsonl_bytes, mtime=0)
    encoded = base64.b64encode(compressed).decode("ascii")
    return raw_sha, encoded


def build_session_archive_bundle(
    db: Session,
    session_id: UUID,
    *,
    branch_mode: str = "head",
) -> SessionArchiveBundleResponse | None:
    """Build a versioned archive bundle for the current head transcript."""
    if branch_mode != "head":
        raise ValueError("branch_mode must be 'head' for archive bundle export")

    result = AgentsStore(db).export_session_jsonl(session_id, branch_mode=branch_mode)
    if result is None:
        return None

    jsonl_bytes, session = result
    payload_sha, encoded_payload = _encode_jsonl_payload(jsonl_bytes)
    kernel_projection = project_session_kernel_fields(db, session)
    lineage_projection = kernel_projection.lineage
    capabilities = kernel_projection.capabilities

    return SessionArchiveBundleResponse(
        bundle_version=BUNDLE_VERSION,
        exported_at=datetime.now(timezone.utc),
        session=SessionArchiveSessionResponse(
            id=str(session.id),
            provider=session.provider,
            provider_session_id=kernel_projection.provider_session_id,
            project=session.project,
            device_id=session.device_id,
            device_name=session.device_name,
            cwd=session.cwd,
            git_repo=session.git_repo,
            git_branch=session.git_branch,
            started_at=session.started_at,
            ended_at=session.ended_at,
            last_activity_at=session.last_activity_at,
            thread_root_session_id=lineage_projection.thread_root_session_id,
            continued_from_session_id=lineage_projection.continued_from_session_id,
            continuation_kind=lineage_projection.continuation_kind,
            origin_label=lineage_projection.origin_label,
            execution_home=capabilities.execution_home.value,
            managed_transport=(capabilities.managed_transport.value if capabilities.managed_transport else None),
            summary_title=session.summary_title,
            summary=session.summary,
            transcript_revision=int(getattr(session, "transcript_revision", 0) or 0),
            summary_revision=int(getattr(session, "summary_revision", 0) or 0),
            embedding_revision=int(getattr(session, "embedding_revision", 0) or 0),
            is_sidechain=lineage_projection.is_sidechain,
        ),
        archive=SessionArchivePayloadResponse(
            format="jsonl",
            branch_mode=branch_mode,
            sha256=payload_sha,
            bytes=len(jsonl_bytes),
            jsonl_b64_gzip=encoded_payload,
        ),
    )


def build_session_archive_manifest_item(db: Session, session) -> SessionArchiveManifestItemResponse:
    lineage_projection = project_session_lineage_fields(db, session)
    return SessionArchiveManifestItemResponse(
        id=str(session.id),
        started_at=session.started_at,
        last_activity_at=session.last_activity_at,
        transcript_revision=int(getattr(session, "transcript_revision", 0) or 0),
        provider=session.provider,
        project=session.project,
        is_sidechain=lineage_projection.is_sidechain,
    )


async def build_storage_v2_archive_bundle(
    *,
    session_id: UUID,
    owner_id: int,
    branch_mode: str = "head",
) -> SessionArchiveBundleResponse | None:
    """Build the Life Hub bundle from catalogd and immutable raw objects."""

    if branch_mode != "head":
        raise ValueError("branch_mode must be 'head' for archive bundle export")
    catalog = get_catalogd_client()
    if catalog is None:
        raise CatalogUnavailable("catalogd is unavailable")
    storage_result = await catalog.call("storage.session.read.v2", {"session_id": str(session_id)})
    storage_session = storage_result.get("session")
    if not isinstance(storage_session, dict) or str(storage_session.get("owner_id")) != str(owner_id):
        return None
    live_result = await catalog.call("session.read.v2", {"session_id": str(session_id)})
    facts = live_result.get("facts")
    observed_at = decode_catalog_datetime(live_result.get("observed_at"))
    if not isinstance(facts, dict) or observed_at is None:
        raise RuntimeError("catalog omitted session facts")
    projected = project_catalog_session_facts(facts, observed_at=observed_at)

    payload = bytearray()
    after_source_key: str | None = None
    workers = get_raw_object_worker_pool()
    while True:
        manifest = await catalog.call(
            "storage.session.raw_manifest.v2",
            {
                "session_id": str(session_id),
                "owner_id": str(owner_id),
                "after_source_key": after_source_key,
                "limit": 8,
            },
        )
        objects = manifest.get("objects")
        if not isinstance(objects, list):
            raise RuntimeError("catalog returned an invalid raw manifest")
        if not objects:
            break
        for item in objects:
            if not isinstance(item, dict):
                raise RuntimeError("catalog returned an invalid raw object")
            try:
                decoded = await workers.read(str(item["object_path"]), str(item["object_hash"]), str(item["tenant_id"]))
            except (KeyError, RawObjectCorruptError, RawObjectWorkerError) as exc:
                raise RawObjectWorkerError("immutable raw object could not be verified") from exc
            if decoded.envelope_id != item.get("envelope_id") or decoded.spec.session_id != session_id:
                raise RawObjectCorruptError("raw object does not match its catalog manifest")
            for record in decoded.spec.records:
                payload.extend(record.data)
                if not record.data.endswith(b"\n"):
                    payload.extend(b"\n")
        after_source_key = _storage_source_key(objects[-1])
        if manifest.get("objects_truncated") is not True:
            break

    payload_sha, encoded_payload = _encode_jsonl_payload(bytes(payload))
    catalog_facts = facts.get("catalog") if isinstance(facts.get("catalog"), dict) else {}
    connections = facts.get("connections") if isinstance(facts.get("connections"), list) else []
    managed_transport = next(
        (str(item["control_plane"]) for item in connections if isinstance(item, dict) and item.get("control_plane")),
        None,
    )
    provider_alias = facts.get("provider_alias")
    return SessionArchiveBundleResponse(
        bundle_version=BUNDLE_VERSION,
        exported_at=datetime.now(timezone.utc),
        session=SessionArchiveSessionResponse(
            id=str(session_id),
            provider=str(storage_session["provider"]),
            provider_session_id=str(provider_alias) if provider_alias else None,
            project=storage_session.get("project"),
            device_id=storage_session.get("machine_id"),
            device_name=catalog_facts.get("device_name"),
            cwd=storage_session.get("cwd"),
            git_repo=storage_session.get("git_repo"),
            git_branch=storage_session.get("git_branch"),
            started_at=storage_session["started_at"],
            ended_at=storage_session.get("ended_at"),
            last_activity_at=storage_session.get("last_activity_at"),
            thread_root_session_id=projected.thread_root_session_id,
            continued_from_session_id=projected.continued_from_session_id,
            continuation_kind=projected.continuation_kind,
            origin_label=projected.origin_label,
            execution_home=str(getattr(projected.session_state.mode, "value", projected.session_state.mode)),
            managed_transport=managed_transport,
            summary_title=storage_session.get("summary_title"),
            summary=catalog_facts.get("summary"),
            transcript_revision=int(storage_session.get("transcript_revision") or 0),
            summary_revision=int(catalog_facts.get("summary_revision") or 0),
            embedding_revision=0,
            is_sidechain=projected.is_sidechain,
        ),
        archive=SessionArchivePayloadResponse(
            format="jsonl",
            branch_mode=branch_mode,
            sha256=payload_sha,
            bytes=len(payload),
            jsonl_b64_gzip=encoded_payload,
        ),
    )


def _storage_source_key(item: dict[str, object]) -> str:
    import json

    return json.dumps(
        [
            item["machine_id"],
            item["provider"],
            item["opaque_source_id"],
            item["source_epoch"],
            f"{int(item['range_start']):020d}",
            item["envelope_id"],
        ],
        separators=(",", ":"),
    )


def build_storage_v2_archive_manifest(snapshot: dict[str, object]) -> SessionArchiveManifestResponse:
    """Project one catalogd timeline snapshot into the archive manifest contract."""

    observed_at = decode_catalog_datetime(snapshot.get("observed_at"))
    rows = snapshot.get("rows")
    if observed_at is None or not isinstance(rows, list):
        raise RuntimeError("catalog returned an invalid archive manifest snapshot")
    sessions: list[SessionArchiveManifestItemResponse] = []
    for row in rows:
        facts = row.get("facts") if isinstance(row, dict) else None
        catalog_facts = facts.get("catalog") if isinstance(facts, dict) else None
        if not isinstance(facts, dict) or not isinstance(catalog_facts, dict):
            raise RuntimeError("catalog archive manifest row is incomplete")
        projected = project_catalog_session_facts(facts, observed_at=observed_at)
        sessions.append(
            SessionArchiveManifestItemResponse(
                id=projected.id,
                started_at=projected.started_at,
                last_activity_at=projected.last_activity_at,
                transcript_revision=int(catalog_facts.get("transcript_revision") or 0),
                provider=projected.provider,
                project=projected.project,
                is_sidechain=projected.is_sidechain,
            )
        )
    return SessionArchiveManifestResponse(sessions=sessions, total=int(snapshot.get("total") or 0))


__all__ = [
    "BUNDLE_VERSION",
    "SessionArchiveBundleResponse",
    "SessionArchiveManifestItemResponse",
    "SessionArchiveManifestResponse",
    "build_session_archive_manifest_item",
    "build_session_archive_bundle",
    "build_storage_v2_archive_bundle",
    "build_storage_v2_archive_manifest",
]
