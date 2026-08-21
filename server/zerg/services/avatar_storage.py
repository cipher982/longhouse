"""Canonical durable storage location for user-uploaded avatars."""

from __future__ import annotations

from pathlib import Path

from zerg.config import get_settings


def avatar_storage_dir() -> Path:
    """Return the avatar directory below the Runtime Host's data root."""

    return get_settings().data_dir / "avatars"
