"""Channel Plugin Architecture for Zerg.

NOTE: Uses `from __future__ import annotations` for forward reference support.

This module provides an extensible plugin system for messaging channel integrations
(Telegram, Slack, Discord, etc.). Channels differ from Connectors in that they
support bi-directional communication with conversation state.

Key Components:
- ChannelPlugin: Interface for channel implementations
- ChannelRegistry: Dynamic plugin registration and discovery
- ChannelRouter: Routes messages to/from appropriate channels
- ChannelAdapter: Base adapters for common operations

Example usage:
    from zerg.channels import ChannelRegistry, get_channel

    # Get a registered channel
    telegram = get_channel("telegram")
    if telegram:
        await telegram.send_message(to="@user", text="Hello!")
"""

from zerg.channels.plugin import ChannelPlugin
from zerg.channels.registry import ChannelRegistry
from zerg.channels.registry import get_channel
from zerg.channels.registry import get_registry
from zerg.channels.registry import list_channels
from zerg.channels.registry import register_channel
from zerg.channels.router import ChannelRouter
from zerg.channels.types import ChannelCapabilities
from zerg.channels.types import ChannelConfig
from zerg.channels.types import ChannelId
from zerg.channels.types import ChannelMessage
from zerg.channels.types import ChannelMessageEvent
from zerg.channels.types import ChannelMeta
from zerg.channels.types import ChannelPresence
from zerg.channels.types import ChannelStatus
from zerg.channels.types import ChannelTypingEvent
from zerg.channels.types import MediaAttachment

__all__ = [
    # Types
    "ChannelCapabilities",
    "ChannelConfig",
    "ChannelId",
    "ChannelMeta",
    "ChannelMessage",
    "ChannelMessageEvent",
    "ChannelPresence",
    "ChannelStatus",
    "ChannelTypingEvent",
    "MediaAttachment",
    # Plugin Interface
    "ChannelPlugin",
    # Registry
    "ChannelRegistry",
    "get_channel",
    "get_registry",
    "list_channels",
    "register_channel",
    # Router
    "ChannelRouter",
]
