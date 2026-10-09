"""Provider CLI release per session: envelope parsing, catalog upsert, and the field-versions read."""

from __future__ import annotations

import copy
import os
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi import status

os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from tests_lite.test_catalogd_storage_v2 import _raw_params
from tests_lite.test_provider_capability_proof_routes import _client
from zerg.auth.caller import Caller
from zerg.auth.managed_session_tokens import ManagedSessionToken
from zerg.catalogd.client import CatalogClient
from zerg.catalogd.models import StorageSession
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.server import CatalogDaemon
from zerg.catalogd.store import CatalogStore
from zerg.dependencies.agents_auth import verify_agents_caller
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.main import api_app
from zerg.routers import provider_capability_proofs as routes
from zerg.routers.agents_storage_v2 import _parse_session_facts

URL = "/api/agents/provider-field-versions"


def _session_facts(**overrides) -> dict:
    facts = {
        "environment": "local",
        "project": "longhouse",
        "cwd": "/workspace/longhouse",
        "git_repo": "cipher982/longhouse",
        "git_branch": "main",
        "started_at": "2026-07-12T11:00:00+00:00",
        "last_activity_at": "2026-07-12T12:00:00+00:00",
        "ended_at": None,
        "origin_kind": "shadow",
        "hidden_from_default_timeline": False,
        "launch_actor": None,
        "launch_surface": None,
    }
    facts.update(overrides)
    return facts


# --- envelope parsing -----------------------------------------------------


def test_envelope_without_provider_version_parses_unchanged_for_old_engines():
    legacy = _session_facts()
    parsed = _parse_session_facts(copy.deepcopy(legacy))

    assert parsed["provider_version"] is None
    assert {key: parsed[key] for key in legacy} == legacy


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2.1.0", "2.1.0"),
        ("  0.145.0 \n", "0.145.0"),
        ("", None),
        ("   ", None),
        ("x" * 65, None),
        (3, None),  # Pi/OMP numeric header version is a file-format number, never a CLI release
        (None, None),
    ],
)
def test_envelope_provider_version_is_kept_only_when_usable(raw, expected):
    parsed = _parse_session_facts(_session_facts(provider_version=raw))

    assert parsed["provider_version"] == expected


def test_unusable_provider_version_never_rejects_the_envelope():
    parsed = _parse_session_facts(_session_facts(provider_version={"nested": True}))

    assert parsed["provider_version"] is None


# --- catalog ingest round trip and upsert rule -----------------------------


@pytest.fixture
def catalog_paths():
    root = Path("/tmp") / f"lhpfv-{uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    yield root / "live.db", root / "catalogd.sock"
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


def _version_of(database_path: Path, session_id) -> str | None:
    engine = create_catalog_engine(database_path)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                StorageSession.__table__.select().where(StorageSession.__table__.c.session_id == str(session_id))
            ).one()
            return row.provider_version
    finally:
        engine.dispose()


def _envelope(session_id, *, start: int, end: int, version, now: datetime, epoch) -> dict:
    raw = _raw_params(
        epoch=epoch, session_id=session_id, start=start, end=end, records=(b"x" * (end - start),), sealed_at=now, provider="claude"
    )
    raw["session_facts"]["provider_version"] = version
    return raw


@pytest.mark.asyncio
async def test_ingest_stores_provider_version_then_later_value_replaces_and_null_never_clears(catalog_paths):
    database_path, socket_path = catalog_paths
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = uuid4()
    epoch = uuid4()
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        await client.call("storage.raw_object.commit.v2", _envelope(session_id, start=0, end=6, version="2.1.0", now=now, epoch=epoch))
        assert _version_of(database_path, session_id) == "2.1.0"

        # A resumed session on a newer CLI replaces the recorded version.
        await client.call("storage.raw_object.commit.v2", _envelope(session_id, start=6, end=12, version="2.1.7", now=now, epoch=epoch))
        assert _version_of(database_path, session_id) == "2.1.7"

        # An envelope from an engine that does not know the field must not clear it.
        await client.call("storage.raw_object.commit.v2", _envelope(session_id, start=12, end=18, version=None, now=now, epoch=epoch))
        assert _version_of(database_path, session_id) == "2.1.7"
    finally:
        await client.close()
        await daemon.close()


# --- read: aggregation, window, environment, owner ------------------------


@pytest.fixture
def seeded_engine(tmp_path):
    engine = create_catalog_engine(tmp_path / "catalog.db")
    initialize_catalog_schema(engine)
    yield engine
    engine.dispose()


def _seed(
    connection,
    *,
    owner: str = "1",
    provider: str,
    version: str | None,
    device: str,
    started_at: datetime,
    env: str = "local",
    last_offset_minutes: int = 5,
) -> None:
    connection.execute(
        StorageSession.__table__.insert().values(
            session_id=str(uuid4()),
            tenant_id="default",
            owner_id=owner,
            provider=provider,
            provider_version=version,
            environment=env,
            machine_id=device,
            started_at=started_at,
            last_activity_at=started_at + timedelta(minutes=last_offset_minutes),
            user_messages=1,
            assistant_messages=1,
            render_state="ready",
            raw_state="durable",
            media_state="complete",
            commit_seq=1,
            created_at=started_at,
            updated_at=started_at,
        )
    )


