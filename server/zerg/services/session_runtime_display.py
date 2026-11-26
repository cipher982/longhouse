"""Runtime display vocabulary shared by the served session projections.

Owns the enum vocabularies web and iOS render against, the transcript-sync
display window, and the compact tool-label normalizer. The projections
themselves are built in ``session_views`` and ``live_catalog_timeline``.
"""

from __future__ import annotations

from datetime import timedelta
from enum import Enum


class TruthTier(str, Enum):
    NONE = "none"
    STALE = "stale"
    FRESH = "fresh"
    MANAGED_LOCAL = "managed-local"


class SignalTier(str, Enum):
    NONE = "none"
    PHASE_SIGNAL = "phase_signal"
    PROCESS_BINDING = "process_binding"
    TRANSCRIPT_PROGRESS = "transcript_progress"


class ControlPath(str, Enum):
    MANAGED = "managed"
    UNMANAGED = "unmanaged"


class ActivityRecency(str, Enum):
    LIVE = "live"
    RECENT = "recent"
    STALE = "stale"
    NONE = "none"


class Lifecycle(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class HostState(str, Enum):
    ONLINE = "online"
    STALE = "stale"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class PresenceState(str, Enum):
    THINKING = "thinking"
    RUNNING = "running"
    IDLE = "idle"
    NEEDS_USER = "needs_user"
    BLOCKED = "blocked"
    STALLED = "stalled"


class Tone(str, Enum):
    STALLED = "stalled"
    BLOCKED = "blocked"
    RUNNING = "running"
    THINKING = "thinking"
    IDLE = "idle"
    ACTIVE = "active"
    INACTIVE = "inactive"
    CLOSED = "closed"


class TerminalReason(str, Enum):
    SESSION_ENDED = "session_ended"
    USER_CLOSED = "user_closed"
    BRIDGE_STOP = "bridge_stop"
    PROVIDER_EXIT = "provider_exit"
    PROCESS_GONE = "process_gone"
    OWNER_GONE = "owner_gone"
    HOST_EXPIRED = "host_expired"
    PROVIDER_SIGNAL = "provider_signal"
    UNKNOWN = "unknown"


TRANSCRIPT_SYNC_DISPLAY_WINDOW = timedelta(seconds=30)
