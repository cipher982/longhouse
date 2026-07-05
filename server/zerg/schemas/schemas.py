"""Shared user/auth schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel
from pydantic import ConfigDict

from zerg.utils.time import UTCBaseModel


class UserOut(UTCBaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    is_active: bool
    created_at: datetime
    display_name: str | None = None
    avatar_url: str | None = None
    prefs: dict[str, Any] | None = None
    last_login: datetime | None = None
    role: str = "USER"


class UserUpdate(BaseModel):
    display_name: str | None = None
    avatar_url: str | None = None
    prefs: dict[str, Any] | None = None


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
