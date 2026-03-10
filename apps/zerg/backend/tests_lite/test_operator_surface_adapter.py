from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from zerg.surfaces.adapters.operator import OperatorSurfaceAdapter


@pytest.mark.asyncio
async def test_operator_adapter_normalize_inbound_builds_event():
    adapter = OperatorSurfaceAdapter(owner_id=7)

    event = await adapter.normalize_inbound(
        {
            "owner_id": 7,
            "message": "operator wakeup",
            "message_id": "msg-1",
            "conversation_id": "operator:main",
            "run_id": 100,
        }
    )

    assert event is not None
    assert event.surface_id == "operator"
    assert event.conversation_id == "operator:main"
    assert event.dedupe_key == "operator:7:msg-1"
    assert event.source_message_id == "msg-1"
    assert event.text == "operator wakeup"


@pytest.mark.asyncio
async def test_operator_adapter_normalize_requires_message_id():
    adapter = OperatorSurfaceAdapter(owner_id=7)

    with pytest.raises(ValueError, match="missing message_id"):
        await adapter.normalize_inbound(
            {
                "owner_id": 7,
                "message": "operator wakeup",
                "conversation_id": "operator:main",
                "run_id": 100,
            }
        )


@pytest.mark.asyncio
async def test_operator_adapter_resolve_owner_id_rejects_owner_mismatch():
    adapter = OperatorSurfaceAdapter(owner_id=7)
    event = await adapter.normalize_inbound(
        {
            "owner_id": 8,
            "message": "operator wakeup",
            "message_id": "msg-1",
            "conversation_id": "operator:main",
            "run_id": 100,
        }
    )
    assert event is not None

    with pytest.raises(ValueError, match="owner mismatch"):
        await adapter.resolve_owner_id(event, MagicMock())


@pytest.mark.asyncio
async def test_operator_adapter_build_run_kwargs_requires_run_id():
    adapter = OperatorSurfaceAdapter(owner_id=7)
    event = await adapter.normalize_inbound(
        {
            "owner_id": 7,
            "message": "operator wakeup",
            "message_id": "msg-1",
            "conversation_id": "operator:main",
        }
    )
    assert event is not None

    with pytest.raises(ValueError, match="missing run_id"):
        adapter.build_run_kwargs(event)


@pytest.mark.asyncio
async def test_operator_adapter_build_run_kwargs_includes_optional_fields():
    adapter = OperatorSurfaceAdapter(owner_id=7)
    event = await adapter.normalize_inbound(
        {
            "owner_id": 7,
            "message": "operator wakeup",
            "message_id": "msg-1",
            "conversation_id": "operator:main",
            "run_id": 100,
            "trace_id": "trace-1",
            "timeout": 30,
            "model_override": "gpt-5.3-codex",
            "reasoning_effort": "high",
            "return_on_deferred": False,
        }
    )
    assert event is not None

    kwargs = adapter.build_run_kwargs(event)

    assert kwargs["run_id"] == 100
    assert kwargs["message_id"] == "msg-1"
    assert kwargs["trace_id"] == "trace-1"
    assert kwargs["timeout"] == 30
    assert kwargs["model_override"] == "gpt-5.3-codex"
    assert kwargs["reasoning_effort"] == "high"
    assert kwargs["return_on_deferred"] is False
