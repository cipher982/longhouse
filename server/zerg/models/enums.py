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


class FicheStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    ERROR = "error"
    PROCESSING = "processing"


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"  # Interrupted waiting for external completion
    DEFERRED = "deferred"  # Timeout migration: still executing, but caller stopped waiting
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunTrigger(str, Enum):
    MANUAL = "manual"
    SCHEDULE = "schedule"
    CHAT = "chat"
    WEBHOOK = "webhook"
    API = "api"  # Generic fallback for other API calls
    CONTINUATION = "continuation"  # Triggered by external completion


class ThreadType(str, Enum):
    CHAT = "chat"
    SCHEDULED = "scheduled"
    MANUAL = "manual"
    SUPER = "super"  # Legacy thread type


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
    "FicheStatus",
    "RunStatus",
    "RunTrigger",
    "ThreadType",
    "RunnerStatus",
    "RunnerJobStatus",
    "SessionLoopMode",
]
