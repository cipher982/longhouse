from __future__ import annotations

import os
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.surface_ingress import SurfaceIngressClaim
from zerg.models.user import User
from zerg.surfaces.adapters.telegram import TelegramSurfaceAdapter
from zerg.surfaces.adapters.voice import VoiceSurfaceAdapter
from zerg.surfaces.adapters.web import WebSurfaceAdapter
from zerg.surfaces.base import SurfaceHandleStatus
from zerg.surfaces.orchestrator import SurfaceOrchestrator


def _make_db(tmp_path):
    db_path = tmp_path / "test_surface_orchestrator_multi_surface.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


@contextmanager
def _session_factory(SessionLocal):
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class FakeOikosService:
    calls: list[dict[str, Any]] = []

    def __init__(self, _db):
        self._db = _db

    async def run_oikos(self, **kwargs):
        type(self).calls.append(kwargs)
        return SimpleNamespace(run_id=4242, result="adapter response")


@pytest.mark.asyncio
async def test_orchestrator_multi_surface_end_to_end_with_telegram_delivery(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user = User(email="orch-multi-surface@test.local", role="USER")
        db.add(user)
        db.commit()
        owner_id = user.id

    FakeOikosService.calls.clear()
    web_adapter = WebSurfaceAdapter(owner_id=owner_id)
    voice_adapter = VoiceSurfaceAdapter(owner_id=owner_id)

    send_cb = AsyncMock()
    resolve_owner_cb = AsyncMock(return_value=owner_id)
    persist_chat_id_cb = AsyncMock()
    telegram_adapter = TelegramSurfaceAdapter(
        send_cb=send_cb,
        resolve_owner_cb=resolve_owner_cb,
        persist_chat_id_cb=persist_chat_id_cb,
        formatter=lambda text: f"fmt::{text}",
    )
    telegram_event = {
        "chat_id": "42",
        "chat_type": "dm",
        "message_id": "77",
        "text": "from telegram",
        "raw": {"update_id": 9001},
    }

    orchestrator = SurfaceOrchestrator(
        session_factory=lambda: _session_factory(SessionLocal),
        oikos_service_cls=FakeOikosService,
    )

    web_result = await orchestrator.handle_inbound(
        web_adapter,
        raw_input={
            "owner_id": owner_id,
            "message": "from web",
            "message_id": "web-msg-1",
            "conversation_id": "web:main",
            "run_id": 2001,
        },
    )
    voice_result = await orchestrator.handle_inbound(
        voice_adapter,
        raw_input={
            "owner_id": owner_id,
            "transcript": "from voice",
            "message_id": "voice-msg-1",
            "conversation_id": "voice:default",
        },
    )
    telegram_result = await orchestrator.handle_inbound(telegram_adapter, raw_input=telegram_event)

    assert web_result.status == SurfaceHandleStatus.PROCESSED
    assert voice_result.status == SurfaceHandleStatus.PROCESSED
    assert telegram_result.status == SurfaceHandleStatus.PROCESSED

    resolve_owner_cb.assert_awaited_once_with("42")
    persist_chat_id_cb.assert_awaited_once_with(owner_id, "42")
    send_cb.assert_awaited_once_with("42", "fmt::adapter response", None)

    assert len(FakeOikosService.calls) == 3
    calls_by_surface = {call["source_surface_id"]: call for call in FakeOikosService.calls}
    assert set(calls_by_surface) == {"web", "voice", "telegram"}
    assert calls_by_surface["web"]["source_conversation_id"] == "web:main"
    assert calls_by_surface["web"]["message_id"] == "web-msg-1"
    assert calls_by_surface["web"]["run_id"] == 2001
    assert calls_by_surface["voice"]["source_conversation_id"] == "voice:default"
    assert calls_by_surface["voice"]["message_id"] == "voice-msg-1"
    assert calls_by_surface["telegram"]["source_conversation_id"] == "telegram:42"
    assert calls_by_surface["telegram"]["source_message_id"] == "77"
    assert calls_by_surface["telegram"]["source_event_id"] == "9001"
    assert calls_by_surface["telegram"]["source_idempotency_key"] == "telegram:42:9001"

    with SessionLocal() as db:
        claims = db.query(SurfaceIngressClaim).all()
        assert len(claims) == 3
        assert {claim.surface_id for claim in claims} == {"web", "voice", "telegram"}


@pytest.mark.asyncio
async def test_orchestrator_preserves_telegram_topic_conversation_id(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user = User(email="orch-telegram-topic@test.local", role="USER")
        db.add(user)
        db.commit()
        owner_id = user.id

    FakeOikosService.calls.clear()
    send_cb = AsyncMock()
    telegram_adapter = TelegramSurfaceAdapter(
        send_cb=send_cb,
        resolve_owner_cb=AsyncMock(return_value=owner_id),
        persist_chat_id_cb=AsyncMock(),
        formatter=lambda text: text,
    )

    orchestrator = SurfaceOrchestrator(
        session_factory=lambda: _session_factory(SessionLocal),
        oikos_service_cls=FakeOikosService,
    )

    result = await orchestrator.handle_inbound(
        telegram_adapter,
        raw_input={
            "chat_id": "42",
            "chat_type": "group",
            "thread_id": "777",
            "message_id": "77",
            "text": "from telegram topic",
            "raw": {"update_id": 9002},
        },
    )

    assert result.status == SurfaceHandleStatus.PROCESSED
    assert FakeOikosService.calls[-1]["source_conversation_id"] == "telegram:42:topic:777"
    send_cb.assert_awaited_once_with("42", "adapter response", "777")


@pytest.mark.asyncio
async def test_orchestrator_idempotency_is_surface_scoped(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user = User(email="orch-surface-scoped@test.local", role="USER")
        db.add(user)
        db.commit()
        owner_id = user.id

    FakeOikosService.calls.clear()
    web_adapter = WebSurfaceAdapter(owner_id=owner_id)
    voice_adapter = VoiceSurfaceAdapter(owner_id=owner_id)

    orchestrator = SurfaceOrchestrator(
        session_factory=lambda: _session_factory(SessionLocal),
        oikos_service_cls=FakeOikosService,
    )

    first = await orchestrator.handle_inbound(
        web_adapter,
        raw_input={
            "owner_id": owner_id,
            "message": "web event",
            "message_id": "same-key",
            "conversation_id": "web:main",
            "run_id": 2010,
        },
    )
    second = await orchestrator.handle_inbound(
        voice_adapter,
        raw_input={
            "owner_id": owner_id,
            "transcript": "voice event",
            "message_id": "same-key",
            "conversation_id": "voice:default",
        },
    )
    third = await orchestrator.handle_inbound(
        web_adapter,
        raw_input={
            "owner_id": owner_id,
            "message": "web event",
            "message_id": "same-key",
            "conversation_id": "web:main",
            "run_id": 2010,
        },
    )

    assert first.status == SurfaceHandleStatus.PROCESSED
    assert second.status == SurfaceHandleStatus.PROCESSED
    assert third.status == SurfaceHandleStatus.DUPLICATE

    # The same dedupe key is valid across different surfaces for the same owner.
    assert len(FakeOikosService.calls) == 2
    with SessionLocal() as db:
        claims = db.query(SurfaceIngressClaim).all()
        assert len(claims) == 2
        assert {claim.surface_id for claim in claims} == {"web", "voice"}
