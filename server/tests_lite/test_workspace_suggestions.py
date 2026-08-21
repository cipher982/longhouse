"""Tests for the launch-picker workspace suggestions service + routes.

Covers the renamed-machine ghost-device leak that motivated the refactor:
sessions whose dead ``device_id`` differs from the live machine name must NOT
surface for that machine, only true ``device_id`` matches.
"""

from __future__ import annotations

import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from uuid import uuid4

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from zerg.database import Base  # noqa: E402
from zerg.database import get_db  # noqa: E402
from zerg.database import make_engine  # noqa: E402
from zerg.dependencies.agents_auth import require_single_tenant  # noqa: E402
from zerg.dependencies.agents_auth import verify_agents_token  # noqa: E402
from zerg.models import User  # noqa: E402
from zerg.models.agents import AgentSession  # noqa: E402
from zerg.models.agents import SessionThread  # noqa: E402
from zerg.models.device_token import DeviceToken  # noqa: E402
from zerg.services.session_hot_cards import upsert_timeline_card_from_session  # noqa: E402
from zerg.services.workspace_suggestion_projection import WORKSPACE_CANDIDATE_PAGE_SIZE  # noqa: E402
from zerg.services.workspace_suggestion_projection import WorkspaceSessionFacts  # noqa: E402
from zerg.services.workspace_suggestion_projection import rank_human_workspace_candidates  # noqa: E402
from zerg.services.workspace_suggestions import build_workspace_suggestions  # noqa: E402

OWNER_ID = 42


def _make_db(tmp_path):
    db_path = tmp_path / "test_workspace_suggestions.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _seed_user(SessionLocal):
    with SessionLocal() as db:
        db.add(User(id=OWNER_ID, email=f"user{OWNER_ID}@example.com", role="ADMIN"))
        db.commit()


def _enroll(SessionLocal, device_id: str):
    with SessionLocal() as db:
        db.add(DeviceToken(owner_id=OWNER_ID, device_id=device_id, token_hash=f"hash-{device_id}"))
        db.commit()


def _seed_session(
    SessionLocal,
    *,
    device_id: str | None,
    cwd: str | None,
    environment: str = "production",
    days_ago: float = 0.0,
    git_repo: str | None = None,
    git_branch: str | None = None,
    provider: str = "codex",
    project: str | None = None,
    launch_actor: str | None = "human_shell",
    origin_kind: str | None = None,
    hidden_from_default_timeline: int = 0,
    user_hidden_from_timeline: int = 0,
    first_user_message_preview: str | None = None,
    sidechain: bool = False,
):
    now = datetime.now(timezone.utc)
    ts = now - timedelta(days=days_ago)
    with SessionLocal() as db:
        session = AgentSession(
            provider=provider,
            environment=environment,
            project=project,
            device_id=device_id,
            cwd=cwd,
            git_repo=git_repo,
            git_branch=git_branch,
            started_at=ts,
            last_activity_at=ts,
            user_messages=1,
            needs_embedding=0,
            launch_actor=launch_actor,
            origin_kind=origin_kind,
            hidden_from_default_timeline=hidden_from_default_timeline,
            user_hidden_from_timeline=user_hidden_from_timeline,
            first_user_message_preview=first_user_message_preview,
        )
        db.add(session)
        db.flush()
        if sidechain:
            thread = SessionThread(
                session_id=session.id,
                provider=provider,
                branch_kind="subagent",
                is_primary=1,
            )
            db.add(thread)
            db.flush()
            session.primary_thread_id = thread.id
        upsert_timeline_card_from_session(db, session)
        db.commit()


def test_device_scoping_excludes_ghost_rows(tmp_path):
    """Renamed-machine ghost rows (dead device_id, env=new name) must not leak."""
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    _enroll(SessionLocal, "cinder")
    # Real cinder session
    _seed_session(SessionLocal, device_id="cinder", cwd="/Users/d/git/zerg", days_ago=0.5)
    # Ghost: dead device_id, environment names the live machine
    _seed_session(
        SessionLocal, device_id="shipper-laptop", cwd="/Users/d/git/ghost", environment="cinder", days_ago=0.1
    )

    with SessionLocal() as db:
        entries = build_workspace_suggestions(db, owner_id=OWNER_ID, device_id="cinder", limit=12)

    paths = [e.path for e in entries]
    assert "/Users/d/git/zerg" in paths
    assert "/Users/d/git/ghost" not in paths


