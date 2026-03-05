"""Tests for TelegramBridge - Telegram ↔ Oikos routing.

Uses mocked TelegramChannel and OikosService.
No real bot token or database required.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from zerg.services.telegram_bridge import TelegramBridge
from zerg.services.telegram_bridge import _format_for_telegram


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(text: str, chat_id: str = "12345", sender_id: str = "99") -> dict:
    return {
        "event_id": "evt-1",
        "channel_id": "telegram",
        "message_id": "1",
        "sender_id": sender_id,
        "chat_id": chat_id,
        "chat_type": "dm",
        "text": text,
        "raw": {"update_id": 123456},
    }


def _make_channel() -> MagicMock:
    """Return a mock TelegramChannel with async send methods."""
    ch = MagicMock()
    ch.send_message = AsyncMock(return_value={"success": True, "message_id": "42"})
    ch.send_typing = AsyncMock()
    # on_message stores the callback and returns an unsubscribe fn
    _callbacks: list = []

    def _on_message(cb):
        _callbacks.append(cb)
        return lambda: _callbacks.remove(cb) if cb in _callbacks else None

    ch.on_message = MagicMock(side_effect=_on_message)
    ch._callbacks = _callbacks
    return ch


async def _dispatch(channel: MagicMock, event: dict) -> None:
    """Trigger the registered on_message callbacks with single arg (matches plugin API)."""
    for cb in channel._callbacks:
        cb(event)  # plugin calls handler(event) — one arg only
    # Let the created tasks run
    await asyncio.sleep(0)
    # Run pending tasks
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# _format_for_telegram tests
# ---------------------------------------------------------------------------


class TestFormatForTelegram:
    def test_plain_text_passthrough(self):
        assert _format_for_telegram("Hello world") == "Hello world"

    def test_bold_asterisk(self):
        result = _format_for_telegram("This is **bold** text")
        assert "<b>bold</b>" in result

    def test_bold_underscore(self):
        result = _format_for_telegram("This is __bold__ text")
        assert "<b>bold</b>" in result

    def test_italic_asterisk(self):
        result = _format_for_telegram("*italic*")
        assert "<i>italic</i>" in result

    def test_inline_code(self):
        result = _format_for_telegram("Run `make test` now")
        assert "<code>make test</code>" in result

    def test_fenced_code_block(self):
        result = _format_for_telegram("```python\nprint('hi')\n```")
        assert "<pre>" in result
        assert "print('hi')" in result

    def test_html_chars_escaped_in_plain_text(self):
        result = _format_for_telegram("A & B < C > D")
        assert "&amp;" in result
        assert "&lt;" in result
        assert "&gt;" in result

    def test_html_chars_not_double_escaped_in_code(self):
        # Code content should keep < > as-is (already inside <code>)
        result = _format_for_telegram("`a < b`")
        # The raw text inside code should still be readable
        assert "<code>" in result

    def test_strikethrough(self):
        result = _format_for_telegram("~~deleted~~")
        assert "<s>deleted</s>" in result

    def test_code_block_content_html_escaped(self):
        """Code content with < > & must be escaped so Telegram HTML parser doesn't choke."""
        result = _format_for_telegram("```\na < b && b > c\n```")
        assert "&lt;" in result
        assert "&gt;" in result
        assert "&amp;" in result
        # Should be wrapped in <pre>
        assert "<pre>" in result

    def test_inline_code_content_html_escaped(self):
        result = _format_for_telegram("`a < b`")
        assert "<code>" in result
        assert "&lt;" in result


