from __future__ import annotations

import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from uuid import UUID
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models import CommisJob
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionPresence
from zerg.models.agents import SessionRuntimeState
from zerg.models.agents import SessionTurnReview
from zerg.models.enums import UserRole
from zerg.models.models import Runner
from zerg.models.user import User
from zerg.models.work import OikosWakeup
from zerg.services.oikos_operator_policy import OikosOperatorPolicy
from zerg.services.session_loop_controller import LoopControllerDecision
from zerg.services.session_turn_reviews import classify_turn_review_outcome_for_run
from zerg.services.session_turn_reviews import maybe_process_session_turn_loop
from zerg.services.session_turn_reviews import maybe_record_session_turn_review
from zerg.services.session_turn_reviews import reply_to_pending_turn_review


def _make_db(tmp_path, name: str):
    engine = make_engine(f"sqlite:///{tmp_path / name}")
    initialize_database(engine)
    return make_sessionmaker(engine)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _create_user(db, *, allow_continue: bool = False, telegram_chat_id: str | None = None) -> User:
    context = {
        "preferences": {
            "operator_mode": {
                "enabled": True,
                "allow_continue": allow_continue,
                "allow_notify": True,
            }
        }
    }
    if telegram_chat_id:
        context["telegram_chat_id"] = telegram_chat_id
    user = User(
        email=f"user-{uuid4()}@example.com",
        role=UserRole.USER.value,
        context=context,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_session(
    db,
    *,
    loop_mode: str,
    user_text: str,
    assistant_text: str,
    provider: str = "claude",
):
    session_id = uuid4()
    started_at = _now()
    session = AgentSession(
        id=session_id,
        provider=provider,
        environment="development",
        project="zerg",
        cwd="/tmp/zerg",
        started_at=started_at,
        ended_at=started_at,
        loop_mode=loop_mode,
    )
    db.add(session)
    db.flush()
    db.add(
        AgentEvent(
            session_id=session_id,
            role="user",
            content_text=user_text,
            timestamp=started_at,
        )
    )
    db.add(
        AgentEvent(
            session_id=session_id,
            role="assistant",
            content_text=assistant_text,
            timestamp=started_at,
        )
    )
    db.commit()
    return session_id


def _create_runner(db, *, owner_id: int, name: str = "cinder") -> Runner:
    runner = Runner(
        owner_id=owner_id,
        name=name,
        availability_policy="always_on",
        capabilities=["exec.full"],
        status="online",
        auth_secret_hash="secret-hash",
        runner_metadata={"install_mode": "desktop"},
    )
    db.add(runner)
    db.commit()
    db.refresh(runner)
    return runner


@pytest.mark.asyncio
async def test_turn_review_autopilot_enqueues_same_session_continue_job(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path, "turn_review_autopilot_enqueue.db")

    async def _fake_evaluate(**_kwargs):
        return LoopControllerDecision(
            decision="continue",
            summary="The next step is a bounded same-session continue.",
            rationale="The assistant left exactly one obvious follow-up.",
            recommended_action="continue_session",
            follow_up_prompt="Run the pending targeted tests.",
            blocked_reasons=(),
            model_id="gpt-test",
            raw_response='{"decision":"continue"}',
            loop_thread_id=11,
        )

    monkeypatch.setattr("zerg.services.session_turn_reviews.evaluate_session_turn_with_llm", _fake_evaluate)

    with SessionLocal() as db:
        _create_user(db, allow_continue=True)
        session_id = _seed_session(
            db,
            loop_mode="autopilot",
            user_text="finish the session detail page",
            assistant_text="Only targeted verification remains. Run the pending targeted tests.",
        )

        review = await maybe_process_session_turn_loop(db=db, session_id=str(session_id))
        assert review is not None
        assert review.decision == "continue"
        assert review.execution_state == "would_auto_continue"
        assert review.recommended_action == "continue_session"
        assert review.follow_up_prompt == "Run the pending targeted tests."
        assert review.status == "acted"
        assert review.reason == "continue_session"
        assert review.actual_outcome == "continue_session"
        assert review.shadow_alignment == "matched"
        assert review.run_id is None

        jobs = db.query(CommisJob).order_by(CommisJob.id.asc()).all()
        assert len(jobs) == 1
        assert jobs[0].task == "Run the pending targeted tests."
        assert jobs[0].config["execution_mode"] == "workspace"
        assert jobs[0].config["resume_session_id"] == str(session_id)
        assert jobs[0].config["backend"] == "zai"
        assert jobs[0].config["trigger"] == "turn_loop"
        assert jobs[0].config["assistant_event_id"] == review.assistant_event_id


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "codex"])
async def test_turn_review_autopilot_routes_managed_local_continue_without_cloud_job(
    monkeypatch, tmp_path, provider
):
    SessionLocal = _make_db(tmp_path, "turn_review_autopilot_managed_local.db")
    calls: list[dict[str, object]] = []

    async def _fake_evaluate(**_kwargs):
        return LoopControllerDecision(
            decision="continue",
            summary="The next step is a bounded same-session continue.",
            rationale="The assistant left exactly one obvious follow-up.",
            recommended_action="continue_session",
            follow_up_prompt="Run the pending targeted tests.",
            blocked_reasons=(),
            model_id="gpt-test",
            raw_response='{"decision":"continue"}',
            loop_thread_id=51,
        )

    async def _fake_send_text(*, db, owner_id, session, text, commis_id=None, timeout_secs=15):
        calls.append(
            {
                "owner_id": owner_id,
                "session_id": str(session.id),
                "text": text,
                "commis_id": commis_id,
                "timeout_secs": timeout_secs,
                "transport": "tmux",
            }
        )
        return SimpleNamespace(ok=True, exit_code=0, error=None)

    async def _fake_codex_exec(*, db, owner_id, session, text, commis_id=None, timeout_secs=300):
        calls.append(
            {
                "owner_id": owner_id,
                "session_id": str(session.id),
                "text": text,
                "commis_id": commis_id,
                "timeout_secs": timeout_secs,
                "transport": "codex_exec",
            }
        )
        return SimpleNamespace(ok=True, exit_code=0, error=None)

    monkeypatch.setattr("zerg.services.session_turn_reviews.evaluate_session_turn_with_llm", _fake_evaluate)
    monkeypatch.setattr("zerg.services.session_turn_reviews.send_text_to_managed_local_session", _fake_send_text)
    monkeypatch.setattr(
        "zerg.services.session_turn_reviews.run_codex_exec_resume_for_managed_local_session",
        _fake_codex_exec,
    )
    monkeypatch.setattr(
        "zerg.services.session_turn_reviews._load_policy",
        lambda _db, _owner_id: OikosOperatorPolicy(
            enabled=True,
            allow_continue=True,
            allow_notify=True,
        ),
    )

    with SessionLocal() as db:
        user = _create_user(db, allow_continue=True)
        runner = _create_runner(db, owner_id=user.id, name="cinder")
        session_id = _seed_session(
            db,
            loop_mode="autopilot",
            user_text="finish the session detail page",
            assistant_text="Only targeted verification remains. Run the pending targeted tests.",
            provider=provider,
        )
        session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        session.execution_home = "managed_local"
        session.managed_transport = "tmux"
        session.source_runner_id = runner.id
        session.source_runner_name = runner.name
        session.managed_session_name = "lh-autopilot-managed-local"
        db.commit()
        db.refresh(session)

        review = await maybe_process_session_turn_loop(db=db, session_id=str(session_id))
        assert review is not None
        assert review.execution_state == "would_auto_continue"
        assert review.status == "acted"
        assert review.reason == "continue_session"
        assert review.actual_outcome == "continue_session"

        jobs = db.query(CommisJob).all()
        assert jobs == []

        assert len(calls) == 1
        assert calls[0]["owner_id"] == user.id
        assert calls[0]["session_id"] == str(session_id)
        assert calls[0]["text"] == "Run the pending targeted tests."
        assert calls[0]["commis_id"] == f"turn-review-{review.id}"
        assert calls[0]["transport"] == ("codex_exec" if provider == "codex" else "tmux")
        assert calls[0]["timeout_secs"] == (300 if provider == "codex" else 15)


