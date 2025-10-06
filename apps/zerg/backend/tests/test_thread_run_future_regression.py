"""Ensure the Future/InvalidUpdateError regression is fixed.

The test recreates the *exact* REST flow the frontend performs:

1. Create an agent
2. Create a thread for that agent
3. POST a *user* message (unprocessed)
4. POST /threads/{id}/run – should return **202 Accepted** and *not* raise
   ``InvalidUpdateError`` internally.

If the underlying bug resurfaces (e.g. ChatOpenAI invokes return a Future that
isn't unwrapped) the request will bubble up as HTTP 500 and the test will
fail.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_thread_run_returns_202(client: TestClient):
    """Full e2e happy-path – endpoint responds 202 (no exception)."""

    # 1. Agent
    agent_resp = client.post(
        "/api/agents",
        json={
            "name": "Future Regression Agent",
            "system_instructions": "You are helpful",
            "task_instructions": "Assist the user",
            "model": "gpt-mock",
        },
    )
    assert agent_resp.status_code == 201, agent_resp.text
    agent_id = agent_resp.json()["id"]

    # 2. Thread
    thread_resp = client.post(
        "/api/threads",
        json={"title": "Future-bug thread", "agent_id": agent_id},
    )
    assert thread_resp.status_code == 201, thread_resp.text
    thread_id = thread_resp.json()["id"]

    # 3. User message
    msg_resp = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"role": "user", "content": "hello"},
    )
    assert msg_resp.status_code == 201, msg_resp.text

    # 4. Run – should now succeed (202) after bug-fix.
    run_resp = client.post(f"/api/threads/{thread_id}/run")
    assert run_resp.status_code == 202, run_resp.text
