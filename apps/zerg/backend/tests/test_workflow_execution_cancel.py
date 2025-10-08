"""Tests for the /workflow-executions/{id}/cancel endpoint."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from zerg.models.models import Workflow
from zerg.services.workflow_engine import workflow_engine as workflow_execution_engine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_workflow(db, *, name: str):
    wf = Workflow(owner_id=1, name=name, canvas={"nodes": [], "edges": []})
    db.add(wf)
    db.commit()
    db.refresh(wf)
    return wf


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_cancel_endpoint_sets_status(client: TestClient, db_session):
    wf = _insert_workflow(db_session, name="wf-cancel")

    # Start execution (async but returns immediately after run due to empty nodes)
    execution_id = asyncio.run(workflow_execution_engine.execute_workflow(wf.id))

    # Cancel should return 409 because already finished
    resp = client.patch(f"/api/workflow-executions/{execution_id}/cancel", json={"reason": "user pressed stop"})
    assert resp.status_code == 409


@pytest.mark.skip(reason="Cancellation test needs rewrite for LangGraph engine")
def test_cancel_running_execution(client: TestClient, db_session, monkeypatch):
    """Cancel while running – simulate long node to allow cancel."""

    # Create workflow with one node that sleeps long so we can cancel
    wf = _insert_workflow(db_session, name="wf-long")
    wf.canvas = {
        "nodes": [{"id": "slow", "type": "dummy", "simulate_failures": 0}],
    }
    db_session.commit()

    # Monkeypatch placeholder execute to sleep longer
    from zerg.services import langgraph_workflow_engine as _we_mod

    def _slow_execute(node_type, node_payload):
        import time

        time.sleep(0.2)

    monkeypatch.setattr(_we_mod.LangGraphWorkflowEngine, "_execute_placeholder_node", staticmethod(_slow_execute))

    async def _run():
        return await workflow_execution_engine.execute_workflow(wf.id)

    # Run execution in a separate task
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    exec_future = loop.create_task(_run())

    loop.run_until_complete(asyncio.sleep(0.05))

    # Cancel via API
    # Wait until execution row exists
    import time as _time

    exec_row_id = None
    for _ in range(10):
        from zerg.models.models import WorkflowExecution

        db_session.expire_all()
        row = (
            db_session.query(WorkflowExecution)
            .filter(WorkflowExecution.workflow_id == wf.id)
            .order_by(WorkflowExecution.id.desc())
            .first()
        )
        if row:
            exec_row_id = row.id
            break
        _time.sleep(0.02)

    assert exec_row_id is not None, "execution row not created in time"

    resp = client.patch(f"/api/workflow-executions/{exec_row_id}/cancel", json={"reason": "stop"})
    assert resp.status_code == 204

    # Wait for execution task to finish
    loop.run_until_complete(exec_future)
    loop.close()

    from zerg.models.models import WorkflowExecution

    db_session.expire_all()
    execution = db_session.query(WorkflowExecution).get(exec_row_id)
    assert execution.phase == "finished"
    assert execution.result == "cancelled"
