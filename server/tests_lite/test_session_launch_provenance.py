"""Launch provenance guardrails for human-owned versus automation-owned starts."""

from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy.orm import sessionmaker

from zerg.cli._managed_launch import build_managed_local_launch_payload
from zerg.cli._managed_launch import interactive_human_shell_launch_provenance
from zerg.database import Base
from zerg.database import make_engine
from zerg.models.agents import AgentSession
from zerg.models.agents import TimelineCard
from zerg.services.agents import AgentsStore
from zerg.services.agents import EventIngest
from zerg.services.agents import SessionIngest
from zerg.services.managed_local_launcher import ManagedLocalLaunchParams
from zerg.services.managed_local_launcher import build_managed_local_launch_plan
from zerg.services.managed_local_launcher import materialize_managed_local_launch_plan_sync
from zerg.services.session_hot_cards import upsert_timeline_card_from_session
from zerg.session_loop_mode import SessionLoopMode


def _make_db(tmp_path):
    db_path = tmp_path / "launch_provenance.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _event(text: str = "hello"):
    return EventIngest(
        role="user",
        content_text=text,
        timestamp=datetime(2026, 7, 9, tzinfo=timezone.utc),
        source_path="/tmp/session.jsonl",
        source_offset=0,
    )


def test_ingest_persists_launch_provenance_and_timeline_card(tmp_path):
    SessionLocal = _make_db(tmp_path)
    session_id = uuid4()

    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="codex",
                environment="development",
                started_at=datetime(2026, 7, 9, tzinfo=timezone.utc),
                launch_actor="human-shell",
                launch_surface="terminal",
                events=[_event()],
            )
        )
        session = db.get(AgentSession, session_id)
        assert session is not None
        upsert_timeline_card_from_session(db, session)
        db.commit()

    with SessionLocal() as db:
        session = db.get(AgentSession, session_id)
        card = db.get(TimelineCard, session_id)
        assert session.launch_actor == "human_shell"
        assert session.launch_surface == "terminal"
        assert card.launch_actor == "human_shell"
        assert card.launch_surface == "terminal"


def test_hidden_origin_drops_inherited_human_launch_provenance(tmp_path):
    SessionLocal = _make_db(tmp_path)
    session_id = uuid4()

    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="opencode",
                environment="development",
                started_at=datetime(2026, 7, 9, tzinfo=timezone.utc),
                origin_kind="hatch-automation",
                launch_actor="human_shell",
                launch_surface="terminal",
                events=[_event("review this patch")],
            )
        )
        db.commit()

    with SessionLocal() as db:
        session = db.get(AgentSession, session_id)
        assert session.origin_kind == "hatch_automation"
        assert session.hidden_from_default_timeline == 1
        assert session.launch_actor is None
        assert session.launch_surface is None


def test_sidechain_drops_inherited_human_launch_provenance(tmp_path):
    SessionLocal = _make_db(tmp_path)
    session_id = uuid4()

    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="claude",
                environment="development",
                started_at=datetime(2026, 7, 9, tzinfo=timezone.utc),
                is_sidechain=True,
                launch_actor="human_shell",
                launch_surface="terminal",
                events=[_event("child task")],
            )
        )
        db.commit()

    with SessionLocal() as db:
        session = db.get(AgentSession, session_id)
        assert session.launch_actor is None
        assert session.launch_surface is None


def test_late_hidden_origin_clears_prior_human_launch_provenance(tmp_path):
    SessionLocal = _make_db(tmp_path)
    session_id = uuid4()

    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="opencode",
                environment="development",
                started_at=datetime(2026, 7, 9, tzinfo=timezone.utc),
                launch_actor="human_shell",
                launch_surface="terminal",
                events=[_event("first ingest before sidecar")],
            )
        )
        store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="opencode",
                environment="development",
                started_at=datetime(2026, 7, 9, tzinfo=timezone.utc),
                origin_kind="hatch_automation",
                launch_actor="human_shell",
                launch_surface="terminal",
                events=[_event("second ingest after sidecar")],
            )
        )
        db.commit()

    with SessionLocal() as db:
        session = db.get(AgentSession, session_id)
        assert session.origin_kind == "hatch_automation"
        assert session.hidden_from_default_timeline == 1
        assert session.launch_actor is None
        assert session.launch_surface is None


