"""API router for Knowledge Base."""

import logging
from typing import List
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Path
from fastapi import Query
from fastapi import status
from sqlalchemy.orm import Session

from zerg.connectors.credentials import get_account_credential
from zerg.connectors.credentials import has_account_credential
from zerg.connectors.github_api import github_async_client
from zerg.connectors.registry import ConnectorType
from zerg.crud import knowledge_crud
from zerg.database import get_db
from zerg.dependencies.auth import get_current_user
from zerg.models.models import User
from zerg.schemas.schemas import KnowledgeDocument
from zerg.schemas.schemas import KnowledgeSearchResult
from zerg.schemas.schemas import KnowledgeSource
from zerg.schemas.schemas import KnowledgeSourceCreate
from zerg.schemas.schemas import KnowledgeSourceUpdate
from zerg.services import knowledge_sync_service

logger = logging.getLogger(__name__)

# Supported source types
ALLOWED_SOURCE_TYPES = {"url", "github_repo", "user_text"}

# Allowed URL schemes for URL sources (security: prevent javascript: and other dangerous schemes)
ALLOWED_URL_SCHEMES = {"http", "https"}


def _validate_url_scheme(url: str) -> None:
    """Validate that a URL uses an allowed scheme (http/https only).

    Raises HTTPException if the URL scheme is not allowed.
    This prevents javascript:, data:, and other potentially dangerous schemes.
    """
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
        if parsed.scheme.lower() not in ALLOWED_URL_SCHEMES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid URL scheme '{parsed.scheme}'. Only http and https are allowed.",
            )
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid URL format: {e}",
        )


router = APIRouter(
    prefix="/knowledge",
    tags=["knowledge"],
    dependencies=[Depends(get_current_user)],
)


# ---------------------------------------------------------------------------
# Knowledge Sources CRUD
# ---------------------------------------------------------------------------


