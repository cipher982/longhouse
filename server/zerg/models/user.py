"""User model for authentication and authorization."""

from sqlalchemy import JSON
from sqlalchemy import Boolean
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import Enum as SAEnum
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.sql import func

from zerg.database import Base
from zerg.models.enums import UserRole


class User(Base):
    """Application user.

    For the MVP we only support Google sign-in, but we leave provider fields
    generic to allow future providers (e.g. GitHub, email, etc.).
    """

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)

    # OAuth provider details -------------------------------------------------
    provider = Column(String, nullable=True, default="google")
    provider_user_id = Column(String, nullable=True, index=True)

    # Core identity ----------------------------------------------------------
    email = Column(String, unique=True, nullable=False, index=True)
    is_active = Column(Boolean, default=True, nullable=False)

    # Role / permission level – backed by :class:`zerg.models.enums.UserRole`.
    role = Column(
        SAEnum(UserRole, native_enum=False, name="user_role_enum"),
        nullable=False,
        default=UserRole.USER.value,
    )

    # -------------------------------------------------------------------
    # Personalisation fields (introduced in *User Personalisation* feature)
    # -------------------------------------------------------------------
    # Optional display name shown in the UI (fallback: e-mail)
    display_name = Column(String, nullable=True)
    # User-supplied avatar URL (fallback: generated initial)
    avatar_url = Column(String, nullable=True)
    # Store arbitrary UI preferences (theme, timezone, etc.)
    prefs = Column(MutableDict.as_mutable(JSON), nullable=True, default={})

    # User context for prompt composition (servers, integrations, preferences)
    context = Column(MutableDict.as_mutable(JSON), nullable=False, default={})

    # Login tracking
    last_login = Column(DateTime, nullable=True)

    # -------------------------------------------------------------------
    # Google Mail integration (Phase-2 Email Triggers)
    # -------------------------------------------------------------------
    # When a user connects their Gmail account with *offline_access* scope we
    # receive a **refresh token** that allows the backend to fetch short-lived
    # access-tokens without further user interaction.  Persist the token
    # encrypted-at-rest in a future iteration – for now we store the raw value
    # because unit-tests run against an ephemeral in-memory SQLite database.

    gmail_refresh_token = Column(String, nullable=True)

    # -------------------------------------------------------------------
    # Convenience property used by the API layer / Pydantic models.
    # -------------------------------------------------------------------

    @property
    def gmail_connected(self) -> bool:  # noqa: D401 – simple boolean accessor
        """Return *True* if the user granted offline Gmail access (refresh token stored)."""

        return self.gmail_refresh_token is not None

    # Timestamps -------------------------------------------------------------
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
