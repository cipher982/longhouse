"""CRUD operations for user skills (DB-backed SKILL.md)."""

from __future__ import annotations

from typing import List

from sqlalchemy.orm import Session

from zerg.models.models import UserSkill


def get_user_skill_by_name(db: Session, *, owner_id: int, name: str) -> UserSkill | None:
    """Fetch a user skill by owner + name."""
    return db.query(UserSkill).filter(UserSkill.owner_id == owner_id, UserSkill.name == name).first()


def list_user_skills(
    db: Session,
    *,
    owner_id: int,
    include_inactive: bool = False,
) -> List[UserSkill]:
    """List user skills for a user."""
    query = db.query(UserSkill).filter(UserSkill.owner_id == owner_id)
    if not include_inactive:
        query = query.filter(UserSkill.is_active.is_(True))
    return query.order_by(UserSkill.updated_at.desc()).all()


def create_user_skill(
    db: Session,
    *,
    owner_id: int,
    name: str,
    content: str,
    is_active: bool = True,
) -> UserSkill:
    """Create a new user skill.

    Raises ValueError if the skill already exists.
    """
    existing = get_user_skill_by_name(db, owner_id=owner_id, name=name)
    if existing:
        raise ValueError(f"Skill already exists: {name}")

    skill = UserSkill(
        owner_id=owner_id,
        name=name,
        content=content,
        is_active=is_active,
    )
    db.add(skill)
    db.commit()
    db.refresh(skill)
    return skill


def update_user_skill(
    db: Session,
    *,
    skill: UserSkill,
    name: str | None = None,
    content: str | None = None,
    is_active: bool | None = None,
) -> UserSkill:
    """Update an existing user skill."""
    if name is not None:
        skill.name = name
    if content is not None:
        skill.content = content
    if is_active is not None:
        skill.is_active = is_active

    db.commit()
    db.refresh(skill)
    return skill


def delete_user_skill(db: Session, *, skill: UserSkill) -> None:
    """Delete a user skill."""
    db.delete(skill)
    db.commit()
