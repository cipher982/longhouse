"""Synthetic end-to-end canaries for operator wakeup loop ceilings."""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from uuid import uuid4

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentsBase
from zerg.models.enums import RunStatus
from zerg.models.enums import RunTrigger
from zerg.models.enums import UserRole
from zerg.models.models import CommisJob
from zerg.models.models import Run
from zerg.models.user import User
from zerg.models.work import OikosWakeup
from zerg.services.oikos_service import OikosService
from zerg.surfaces.adapters.operator import OperatorSurfaceAdapter
from zerg.surfaces.base import SurfaceHandleStatus
from zerg.surfaces.orchestrator import SurfaceOrchestrator


def _make_db(tmp_path, name: str):
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    AgentsBase.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


@contextmanager
def _session_factory(SessionLocal):
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _patch_oikos_side_effects(monkeypatch) -> None:
    async def _noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr("zerg.services.event_store.emit_run_event", _noop_async)
    monkeypatch.setattr("zerg.services.event_store.append_run_event", _noop_async)
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


def _create_user(db, *, email: str, allow_continue: bool = True) -> User:
    user = User(
        email=email,
        role=UserRole.USER.value,
        context={
            "preferences": {
                "operator_mode": {
                    "enabled": True,
                    "allow_continue": allow_continue,
                }
            }
        },
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _shadow_review(*, mode_capability: str) -> dict:
    if mode_capability == "bounded_autonomy":
        return {
            "decision": {
                "summary": "Only targeted verification remains, so the next step is explicit.",
            },
            "loop_review": {
                "loop_mode": "autopilot",
                "mode_capability": "bounded_autonomy",
                "mode_summary": "May continue only explicit bounded follow-ups.",
                "execution_state": "would_auto_continue",
                "recommended_action": "continue_session",
                "would_continue_session": True,
                "would_notify_user": False,
            },
        }

    return {
        "decision": {
            "summary": "The session should notify the user before continuing.",
        },
        "loop_review": {
            "loop_mode": "assist",
            "mode_capability": "notify_only",
            "mode_summary": "May notify but must not continue without approval.",
            "execution_state": "awaiting_user_approval",
            "recommended_action": "notify_user",
            "would_continue_session": False,
            "would_notify_user": True,
        },
    }


def _seed_operator_run(db, *, owner_id: int, session_id: str, mode_capability: str) -> tuple[Run, dict]:
    service = OikosService(db)
    fiche = service.get_or_create_oikos_fiche(owner_id)
    thread = service.get_or_create_oikos_thread(owner_id, fiche)
    run = Run(
        fiche_id=fiche.id,
        thread_id=thread.id,
        status=RunStatus.RUNNING,
        trigger=RunTrigger.API,
        started_at=datetime.now(timezone.utc).replace(tzinfo=None),
        model="gpt-scripted",
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    shadow_review = _shadow_review(mode_capability=mode_capability)
    db.add(
        OikosWakeup(
            owner_id=owner_id,
            source="presence",
            trigger_type="presence.blocked",
            status="enqueued",
            session_id=session_id,
            conversation_id="operator:main",
            wakeup_key=f"presence:{session_id}:{run.id}",
            run_id=run.id,
            payload={
                "trigger_type": "presence.blocked",
                "session_id": session_id,
                "shadow_review": shadow_review,
            },
        )
    )
    db.commit()
    return run, shadow_review


async def _run_operator_canary(
    SessionLocal,
    *,
    owner_id: int,
    run_id: int,
    session_id: str,
    shadow_review: dict,
    message_session_id: str | None = None,
):
    adapter = OperatorSurfaceAdapter(owner_id=owner_id)
    orchestrator = SurfaceOrchestrator(session_factory=lambda: _session_factory(SessionLocal))
    prompt_session_id = message_session_id or session_id
    return await orchestrator.handle_inbound(
        adapter,
        {
            "owner_id": owner_id,
            "message": (
                f"System/operator wakeup: Continue session {prompt_session_id} "
                "by running the pending targeted tests."
            ),
            "message_id": str(uuid4()),
            "conversation_id": "operator:main",
            "run_id": run_id,
            "timeout": 20,
            "model_override": "gpt-scripted",
            "session_id": session_id,
            "shadow_review": shadow_review,
        },
    )


@pytest.mark.asyncio
async def test_operator_loop_canary_allows_bounded_same_session_resume(monkeypatch, tmp_path):
    monkeypatch.setenv("OIKOS_OPERATOR_MODE_ENABLED", "1")
    SessionLocal = _make_db(tmp_path, "operator_loop_canary_allowed.db")

    with SessionLocal() as db:
        _patch_oikos_side_effects(monkeypatch)
        user = _create_user(db, email="operator-loop-allowed@example.com")
        session_id = "11111111-1111-1111-1111-111111111111"
        run, shadow_review = _seed_operator_run(
            db,
            owner_id=user.id,
            session_id=session_id,
            mode_capability="bounded_autonomy",
        )
        owner_id = user.id
        run_id = run.id

    result = await _run_operator_canary(
        SessionLocal,
        owner_id=owner_id,
        run_id=run_id,
        session_id=session_id,
        shadow_review=shadow_review,
    )

    assert result.status == SurfaceHandleStatus.PROCESSED
    assert result.run_status == "waiting"

    with SessionLocal() as db:
        jobs = db.query(CommisJob).filter(CommisJob.oikos_run_id == run_id).all()
        assert len(jobs) == 1
        assert jobs[0].task == "Run the pending targeted tests"
        assert jobs[0].config is not None
        assert jobs[0].config.get("execution_mode") == "workspace"
        assert jobs[0].config.get("resume_session_id") == session_id

        wakeup = db.query(OikosWakeup).filter(OikosWakeup.run_id == run_id).one()
        assert wakeup.status == "acted"
        assert wakeup.reason == "continue_session"
        assert wakeup.payload["outcome"] == "continue_session"
        assert wakeup.payload["shadow_expected_outcome"] == "continue_session"
        assert wakeup.payload["shadow_alignment"] == "matched"
        assert wakeup.payload["resume_session_ids"] == [session_id]


@pytest.mark.asyncio
async def test_operator_loop_canary_blocks_notify_only_resume_attempt(monkeypatch, tmp_path):
    monkeypatch.setenv("OIKOS_OPERATOR_MODE_ENABLED", "1")
    SessionLocal = _make_db(tmp_path, "operator_loop_canary_blocked.db")

    with SessionLocal() as db:
        _patch_oikos_side_effects(monkeypatch)
        user = _create_user(db, email="operator-loop-blocked@example.com")
        session_id = "22222222-2222-2222-2222-222222222222"
        run, shadow_review = _seed_operator_run(
            db,
            owner_id=user.id,
            session_id=session_id,
            mode_capability="notify_only",
        )
        owner_id = user.id
        run_id = run.id

    result = await _run_operator_canary(
        SessionLocal,
        owner_id=owner_id,
        run_id=run_id,
        session_id=session_id,
        shadow_review=shadow_review,
    )

    assert result.status == SurfaceHandleStatus.PROCESSED
    assert result.run_status == "success"
    assert "capped below autonomous continuation" in (result.response_text or "").lower()

    with SessionLocal() as db:
        assert db.query(CommisJob).filter(CommisJob.oikos_run_id == run_id).count() == 0

        wakeup = db.query(OikosWakeup).filter(OikosWakeup.run_id == run_id).one()
        assert wakeup.status == "ignored"
        assert wakeup.reason == "no_action"
        assert wakeup.payload["outcome"] == "ignore"
        assert wakeup.payload["shadow_expected_outcome"] == "notify_user"
        assert wakeup.payload["shadow_alignment"] == "more_conservative"


@pytest.mark.asyncio
async def test_operator_loop_canary_blocks_bounded_resume_for_wrong_session(monkeypatch, tmp_path):
    monkeypatch.setenv("OIKOS_OPERATOR_MODE_ENABLED", "1")
    SessionLocal = _make_db(tmp_path, "operator_loop_canary_wrong_session.db")

    with SessionLocal() as db:
        _patch_oikos_side_effects(monkeypatch)
        user = _create_user(db, email="operator-loop-wrong-session@example.com")
        session_id = "33333333-3333-3333-3333-333333333333"
        run, shadow_review = _seed_operator_run(
            db,
            owner_id=user.id,
            session_id=session_id,
            mode_capability="bounded_autonomy",
        )
        owner_id = user.id
        run_id = run.id

    result = await _run_operator_canary(
        SessionLocal,
        owner_id=owner_id,
        run_id=run_id,
        session_id=session_id,
        shadow_review=shadow_review,
        message_session_id="44444444-4444-4444-4444-444444444444",
    )

    assert result.status == SurfaceHandleStatus.PROCESSED
    assert result.run_status == "success"
    assert "exact session named in the operator wakeup" in (result.response_text or "").lower()

    with SessionLocal() as db:
        assert db.query(CommisJob).filter(CommisJob.oikos_run_id == run_id).count() == 0

        wakeup = db.query(OikosWakeup).filter(OikosWakeup.run_id == run_id).one()
        assert wakeup.status == "ignored"
        assert wakeup.reason == "no_action"
        assert wakeup.payload["outcome"] == "ignore"
        assert wakeup.payload["shadow_expected_outcome"] == "continue_session"
        assert wakeup.payload["shadow_alignment"] == "more_conservative"
