"""Ensure the Future/InvalidUpdateError regression is fixed.

The test recreates the *exact* REST flow the frontend performs:

1. Create an fiche
2. Create a thread for that fiche
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

    # 1. Fiche
    fiche_resp = client.post(
        "/api/fiches",
        json={
            "name": "Future Regression Fiche",
            "system_instructions": "You are helpful",
            "task_instructions": "Assist the user",
            "model": "gpt-mock",
        },
    )
    assert fiche_resp.status_code == 201, fiche_resp.text
    fiche_id = fiche_resp.json()["id"]

    # 2. Thread
    thread_resp = client.post(
        "/api/threads",
        json={"title": "Future-bug thread", "fiche_id": fiche_id},
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
