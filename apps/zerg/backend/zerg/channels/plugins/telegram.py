"""Telegram Channel Plugin.

Provides bi-directional messaging integration with Telegram via the Bot API.
Supports DMs, groups, typing indicators, and media handling.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any
from uuid import uuid4

from telegram import Bot
from telegram import InputFile
from telegram import Message
from telegram import Update
from telegram.constants import ChatAction
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.error import Forbidden
from telegram.error import NetworkError
from telegram.error import TelegramError
from telegram.error import TimedOut
from telegram.ext import Application
from telegram.ext import ContextTypes
from telegram.ext import MessageHandler
from telegram.ext import filters

from zerg.channels.plugin import ChannelPlugin
from zerg.channels.types import ChannelCapabilities
from zerg.channels.types import ChannelConfig
from zerg.channels.types import ChannelConfigField
from zerg.channels.types import ChannelConfigSchema
from zerg.channels.types import ChannelMessage
from zerg.channels.types import ChannelMessageEvent
from zerg.channels.types import ChannelMeta
from zerg.channels.types import ChannelStatus
from zerg.channels.types import MediaAttachment
from zerg.channels.types import MessageDeliveryResult

logger = logging.getLogger(__name__)

# Telegram message limits
MAX_TEXT_LENGTH = 4096
MAX_CAPTION_LENGTH = 1024


class TelegramChannel(ChannelPlugin):
    """Telegram channel implementation using python-telegram-bot.

    Supports:
    - Direct messages (DMs)
    - Group messages
    - Forum/topic threads
    - Typing indicators
    - Media (images, audio, video, files)
    - Markdown/HTML formatting
    - Message editing and deletion
    - Reactions

    Configuration:
        credentials:
            bot_token: Your Telegram Bot API token from @BotFather
        settings:
            parse_mode: Default message format (markdown, html, text)
            disable_notification: Send messages silently by default

    Example:
        channel = TelegramChannel()
        await channel.configure({
            "credentials": {"bot_token": "123456:ABC-DEF"},
            "settings": {"parse_mode": "markdown"}
        })
        await channel.start()

        result = await channel.send_message(ChannelMessage(
            channel_id="telegram",
            to="123456789",  # Chat ID
            text="Hello from Zerg!"
        ))
    """

    def __init__(self) -> None:
        """Initialize the Telegram channel."""
        self._status = ChannelStatus.DISCONNECTED
        self._config: ChannelConfig = {}
        self._bot: Bot | None = None
        self._application: Application | None = None
        self._bot_info: dict[str, Any] = {}
        self._polling_task: asyncio.Task | None = None
        self._message_handlers: list = []
        self._typing_handlers: list = []
        self._status_handlers: list = []
        self._parse_mode: str = "markdown"
        self._disable_notification: bool = False

    @property
    def meta(self) -> ChannelMeta:
        return {
            "id": "telegram",
            "name": "Telegram",
            "description": "Telegram Bot API integration for messaging",
            "icon": "telegram",
            "docs_url": "https://core.telegram.org/bots/api",
            "aliases": ["tg"],
            "order": 10,
        }

    @property
    def capabilities(self) -> ChannelCapabilities:
        return {
            "send_text": True,
            "send_media": True,
            "send_voice": True,
            "send_reactions": True,
            "receive_messages": True,
            "threads": True,
            "replies": True,
            "edit_messages": True,
            "delete_messages": True,
            "polls": True,
            "groups": True,
            "group_management": False,
            "typing_indicator": True,
            "read_receipts": False,
            "presence": False,
            "media_types": [
                "image/jpeg",
                "image/png",
                "image/gif",
                "image/webp",
                "video/mp4",
                "audio/mpeg",
                "audio/ogg",
                "application/pdf",
            ],
        }

    @property
    def config_schema(self) -> ChannelConfigSchema:
        return {
            "fields": [
                ChannelConfigField(
                    key="bot_token",
                    label="Bot Token",
                    type="password",
                    placeholder="123456:ABC-DEF1234ghIkl-zyx57W2v",
                    required=True,
                    sensitive=True,
                    help_text="Get this from @BotFather on Telegram",
                ),
                ChannelConfigField(
                    key="parse_mode",
                    label="Default Parse Mode",
                    type="select",
                    required=False,
                    default="markdown",
                    options=[
                        {"value": "text", "label": "Plain Text"},
                        {"value": "markdown", "label": "Markdown"},
                        {"value": "html", "label": "HTML"},
                    ],
                    help_text="How to format message text by default",
                ),
                ChannelConfigField(
                    key="disable_notification",
                    label="Silent Messages",
                    type="boolean",
                    required=False,
                    default=False,
                    help_text="Send messages without notification sound",
                ),
                ChannelConfigField(
                    key="webhook_url",
                    label="Webhook URL",
                    type="url",
                    required=False,
                    placeholder="https://your-domain.com/webhook/telegram",
                    help_text="Optional: Use webhooks instead of polling",
                    advanced=True,
                ),
            ]
        }

    @property
    def status(self) -> ChannelStatus:
        return self._status

    # --- Lifecycle ---

    async def configure(self, config: ChannelConfig) -> None:
        """Configure the Telegram channel.

        Args:
            config: Channel configuration with bot_token in credentials

        Raises:
            ValueError: If bot_token is missing
        """
        errors = self.validate_config(config)
        if errors:
            raise ValueError(f"Invalid configuration: {', '.join(errors)}")

        self._config = config
        credentials = config.get("credentials", {})
        settings = config.get("settings", {})

        bot_token = credentials.get("bot_token", "")
        if not bot_token:
            raise ValueError("Missing required bot_token in credentials")

        self._parse_mode = settings.get("parse_mode", "markdown")
        self._disable_notification = settings.get("disable_notification", False)

        # Create bot instance
        self._bot = Bot(token=bot_token)

        # Create application for receiving updates
        self._application = Application.builder().token(bot_token).build()

        # Register message handler
        self._application.add_handler(
            MessageHandler(
                filters.ALL & ~filters.COMMAND,
                self._handle_telegram_message,
            )
        )

    async def start(self) -> None:
        """Start the Telegram channel and begin receiving updates."""
        if not self._bot or not self._application:
            raise ConnectionError("Channel not configured. Call configure() first.")

        self._status = ChannelStatus.CONNECTING
        self._emit_status(self._status)

        try:
            # Get bot info
            bot_info = await self._bot.get_me()
            self._bot_info = {
                "id": bot_info.id,
                "username": bot_info.username,
                "first_name": bot_info.first_name,
                "can_join_groups": bot_info.can_join_groups,
                "can_read_all_group_messages": bot_info.can_read_all_group_messages,
            }
            logger.info(f"Telegram bot connected: @{bot_info.username}")

            # Initialize the application
            await self._application.initialize()

            # Check for webhook configuration
            webhook_url = self._config.get("settings", {}).get("webhook_url")
            if webhook_url:
                await self._bot.set_webhook(url=webhook_url)
                logger.info(f"Telegram webhook set: {webhook_url}")
            else:
                # Start polling in background
                await self._application.start()
                self._polling_task = asyncio.create_task(self._application.updater.start_polling(drop_pending_updates=True))
                logger.info("Telegram polling started")

            self._status = ChannelStatus.CONNECTED
            self._emit_status(self._status)

        except TelegramError as e:
            self._status = ChannelStatus.ERROR
            self._emit_status(self._status)
            raise ConnectionError(f"Failed to connect to Telegram: {e}")

    async def stop(self) -> None:
        """Stop the Telegram channel gracefully."""
        self._status = ChannelStatus.DISCONNECTED

        if self._polling_task:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
            self._polling_task = None

        if self._application:
            try:
                if self._application.updater and self._application.updater.running:
                    await self._application.updater.stop()
                await self._application.stop()
                await self._application.shutdown()
            except Exception as e:
                logger.warning(f"Error stopping Telegram application: {e}")

        self._emit_status(self._status)
        logger.info("Telegram channel stopped")

    # --- Messaging ---

    async def send_message(self, message: ChannelMessage) -> MessageDeliveryResult:
        """Send a message through Telegram.

        Args:
            message: Message to send with to, text, and optional media

        Returns:
            MessageDeliveryResult with success status and message ID
        """
        if not self._bot:
            return MessageDeliveryResult(
                success=False,
                error="Channel not started",
                error_code="NOT_STARTED",
            )

        chat_id = message.get("to", "")
        if not chat_id:
            return MessageDeliveryResult(
                success=False,
                error="Missing recipient (to)",
                error_code="MISSING_RECIPIENT",
            )

        text = message.get("text", "")
        media = message.get("media", [])
        parse_mode = self._resolve_parse_mode(message.get("parse_mode"))
        reply_to_id = message.get("reply_to_id")
        thread_id = message.get("thread_id")
        silent = message.get("silent", self._disable_notification)

        # Build optional parameters
        kwargs: dict[str, Any] = {}
        if parse_mode:
            kwargs["parse_mode"] = parse_mode
        if silent:
            kwargs["disable_notification"] = True
        if reply_to_id:
            kwargs["reply_to_message_id"] = int(reply_to_id)
        if thread_id:
            kwargs["message_thread_id"] = int(thread_id)

        try:
            # Send media if present
            if media:
                return await self._send_media(chat_id, media, text, kwargs)

            # Send text message
            if not text:
                return MessageDeliveryResult(
                    success=False,
                    error="Message must have text or media",
                    error_code="EMPTY_MESSAGE",
                )

            # Split long messages
            if len(text) > MAX_TEXT_LENGTH:
                return await self._send_long_message(chat_id, text, kwargs)

            result = await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                **kwargs,
            )

            return MessageDeliveryResult(
                success=True,
                message_id=str(result.message_id),
            )

        except Forbidden as e:
            return MessageDeliveryResult(
                success=False,
                error=f"Bot blocked or kicked: {e}",
                error_code="FORBIDDEN",
            )
        except BadRequest as e:
            error_msg = str(e)
            # Handle parse errors by falling back to plain text
            if "can't parse" in error_msg.lower():
                try:
                    kwargs.pop("parse_mode", None)
                    result = await self._bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        **kwargs,
                    )
                    return MessageDeliveryResult(
                        success=True,
                        message_id=str(result.message_id),
                    )
                except TelegramError as fallback_error:
                    return MessageDeliveryResult(
                        success=False,
                        error=str(fallback_error),
                        error_code="BAD_REQUEST",
                    )

            return MessageDeliveryResult(
                success=False,
                error=error_msg,
                error_code="BAD_REQUEST",
            )
        except (NetworkError, TimedOut) as e:
            return MessageDeliveryResult(
                success=False,
                error=f"Network error: {e}",
                error_code="NETWORK_ERROR",
                retry_after=5,
            )
        except TelegramError as e:
            return MessageDeliveryResult(
                success=False,
                error=str(e),
                error_code="TELEGRAM_ERROR",
            )

    async def _send_long_message(
        self,
        chat_id: str,
        text: str,
        kwargs: dict[str, Any],
    ) -> MessageDeliveryResult:
        """Send a message that exceeds Telegram's length limit."""
        if not self._bot:
            return MessageDeliveryResult(success=False, error="Bot not initialized")

        # Split into chunks
        chunks = self._split_message(text, MAX_TEXT_LENGTH)
        last_message_id = None

        for chunk in chunks:
            result = await self._bot.send_message(
                chat_id=chat_id,
                text=chunk,
                **kwargs,
            )
            last_message_id = result.message_id

        return MessageDeliveryResult(
            success=True,
            message_id=str(last_message_id) if last_message_id else None,
        )

    async def _send_media(
        self,
        chat_id: str,
        media: list[MediaAttachment],
        caption: str | None,
        kwargs: dict[str, Any],
    ) -> MessageDeliveryResult:
        """Send media attachments."""
        if not self._bot or not media:
            return MessageDeliveryResult(success=False, error="No media to send")

        # Use first attachment (Telegram groups media differently)
        attachment = media[0]
        media_type = attachment.get("type", "file")
        url = attachment.get("url")
        data = attachment.get("data")
        filename = attachment.get("filename")

        # Truncate caption if needed
        if caption and len(caption) > MAX_CAPTION_LENGTH:
            caption = caption[: MAX_CAPTION_LENGTH - 3] + "..."

        if caption:
            kwargs["caption"] = caption

        try:
            # Prepare the file
            if data:
                file_input = InputFile(data, filename=filename)
            elif url:
                file_input = url
            else:
                return MessageDeliveryResult(
                    success=False,
                    error="Media must have url or data",
                    error_code="INVALID_MEDIA",
                )

            # Send based on type
            result: Message
            if media_type == "image":
                result = await self._bot.send_photo(
                    chat_id=chat_id,
                    photo=file_input,
                    **kwargs,
                )
            elif media_type == "video":
                result = await self._bot.send_video(
                    chat_id=chat_id,
                    video=file_input,
                    **kwargs,
                )
            elif media_type == "audio":
                result = await self._bot.send_audio(
                    chat_id=chat_id,
                    audio=file_input,
                    **kwargs,
                )
            elif media_type == "voice":
                result = await self._bot.send_voice(
                    chat_id=chat_id,
                    voice=file_input,
                    **kwargs,
                )
            elif media_type == "sticker":
                result = await self._bot.send_sticker(
                    chat_id=chat_id,
                    sticker=file_input,
                    **kwargs,
                )
            else:
                # Default to document
                result = await self._bot.send_document(
                    chat_id=chat_id,
                    document=file_input,
                    **kwargs,
                )

            return MessageDeliveryResult(
                success=True,
                message_id=str(result.message_id),
            )

        except TelegramError as e:
            return MessageDeliveryResult(
                success=False,
                error=str(e),
                error_code="MEDIA_ERROR",
            )

    async def edit_message(
        self,
        message_id: str,
        text: str,
        chat_id: str | None = None,
    ) -> MessageDeliveryResult:
        """Edit a previously sent message."""
        if not self._bot:
            return MessageDeliveryResult(success=False, error="Bot not initialized")

        if not chat_id:
            return MessageDeliveryResult(
                success=False,
                error="chat_id required for Telegram edit",
                error_code="MISSING_CHAT_ID",
            )

        try:
            await self._bot.edit_message_text(
                chat_id=chat_id,
                message_id=int(message_id),
                text=text,
                parse_mode=self._resolve_parse_mode(self._parse_mode),
            )
            return MessageDeliveryResult(success=True, message_id=message_id)

        except TelegramError as e:
            return MessageDeliveryResult(
                success=False,
                error=str(e),
                error_code="EDIT_ERROR",
            )

    async def delete_message(
        self,
        message_id: str,
        chat_id: str | None = None,
    ) -> MessageDeliveryResult:
        """Delete a message."""
        if not self._bot:
            return MessageDeliveryResult(success=False, error="Bot not initialized")

        if not chat_id:
            return MessageDeliveryResult(
                success=False,
                error="chat_id required for Telegram delete",
                error_code="MISSING_CHAT_ID",
            )

        try:
            await self._bot.delete_message(
                chat_id=chat_id,
                message_id=int(message_id),
            )
            return MessageDeliveryResult(success=True, message_id=message_id)

        except TelegramError as e:
            return MessageDeliveryResult(
                success=False,
                error=str(e),
                error_code="DELETE_ERROR",
            )

    async def send_reaction(
        self,
        message_id: str,
        reaction: str,
        chat_id: str | None = None,
    ) -> MessageDeliveryResult:
        """React to a message with an emoji."""
        if not self._bot:
            return MessageDeliveryResult(success=False, error="Bot not initialized")

        if not chat_id:
            return MessageDeliveryResult(
                success=False,
                error="chat_id required for Telegram reaction",
                error_code="MISSING_CHAT_ID",
            )

        try:
            await self._bot.set_message_reaction(
                chat_id=chat_id,
                message_id=int(message_id),
                reaction=[{"type": "emoji", "emoji": reaction}] if reaction else [],
            )
            return MessageDeliveryResult(success=True, message_id=message_id)

        except TelegramError as e:
            return MessageDeliveryResult(
                success=False,
                error=str(e),
                error_code="REACTION_ERROR",
            )

    async def send_typing(self, chat_id: str) -> None:
        """Send a typing indicator."""
        if not self._bot:
            return

        try:
            await self._bot.send_chat_action(
                chat_id=chat_id,
                action=ChatAction.TYPING,
            )
        except TelegramError as e:
            logger.debug(f"Failed to send typing indicator: {e}")

    # --- Internal Handlers ---

    async def _handle_telegram_message(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handle incoming Telegram messages."""
        message = update.message or update.edited_message
        if not message:
            return

        # Skip messages from bots
        if message.from_user and message.from_user.is_bot:
            return

        # Build message event
        event = self._build_message_event(message, edited=update.edited_message is not None)
        self._emit_message(event)

    def _build_message_event(self, message: Message, edited: bool = False) -> ChannelMessageEvent:
        """Build a ChannelMessageEvent from a Telegram message."""
        chat = message.chat
        sender = message.from_user

        # Determine chat type
        chat_type: str = "dm"
        if chat.type in ("group", "supergroup"):
            chat_type = "group"
        elif chat.type == "channel":
            chat_type = "channel"

        # Extract text content
        text = message.text or message.caption

        # Extract media attachments
        media: list[MediaAttachment] = []
        if message.photo:
            # Get largest photo
            largest = max(message.photo, key=lambda p: p.file_size or 0)
            media.append(
                MediaAttachment(
                    type="image",
                    filename=f"photo_{largest.file_id}.jpg",
                )
            )
        if message.video:
            media.append(
                MediaAttachment(
                    type="video",
                    filename=message.video.file_name,
                    mime_type=message.video.mime_type,
                    size_bytes=message.video.file_size,
                )
            )
        if message.audio:
            media.append(
                MediaAttachment(
                    type="audio",
                    filename=message.audio.file_name,
                    mime_type=message.audio.mime_type,
                    size_bytes=message.audio.file_size,
                )
            )
        if message.voice:
            media.append(
                MediaAttachment(
                    type="voice",
                    mime_type=message.voice.mime_type,
                    size_bytes=message.voice.file_size,
                )
            )
        if message.document:
            media.append(
                MediaAttachment(
                    type="file",
                    filename=message.document.file_name,
                    mime_type=message.document.mime_type,
                    size_bytes=message.document.file_size,
                )
            )
        if message.sticker:
            media.append(
                MediaAttachment(
                    type="sticker",
                    filename=message.sticker.file_id,
                )
            )

        # Build event
        return ChannelMessageEvent(
            event_id=str(uuid4()),
            channel_id="telegram",
            message_id=str(message.message_id),
            sender_id=str(sender.id) if sender else "",
            sender_name=(f"{sender.first_name or ''} {sender.last_name or ''}".strip() if sender else None),
            sender_handle=f"@{sender.username}" if sender and sender.username else None,
            chat_id=str(chat.id),
            chat_type=chat_type,  # type: ignore
            chat_name=chat.title or chat.username,
            thread_id=str(message.message_thread_id) if message.message_thread_id else None,
            reply_to_id=(str(message.reply_to_message.message_id) if message.reply_to_message else None),
            text=text,
            media=media if media else None,
            raw={
                "message_id": message.message_id,
                "chat_id": chat.id,
                "date": message.date.isoformat() if message.date else None,
            },
            timestamp=message.date or datetime.utcnow(),
            edited=edited,
            is_bot=sender.is_bot if sender else False,
        )

    # --- Helpers ---

    def _resolve_parse_mode(self, mode: str | None) -> str | None:
        """Convert parse mode string to Telegram ParseMode."""
        if mode == "markdown":
            return ParseMode.MARKDOWN_V2
        elif mode == "html":
            return ParseMode.HTML
        elif mode == "text" or mode is None:
            return None
        return mode

    @staticmethod
    def _split_message(text: str, max_length: int) -> list[str]:
        """Split a message into chunks at word boundaries."""
        if len(text) <= max_length:
            return [text]

        chunks = []
        while text:
            if len(text) <= max_length:
                chunks.append(text)
                break

            # Find split point
            split_point = text.rfind("\n", 0, max_length)
            if split_point == -1:
                split_point = text.rfind(" ", 0, max_length)
            if split_point == -1:
                split_point = max_length

            chunks.append(text[:split_point].rstrip())
            text = text[split_point:].lstrip()

        return chunks


def register_channel() -> ChannelPlugin:
    """Factory function for plugin discovery."""
    return TelegramChannel()
