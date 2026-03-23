"""Built-in surface adapter implementations."""

from zerg.surfaces.adapters.operator import OperatorSurfaceAdapter
from zerg.surfaces.adapters.telegram import TelegramSurfaceAdapter
from zerg.surfaces.adapters.voice import VoiceSurfaceAdapter
from zerg.surfaces.adapters.web import WebSurfaceAdapter

__all__ = [
    "OperatorSurfaceAdapter",
    "TelegramSurfaceAdapter",
    "VoiceSurfaceAdapter",
    "WebSurfaceAdapter",
]
