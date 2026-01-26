"""Event bus and event handling infrastructure.

This package provides:
- EventBus: Central pub/sub for system events
- EventEmitter: Protocol for tool event emission with baked-in identity
- CommisEmitter/ConciergeEmitter: Concrete emitters that always emit correct event types
- get_emitter/set_emitter: Contextvar-based emitter transport
"""

from .commis_emitter import CommisEmitter
from .commis_emitter import ToolCall
from .concierge_emitter import ConciergeEmitter
from .emitter_context import get_emitter
from .emitter_context import reset_emitter
from .emitter_context import set_emitter
from .emitter_protocol import EventEmitter
from .event_bus import EventBus
from .event_bus import EventType
from .event_bus import event_bus
from .null_emitter import NullEmitter

__all__ = [
    # Event bus
    "EventBus",
    "EventType",
    "event_bus",
    # Emitter protocol and implementations
    "EventEmitter",
    "CommisEmitter",
    "ConciergeEmitter",
    "NullEmitter",
    "ToolCall",
    # Emitter context management
    "get_emitter",
    "set_emitter",
    "reset_emitter",
]
