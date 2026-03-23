"""Tests for the TelegramChannel plugin."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from zerg.channels.plugins.telegram import MAX_TEXT_LENGTH
from zerg.channels.plugins.telegram import TelegramChannel
from zerg.channels.types import ChannelConfig
from zerg.channels.types import ChannelMessage
from zerg.channels.types import ChannelStatus
from zerg.channels.types import MediaAttachment


class TestTelegramChannelMeta:
    """Tests for TelegramChannel metadata."""

    def test_meta_id(self):
        """Test channel ID."""
        channel = TelegramChannel()
        assert channel.meta["id"] == "telegram"

    def test_meta_name(self):
        """Test channel name."""
        channel = TelegramChannel()
        assert channel.meta["name"] == "Telegram"

    def test_meta_aliases(self):
        """Test channel aliases."""
        channel = TelegramChannel()
        assert "tg" in channel.meta["aliases"]

    def test_meta_order(self):
        """Test channel display order."""
        channel = TelegramChannel()
        assert channel.meta["order"] == 10


class TestTelegramChannelCapabilities:
    """Tests for TelegramChannel capabilities."""

    def test_send_capabilities(self):
        """Test sending capabilities."""
        channel = TelegramChannel()
        caps = channel.capabilities
        assert caps["send_text"] is True
        assert caps["send_media"] is True
        assert caps["send_voice"] is True

    def test_receive_capabilities(self):
        """Test receiving capabilities."""
        channel = TelegramChannel()
        caps = channel.capabilities
        assert caps["receive_messages"] is True

    def test_feature_capabilities(self):
        """Test feature capabilities."""
        channel = TelegramChannel()
        caps = channel.capabilities
        assert caps["threads"] is True
        assert caps["replies"] is True
        assert caps["edit_messages"] is True
        assert caps["delete_messages"] is True
        assert caps["send_reactions"] is True
        assert caps["polls"] is True
        assert caps["groups"] is True
        assert caps["typing_indicator"] is True

    def test_media_types(self):
        """Test supported media types."""
        channel = TelegramChannel()
        caps = channel.capabilities
        assert "image/jpeg" in caps["media_types"]
        assert "image/png" in caps["media_types"]
        assert "video/mp4" in caps["media_types"]
        assert "audio/mpeg" in caps["media_types"]


class TestTelegramChannelConfigSchema:
    """Tests for TelegramChannel configuration schema."""

    def test_config_schema_has_bot_token(self):
        """Test bot_token field in schema."""
        channel = TelegramChannel()
        schema = channel.config_schema

        assert schema is not None
        field_keys = [f["key"] for f in schema["fields"]]
        assert "bot_token" in field_keys

        bot_token_field = next(f for f in schema["fields"] if f["key"] == "bot_token")
        assert bot_token_field["required"] is True
        assert bot_token_field["sensitive"] is True
        assert bot_token_field["type"] == "password"

    def test_config_schema_has_parse_mode(self):
        """Test parse_mode field in schema."""
        channel = TelegramChannel()
        schema = channel.config_schema

        field_keys = [f["key"] for f in schema["fields"]]
        assert "parse_mode" in field_keys

        parse_mode_field = next(f for f in schema["fields"] if f["key"] == "parse_mode")
        assert parse_mode_field["type"] == "select"
        assert parse_mode_field["default"] == "markdown"

    def test_validate_config_missing_token(self):
        """Test validation fails without bot_token."""
        channel = TelegramChannel()
        errors = channel.validate_config({})
        assert len(errors) > 0
        assert any("bot_token" in e.lower() or "Bot Token" in e for e in errors)

    def test_validate_config_with_token(self):
        """Test validation passes with bot_token."""
        channel = TelegramChannel()
        config: ChannelConfig = {
            "credentials": {"bot_token": "123456:ABC-DEF"},
        }
        errors = channel.validate_config(config)
        assert len(errors) == 0


class TestTelegramChannelLifecycle:
    """Tests for TelegramChannel lifecycle methods."""

    def test_initial_status(self):
        """Test initial disconnected status."""
        channel = TelegramChannel()
        assert channel.status == ChannelStatus.DISCONNECTED

    @pytest.mark.asyncio
    async def test_configure_missing_token_raises(self):
        """Test configuration fails without token."""
        channel = TelegramChannel()
        with pytest.raises(ValueError, match="Missing required field.*Bot Token"):
            await channel.configure({})

    @pytest.mark.asyncio
    async def test_configure_with_empty_token_raises(self):
        """Test configuration fails with empty token."""
        channel = TelegramChannel()
        with pytest.raises(ValueError, match="Missing required field.*Bot Token"):
            await channel.configure({"credentials": {"bot_token": ""}})

    @pytest.mark.asyncio
    async def test_configure_valid(self):
        """Test valid configuration."""
        channel = TelegramChannel()
        config: ChannelConfig = {
            "credentials": {"bot_token": "123456:ABC-DEF"},
            "settings": {
                "parse_mode": "html",
                "disable_notification": True,
            },
        }
        await channel.configure(config)
        assert channel._parse_mode == "html"
        assert channel._disable_notification is True
        assert channel._bot is not None

    @pytest.mark.asyncio
    async def test_start_without_configure_raises(self):
        """Test start fails without configuration."""
        channel = TelegramChannel()
        with pytest.raises(ConnectionError, match="not configured"):
            await channel.start()

    @pytest.mark.asyncio
    @patch("zerg.channels.plugins.telegram.Bot")
    @patch("zerg.channels.plugins.telegram.Application")
    async def test_start_success(self, mock_app_class, mock_bot_class):
        """Test successful channel start."""
        # Setup mocks
        mock_bot = AsyncMock()
        mock_bot.get_me = AsyncMock(
            return_value=MagicMock(
                id=123456,
                username="test_bot",
                first_name="Test Bot",
                can_join_groups=True,
                can_read_all_group_messages=False,
            )
        )
        mock_bot_class.return_value = mock_bot

        mock_app = AsyncMock()
        mock_app.initialize = AsyncMock()
        mock_app.start = AsyncMock()
        mock_app.updater = MagicMock()
        mock_app.updater.start_polling = AsyncMock()
        mock_app.updater.running = False
        mock_app.add_handler = MagicMock()

        mock_builder = MagicMock()
        mock_builder.token = MagicMock(return_value=mock_builder)
        mock_builder.build = MagicMock(return_value=mock_app)
        mock_app_class.builder = MagicMock(return_value=mock_builder)

        channel = TelegramChannel()
        await channel.configure({"credentials": {"bot_token": "123456:ABC-DEF"}})
        await channel.start()

        assert channel.status == ChannelStatus.CONNECTED
        assert channel._bot_info["username"] == "test_bot"

    @pytest.mark.asyncio
    async def test_stop_resets_status(self):
        """Test stop resets channel status."""
        channel = TelegramChannel()
        channel._status = ChannelStatus.CONNECTED
        channel._application = None
        channel._polling_task = None

        await channel.stop()

        assert channel.status == ChannelStatus.DISCONNECTED


class TestTelegramChannelMessaging:
    """Tests for TelegramChannel messaging."""

    @pytest.mark.asyncio
    async def test_send_message_without_bot_fails(self):
        """Test send fails without bot instance."""
        channel = TelegramChannel()
        message: ChannelMessage = {
            "channel_id": "telegram",
            "to": "123456789",
            "text": "Hello!",
        }
        result = await channel.send_message(message)
        assert result["success"] is False
        assert result["error_code"] == "NOT_STARTED"

    @pytest.mark.asyncio
    async def test_send_message_missing_recipient_fails(self):
        """Test send fails without recipient."""
        channel = TelegramChannel()
        channel._bot = MagicMock()

        message: ChannelMessage = {
            "channel_id": "telegram",
            "to": "",
            "text": "Hello!",
        }
        result = await channel.send_message(message)
        assert result["success"] is False
        assert result["error_code"] == "MISSING_RECIPIENT"

    @pytest.mark.asyncio
    async def test_send_message_empty_fails(self):
        """Test send fails with empty message."""
        channel = TelegramChannel()
        channel._bot = MagicMock()

        message: ChannelMessage = {
            "channel_id": "telegram",
            "to": "123456789",
            "text": "",
        }
        result = await channel.send_message(message)
        assert result["success"] is False
        assert result["error_code"] == "EMPTY_MESSAGE"

    @pytest.mark.asyncio
    async def test_send_message_success(self):
        """Test successful message send."""
        channel = TelegramChannel()
        mock_bot = AsyncMock()
        mock_bot.send_message = AsyncMock(return_value=MagicMock(message_id=12345))
        channel._bot = mock_bot

        message: ChannelMessage = {
            "channel_id": "telegram",
            "to": "123456789",
            "text": "Hello!",
        }
        result = await channel.send_message(message)

        assert result["success"] is True
        assert result["message_id"] == "12345"
        mock_bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_message_with_reply_to(self):
        """Test sending a reply to a message."""
        channel = TelegramChannel()
        mock_bot = AsyncMock()
        mock_bot.send_message = AsyncMock(return_value=MagicMock(message_id=12346))
        channel._bot = mock_bot

        message: ChannelMessage = {
            "channel_id": "telegram",
            "to": "123456789",
            "text": "This is a reply",
            "reply_to_id": "999",
        }
        result = await channel.send_message(message)

        assert result["success"] is True
        call_kwargs = mock_bot.send_message.call_args[1]
        assert call_kwargs["reply_to_message_id"] == 999

    @pytest.mark.asyncio
    async def test_send_message_with_thread_id(self):
        """Test sending a message to a forum thread."""
        channel = TelegramChannel()
        mock_bot = AsyncMock()
        mock_bot.send_message = AsyncMock(return_value=MagicMock(message_id=12347))
        channel._bot = mock_bot

        message: ChannelMessage = {
            "channel_id": "telegram",
            "to": "123456789",
            "text": "Forum topic message",
            "thread_id": "42",
        }
        result = await channel.send_message(message)

        assert result["success"] is True
        call_kwargs = mock_bot.send_message.call_args[1]
        assert call_kwargs["message_thread_id"] == 42

    @pytest.mark.asyncio
    async def test_send_message_silent(self):
        """Test sending a silent message."""
        channel = TelegramChannel()
        mock_bot = AsyncMock()
        mock_bot.send_message = AsyncMock(return_value=MagicMock(message_id=12348))
        channel._bot = mock_bot

        message: ChannelMessage = {
            "channel_id": "telegram",
            "to": "123456789",
            "text": "Silent message",
            "silent": True,
        }
        result = await channel.send_message(message)

        assert result["success"] is True
        call_kwargs = mock_bot.send_message.call_args[1]
        assert call_kwargs["disable_notification"] is True


class TestTelegramChannelMediaSending:
    """Tests for TelegramChannel media sending."""

    @pytest.mark.asyncio
    async def test_send_photo(self):
        """Test sending a photo."""
        channel = TelegramChannel()
        mock_bot = AsyncMock()
        mock_bot.send_photo = AsyncMock(return_value=MagicMock(message_id=12349))
        channel._bot = mock_bot

        message: ChannelMessage = {
            "channel_id": "telegram",
            "to": "123456789",
            "text": "Photo caption",
            "media": [
                MediaAttachment(type="image", url="https://example.com/photo.jpg"),
            ],
        }
        result = await channel.send_message(message)

        assert result["success"] is True
        mock_bot.send_photo.assert_called_once()
        call_kwargs = mock_bot.send_photo.call_args[1]
        assert call_kwargs["caption"] == "Photo caption"

    @pytest.mark.asyncio
    async def test_send_video(self):
        """Test sending a video."""
        channel = TelegramChannel()
        mock_bot = AsyncMock()
        mock_bot.send_video = AsyncMock(return_value=MagicMock(message_id=12350))
        channel._bot = mock_bot

        message: ChannelMessage = {
            "channel_id": "telegram",
            "to": "123456789",
            "media": [
                MediaAttachment(type="video", url="https://example.com/video.mp4"),
            ],
        }
        result = await channel.send_message(message)

        assert result["success"] is True
        mock_bot.send_video.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_document(self):
        """Test sending a document."""
        channel = TelegramChannel()
        mock_bot = AsyncMock()
        mock_bot.send_document = AsyncMock(return_value=MagicMock(message_id=12351))
        channel._bot = mock_bot

        message: ChannelMessage = {
            "channel_id": "telegram",
            "to": "123456789",
            "media": [
                MediaAttachment(type="file", url="https://example.com/doc.pdf"),
            ],
        }
        result = await channel.send_message(message)

        assert result["success"] is True
        mock_bot.send_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_voice(self):
        """Test sending a voice message."""
        channel = TelegramChannel()
        mock_bot = AsyncMock()
        mock_bot.send_voice = AsyncMock(return_value=MagicMock(message_id=12352))
        channel._bot = mock_bot

        message: ChannelMessage = {
            "channel_id": "telegram",
            "to": "123456789",
            "media": [
                MediaAttachment(type="voice", url="https://example.com/voice.ogg"),
            ],
        }
        result = await channel.send_message(message)

        assert result["success"] is True
        mock_bot.send_voice.assert_called_once()


class TestTelegramChannelMessageEditing:
    """Tests for TelegramChannel message editing and deletion."""

    @pytest.mark.asyncio
    async def test_edit_message_without_chat_id_fails(self):
        """Test edit fails without chat_id."""
        channel = TelegramChannel()
        channel._bot = MagicMock()

        result = await channel.edit_message("123", "New text")
        assert result["success"] is False
        assert result["error_code"] == "MISSING_CHAT_ID"

    @pytest.mark.asyncio
    async def test_edit_message_success(self):
        """Test successful message edit."""
        channel = TelegramChannel()
        mock_bot = AsyncMock()
        mock_bot.edit_message_text = AsyncMock()
        channel._bot = mock_bot

        result = await channel.edit_message("123", "Updated text", chat_id="456")
        assert result["success"] is True
        mock_bot.edit_message_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_message_without_chat_id_fails(self):
        """Test delete fails without chat_id."""
        channel = TelegramChannel()
        channel._bot = MagicMock()

        result = await channel.delete_message("123")
        assert result["success"] is False
        assert result["error_code"] == "MISSING_CHAT_ID"

    @pytest.mark.asyncio
    async def test_delete_message_success(self):
        """Test successful message deletion."""
        channel = TelegramChannel()
        mock_bot = AsyncMock()
        mock_bot.delete_message = AsyncMock()
        channel._bot = mock_bot

        result = await channel.delete_message("123", chat_id="456")
        assert result["success"] is True
        mock_bot.delete_message.assert_called_once()


class TestTelegramChannelReactions:
    """Tests for TelegramChannel reactions."""

    @pytest.mark.asyncio
    async def test_send_reaction_without_chat_id_fails(self):
        """Test reaction fails without chat_id."""
        channel = TelegramChannel()
        channel._bot = MagicMock()

        result = await channel.send_reaction("123", "thumbs_up")
        assert result["success"] is False
        assert result["error_code"] == "MISSING_CHAT_ID"

    @pytest.mark.asyncio
    async def test_send_reaction_success(self):
        """Test successful reaction."""
        channel = TelegramChannel()
        mock_bot = AsyncMock()
        mock_bot.set_message_reaction = AsyncMock()
        channel._bot = mock_bot

        result = await channel.send_reaction("123", "thumbs_up", chat_id="456")
        assert result["success"] is True
        mock_bot.set_message_reaction.assert_called_once()


class TestTelegramChannelTyping:
    """Tests for TelegramChannel typing indicator."""

    @pytest.mark.asyncio
    async def test_send_typing_without_bot(self):
        """Test typing indicator with no bot is a no-op."""
        channel = TelegramChannel()
        # Should not raise
        await channel.send_typing("123456789")

    @pytest.mark.asyncio
    async def test_send_typing_success(self):
        """Test successful typing indicator."""
        channel = TelegramChannel()
        mock_bot = AsyncMock()
        mock_bot.send_chat_action = AsyncMock()
        channel._bot = mock_bot

        await channel.send_typing("123456789")
        mock_bot.send_chat_action.assert_called_once()


class TestTelegramChannelMessageSplitting:
    """Tests for message splitting."""

    def test_split_short_message(self):
        """Test that short messages aren't split."""
        channel = TelegramChannel()
        text = "Short message"
        chunks = channel._split_message(text, MAX_TEXT_LENGTH)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_split_long_message_at_newline(self):
        """Test splitting at newline boundary."""
        channel = TelegramChannel()
        part1 = "First part" + "\n"
        part2 = "Second part"
        text = part1 + "a" * (MAX_TEXT_LENGTH - len(part1) - 10) + "\n" + part2
        chunks = channel._split_message(text, 100)
        assert len(chunks) > 1

    def test_split_long_message_at_space(self):
        """Test splitting at word boundary when no newline."""
        channel = TelegramChannel()
        # Create text that needs splitting
        text = " ".join(["word"] * 100)
        chunks = channel._split_message(text, 50)
        assert len(chunks) > 1
        # Each chunk should end at a word boundary
        for chunk in chunks[:-1]:
            assert not chunk.endswith(" ")


