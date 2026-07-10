"""Agents API for cheap source-line reconciliation."""

from __future__ import annotations

import re
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from pydantic import BaseModel
from sqlalchemy import tuple_
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.models.agents import AgentSessionBranch
from zerg.models.agents import AgentSourceLine
from zerg.services.archive_transcript import ArchiveTranscriptUnavailable
from zerg.services.archive_transcript import load_session_source_line_bytes
from zerg.services.raw_json_compression import decode_raw_json
from zerg.services.session_archive import build_session_archive_bundle

router = APIRouter(prefix="/agents/source-lines", tags=["agents"])

_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_PROOF_VERSION = "head-archive-bundle-ro-v1"


class SourceLineClaimItem(BaseModel):
    session_id: UUID
    source_path: str
    source_offset: int
    line_hash: str


class SourceLineClaimsRequest(BaseModel):
    items: list[SourceLineClaimItem]


class SourceLineClaimResponseItem(BaseModel):
    source_path: str
    source_offset: int
    line_hash: str


class SourceLineRejectedItem(BaseModel):
    source_path: str | None = None
    source_offset: int | None = None
    line_hash: str | None = None
    reason: str


class SourceLineClaimsResponse(BaseModel):
    proof_version: str = _PROOF_VERSION
    present: list[SourceLineClaimResponseItem]
    missing: list[SourceLineClaimResponseItem]
    rejected: list[SourceLineRejectedItem]


def _normalized_claim(item: SourceLineClaimItem) -> tuple[SourceLineClaimResponseItem | None, str | None]:
    source_path = item.source_path.strip()
    line_hash = item.line_hash.strip().lower()
    if not source_path:
        return None, "missing_source_path"
    if item.source_offset < 0:
        return None, "invalid_source_offset"
    if not _SHA256_RE.fullmatch(line_hash):
        return None, "invalid_line_hash"
    return (
        SourceLineClaimResponseItem(
            source_path=source_path,
            source_offset=int(item.source_offset),
            line_hash=line_hash,
        ),
        None,
    )


@router.post(
    "/claims",
    response_model=SourceLineClaimsResponse,
    dependencies=[Depends(verify_agents_token), Depends(require_single_tenant)],
)
async def create_source_line_claims(
    request: SourceLineClaimsRequest,
    db: Session = Depends(get_db),
) -> SourceLineClaimsResponse:
    """Return which source-line identities are already durable on this host."""

    if len(request.items) > 512:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="too many source-line claim items")

    present: list[SourceLineClaimResponseItem] = []
    missing: list[SourceLineClaimResponseItem] = []
    rejected: list[SourceLineRejectedItem] = []
    valid: list[tuple[SourceLineClaimItem, SourceLineClaimResponseItem]] = []

    for item in request.items:
        normalized, error = _normalized_claim(item)
        if error is not None or normalized is None:
            rejected.append(
                SourceLineRejectedItem(
                    source_path=item.source_path,
                    source_offset=item.source_offset,
                    line_hash=item.line_hash,
                    reason=error or "invalid_claim",
                )
            )
            continue
        valid.append((item, normalized))

    if not valid:
        return SourceLineClaimsResponse(present=present, missing=missing, rejected=rejected)

    identities = {(item.session_id, normalized.source_path, normalized.source_offset, normalized.line_hash) for item, normalized in valid}
    rows = (
        db.query(AgentSourceLine)
        .filter(
            tuple_(
                AgentSourceLine.session_id,
                AgentSourceLine.source_path,
                AgentSourceLine.source_offset,
                AgentSourceLine.line_hash,
            ).in_(identities)
        )
        .all()
    )
    session_ids = {item.session_id for item, _normalized in valid}
    head_branch_ids = dict(
        db.query(AgentSessionBranch.session_id, AgentSessionBranch.id)
        .filter(AgentSessionBranch.session_id.in_(session_ids))
        .filter(AgentSessionBranch.is_head == 1)
        .all()
    )

    durable: set[tuple[UUID, str, int, str]] = set()
    slim_sessions: set[UUID] = set()
    for row in rows:
        head_branch_id = head_branch_ids.get(row.session_id)
        if head_branch_id is not None and int(row.branch_id) != int(head_branch_id):
            continue
        identity = (row.session_id, row.source_path, int(row.source_offset), row.line_hash)
        try:
            raw_json = decode_raw_json(row)
        except Exception:
            raw_json = None
        if raw_json:
            durable.add(identity)
        else:
            slim_sessions.add(row.session_id)

    for session_id in slim_sessions:
        archived = load_session_source_line_bytes(db, session_id)
        durable.update((session_id, source_path, source_offset, line_hash) for source_path, source_offset, line_hash in archived)

    # Use the exact Life Hub bundle builder as the final authority. A matching
    # row is not durable proof when the selected bundle still cannot be built.
    for session_id in {identity[0] for identity in durable}:
        try:
            bundle = build_session_archive_bundle(db, session_id, branch_mode="head")
        except ArchiveTranscriptUnavailable:
            bundle = None
        if bundle is None:
            durable = {identity for identity in durable if identity[0] != session_id}

    for item, normalized in valid:
        identity = (item.session_id, normalized.source_path, normalized.source_offset, normalized.line_hash)
        (present if identity in durable else missing).append(normalized)

    return SourceLineClaimsResponse(present=present, missing=missing, rejected=rejected)
