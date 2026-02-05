"""API tests for agents session search (FTS-backed)."""

from tests.helpers.agents_seed import seed_agent_session


def test_agents_sessions_query_returns_matches(client, db_session):
    seed_agent_session(
        db_session,
        project="api-search",
        user_text="api search needle",
        tool_output_text="grep output",
        include_assistant=False,
    )

    response = client.get("/api/agents/sessions", params={"query": "needle", "include_test": True})
    assert response.status_code == 200

    data = response.json()
    assert data["total"] >= 1
    assert data["sessions"]

    match = data["sessions"][0]
    assert match.get("match_event_id") is not None
    assert "needle" in (match.get("match_snippet") or "").lower()