# ---------------------------------------------------------------------------
# TelegramBridge routing tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestTelegramBridgeRouting:
    @pytest.fixture(autouse=True)
    def _stub_dedupe_lookup(self, monkeypatch):
        async def _never_duplicate(*_args, **_kwargs):
            return False

        monkeypatch.setattr(TelegramBridge, "_is_duplicate_inbound", _never_duplicate)

    async def test_start_subscribes_to_channel(self):
        ch = _make_channel()
        bridge = TelegramBridge(ch)
        bridge.start()
        ch.on_message.assert_called_once()

    async def test_stop_unsubscribes(self):
        ch = _make_channel()
        bridge = TelegramBridge(ch)
        bridge.start()
        assert len(ch._callbacks) == 1
        bridge.stop()
        assert len(ch._callbacks) == 0

    async def test_empty_message_ignored(self):
        ch = _make_channel()
        bridge = TelegramBridge(ch)
        bridge.start()

        with patch.object(bridge, "_run_oikos") as mock_oikos:
            await _dispatch(ch, _make_event(""))
            mock_oikos.assert_not_called()

        ch.send_message.assert_not_called()

    async def test_start_command_sends_welcome(self):
        ch = _make_channel()
        bridge = TelegramBridge(ch)
        bridge.start()

        await _dispatch(ch, _make_event("/start"))

        ch.send_message.assert_awaited_once()
        args = ch.send_message.call_args[0][0]
        assert "Longhouse" in args["text"]

    async def test_unknown_sender_multitenant_gets_link_prompt(self):
        ch = _make_channel()
        bridge = TelegramBridge(ch)
        bridge.start()

        with patch.object(bridge, "_resolve_user", new=AsyncMock(return_value=None)):
            await _dispatch(ch, _make_event("hello"))

        ch.send_message.assert_awaited_once()
        sent_text = ch.send_message.call_args[0][0]["text"]
        assert "/link" in sent_text

    async def test_message_routes_to_oikos_and_replies(self):
        ch = _make_channel()
        bridge = TelegramBridge(ch)
        bridge.start()

        with (
            patch.object(bridge, "_resolve_user", new=AsyncMock(return_value=7)),
            patch.object(bridge, "_persist_chat_id", new=AsyncMock()),
            patch.object(bridge, "_run_oikos", new=AsyncMock(return_value="Here is your answer!")),
        ):
            await _dispatch(ch, _make_event("what is 2+2?", chat_id="777"))

        ch.send_message.assert_awaited_once()
        sent = ch.send_message.call_args[0][0]
        assert sent["to"] == "777"
        assert "Here is your answer!" in sent["text"]
        assert sent.get("parse_mode") == "html"

    async def test_message_passes_surface_context_to_oikos(self):
        ch = _make_channel()
        bridge = TelegramBridge(ch)
        bridge.start()

        event = _make_event("status?", chat_id="777")
        event["message_id"] = "99"
        event["raw"] = {"update_id": 444}

        mock_run_oikos = AsyncMock(return_value="ok")
        with (
            patch.object(bridge, "_resolve_user", new=AsyncMock(return_value=7)),
            patch.object(bridge, "_persist_chat_id", new=AsyncMock()),
            patch.object(bridge, "_run_oikos", new=mock_run_oikos),
        ):
            await _dispatch(ch, event)

        mock_run_oikos.assert_awaited_once_with(
            7,
            "status?",
            chat_id="777",
            source_message_id="99",
            source_event_id="444",
            source_idempotency_key="telegram:777:444",
        )

    async def test_duplicate_webhook_retry_is_deduped(self):
        ch = _make_channel()
        bridge = TelegramBridge(ch)
        bridge.start()

        mock_run_oikos = AsyncMock(return_value="ok")
        with (
            patch.object(bridge, "_resolve_user", new=AsyncMock(return_value=7)),
            patch.object(bridge, "_persist_chat_id", new=AsyncMock()),
            patch.object(bridge, "_is_duplicate_inbound", new=AsyncMock(return_value=True)) as mock_dedupe,
            patch.object(bridge, "_run_oikos", new=mock_run_oikos),
        ):
            await _dispatch(ch, _make_event("status?", chat_id="777"))

        mock_dedupe.assert_awaited_once_with(7, "telegram:777:123456")
        mock_run_oikos.assert_not_awaited()
        ch.send_message.assert_not_called()

    async def test_dedupe_lookup_error_drops_message(self):
        ch = _make_channel()
        bridge = TelegramBridge(ch)
        bridge.start()

        mock_run_oikos = AsyncMock(return_value="ok")
        with (
            patch.object(bridge, "_resolve_user", new=AsyncMock(return_value=7)),
            patch.object(bridge, "_persist_chat_id", new=AsyncMock()),
            patch.object(bridge, "_is_duplicate_inbound", new=AsyncMock(side_effect=RuntimeError("db down"))),
            patch.object(bridge, "_run_oikos", new=mock_run_oikos),
        ):
            await _dispatch(ch, _make_event("status?", chat_id="777"))

        mock_run_oikos.assert_not_awaited()
        ch.send_message.assert_not_called()

    async def test_missing_update_id_drops_message(self):
        ch = _make_channel()
        bridge = TelegramBridge(ch)
        bridge.start()

        event = _make_event("status?", chat_id="777")
        event["raw"] = {}
        mock_run_oikos = AsyncMock(return_value="ok")
        with (
            patch.object(bridge, "_resolve_user", new=AsyncMock(return_value=7)),
            patch.object(bridge, "_persist_chat_id", new=AsyncMock()),
            patch.object(bridge, "_run_oikos", new=mock_run_oikos),
        ):
            await _dispatch(ch, event)

        mock_run_oikos.assert_not_awaited()
        ch.send_message.assert_not_called()

    async def test_send_failure_logged(self):
        """Delivery failure from channel should be logged, not silently dropped."""
        ch = _make_channel()
        ch.send_message = AsyncMock(return_value={"success": False, "error": "Forbidden", "error_code": "FORBIDDEN"})
        bridge = TelegramBridge(ch)
        bridge.start()

        with (
            patch.object(bridge, "_resolve_user", new=AsyncMock(return_value=1)),
            patch.object(bridge, "_persist_chat_id", new=AsyncMock()),
            patch.object(bridge, "_run_oikos", new=AsyncMock(return_value="reply")),
            patch("zerg.services.telegram_bridge.logger") as mock_log,
        ):
            await _dispatch(ch, _make_event("hi", chat_id="555"))

        mock_log.warning.assert_called()
        warning_msg = str(mock_log.warning.call_args)
        assert "555" in warning_msg or "send failed" in warning_msg.lower()

    async def test_oikos_error_sends_error_message(self):
        ch = _make_channel()
        bridge = TelegramBridge(ch)
        bridge.start()

        with (
            patch.object(bridge, "_resolve_user", new=AsyncMock(return_value=1)),
            patch.object(bridge, "_persist_chat_id", new=AsyncMock()),
            patch.object(bridge, "_run_oikos", new=AsyncMock(side_effect=RuntimeError("boom"))),
        ):
            await _dispatch(ch, _make_event("do something", chat_id="888"))

        sent = ch.send_message.call_args[0][0]
        assert "error" in sent["text"].lower()

    async def test_typing_indicator_sent(self):
        """_keep_typing is scheduled; at minimum one send_typing call must occur."""
        ch = _make_channel()
        bridge = TelegramBridge(ch)
        bridge.start()

        # Make run_oikos slow enough that typing fires at least once
        async def _slow_oikos(*_a, **_kw):
            await asyncio.sleep(0.01)
            return "ok"

        with (
            patch.object(bridge, "_resolve_user", new=AsyncMock(return_value=1)),
            patch.object(bridge, "_persist_chat_id", new=AsyncMock()),
            patch.object(bridge, "_run_oikos", new=AsyncMock(side_effect=_slow_oikos)),
        ):
            await _dispatch(ch, _make_event("hi", chat_id="321"))

        assert ch.send_typing.await_count >= 1


