import pytest

pytest.skip(
    "session-identity-kernel cleanup: thread_root_session_id and continuation lineage "
    "columns were removed; each session is now its own thread root and the legacy "
    "stitching projection is retired.",
    allow_module_level=True,
)

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.database import Base
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest
from zerg.services.agents_store import SourceLineIngest


def _make_db(tmp_path):
    db_path = tmp_path / "session_projection.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _get_client(session_factory):
    from zerg.main import api_app

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    def override_verify_agents_token():
        return SimpleNamespace(device_id="projection-api", id="token-1", owner_id=1)

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    client = TestClient(api_app)
    yield client
    api_app.dependency_overrides.clear()


def _ingest_session(
    store: AgentsStore,
    *,
    session_id,
    started_at: datetime,
    environment: str,
    device_id: str,
    continuation_kind: str,
    origin_label: str,
    events: list[tuple[str, str]],
    provider_session_id: str = "prov-1",
    thread_root_session_id=None,
    continued_from_session_id=None,
    branched_from_event_id=None,
    offset_base: int = 0,
):
    payload = SessionIngest(
        id=session_id,
        provider="claude",
        environment=environment,
        project="zerg",
        device_id=device_id,
        cwd="/tmp/zerg",
        git_repo="git@github.com:cipher982/longhouse.git",
        git_branch="main",
        started_at=started_at,
        provider_session_id=provider_session_id,
        thread_root_session_id=thread_root_session_id,
        continued_from_session_id=continued_from_session_id,
        continuation_kind=continuation_kind,
        origin_label=origin_label,
        branched_from_event_id=branched_from_event_id,
        events=[
            EventIngest(
                role=role,
                content_text=content_text,
                timestamp=started_at + timedelta(seconds=index),
                source_path="/tmp/session.jsonl",
                source_offset=(offset_base + index) * 100,
            )
            for index, (role, content_text) in enumerate(events)
        ],
        source_lines=[
            SourceLineIngest(
                source_path="/tmp/source.jsonl",
                source_offset=offset_base + index,
                raw_json=f'{{"role":"{role}","content":"{content_text}"}}',
            )
            for index, (role, content_text) in enumerate(events)
        ],
    )
    result = store.ingest_session(payload)
    store.db.commit()
    return result


def _seed_root_and_cloud(db):
    store = AgentsStore(db)
    root_id = uuid4()
    cloud_id = uuid4()
    root_started = datetime(2026, 3, 19, 13, 0, tzinfo=timezone.utc)
    cloud_started = root_started + timedelta(minutes=5)

    _ingest_session(
        store,
        session_id=root_id,
        started_at=root_started,
        environment="Cinder",
        device_id="shipper-cinder",
        continuation_kind="local",
        origin_label="Cinder",
        events=[
            ("user", "root question"),
            ("assistant", "root answer"),
        ],
        offset_base=0,
    )

    root = store.get_session(root_id)
    assert root is not None
    branch_event_id = store.get_latest_event_id(root_id)

    _ingest_session(
        store,
        session_id=cloud_id,
        started_at=cloud_started,
        environment="Cloud",
        device_id="zerg-commis-cloud",
        continuation_kind="cloud",
        origin_label="Cloud",
        thread_root_session_id=root_id,
        continued_from_session_id=root_id,
        branched_from_event_id=branch_event_id,
        events=[("user", "cloud follow-up")],
        offset_base=10,
    )

    root = store.get_session(root_id)
    cloud = store.get_session(cloud_id)
    assert root is not None and cloud is not None
    return store, root, cloud


def _seed_stale_sibling_fixture(db):
    store = AgentsStore(db)
    root_id = uuid4()
    cloud_id = uuid4()
    local_id = uuid4()
    root_started = datetime(2026, 3, 19, 13, 0, tzinfo=timezone.utc)

    _ingest_session(
        store,
        session_id=root_id,
        started_at=root_started,
        environment="Cinder",
        device_id="shipper-cinder",
        continuation_kind="local",
        origin_label="Cinder",
        events=[("user", "root only")],
        offset_base=0,
    )
    branch_event_id = store.get_latest_event_id(root_id)

    _ingest_session(
        store,
        session_id=cloud_id,
        started_at=root_started + timedelta(minutes=5),
        environment="Cloud",
        device_id="zerg-commis-cloud",
        continuation_kind="cloud",
        origin_label="Cloud",
        thread_root_session_id=root_id,
        continued_from_session_id=root_id,
        branched_from_event_id=branch_event_id,
        events=[("assistant", "cloud head")],
        offset_base=10,
    )

    _ingest_session(
        store,
        session_id=local_id,
        started_at=root_started + timedelta(minutes=8),
        environment="Cinder",
        device_id="shipper-cinder",
        continuation_kind="local",
        origin_label="Cinder",
        thread_root_session_id=root_id,
        continued_from_session_id=root_id,
        branched_from_event_id=branch_event_id,
        events=[("assistant", "local stale sibling")],
        offset_base=20,
    )

    root = store.get_session(root_id)
    cloud = store.get_session(cloud_id)
    local = store.get_session(local_id)
    assert root is not None and cloud is not None and local is not None

    # Keep the cloud child as the current head so the local child is a stale sibling.
    root.is_writable_head = 0
    cloud.is_writable_head = 1
    local.is_writable_head = 0
    db.commit()

    return store, root, cloud, local


