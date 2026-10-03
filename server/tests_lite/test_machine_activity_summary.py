from __future__ import annotations

import os
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.models import StorageSession
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.server import CatalogDaemon
from zerg.catalogd.store import CatalogStore
from zerg.models.live_store import LiveSession
from zerg.services import machines_summary


@pytest.fixture
def engine():
    root = Path("/tmp") / f"lh-machine-activity-{uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    engine = create_catalog_engine(root / "live.db")
    initialize_catalog_schema(engine)
    yield engine
    engine.dispose()
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


def _session(
    connection,
    *,
    device: str,
    provider: str,
    project: str,
    started_at: datetime,
    owner: str = "1",
    hidden: bool = False,
    title: str = "Work",
) -> str:
    session_id = str(uuid4())
    connection.execute(
        LiveSession.__table__.insert().values(
            session_id=session_id,
            owner_id=owner,
            provider=provider,
            device_id=device,
            machine_id=device,
            state="idle",
            started_at=started_at,
            last_seen_at=started_at,
            updated_at=started_at,
        )
    )
    connection.execute(
        StorageSession.__table__.insert().values(
            session_id=session_id,
            tenant_id="default",
            owner_id=owner,
            provider=provider,
            environment="development",
            machine_id=device,
            project=project,
            started_at=started_at,
            last_activity_at=started_at + timedelta(minutes=5),
            user_messages=1,
            assistant_messages=1,
            first_user_message_preview=title,
            render_state="ready",
            raw_state="durable",
            media_state="complete",
            user_hidden_from_timeline=1 if hidden else 0,
            commit_seq=1,
            created_at=started_at,
            updated_at=started_at,
        )
    )
    return session_id


def _activity(store: CatalogStore, *, days: int, utc_offset_minutes: int = 0) -> dict[str, dict]:
    result = store.list_session_timeline(
        project=None,
        provider=None,
        environment=None,
        include_test=False,
        hide_autonomous=True,
        include_automation=False,
        include_hidden=False,
        device_id=None,
        days_back=days,
        limit=1,
        offset=0,
        owner_id=1,
        machine_activity=True,
        utc_offset_minutes=utc_offset_minutes,
    )
    return {item["device_id"]: item for item in result["machines"]}


def test_machine_activity_counts_only_the_owners_visible_sessions_in_the_window(engine):
    now = datetime.now(UTC)
    with engine.begin() as connection:
        _session(connection, device="cinder", provider="omp", project="zerg", started_at=now - timedelta(hours=1))
        _session(connection, device="cinder", provider="claude", project="zerg", started_at=now - timedelta(hours=2))
        _session(connection, device="cinder", provider="omp", project="zeta", started_at=now - timedelta(days=1))
        latest = _session(
            connection, device="cube", provider="codex", project="g55", started_at=now - timedelta(minutes=10), title="Newest"
        )
        _session(connection, device="cinder", provider="omp", project="zerg", started_at=now - timedelta(days=20))
        _session(connection, device="cinder", provider="omp", project="zerg", started_at=now - timedelta(hours=1), owner="2")
        _session(connection, device="cinder", provider="omp", project="zerg", started_at=now - timedelta(hours=1), hidden=True)

    machines = _activity(CatalogStore(engine), days=14)

    assert set(machines) == {"cinder", "cube"}
    cinder = machines["cinder"]
    assert cinder["sessions_started"] == 3
    assert sum(sum(day["by_provider"].values()) for day in cinder["daily"]) == 3
    assert cinder["top_projects"] == [{"project": "zerg", "sessions": 2}, {"project": "zeta", "sessions": 1}]
    assert machines["cube"]["latest"]["session_id"] == latest
    assert machines["cube"]["latest"]["title"] == "Newest"


def test_machine_activity_buckets_days_in_the_callers_timezone(engine):
    now = datetime.now(UTC)
    # Shortly after midnight UTC today is still yesterday six hours west.
    started = now.replace(hour=0, minute=30, second=0, microsecond=0)
    if started > now:
        started -= timedelta(days=1)
    with engine.begin() as connection:
        _session(connection, device="cinder", provider="omp", project="zerg", started_at=started)

    utc_days = [day["date"] for day in _activity(CatalogStore(engine), days=3)["cinder"]["daily"]]
    west_days = [day["date"] for day in _activity(CatalogStore(engine), days=3, utc_offset_minutes=-360)["cinder"]["daily"]]

    assert utc_days == [started.date().isoformat()]
    assert west_days == [(started.date() - timedelta(days=1)).isoformat()]