# ---------------------------------------------------------------------------
# Identity resolution tests
# ---------------------------------------------------------------------------


class TestResolveUser:
    @pytest.mark.asyncio
    async def test_single_tenant_returns_admin(self):
        ch = _make_channel()
        bridge = TelegramBridge(ch)

        admin = MagicMock()
        admin.id = 1
        admin.role = "ADMIN"

        settings = MagicMock()
        settings.single_tenant = True

        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = admin

        with (
            patch("zerg.services.telegram_bridge.get_settings", return_value=settings),
            patch("zerg.services.telegram_bridge.db_session") as mock_db_ctx,
        ):
            mock_db_ctx.return_value.__enter__ = MagicMock(return_value=db)
            mock_db_ctx.return_value.__exit__ = MagicMock(return_value=False)

            result = await bridge._resolve_user("any_chat_id")

        assert result == 1

    @pytest.mark.asyncio
    async def test_multitenant_matches_by_context(self):
        ch = _make_channel()
        bridge = TelegramBridge(ch)

        user_a = MagicMock()
        user_a.id = 5
        user_a.context = {"telegram_chat_id": "42"}

        user_b = MagicMock()
        user_b.id = 6
        user_b.context = {"telegram_chat_id": "99"}

        settings = MagicMock()
        settings.single_tenant = False

        db = MagicMock()
        db.query.return_value.all.return_value = [user_a, user_b]

        with (
            patch("zerg.services.telegram_bridge.get_settings", return_value=settings),
            patch("zerg.services.telegram_bridge.db_session") as mock_db_ctx,
        ):
            mock_db_ctx.return_value.__enter__ = MagicMock(return_value=db)
            mock_db_ctx.return_value.__exit__ = MagicMock(return_value=False)

            result = await bridge._resolve_user("99")

        assert result == 6

    @pytest.mark.asyncio
    async def test_multitenant_unknown_returns_none(self):
        ch = _make_channel()
        bridge = TelegramBridge(ch)

        settings = MagicMock()
        settings.single_tenant = False

        db = MagicMock()
        db.query.return_value.all.return_value = []

        with (
            patch("zerg.services.telegram_bridge.get_settings", return_value=settings),
            patch("zerg.services.telegram_bridge.db_session") as mock_db_ctx,
        ):
            mock_db_ctx.return_value.__enter__ = MagicMock(return_value=db)
            mock_db_ctx.return_value.__exit__ = MagicMock(return_value=False)

            result = await bridge._resolve_user("unknown")

        assert result is None


