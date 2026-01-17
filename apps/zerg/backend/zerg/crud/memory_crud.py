"""CRUD operations for Memory Files."""

from __future__ import annotations

from typing import Iterable
from typing import List

from sqlalchemy.orm import Session

from zerg.models.models import MemoryFile


def upsert_memory_file(
    db: Session,
    *,
    owner_id: int,
    path: str,
    content: str,
    title: str | None = None,
    tags: List[str] | None = None,
    metadata: dict | None = None,
) -> MemoryFile:
    """Create or update a memory file by (owner_id, path)."""
    existing = get_memory_file_by_path(db, owner_id=owner_id, path=path)
    tag_list = tags or []
    meta = metadata or {}

    if existing:
        existing.title = title or existing.title
        existing.content = content
        existing.tags = tag_list
        existing.file_metadata = meta
        db.commit()
        db.refresh(existing)
        return existing

    memory_file = MemoryFile(
        owner_id=owner_id,
        path=path,
        title=title,
        content=content,
        tags=tag_list,
        file_metadata=meta,
    )
    db.add(memory_file)
    db.commit()
    db.refresh(memory_file)
    return memory_file


def get_memory_file_by_path(db: Session, *, owner_id: int, path: str) -> MemoryFile | None:
    """Get a memory file by owner + path."""
    return db.query(MemoryFile).filter(MemoryFile.owner_id == owner_id, MemoryFile.path == path).first()


def get_memory_files_by_ids(db: Session, *, owner_id: int, ids: Iterable[int]) -> List[MemoryFile]:
    """Fetch memory files by id list (owner-scoped)."""
    id_list = list(ids)
    if not id_list:
        return []
    return db.query(MemoryFile).filter(MemoryFile.owner_id == owner_id, MemoryFile.id.in_(id_list)).all()


def list_memory_files(
    db: Session,
    *,
    owner_id: int,
    prefix: str | None = None,
    skip: int = 0,
    limit: int = 100,
) -> List[MemoryFile]:
    """List memory files for a user, optionally filtered by path prefix."""
    query = db.query(MemoryFile).filter(MemoryFile.owner_id == owner_id)
    if prefix:
        query = query.filter(MemoryFile.path.ilike(f"{prefix}%"))
    return query.order_by(MemoryFile.updated_at.desc()).offset(skip).limit(limit).all()


def delete_memory_file(db: Session, *, owner_id: int, path: str) -> bool:
    """Delete a memory file by path."""
    memory_file = get_memory_file_by_path(db, owner_id=owner_id, path=path)
    if not memory_file:
        return False
    db.delete(memory_file)
    db.commit()
    return True


def search_memory_files_keyword(
    db: Session,
    *,
    owner_id: int,
    query: str,
    tags: List[str] | None = None,
    limit: int = 10,
) -> List[MemoryFile]:
    """Keyword search over memory files (ILIKE) with optional tag filter."""
    search_pattern = f"%{query}%"
    rows = (
        db.query(MemoryFile)
        .filter(
            MemoryFile.owner_id == owner_id,
            MemoryFile.content.ilike(search_pattern),
        )
        .order_by(MemoryFile.updated_at.desc())
        .limit(limit * 5)  # fetch extra for tag filtering
        .all()
    )

    if tags:
        tag_set = {t.lower() for t in tags}
        rows = [row for row in rows if any(t.lower() in tag_set for t in (row.tags or []))]

    return rows[:limit]
