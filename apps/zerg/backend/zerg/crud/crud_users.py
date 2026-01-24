"""CRUD operations for Users."""

import logging
import threading
from typing import Any
from typing import Dict
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from zerg.config import get_settings
from zerg.models import User

logger = logging.getLogger(__name__)


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

    # Send Discord notification for new user signup (background thread, sync httpx)
    try:
        total_users = count_users(db)
    except Exception:
        total_users = None

    def _send_signup_alert():
        settings = get_settings()
        if settings.testing or not settings.discord_enable_alerts or not settings.discord_webhook_url:
            return
        count_info = f" (#{total_users} total)" if total_users else ""
        content = f"@here 🎉 **New User Signup!** {email} just joined Swarmlet{count_info}"
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.post(settings.discord_webhook_url, json={"content": content})
                if resp.status_code >= 300:
                    logger.warning("Discord webhook returned %s: %s", resp.status_code, resp.text)
        except Exception as exc:
            logger.warning("Discord signup alert failed: %s", exc)

    threading.Thread(target=_send_signup_alert, daemon=True).start()

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
