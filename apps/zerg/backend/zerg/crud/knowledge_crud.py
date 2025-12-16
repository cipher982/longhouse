"""CRUD operations for Knowledge Base (Phase 0)."""

import hashlib
from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session, selectinload

from zerg.models.models import KnowledgeDocument, KnowledgeSource
from zerg.utils.time import utc_now_naive


# ---------------------------------------------------------------------------
# KnowledgeSource CRUD
# ---------------------------------------------------------------------------


def create_knowledge_source(
    db: Session,
    *,
    owner_id: int,
    name: str,
    source_type: str,
    config: dict,
    sync_schedule: Optional[str] = None,
) -> KnowledgeSource:
    """Create a new knowledge source.

    Args:
        db: Database session
        owner_id: User ID who owns this source
        name: User-friendly source name
        source_type: Type of source ("url", "git_repo", etc.)
        config: Type-specific configuration dict
        sync_schedule: Optional cron expression for automatic sync

    Returns:
        Created KnowledgeSource instance
    """
    source = KnowledgeSource(
        owner_id=owner_id,
        name=name,
        source_type=source_type,
        config=config,
        sync_schedule=sync_schedule,
        sync_status="pending",
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


def get_knowledge_source(db: Session, source_id: int) -> Optional[KnowledgeSource]:
    """Get a knowledge source by ID.

    Args:
        db: Database session
        source_id: Source ID to retrieve

    Returns:
        KnowledgeSource instance or None if not found
    """
    return db.query(KnowledgeSource).filter(KnowledgeSource.id == source_id).first()


def get_knowledge_sources(
    db: Session,
    *,
    owner_id: int,
    skip: int = 0,
    limit: int = 100,
) -> List[KnowledgeSource]:
    """List knowledge sources for a user.

    Args:
        db: Database session
        owner_id: User ID to filter by
        skip: Number of records to skip
        limit: Maximum number of records to return

    Returns:
        List of KnowledgeSource instances
    """
    return (
        db.query(KnowledgeSource)
        .filter(KnowledgeSource.owner_id == owner_id)
        .order_by(KnowledgeSource.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


def update_knowledge_source(
    db: Session,
    source_id: int,
    *,
    name: Optional[str] = None,
    config: Optional[dict] = None,
    sync_schedule: Optional[str] = None,
) -> Optional[KnowledgeSource]:
    """Update a knowledge source.

    Args:
        db: Database session
        source_id: Source ID to update
        name: New name (optional)
        config: New config dict (optional)
        sync_schedule: New sync schedule (optional)

    Returns:
        Updated KnowledgeSource instance or None if not found
    """
    source = get_knowledge_source(db, source_id)
    if source is None:
        return None

    if name is not None:
        source.name = name
    if config is not None:
        source.config = config
    if sync_schedule is not None:
        source.sync_schedule = sync_schedule

    source.updated_at = utc_now_naive()
    db.commit()
    db.refresh(source)
    return source


def delete_knowledge_source(db: Session, source_id: int) -> bool:
    """Delete a knowledge source and all its documents.

    Args:
        db: Database session
        source_id: Source ID to delete

    Returns:
        True if deleted, False if not found
    """
    source = get_knowledge_source(db, source_id)
    if source is None:
        return False

    db.delete(source)
    db.commit()
    return True


def update_source_sync_status(
    db: Session,
    source_id: int,
    *,
    status: str,
    error: Optional[str] = None,
    last_synced_at: Optional[datetime] = None,
) -> Optional[KnowledgeSource]:
    """Update sync status for a knowledge source.

    Args:
        db: Database session
        source_id: Source ID to update
        status: Sync status ("pending", "success", "failed")
        error: Error message if status is "failed"
        last_synced_at: Timestamp of last sync (defaults to now)

    Returns:
        Updated KnowledgeSource instance or None if not found
    """
    source = get_knowledge_source(db, source_id)
    if source is None:
        return None

    source.sync_status = status
    source.sync_error = error
    source.last_synced_at = last_synced_at or utc_now_naive()
    source.updated_at = utc_now_naive()

    db.commit()
    db.refresh(source)
    return source


def get_sources_due_for_sync(db: Session) -> List[KnowledgeSource]:
    """Get all knowledge sources that have a sync schedule and are due for sync.

    Args:
        db: Database session

    Returns:
        List of KnowledgeSource instances that need syncing
    """
    # For Phase 0, just return all sources with a schedule
    # In production, we'd check APScheduler or compute next run time
    return (
        db.query(KnowledgeSource)
        .filter(KnowledgeSource.sync_schedule.isnot(None))
        .all()
    )


# ---------------------------------------------------------------------------
# KnowledgeDocument CRUD
# ---------------------------------------------------------------------------


def upsert_knowledge_document(
    db: Session,
    *,
    source_id: int,
    owner_id: int,
    path: str,
    content_text: str,
    title: Optional[str] = None,
    doc_metadata: Optional[dict] = None,
) -> KnowledgeDocument:
    """Create or update a knowledge document.

    Uses the unique constraint on (source_id, path) to detect existing docs.

    Args:
        db: Database session
        source_id: Source ID this document belongs to
        owner_id: User ID who owns this document
        path: Document path/URL (unique per source)
        content_text: Text content of the document
        title: Optional document title
        doc_metadata: Optional metadata dict

    Returns:
        Created or updated KnowledgeDocument instance
    """
    # Compute content hash
    content_hash = hashlib.sha256(content_text.encode()).hexdigest()

    # Try to find existing document
    existing = (
        db.query(KnowledgeDocument)
        .filter(
            KnowledgeDocument.source_id == source_id,
            KnowledgeDocument.path == path,
        )
        .first()
    )

    now = utc_now_naive()

    if existing:
        # Update existing document
        existing.content_text = content_text
        existing.content_hash = content_hash
        existing.title = title
        existing.doc_metadata = doc_metadata or {}
        existing.fetched_at = now
        db.commit()
        db.refresh(existing)
        return existing
    else:
        # Create new document
        doc = KnowledgeDocument(
            source_id=source_id,
            owner_id=owner_id,
            path=path,
            content_text=content_text,
            content_hash=content_hash,
            title=title,
            doc_metadata=doc_metadata or {},
            fetched_at=now,
        )
        db.add(doc)
        db.commit()
        db.refresh(doc)
        return doc


def get_knowledge_document(db: Session, document_id: int) -> Optional[KnowledgeDocument]:
    """Get a knowledge document by ID.

    Args:
        db: Database session
        document_id: Document ID to retrieve

    Returns:
        KnowledgeDocument instance or None if not found
    """
    return db.query(KnowledgeDocument).filter(KnowledgeDocument.id == document_id).first()


def get_knowledge_documents(
    db: Session,
    *,
    owner_id: int,
    source_id: Optional[int] = None,
    skip: int = 0,
    limit: int = 100,
) -> List[KnowledgeDocument]:
    """List knowledge documents for a user.

    Args:
        db: Database session
        owner_id: User ID to filter by
        source_id: Optional source ID to filter by
        skip: Number of records to skip
        limit: Maximum number of records to return

    Returns:
        List of KnowledgeDocument instances
    """
    query = db.query(KnowledgeDocument).filter(KnowledgeDocument.owner_id == owner_id)

    if source_id is not None:
        query = query.filter(KnowledgeDocument.source_id == source_id)

    return (
        query
        .order_by(KnowledgeDocument.fetched_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


def delete_knowledge_document(db: Session, document_id: int) -> bool:
    """Delete a knowledge document.

    Args:
        db: Database session
        document_id: Document ID to delete

    Returns:
        True if deleted, False if not found
    """
    doc = get_knowledge_document(db, document_id)
    if doc is None:
        return False

    db.delete(doc)
    db.commit()
    return True


def search_knowledge_documents(
    db: Session,
    *,
    owner_id: int,
    query: str,
    limit: int = 10,
) -> List[tuple[KnowledgeDocument, KnowledgeSource]]:
    """Search knowledge documents for a user using keyword search.

    Phase 0 implementation: simple case-insensitive substring search.
    Phase 2 will add semantic search with embeddings.

    Args:
        db: Database session
        owner_id: User ID to filter by
        query: Search query string
        limit: Maximum number of results to return

    Returns:
        List of (KnowledgeDocument, KnowledgeSource) tuples
    """
    # Simple case-insensitive LIKE search
    # In PostgreSQL, use ILIKE; in SQLite, LIKE is case-insensitive
    search_pattern = f"%{query}%"

    results = (
        db.query(KnowledgeDocument, KnowledgeSource)
        .join(KnowledgeSource, KnowledgeDocument.source_id == KnowledgeSource.id)
        .filter(
            KnowledgeDocument.owner_id == owner_id,
            KnowledgeDocument.content_text.ilike(search_pattern),
        )
        .order_by(KnowledgeDocument.fetched_at.desc())
        .limit(limit)
        .all()
    )

    return results
