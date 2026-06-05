from __future__ import annotations

import os
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import text

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.main import api_app


def test_runtime_batch_releases_request_db_before_serialized_write(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path}/runtime_release.db", pool_size=1, max_overflow=0)
    Base.metadata.create_all(bind=engine)
    factory = make_sessionmaker(engine)
    observations: dict[str, int] = {}

    class ReleaseCheckingSerializer:
        is_configured = True

        async def execute_after_closing_request_session(self, fn, fallback_db, **_kwargs):
            observations["before_close"] = engine.pool.checkedout()
            fallback_db.close()
            observations["after_close"] = engine.pool.checkedout()
            with factory() as write_db:
                result = fn(write_db)
                write_db.commit()
                return result

        async def execute_or_direct(self, *_args, **_kwargs):  # pragma: no cover - regression guard
            raise AssertionError("runtime batch must release the request DB before waiting on serialized writes")

    def override_db():
        db = factory()
        try:
            db.execute(text("SELECT 1"))
            yield db
        finally:
            db.close()

    def override_verify_agents_token():
        return SimpleNamespace(device_id="runtime-release", id="token-1", owner_id=1)

    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.setattr("zerg.routers.runtime.get_write_serializer", lambda: ReleaseCheckingSerializer())
    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    try:
        with TestClient(api_app) as client:
            response = client.post(
                "/agents/runtime/events/batch",
                json={
                    "events": [
                        {
                            "runtime_key": "codex:runtime-release",
                            "provider": "codex",
                            "device_id": "runtime-release",
                            "source": "codex_bridge",
                            "kind": "phase_signal",
                            "phase": "idle",
                            "occurred_at": "2026-01-01T00:00:00Z",
                            "freshness_ms": 60000,
                            "dedupe_key": "runtime-release-1",
                            "payload": {},
                        }
                    ]
                },
                headers={"X-Agents-Token": "dev"},
            )
        assert response.status_code == 200, response.text
    finally:
        api_app.dependency_overrides.clear()
        engine.dispose()

    assert observations == {"before_close": 1, "after_close": 0}
