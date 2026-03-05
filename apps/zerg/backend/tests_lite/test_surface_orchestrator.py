from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from typing import Any

import pytest

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.surface_ingress import SurfaceIngressClaim
from zerg.models.user import User
from zerg.surfaces.base import SurfaceHandleStatus
from zerg.surfaces.base import SurfaceInboundEvent
from zerg.surfaces.idempotency import SurfaceIdempotencyError
from zerg.surfaces.idempotency import SurfaceIngressClaimStore
from zerg.surfaces.orchestrator import SurfaceOrchestrator


def _make_db(tmp_path):
    db_path = tmp_path / "test_surface_orchestrator.db"
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
        return SimpleNamespace(run_id=9001, result="adapter response")


class FailingOikosService:
    def __init__(self, _db):
        self._db = _db

    async def run_oikos(self, **_kwargs):
        raise RuntimeError("run blew up")


@dataclass
class FakeAdapter:
    event: SurfaceInboundEvent | None
    owner_id: int | None = 1
    mode: str = "push"
    run_kwargs: dict[str, Any] | None = None
    normalize_error: Exception | None = None
    resolve_error: Exception | None = None
    run_kwargs_error: Exception | None = None
    deliver_error: Exception | None = None

    def __post_init__(self) -> None:
        self.surface_id = "telegram"
        self.deliveries: list[dict[str, Any]] = []
        self.unresolved_events: list[SurfaceInboundEvent] = []

    async def normalize_inbound(self, _raw_input: Any) -> SurfaceInboundEvent | None:
        if self.normalize_error is not None:
            raise self.normalize_error
        return self.event

    async def resolve_owner_id(self, _event: SurfaceInboundEvent, _db) -> int | None:
        if self.resolve_error is not None:
            raise self.resolve_error
        return self.owner_id

    def build_run_kwargs(self, _event: SurfaceInboundEvent) -> dict[str, Any]:
        if self.run_kwargs_error is not None:
            raise self.run_kwargs_error
        return self.run_kwargs or {}

    async def deliver(self, *, owner_id: int, text: str, event: SurfaceInboundEvent) -> None:
        if self.deliver_error is not None:
            raise self.deliver_error
        self.deliveries.append({"owner_id": owner_id, "text": text, "event": event})

    async def handle_unresolved_owner(self, event: SurfaceInboundEvent) -> None:
        self.unresolved_events.append(event)


def _event(*, dedupe_key: str = "telegram:42:1001", text: str = "hello") -> SurfaceInboundEvent:
    return SurfaceInboundEvent(
        surface_id="telegram",
        conversation_id="telegram:42",
        dedupe_key=dedupe_key,
        owner_hint="42",
        source_message_id="10",
        source_event_id="1001",
        text=text,
        timestamp_utc=datetime(2026, 3, 5, 12, 0, tzinfo=timezone.utc),
        raw={"update_id": 1001},
    )