@pytest.mark.asyncio
async def test_turn_review_autopilot_codex_managed_local_does_not_fallback_to_tmux_when_exec_fails(
    monkeypatch, tmp_path
):
    SessionLocal = _make_db(tmp_path, "turn_review_autopilot_codex_exec_failure.db")

    async def _fake_evaluate(**_kwargs):
        return LoopControllerDecision(
            decision="continue",
            summary="The next step is a bounded same-session continue.",
            rationale="The assistant left exactly one obvious follow-up.",
            recommended_action="continue_session",
            follow_up_prompt="Run the pending targeted tests.",
            blocked_reasons=(),
            model_id="gpt-test",
            raw_response='{"decision":"continue"}',
            loop_thread_id=52,
        )

    async def _fail_tmux(**_kwargs):
        raise AssertionError("managed-local Codex continue must not fall back to tmux")

    async def _fake_codex_exec(**_kwargs):
        return SimpleNamespace(
            ok=False,
            exit_code=17,
            error="managed-local Codex session mapping missing",
        )

    monkeypatch.setattr("zerg.services.session_turn_reviews.evaluate_session_turn_with_llm", _fake_evaluate)
    monkeypatch.setattr("zerg.services.session_turn_reviews.send_text_to_managed_local_session", _fail_tmux)
    monkeypatch.setattr(
        "zerg.services.session_turn_reviews.run_codex_exec_resume_for_managed_local_session",
        _fake_codex_exec,
    )
    monkeypatch.setattr(
        "zerg.services.session_turn_reviews._load_policy",
        lambda _db, _owner_id: OikosOperatorPolicy(
            enabled=True,
            allow_continue=True,
            allow_notify=True,
        ),
    )

    with SessionLocal() as db:
        user = _create_user(db, allow_continue=True)
        runner = _create_runner(db, owner_id=user.id, name="cinder")
        session_id = _seed_session(
            db,
            loop_mode="autopilot",
            user_text="finish the session detail page",
            assistant_text="Only targeted verification remains. Run the pending targeted tests.",
            provider="codex",
        )
        session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        session.execution_home = "managed_local"
        session.managed_transport = "tmux"
        session.source_runner_id = runner.id
        session.source_runner_name = runner.name
        session.managed_session_name = "lh-autopilot-managed-local-codex"
        db.commit()
        db.refresh(session)

        review = await maybe_process_session_turn_loop(db=db, session_id=str(session_id))
        assert review is not None
        assert review.execution_state == "would_auto_continue"
        assert review.status == "failed"
        assert review.reason == "managed_local_send_failed"
        assert review.actual_outcome == "failed"

        jobs = db.query(CommisJob).all()
        assert jobs == []


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "codex"])
async def test_reply_to_pending_turn_review_routes_managed_local_reply_without_cloud_job(
    monkeypatch, tmp_path, provider
):
    SessionLocal = _make_db(tmp_path, "turn_review_reply_managed_local.db")
    calls: list[dict[str, object]] = []

    async def _fake_send_text(*, db, owner_id, session, text, commis_id=None, timeout_secs=15):
        calls.append(
            {
                "owner_id": owner_id,
                "session_id": str(session.id),
                "text": text,
                "commis_id": commis_id,
                "timeout_secs": timeout_secs,
                "transport": "tmux",
            }
        )
        return SimpleNamespace(ok=True, exit_code=0, error=None)

    async def _fail_codex_exec(**_kwargs):
        raise AssertionError("reply path must stay on tmux")

    monkeypatch.setattr("zerg.services.session_turn_reviews.send_text_to_managed_local_session", _fake_send_text)
    monkeypatch.setattr(
        "zerg.services.session_turn_reviews.run_codex_exec_resume_for_managed_local_session",
        _fail_codex_exec,
    )

    with SessionLocal() as db:
        user = _create_user(db, allow_continue=False)
        runner = _create_runner(db, owner_id=user.id, name="cinder")
        session_id = _seed_session(
            db,
            loop_mode="assist",
            user_text="keep the hiring task moving",
            assistant_text="I finished the last turn and need your direction on what to do next.",
            provider=provider,
        )
        session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        session.execution_home = "managed_local"
        session.managed_transport = "tmux"
        session.source_runner_id = runner.id
        session.source_runner_name = runner.name
        session.managed_session_name = "lh-managed-local-reply"
        db.commit()
        db.refresh(session)

        review = SessionTurnReview(
            session_id=session_id,
            owner_id=user.id,
            assistant_event_id=2,
            turn_index=1,
            trigger_type="turn.completed",
            loop_mode="assist",
            decision="wait",
            summary="Awaiting your direction on the next hiring step.",
            rationale="The finished turn needs a human reply rather than autonomous continuation.",
            turn_excerpt="I finished the last turn and need your direction on what to do next.",
            mode_capability="notify_only",
            mode_summary="Suggest or escalate from completed turns, but wait for user approval before continuing.",
            execution_state="needs_human",
            recommended_action="wait",
            follow_up_prompt=None,
            blocked_reasons=[],
            status="enqueued",
            reason="notify_user",
        )
        db.add(review)
        db.commit()
        db.refresh(review)

        await reply_to_pending_turn_review(
            db=db,
            review=review,
            reply_text="keep going with the hiring shortlist",
        )

        jobs = db.query(CommisJob).all()
        assert jobs == []

        db.refresh(review)
        assert review.status == "acted"
        assert review.reason == "reply_to_session"
        assert review.actual_outcome == "delegated_follow_up"

        assert len(calls) == 1
        assert calls[0]["owner_id"] == user.id
        assert calls[0]["session_id"] == str(session_id)
        assert calls[0]["text"] == "keep going with the hiring shortlist"
        assert calls[0]["commis_id"] == f"turn-review-reply-{review.id}"
        assert calls[0]["transport"] == "tmux"
        assert calls[0]["timeout_secs"] == 15


