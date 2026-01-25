"""Channel Plugin SDK.

Provides utilities and base classes for developing third-party channel plugins.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from abc import abstractmethod
from datetime import datetime
from datetime import timedelta
from typing import Any

from zerg.channels.plugin import ChannelPlugin
from zerg.channels.types import ChannelCapabilities
from zerg.channels.types import ChannelConfig
from zerg.channels.types import ChannelMessage
from zerg.channels.types import ChannelMessageEvent
from zerg.channels.types import ChannelMeta
from zerg.channels.types import ChannelStatus
from zerg.channels.types import MessageDeliveryResult

logger = logging.getLogger(__name__)


class BaseChannel(ChannelPlugin):
    """Base class with common functionality for channel plugins.

    Provides:
    - Automatic status management
    - Retry logic for message sending
    - Rate limiting support
    - Configuration validation

    Example:
        class MyChannel(BaseChannel):
            @property
            def meta(self) -> ChannelMeta:
                return {"id": "mychannel", "name": "My Channel", ...}

            async def _do_send(self, message: ChannelMessage) -> MessageDeliveryResult:
                # Platform-specific sending logic
                ...
    """

    def __init__(self) -> None:
        """Initialize the base channel."""
        self._status = ChannelStatus.DISCONNECTED
        self._config: ChannelConfig = {}
        self._message_handlers: list = []
        self._typing_handlers: list = []
        self._status_handlers: list = []

        # Retry settings
        self._max_retries = 3
        self._retry_delay_base = 1.0  # seconds
        self._retry_delay_max = 30.0

        # Rate limiting
        self._rate_limit_remaining: int | None = None
        self._rate_limit_reset: datetime | None = None

    @property
    def status(self) -> ChannelStatus:
        return self._status

    def _set_status(self, status: ChannelStatus) -> None:
        """Update status and notify handlers."""
        if self._status != status:
            self._status = status
            self._emit_status(status)

    # --- Abstract methods that subclasses must implement ---

    @property
    @abstractmethod
    def meta(self) -> ChannelMeta:
        """Return plugin metadata."""
        ...

    @property
    @abstractmethod
    def capabilities(self) -> ChannelCapabilities:
        """Return capabilities."""
        ...

    @abstractmethod
    async def _do_connect(self) -> None:
        """Platform-specific connection logic.

        Called by start(). Should establish connection to the platform.
        Raise ConnectionError on failure.
        """
        ...

    @abstractmethod
    async def _do_disconnect(self) -> None:
        """Platform-specific disconnection logic.

        Called by stop(). Should cleanly close connections.
        """
        ...

    @abstractmethod
    async def _do_send(self, message: ChannelMessage) -> MessageDeliveryResult:
        """Platform-specific message sending.

        Called by send_message() after validation and retry logic.

        Args:
            message: The message to send

        Returns:
            MessageDeliveryResult
        """
        ...

    # --- Template methods ---

    async def configure(self, config: ChannelConfig) -> None:
        """Configure the channel.

        Override _on_configure for custom configuration handling.
        """
        errors = self.validate_config(config)
        if errors:
            raise ValueError(f"Invalid configuration: {', '.join(errors)}")

        self._config = config
        await self._on_configure(config)

    async def _on_configure(self, config: ChannelConfig) -> None:
        """Hook for custom configuration handling.

        Override in subclass to handle configuration changes.
        """
        pass

    async def start(self) -> None:
        """Start the channel connection with automatic status management."""
        if self._status == ChannelStatus.CONNECTED:
            return

        self._set_status(ChannelStatus.CONNECTING)

        try:
            await self._do_connect()
            self._set_status(ChannelStatus.CONNECTED)
        except Exception as e:
            self._set_status(ChannelStatus.ERROR)
            raise ConnectionError(f"Failed to connect: {e}") from e

    async def stop(self) -> None:
        """Stop the channel with automatic status management."""
        if self._status == ChannelStatus.DISCONNECTED:
            return

        try:
            await self._do_disconnect()
        finally:
            self._set_status(ChannelStatus.DISCONNECTED)

    async def send_message(self, message: ChannelMessage) -> MessageDeliveryResult:
        """Send a message with automatic retry logic."""
        if self._status != ChannelStatus.CONNECTED:
            return MessageDeliveryResult(
                success=False,
                error="Channel not connected",
                error_code="NOT_CONNECTED",
            )

        # Check rate limiting
        if self._is_rate_limited():
            wait_time = self._get_rate_limit_wait()
            if wait_time > 0:
                return MessageDeliveryResult(
                    success=False,
                    error="Rate limited",
                    error_code="RATE_LIMITED",
                    retry_after=int(wait_time),
                )

        # Retry loop
        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                result = await self._do_send(message)

                # Update rate limit info if provided
                if result.get("retry_after"):
                    self._update_rate_limit(result["retry_after"])

                if result.get("success"):
                    return result

                # Don't retry on certain errors
                if result.get("error_code") in ("INVALID_RECIPIENT", "BLOCKED", "FORBIDDEN"):
                    return result

            except Exception as e:
                last_error = e
                logger.warning(f"Send attempt {attempt + 1}/{self._max_retries} failed: {e}")

            # Calculate retry delay with exponential backoff
            if attempt < self._max_retries - 1:
                delay = min(
                    self._retry_delay_base * (2**attempt),
                    self._retry_delay_max,
                )
                await asyncio.sleep(delay)

        return MessageDeliveryResult(
            success=False,
            error=str(last_error) if last_error else "Max retries exceeded",
            error_code="MAX_RETRIES",
        )

    # --- Rate limiting ---

    def _is_rate_limited(self) -> bool:
        """Check if currently rate limited."""
        if self._rate_limit_remaining is not None and self._rate_limit_remaining <= 0:
            if self._rate_limit_reset and datetime.utcnow() < self._rate_limit_reset:
                return True
        return False

    def _get_rate_limit_wait(self) -> float:
        """Get seconds to wait before rate limit resets."""
        if self._rate_limit_reset:
            wait = (self._rate_limit_reset - datetime.utcnow()).total_seconds()
            return max(0, wait)
        return 0

    def _update_rate_limit(self, retry_after: int) -> None:
        """Update rate limit state."""
        self._rate_limit_remaining = 0
        self._rate_limit_reset = datetime.utcnow() + timedelta(seconds=retry_after)


class PollingChannel(BaseChannel):
    """Base class for channels that poll for new messages.

    Provides automatic polling loop with configurable interval.

    Example:
        class EmailChannel(PollingChannel):
            async def _poll_messages(self) -> list[ChannelMessageEvent]:
                # Check for new emails
                return new_messages
    """

    def __init__(self, poll_interval: float = 5.0) -> None:
        """Initialize the polling channel.

        Args:
            poll_interval: Seconds between polls (default: 5)
        """
        super().__init__()
        self._poll_interval = poll_interval
        self._poll_task: asyncio.Task | None = None

    @abstractmethod
    async def _poll_messages(self) -> list[ChannelMessageEvent]:
        """Poll for new messages.

        Override to implement platform-specific polling.

        Returns:
            List of new message events
        """
        ...

    async def _do_connect(self) -> None:
        """Start the polling loop."""
        self._poll_task = asyncio.create_task(self._polling_loop())

    async def _do_disconnect(self) -> None:
        """Stop the polling loop."""
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

    async def _polling_loop(self) -> None:
        """Main polling loop."""
        while True:
            try:
                messages = await self._poll_messages()
                for message in messages:
                    self._emit_message(message)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Polling error: {e}")

            await asyncio.sleep(self._poll_interval)


class WebhookChannel(BaseChannel):
    """Base class for channels that receive webhooks.

    Provides webhook URL generation and payload validation.

    Example:
        class TelegramChannel(WebhookChannel):
            async def handle_webhook(self, payload: dict) -> None:
                # Parse Telegram update
                event = self._parse_update(payload)
                self._emit_message(event)
    """

    def __init__(self) -> None:
        """Initialize the webhook channel."""
        super().__init__()
        self._webhook_secret: str | None = None

    @property
    def webhook_path(self) -> str:
        """Return the webhook path for this channel.

        Override to customize the webhook URL path.
        """
        return f"/webhooks/channels/{self.meta['id']}"

    @abstractmethod
    async def handle_webhook(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Handle an incoming webhook request.

        Args:
            payload: The webhook payload (parsed JSON)

        Returns:
            Optional response to send back
        """
        ...

    def validate_webhook_signature(
        self,
        payload: bytes,
        signature: str,
    ) -> bool:
        """Validate webhook signature.

        Override to implement platform-specific signature validation.
        Use the helper methods below for common signature formats.

        WARNING: Default implementation returns True (no validation).
        Production channels should override this method.

        Args:
            payload: Raw request body
            signature: Signature header value

        Returns:
            True if signature is valid
        """
        # Log warning if no secret is set but signature validation is called
        if not self._webhook_secret:
            logger.warning(f"Webhook signature validation called for {self.meta['id']} " "but no secret is configured. Allowing request.")
        return True

    def _validate_hmac_sha256(
        self,
        payload: bytes,
        signature: str,
        secret: str | bytes,
        *,
        prefix: str = "",
    ) -> bool:
        """Validate HMAC-SHA256 signature (used by many platforms).

        Args:
            payload: Raw request body bytes
            signature: Signature header value
            secret: Secret key for HMAC
            prefix: Optional prefix to strip (e.g., "sha256=")

        Returns:
            True if signature matches
        """
        if prefix and signature.startswith(prefix):
            signature = signature[len(prefix) :]

        if isinstance(secret, str):
            secret = secret.encode("utf-8")

        expected = hmac.new(secret, payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature.lower(), expected.lower())

    def _validate_hmac_sha1(
        self,
        payload: bytes,
        signature: str,
        secret: str | bytes,
        *,
        prefix: str = "",
    ) -> bool:
        """Validate HMAC-SHA1 signature (used by GitHub, etc.).

        Args:
            payload: Raw request body bytes
            signature: Signature header value
            secret: Secret key for HMAC
            prefix: Optional prefix to strip (e.g., "sha1=")

        Returns:
            True if signature matches
        """
        if prefix and signature.startswith(prefix):
            signature = signature[len(prefix) :]

        if isinstance(secret, str):
            secret = secret.encode("utf-8")

        expected = hmac.new(secret, payload, hashlib.sha1).hexdigest()
        return hmac.compare_digest(signature.lower(), expected.lower())


