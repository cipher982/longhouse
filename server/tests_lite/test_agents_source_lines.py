"""Tests for source-line reconciliation claims."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime
from datetime import timezone
from uuid import uuid4

from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.main import api_app
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentSourceLine


def _setup_app(tmp_path):
    db_path = tmp_path / "test_agents_source_lines.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    factory = make_sessionmaker(engine)

    def _override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    api_app.dependency_overrides[get_db] = _override_db
    api_app.dependency_overrides[verify_agents_token] = lambda: None
    api_app.dependency_overrides[require_single_tenant] = lambda: None

    def _cleanup():
        api_app.dependency_overrides.pop(get_db, None)
        api_app.dependency_overrides.pop(verify_agents_token, None)
        api_app.dependency_overrides.pop(require_single_tenant, None)

    return factory, _cleanup


def test_source_line_claims_split_present_missing_and_rejected(tmp_path):
    factory, cleanup = _setup_app(tmp_path)
    client = TestClient(api_app)
    session_id = uuid4()
    source_path = "/tmp/session.jsonl"
    raw_line = '{"type":"message","content":"already durable"}'
    line_hash = hashlib.sha256(raw_line.encode()).hexdigest()
    missing_hash = hashlib.sha256(b"missing").hexdigest()

    try:
        with factory() as db:
            db.add(
                AgentSession(
                    id=session_id,
                    provider="codex",
                    environment="test",
                    started_at=datetime.now(timezone.utc),
                )
            )
            db.add(
                AgentSourceLine(
                    session_id=session_id,
                    source_path=source_path,
                    source_offset=10,
                    branch_id=1,
                    raw_json=raw_line,
                    line_hash=line_hash,
                )
            )
            db.commit()

        response = client.post(
            "/agents/source-lines/claims",
            json={
                "items": [
                    {
                        "session_id": str(session_id),
                        "source_path": source_path,
                        "source_offset": 10,
                        "line_hash": line_hash,
                    },
                    {
                        "session_id": str(session_id),
                        "source_path": source_path,
                        "source_offset": 20,
                        "line_hash": missing_hash,
                    },
                    {
                        "session_id": str(session_id),
                        "source_path": source_path,
                        "source_offset": 30,
                        "line_hash": "not-a-sha",
                    },
                ]
            },
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["present"] == [
            {
                "source_path": source_path,
                "source_offset": 10,
                "line_hash": line_hash,
            }
        ]
        assert body["missing"] == [
            {
                "source_path": source_path,
                "source_offset": 20,
                "line_hash": missing_hash,
            }
        ]
        assert body["rejected"] == [
            {
                "source_path": source_path,
                "source_offset": 30,
                "line_hash": "not-a-sha",
                "reason": "invalid_line_hash",
            }
        ]
    finally:
        cleanup()


def test_source_line_claims_reject_too_many_items(tmp_path):
    _factory, cleanup = _setup_app(tmp_path)
    client = TestClient(api_app)
    session_id = uuid4()
    line_hash = hashlib.sha256(b"line").hexdigest()

    try:
        response = client.post(
            "/agents/source-lines/claims",
            json={
                "items": [
                    {
                        "session_id": str(session_id),
                        "source_path": "/tmp/session.jsonl",
                        "source_offset": i,
                        "line_hash": line_hash,
                    }
                    for i in range(513)
                ]
            },
        )
        assert response.status_code == 400, response.text
        assert response.json()["detail"] == "too many source-line claim items"
    finally:
        cleanup()