@pytest.mark.asyncio
async def test_turn_review_assist_enqueues_operator_wakeup(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path, "turn_review_assist_operator.db")
    calls: list[dict[str, object]] = []

    async def _fake_evaluate(**_kwargs):
        return LoopControllerDecision(
            decision="continue",
            summary="The same session has one obvious bounded next step.",
            rationale="This is the routine continue case after a completed assistant turn.",
            recommended_action="continue_session",
            follow_up_prompt="Run the pending targeted tests.",
            blocked_reasons=(),
            model_id="gpt-test",
            raw_response='{"decision":"continue"}',
            loop_thread_id=21,
        )

    async def _fake_invoke(owner_id, message, message_id, **kwargs):
        calls.append(
            {
                "owner_id": owner_id,
                "message": message,
                "message_id": message_id,
                **kwargs,
            }
        )
        return 321

    monkeypatch.setattr("zerg.services.session_turn_reviews.evaluate_session_turn_with_llm", _fake_evaluate)
    monkeypatch.setattr("zerg.services.oikos_service.invoke_oikos", _fake_invoke)

    with SessionLocal() as db:
        user = _create_user(db, allow_continue=False)
        session_id = _seed_session(
            db,
            loop_mode="assist",
            user_text="finish the session detail page",
            assistant_text="Only targeted verification remains. Run the pending targeted tests.",
        )

        review = await maybe_process_session_turn_loop(db=db, session_id=str(session_id))
        assert review is not None
        assert review.decision == "continue"
        assert review.execution_state == "awaiting_user_approval"
        assert review.status == "enqueued"
        assert review.reason == "notify_user"
        assert review.run_id == 321
        assert review.actual_outcome is None
        assert review.shadow_alignment is None
        assert review.follow_up_prompt == "Run the pending targeted tests."

        jobs = db.query(CommisJob).all()
        wakeups = db.query(OikosWakeup).order_by(OikosWakeup.id.asc()).all()

        assert jobs == []
        assert len(wakeups) == 1
        assert wakeups[0].owner_id == user.id
        assert wakeups[0].source == "turn_loop"
        assert wakeups[0].trigger_type == "turn.completed"
        assert wakeups[0].status == "enqueued"
        assert wakeups[0].run_id == 321
        assert wakeups[0].session_id == str(session_id)
        assert wakeups[0].payload["turn_review"]["decision"]["follow_up_prompt"] == "Run the pending targeted tests."
        assert wakeups[0].payload["turn_review"]["loop_review"]["execution_state"] == "awaiting_user_approval"

    assert len(calls) == 1
    assert calls[0]["owner_id"] == user.id
    assert "System/turn loop" in str(calls[0]["message"])
    assert "Suggested follow-up prompt: Run the pending targeted tests." in str(calls[0]["message"])
    assert str(UUID(str(calls[0]["message_id"]))) == str(calls[0]["message_id"])
    assert calls[0]["source"] == "operator"
    assert getattr(calls[0]["surface_adapter"], "surface_id", None) == "operator"
    assert (
        calls[0]["surface_payload"]["turn_review"]["decision"]["follow_up_prompt"] == "Run the pending targeted tests."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "codex"])
