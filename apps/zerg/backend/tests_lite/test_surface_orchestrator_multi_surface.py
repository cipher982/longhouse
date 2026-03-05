from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.surface_ingress import SurfaceIngressClaim
from zerg.models.user import User
from zerg.surfaces.adapters.telegram import TelegramSurfaceAdapter
from zerg.surfaces.base import SurfaceHandleStatus
from zerg.surfaces.base import SurfaceInboundEvent
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


@dataclass
class InlineSurfaceAdapter:
    surface_id: str
    conversation_id: str
    dedupe_key: str
    text: str
    owner_id: int
    mode: str = "inline"

    def __post_init__(self) -> None:
        self.delivery_calls = 0

    async def normalize_inbound(self, _raw_input: Any) -> SurfaceInboundEvent | None:
        return SurfaceInboundEvent(
            surface_id=self.surface_id,
            conversation_id=self.conversation_id,
            dedupe_key=self.dedupe_key,
            owner_hint=str(self.owner_id),
            source_message_id=None,
            source_event_id=None,
            text=self.text,
            timestamp_utc=datetime(2026, 3, 5, 12, 0, tzinfo=timezone.utc),
            raw={},
        )

    async def resolve_owner_id(self, _event: SurfaceInboundEvent, _db) -> int | None:
        return self.owner_id

    def build_run_kwargs(self, _event: SurfaceInboundEvent) -> dict[str, Any]:
        return {"timeout": 30, "return_on_deferred": False}

    async def deliver(self, *, owner_id: int, text: str, event: SurfaceInboundEvent) -> None:
        del owner_id
        del text
        del event
        self.delivery_calls += 1


@pytest.mark.asyncio
async def test_orchestrator_multi_surface_end_to_end_with_telegram_delivery(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user = User(email="orch-multi-surface@test.local", role="USER")
        db.add(user)
        db.commit()
        owner_id = user.id

    FakeOikosService.calls.clear()
    web_adapter = InlineSurfaceAdapter(
        surface_id="web",
        conversation_id="web:main",
        dedupe_key="web:main:1",
        text="from web",
        owner_id=owner_id,
    )
    voice_adapter = InlineSurfaceAdapter(
        surface_id="voice",
        conversation_id="voice:default",
        dedupe_key="voice:default:1",
        text="from voice",
        owner_id=owner_id,
    )

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

    web_result = await orchestrator.handle_inbound(web_adapter, raw_input={})
    voice_result = await orchestrator.handle_inbound(voice_adapter, raw_input={})
    telegram_result = await orchestrator.handle_inbound(telegram_adapter, raw_input=telegram_event)

    assert web_result.status == SurfaceHandleStatus.PROCESSED
    assert voice_result.status == SurfaceHandleStatus.PROCESSED
    assert telegram_result.status == SurfaceHandleStatus.PROCESSED

    # Inline surfaces do not receive push delivery.
    assert web_adapter.delivery_calls == 0
    assert voice_adapter.delivery_calls == 0

    resolve_owner_cb.assert_awaited_once_with("42")
    persist_chat_id_cb.assert_awaited_once_with(owner_id, "42")
    send_cb.assert_awaited_once_with("42", "fmt::adapter response")

    assert len(FakeOikosService.calls) == 3
    calls_by_surface = {call["source_surface_id"]: call for call in FakeOikosService.calls}
    assert set(calls_by_surface) == {"web", "voice", "telegram"}
    assert calls_by_surface["web"]["source_conversation_id"] == "web:main"
    assert calls_by_surface["voice"]["source_conversation_id"] == "voice:default"
    assert calls_by_surface["telegram"]["source_conversation_id"] == "telegram:42"
    assert calls_by_surface["telegram"]["source_message_id"] == "77"
    assert calls_by_surface["telegram"]["source_event_id"] == "9001"
    assert calls_by_surface["telegram"]["source_idempotency_key"] == "telegram:42:9001"

    with SessionLocal() as db:
        claims = db.query(SurfaceIngressClaim).all()
        assert len(claims) == 3
        assert {claim.surface_id for claim in claims} == {"web", "voice", "telegram"}


@pytest.mark.asyncio
async def test_orchestrator_idempotency_is_surface_scoped(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user = User(email="orch-surface-scoped@test.local", role="USER")
        db.add(user)
        db.commit()
        owner_id = user.id

    FakeOikosService.calls.clear()
    web_adapter = InlineSurfaceAdapter(
        surface_id="web",
        conversation_id="web:main",
        dedupe_key="same-key",
        text="web event",
        owner_id=owner_id,
    )
    voice_adapter = InlineSurfaceAdapter(
        surface_id="voice",
        conversation_id="voice:default",
        dedupe_key="same-key",
        text="voice event",
        owner_id=owner_id,
    )

    orchestrator = SurfaceOrchestrator(
        session_factory=lambda: _session_factory(SessionLocal),
        oikos_service_cls=FakeOikosService,
    )

    first = await orchestrator.handle_inbound(web_adapter, raw_input={})
    second = await orchestrator.handle_inbound(voice_adapter, raw_input={})
    third = await orchestrator.handle_inbound(web_adapter, raw_input={})

    assert first.status == SurfaceHandleStatus.PROCESSED
    assert second.status == SurfaceHandleStatus.PROCESSED
    assert third.status == SurfaceHandleStatus.DUPLICATE

    # The same dedupe key is valid across different surfaces for the same owner.
    assert len(FakeOikosService.calls) == 2
    with SessionLocal() as db:
        claims = db.query(SurfaceIngressClaim).all()
        assert len(claims) == 2
        assert {claim.surface_id for claim in claims} == {"web", "voice"}