def test_projection_path_follows_the_selected_branch(tmp_path):
    Session = _make_db(tmp_path)

    with Session() as db:
        store, root, cloud, local = _seed_stale_sibling_fixture(db)

        assert store.get_thread_head(root.id).id == cloud.id
        path = store.get_session_lineage_path(local.id)
        assert [item.id for item in path] == [root.id, local.id]

        projection = store.get_session_projection_page(local.id, limit=10, offset=0)
        assert [item.kind for item in projection.items] == ["event", "seam", "event"]
        assert [item.session.id for item in projection.items] == [root.id, local.id, local.id]


def test_projection_paginates_across_a_seam_boundary(tmp_path):
    Session = _make_db(tmp_path)

    with Session() as db:
        store, root, cloud = _seed_root_and_cloud(db)

        projection = store.get_session_projection_page(cloud.id, limit=2, offset=2)

        assert projection.total == 4
        assert [item.kind for item in projection.items] == ["seam", "event"]
        assert projection.items[0].session.id == cloud.id
        assert projection.items[1].event is not None
        assert projection.items[1].event.content_text == "cloud follow-up"
        assert [item.id for item in projection.path_sessions] == [root.id, cloud.id]


def test_projection_endpoint_returns_stitched_items(tmp_path):
    Session = _make_db(tmp_path)
    db = Session()
    try:
        _store, root, cloud = _seed_root_and_cloud(db)
        root_id = str(root.id)
        cloud_id = str(cloud.id)
    finally:
        db.close()

    for client in _get_client(Session):
        response = client.get(f"/agents/sessions/{cloud_id}/projection?limit=10&offset=0")
        assert response.status_code == 200
        data = response.json()

        assert data["root_session_id"] == root_id
        assert data["focus_session_id"] == cloud_id
        assert data["head_session_id"] == cloud_id
        assert data["path_session_ids"] == [root_id, cloud_id]
        assert data["total"] == 4
        assert [item["kind"] for item in data["items"]] == ["event", "event", "seam", "event"]
        assert data["items"][2]["continued_from_session_id"] == root_id
        assert data["items"][2]["origin_label"] == "Cloud"
        assert data["items"][3]["event"]["content_text"] == "cloud follow-up"


def test_projection_endpoint_anchor_tail_returns_latest_window(tmp_path):
    Session = _make_db(tmp_path)
    db = Session()
    try:
        store = AgentsStore(db)
        session_id = uuid4()
        started_at = datetime(2026, 4, 3, 12, 0, tzinfo=timezone.utc)
        _ingest_session(
            store,
            session_id=session_id,
            started_at=started_at,
            environment="Cinder",
            device_id="shipper-cinder",
            continuation_kind="local",
            origin_label="Cinder",
            events=[
                ("user", "event 1"),
                ("assistant", "event 2"),
                ("user", "event 3"),
                ("assistant", "event 4"),
                ("user", "event 5"),
            ],
        )
        session_id_str = str(session_id)
    finally:
        db.close()

    for client in _get_client(Session):
        response = client.get(f"/agents/sessions/{session_id_str}/projection?limit=2&anchor=tail")
        assert response.status_code == 200
        data = response.json()

        assert data["total"] == 5
        assert data["page_offset"] == 3
        assert [item["event"]["content_text"] for item in data["items"]] == ["event 4", "event 5"]

        response = client.get(f"/agents/sessions/{session_id_str}/projection?limit=2&anchor=tail&offset=2")
        assert response.status_code == 200
        data = response.json()

        assert data["total"] == 5
        assert data["page_offset"] == 1
        assert [item["event"]["content_text"] for item in data["items"]] == ["event 2", "event 3"]