async def test_turn_review_marks_managed_local_attention_phase(monkeypatch, tmp_path, provider):
    SessionLocal = _make_db(tmp_path, "turn_review_managed_local_attention.db")

    async def _fake_evaluate(**_kwargs):
        return LoopControllerDecision(
            decision="wait",
            summary="The session needs a direct human reply.",
            rationale="The completed turn ended in a handoff request with no safe autonomous next step.",
            recommended_action="wait",
            follow_up_prompt=None,
            blocked_reasons=(),
            model_id="gpt-test",
            raw_response='{"decision":"wait"}',
            loop_thread_id=77,
        )

    async def _fake_invoke(*_args, **_kwargs):
        return 654

    monkeypatch.setattr("zerg.services.session_turn_reviews.evaluate_session_turn_with_llm", _fake_evaluate)
    monkeypatch.setattr("zerg.services.oikos_service.invoke_oikos", _fake_invoke)

    with SessionLocal() as db:
        user = _create_user(db, allow_continue=False)
        runner = _create_runner(db, owner_id=user.id, name="cinder")
        session_id = _seed_session(
            db,
            loop_mode="assist",
            user_text="what should we do next?",
            assistant_text="I finished the last turn and now need your direction on the next hiring step.",
            provider=provider,
        )
        session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        session.execution_home = "managed_local"
        session.managed_transport = "tmux"
        session.source_runner_id = runner.id
        session.source_runner_name = runner.name
        session.managed_session_name = "lh-managed-local-attention"
        db.commit()

        review = await maybe_process_session_turn_loop(db=db, session_id=str(session_id))
        assert review is not None
        assert review.execution_state == "needs_human"

        runtime_state = db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == session_id).one()
        assert runtime_state.phase == "needs_user"
        assert runtime_state.phase_source == "semantic"
        assert runtime_state.last_runtime_signal_at is not None


