from __future__ import annotations

import asyncio
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from zerg.crud import create_thread_message
from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models import ThreadMessage
from zerg.models import User
from zerg.models.enums import UserRole
from zerg.routers.oikos_auth import get_current_oikos_user
from zerg.services.oikos_service import OikosService


def _make_db(tmp_path):
    db_path = tmp_path / "test_oikos_surface_history.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _make_client(db_session, current_user):
    from zerg.main import api_app
    from zerg.main import app

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    def override_current_user():
        return current_user

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[get_current_oikos_user] = override_current_user
    return TestClient(app, backend="asyncio"), api_app


def _surface_metadata(surface_id: str, conversation_id: str) -> dict:
    return {
        "surface": {
            "origin_surface_id": surface_id,
            "origin_conversation_id": conversation_id,
            "delivery_surface_id": surface_id,
            "delivery_conversation_id": conversation_id,
            "visibility": "surface-local",
        }
    }


def test_oikos_history_filters_by_surface_and_defaults_to_web(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        user = User(email="surface@test.local", role=UserRole.USER.value)
        db.add(user)
        db.commit()
        db.refresh(user)

        oikos_service = OikosService(db)
        fiche = oikos_service.get_or_create_oikos_fiche(user.id)
        thread = oikos_service.get_or_create_oikos_thread(user.id, fiche)

        base_ts = datetime(2026, 3, 4, 12, 0, 0, tzinfo=timezone.utc)
        # Legacy row with no metadata should be treated as web.
        create_thread_message(
            db=db,
            thread_id=thread.id,
            role="user",
            content="legacy web user",
            sent_at=base_ts,
            processed=True,
        )
        create_thread_message(
            db=db,
            thread_id=thread.id,
            role="assistant",
            content="web assistant",
            sent_at=base_ts + timedelta(seconds=1),
            processed=True,
            message_metadata=_surface_metadata("web", "web:main"),
        )
        create_thread_message(
            db=db,
            thread_id=thread.id,
            role="user",
            content="telegram user",
            sent_at=base_ts + timedelta(seconds=2),
            processed=True,
            message_metadata=_surface_metadata("telegram", "telegram:6311583060"),
        )
        create_thread_message(
            db=db,
            thread_id=thread.id,
            role="assistant",
            content="telegram assistant",
            sent_at=base_ts + timedelta(seconds=3),
            processed=True,
            message_metadata=_surface_metadata("telegram", "telegram:6311583060"),
        )

        client, api_app_ref = _make_client(db, user)
        try:
            # Default view should only include web-surface history.
            default_resp = client.get("/api/oikos/history?limit=50")
            assert default_resp.status_code == 200
            default_payload = default_resp.json()
            assert default_payload["total"] == 2
            assert [m["content"] for m in default_payload["messages"]] == [
                "legacy web user",
                "web assistant",
            ]

            telegram_resp = client.get("/api/oikos/history?surface_id=telegram&limit=50")
            assert telegram_resp.status_code == 200
            telegram_payload = telegram_resp.json()
            assert telegram_payload["total"] == 2
            assert [m["content"] for m in telegram_payload["messages"]] == [
                "telegram user",
                "telegram assistant",
            ]
            assert telegram_payload["messages"][0]["origin_surface_id"] == "telegram"

            all_resp = client.get("/api/oikos/history?view=all&limit=50")
            assert all_resp.status_code == 200
            all_payload = all_resp.json()
            assert all_payload["total"] == 4

            bad_view = client.get("/api/oikos/history?view=nope")
            assert bad_view.status_code == 400
            assert "view must be" in bad_view.json()["detail"]
        finally:
            api_app_ref.dependency_overrides = {}


@pytest.mark.asyncio
async def test_run_oikos_persists_surface_metadata_on_user_and_assistant(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        user = User(email="metadata@test.local", role=UserRole.USER.value)
        db.add(user)
        db.commit()
        db.refresh(user)

        async def _noop_async(*_args, **_kwargs):
            return None

        class FakeRunner:
            def __init__(self, *_args, **_kwargs):
                self.usage_prompt_tokens = None
                self.usage_completion_tokens = None
                self.usage_total_tokens = None
                self.usage_reasoning_tokens = None

            async def run_thread(self, inner_db, thread):
                assistant = create_thread_message(
                    db=inner_db,
                    thread_id=thread.id,
                    role="assistant",
                    content="done",
                    processed=True,
                )
                return [assistant]

        monkeypatch.setattr("zerg.services.oikos_service.Runner", FakeRunner)
        monkeypatch.setattr("zerg.services.event_store.emit_run_event", _noop_async)
        monkeypatch.setattr("zerg.services.oikos_service.emit_oikos_complete_success", _noop_async)
        monkeypatch.setattr("zerg.services.oikos_service.emit_stream_control_for_pending_commiss", _noop_async)
        monkeypatch.setattr("zerg.services.oikos_service.emit_success_run_updated", _noop_async)
        monkeypatch.setattr("zerg.services.ops_discord.send_run_completion_notification", _noop_async)
        monkeypatch.setattr("zerg.services.memory_summarizer.schedule_run_summary", lambda **_kwargs: None)

        service = OikosService(db)
        result = await service.run_oikos(
            owner_id=user.id,
            task="test surface metadata",
            timeout=10,
            source_surface_id="telegram",
            source_conversation_id="telegram:6311583060",
            source_message_id="msg-99",
            source_event_id="upd-444",
            source_idempotency_key="telegram:6311583060:444",
        )

        assert result.status == "success"

        rows = (
            db.query(ThreadMessage)
            .filter(
                ThreadMessage.thread_id == result.thread_id,
                ThreadMessage.role.in_(["user", "assistant"]),
            )
            .order_by(ThreadMessage.id.asc())
            .all()
        )
        assert len(rows) == 2

        user_row = rows[0]
        assistant_row = rows[1]

        assert user_row.role == "user"
        assert assistant_row.role == "assistant"

        user_surface = (user_row.message_metadata or {}).get("surface") or {}
        assistant_surface = (assistant_row.message_metadata or {}).get("surface") or {}

        expected = {
            "origin_surface_id": "telegram",
            "origin_conversation_id": "telegram:6311583060",
            "delivery_surface_id": "telegram",
            "delivery_conversation_id": "telegram:6311583060",
            "visibility": "surface-local",
            "source_message_id": "msg-99",
            "source_event_id": "upd-444",
            "idempotency_key": "telegram:6311583060:444",
        }
        assert user_surface == expected
        assert assistant_surface == expected


@pytest.mark.asyncio
async def test_run_oikos_serializes_concurrent_runs_per_owner(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        user = User(email="lock@test.local", role=UserRole.USER.value)
        db.add(user)
        db.commit()
        db.refresh(user)

        async def _noop_async(*_args, **_kwargs):
            return None

        class FakeRunner:
            active_count = 0
            max_active = 0

            def __init__(self, *_args, **_kwargs):
                self.usage_prompt_tokens = None
                self.usage_completion_tokens = None
                self.usage_total_tokens = None
                self.usage_reasoning_tokens = None

            async def run_thread(self, inner_db, thread):
                type(self).active_count += 1
                type(self).max_active = max(type(self).max_active, type(self).active_count)
                try:
                    await asyncio.sleep(0.01)
                    assistant = create_thread_message(
                        db=inner_db,
                        thread_id=thread.id,
                        role="assistant",
                        content="done",
                        processed=True,
                    )
                    return [assistant]
                finally:
                    type(self).active_count -= 1

        monkeypatch.setattr("zerg.services.oikos_service.Runner", FakeRunner)
        monkeypatch.setattr("zerg.services.event_store.emit_run_event", _noop_async)
        monkeypatch.setattr("zerg.services.oikos_service.emit_oikos_complete_success", _noop_async)
        monkeypatch.setattr("zerg.services.oikos_service.emit_stream_control_for_pending_commiss", _noop_async)
        monkeypatch.setattr("zerg.services.oikos_service.emit_success_run_updated", _noop_async)
        monkeypatch.setattr("zerg.services.ops_discord.send_run_completion_notification", _noop_async)
        monkeypatch.setattr("zerg.services.memory_summarizer.schedule_run_summary", lambda **_kwargs: None)

        service = OikosService(db)

        result_a, result_b = await asyncio.gather(
            service.run_oikos(
                owner_id=user.id,
                task="task a",
                message_id=str(uuid4()),
                source_surface_id="web",
                source_conversation_id="web:main",
            ),
            service.run_oikos(
                owner_id=user.id,
                task="task b",
                message_id=str(uuid4()),
                source_surface_id="telegram",
                source_conversation_id="telegram:6311583060",
            ),
        )

        assert result_a.status == "success"
        assert result_b.status == "success"
        assert FakeRunner.max_active == 1
