"""Integration tests for post-run Oikos wakeup outcome classification."""

from __future__ import annotations

import pytest

from zerg.crud import crud
from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.managers.fiche_runner import FicheInterrupted
from zerg.models.agents import AgentsBase
from zerg.models.enums import UserRole
from zerg.models.models import CommisJob
from zerg.models.user import User
from zerg.models.work import OikosWakeup
from zerg.services.oikos_service import OikosService
from zerg.services.oikos_wakeup_ledger import WAKEUP_STATUS_ENQUEUED
from zerg.services.oikos_wakeup_ledger import WAKEUP_STATUS_FAILED


def _make_db(tmp_path, name: str) -> object:
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    AgentsBase.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _create_user(db, email: str) -> User:
    user = User(email=email, role=UserRole.USER.value)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _patch_oikos_side_effects(monkeypatch) -> None:
    async def _noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr("zerg.services.event_store.emit_run_event", _noop_async)
    monkeypatch.setattr("zerg.services.oikos_service.emit_oikos_complete_success", _noop_async)
    monkeypatch.setattr("zerg.services.oikos_service.emit_stream_control_for_pending_commiss", _noop_async)
    monkeypatch.setattr("zerg.services.oikos_service.emit_success_run_updated", _noop_async)
    monkeypatch.setattr("zerg.services.oikos_service.emit_oikos_waiting_and_run_updated", _noop_async)
    monkeypatch.setattr("zerg.services.oikos_service.emit_error_event_and_close_stream", _noop_async)
    monkeypatch.setattr("zerg.services.oikos_service.emit_failed_run_updated", _noop_async)
    monkeypatch.setattr("zerg.services.oikos_service.emit_cancelled_run_updated", _noop_async)
    monkeypatch.setattr("zerg.services.oikos_service.emit_stream_control", _noop_async)
    monkeypatch.setattr("zerg.services.ops_discord.send_run_completion_notification", _noop_async)
    monkeypatch.setattr("zerg.services.memory_summarizer.schedule_run_summary", lambda **_kwargs: None)


def _append_enqueued_wakeup(inner_db, *, owner_id: int, run_id: int) -> None:
    inner_db.add(
        OikosWakeup(
            owner_id=owner_id,
            source="presence",
            trigger_type="presence.blocked",
            status=WAKEUP_STATUS_ENQUEUED,
            session_id="session-123",
            conversation_id="operator:main",
            wakeup_key=f"presence:session-123:{run_id}",
            run_id=run_id,
            payload={"trigger_type": "presence.blocked", "session_id": "session-123"},
        )
    )
    inner_db.commit()


@pytest.mark.asyncio
async def test_run_oikos_marks_enqueued_wakeup_ignored_when_no_follow_up(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path, "wakeup_ignored.db")

    with SessionLocal() as db:
        user = _create_user(db, "wakeup-ignored@example.com")
        _patch_oikos_side_effects(monkeypatch)

        class FakeRunner:
            def __init__(self, *_args, **_kwargs):
                self.usage_prompt_tokens = None
                self.usage_completion_tokens = None
                self.usage_total_tokens = None
                self.usage_reasoning_tokens = None

            async def run_thread(self, inner_db, thread):
                from zerg.services.oikos_context import get_oikos_context

                ctx = get_oikos_context()
                assert ctx is not None
                _append_enqueued_wakeup(inner_db, owner_id=user.id, run_id=ctx.run_id)
                assistant = crud.create_thread_message(
                    db=inner_db,
                    thread_id=thread.id,
                    role="assistant",
                    content="No follow-up action needed.",
                    processed=True,
                )
                return [assistant]

        monkeypatch.setattr("zerg.services.oikos_service.Runner", FakeRunner)

        service = OikosService(db)
        result = await service.run_oikos(
            owner_id=user.id,
            task="Operator wakeup",
            timeout=10,
            source_surface_id="operator",
            source_conversation_id="operator:main",
        )

        assert result.status == "success"
        wakeup = db.query(OikosWakeup).one()
        assert wakeup.status == "ignored"
        assert wakeup.reason == "no_action"
        assert wakeup.payload["outcome"] == "ignore"


@pytest.mark.asyncio
async def test_run_oikos_marks_waiting_wakeup_acted_when_continuing_session(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path, "wakeup_acted.db")

    with SessionLocal() as db:
        user = _create_user(db, "wakeup-acted@example.com")
        _patch_oikos_side_effects(monkeypatch)

        class FakeRunner:
            def __init__(self, *_args, **_kwargs):
                self.usage_prompt_tokens = None
                self.usage_completion_tokens = None
                self.usage_total_tokens = None
                self.usage_reasoning_tokens = None

            async def run_thread(self, inner_db, _thread):
                from zerg.services.oikos_context import get_oikos_context

                ctx = get_oikos_context()
                assert ctx is not None
                _append_enqueued_wakeup(inner_db, owner_id=user.id, run_id=ctx.run_id)
                job = CommisJob(
                    owner_id=user.id,
                    oikos_run_id=ctx.run_id,
                    task="Run the pending targeted tests",
                    status="queued",
                    config={"execution_mode": "workspace", "resume_session_id": "session-123"},
                )
                inner_db.add(job)
                inner_db.commit()
                inner_db.refresh(job)
                raise FicheInterrupted(
                    {
                        "type": "commis_pending",
                        "job_id": job.id,
                        "message": "Working on this in the background...",
                    }
                )

        monkeypatch.setattr("zerg.services.oikos_service.Runner", FakeRunner)

        service = OikosService(db)
        result = await service.run_oikos(
            owner_id=user.id,
            task="Operator wakeup",
            timeout=10,
            source_surface_id="operator",
            source_conversation_id="operator:main",
        )

        assert result.status == "waiting"
        wakeup = db.query(OikosWakeup).one()
        assert wakeup.status == "acted"
        assert wakeup.reason == "continue_session"
        assert wakeup.payload["outcome"] == "continue_session"
        assert wakeup.payload["resume_session_ids"] == ["session-123"]


@pytest.mark.asyncio
async def test_run_oikos_marks_enqueued_wakeup_failed_when_run_fails(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path, "wakeup_failed.db")

    with SessionLocal() as db:
        user = _create_user(db, "wakeup-failed@example.com")
        _patch_oikos_side_effects(monkeypatch)

        class FakeRunner:
            def __init__(self, *_args, **_kwargs):
                self.usage_prompt_tokens = None
                self.usage_completion_tokens = None
                self.usage_total_tokens = None
                self.usage_reasoning_tokens = None

            async def run_thread(self, inner_db, _thread):
                from zerg.services.oikos_context import get_oikos_context

                ctx = get_oikos_context()
                assert ctx is not None
                _append_enqueued_wakeup(inner_db, owner_id=user.id, run_id=ctx.run_id)
                raise RuntimeError("boom")

        monkeypatch.setattr("zerg.services.oikos_service.Runner", FakeRunner)

        service = OikosService(db)
        result = await service.run_oikos(
            owner_id=user.id,
            task="Operator wakeup",
            timeout=10,
            source_surface_id="operator",
            source_conversation_id="operator:main",
        )

        assert result.status == "failed"
        wakeup = db.query(OikosWakeup).one()
        assert wakeup.status == WAKEUP_STATUS_FAILED
        assert wakeup.reason == "run_failed"
        assert wakeup.payload["outcome"] == "failed"
        assert wakeup.payload["error"] == "boom"