def test_ingest_launch_provenance_is_fill_only(tmp_path):
    SessionLocal = _make_db(tmp_path)
    session_id = uuid4()

    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="codex",
                environment="development",
                started_at=datetime(2026, 7, 9, tzinfo=timezone.utc),
                launch_actor="human_shell",
                launch_surface="terminal",
                events=[_event("first")],
            )
        )
        store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="codex",
                environment="development",
                started_at=datetime(2026, 7, 9, tzinfo=timezone.utc),
                launch_actor="automation",
                launch_surface="test",
                events=[_event("second")],
            )
        )
        db.commit()

    with SessionLocal() as db:
        session = db.get(AgentSession, session_id)
        assert session.launch_actor == "human_shell"
        assert session.launch_surface == "terminal"


def test_ingest_only_backfills_surface_for_matching_actor(tmp_path):
    SessionLocal = _make_db(tmp_path)
    session_id = uuid4()

    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="codex",
                environment="development",
                started_at=datetime(2026, 7, 9, tzinfo=timezone.utc),
                launch_actor="human_shell",
                events=[_event("first")],
            )
        )
        store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="codex",
                environment="development",
                started_at=datetime(2026, 7, 9, tzinfo=timezone.utc),
                launch_actor="automation",
                launch_surface="test",
                events=[_event("conflicting")],
            )
        )
        db.flush()
        session = db.get(AgentSession, session_id)
        assert session.launch_actor == "human_shell"
        assert session.launch_surface is None

        store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="codex",
                environment="development",
                started_at=datetime(2026, 7, 9, tzinfo=timezone.utc),
                launch_actor="human_shell",
                launch_surface="terminal",
                events=[_event("matching")],
            )
        )
        db.commit()

    with SessionLocal() as db:
        session = db.get(AgentSession, session_id)
        assert session.launch_actor == "human_shell"
        assert session.launch_surface == "terminal"


def test_managed_local_materialization_persists_human_shell_launch(tmp_path):
    SessionLocal = _make_db(tmp_path)
    runner = SimpleNamespace(id=12, name="laptop", status="online", capabilities=["exec.full"])
    params = ManagedLocalLaunchParams(
        owner_id=77,
        runner_target="laptop",
        cwd="/Users/me/repo",
        provider="codex",
        machine_name="laptop",
        launch_actor="human_shell",
        launch_surface="terminal",
    )

    with SessionLocal() as db:
        plan = build_managed_local_launch_plan(params, runner=runner)
        session = materialize_managed_local_launch_plan_sync(db, plan)
        db.commit()
        db.refresh(session)

    with SessionLocal() as db:
        session = db.get(AgentSession, plan.session_id)
        assert session.launch_actor == "human_shell"
        assert session.launch_surface == "terminal"


def test_cli_human_shell_stamp_requires_interactive_tty_and_no_automation_env(tmp_path):
    env = {"LONGHOUSE_ORIGIN_KIND": "", "LONGHOUSE_IS_SIDECHAIN": ""}
    assert interactive_human_shell_launch_provenance(env=env, stdin_is_tty=True, stdout_is_tty=True) == (
        "human_shell",
        "terminal",
    )
    assert interactive_human_shell_launch_provenance(
        env={"LONGHOUSE_ORIGIN_KIND": "hatch_automation"},
        stdin_is_tty=True,
        stdout_is_tty=True,
    ) == (None, None)
    assert interactive_human_shell_launch_provenance(
        env={"LONGHOUSE_IS_SIDECHAIN": "1"},
        stdin_is_tty=True,
        stdout_is_tty=True,
    ) == (None, None)
    assert interactive_human_shell_launch_provenance(env=env, stdin_is_tty=False, stdout_is_tty=True) == (
        None,
        None,
    )

    payload = build_managed_local_launch_payload(
        cwd=tmp_path,
        provider="codex",
        project=None,
        name=None,
        loop_mode=SessionLoopMode.ASSIST,
        machine_name="laptop",
        launch_actor="human_shell",
        launch_surface="terminal",
    )
    assert payload["launch_actor"] == "human_shell"
    assert payload["launch_surface"] == "terminal"
