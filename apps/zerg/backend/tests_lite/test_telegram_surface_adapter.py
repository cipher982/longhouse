from __future__ import annotations

from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from zerg.surfaces.adapters.telegram import TelegramSurfaceAdapter


def _make_adapter(send_cb=None, resolve_cb=None, persist_cb=None, formatter=None):
    return TelegramSurfaceAdapter(
        send_cb=send_cb or AsyncMock(),
        resolve_owner_cb=resolve_cb or AsyncMock(return_value=1),
        persist_chat_id_cb=persist_cb or AsyncMock(),
        formatter=formatter or (lambda text: text),
    )


@pytest.mark.asyncio
async def test_normalize_inbound_returns_none_for_empty_text():
    adapter = _make_adapter()

    result = await adapter.normalize_inbound({"chat_id": "42", "text": "   "})

    assert result is None


@pytest.mark.asyncio
async def test_normalize_inbound_builds_required_surface_fields():
    adapter = _make_adapter()
    event = {
        "chat_id": "42",
        "chat_type": "dm",
        "message_id": "77",
        "text": "hello",
        "raw": {"update_id": 123456},
    }

    result = await adapter.normalize_inbound(event)

    assert result is not None
    assert result.surface_id == "telegram"
    assert result.conversation_id == "telegram:42"
    assert result.dedupe_key == "telegram:42:123456"
    assert result.source_message_id == "77"
    assert result.source_event_id == "123456"
    assert result.raw.get("chat_type") == "dm"


@pytest.mark.asyncio
async def test_normalize_inbound_missing_update_id_has_empty_dedupe_key():
    adapter = _make_adapter()
    event = {
        "chat_id": "42",
        "message_id": "77",
        "text": "hello",
        "raw": {},
    }

    result = await adapter.normalize_inbound(event)

    assert result is not None
    assert result.dedupe_key == ""


@pytest.mark.asyncio
async def test_resolve_owner_id_persists_dm_chat_id():
    resolve_cb = AsyncMock(return_value=7)
    persist_cb = AsyncMock()
    adapter = _make_adapter(resolve_cb=resolve_cb, persist_cb=persist_cb)

    event = await adapter.normalize_inbound(
        {
            "chat_id": "42",
            "chat_type": "dm",
            "message_id": "77",
            "text": "hello",
            "raw": {"update_id": 123456},
        }
    )
    assert event is not None

    owner_id = await adapter.resolve_owner_id(event, MagicMock())

    assert owner_id == 7
    resolve_cb.assert_awaited_once_with("42")
    persist_cb.assert_awaited_once_with(7, "42")


@pytest.mark.asyncio
async def test_handle_unresolved_owner_sends_link_prompt():
    send_cb = AsyncMock()
    adapter = _make_adapter(send_cb=send_cb)

    event = await adapter.normalize_inbound(
        {
            "chat_id": "42",
            "chat_type": "dm",
            "message_id": "77",
            "text": "hello",
            "raw": {"update_id": 123456},
        }
    )
    assert event is not None

    await adapter.handle_unresolved_owner(event)

    send_cb.assert_awaited_once()
    args = send_cb.await_args.args
    assert args[0] == "42"
    assert "/link" in args[1]


@pytest.mark.asyncio
async def test_deliver_formats_and_sends_text():
    send_cb = AsyncMock()
    formatter = MagicMock(return_value="<b>formatted</b>")
    adapter = _make_adapter(send_cb=send_cb, formatter=formatter)

    event = await adapter.normalize_inbound(
        {
            "chat_id": "42",
            "chat_type": "dm",
            "message_id": "77",
            "text": "hello",
            "raw": {"update_id": 123456},
        }
    )
    assert event is not None

    await adapter.deliver(owner_id=1, text="assistant says hi", event=event)

    formatter.assert_called_once_with("assistant says hi")
    send_cb.assert_awaited_once_with("42", "<b>formatted</b>")
