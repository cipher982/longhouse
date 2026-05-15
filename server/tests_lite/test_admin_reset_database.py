import os
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.auth import get_current_user
from zerg.dependencies.auth import require_admin
from zerg.main import api_app
from zerg.models import User
from zerg.models.agents import AgentSession
from zerg.models.models import Runner


def _make_db(tmp_path, name="admin_reset.db"):
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _client(factory):
    def override_db():
        with factory() as db:
            yield db

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=1,
        email="owner@example.com",
        role="ADMIN",
    )
    api_app.dependency_overrides[require_admin] = lambda: None
    return TestClient(api_app)


def test_clear_data_reset_clears_agent_sessions_but_preserves_runners(tmp_path):
    factory = _make_db(tmp_path)
    with factory() as db:
        db.add(User(id=1, email="owner@example.com", role="ADMIN"))
        db.add(
            Runner(
                owner_id=1,
                name="cinder",
                auth_secret_hash="hash",
                status="online",
            )
        )
        db.add(
            AgentSession(
                provider="claude",
                environment="development",
                project="zerg",
                device_id="cinder",
                started_at=datetime.now(timezone.utc),
                ended_at=datetime.now(timezone.utc),
                user_messages=1,
                assistant_messages=1,
                tool_calls=0,
            )
        )
        db.commit()

        assert db.query(Runner).count() == 1
        assert db.query(AgentSession).count() == 1

    client = _client(factory)
    try:
        with patch("zerg.routers.admin.get_session_factory", return_value=factory):
            response = client.post("/admin/reset-database", json={"reset_type": "clear_data"})
            assert response.status_code == 200

        with factory() as db:
            assert db.query(Runner).count() == 1
            assert db.query(AgentSession).count() == 0
    finally:
        api_app.dependency_overrides.clear()
