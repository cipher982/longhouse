"""API router for Knowledge Base (Phase 0)."""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.orm import Session

from zerg.crud import knowledge_crud
from zerg.database import get_db
from zerg.dependencies.auth import get_current_user
from zerg.models.models import User
from zerg.schemas.schemas import (
    KnowledgeDocument,
    KnowledgeSearchResult,
    KnowledgeSource,
    KnowledgeSourceCreate,
    KnowledgeSourceUpdate,
)
from zerg.services import knowledge_sync_service

logger = logging.getLogger(__name__)

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

    Phase 0 only supports source_type="url".
    """
    # Validate source type
    if source_in.source_type not in {"url"}:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported source_type: {source_in.source_type}. Phase 0 only supports 'url'.",
        )

    # Validate URL source config
    if source_in.source_type == "url":
        if "url" not in source_in.config:
            raise HTTPException(
                status_code=400,
                detail="URL source requires 'url' in config",
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


@router.post("/sources/{source_id}/sync", status_code=status.HTTP_202_ACCEPTED)
async def sync_source(
    *,
    source_id: int = Path(..., gt=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Trigger immediate sync for a knowledge source."""
    source = knowledge_crud.get_knowledge_source(db, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Knowledge source not found")

    # Check ownership
    if source.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Trigger sync
    try:
        await knowledge_sync_service.sync_knowledge_source(db, source_id)
        return {"status": "syncing", "source_id": source_id}
    except Exception as exc:
        logger.error(f"Failed to sync source {source_id}: {exc}")
        raise HTTPException(
            status_code=500,
            detail=f"Sync failed: {str(exc)}",
        )


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

        formatted_results.append(
            KnowledgeSearchResult(
                source_name=source.name,
                source_id=source.id,
                document_id=doc.id,
                path=doc.path,
                title=doc.title,
                snippets=snippets,
                score=1.0,  # Phase 0: no relevance scoring
            )
        )

    return formatted_results