def test_classify_turn_review_outcome_keeps_notify_reviews_actionable_without_jobs(tmp_path):
    SessionLocal = _make_db(tmp_path, "turn_review_notify_without_jobs.db")

    with SessionLocal() as db:
        _create_user(db, allow_continue=False)
        session_id = _seed_session(
            db,
            loop_mode="assist",
            user_text="what should we do next?",
            assistant_text="I answered the question and now need your direction on the next hiring step.",
        )
        review = SessionTurnReview(
            session_id=session_id,
            owner_id=1,
            assistant_event_id=2,
            turn_index=1,
            trigger_type="turn.completed",
            loop_mode="assist",
            decision="wait",
            summary="Awaiting your direction on the next hiring step.",
            rationale="The finished turn does not have one obvious bounded follow-up.",
            turn_excerpt="I answered the question and now need your direction on the next hiring step.",
            mode_capability="notify_only",
            mode_summary="Suggest or escalate from completed turns, but wait for user approval before continuing.",
            execution_state="needs_human",
            recommended_action="wait",
            follow_up_prompt=None,
            blocked_reasons=[],
            status="enqueued",
            reason="notify_user",
            run_id=999,
        )
        db.add(review)
        db.commit()

        changed = classify_turn_review_outcome_for_run(db, run_id=999)
        assert changed == 1

        db.flush()
        db.refresh(review)
        assert review.status == "enqueued"
        assert review.reason == "notify_user"
        assert review.actual_outcome == "notify_user"
        assert review.shadow_alignment == "matched"


@pytest.mark.asyncio
async def test_turn_review_assist_sends_telegram_loop_link_once(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path, "turn_review_assist_notification.db")
    sent_messages: list[dict[str, object]] = []

    async def _fake_evaluate(**_kwargs):
        return LoopControllerDecision(
            decision="continue",
            summary="Only targeted verification remains.",
            rationale="This is the routine continue case after a completed assistant turn.",
            recommended_action="continue_session",
            follow_up_prompt="Run the pending targeted tests.",
            blocked_reasons=(),
            model_id="gpt-test",
            raw_response='{"decision":"continue"}',
            loop_thread_id=31,
        )

    async def _fake_invoke(*_args, **_kwargs):
        return 654

    class _FakeTelegramChannel:
        async def send_message(self, message):
            sent_messages.append(dict(message))
            return {"success": True}

    class _FakeRegistry:
        def get(self, channel_id):
            if channel_id == "telegram":
                return _FakeTelegramChannel()
            return None

    monkeypatch.setattr("zerg.services.session_turn_reviews.evaluate_session_turn_with_llm", _fake_evaluate)
    monkeypatch.setattr("zerg.services.oikos_service.invoke_oikos", _fake_invoke)
    monkeypatch.setattr(
        "zerg.services.session_turn_reviews._send_turn_review_push_notification",
        lambda **_kwargs: False,
    )
    monkeypatch.setattr(
        "zerg.services.session_turn_reviews.get_settings",
        lambda: SimpleNamespace(app_public_url="https://longhouse.example", public_site_url=None),
    )
    monkeypatch.setattr("zerg.channels.registry.get_registry", lambda: _FakeRegistry())

    with SessionLocal() as db:
        _create_user(db, allow_continue=False, telegram_chat_id="1234")
        session_id = _seed_session(
            db,
            loop_mode="assist",
            user_text="finish the verification",
            assistant_text="Only targeted verification remains. Run the pending targeted tests.",
        )

        first = await maybe_process_session_turn_loop(db=db, session_id=str(session_id))
        second = await maybe_process_session_turn_loop(db=db, session_id=str(session_id))

        assert first is not None
        assert second is not None
        assert first.id == second.id

    assert len(sent_messages) == 1
    assert sent_messages[0]["to"] == "1234"
    assert "Only targeted verification remains." in str(sent_messages[0]["text"])
    assert "Run the pending targeted tests." in str(sent_messages[0]["text"])
    assert "/loop/card/" in str(sent_messages[0]["text"])
    assert sent_messages[0]["disable_web_page_preview"] is True


