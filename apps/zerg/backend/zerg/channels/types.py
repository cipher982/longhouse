"""Core types for the Channel Plugin Architecture.

Defines the fundamental data structures and type definitions used across
the channel plugin system.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from typing import Literal
from typing import TypedDict

# --- Channel Identification ---


class ChannelId(str, Enum):
    """Built-in channel identifiers.

    Third-party plugins can register custom channel IDs as strings.
    """

    TELEGRAM = "telegram"
    SLACK = "slack"
    DISCORD = "discord"
    WHATSAPP = "whatsapp"
    SIGNAL = "signal"
    IMESSAGE = "imessage"
    EMAIL = "email"
    SMS = "sms"
    WEB = "web"  # Web chat widget
    MOCK = "mock"  # For testing


# --- Channel Metadata ---


class ChannelMeta(TypedDict, total=False):
    """Metadata describing a channel plugin.

    Required:
        id: Unique channel identifier
        name: Human-readable channel name
        description: Brief description of the channel

    Optional:
        icon: Icon identifier or URL
        docs_url: Link to setup documentation
        aliases: Alternative names for the channel
        order: Display order priority (lower = higher priority)
    """

    id: str
    name: str
    description: str
    icon: str
    docs_url: str
    aliases: list[str]
    order: int


# --- Channel Capabilities ---


class ChannelCapabilities(TypedDict, total=False):
    """Declares what features a channel supports.

    All fields default to False if not specified.
    """

    # Messaging
    send_text: bool  # Can send text messages
    send_media: bool  # Can send images/files
    send_voice: bool  # Can send voice messages
    send_reactions: bool  # Can react to messages
    receive_messages: bool  # Can receive incoming messages

    # Rich features
    threads: bool  # Supports threaded conversations
    replies: bool  # Supports reply-to-message
    edit_messages: bool  # Can edit sent messages
    delete_messages: bool  # Can delete messages
    polls: bool  # Can send polls

    # Group features
    groups: bool  # Supports group conversations
    group_management: bool  # Can manage group membership

    # Presence
    typing_indicator: bool  # Can show typing status
    read_receipts: bool  # Supports read receipts
    presence: bool  # Can show online/offline status

    # Media types
    media_types: list[str]  # Supported MIME types


# --- Channel Status ---


class ChannelStatus(str, Enum):
    """Connection status for a channel instance."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    ERROR = "error"


class ChannelPresence(TypedDict, total=False):
    """Presence information for a user on a channel."""

    user_id: str
    channel_id: str
    status: Literal["online", "offline", "away", "busy"]
    last_seen: datetime | None
    status_text: str | None


# --- Channel Configuration ---


class ChannelConfigField(TypedDict, total=False):
    """Definition of a configuration field for a channel."""

    key: str  # Field key in storage
    label: str  # Human-readable label
    type: Literal["text", "password", "url", "number", "boolean", "select"]
    placeholder: str  # Example/hint
    required: bool  # Whether field is required
    default: Any  # Default value
    options: list[dict[str, str]]  # For select type: [{value, label}]
    help_text: str  # Additional help text
    sensitive: bool  # Should be masked in UI
    advanced: bool  # Only show in advanced settings


class ChannelConfigSchema(TypedDict, total=False):
    """Configuration schema for a channel plugin."""

    fields: list[ChannelConfigField]
    # JSON Schema for validation
    json_schema: dict[str, Any]


class ChannelConfig(TypedDict, total=False):
    """Runtime configuration for a channel instance."""

    channel_id: str
    account_id: str  # User's account ID on this channel
    enabled: bool
    credentials: dict[str, Any]  # Sensitive config (tokens, etc.)
    settings: dict[str, Any]  # Non-sensitive settings
    metadata: dict[str, Any]  # Channel-specific metadata


# --- Messages ---


class MediaAttachment(TypedDict, total=False):
    """Media attachment in a message."""

    type: Literal["image", "video", "audio", "file", "voice", "sticker"]
    url: str | None
    data: bytes | None  # Binary data if not URL
    filename: str | None
    mime_type: str | None
    size_bytes: int | None
    thumbnail_url: str | None
    caption: str | None


class ChannelMessage(TypedDict, total=False):
    """A message to be sent through a channel."""

    # Routing
    channel_id: str
    to: str  # Recipient ID (user, group, or channel)
    thread_id: str | None  # For threaded replies
    reply_to_id: str | None  # Message ID to reply to

    # Content
    text: str | None
    media: list[MediaAttachment] | None
    buttons: list[dict[str, Any]] | None  # Interactive buttons
    embed: dict[str, Any] | None  # Rich embed content

    # Options
    parse_mode: Literal["text", "markdown", "html"] | None
    silent: bool  # Don't trigger notifications
    scheduled_at: datetime | None  # Schedule message


class ChannelMessageEvent(TypedDict, total=False):
    """An incoming message event from a channel."""

    # Identity
    event_id: str  # Unique event ID
    channel_id: str
    message_id: str  # Platform-specific message ID

    # Sender
    sender_id: str  # Platform user ID
    sender_name: str | None
    sender_handle: str | None  # @username, etc.

    # Context
    chat_id: str  # Conversation/chat ID
    chat_type: Literal["dm", "group", "channel"]
    chat_name: str | None
    thread_id: str | None
    reply_to_id: str | None

    # Content
    text: str | None
    media: list[MediaAttachment] | None
    raw: dict[str, Any]  # Original platform event

    # Metadata
    timestamp: datetime
    edited: bool
    is_bot: bool


class ChannelTypingEvent(TypedDict, total=False):
    """A typing indicator event."""

    channel_id: str
    chat_id: str
    user_id: str
    is_typing: bool
    timestamp: datetime


# --- Delivery Results ---


class MessageDeliveryResult(TypedDict, total=False):
    """Result of sending a message."""

    success: bool
    message_id: str | None  # Platform message ID if successful
    error: str | None
    error_code: str | None
    retry_after: int | None  # Seconds to wait before retry
