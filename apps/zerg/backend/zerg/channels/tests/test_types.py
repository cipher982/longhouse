"""Tests for channel types."""

from __future__ import annotations

from datetime import datetime

from zerg.channels.types import ChannelCapabilities
from zerg.channels.types import ChannelId
from zerg.channels.types import ChannelMessage
from zerg.channels.types import ChannelMessageEvent
from zerg.channels.types import ChannelMeta
from zerg.channels.types import ChannelStatus
from zerg.channels.types import MediaAttachment
from zerg.channels.types import MessageDeliveryResult


class TestChannelId:
    """Tests for ChannelId enum."""

    def test_built_in_channels(self):
        """Test that built-in channel IDs are defined."""
        assert ChannelId.TELEGRAM == "telegram"
        assert ChannelId.SLACK == "slack"
        assert ChannelId.DISCORD == "discord"
        assert ChannelId.MOCK == "mock"

    def test_channel_id_values(self):
        """Test channel ID string values."""
        assert ChannelId.TELEGRAM.value == "telegram"
        assert str(ChannelId.SLACK) == "slack"


class TestChannelStatus:
    """Tests for ChannelStatus enum."""

    def test_status_values(self):
        """Test all status values exist."""
        assert ChannelStatus.DISCONNECTED == "disconnected"
        assert ChannelStatus.CONNECTING == "connecting"
        assert ChannelStatus.CONNECTED == "connected"
        assert ChannelStatus.RECONNECTING == "reconnecting"
        assert ChannelStatus.ERROR == "error"


class TestChannelMeta:
    """Tests for ChannelMeta TypedDict."""

    def test_minimal_meta(self):
        """Test creating minimal metadata."""
        meta: ChannelMeta = {
            "id": "test",
            "name": "Test Channel",
            "description": "A test channel",
        }
        assert meta["id"] == "test"
        assert meta["name"] == "Test Channel"

    def test_full_meta(self):
        """Test creating full metadata."""
        meta: ChannelMeta = {
            "id": "test",
            "name": "Test Channel",
            "description": "A test channel",
            "icon": "message",
            "docs_url": "https://example.com/docs",
            "aliases": ["tst", "testing"],
            "order": 10,
        }
        assert len(meta["aliases"]) == 2
        assert meta["order"] == 10


class TestChannelCapabilities:
    """Tests for ChannelCapabilities TypedDict."""

    def test_basic_capabilities(self):
        """Test basic capability flags."""
        caps: ChannelCapabilities = {
            "send_text": True,
            "send_media": True,
            "receive_messages": True,
        }
        assert caps["send_text"] is True
        assert caps.get("send_voice", False) is False

    def test_media_types(self):
        """Test media type list."""
        caps: ChannelCapabilities = {
            "send_media": True,
            "media_types": ["image/png", "image/jpeg", "video/mp4"],
        }
        assert "image/png" in caps["media_types"]


class TestChannelMessage:
    """Tests for ChannelMessage TypedDict."""

    def test_simple_message(self):
        """Test creating a simple text message."""
        msg: ChannelMessage = {
            "channel_id": "telegram",
            "to": "@username",
            "text": "Hello, world!",
        }
        assert msg["text"] == "Hello, world!"

    def test_message_with_media(self):
        """Test message with media attachment."""
        attachment: MediaAttachment = {
            "type": "image",
            "url": "https://example.com/image.png",
            "mime_type": "image/png",
        }
        msg: ChannelMessage = {
            "channel_id": "telegram",
            "to": "123456",
            "media": [attachment],
            "text": "Check out this image!",
        }
        assert len(msg["media"]) == 1
        assert msg["media"][0]["type"] == "image"

    def test_message_with_thread(self):
        """Test message in a thread."""
        msg: ChannelMessage = {
            "channel_id": "slack",
            "to": "#general",
            "text": "Thread reply",
            "thread_id": "1234567890.123456",
            "reply_to_id": "1234567890.000001",
        }
        assert msg["thread_id"] is not None


class TestChannelMessageEvent:
    """Tests for ChannelMessageEvent TypedDict."""

    def test_incoming_message(self):
        """Test creating an incoming message event."""
        event: ChannelMessageEvent = {
            "event_id": "evt_123",
            "channel_id": "telegram",
            "message_id": "msg_456",
            "sender_id": "user_789",
            "sender_name": "John Doe",
            "chat_id": "chat_123",
            "chat_type": "dm",
            "text": "Hello!",
            "timestamp": datetime.utcnow(),
            "edited": False,
            "is_bot": False,
        }
        assert event["chat_type"] == "dm"
        assert event["is_bot"] is False

    def test_group_message(self):
        """Test group message event."""
        event: ChannelMessageEvent = {
            "event_id": "evt_123",
            "channel_id": "discord",
            "message_id": "msg_456",
            "sender_id": "user_789",
            "chat_id": "guild_channel_123",
            "chat_type": "group",
            "chat_name": "General",
            "text": "Hey everyone!",
            "timestamp": datetime.utcnow(),
            "edited": False,
            "is_bot": False,
        }
        assert event["chat_type"] == "group"
        assert event["chat_name"] == "General"


class TestMessageDeliveryResult:
    """Tests for MessageDeliveryResult TypedDict."""

    def test_successful_delivery(self):
        """Test successful delivery result."""
        result: MessageDeliveryResult = {
            "success": True,
            "message_id": "msg_12345",
        }
        assert result["success"] is True
        assert result["message_id"] is not None

    def test_failed_delivery(self):
        """Test failed delivery result."""
        result: MessageDeliveryResult = {
            "success": False,
            "error": "User blocked the bot",
            "error_code": "BLOCKED",
        }
        assert result["success"] is False
        assert result["error_code"] == "BLOCKED"

    def test_rate_limited(self):
        """Test rate-limited delivery result."""
        result: MessageDeliveryResult = {
            "success": False,
            "error": "Rate limited",
            "error_code": "RATE_LIMITED",
            "retry_after": 30,
        }
        assert result["retry_after"] == 30
