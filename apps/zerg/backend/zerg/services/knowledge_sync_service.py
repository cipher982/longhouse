"""Knowledge Sync Service - fetches and syncs knowledge sources (Phase 0)."""

import logging
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from zerg.crud import knowledge_crud
from zerg.models.models import KnowledgeSource

logger = logging.getLogger(__name__)


async def sync_url_source(db: Session, source: KnowledgeSource) -> None:
    """Fetch URL content and store as KnowledgeDocument.

    Phase 0 implementation: fetches a single URL and stores its content.

    Args:
        db: Database session
        source: KnowledgeSource to sync (must be type="url")

    Raises:
        ValueError: If source_type is not "url"
        httpx.HTTPError: If HTTP request fails
    """
    if source.source_type != "url":
        raise ValueError(f"Expected source_type='url', got '{source.source_type}'")

    url = source.config.get("url")
    if not url:
        raise ValueError("URL source missing 'url' in config")

    logger.info(f"Syncing URL source {source.id}: {url}")

    # Prepare headers with optional auth
    headers = {}
    if auth := source.config.get("auth_header"):
        headers["Authorization"] = auth

    try:
        # Fetch URL content
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            content = response.text

        # Extract title from URL (last path segment or hostname)
        from urllib.parse import urlparse
        parsed = urlparse(url)
        title = parsed.path.strip("/").split("/")[-1] if parsed.path.strip("/") else parsed.hostname

        # Upsert document
        doc = knowledge_crud.upsert_knowledge_document(
            db,
            source_id=source.id,
            owner_id=source.owner_id,
            path=url,
            content_text=content,
            title=title,
            metadata={
                "content_type": response.headers.get("content-type"),
                "status_code": response.status_code,
                "content_length": len(content),
            },
        )

        # Update source sync status
        knowledge_crud.update_source_sync_status(
            db,
            source.id,
            status="success",
        )

        logger.info(f"Successfully synced URL source {source.id}, document {doc.id}")

    except httpx.HTTPError as exc:
        error_msg = f"HTTP error fetching {url}: {exc}"
        logger.error(error_msg)

        # Update source with error
        knowledge_crud.update_source_sync_status(
            db,
            source.id,
            status="failed",
            error=error_msg,
        )

        raise

    except Exception as exc:
        error_msg = f"Unexpected error syncing {url}: {exc}"
        logger.exception(error_msg)

        # Update source with error
        knowledge_crud.update_source_sync_status(
            db,
            source.id,
            status="failed",
            error=error_msg,
        )

        raise


async def sync_knowledge_source(db: Session, source_id: int) -> None:
    """Sync a knowledge source by ID.

    Dispatches to the appropriate sync function based on source_type.

    Args:
        db: Database session
        source_id: KnowledgeSource ID to sync

    Raises:
        ValueError: If source not found or unsupported type
    """
    source = knowledge_crud.get_knowledge_source(db, source_id)
    if source is None:
        raise ValueError(f"Knowledge source {source_id} not found")

    logger.info(f"Starting sync for source {source_id} ({source.source_type})")

    if source.source_type == "url":
        await sync_url_source(db, source)
    else:
        raise ValueError(f"Unsupported source_type: {source.source_type}")


async def sync_all_sources(db: Session, owner_id: int) -> dict:
    """Sync all knowledge sources for a user.

    Args:
        db: Database session
        owner_id: User ID whose sources to sync

    Returns:
        Dictionary with sync statistics
    """
    sources = knowledge_crud.get_knowledge_sources(db, owner_id=owner_id, limit=1000)

    stats = {
        "total": len(sources),
        "success": 0,
        "failed": 0,
        "errors": [],
    }

    for source in sources:
        try:
            await sync_knowledge_source(db, source.id)
            stats["success"] += 1
        except Exception as exc:
            stats["failed"] += 1
            stats["errors"].append({
                "source_id": source.id,
                "source_name": source.name,
                "error": str(exc),
            })
            logger.error(f"Failed to sync source {source.id}: {exc}")

    logger.info(f"Synced {stats['success']}/{stats['total']} sources for user {owner_id}")
    return stats
