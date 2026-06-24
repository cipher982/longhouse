from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from uuid import UUID

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("TESTING", "1")

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from tests_lite._kernel_test_helpers import seed_managed_kernel_rows
from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.main import api_app
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionThread
from zerg.services.agents import AgentsStore
from zerg.services.agents import EventIngest
from zerg.services.agents import SessionIngest
from zerg.services.agents.kernel_capabilities import project_session_capabilities
from zerg.services.agents.provider_binding_convergence import BindingCandidate
from zerg.services.agents.provider_binding_convergence import evaluate_provider_binding_convergence
from zerg.services.agents.session_graph_writes import record_thread_alias
from zerg.services.agents.session_graph_writes import resolve_thread_by_provider_session_id

NOW = datetime(2026, 6, 23, 12, 0, 0, tzinfo=timezone.utc)
PROVIDER = "opencode"
NATIVE_ID = "ses_native_convergence"

LAUNCH_SESSION_ID = UUID("aaaaaaaa-0000-4000-8000-000000000001")
TRANSCRIPT_SESSION_ID = UUID("bbbbbbbb-0000-4000-8000-000000000002")


def _session_factory(tmp_path, name="provider-binding-convergence.db"):
    engine = make_engine(f"sqlite:///{tmp_path / name}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _seed_managed_launch(db, *, session_id=LAUNCH_SESSION_ID):
    """Simulate `longhouse opencode` launching: managed kernel rows + the
    provider_session_id alias the launcher records."""
    session = AgentSession(
        id=session_id,
        provider=PROVIDER,
        environment="production",
        project="demo",
        device_id="cinder",
        cwd="/tmp/demo",
        started_at=NOW,
    )
    db.add(session)
    db.flush()
    thread, _run, _conn = seed_managed_kernel_rows(db, session, control_plane="opencode_server")
    record_thread_alias(
        db,
        thread=thread,
        provider=PROVIDER,
        alias_kind="provider_session_id",
        alias_value=NATIVE_ID,
    )
    db.commit()
    return session, thread


def _transcript_ingest(session_id=TRANSCRIPT_SESSION_ID):
    """A root transcript ship carrying the same provider-native id but a
    DIFFERENT Longhouse session id — this is the split-row trigger. The binding
    kernel must resolve by provider_session_id BEFORE creating a new session."""
    return SessionIngest(
        id=session_id,
        provider=PROVIDER,
        environment="production",
        project="demo",
        device_id="cinder",
        cwd="/tmp/demo",
        started_at=NOW,
        provider_session_id=NATIVE_ID,
        events=[
            EventIngest(
                role="user",
                content_text="hello from the transcript",
                timestamp=NOW,
                source_path="/tmp/demo/transcript.jsonl",
                source_offset=0,
            ),
            EventIngest(
                role="assistant",
                content_text="hi back",
                timestamp=NOW,
                source_path="/tmp/demo/transcript.jsonl",
                source_offset=1,
            ),
        ],
    )


def _assert_converged(db, expected_session_id):
    assert db.query(AgentSession).count() == 1, "split row: more than one session"
    assert db.query(SessionThread).count() == 1, "split row: more than one thread"

    thread = resolve_thread_by_provider_session_id(db, provider=PROVIDER, provider_session_id=NATIVE_ID)
    assert thread is not None
    assert thread.session_id == expected_session_id

    caps = project_session_capabilities(db, session_id=expected_session_id)
    assert caps.live_control_available is True, "managed capabilities lost after transcript ingest"

    # Same verdict path the live canary uses.
    verdict = evaluate_provider_binding_convergence(
        provider=PROVIDER,
        provider_session_id=NATIVE_ID,
        candidates=[
            BindingCandidate(
                session_id=str(expected_session_id),
                has_content=True,
                managed=caps.live_control_available or caps.host_reattach_available,
            )
        ],
    )
    assert verdict.ok, verdict.reason


def test_launch_first_then_transcript_converges(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    db = SessionLocal()
    try:
        _seed_managed_launch(db)
        store = AgentsStore(db)
        result = store.ingest_session(_transcript_ingest())
        db.commit()

        # Transcript carried a different Longhouse id but must bind to the launch.
        assert result.session_id == LAUNCH_SESSION_ID
        assert result.session_created is False
        _assert_converged(db, LAUNCH_SESSION_ID)
    finally:
        db.close()


def test_transcript_reship_with_changed_longhouse_id_does_not_split(tmp_path):
    # A re-ship of the same provider-native id under a DIFFERENT Longhouse id
    # (e.g. the original id was lost) must bind to the existing thread, not split.
    SessionLocal = _session_factory(tmp_path)
    db = SessionLocal()
    try:
        store = AgentsStore(db)
        result = store.ingest_session(_transcript_ingest(session_id=TRANSCRIPT_SESSION_ID))
        db.commit()
        assert result.session_id == TRANSCRIPT_SESSION_ID

        result2 = store.ingest_session(_transcript_ingest(session_id=UUID("cccccccc-0000-4000-8000-000000000003")))
        db.commit()
        assert result2.session_id == TRANSCRIPT_SESSION_ID
        assert result2.session_created is False

        assert db.query(AgentSession).count() == 1
        assert db.query(SessionThread).count() == 1
        thread = resolve_thread_by_provider_session_id(db, provider=PROVIDER, provider_session_id=NATIVE_ID)
        assert thread is not None
        assert thread.session_id == TRANSCRIPT_SESSION_ID
    finally:
        db.close()


def test_transcript_first_then_managed_launch_converges(tmp_path):
    # Transcript ships first and creates an imported session bound to the native
    # id. Then `longhouse opencode` launches: it resolves the existing thread by
    # native id and attaches managed control to THAT session instead of creating
    # a second one. Result: one session that is both content-bearing and managed.
    SessionLocal = _session_factory(tmp_path)
    db = SessionLocal()
    try:
        store = AgentsStore(db)
        result = store.ingest_session(_transcript_ingest(session_id=TRANSCRIPT_SESSION_ID))
        db.commit()
        assert result.session_id == TRANSCRIPT_SESSION_ID

        # Imported-only until control attaches.
        caps_before = project_session_capabilities(db, session_id=TRANSCRIPT_SESSION_ID)
        assert caps_before.live_control_available is False

        # Managed launch resolves the bound thread by native id (resolve-before-
        # create) and seeds control on the existing session — not a new row.
        bound = resolve_thread_by_provider_session_id(db, provider=PROVIDER, provider_session_id=NATIVE_ID)
        assert bound is not None
        launched_session = db.query(AgentSession).filter(AgentSession.id == bound.session_id).one()
        seed_managed_kernel_rows(db, launched_session, control_plane="opencode_server")
        db.commit()

        assert db.query(AgentSession).count() == 1
        assert db.query(SessionThread).count() == 1
        _assert_converged(db, TRANSCRIPT_SESSION_ID)
    finally:
        db.close()


def test_evaluator_flags_split_row():
    verdict = evaluate_provider_binding_convergence(
        provider=PROVIDER,
        provider_session_id=NATIVE_ID,
        candidates=[
            BindingCandidate(session_id="s1", has_content=True, managed=True),
            BindingCandidate(session_id="s2", has_content=False, managed=True),
        ],
    )
    assert verdict.ok is False
    assert verdict.reason == "split_row"
    assert verdict.session_ids == ["s1", "s2"]


def test_evaluator_flags_unmanaged_and_empty():
    no_content = evaluate_provider_binding_convergence(
        provider=PROVIDER,
        provider_session_id=NATIVE_ID,
        candidates=[BindingCandidate(session_id="s1", has_content=False, managed=True)],
    )
    assert no_content.reason == "no_content"

    not_managed = evaluate_provider_binding_convergence(
        provider=PROVIDER,
        provider_session_id=NATIVE_ID,
        candidates=[BindingCandidate(session_id="s1", has_content=True, managed=False)],
    )
    assert not_managed.reason == "not_managed"

    empty = evaluate_provider_binding_convergence(
        provider=PROVIDER,
        provider_session_id=NATIVE_ID,
        candidates=[],
    )
    assert empty.reason == "no_session"


def test_session_detail_emits_provider_session_id_header(tmp_path):
    # The live audit groups sessions by the native id from this header; the list
    # API does not expose it, so the detail endpoint must.
    factory = _session_factory(tmp_path)
    db = factory()
    try:
        _seed_managed_launch(db, session_id=LAUNCH_SESSION_ID)
        AgentsStore(db).ingest_session(_transcript_ingest())
        db.commit()
    finally:
        db.close()

    def override_db():
        d = factory()
        try:
            yield d
        finally:
            d.close()

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[verify_agents_token] = lambda: None
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    try:
        client = TestClient(api_app)
        response = client.get(f"/agents/sessions/{LAUNCH_SESSION_ID}")
        assert response.status_code == 200, response.text
        assert response.headers.get("X-Provider-Session-ID") == NATIVE_ID
    finally:
        api_app.dependency_overrides.clear()