@router.post("/sources", response_model=KnowledgeSource, status_code=status.HTTP_201_CREATED)
async def create_source(
    *,
    source_in: KnowledgeSourceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a new knowledge source.

    Supported source_types: url, github_repo
    """
    # Validate source type
    if source_in.source_type not in ALLOWED_SOURCE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported source_type: {source_in.source_type}. " f"Supported types: {', '.join(sorted(ALLOWED_SOURCE_TYPES))}",
        )

    # Validate URL source config
    if source_in.source_type == "url":
        if "url" not in source_in.config:
            raise HTTPException(
                status_code=400,
                detail="URL source requires 'url' in config",
            )
        # Security: Validate URL scheme to prevent javascript:, data:, etc.
        _validate_url_scheme(source_in.config["url"])

    # Validate user_text source config
    if source_in.source_type == "user_text":
        content = source_in.config.get("content")
        if not content or not isinstance(content, str):
            raise HTTPException(
                status_code=400,
                detail="User text source requires non-empty 'content' in config",
            )

    # Validate GitHub repo source config
    if source_in.source_type == "github_repo":
        required_fields = ["owner", "repo"]
        missing = [f for f in required_fields if f not in source_in.config]
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"GitHub repo source missing required fields: {missing}",
            )

        if not has_account_credential(db, current_user.id, ConnectorType.GITHUB):
            raise HTTPException(
                status_code=400,
                detail="GitHub must be connected before adding a GitHub repository source. "
                "Go to Settings > Integrations to connect GitHub.",
            )

    # Create source
    source = knowledge_crud.create_knowledge_source(
        db,
        owner_id=current_user.id,
        name=source_in.name,
        source_type=source_in.source_type,
        config=source_in.config,
        sync_schedule=source_in.sync_schedule,
    )

    # Create immediate document for user_text sources
    if source_in.source_type == "user_text":
        knowledge_crud.upsert_knowledge_document(
            db,
            source_id=source.id,
            owner_id=current_user.id,
            path=f"user_text:{source.id}",
            content_text=source_in.config.get("content", ""),
            title=source_in.name,
            doc_metadata={"source_type": "user_text"},
        )
        knowledge_crud.update_source_sync_status(db, source.id, status="success")

    return source


@router.get("/sources", response_model=List[KnowledgeSource])
async def list_sources(
    *,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List user's knowledge sources."""
    sources = knowledge_crud.get_knowledge_sources(
        db,
        owner_id=current_user.id,
        skip=skip,
        limit=limit,
    )
    return sources


@router.get("/sources/{source_id}", response_model=KnowledgeSource)
async def get_source(
    *,
    source_id: int = Path(..., gt=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get a knowledge source by ID."""
    source = knowledge_crud.get_knowledge_source(db, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Knowledge source not found")

    # Check ownership
    if source.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    return source


@router.put("/sources/{source_id}", response_model=KnowledgeSource)
async def update_source(
    *,
    source_id: int = Path(..., gt=0),
    source_in: KnowledgeSourceUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update a knowledge source."""
    source = knowledge_crud.get_knowledge_source(db, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Knowledge source not found")

    # Check ownership
    if source.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Security: Validate URL scheme if updating a URL source config
    if source.source_type == "url" and source_in.config and "url" in source_in.config:
        _validate_url_scheme(source_in.config["url"])

    # Update source
    updated_source = knowledge_crud.update_knowledge_source(
        db,
        source_id,
        name=source_in.name,
        config=source_in.config,
        sync_schedule=source_in.sync_schedule,
    )

    return updated_source


@router.delete("/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(
    *,
    source_id: int = Path(..., gt=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a knowledge source and all its documents."""
    source = knowledge_crud.get_knowledge_source(db, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Knowledge source not found")

    # Check ownership
    if source.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Delete source
    knowledge_crud.delete_knowledge_source(db, source_id)
    return None


@router.post("/sources/{source_id}/sync", response_model=KnowledgeSource)
async def sync_source(
    *,
    source_id: int = Path(..., gt=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Trigger immediate sync for a knowledge source.

    Note: This endpoint performs a synchronous sync and returns when complete.
    The response contains the updated source with current sync_status.
    """
    source = knowledge_crud.get_knowledge_source(db, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Knowledge source not found")

    # Check ownership
    if source.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Perform sync (sets status to "syncing" then "success"/"failed")
    try:
        await knowledge_sync_service.sync_knowledge_source(db, source_id)
    except Exception as exc:
        logger.error(f"Failed to sync source {source_id}: {exc}")
        # Don't raise - source.sync_status is already set to "failed" by service
        # Fall through to return the updated source

    # Refresh and return updated source
    db.refresh(source)
    return source


# ---------------------------------------------------------------------------
# Knowledge Documents
# ---------------------------------------------------------------------------


@router.get("/documents", response_model=List[KnowledgeDocument])
async def list_documents(
    *,
    source_id: Optional[int] = Query(None, gt=0),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List knowledge documents for the user.

    Optionally filter by source_id.
    """
    documents = knowledge_crud.get_knowledge_documents(
        db,
        owner_id=current_user.id,
        source_id=source_id,
        skip=skip,
        limit=limit,
    )
    return documents


@router.get("/documents/{document_id}", response_model=KnowledgeDocument)
async def get_document(
    *,
    document_id: int = Path(..., gt=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get a knowledge document by ID."""
    document = knowledge_crud.get_knowledge_document(db, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Knowledge document not found")

    # Check ownership
    if document.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    return document


# ---------------------------------------------------------------------------
# Knowledge Search
# ---------------------------------------------------------------------------


@router.get("/search", response_model=List[KnowledgeSearchResult])
async def search(
    *,
    q: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Search across all user's knowledge documents.

    Phase 0: Simple keyword search.
    """
    results = knowledge_crud.search_knowledge_documents(
        db,
        owner_id=current_user.id,
        query=q,
        limit=limit,
    )

    # Format results
    from zerg.tools.builtin.knowledge_tools import extract_snippets

    formatted_results = []
    for doc, source in results:
        snippets = extract_snippets(doc.content_text, q, max_snippets=3)

        # V1.1: Extract permalink from doc_metadata if available
        doc_metadata = doc.doc_metadata or {}
        permalink = doc_metadata.get("github_permalink_url")

        formatted_results.append(
            KnowledgeSearchResult(
                source_name=source.name,
                source_id=source.id,
                document_id=doc.id,
                path=doc.path,
                title=doc.title,
                snippets=snippets,
                score=1.0,  # Phase 0: no relevance scoring
                permalink=permalink,
            )
        )

    return formatted_results


# ---------------------------------------------------------------------------
# GitHub Integration
# ---------------------------------------------------------------------------


@router.get("/github/repos")
async def list_github_repos(
    *,
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List GitHub repositories available to the user (paginated).

    V1: This endpoint mirrors GitHub's /user/repos listing (no global search).
    The UI search box filters locally across loaded pages and uses "Load more".
    """
    creds = get_account_credential(db, current_user.id, ConnectorType.GITHUB)
    if not creds or "token" not in creds:
        raise HTTPException(
            status_code=400,
            detail="GitHub not connected. Go to Settings > Integrations to connect GitHub.",
        )

    token = creds["token"]

    # Build params
    params = {"per_page": per_page, "page": page, "sort": "updated"}

    async with github_async_client(token) as gh:
        response = await gh.get("/user/repos", params=params)
        response.raise_for_status()
        repos_data = response.json()

    repos = []
    for repo in repos_data:
        repos.append(
            {
                "full_name": repo["full_name"],
                "owner": repo["owner"]["login"],
                "name": repo["name"],
                "private": repo["private"],
                "default_branch": repo["default_branch"],
                "description": repo["description"],
                "updated_at": repo["updated_at"],
            }
        )

    # Check if there are more pages via Link header
    link_header = response.headers.get("Link", "")
    has_more = 'rel="next"' in link_header

    return {
        "repositories": repos,
        "page": page,
        "per_page": per_page,
        "has_more": has_more,
    }


@router.get("/github/repos/{owner}/{repo}/branches")
async def list_repo_branches(
    *,
    owner: str = Path(...),
    repo: str = Path(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List branches for a GitHub repository."""
    creds = get_account_credential(db, current_user.id, ConnectorType.GITHUB)
    if not creds or "token" not in creds:
        raise HTTPException(status_code=400, detail="GitHub not connected")

    token = creds["token"]

    async with github_async_client(token) as gh:
        # Get repo info for default branch
        repo_resp = await gh.get(f"/repos/{owner}/{repo}")
        if repo_resp.status_code == 404:
            raise HTTPException(status_code=404, detail="Repository not found or not accessible")
        repo_resp.raise_for_status()
        default_branch = repo_resp.json()["default_branch"]

        # Get branches
        branches_resp = await gh.get(f"/repos/{owner}/{repo}/branches", params={"per_page": 100})
        branches_resp.raise_for_status()

    branches = []
    for b in branches_resp.json():
        branches.append(
            {
                "name": b["name"],
                "protected": b.get("protected", False),
                "is_default": b["name"] == default_branch,
            }
        )

    # Sort so default branch is first
    branches.sort(key=lambda x: (not x["is_default"], x["name"]))

    return {"branches": branches}
