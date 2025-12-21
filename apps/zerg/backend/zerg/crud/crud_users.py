"""CRUD operations for Users."""

from typing import Any
from typing import Dict
from typing import Optional

from sqlalchemy.orm import Session

from zerg.models import User


def get_user(db: Session, user_id: int) -> Optional[User]:
    """Return user by primary key."""
    return db.query(User).filter(User.id == user_id).first()


def get_user_by_email(db: Session, email: str) -> Optional[User]:
    """Return user by e-mail address (case-insensitive)."""
    return (
        db.query(User)
        .filter(User.email.ilike(email))  # type: ignore[arg-type]
        .first()
    )


def count_users(db: Session) -> int:
    """Return total number of users in the system."""
    return db.query(User).count()


def create_user(
    db: Session,
    *,
    email: str,
    provider: Optional[str] = None,
    provider_user_id: Optional[str] = None,
    role: str = "USER",
) -> User:
    """Insert new user row.

    Caller is expected to ensure uniqueness beforehand; we do not upsert here.
    """
    new_user = User(
        email=email,
        provider=provider,
        provider_user_id=provider_user_id,
        role=role,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    # Send Discord notification for new user signup
    import asyncio
    import threading

    from zerg.services.ops_discord import send_user_signup_alert

    # Get total user count for the notification
    try:
        total_users = count_users(db)
    except Exception:
        total_users = None

    # Fire-and-forget Discord notification in background thread
    def _send_discord_notification():
        try:
            asyncio.run(send_user_signup_alert(email, total_users))
        except Exception:
            # Don't fail user creation if Discord notification fails
            pass

    try:
        threading.Thread(target=_send_discord_notification, daemon=True).start()
    except Exception:
        # Don't fail user creation if Discord notification fails
        pass

    return new_user


def update_user(
    db: Session,
    user_id: int,
    *,
    display_name: Optional[str] = None,
    avatar_url: Optional[str] = None,
    prefs: Optional[Dict[str, Any]] = None,
    gmail_refresh_token: Optional[str] = None,
) -> Optional[User]:
    """Partial update for the *User* table.

    Only the provided fields are modified – `None` leaves the column unchanged.
    Returns the updated user row or ``None`` if the record was not found.
    """

    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        return None

    if display_name is not None:
        user.display_name = display_name
    if avatar_url is not None:
        user.avatar_url = avatar_url
    if prefs is not None:
        user.prefs = prefs
    if gmail_refresh_token is not None:
        from zerg.utils import crypto  # local import to avoid top-level dependency in non-auth paths

        user.gmail_refresh_token = crypto.encrypt(gmail_refresh_token)

    db.commit()
    db.refresh(user)
    return user
