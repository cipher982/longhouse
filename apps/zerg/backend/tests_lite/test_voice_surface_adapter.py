from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from zerg.surfaces.adapters.voice import VoiceSurfaceAdapter


@pytest.mark.asyncio
async def test_voice_adapter_normalize_inbound_builds_event():
    adapter = VoiceSurfaceAdapter(owner_id=7)

    event = await adapter.normalize_inbound(
        {
            "owner_id": 7,
            "transcript": "hello voice",
            "message_id": "msg-1",
            "conversation_id": "voice:default",
        }
    )

    assert event is not None
    assert event.surface_id == "voice"
    assert event.conversation_id == "voice:default"
    assert event.dedupe_key == "voice:7:msg-1"
    assert event.source_message_id == "msg-1"
    assert event.text == "hello voice"


@pytest.mark.asyncio
async def test_voice_adapter_normalize_requires_message_id():
    adapter = VoiceSurfaceAdapter(owner_id=7)

    with pytest.raises(ValueError, match="missing message_id"):
        await adapter.normalize_inbound(
            {
                "owner_id": 7,
                "transcript": "hello voice",
                "conversation_id": "voice:default",
            }
        )


@pytest.mark.asyncio
async def test_voice_adapter_resolve_owner_id_rejects_owner_mismatch():
    adapter = VoiceSurfaceAdapter(owner_id=7)
    event = await adapter.normalize_inbound(
        {
            "owner_id": 8,
            "transcript": "hello voice",
            "message_id": "msg-1",
            "conversation_id": "voice:default",
        }
    )
    assert event is not None

    with pytest.raises(ValueError, match="owner mismatch"):
        await adapter.resolve_owner_id(event, MagicMock())


@pytest.mark.asyncio
async def test_voice_adapter_build_run_kwargs_defaults_timeout_and_return_mode():
    adapter = VoiceSurfaceAdapter(owner_id=7)
    event = await adapter.normalize_inbound(
        {
            "owner_id": 7,
            "transcript": "hello voice",
            "message_id": "msg-1",
            "conversation_id": "voice:default",
        }
    )
    assert event is not None

    kwargs = adapter.build_run_kwargs(event)

    assert kwargs["message_id"] == "msg-1"
    assert kwargs["timeout"] == 60
    assert kwargs["return_on_deferred"] is True


@pytest.mark.asyncio
async def test_voice_adapter_build_run_kwargs_includes_model_override():
    adapter = VoiceSurfaceAdapter(owner_id=7)
    event = await adapter.normalize_inbound(
        {
            "owner_id": 7,
            "transcript": "hello voice",
            "message_id": "msg-1",
            "conversation_id": "voice:default",
            "model_override": "gpt-5.3-codex",
            "reasoning_effort": "medium",
            "timeout": 15,
        }
    )
    assert event is not None

    kwargs = adapter.build_run_kwargs(event)

    assert kwargs["model_override"] == "gpt-5.3-codex"
    assert kwargs["reasoning_effort"] == "medium"
    assert kwargs["timeout"] == 15