@pytest.mark.asyncio
async def test_turn_review_assist_prefers_loop_push_over_telegram(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path, "turn_review_assist_prefers_loop_push.db")
    push_calls: list[int] = []

    async def _fake_evaluate(**_kwargs):
        return LoopControllerDecision(
            decision="continue",
            summary="Only targeted verification remains.",
            rationale="This is the routine continue case after a completed assistant turn.",
            recommended_action="continue_session",
            follow_up_prompt="Run the pending targeted tests.",
            blocked_reasons=(),
            model_id="gpt-test",
            raw_response='{"decision":"continue"}',
            loop_thread_id=41,
        )

    async def _fake_invoke(*_args, **_kwargs):
        return 777

    async def _fake_telegram(**_kwargs):
        raise AssertionError("Telegram fallback should not run when Loop push succeeds")

    def _fake_push(**kwargs):
        push_calls.append(int(kwargs["review"].id))
        return True

    monkeypatch.setattr("zerg.services.session_turn_reviews.evaluate_session_turn_with_llm", _fake_evaluate)
    monkeypatch.setattr("zerg.services.oikos_service.invoke_oikos", _fake_invoke)
    monkeypatch.setattr("zerg.services.session_turn_reviews._send_turn_review_push_notification", _fake_push)
    monkeypatch.setattr("zerg.services.session_turn_reviews._send_turn_review_telegram_notification", _fake_telegram)

    with SessionLocal() as db:
        _create_user(db, allow_continue=False, telegram_chat_id="1234")
        session_id = _seed_session(
            db,
            loop_mode="assist",
            user_text="finish the verification",
            assistant_text="Only targeted verification remains. Run the pending targeted tests.",
        )

        review = await maybe_process_session_turn_loop(db=db, session_id=str(session_id))

        assert review is not None
        assert push_calls == [int(review.id)]


@pytest.mark.asyncio
async def test_turn_review_autopilot_does_not_send_mobile_notification(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path, "turn_review_autopilot_no_mobile_notification.db")

    async def _fake_evaluate(**_kwargs):
        return LoopControllerDecision(
            decision="continue",
            summary="Only targeted verification remains.",
            rationale="This is the routine continue case after a completed assistant turn.",
            recommended_action="continue_session",
            follow_up_prompt="Run the pending targeted tests.",
            blocked_reasons=(),
            model_id="gpt-test",
            raw_response='{"decision":"continue"}',
            loop_thread_id=43,
        )

    monkeypatch.setattr("zerg.services.session_turn_reviews.evaluate_session_turn_with_llm", _fake_evaluate)
    monkeypatch.setattr(
        "zerg.services.session_turn_reviews._send_turn_review_push_notification",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("Push should not fire for acted reviews")),
    )
    monkeypatch.setattr(
        "zerg.services.session_turn_reviews._send_turn_review_telegram_notification",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("Telegram should not fire for acted reviews")),
    )

    with SessionLocal() as db:
        _create_user(db, allow_continue=True, telegram_chat_id="1234")
        session_id = _seed_session(
            db,
            loop_mode="autopilot",
            user_text="finish the verification",
            assistant_text="Only targeted verification remains. Run the pending targeted tests.",
        )

        review = await maybe_process_session_turn_loop(db=db, session_id=str(session_id))

        assert review is not None
        assert review.status == "acted"
        assert review.reason == "continue_session"


@pytest.mark.asyncio
async def test_new_turn_review_supersedes_older_actionable_review(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path, "turn_review_supersedes_older.db")

    decisions = [
        LoopControllerDecision(
            decision="continue",
            summary="Continue the same session.",
            rationale="One bounded next step remains.",
            recommended_action="continue_session",
            follow_up_prompt="Run the pending targeted tests.",
            blocked_reasons=(),
            model_id="gpt-test",
            raw_response='{"decision":"continue"}',
            loop_thread_id=55,
        ),
        LoopControllerDecision(
            decision="wait",
            summary="Waiting on a broader human decision.",
            rationale="The latest turn no longer has a safe bounded continue path.",
            recommended_action="wait",
            follow_up_prompt=None,
            blocked_reasons=("Needs direction.",),
            model_id="gpt-test",
            raw_response='{"decision":"wait"}',
            loop_thread_id=55,
        ),
    ]

    async def _fake_evaluate(**_kwargs):
        return decisions.pop(0)

    async def _fake_invoke(*_args, **_kwargs):
        return 987

    monkeypatch.setattr("zerg.services.session_turn_reviews.evaluate_session_turn_with_llm", _fake_evaluate)
    monkeypatch.setattr("zerg.services.oikos_service.invoke_oikos", _fake_invoke)

    with SessionLocal() as db:
        _create_user(db, allow_continue=False)
        session_id = _seed_session(
            db,
            loop_mode="assist",
            user_text="run the next step",
            assistant_text="Only targeted verification remains. Run the pending targeted tests.",
        )

        first = await maybe_process_session_turn_loop(db=db, session_id=str(session_id))
        assert first is not None
        assert first.status == "enqueued"

        db.add(
            AgentEvent(
                session_id=session_id,
                role="assistant",
                content_text="Actually this now needs a broader decision before continuing.",
                timestamp=_now(),
            )
        )
        session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        session.ended_at = _now()
        db.commit()

        second = await maybe_process_session_turn_loop(db=db, session_id=str(session_id))
        assert second is not None
        db.refresh(first)

        assert first.status == "ignored"
        assert first.reason == "superseded"
        assert second.id != first.id


