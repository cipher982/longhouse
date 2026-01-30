"""Tests for channel webhooks router."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from zerg.channels.registry import ChannelRegistry
from zerg.channels.sdk import WebhookChannel
from zerg.channels.types import ChannelCapabilities
from zerg.channels.types import ChannelMeta
from zerg.channels.types import ChannelStatus
from zerg.channels.types import MessageDeliveryResult
from zerg.routers.channels_webhooks import router


class MockWebhookChannel(WebhookChannel):
    """Mock webhook channel for testing."""

    def __init__(self, channel_id: str = "mock") -> None:
        super().__init__()
        self._channel_id = channel_id
        self._handled_payloads: list[dict] = []

    @property
    def meta(self) -> ChannelMeta:
        return {
            "id": self._channel_id,
            "name": "Mock Channel",
            "description": "Mock channel for testing",
        }

    @property
    def capabilities(self) -> ChannelCapabilities:
        return {
            "send_text": True,
            "receive_messages": True,
        }

    async def _do_connect(self) -> None:
        pass

    async def _do_disconnect(self) -> None:
        pass

    async def _do_send(self, message: Any) -> MessageDeliveryResult:
        return MessageDeliveryResult(success=True)

    async def handle_webhook(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        self._handled_payloads.append(payload)
        return {"status": "handled", "payload_keys": list(payload.keys())}


@pytest.fixture
def mock_registry():
    """Create a mock channel registry."""
    registry = ChannelRegistry()
    return registry


@pytest.fixture
def app(mock_registry):
    """Create test FastAPI app with the webhook router."""
    app = FastAPI()
    app.include_router(router, prefix="/api")

    # Patch the registry
    with patch("zerg.routers.channels_webhooks.get_registry", return_value=mock_registry):
        yield app


@pytest.fixture
def client(app):
    """Create test client."""
    return TestClient(app)


class TestChannelWebhookEndpoint:
    """Tests for the channel webhook endpoint."""

    def test_webhook_unknown_channel_returns_404(self, client, mock_registry):
        """Test that unknown channel returns 404."""
        with patch("zerg.routers.channels_webhooks.get_registry", return_value=mock_registry):
            response = client.post(
                "/api/webhooks/channels/unknown",
                json={"message": "test"},
            )
            assert response.status_code == 404
            assert "not found" in response.json()["detail"].lower()

    def test_webhook_non_webhook_channel_returns_400(self, client, mock_registry):
        """Test that non-webhook channel returns 400."""
        # Create a mock non-webhook channel
        from zerg.channels.plugin import ChannelPlugin

        class NonWebhookChannel(ChannelPlugin):
            @property
            def meta(self):
                return {"id": "non-webhook", "name": "Non Webhook"}

            @property
            def capabilities(self):
                return {}

            @property
            def status(self):
                return ChannelStatus.DISCONNECTED

            async def configure(self, config):
                pass

            async def start(self):
                pass

            async def stop(self):
                pass

            async def send_message(self, message):
                return MessageDeliveryResult(success=True)

        mock_registry.register(NonWebhookChannel())

        with patch("zerg.routers.channels_webhooks.get_registry", return_value=mock_registry):
            response = client.post(
                "/api/webhooks/channels/non-webhook",
                json={"message": "test"},
            )
            assert response.status_code == 400
            assert "does not support webhooks" in response.json()["detail"]

    def test_webhook_success(self, client, mock_registry):
        """Test successful webhook handling."""
        mock_channel = MockWebhookChannel("test-channel")
        mock_registry.register(mock_channel)

        with patch("zerg.routers.channels_webhooks.get_registry", return_value=mock_registry):
            response = client.post(
                "/api/webhooks/channels/test-channel",
                json={"update_id": 123, "message": {"text": "hello"}},
            )
            assert response.status_code == 200
            assert response.json()["status"] == "handled"
            assert len(mock_channel._handled_payloads) == 1

    def test_webhook_invalid_json_returns_400(self, client, mock_registry):
        """Test that invalid JSON returns 400."""
        mock_channel = MockWebhookChannel("test-channel")
        mock_registry.register(mock_channel)

        with patch("zerg.routers.channels_webhooks.get_registry", return_value=mock_registry):
            response = client.post(
                "/api/webhooks/channels/test-channel",
                content="not valid json",
                headers={"Content-Type": "application/json"},
            )
            assert response.status_code == 400
            assert "invalid json" in response.json()["detail"].lower()

    def test_webhook_body_too_large_returns_413(self, client, mock_registry):
        """Test that oversized body returns 413."""
        mock_channel = MockWebhookChannel("test-channel")
        mock_registry.register(mock_channel)

        # Create a large payload (> 128 KiB)
        large_payload = {"data": "x" * (130 * 1024)}

        with patch("zerg.routers.channels_webhooks.get_registry", return_value=mock_registry):
            response = client.post(
                "/api/webhooks/channels/test-channel",
                json=large_payload,
            )
            assert response.status_code == 413

    def test_webhook_signature_validation_failure(self, client, mock_registry):
        """Test that invalid signature returns 401."""

        class SecretChannel(MockWebhookChannel):
            def __init__(self):
                super().__init__("secret-channel")
                self._webhook_secret = "my-secret"

            def validate_webhook_signature(self, payload: bytes, signature: str) -> bool:
                return signature == self._webhook_secret

        secret_channel = SecretChannel()
        mock_registry.register(secret_channel)

        with patch("zerg.routers.channels_webhooks.get_registry", return_value=mock_registry):
            # Request without secret token
            response = client.post(
                "/api/webhooks/channels/secret-channel",
                json={"message": "test"},
            )
            assert response.status_code == 401
            assert "invalid webhook signature" in response.json()["detail"].lower()

    def test_webhook_signature_validation_success(self, client, mock_registry):
        """Test that valid signature is accepted."""

        class SecretChannel(MockWebhookChannel):
            def __init__(self):
                super().__init__("secret-channel")
                self._webhook_secret = "my-secret"

            def validate_webhook_signature(self, payload: bytes, signature: str) -> bool:
                return signature == self._webhook_secret

        secret_channel = SecretChannel()
        mock_registry.register(secret_channel)

        with patch("zerg.routers.channels_webhooks.get_registry", return_value=mock_registry):
            response = client.post(
                "/api/webhooks/channels/secret-channel",
                json={"message": "test"},
                headers={"X-Telegram-Bot-Api-Secret-Token": "my-secret"},
            )
            assert response.status_code == 200

    def test_webhook_handler_error_returns_500(self, client, mock_registry):
        """Test that handler errors return 500."""

        class ErrorChannel(MockWebhookChannel):
            async def handle_webhook(self, payload: dict[str, Any]) -> dict[str, Any] | None:
                raise RuntimeError("Simulated error")

        error_channel = ErrorChannel("error-channel")
        mock_registry.register(error_channel)

        with patch("zerg.routers.channels_webhooks.get_registry", return_value=mock_registry):
            response = client.post(
                "/api/webhooks/channels/error-channel",
                json={"message": "test"},
            )
            assert response.status_code == 500
            assert "simulated error" in response.json()["detail"].lower()


class TestTelegramWebhookIntegration:
    """Integration tests for Telegram webhook handling."""

    @pytest.mark.asyncio
    async def test_telegram_handle_webhook_message(self):
        """Test that TelegramChannel handles webhook messages correctly."""
        from zerg.channels.plugins.telegram import TelegramChannel

        channel = TelegramChannel()

        # Mock the bot
        mock_bot = MagicMock()
        channel._bot = mock_bot

        # Create a mock Telegram update payload
        payload = {
            "update_id": 123456789,
            "message": {
                "message_id": 1,
                "date": 1704067200,
                "chat": {"id": 123456, "type": "private"},
                "from": {
                    "id": 987654,
                    "first_name": "Test",
                    "is_bot": False,
                },
                "text": "Hello from webhook!",
            },
        }

        # Track emitted messages
        emitted_events = []
        channel.on_message(lambda event: emitted_events.append(event))

        # Handle the webhook
        with patch("zerg.channels.plugins.telegram.Update.de_json") as mock_de_json:
            # Create mock Update object
            mock_update = MagicMock()
            mock_update.message = MagicMock()
            mock_update.message.message_id = 1
            mock_update.message.text = "Hello from webhook!"
            mock_update.message.caption = None
            mock_update.message.chat = MagicMock()
            mock_update.message.chat.id = 123456
            mock_update.message.chat.type = "private"
            mock_update.message.chat.title = None
            mock_update.message.chat.username = "testuser"
            mock_update.message.from_user = MagicMock()
            mock_update.message.from_user.id = 987654
            mock_update.message.from_user.first_name = "Test"
            mock_update.message.from_user.last_name = None
            mock_update.message.from_user.username = "testuser"
            mock_update.message.from_user.is_bot = False
            mock_update.message.date = None
            mock_update.message.message_thread_id = None
            mock_update.message.reply_to_message = None
            mock_update.message.photo = []
            mock_update.message.video = None
            mock_update.message.audio = None
            mock_update.message.voice = None
            mock_update.message.document = None
            mock_update.message.sticker = None
            mock_update.edited_message = None
            mock_update.channel_post = None
            mock_update.edited_channel_post = None
            mock_update.callback_query = None

            mock_de_json.return_value = mock_update

            result = await channel.handle_webhook(payload)

        assert result["status"] == "ok"
        assert len(emitted_events) == 1
        assert emitted_events[0]["text"] == "Hello from webhook!"
        assert emitted_events[0]["sender_id"] == "987654"

    @pytest.mark.asyncio
    async def test_telegram_handle_webhook_skips_bot_messages(self):
        """Test that TelegramChannel skips bot messages."""
        from zerg.channels.plugins.telegram import TelegramChannel

        channel = TelegramChannel()
        channel._bot = MagicMock()

        payload = {"update_id": 123}

        emitted_events = []
        channel.on_message(lambda event: emitted_events.append(event))

        with patch("zerg.channels.plugins.telegram.Update.de_json") as mock_de_json:
            mock_update = MagicMock()
            mock_update.message = MagicMock()
            mock_update.message.from_user = MagicMock()
            mock_update.message.from_user.is_bot = True
            mock_update.edited_message = None
            mock_update.channel_post = None
            mock_update.edited_channel_post = None
            mock_update.callback_query = None

            mock_de_json.return_value = mock_update

            result = await channel.handle_webhook(payload)

        assert result["status"] == "ok"
        assert result.get("skipped") == "bot_message"
        assert len(emitted_events) == 0

    def test_telegram_validate_webhook_signature_no_secret(self):
        """Test signature validation with no secret configured."""
        from zerg.channels.plugins.telegram import TelegramChannel

        channel = TelegramChannel()
        channel._webhook_secret = None

        # Should allow any request when no secret is configured
        assert channel.validate_webhook_signature(b"payload", "") is True
        assert channel.validate_webhook_signature(b"payload", "any-token") is True

    def test_telegram_validate_webhook_signature_with_secret(self):
        """Test signature validation with secret configured."""
        from zerg.channels.plugins.telegram import TelegramChannel

        channel = TelegramChannel()
        channel._webhook_secret = "my-secret-token"

        # Should reject wrong token
        assert channel.validate_webhook_signature(b"payload", "wrong-token") is False

        # Should accept correct token
        assert channel.validate_webhook_signature(b"payload", "my-secret-token") is True

    @pytest.mark.asyncio
    async def test_telegram_handle_webhook_invalid_update(self):
        """Test handling of invalid/unparseable update."""
        from zerg.channels.plugins.telegram import TelegramChannel

        channel = TelegramChannel()
        channel._bot = MagicMock()

        payload = {"invalid": "structure"}

        with patch("zerg.channels.plugins.telegram.Update.de_json") as mock_de_json:
            mock_de_json.return_value = None  # Failed to parse

            result = await channel.handle_webhook(payload)

        assert result["status"] == "error"
        assert "invalid update format" in result["message"].lower()
