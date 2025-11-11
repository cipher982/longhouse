from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from zerg.crud import crud
from zerg.utils.time import utc_now_naive


def _create_finished_run(db_session: Session, agent_id: int, thread_id: int, *, tokens: int, cost: float):
    run_row = crud.create_run(
        db_session,
        agent_id=agent_id,
        thread_id=thread_id,
        trigger="manual",
        status="queued",
    )
    crud.mark_running(db_session, run_row.id, started_at=utc_now_naive() - timedelta(seconds=5))
    crud.mark_finished(
        db_session,
        run_row.id,
        finished_at=utc_now_naive(),
        total_tokens=tokens,
        total_cost_usd=cost,
        duration_ms=5000,
    )
    return run_row


def test_dashboard_snapshot_includes_agents_and_runs(
    client: TestClient,
    db_session: Session,
    sample_agent,
    sample_thread,
):
    run = _create_finished_run(db_session, sample_agent.id, sample_thread.id, tokens=321, cost=0.42)

    response = client.get("/api/agents/dashboard?runs_limit=5")
    assert response.status_code == 200

    payload = response.json()
    assert payload["scope"] == "my"
    assert payload["runs_limit"] == 5
    assert "fetched_at" in payload

    agent_ids = [agent["id"] for agent in payload["agents"]]
    assert sample_agent.id in agent_ids

    bundle = next(item for item in payload["runs"] if item["agent_id"] == sample_agent.id)
    assert bundle["runs"], "Dashboard bundle should contain at least one run"
    run_payload = bundle["runs"][0]
    assert run_payload["id"] == run.id
    assert run_payload["status"] == "success"
    assert run_payload["total_tokens"] == 321


def test_dashboard_snapshot_honors_runs_limit(
    client: TestClient,
    db_session: Session,
    sample_agent,
    sample_thread,
):
    for idx in range(6):
        _create_finished_run(
            db_session,
            sample_agent.id,
            sample_thread.id,
            tokens=idx + 1,
            cost=0.1 * idx,
        )

    response = client.get("/api/agents/dashboard?runs_limit=3")
    assert response.status_code == 200

    data = response.json()
    bundle = next(item for item in data["runs"] if item["agent_id"] == sample_agent.id)
    assert len(bundle["runs"]) == 3


def test_dashboard_snapshot_zero_limit_returns_empty_runs(
    client: TestClient,
    sample_agent,
):
    response = client.get("/api/agents/dashboard?runs_limit=0")
    assert response.status_code == 200

    data = response.json()
    assert all(not item["runs"] for item in data["runs"])
