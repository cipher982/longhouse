"""Regression tests for thread context window selection.

Ensures Oikos/FicheRunner context loading uses the latest N thread messages
in chronological order, not the oldest N.
"""

from zerg.crud import crud
from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.models import User
from zerg.services.thread_service import ThreadService


def _make_db(tmp_path):
    db_path = tmp_path / "thread_context_window.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    SessionLocal = make_sessionmaker(engine)
    return SessionLocal


def _seed_thread(db):
    user = User(email="thread-window@local", role="ADMIN")
    db.add(user)
    db.commit()
    db.refresh(user)

    fiche = crud.create_fiche(
        db,
        owner_id=user.id,
        name="Thread Window Test",
        system_instructions="system",
        task_instructions="task",
        model="gpt-5.3-codex",
    )

    return ThreadService.create_thread_with_system_message(
        db,
        fiche=fiche,
        title="Window Test Thread",
        thread_type="chat",
    )


def _seed_assistant_messages(db, thread_id: int, count: int):
    for i in range(1, count + 1):
        crud.create_thread_message(
            db,
            thread_id=thread_id,
            role="assistant",
            content=f"a{i}",
            processed=True,
            commit=False,
        )
    db.commit()


def test_thread_service_uses_latest_100_messages(tmp_path):
    """History window should be latest 100 rows, ordered oldest->newest."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        thread = _seed_thread(db)
        _seed_assistant_messages(db, thread.id, count=150)

        messages = ThreadService.get_thread_messages_as_langchain(db, thread.id)
        contents = [m.content for m in messages]

        assert len(contents) == 100
        assert contents[0] == "a51"
        assert contents[-1] == "a150"


def test_thread_service_respects_history_limit_override(tmp_path):
    """Custom history_limit should still return the newest rows in order."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        thread = _seed_thread(db)
        _seed_assistant_messages(db, thread.id, count=20)

        messages = ThreadService.get_thread_messages_as_langchain(
            db,
            thread.id,
            history_limit=5,
        )
        contents = [m.content for m in messages]

        assert contents == ["a16", "a17", "a18", "a19", "a20"]
