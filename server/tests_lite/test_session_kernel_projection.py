from __future__ import annotations

from datetime import datetime
from datetime import timezone

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentSession
from zerg.models.models import Runner
from zerg.models.user import User
from zerg.services.agents.session_graph_writes import ensure_primary_thread
from zerg.services.agents.session_graph_writes import record_session_edge
from zerg.services.session_kernel_projection import project_session_control_fields
from zerg.services.session_kernel_projection import project_session_lineage_fields
from tests_lite._kernel_test_helpers import seed_managed_kernel_rows


def _make_db(tmp_path):
    db_path = tmp_path / "session_kernel_projection.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_session(db, *, provider: str = "codex", device_id: str = "cinder") -> AgentSession:
    session = AgentSession(
        provider=provider,
        environment="development",
        project="longhouse",
        device_id=device_id,
        cwd="/tmp/longhouse",
        started_at=datetime.now(timezone.utc),
    )
    db.add(session)
    db.flush()
    return session


def test_codex_exec_direct_control_plane_does_not_project_runner(tmp_path):
    Session = _make_db(tmp_path)

    with Session() as db:
        session = _seed_session(db, device_id="cinder")
        db.add(User(id=1, email="projection-owner@test.local"))
        db.add(
            Runner(
                owner_id=1,
                name="cinder",
                auth_secret_hash="test-hash",
            )
        )
        seed_managed_kernel_rows(
            db,
            session,
            control_plane="codex_exec",
            can_send_input=False,
            can_interrupt=False,
            can_terminate=False,
            can_tail_output=False,
            can_resume=False,
        )
        db.commit()

        control = project_session_control_fields(db, session)

        assert control.source_runner_id is None
        assert control.source_runner_name == "cinder"
        assert control.managed_session_name is None


def test_root_lineage_projection_has_no_synthetic_continuation_kind(tmp_path):
    Session = _make_db(tmp_path)

    with Session() as db:
        session = _seed_session(db)
        ensure_primary_thread(db, session)
        db.commit()

        lineage = project_session_lineage_fields(db, session)

        assert lineage.thread_root_session_id == str(session.id)
        assert lineage.continued_from_session_id is None
        assert lineage.continuation_kind is None
        assert lineage.origin_label == "cinder"


def test_lineage_projection_reads_kernel_edge_and_branch_kind(tmp_path):
    Session = _make_db(tmp_path)

    with Session() as db:
        source = _seed_session(db, device_id="cinder")
        target = _seed_session(db, device_id="cinder-fork")
        source_thread = ensure_primary_thread(db, source)
        target_thread = ensure_primary_thread(db, target)
        target_thread.parent_thread_id = source_thread.id
        target_thread.branch_kind = "fork"
        record_session_edge(
            db,
            provider="codex",
            edge_kind="fork",
            visibility="timeline",
            evidence_kind="test",
            source_thread=source_thread,
            target_thread=target_thread,
        )
        db.commit()

        lineage = project_session_lineage_fields(db, target)

        assert lineage.thread_root_session_id == str(target.id)
        assert lineage.continued_from_session_id == str(source.id)
        assert lineage.continuation_kind == "fork"
        assert lineage.branched_from_event_id is None