class TestTelegramChannelParseMode:
    """Tests for parse mode resolution."""

    def test_resolve_markdown(self):
        """Test markdown parse mode."""
        channel = TelegramChannel()
        result = channel._resolve_parse_mode("markdown")
        assert result is not None  # ParseMode.MARKDOWN_V2

    def test_resolve_html(self):
        """Test HTML parse mode."""
        channel = TelegramChannel()
        result = channel._resolve_parse_mode("html")
        assert result is not None  # ParseMode.HTML

    def test_resolve_text(self):
        """Test plain text parse mode."""
        channel = TelegramChannel()
        result = channel._resolve_parse_mode("text")
        assert result is None

    def test_resolve_none(self):
        """Test None parse mode."""
        channel = TelegramChannel()
        result = channel._resolve_parse_mode(None)
        assert result is None


class TestTelegramChannelMessageEventBuilding:
    """Tests for building message events from Telegram messages."""

    def test_build_message_event_dm(self):
        """Test building event from DM."""
        channel = TelegramChannel()

        # Create mock Telegram message
        mock_message = MagicMock()
        mock_message.message_id = 12345
        mock_message.text = "Hello!"
        mock_message.caption = None
        mock_message.date = datetime.utcnow()
        mock_message.message_thread_id = None
        mock_message.reply_to_message = None
        mock_message.photo = []
        mock_message.video = None
        mock_message.audio = None
        mock_message.voice = None
        mock_message.document = None
        mock_message.sticker = None

        mock_message.chat = MagicMock()
        mock_message.chat.id = 123456789
        mock_message.chat.type = "private"
        mock_message.chat.title = None
        mock_message.chat.username = "testuser"

        mock_message.from_user = MagicMock()
        mock_message.from_user.id = 987654321
        mock_message.from_user.first_name = "John"
        mock_message.from_user.last_name = "Doe"
        mock_message.from_user.username = "johndoe"
        mock_message.from_user.is_bot = False

        event = channel._build_message_event(mock_message)

        assert event["channel_id"] == "telegram"
        assert event["message_id"] == "12345"
        assert event["sender_id"] == "987654321"
        assert event["sender_name"] == "John Doe"
        assert event["sender_handle"] == "@johndoe"
        assert event["chat_id"] == "123456789"
        assert event["chat_type"] == "dm"
        assert event["text"] == "Hello!"
        assert event["is_bot"] is False

    def test_build_message_event_group(self):
        """Test building event from group message."""
        channel = TelegramChannel()

        mock_message = MagicMock()
        mock_message.message_id = 12346
        mock_message.text = "Group message"
        mock_message.caption = None
        mock_message.date = datetime.utcnow()
        mock_message.message_thread_id = None
        mock_message.reply_to_message = None
        mock_message.photo = []
        mock_message.video = None
        mock_message.audio = None
        mock_message.voice = None
        mock_message.document = None
        mock_message.sticker = None

        mock_message.chat = MagicMock()
        mock_message.chat.id = -1001234567890
        mock_message.chat.type = "supergroup"
        mock_message.chat.title = "Test Group"
        mock_message.chat.username = None

        mock_message.from_user = MagicMock()
        mock_message.from_user.id = 111222333
        mock_message.from_user.first_name = "Jane"
        mock_message.from_user.last_name = None
        mock_message.from_user.username = None
        mock_message.from_user.is_bot = False

        event = channel._build_message_event(mock_message)

        assert event["chat_type"] == "group"
        assert event["chat_name"] == "Test Group"
        assert event["sender_name"] == "Jane"
        assert event["sender_handle"] is None

    def test_build_message_event_with_thread(self):
        """Test building event from forum thread message."""
        channel = TelegramChannel()

        mock_message = MagicMock()
        mock_message.message_id = 12347
        mock_message.text = "Thread message"
        mock_message.caption = None
        mock_message.date = datetime.utcnow()
        mock_message.message_thread_id = 42
        mock_message.reply_to_message = None
        mock_message.photo = []
        mock_message.video = None
        mock_message.audio = None
        mock_message.voice = None
        mock_message.document = None
        mock_message.sticker = None

        mock_message.chat = MagicMock()
        mock_message.chat.id = -1001234567890
        mock_message.chat.type = "supergroup"
        mock_message.chat.title = "Forum"
        mock_message.chat.username = None

        mock_message.from_user = MagicMock()
        mock_message.from_user.id = 111
        mock_message.from_user.first_name = "User"
        mock_message.from_user.last_name = None
        mock_message.from_user.username = None
        mock_message.from_user.is_bot = False

        event = channel._build_message_event(mock_message)

        assert event["thread_id"] == "42"

    def test_build_message_event_with_reply(self):
        """Test building event from reply message."""
        channel = TelegramChannel()

        mock_reply = MagicMock()
        mock_reply.message_id = 12300

        mock_message = MagicMock()
        mock_message.message_id = 12348
        mock_message.text = "Reply message"
        mock_message.caption = None
        mock_message.date = datetime.utcnow()
        mock_message.message_thread_id = None
        mock_message.reply_to_message = mock_reply
        mock_message.photo = []
        mock_message.video = None
        mock_message.audio = None
        mock_message.voice = None
        mock_message.document = None
        mock_message.sticker = None

        mock_message.chat = MagicMock()
        mock_message.chat.id = 123
        mock_message.chat.type = "private"
        mock_message.chat.title = None
        mock_message.chat.username = None

        mock_message.from_user = MagicMock()
        mock_message.from_user.id = 111
        mock_message.from_user.first_name = "User"
        mock_message.from_user.last_name = None
        mock_message.from_user.username = None
        mock_message.from_user.is_bot = False

        event = channel._build_message_event(mock_message)

        assert event["reply_to_id"] == "12300"

    def test_build_message_event_with_photo(self):
        """Test building event with photo attachment."""
        channel = TelegramChannel()

        mock_photo = MagicMock()
        mock_photo.file_id = "photo_file_id"
        mock_photo.file_size = 100000

        mock_message = MagicMock()
        mock_message.message_id = 12349
        mock_message.text = None
        mock_message.caption = "Photo caption"
        mock_message.date = datetime.utcnow()
        mock_message.message_thread_id = None
        mock_message.reply_to_message = None
        mock_message.photo = [mock_photo]
        mock_message.video = None
        mock_message.audio = None
        mock_message.voice = None
        mock_message.document = None
        mock_message.sticker = None

        mock_message.chat = MagicMock()
        mock_message.chat.id = 123
        mock_message.chat.type = "private"
        mock_message.chat.title = None
        mock_message.chat.username = None

        mock_message.from_user = MagicMock()
        mock_message.from_user.id = 111
        mock_message.from_user.first_name = "User"
        mock_message.from_user.last_name = None
        mock_message.from_user.username = None
        mock_message.from_user.is_bot = False

        event = channel._build_message_event(mock_message)

        assert event["text"] == "Photo caption"
        assert event["media"] is not None
        assert len(event["media"]) == 1
        assert event["media"][0]["type"] == "image"