@pytest.mark.asyncio
async def test_turn_review_keeps_manual_mode_observe_only_even_for_escalation(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path, "turn_review_manual_escalate.db")

    async def _fake_evaluate(**_kwargs):
        return LoopControllerDecision(
            decision="escalate",
            summary="A real product decision is required.",
            rationale="The assistant is asking for a human choice, not a routine continue.",
            recommended_action="escalate",
            blocked_reasons=("Meaningful product decision required.",),
            model_id="gpt-test",
            raw_response='{"decision":"escalate"}',
            loop_thread_id=12,
        )

    monkeypatch.setattr("zerg.services.session_turn_reviews.evaluate_session_turn_with_llm", _fake_evaluate)

    with SessionLocal() as db:
        _create_user(db, allow_continue=True)
        session_id = _seed_session(
            db,
            loop_mode="manual",
            user_text="should we make the risky production migration now?",
            assistant_text="This looks like a risky production migration and needs your decision before I proceed.",
        )

        review = await maybe_record_session_turn_review(db=db, session_id=str(session_id))
        assert review is not None
        assert review.decision == "escalate"
        assert review.execution_state == "observe_only"
        assert review.status == "recorded"
        assert review.run_id is None


@pytest.mark.asyncio
async def test_turn_review_dedupes_same_completed_assistant_turn(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path, "turn_review_dedupe.db")

    async def _fake_evaluate(**_kwargs):
        return LoopControllerDecision(
            decision="continue",
            summary="Continue the same session.",
            rationale="Same bounded next step remains.",
            recommended_action="continue_session",
            blocked_reasons=(),
            model_id="gpt-test",
            raw_response='{"decision":"continue"}',
            loop_thread_id=13,
        )

    monkeypatch.setattr("zerg.services.session_turn_reviews.evaluate_session_turn_with_llm", _fake_evaluate)

    with SessionLocal() as db:
        _create_user(db, allow_continue=True)
        session_id = _seed_session(
            db,
            loop_mode="assist",
            user_text="run the last test pass",
            assistant_text="Ready for phase 2. Say continue and I will run the pending targeted tests.",
        )

        first = await maybe_record_session_turn_review(db=db, session_id=str(session_id))
        second = await maybe_record_session_turn_review(db=db, session_id=str(session_id))

        assert first is not None
        assert second is not None
        assert first.id == second.id
        count = db.query(SessionTurnReview).filter(SessionTurnReview.session_id == session_id).count()
        assert count == 1


@pytest.mark.asyncio
async def test_turn_review_uses_latest_assistant_turn_timestamp_when_session_ended_at_is_stale(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path, "turn_review_stale_session_ended_at.db")

    async def _fake_evaluate(**_kwargs):
        return LoopControllerDecision(
            decision="continue",
            summary="Continue the fresh managed-local turn.",
            rationale="The latest assistant turn just completed and still has one bounded next step.",
            recommended_action="continue_session",
            blocked_reasons=(),
            model_id="gpt-test",
            raw_response='{"decision":"continue"}',
            loop_thread_id=15,
        )

    monkeypatch.setattr("zerg.services.session_turn_reviews.evaluate_session_turn_with_llm", _fake_evaluate)

    with SessionLocal() as db:
        _create_user(db, allow_continue=False)
        session_id = _seed_session(
            db,
            loop_mode="assist",
            user_text="finish the verification",
            assistant_text="The earlier turn finished a while ago.",
        )
        stale_ended_at = _now() - timedelta(minutes=20)
        fresh_turn_at = _now()
        session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        session.ended_at = stale_ended_at
        db.add(
            AgentEvent(
                session_id=session_id,
                role="user",
                content_text="continue from the same managed-local session",
                timestamp=fresh_turn_at,
            )
        )
        db.add(
            AgentEvent(
                session_id=session_id,
                role="assistant",
                content_text="Only targeted verification remains. Run the pending targeted tests.",
                timestamp=fresh_turn_at,
            )
        )
        db.commit()
        latest_assistant_event = (
            db.query(AgentEvent)
            .filter(AgentEvent.session_id == session_id, AgentEvent.role == "assistant")
            .order_by(AgentEvent.id.desc())
            .first()
        )

        review = await maybe_record_session_turn_review(db=db, session_id=str(session_id))

        assert review is not None
        assert latest_assistant_event is not None
        assert review.assistant_event_id == latest_assistant_event.id
        assert review.execution_state == "awaiting_user_approval"
        assert review.status == "recorded"