def test_frecency_ranks_frequent_recent_first(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    _enroll(SessionLocal, "cinder")
    # Frequent + recent home dir
    for _ in range(5):
        _seed_session(SessionLocal, device_id="cinder", cwd="/Users/d", days_ago=0.2)
    # Stale, used once long ago
    _seed_session(SessionLocal, device_id="cinder", cwd="/Users/d/git/stale", days_ago=20.0)

    with SessionLocal() as db:
        entries = build_workspace_suggestions(db, owner_id=OWNER_ID, device_id="cinder", limit=12)

    assert entries[0].path == "/Users/d"
    assert entries[0].session_count == 5
    assert entries[0].score > entries[1].score
    assert entries[0].last_used_at is not None


def test_git_labels_and_fallback(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    _enroll(SessionLocal, "cinder")
    _seed_session(
        SessionLocal,
        device_id="cinder",
        cwd="/Users/d/git/zerg/longhouse",
        git_repo="git@github.com:cipher982/longhouse.git",
        git_branch="main",
        days_ago=0.1,
    )
    _seed_session(SessionLocal, device_id="cinder", cwd="/Users/d", days_ago=0.2)

    with SessionLocal() as db:
        entries = {e.path: e for e in build_workspace_suggestions(db, owner_id=OWNER_ID, device_id="cinder")}

    assert entries["/Users/d/git/zerg/longhouse"].label == "longhouse (main)"
    assert entries["/Users/d"].label == "~"


def test_limit_caps_top_ranked(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    _enroll(SessionLocal, "cinder")
    for i in range(8):
        _seed_session(SessionLocal, device_id="cinder", cwd=f"/Users/d/p{i}", days_ago=float(i))

    with SessionLocal() as db:
        entries = build_workspace_suggestions(db, owner_id=OWNER_ID, device_id="cinder", limit=3)

    assert len(entries) == 3
    # Most recent (p0) ranks first
    assert entries[0].path == "/Users/d/p0"


def test_unenrolled_device_returns_empty(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    _seed_session(SessionLocal, device_id="ghost", cwd="/Users/d/git/zerg", days_ago=0.1)

    with SessionLocal() as db:
        entries = build_workspace_suggestions(db, owner_id=OWNER_ID, device_id="ghost", limit=12)

    assert entries == []


def test_excludes_relative_paths_and_test_environments(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    _enroll(SessionLocal, "cinder")
    _seed_session(SessionLocal, device_id="cinder", cwd="relative/path", days_ago=0.1)
    _seed_session(SessionLocal, device_id="cinder", cwd=None, days_ago=0.1)
    _seed_session(SessionLocal, device_id="cinder", cwd="/Users/d/e2e", environment="e2e", days_ago=0.1)
    _seed_session(SessionLocal, device_id="cinder", cwd="/Users/d/real", days_ago=0.1)

    with SessionLocal() as db:
        paths = [e.path for e in build_workspace_suggestions(db, owner_id=OWNER_ID, device_id="cinder")]

    assert paths == ["/Users/d/real"]


def test_excludes_sessions_outside_lookback(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    _enroll(SessionLocal, "cinder")
    _seed_session(SessionLocal, device_id="cinder", cwd="/Users/d/old", days_ago=200.0)
    _seed_session(SessionLocal, device_id="cinder", cwd="/Users/d/fresh", days_ago=1.0)

    with SessionLocal() as db:
        paths = [e.path for e in build_workspace_suggestions(db, owner_id=OWNER_ID, device_id="cinder", days_back=45)]

    assert paths == ["/Users/d/fresh"]


def test_only_typed_human_visible_root_sessions_contribute_to_rank(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    _enroll(SessionLocal, "cinder")
    # Two eligible human rows establish the score/count and newest git label.
    _seed_session(
        SessionLocal,
        device_id="cinder",
        cwd="/Users/d/git/zerg",
        days_ago=2.0,
        git_repo="x/old.git",
        git_branch="old",
    )
    _seed_session(
        SessionLocal,
        device_id="cinder",
        cwd="/Users/d/git/zerg",
        days_ago=0.5,
        git_repo="x/zerg.git",
        git_branch="main",
        launch_actor="human_ui",
    )
    # Negative facts must not contribute even when cwd/time otherwise match.
    _seed_session(
        SessionLocal,
        device_id="cinder",
        cwd="/Users/d/git/zerg",
        days_ago=0.1,
        hidden_from_default_timeline=1,
    )
    _seed_session(
        SessionLocal,
        device_id="cinder",
        cwd="/Users/d/hidden-by-user",
        days_ago=0.1,
        user_hidden_from_timeline=1,
    )
    _seed_session(
        SessionLocal,
        device_id="cinder",
        cwd="/Users/d/automation",
        days_ago=0.1,
        launch_actor="automation",
        origin_kind="hatch_automation",
    )
    _seed_session(
        SessionLocal,
        device_id="cinder",
        cwd="/Users/d/no-positive-provenance",
        days_ago=0.1,
        launch_actor=None,
    )
    _seed_session(
        SessionLocal,
        device_id="cinder",
        cwd="/Users/d/sidechain",
        days_ago=0.1,
        sidechain=True,
    )
    # Narrow provider-proof recognition is negative evidence only: even stale
    # development rows with a human actor stay out.
    _seed_session(
        SessionLocal,
        device_id="cinder",
        cwd="/Users/d/.longhouse/canaries/provider-live/cursor/product-e2e/workspace",
        days_ago=0.1,
        launch_actor="human_shell",
    )
    _seed_session(
        SessionLocal,
        device_id="cinder",
        cwd="/Users/d/.longhouse/canaries/provider-live/claude/qualification-r5",
        days_ago=0.1,
        launch_actor="human_shell",
    )
    _seed_session(
        SessionLocal,
        device_id="cinder",
        cwd="/Users/d/marker-proof",
        days_ago=0.1,
        launch_actor="human_shell",
        first_user_message_preview="LONGHOUSE_CURSOR_NOREPLY_deadbeef",
    )

    with SessionLocal() as db:
        entries = build_workspace_suggestions(db, owner_id=OWNER_ID, device_id="cinder")

    assert [entry.path for entry in entries] == ["/Users/d/git/zerg", "/Users/d/hidden-by-user"]
    assert entries[0].session_count == 2
    assert entries[0].score == 170
    assert entries[0].label == "zerg (main)"


def test_projection_preserves_user_curated_workspace_but_rejects_system_policy() -> None:
    now = datetime.now(timezone.utc)

    def facts(path: str, *, user_hidden: bool = False, system_hidden: bool = False, actor: str = "human_shell"):
        return WorkspaceSessionFacts(
            device_id="cinder",
            provider="codex",
            environment="production",
            project="zerg",
            cwd=path,
            git_repo=None,
            git_branch=None,
            last_activity_at=now,
            started_at=now,
            first_user_message_preview=None,
            origin_kind=None,
            hidden_from_default_timeline=system_hidden,
            user_hidden_from_timeline=user_hidden,
            launch_actor=actor,
            is_sidechain=False,
        )

    entries = rank_human_workspace_candidates(
        [
            facts("/Users/d/user-curated", user_hidden=True),
            facts("/Users/d/system-hidden", system_hidden=True),
            facts("/Users/d/automation", actor="automation"),
        ],
        device_id="cinder",
        now=now,
        days_back=45,
        limit=12,
    )

    assert [entry.path for entry in entries] == ["/Users/d/user-curated"]


def test_paged_scan_ranks_over_full_bounded_corpus(tmp_path, monkeypatch):
    import zerg.services.workspace_suggestions as workspace_service

    monkeypatch.setattr(workspace_service, "WORKSPACE_CANDIDATE_PAGE_SIZE", 2)
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    _enroll(SessionLocal, "cinder")
    _seed_session(SessionLocal, device_id="cinder", cwd="/Users/d/one-off-a", days_ago=0.01)
    _seed_session(SessionLocal, device_id="cinder", cwd="/Users/d/one-off-b", days_ago=0.02)
    for age in (0.5, 0.6, 0.7):
        _seed_session(SessionLocal, device_id="cinder", cwd="/Users/d/repeated", days_ago=age)

    with SessionLocal() as db:
        entries = build_workspace_suggestions(db, owner_id=OWNER_ID, device_id="cinder", limit=2)

    assert [entry.path for entry in entries] == ["/Users/d/repeated", "/Users/d/one-off-a"]
    assert entries[0].session_count == 3


def test_automation_volume_cannot_crow_human_workspace_out_of_raw_page(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    _enroll(SessionLocal, "cinder")
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        db.execute(
            AgentSession.__table__.insert(),
            [
                {
                    "id": uuid4(),
                    "provider": "canary",
                    "environment": "production",
                    "project": "canary",
                    "device_id": "cinder",
                    "cwd": f"/tmp/provider-factory-{index}/workspace",
                    "started_at": now,
                    "last_activity_at": now,
                    # Historical canaries may carry an incorrect human stamp;
                    # the classifier still rejects their typed proof identity.
                    "launch_actor": "human_shell",
                    "user_messages": 1,
                    "needs_embedding": 0,
                }
                for index in range(WORKSPACE_CANDIDATE_PAGE_SIZE + 1)
            ],
        )
        db.commit()
    _seed_session(
        SessionLocal,
        device_id="cinder",
        cwd="/Users/d/git/human",
        days_ago=1,
        git_repo="x/human.git",
        git_branch="main",
    )

    with SessionLocal() as db:
        entries = build_workspace_suggestions(db, owner_id=OWNER_ID, device_id="cinder", limit=12)

    assert [entry.path for entry in entries] == ["/Users/d/git/human"]


def test_list_sessions_no_longer_falls_back_to_environment(tmp_path):
    """Regression: the device_id filter must not match on `environment`."""
    from zerg.services.agents.store import AgentsStore

    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    _enroll(SessionLocal, "cinder")
    _seed_session(SessionLocal, device_id="cinder", cwd="/Users/d/git/zerg", days_ago=0.1)
    # Ghost row: only `environment` equals the filter value.
    _seed_session(
        SessionLocal, device_id="shipper-laptop", cwd="/Users/d/git/ghost", environment="cinder", days_ago=0.1
    )

    with SessionLocal() as db:
        sessions, _total = AgentsStore(db).list_sessions(device_id="cinder", limit=50, hide_autonomous=False)

    device_ids = {s.device_id for s in sessions}
    assert device_ids == {"cinder"}


def test_endpoint_returns_scoped_workspaces(tmp_path):
    SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)
    _enroll(SessionLocal, "cinder")
    _seed_session(
        SessionLocal,
        device_id="cinder",
        cwd="/Users/d/git/zerg",
        git_repo="x/zerg.git",
        git_branch="main",
        days_ago=0.1,
    )
    _seed_session(
        SessionLocal, device_id="shipper-laptop", cwd="/Users/d/git/ghost", environment="cinder", days_ago=0.05
    )

    from zerg.main import api_app

    def _get_db_override():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    def _token_override():
        return DeviceToken(owner_id=OWNER_ID, device_id="cinder", token_hash="h")

    api_app.dependency_overrides[get_db] = _get_db_override
    api_app.dependency_overrides[verify_agents_token] = _token_override
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    try:
        client = TestClient(api_app)
        resp = client.get("/agents/machines/cinder/workspaces")
        assert resp.status_code == 200
        body = resp.json()
        assert body["device_id"] == "cinder"
        paths = [w["path"] for w in body["workspaces"]]
        assert paths == ["/Users/d/git/zerg"]
        assert body["workspaces"][0]["label"] == "zerg (main)"
    finally:
        api_app.dependency_overrides.clear()