@pytest.mark.asyncio
async def test_machine_activity_rpc_is_owner_scoped_and_bounded(engine):
    now = datetime.now(UTC)
    with engine.begin() as connection:
        _session(connection, device="cinder", provider="omp", project="zerg", started_at=now - timedelta(hours=1))
        _session(connection, device="private", provider="omp", project="secret", started_at=now - timedelta(hours=1), owner="2")
    database_path = Path(engine.url.database)
    engine.dispose()
    daemon = CatalogDaemon(database_path=database_path, socket_path=database_path.parent / "catalogd.sock")
    await daemon.start()
    client = CatalogClient(database_path.parent / "catalogd.sock")
    try:
        result = await client.call("machine.activity.summary.v2", {"owner_id": 1, "days_back": 14, "utc_offset_minutes": -300})
        assert [item["device_id"] for item in result["machines"]] == ["cinder"]
        assert result["machines"][0]["sessions_started"] == 1
        with pytest.raises(CatalogRemoteError):
            await client.call("machine.activity.summary.v2", {"owner_id": 1, "days_back": 31, "utc_offset_minutes": 0})
    finally:
        await client.close()
        await daemon.close()


def _head(session_id: str, *, working_set: str, minutes_ago: int):
    return SimpleNamespace(
        id=session_id,
        timeline_title=f"session {session_id}",
        anchor_title=None,
        summary_title=None,
        project="zerg",
        provider="omp",
        last_activity_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        session_state=SimpleNamespace(working_set=working_set, activity=SimpleNamespace(state="thinking")),
    )


def test_summary_zero_fills_days_and_counts_only_served_open_sessions(monkeypatch):
    observed = datetime(2026, 10, 3, 16, 0, tzinfo=UTC)
    directory_entry = SimpleNamespace(
        to_response=lambda: {
            "device_id": "cinder",
            "machine_name": "cinder",
            "online": True,
            "control_channel_status": "connected",
            "launch": {"providers": [{"provider": "omp"}], "default_provider": "omp"},
        }
    )
    idle_entry = SimpleNamespace(
        to_response=lambda: {
            "device_id": "cube",
            "machine_name": "cube",
            "online": False,
            "control_channel_status": "disconnected",
            "launch": {"providers": [], "blocked_by": "control_down"},
        }
    )
    monkeypatch.setattr(machines_summary, "enrolled_machines", lambda owner_id: {"enrollments": []})
    monkeypatch.setattr(machines_summary, "build_machines_directory", lambda **_: [directory_entry, idle_entry])
    monkeypatch.setattr(
        machines_summary,
        "machine_activity",
        lambda **_: {
            "observed_at": observed.isoformat(),
            "machines": [
                {
                    "device_id": "cinder",
                    "sessions_started": 2,
                    "daily": [{"date": "2026-10-03", "by_provider": {"omp": 2}}],
                    "top_projects": [{"project": "zerg", "sessions": 2}],
                    "latest": None,
                    "open_candidates": 2,
                    "unread": 0,
                }
            ],
        },
    )
    monkeypatch.setattr(machines_summary, "machine_heartbeats", lambda **_: {"heartbeats": []})
    # The SQL open flag is a superset; one candidate is history once projected.
    page = SimpleNamespace(
        sessions=[
            SimpleNamespace(head=_head("a", working_set="open", minutes_ago=1)),
            SimpleNamespace(head=_head("b", working_set="history", minutes_ago=2)),
        ]
    )
    monkeypatch.setattr(machines_summary, "list_live_catalog_timeline", lambda **_: page)

    summary = machines_summary.build_machines_summary(owner_id=1, days=7, utc_offset_minutes=0)

    cinder, cube = summary.machines
    assert [day.date for day in cinder.activity.daily] == [f"2026-09-{day}" for day in range(27, 31)] + [
        "2026-10-01",
        "2026-10-02",
        "2026-10-03",
    ]
    assert [day.total for day in cinder.activity.daily] == [0, 0, 0, 0, 0, 0, 2]
    assert cinder.activity.live_count == 1
    assert [item.session_id for item in cinder.activity.live_sessions] == ["a"]
    assert cube.activity.sessions_started == 0
    assert cube.activity.live_count == 0
    assert len(cube.activity.daily) == 7
    assert cube.sync is None
    assert (summary.first_day, summary.last_day) == ("2026-09-27", "2026-10-03")
