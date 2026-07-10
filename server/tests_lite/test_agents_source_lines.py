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
from zerg.models.agents import AgentSessionBranch
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


def test_source_line_claims_require_recoverable_raw_bytes(tmp_path, monkeypatch):
    factory, cleanup = _setup_app(tmp_path)
    client = TestClient(api_app)
    session_id = uuid4()
    source_path = "/tmp/opencode.db#opencode:ses_test"
    archived_raw = '{"part":"archived"}'
    archived_hash = hashlib.sha256(archived_raw.encode()).hexdigest()
    lost_hash = hashlib.sha256(b'{"part":"lost"}').hexdigest()

    try:
        with factory() as db:
            db.add(
                AgentSession(
                    id=session_id,
                    provider="opencode",
                    environment="test",
                    started_at=datetime.now(timezone.utc),
                )
            )
            for source_offset, line_hash in ((10, archived_hash), (20, lost_hash)):
                db.add(
                    AgentSourceLine(
                        session_id=session_id,
                        source_path=source_path,
                        source_offset=source_offset,
                        branch_id=1,
                        raw_json="",
                        raw_json_z=None,
                        raw_json_codec=0,
                        line_hash=line_hash,
                    )
                )
            db.commit()

        monkeypatch.setattr(
            "zerg.routers.agents_source_lines.load_session_source_line_bytes",
            lambda db, requested_session_id: {
                (source_path, 10, archived_hash): archived_raw,
            },
        )
        monkeypatch.setattr(
            "zerg.routers.agents_source_lines.AgentsStore.export_session_jsonl",
            lambda self, requested_session_id, *, branch_mode: (archived_raw.encode(), object()),
        )
        response = client.post(
            "/agents/source-lines/claims",
            json={
                "items": [
                    {
                        "session_id": str(session_id),
                        "source_path": source_path,
                        "source_offset": 10,
                        "line_hash": archived_hash,
                    },
                    {
                        "session_id": str(session_id),
                        "source_path": source_path,
                        "source_offset": 20,
                        "line_hash": lost_hash,
                    },
                ]
            },
        )

        assert response.status_code == 200, response.text
        assert response.json() == {
            "present": [
                {
                    "source_path": source_path,
                    "source_offset": 10,
                    "line_hash": archived_hash,
                }
            ],
            "missing": [
                {
                    "source_path": source_path,
                    "source_offset": 20,
                    "line_hash": lost_hash,
                }
            ],
            "rejected": [],
        }
    finally:
        cleanup()


def test_source_line_claims_require_complete_head_export(tmp_path, monkeypatch):
    from zerg.services.archive_transcript import ArchiveTranscriptUnavailable

    factory, cleanup = _setup_app(tmp_path)
    client = TestClient(api_app)
    session_id = uuid4()
    source_path = "/tmp/opencode.db#opencode:ses_export"
    raw_line = '{"part":"present-row"}'
    line_hash = hashlib.sha256(raw_line.encode()).hexdigest()

    try:
        with factory() as db:
            db.add(
                AgentSession(
                    id=session_id,
                    provider="opencode",
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

        def fail_export(self, requested_session_id, *, branch_mode):  # noqa: ARG001
            raise ArchiveTranscriptUnavailable("another selected row has no bytes")

        monkeypatch.setattr(
            "zerg.routers.agents_source_lines.AgentsStore.export_session_jsonl",
            fail_export,
        )
        response = client.post(
            "/agents/source-lines/claims",
            json={
                "items": [
                    {
                        "session_id": str(session_id),
                        "source_path": source_path,
                        "source_offset": 10,
                        "line_hash": line_hash,
                    }
                ]
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["present"] == []
        assert response.json()["missing"] == [
            {
                "source_path": source_path,
                "source_offset": 10,
                "line_hash": line_hash,
            }
        ]
    finally:
        cleanup()


def test_source_line_claims_do_not_use_inline_bytes_from_non_head_branch(tmp_path):
    factory, cleanup = _setup_app(tmp_path)
    client = TestClient(api_app)
    session_id = uuid4()
    source_path = "/tmp/opencode.db#opencode:ses_branch"
    raw_line = '{"part":"same-identity"}'
    line_hash = hashlib.sha256(raw_line.encode()).hexdigest()

    try:
        with factory() as db:
            db.add(
                AgentSession(
                    id=session_id,
                    provider="opencode",
                    environment="test",
                    started_at=datetime.now(timezone.utc),
                )
            )
            db.flush()
            stale_branch = AgentSessionBranch(session_id=session_id, branch_reason="root", is_head=0)
            head_branch = AgentSessionBranch(session_id=session_id, branch_reason="rewind", is_head=1)
            db.add_all([stale_branch, head_branch])
            db.flush()
            db.add_all(
                [
                    AgentSourceLine(
                        session_id=session_id,
                        source_path=source_path,
                        source_offset=10,
                        branch_id=stale_branch.id,
                        raw_json=raw_line,
                        line_hash=line_hash,
                    ),
                    AgentSourceLine(
                        session_id=session_id,
                        source_path=source_path,
                        source_offset=10,
                        branch_id=head_branch.id,
                        raw_json="",
                        raw_json_z=None,
                        raw_json_codec=0,
                        line_hash=line_hash,
                    ),
                ]
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
                    }
                ]
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["present"] == []
        assert response.json()["missing"] == [
            {
                "source_path": source_path,
                "source_offset": 10,
                "line_hash": line_hash,
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