@pytest.mark.asyncio
async def test_orchestrator_processes_claims_and_delivers_push(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user = User(email="orch@test.local", role="USER")
        db.add(user)
        db.commit()

    FakeOikosService.calls.clear()
    adapter = FakeAdapter(event=_event(), owner_id=1)
    orchestrator = SurfaceOrchestrator(
        session_factory=lambda: _session_factory(SessionLocal),
        oikos_service_cls=FakeOikosService,
    )

    result = await orchestrator.handle_inbound(adapter, raw_input={})

    assert result.status == SurfaceHandleStatus.PROCESSED
    assert result.owner_id == 1
    assert result.run_id == 9001

    assert len(FakeOikosService.calls) == 1
    call = FakeOikosService.calls[0]
    assert call["owner_id"] == 1
    assert call["task"] == "hello"
    assert call["source_surface_id"] == "telegram"
    assert call["source_conversation_id"] == "telegram:42"
    assert call["source_message_id"] == "10"
    assert call["source_event_id"] == "1001"
    assert call["source_idempotency_key"] == "telegram:42:1001"

    assert len(adapter.deliveries) == 1
    assert adapter.deliveries[0]["text"] == "adapter response"

    with SessionLocal() as db:
        rows = db.query(SurfaceIngressClaim).all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_orchestrator_duplicate_claim_returns_duplicate_and_skips_run(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user = User(email="orch-dupe@test.local", role="USER")
        db.add(user)
        db.commit()

    FakeOikosService.calls.clear()
    adapter = FakeAdapter(event=_event(dedupe_key="telegram:42:dupe"), owner_id=1)
    orchestrator = SurfaceOrchestrator(
        session_factory=lambda: _session_factory(SessionLocal),
        oikos_service_cls=FakeOikosService,
    )

    first = await orchestrator.handle_inbound(adapter, raw_input={})
    second = await orchestrator.handle_inbound(adapter, raw_input={})

    assert first.status == SurfaceHandleStatus.PROCESSED
    assert second.status == SurfaceHandleStatus.DUPLICATE
    assert len(FakeOikosService.calls) == 1


@pytest.mark.asyncio
async def test_orchestrator_rejects_missing_dedupe_key(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user = User(email="orch-bad@test.local", role="USER")
        db.add(user)
        db.commit()

    FakeOikosService.calls.clear()
    adapter = FakeAdapter(event=_event(dedupe_key=""), owner_id=1)
    orchestrator = SurfaceOrchestrator(
        session_factory=lambda: _session_factory(SessionLocal),
        oikos_service_cls=FakeOikosService,
    )

    result = await orchestrator.handle_inbound(adapter, raw_input={})

    assert result.status == SurfaceHandleStatus.REJECTED
    assert len(FakeOikosService.calls) == 0


@pytest.mark.asyncio
async def test_orchestrator_handles_unresolved_owner(tmp_path):
    SessionLocal = _make_db(tmp_path)

    FakeOikosService.calls.clear()
    adapter = FakeAdapter(event=_event(), owner_id=None)
    orchestrator = SurfaceOrchestrator(
        session_factory=lambda: _session_factory(SessionLocal),
        oikos_service_cls=FakeOikosService,
    )

    result = await orchestrator.handle_inbound(adapter, raw_input={})

    assert result.status == SurfaceHandleStatus.UNRESOLVED_OWNER
    assert len(FakeOikosService.calls) == 0
    assert len(adapter.unresolved_events) == 1


@pytest.mark.asyncio
async def test_orchestrator_rejects_surface_mismatch(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user = User(email="orch-mismatch@test.local", role="USER")
        db.add(user)
        db.commit()

    mismatched = SurfaceInboundEvent(
        surface_id="web",
        conversation_id="web:main",
        dedupe_key="web:1:msg",
        owner_hint="1",
        source_message_id="msg",
        source_event_id="evt",
        text="hello",
        timestamp_utc=datetime(2026, 3, 5, 12, 0, tzinfo=timezone.utc),
        raw={},
    )
    adapter = FakeAdapter(event=mismatched, owner_id=1)
    orchestrator = SurfaceOrchestrator(
        session_factory=lambda: _session_factory(SessionLocal),
        oikos_service_cls=FakeOikosService,
    )

    result = await orchestrator.handle_inbound(adapter, raw_input={})

    assert result.status == SurfaceHandleStatus.REJECTED


@pytest.mark.asyncio
async def test_orchestrator_fails_closed_on_claim_store_error(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user = User(email="orch-claim-error@test.local", role="USER")
        db.add(user)
        db.commit()

    def _boom(*_args, **_kwargs):
        raise SurfaceIdempotencyError("boom")

    monkeypatch.setattr(SurfaceIngressClaimStore, "claim", _boom)

    adapter = FakeAdapter(event=_event(), owner_id=1)
    orchestrator = SurfaceOrchestrator(
        session_factory=lambda: _session_factory(SessionLocal),
        oikos_service_cls=FakeOikosService,
    )

    result = await orchestrator.handle_inbound(adapter, raw_input={})

    assert result.status == SurfaceHandleStatus.REJECTED


@pytest.mark.asyncio
async def test_orchestrator_rejects_invalid_run_kwargs(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user = User(email="orch-run-kwargs@test.local", role="USER")
        db.add(user)
        db.commit()

    adapter = FakeAdapter(event=_event(), owner_id=1, run_kwargs={"not_allowed": True})
    orchestrator = SurfaceOrchestrator(
        session_factory=lambda: _session_factory(SessionLocal),
        oikos_service_cls=FakeOikosService,
    )

    result = await orchestrator.handle_inbound(adapter, raw_input={})

    assert result.status == SurfaceHandleStatus.REJECTED
    assert "invalid run kwargs" in (result.message or "")


@pytest.mark.asyncio
async def test_orchestrator_rejects_when_run_oikos_throws(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user = User(email="orch-run-fail@test.local", role="USER")
        db.add(user)
        db.commit()

    adapter = FakeAdapter(event=_event(), owner_id=1)
    orchestrator = SurfaceOrchestrator(
        session_factory=lambda: _session_factory(SessionLocal),
        oikos_service_cls=FailingOikosService,
    )

    result = await orchestrator.handle_inbound(adapter, raw_input={})

    assert result.status == SurfaceHandleStatus.REJECTED
    assert "run_oikos failed" in (result.message or "")


@pytest.mark.asyncio
async def test_orchestrator_rejects_when_normalize_throws(tmp_path):
    SessionLocal = _make_db(tmp_path)
    adapter = FakeAdapter(event=None, normalize_error=RuntimeError("normalize boom"))
    orchestrator = SurfaceOrchestrator(
        session_factory=lambda: _session_factory(SessionLocal),
        oikos_service_cls=FakeOikosService,
    )

    result = await orchestrator.handle_inbound(adapter, raw_input={})

    assert result.status == SurfaceHandleStatus.REJECTED
    assert "normalize failed" in (result.message or "")


@pytest.mark.asyncio
async def test_orchestrator_rejects_when_resolve_owner_throws(tmp_path):
    SessionLocal = _make_db(tmp_path)
    adapter = FakeAdapter(event=_event(), resolve_error=RuntimeError("resolve boom"))
    orchestrator = SurfaceOrchestrator(
        session_factory=lambda: _session_factory(SessionLocal),
        oikos_service_cls=FakeOikosService,
    )

    result = await orchestrator.handle_inbound(adapter, raw_input={})

    assert result.status == SurfaceHandleStatus.REJECTED
    assert "resolve_owner failed" in (result.message or "")


@pytest.mark.asyncio
async def test_orchestrator_rejects_when_build_run_kwargs_throws(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user = User(email="orch-run-kwargs-fail@test.local", role="USER")
        db.add(user)
        db.commit()

    adapter = FakeAdapter(event=_event(), run_kwargs_error=RuntimeError("kwargs boom"))
    orchestrator = SurfaceOrchestrator(
        session_factory=lambda: _session_factory(SessionLocal),
        oikos_service_cls=FakeOikosService,
    )

    result = await orchestrator.handle_inbound(adapter, raw_input={})

    assert result.status == SurfaceHandleStatus.REJECTED
    assert "build_run_kwargs failed" in (result.message or "")


@pytest.mark.asyncio
async def test_orchestrator_returns_delivery_failed_when_deliver_throws(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user = User(email="orch-delivery-fail@test.local", role="USER")
        db.add(user)
        db.commit()

    adapter = FakeAdapter(event=_event(), deliver_error=RuntimeError("deliver boom"))
    orchestrator = SurfaceOrchestrator(
        session_factory=lambda: _session_factory(SessionLocal),
        oikos_service_cls=FakeOikosService,
    )

    result = await orchestrator.handle_inbound(adapter, raw_input={})

    assert result.status == SurfaceHandleStatus.DELIVERY_FAILED
    assert result.run_id == 9001