# ---------------------------------------------------------------------------
# Account linking tests
# ---------------------------------------------------------------------------


class TestLinkAccount:
    @pytest.mark.asyncio
    async def test_valid_token_links_account(self):
        ch = _make_channel()
        bridge = TelegramBridge(ch)

        user = MagicMock()
        user.id = 3
        user.context = {"telegram_link_token": "mytoken123"}

        db = MagicMock()
        db.query.return_value.all.return_value = [user]

        with patch("zerg.services.telegram_bridge.db_session") as mock_db_ctx:
            mock_db_ctx.return_value.__enter__ = MagicMock(return_value=db)
            mock_db_ctx.return_value.__exit__ = MagicMock(return_value=False)

            result = await bridge._link_account("888999", "mytoken123")

        assert result is True
        assert user.context["telegram_chat_id"] == "888999"
        assert "telegram_link_token" not in user.context
        db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_token_returns_false(self):
        ch = _make_channel()
        bridge = TelegramBridge(ch)

        db = MagicMock()
        db.query.return_value.all.return_value = []

        with patch("zerg.services.telegram_bridge.db_session") as mock_db_ctx:
            mock_db_ctx.return_value.__enter__ = MagicMock(return_value=db)
            mock_db_ctx.return_value.__exit__ = MagicMock(return_value=False)

            result = await bridge._link_account("888999", "wrongtoken")

        assert result is False

    @pytest.mark.asyncio
    async def test_link_command_success_replies(self):
        ch = _make_channel()
        bridge = TelegramBridge(ch)

        with patch.object(bridge, "_link_account", new=AsyncMock(return_value=True)):
            await bridge._handle_link_command("123", "/link abc123")

        sent = ch.send_message.call_args[0][0]
        assert "linked" in sent["text"].lower()

    @pytest.mark.asyncio
    async def test_link_command_no_token_shows_usage(self):
        ch = _make_channel()
        bridge = TelegramBridge(ch)

        await bridge._handle_link_command("123", "/link")

        sent = ch.send_message.call_args[0][0]
        assert "Usage" in sent["text"]
