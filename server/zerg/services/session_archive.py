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

from zerg.services.agents import AgentsStore
from zerg.services.agents.kernel_capabilities import project_session_capabilities
from zerg.services.session_kernel_projection import project_provider_session_id
from zerg.services.session_kernel_projection import project_session_lineage_fields
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
    continuation_kind: Optional[str] = Field(None, description="Continuation kind: local|cloud|runner")
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
    lineage_projection = project_session_lineage_fields(db, session)
    capabilities = project_session_capabilities(db, session_id=session.id)

    return SessionArchiveBundleResponse(
        bundle_version=BUNDLE_VERSION,
        exported_at=datetime.now(timezone.utc),
        session=SessionArchiveSessionResponse(
            id=str(session.id),
            provider=session.provider,
            provider_session_id=project_provider_session_id(db, session),
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


__all__ = [
    "BUNDLE_VERSION",
    "SessionArchiveBundleResponse",
    "SessionArchiveManifestItemResponse",
    "SessionArchiveManifestResponse",
    "build_session_archive_manifest_item",
    "build_session_archive_bundle",
]
