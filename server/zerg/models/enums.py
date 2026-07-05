"""Shared *Enum* definitions for SQLAlchemy & Pydantic models.

The Enums inherit from ``str`` so that:

* JSON serialisation remains unchanged (values render as plain strings).
* Equality checks against raw literals (``role == "ADMIN"``) keep working –
  important for backwards compatibility with existing test-suite asserts.
"""

from __future__ import annotations

from enum import Enum

from zerg.session_loop_mode import SessionLoopMode


class UserRole(str, Enum):
    USER = "USER"
    ADMIN = "ADMIN"


class RunnerStatus(str, Enum):
    """Runner connection status"""

    ONLINE = "online"
    OFFLINE = "offline"
    REVOKED = "revoked"


class RunnerJobStatus(str, Enum):
    """Runner job execution status"""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELED = "canceled"


__all__ = [
    "UserRole",
    "RunnerStatus",
    "RunnerJobStatus",
    "SessionLoopMode",
]
