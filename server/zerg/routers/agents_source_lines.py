"""Agents API for cheap source-line reconciliation."""

from __future__ import annotations

import re
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from pydantic import BaseModel
from sqlalchemy import and_
from sqlalchemy import or_
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.models.agents import AgentSessionBranch
from zerg.models.agents import AgentSourceLine
from zerg.services.archive_transcript import load_session_source_line_bytes
from zerg.services.raw_json_compression import decode_raw_json

router = APIRouter(prefix="/agents/source-lines", tags=["agents"])

_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_PROOF_VERSION = "head-source-bytes-ro-v1"


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


def _missing_head_source_identities(
    db: Session,
    session_id: UUID,
    head_branch_id: int | None,
) -> set[tuple[str, int, str]]:
    """Return head rows whose exact raw bytes are absent from the monolith."""
    query = db.query(
        AgentSourceLine.source_path,
        AgentSourceLine.source_offset,
        AgentSourceLine.line_hash,
    ).filter(AgentSourceLine.session_id == session_id)
    if head_branch_id is not None:
        query = query.filter(AgentSourceLine.branch_id == head_branch_id)
    query = query.filter(
        or_(
            and_(AgentSourceLine.raw_json_codec == 1, AgentSourceLine.raw_json_z.is_(None)),
            and_(AgentSourceLine.raw_json_codec != 1, AgentSourceLine.raw_json == ""),
        )
    )
    return {(source_path, int(source_offset), line_hash) for source_path, source_offset, line_hash in query.all()}


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

    session_ids = {item.session_id for item, _normalized in valid}
    head_branch_ids = dict(
        db.query(AgentSessionBranch.session_id, AgentSessionBranch.id)
        .filter(AgentSessionBranch.session_id.in_(session_ids))
        .filter(AgentSessionBranch.is_head == 1)
        .all()
    )
    identities_by_session: dict[UUID, set[tuple[UUID, str, int, str]]] = {}
    for item, normalized in valid:
        identities_by_session.setdefault(item.session_id, set()).add(
            (item.session_id, normalized.source_path, normalized.source_offset, normalized.line_hash)
        )

    # SQLite does not use the source-line indexes for a four-column tuple IN
    # predicate. Group by session and constrain the head branch + offsets so the
    # existing (session_id, branch_id, source_offset) index owns every lookup.
    rows: list[AgentSourceLine] = []
    for session_id, identities in identities_by_session.items():
        query = db.query(AgentSourceLine).filter(AgentSourceLine.session_id == session_id)
        if head_branch_id := head_branch_ids.get(session_id):
            query = query.filter(AgentSourceLine.branch_id == head_branch_id)
        query = query.filter(AgentSourceLine.source_offset.in_({identity[2] for identity in identities}))
        rows.extend(row for row in query.all() if (row.session_id, row.source_path, int(row.source_offset), row.line_hash) in identities)

    durable: set[tuple[UUID, str, int, str]] = set()
    slim_sessions: set[UUID] = set()
    archived_by_session: dict[UUID, dict[tuple[str, int, str], str]] = {}
    for row in rows:
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
        archived_by_session[session_id] = archived
        durable.update((session_id, source_path, source_offset, line_hash) for source_path, source_offset, line_hash in archived)

    # A proof covers the complete selected head, not only the identities in this
    # request. Check only metadata for inline rows; load archive chunks solely
    # when a head row has already had its inline bytes reclaimed. Constructing
    # and gzipping the full transcript here made this cheap claim endpoint a
    # second archive export path.
    for session_id in {identity[0] for identity in durable}:
        missing_inline = _missing_head_source_identities(db, session_id, head_branch_ids.get(session_id))
        if not missing_inline:
            continue
        archived = archived_by_session.get(session_id)
        if archived is None:
            archived = load_session_source_line_bytes(db, session_id)
            archived_by_session[session_id] = archived
        if not missing_inline.issubset(archived):
            durable = {identity for identity in durable if identity[0] != session_id}

    for item, normalized in valid:
        identity = (item.session_id, normalized.source_path, normalized.source_offset, normalized.line_hash)
        (present if identity in durable else missing).append(normalized)

    return SourceLineClaimsResponse(present=present, missing=missing, rejected=rejected)
