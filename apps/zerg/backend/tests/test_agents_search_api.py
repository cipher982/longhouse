"""API tests for agents session search (FTS-backed)."""

from datetime import datetime
from datetime import timezone
from uuid import uuid4

from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest


def test_agents_sessions_query_returns_matches(client, db_session):
    store = AgentsStore(db_session)
    session_id = uuid4()
    timestamp = datetime(2026, 2, 5, tzinfo=timezone.utc)

    store.ingest_session(
        SessionIngest(
            id=session_id,
            provider="claude",
            environment="test",
            project="api-search",
            device_id="dev-machine",
            cwd="/tmp",
            git_repo=None,
            git_branch=None,
            started_at=timestamp,
            events=[
                EventIngest(
                    role="user",
                    content_text="api search needle",
                    timestamp=timestamp,
                    source_path="/tmp/session.jsonl",
                    source_offset=0,
                ),
                EventIngest(
                    role="tool",
                    tool_name="Bash",
                    tool_output_text="grep output",
                    timestamp=timestamp,
                    source_path="/tmp/session.jsonl",
                    source_offset=1,
                ),
            ],
        )
    )

    response = client.get("/api/agents/sessions", params={"query": "needle", "include_test": True})
    assert response.status_code == 200

    data = response.json()
    assert data["total"] >= 1
    assert data["sessions"]

    match = data["sessions"][0]
    assert match.get("match_event_id") is not None
    assert "needle" in (match.get("match_snippet") or "").lower()
