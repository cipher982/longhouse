"""Event bus and event handling infrastructure.

This package provides:
- EventBus: Central pub/sub for system events
"""

from .event_bus import EventBus
from .event_bus import EventType
from .event_bus import event_bus

__all__ = [
    "EventBus",
    "EventType",
    "event_bus",
]
