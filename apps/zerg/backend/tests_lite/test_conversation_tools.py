from __future__ import annotations

import os
from contextlib import contextmanager

os.environ.setdefault("DATABASE_URL", "sqlite://")

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models import User
from zerg.models.enums import UserRole
from zerg.services.conversation_service import ConversationService
from zerg.services.oikos_context import reset_oikos_context
from zerg.services.oikos_context import set_oikos_context
from zerg.tools import ImmutableToolRegistry
from zerg.tools.builtin import BUILTIN_TOOLS
from zerg.tools.builtin import conversation_tools


def _make_db(tmp_path):
    db_path = tmp_path / "test_conversation_tools.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_user(db, email: str) -> User:
    user = User(email=email, role=UserRole.USER.value)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _patch_db_session(monkeypatch, SessionLocal):
    @contextmanager
    def _db_session():
        with SessionLocal() as db:
            yield db
            db.commit()

    monkeypatch.setattr(conversation_tools, "db_session", _db_session)


def test_search_conversations_returns_owner_scoped_results(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        owner = _seed_user(db, "owner@test.local")
        other = _seed_user(db, "other@test.local")

        owner_conversation = ConversationService.get_or_create_by_binding(
            db,
            owner_id=owner.id,
            kind="email",
            surface_id="email",
            provider="gmail",
            binding_scope="connector:1",
            external_conversation_id="thread-owner",
            title="Portugal planning",
        )
        ConversationService.append_message(
            db,
            owner_id=owner.id,
            conversation_id=owner_conversation.id,
            role="user",
            content="Remember the Portugal flights",
        )

        other_conversation = ConversationService.get_or_create_by_binding(
            db,
            owner_id=other.id,
            kind="email",
            surface_id="email",
            provider="gmail",
            binding_scope="connector:2",
            external_conversation_id="thread-other",
            title="Other planning",
        )
        ConversationService.append_message(
            db,
            owner_id=other.id,
            conversation_id=other_conversation.id,
            role="user",
            content="Remember the Portugal flights",
        )
        owner_id = owner.id
        owner_conversation_id = owner_conversation.id

    _patch_db_session(monkeypatch, SessionLocal)

    token = set_oikos_context(run_id=1, owner_id=owner_id, message_id="msg-1")
    try:
        result = conversation_tools.search_conversations("Portugal", limit=5)
    finally:
        reset_oikos_context(token)

    assert result["ok"] is True
    assert result["data"]["total"] == 1
    assert result["data"]["conversations"][0]["id"] == owner_conversation_id


def test_read_conversation_returns_bindings_and_messages(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        owner = _seed_user(db, "owner@test.local")
        conversation = ConversationService.get_or_create_by_binding(
            db,
            owner_id=owner.id,
            kind="email",
            surface_id="email",
            provider="gmail",
            binding_scope="connector:1",
            external_conversation_id="thread-123",
            title="Dinner plans",
        )
        ConversationService.append_message(
            db,
            owner_id=owner.id,
            conversation_id=conversation.id,
            role="user",
            content="Can you book dinner for 7?",
            external_message_id="gmail-msg-1",
        )
        owner_id = owner.id
        conversation_id = conversation.id

    _patch_db_session(monkeypatch, SessionLocal)

    token = set_oikos_context(run_id=2, owner_id=owner_id, message_id="msg-2")
    try:
        result = conversation_tools.read_conversation(conversation_id)
    finally:
        reset_oikos_context(token)

    assert result["ok"] is True
    assert result["data"]["id"] == conversation_id
    assert result["data"]["bindings"][0]["external_conversation_id"] == "thread-123"
    assert result["data"]["messages"][0]["content"] == "Can you book dinner for 7?"


def test_conversation_tools_are_registered(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        owner = _seed_user(db, "owner@test.local")
        conversation = ConversationService.get_or_create_by_binding(
            db,
            owner_id=owner.id,
            kind="email",
            surface_id="email",
            provider="gmail",
            binding_scope="connector:1",
            external_conversation_id="thread-123",
            title="Dinner plans",
        )
        ConversationService.append_message(
            db,
            owner_id=owner.id,
            conversation_id=conversation.id,
            role="user",
            content="Book dinner",
        )
        owner_id = owner.id

    _patch_db_session(monkeypatch, SessionLocal)

    registry = ImmutableToolRegistry.build([BUILTIN_TOOLS])
    search_tool = registry.get("search_conversations")
    read_tool = registry.get("read_conversation")
    assert search_tool is not None
    assert read_tool is not None

    token = set_oikos_context(run_id=3, owner_id=owner_id, message_id="msg-3")
    try:
        result = search_tool.invoke({"query": "dinner", "limit": 5})
    finally:
        reset_oikos_context(token)

    assert result["ok"] is True
    assert result["data"]["conversations"]