def test_summary_counts_devices_and_first_last_seen_per_provider_version(seeded_engine):
    now = datetime.now(UTC)
    first = now - timedelta(days=3)
    with seeded_engine.begin() as connection:
        _seed(connection, provider="claude", version="2.1.0", device="cinder", started_at=first, last_offset_minutes=10)
        _seed(connection, provider="claude", version="2.1.0", device="cinder", started_at=now - timedelta(days=1), last_offset_minutes=60)
        _seed(connection, provider="claude", version="2.1.0", device="cube", started_at=now - timedelta(hours=2))
        _seed(connection, provider="claude", version="2.1.7", device="cube", started_at=now - timedelta(hours=1))
        _seed(connection, provider="codex", version="0.145.0", device="cinder", started_at=now - timedelta(hours=5))

    summary = CatalogStore(seeded_engine).summarize_provider_versions(owner_id=1, days_back=14)

    by_key = {(item["provider"], item["provider_version"]): item for item in summary["versions"]}
    # Ordered by provider, then most recent activity first: 2.1.7 was active
    # an hour ago, 2.1.0 two hours ago.
    assert [(item["provider"], item["provider_version"]) for item in summary["versions"]] == [
        ("claude", "2.1.7"),
        ("claude", "2.1.0"),
        ("codex", "0.145.0"),
    ]
    old_claude = by_key[("claude", "2.1.0")]
    assert old_claude["sessions"] == 3
    assert old_claude["devices"] == 2
    assert old_claude["first_seen"] == first.isoformat()
    assert datetime.fromisoformat(old_claude["last_seen"]) == now - timedelta(hours=2) + timedelta(minutes=5)
    assert by_key[("codex", "0.145.0")]["devices"] == 1


def test_summary_excludes_old_null_test_other_owner_sessions(seeded_engine):
    now = datetime.now(UTC)
    with seeded_engine.begin() as connection:
        _seed(connection, provider="claude", version="2.1.0", device="cinder", started_at=now - timedelta(days=20))
        _seed(connection, provider="claude", version="2.1.0", device="cinder", started_at=now - timedelta(hours=1), env="test")
        _seed(connection, provider="claude", version="2.1.0", device="cinder", started_at=now - timedelta(hours=1), env="e2e")
        _seed(connection, provider="claude", version=None, device="cinder", started_at=now - timedelta(hours=1))
        _seed(connection, owner="2", provider="claude", version="9.9.9", device="cinder", started_at=now - timedelta(hours=1))
        _seed(connection, provider="claude", version="2.1.1", device="cinder", started_at=now - timedelta(days=6))

    store = CatalogStore(seeded_engine)
    seven_day = store.summarize_provider_versions(owner_id=1, days_back=7)
    assert [(item["provider_version"], item["sessions"]) for item in seven_day["versions"]] == [("2.1.1", 1)]
    assert store.summarize_provider_versions(owner_id=1, days_back=14)["versions"][0]["provider_version"] == "2.1.1"
    assert store.summarize_provider_versions(owner_id=3, days_back=90)["versions"] == []


# --- route ----------------------------------------------------------------


def test_field_versions_route_shapes_the_owner_summary(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)
    calls = []

    def fake_summary(*, owner_id: int, days_back: int):
        calls.append((owner_id, days_back))
        return {
            "observed_at": "ignored",
            "window_days": days_back,
            "commit_seq": "7",
            "versions": [
                {
                    "provider": "claude",
                    "provider_version": "2.1.0",
                    "sessions": 3,
                    "devices": 2,
                    "first_seen": "2026-10-01T00:00:00+00:00",
                    "last_seen": "2026-10-08T00:00:00+00:00",
                }
            ],
        }

    monkeypatch.setattr(routes, "provider_version_summary", fake_summary)
    try:
        default = client.get(URL)
        bounded = client.get(URL, params={"days": 90})
        invalid_low = client.get(URL, params={"days": 0})
        invalid_high = client.get(URL, params={"days": 91})
    finally:
        api_app.dependency_overrides.clear()

    assert default.status_code == 200
    payload = default.json()
    assert set(payload) == {"generated_at", "window_days", "versions"}
    assert payload["window_days"] == 14
    assert payload["versions"] == [
        {
            "provider": "claude",
            "provider_version": "2.1.0",
            "sessions": 3,
            "devices": 2,
            "first_seen": "2026-10-01T00:00:00+00:00",
            "last_seen": "2026-10-08T00:00:00+00:00",
        }
    ]
    assert bounded.status_code == 200
    assert bounded.json()["window_days"] == 90
    assert invalid_low.status_code == 422
    assert invalid_high.status_code == 422
    assert calls == [(1, 14), (1, 90)]


def test_field_versions_route_requires_agents_auth(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)

    def reject_machine():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing machine token")

    api_app.dependency_overrides[verify_agents_token] = reject_machine
    try:
        response = client.get(URL)
    finally:
        api_app.dependency_overrides.clear()

    assert response.status_code == 401


def test_field_versions_route_refuses_a_managed_session_token(monkeypatch, tmp_path: Path) -> None:
    """Owner-wide field versions follow the evidence guard: a scoped session token cannot read them."""
    client = _client(monkeypatch, tmp_path)
    api_app.dependency_overrides[verify_agents_caller] = lambda: Caller(
        owner_id=1, principal=ManagedSessionToken(owner_id=1, session_id="session", scope="hook")
    )
    try:
        response = client.get(URL)
    finally:
        api_app.dependency_overrides.clear()

    assert response.status_code == 403


def test_field_versions_route_is_refused_on_public_demo(monkeypatch, tmp_path: Path) -> None:
    from types import SimpleNamespace

    client = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(routes, "get_settings", lambda: SimpleNamespace(demo_mode=True, provider_capability_factory_token=None))
    try:
        response = client.get(URL)
    finally:
        api_app.dependency_overrides.clear()

    assert response.status_code == 404