@pytest.mark.asyncio
async def test_turn_review_skips_stale_completed_turn_by_default(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path, "turn_review_stale_completed_turn.db")

    async def _fake_evaluate(**_kwargs):
        return LoopControllerDecision(
            decision="continue",
            summary="Continue the turn.",
            rationale="The next step is bounded.",
            recommended_action="continue_session",
            blocked_reasons=(),
            model_id="gpt-test",
            raw_response='{"decision":"continue"}',
            loop_thread_id=16,
        )

    monkeypatch.setattr("zerg.services.session_turn_reviews.evaluate_session_turn_with_llm", _fake_evaluate)

    with SessionLocal() as db:
        _create_user(db, allow_continue=False)
        session_id = _seed_session(
            db,
            loop_mode="assist",
            user_text="finish the verification",
            assistant_text="Only targeted verification remains. Run the pending targeted tests.",
        )
        session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        stale_turn_at = _now() - timedelta(minutes=20)
        session.ended_at = stale_turn_at
        latest_assistant = (
            db.query(AgentEvent)
            .filter(AgentEvent.session_id == session_id, AgentEvent.role == "assistant")
            .order_by(AgentEvent.id.desc())
            .first()
        )
        assert latest_assistant is not None
        latest_assistant.timestamp = stale_turn_at
        db.commit()

        review = await maybe_record_session_turn_review(db=db, session_id=str(session_id))

        assert review is None


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("presence_state", ["needs_user", "blocked"])
async def test_turn_review_still_records_when_latest_presence_is_pause_state(
    monkeypatch, tmp_path, presence_state, provider
):
    SessionLocal = _make_db(tmp_path, f"turn_review_pause_{presence_state}.db")

    async def _fake_evaluate(**_kwargs):
        return LoopControllerDecision(
            decision="continue",
            summary="Continue after this completed turn.",
            rationale="The next step is still bounded.",
            recommended_action="continue_session",
            blocked_reasons=(),
            model_id="gpt-test",
            raw_response='{"decision":"continue"}',
            loop_thread_id=14,
        )

    monkeypatch.setattr("zerg.services.session_turn_reviews.evaluate_session_turn_with_llm", _fake_evaluate)

    with SessionLocal() as db:
        _create_user(db, allow_continue=False)
        session_id = _seed_session(
            db,
            loop_mode="assist",
            user_text="finish the verification",
            assistant_text="Only targeted verification remains. Run the pending targeted tests.",
            provider=provider,
        )
        db.add(
            SessionPresence(
                session_id=str(session_id),
                state=presence_state,
                provider=provider,
                project="zerg",
                updated_at=_now(),
            )
        )
        db.commit()

        review = await maybe_record_session_turn_review(db=db, session_id=str(session_id))
        assert review is not None
        assert review.decision == "continue"
        assert review.execution_state == "awaiting_user_approval"
        assert review.status == "recorded"


@pytest.mark.asyncio
async def test_turn_review_marks_controller_failures(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path, "turn_review_controller_failure.db")

    async def _boom(**_kwargs):
        raise RuntimeError("llm exploded")

    monkeypatch.setattr("zerg.services.session_turn_reviews.evaluate_session_turn_with_llm", _boom)

    with SessionLocal() as db:
        _create_user(db, allow_continue=True)
        session_id = _seed_session(
            db,
            loop_mode="assist",
            user_text="continue if the next step is obvious",
            assistant_text="Only targeted verification remains.",
        )

        review = await maybe_record_session_turn_review(db=db, session_id=str(session_id))
        assert review is not None
        assert review.decision == "ask_user"
        assert review.status == "failed"
        assert review.reason == "controller_error"
        assert "Loop controller evaluation failed." in (review.blocked_reasons or [])
