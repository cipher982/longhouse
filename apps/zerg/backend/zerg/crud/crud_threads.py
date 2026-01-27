"""CRUD operations for Threads."""

from typing import Any
from typing import Dict
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from zerg.models import Fiche
from zerg.models import Thread
from zerg.utils.time import utc_now_naive


def get_threads(
    db: Session,
    owner_id: Optional[int] = None,
    fiche_id: Optional[int] = None,
    thread_type: Optional[str] = None,
    title: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    *,
    include_messages: bool = False,
):
    """Get threads, optionally filtered by fiche_id, thread_type, and/or title"""
    query = db.query(Thread)
    if include_messages:
        query = query.options(selectinload(Thread.messages))
    if owner_id is not None:
        query = query.join(Fiche, Fiche.id == Thread.fiche_id).filter(Fiche.owner_id == owner_id)
    if fiche_id is not None:
        query = query.filter(Thread.fiche_id == fiche_id)
    if thread_type is not None:
        query = query.filter(Thread.thread_type == thread_type)
    if title is not None:
        query = query.filter(Thread.title == title)
    return query.order_by(Thread.created_at.desc()).offset(skip).limit(limit).all()


def get_active_thread(db: Session, fiche_id: int):
    """Get the active thread for a fiche, if it exists"""
    return db.query(Thread).filter(Thread.fiche_id == fiche_id, Thread.active).first()


def get_thread(db: Session, thread_id: int):
    """Get a specific thread by ID"""
    return db.query(Thread).options(selectinload(Thread.messages)).filter(Thread.id == thread_id).first()


def create_thread(
    db: Session,
    fiche_id: int,
    title: str,
    active: bool = True,
    fiche_state: Optional[Dict[str, Any]] = None,
    memory_strategy: Optional[str] = "buffer",
    thread_type: Optional[str] = "chat",
):
    """Create a new thread for a fiche"""
    # If this is set as active, deactivate any other active threads
    if active:
        db.query(Thread).filter(Thread.fiche_id == fiche_id, Thread.active).update({"active": False})

    db_thread = Thread(
        fiche_id=fiche_id,
        title=title,
        active=active,
        fiche_state=fiche_state,
        memory_strategy=memory_strategy,
        thread_type=thread_type,
    )
    db.add(db_thread)
    db.commit()
    db.refresh(db_thread)
    return db_thread


def update_thread(
    db: Session,
    thread_id: int,
    title: Optional[str] = None,
    active: Optional[bool] = None,
    fiche_state: Optional[Dict[str, Any]] = None,
    memory_strategy: Optional[str] = None,
    thread_type: Optional[str] = None,
):
    """Update a thread"""
    db_thread = db.query(Thread).filter(Thread.id == thread_id).first()
    if db_thread is None:
        return None

    # Update provided fields
    if title is not None:
        db_thread.title = title
    if active is not None:
        if active:
            # Deactivate other threads for this fiche
            db.query(Thread).filter(Thread.fiche_id == db_thread.fiche_id, Thread.id != thread_id).update({"active": False})
        db_thread.active = active
    if fiche_state is not None:
        db_thread.fiche_state = fiche_state
    if memory_strategy is not None:
        db_thread.memory_strategy = memory_strategy
    if thread_type is not None:
        db_thread.thread_type = thread_type

    db_thread.updated_at = utc_now_naive()
    db.commit()
    db.refresh(db_thread)
    return db_thread


def delete_thread(db: Session, thread_id: int):
    """Delete a thread and all its messages"""
    db_thread = db.query(Thread).filter(Thread.id == thread_id).first()
    if db_thread is None:
        return False
    db.delete(db_thread)
    db.commit()
    return True