class TestTelegramChannelErrorHandling:
    """Tests for error handling."""

    @pytest.mark.asyncio
    async def test_forbidden_error(self):
        """Test handling of Forbidden error (blocked bot)."""
        from telegram.error import Forbidden

        channel = TelegramChannel()
        mock_bot = AsyncMock()
        mock_bot.send_message = AsyncMock(side_effect=Forbidden("Bot blocked"))
        channel._bot = mock_bot

        message: ChannelMessage = {
            "channel_id": "telegram",
            "to": "123456789",
            "text": "Hello!",
        }
        result = await channel.send_message(message)

        assert result["success"] is False
        assert result["error_code"] == "FORBIDDEN"

    @pytest.mark.asyncio
    async def test_network_error(self):
        """Test handling of network errors."""
        from telegram.error import NetworkError

        channel = TelegramChannel()
        mock_bot = AsyncMock()
        mock_bot.send_message = AsyncMock(side_effect=NetworkError("Connection failed"))
        channel._bot = mock_bot

        message: ChannelMessage = {
            "channel_id": "telegram",
            "to": "123456789",
            "text": "Hello!",
        }
        result = await channel.send_message(message)

        assert result["success"] is False
        assert result["error_code"] == "NETWORK_ERROR"
        assert result["retry_after"] == 5

    @pytest.mark.asyncio
    async def test_parse_error_fallback_to_plaintext(self):
        """Test fallback to plain text on parse error."""
        from telegram.error import BadRequest

        channel = TelegramChannel()
        mock_bot = AsyncMock()

        call_count = 0

        async def mock_send(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise BadRequest("Can't parse entities")
            return MagicMock(message_id=12350)

        mock_bot.send_message = mock_send
        channel._bot = mock_bot

        message: ChannelMessage = {
            "channel_id": "telegram",
            "to": "123456789",
            "text": "Hello with *bad* markdown",
            "parse_mode": "markdown",
        }
        result = await channel.send_message(message)

        # Should succeed after fallback
        assert result["success"] is True
        assert call_count == 2


class TestRegisterChannel:
    """Tests for the register_channel factory function."""

    def test_register_channel_returns_telegram_instance(self):
        """Test factory function returns TelegramChannel."""
        from zerg.channels.plugins.telegram import register_channel

        channel = register_channel()
        assert isinstance(channel, TelegramChannel)
        assert channel.meta["id"] == "telegram"
