from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from uuid import uuid4

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

import pytest

from zerg.crud import get_thread_messages
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentSession
from zerg.models.enums import UserRole
from zerg.models.user import User
from zerg.services.session_loop_controller import build_loop_controller_payload
from zerg.services.session_loop_controller import evaluate_session_turn_with_llm


def _make_db(tmp_path, name: str):
    engine = make_engine(f"sqlite:///{tmp_path / name}")
    initialize_database(engine)
    return make_sessionmaker(engine)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _create_user(db) -> User:
    user = User(email=f"user-{uuid4()}@example.com", role=UserRole.USER.value, context={})
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _create_session(db, *, loop_mode: str = "assist") -> AgentSession:
    session = AgentSession(
        id=uuid4(),
        provider="claude",
        environment="development",
        project="zerg",
        cwd="/tmp/zerg",
        started_at=_now(),
        ended_at=_now(),
        loop_mode=loop_mode,
        summary_title="Session Detail Page",
        summary="Working through the session detail page follow-up tasks.",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


class _FakeChatCompletions:
    def __init__(self, content: str):
        self._content = content

    async def create(self, **_kwargs):
        message = type("FakeMessage", (), {"content": self._content})()
        choice = type("FakeChoice", (), {"message": message})()
        return type("FakeResponse", (), {"choices": [choice]})()


class _FakeClient:
    def __init__(self, content: str):
        self.chat = type("FakeChat", (), {"completions": _FakeChatCompletions(content)})()
        self.closed = False

    async def close(self):
        self.closed = True


async def _run_controller(db, *, owner_id: int, session: AgentSession):
    payload = build_loop_controller_payload(
        session=session,
        turn_text="Only targeted verification remains. Run the pending targeted tests.",
        last_user_text="finish the session detail page",
        turn_index=3,
        assistant_event_id=99,
        auto_continue_streak=1,
        dialog_tail=[
            {"role": "user", "text": "finish the session detail page"},
            {"role": "assistant", "text": "Only targeted verification remains. Run the pending targeted tests."},
        ],
    )
    return await evaluate_session_turn_with_llm(
        db=db,
        owner_id=owner_id,
        session=session,
        payload=payload,
        metadata={"assistant_event_id": 99},
    )


@pytest.mark.asyncio
async def test_loop_controller_creates_per_session_thread_and_persists_messages(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path, "loop_controller_thread.db")
    fake_response = (
        '{"decision":"continue","summary":"Continue the same session.",'
        '"rationale":"One bounded next step remains.",'
        '"recommended_action":"continue_session",'
        '"follow_up_prompt":"Run the pending targeted tests.",'
        '"blocked_reasons":[]}'
    )
    fake_client = _FakeClient(fake_response)
    monkeypatch.setattr(
        "zerg.services.session_loop_controller.get_llm_client_with_db_fallback",
        lambda *_args, **_kwargs: (fake_client, "gpt-test", "openai"),
    )

    with SessionLocal() as db:
        user = _create_user(db)
        session = _create_session(db)

        first = await _run_controller(db, owner_id=user.id, session=session)
        db.refresh(session)
        assert first.decision == "continue"
        assert first.follow_up_prompt == "Run the pending targeted tests."
        assert first.loop_thread_id == session.loop_thread_id
        assert session.loop_thread_id is not None

        rows = get_thread_messages(db, session.loop_thread_id, include_internal=True)
        assert len(rows) == 2
        assert rows[0].role == "user"
        assert rows[1].role == "assistant"

        second = await _run_controller(db, owner_id=user.id, session=session)
        db.refresh(session)
        assert second.loop_thread_id == first.loop_thread_id

        rows = get_thread_messages(db, session.loop_thread_id, include_internal=True)
        assert len(rows) == 4
        assert fake_client.closed is True


@pytest.mark.asyncio
async def test_loop_controller_isolates_threads_per_session(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path, "loop_controller_isolation.db")
    fake_response = (
        '{"decision":"ask_user","summary":"Check in with the user.",'
        '"rationale":"The next step is still bounded but should be approved.",'
        '"recommended_action":"ask_user",'
        '"blocked_reasons":["Needs explicit approval."]}'
    )
    monkeypatch.setattr(
        "zerg.services.session_loop_controller.get_llm_client_with_db_fallback",
        lambda *_args, **_kwargs: (_FakeClient(fake_response), "gpt-test", "openai"),
    )

    with SessionLocal() as db:
        user = _create_user(db)
        session_a = _create_session(db, loop_mode="assist")
        session_b = _create_session(db, loop_mode="autopilot")

        await _run_controller(db, owner_id=user.id, session=session_a)
        await _run_controller(db, owner_id=user.id, session=session_b)
        db.refresh(session_a)
        db.refresh(session_b)

        assert session_a.loop_thread_id is not None
        assert session_b.loop_thread_id is not None
        assert session_a.loop_thread_id != session_b.loop_thread_id


@pytest.mark.asyncio
async def test_loop_controller_routes_thread_writes_through_serializer(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path, "loop_controller_serializer.db")
    fake_response = (
        '{"decision":"continue","summary":"Continue the same session.",'
        '"rationale":"One bounded next step remains.",'
        '"recommended_action":"continue_session",'
        '"follow_up_prompt":"Run the pending targeted tests.",'
        '"blocked_reasons":[]}'
    )
    fake_client = _FakeClient(fake_response)
    serializer_labels: list[str] = []

    class _FakeSerializer:
        async def execute_or_direct(self, fn, fallback_db, *, label="", auto_commit=True):
            serializer_labels.append(label)
            result = fn(fallback_db)
            if auto_commit:
                fallback_db.commit()
            return result

    monkeypatch.setattr(
        "zerg.services.session_loop_controller.get_llm_client_with_db_fallback",
        lambda *_args, **_kwargs: (fake_client, "gpt-test", "openai"),
    )
    monkeypatch.setattr(
        "zerg.services.session_loop_controller.get_write_serializer",
        lambda: _FakeSerializer(),
    )

    with SessionLocal() as db:
        user = _create_user(db)
        session = _create_session(db)

        decision = await _run_controller(db, owner_id=user.id, session=session)
        assert decision.decision == "continue"
        assert serializer_labels == ["loop-thread", "loop-thread-message", "loop-thread-message"]
