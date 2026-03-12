from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from fastapi.testclient import TestClient

from zerg.crud import crud
from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models import User
from zerg.models.enums import UserRole
from zerg.routers.oikos_auth import get_current_oikos_user
from zerg.services.conversation_service import ConversationService
from zerg.services.oikos_service import OikosService


def _make_db(tmp_path):
    db_path = tmp_path / "test_oikos_web_conversations.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_user(db, email: str) -> User:
    user = User(email=email, role=UserRole.USER.value)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


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


def _patch_oikos_run_side_effects(monkeypatch, runner_cls):
    async def _noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr("zerg.services.oikos_service.Runner", runner_cls)
    monkeypatch.setattr("zerg.services.event_store.emit_run_event", _noop_async)
    monkeypatch.setattr("zerg.services.oikos_service.emit_oikos_complete_success", _noop_async)
    monkeypatch.setattr("zerg.services.oikos_service.emit_stream_control_for_pending_commiss", _noop_async)
    monkeypatch.setattr("zerg.services.oikos_service.emit_success_run_updated", _noop_async)
    monkeypatch.setattr("zerg.services.ops_discord.send_run_completion_notification", _noop_async)
    monkeypatch.setattr("zerg.services.memory_summarizer.schedule_run_summary", lambda **_kwargs: None)


def test_oikos_thread_exposes_canonical_web_conversation(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        user = _seed_user(db, "web-surface@test.local")

        client, api_app_ref = _make_client(db, user)
        try:
            resp = client.get("/api/oikos/thread")
            assert resp.status_code == 200
            payload = resp.json()

            assert payload["title"] == "Oikos"
            assert payload["canonical_conversation"]["kind"] == "web"
            assert payload["canonical_conversation"]["external_conversation_id"] == "web:main"
            assert payload["canonical_conversation"]["message_count"] == 0

            conversation = ConversationService.get_conversation_by_binding(
                db,
                owner_id=user.id,
                surface_id="web",
                external_conversation_id="web:main",
            )
            assert conversation is not None
            assert conversation.id == payload["canonical_conversation"]["id"]
        finally:
            api_app_ref.dependency_overrides = {}


def test_oikos_thread_backfills_legacy_web_history_into_canonical_conversation(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        user = _seed_user(db, "backfill@test.local")
        service = OikosService(db)
        fiche = service.get_or_create_oikos_fiche(user.id)
        thread = service.get_or_create_oikos_thread(user.id, fiche)

        crud.create_thread_message(
            db=db,
            thread_id=thread.id,
            role="user",
            content="legacy web user",
            processed=True,
        )
        crud.create_thread_message(
            db=db,
            thread_id=thread.id,
            role="assistant",
            content="legacy web assistant",
            processed=True,
        )
        crud.create_thread_message(
            db=db,
            thread_id=thread.id,
            role="user",
            content="telegram user should stay out",
            processed=True,
            message_metadata={
                "surface": {
                    "origin_surface_id": "telegram",
                    "origin_conversation_id": "telegram:42",
                    "delivery_surface_id": "telegram",
                    "delivery_conversation_id": "telegram:42",
                    "visibility": "surface-local",
                }
            },
        )

        client, api_app_ref = _make_client(db, user)
        try:
            resp = client.get("/api/oikos/thread")
            assert resp.status_code == 200
            payload = resp.json()
            canonical = payload["canonical_conversation"]
            assert canonical["message_count"] == 2

            conversation = ConversationService.get_conversation_by_binding(
                db,
                owner_id=user.id,
                surface_id="web",
                external_conversation_id="web:main",
            )
            assert conversation is not None

            messages = ConversationService.list_messages(
                db,
                owner_id=user.id,
                conversation_id=conversation.id,
                limit=10,
            )
            assert [message.content for message in messages] == [
                "legacy web user",
                "legacy web assistant",
            ]
            assert messages[1].message_metadata["oikos"]["mirrored_from_oikos_thread"] is True
        finally:
            api_app_ref.dependency_overrides = {}


@pytest.mark.asyncio
async def test_run_oikos_mirrors_new_web_turns_into_canonical_conversation(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path)

    class FakeRunner:
        call_count = 0

        def __init__(self, *_args, **_kwargs):
            self.usage_prompt_tokens = None
            self.usage_completion_tokens = None
            self.usage_total_tokens = None
            self.usage_reasoning_tokens = None

        async def run_thread(self, inner_db, thread):
            type(self).call_count += 1
            assistant = crud.create_thread_message(
                db=inner_db,
                thread_id=thread.id,
                role="assistant",
                content=f"done {type(self).call_count}",
                processed=True,
            )
            return [assistant]

    _patch_oikos_run_side_effects(monkeypatch, FakeRunner)

    with SessionLocal() as db:
        user = _seed_user(db, "mirror@test.local")

        service = OikosService(db)
        first = await service.run_oikos(
            owner_id=user.id,
            task="first task",
            timeout=10,
            source_surface_id="web",
            source_conversation_id="web:main",
            source_message_id="client-msg-1",
        )
        second = await service.run_oikos(
            owner_id=user.id,
            task="second task",
            timeout=10,
            source_surface_id="web",
            source_conversation_id="web:main",
            source_message_id="client-msg-2",
        )

        assert first.status == "success"
        assert second.status == "success"

        conversation = ConversationService.get_conversation_by_binding(
            db,
            owner_id=user.id,
            surface_id="web",
            external_conversation_id="web:main",
        )
        assert conversation is not None

        messages = ConversationService.list_messages(
            db,
            owner_id=user.id,
            conversation_id=conversation.id,
            limit=20,
        )

        assert [message.role for message in messages] == ["user", "assistant", "user", "assistant"]
        assert [message.content for message in messages] == ["first task", "done 1", "second task", "done 2"]
        assert messages[0].external_message_id.startswith("thread-message:")
        assert messages[1].sender_kind == "agent"
        assert messages[0].message_metadata["surface"]["source_message_id"] == "client-msg-1"
        assert messages[2].message_metadata["surface"]["source_message_id"] == "client-msg-2"