# --- Utility Functions ---


def create_message_event(
    channel_id: str,
    message_id: str,
    text: str | None = None,
    sender_id: str = "",
    chat_id: str = "",
    **kwargs: Any,
) -> ChannelMessageEvent:
    """Helper to create a ChannelMessageEvent.

    Args:
        channel_id: Channel ID
        message_id: Platform message ID
        text: Message text
        sender_id: Sender ID
        chat_id: Chat/conversation ID
        **kwargs: Additional event fields

    Returns:
        ChannelMessageEvent
    """
    from uuid import uuid4

    return ChannelMessageEvent(
        event_id=str(uuid4()),
        channel_id=channel_id,
        message_id=message_id,
        sender_id=sender_id,
        chat_id=chat_id or sender_id,
        chat_type=kwargs.pop("chat_type", "dm"),
        text=text,
        timestamp=kwargs.pop("timestamp", datetime.utcnow()),
        edited=kwargs.pop("edited", False),
        is_bot=kwargs.pop("is_bot", False),
        **kwargs,
    )


def chunk_text(text: str, max_length: int = 4096) -> list[str]:
    """Split text into chunks that fit platform limits.

    Tries to split on sentence/paragraph boundaries.

    Args:
        text: Text to split
        max_length: Maximum chunk length

    Returns:
        List of text chunks
    """
    if len(text) <= max_length:
        return [text]

    chunks = []
    remaining = text

    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break

        # Find a good split point
        split_point = max_length

        # Try to split on paragraph
        para_break = remaining.rfind("\n\n", 0, max_length)
        if para_break > max_length // 2:
            split_point = para_break + 2
        else:
            # Try to split on sentence
            for sep in (". ", "! ", "? ", "\n"):
                pos = remaining.rfind(sep, 0, max_length)
                if pos > max_length // 2:
                    split_point = pos + len(sep)
                    break
            else:
                # Try to split on word
                space = remaining.rfind(" ", 0, max_length)
                if space > max_length // 2:
                    split_point = space + 1

        chunks.append(remaining[:split_point].rstrip())
        remaining = remaining[split_point:].lstrip()

    return chunks
