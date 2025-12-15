"""Shared *Enum* definitions for SQLAlchemy & Pydantic models.

The Enums inherit from ``str`` so that:

* JSON serialisation remains unchanged (values render as plain strings).
* Equality checks against raw literals (``role == "ADMIN"``) keep working –
  important for backwards compatibility with existing test-suite asserts.
"""

from __future__ import annotations

from enum import Enum


class UserRole(str, Enum):
    USER = "USER"
    ADMIN = "ADMIN"


class AgentStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    ERROR = "error"
    PROCESSING = "processing"


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunTrigger(str, Enum):
    MANUAL = "manual"
    SCHEDULE = "schedule"
    CHAT = "chat"
    WEBHOOK = "webhook"
    API = "api"  # Generic fallback for other API calls


class ThreadType(str, Enum):
    CHAT = "chat"
    SCHEDULED = "scheduled"
    MANUAL = "manual"
    SUPER = "super"  # Supervisor thread (Super Siri architecture)


# New Phase/Result enums for execution state architecture refactor
class Phase(str, Enum):
    """Execution phase - what is happening RIGHT NOW"""

    WAITING = "waiting"  # never executed, may run in future
    RUNNING = "running"  # actively executing
    FINISHED = "finished"  # execution terminated


class Result(str, Enum):
    """Execution result - how did it END (only when phase=finished)"""

    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"


class FailureKind(str, Enum):
    """Classification of failure types for better debugging and analytics"""

    USER = "user"  # user-initiated cancellation
    SYSTEM = "system"  # system error (code bug, dependency failure)
    TIMEOUT = "timeout"  # execution timeout
    EXTERNAL = "external"  # external service failure
    UNKNOWN = "unknown"  # unclassified failure


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
    "AgentStatus",
    "RunStatus",
    "RunTrigger",
    "ThreadType",
    "Phase",
    "Result",
    "FailureKind",
    "RunnerStatus",
    "RunnerJobStatus",
]
