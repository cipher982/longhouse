"""Integration test: supervisor → spawn_worker → worker_complete → continuation → final supervisor response.

This covers the 0→1 "master/worker" flow used by Jarvis chat:
- Supervisor can acknowledge and spawn a worker (intermediate message)
- Original run is marked DEFERRED so worker completion triggers a continuation
- Continuation run's supervisor_complete is delivered on the original SSE stream

This test is intentionally "thin" (no real LLM/tool execution) but exercises the
real plumbing: run status transitions, event emission, and SSE filtering.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from tests.conftest import TEST_WORKER_MODEL
from zerg.connectors.context import set_credential_resolver
from zerg.connectors.resolver import CredentialResolver
from zerg.models.enums import RunStatus
from zerg.models.enums import RunTrigger
from zerg.models.models import AgentRun
from zerg.models.models import WorkerJob
from zerg.routers.jarvis_sse import stream_run_events
from zerg.services.event_store import emit_run_event
from zerg.services.supervisor_service import SupervisorService
from zerg.services.worker_runner import WorkerRunner


@pytest.fixture
def temp_artifact_path(monkeypatch):
    """Create temporary artifact store path and set environment variable."""
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setenv("SWARMLET_DATA_PATH", tmpdir)
        yield tmpdir


@pytest.fixture
def credential_context(db_session, test_user):
    """Set up credential resolver context for tools."""
    resolver = CredentialResolver(agent_id=1, db=db_session, owner_id=test_user.id)
    token = set_credential_resolver(resolver)
    yield resolver
    set_credential_resolver(None)


@pytest.mark.asyncio
@pytest.mark.timeout(15)
async def test_supervisor_worker_continuation_delivered_on_original_stream(
    db_session,
    test_user,
    credential_context,  # noqa: ARG001 - fixture activates resolver context
    temp_artifact_path,  # noqa: ARG001 - ensures artifact store is writable if used
):
    service = SupervisorService(db_session)
    agent = service.get_or_create_supervisor_agent(test_user.id)
    thread = service.get_or_create_supervisor_thread(test_user.id, agent)

    # Create a run record the same way /api/jarvis/chat does (run_id known before streaming).
    run = AgentRun(
        agent_id=agent.id,
        thread_id=thread.id,
        status=RunStatus.RUNNING,
        trigger=RunTrigger.API,
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    # Start consuming SSE stream BEFORE running the supervisor to avoid missing early events.
    events: list[dict] = []

    async def consume_stream() -> None:
        async for evt in stream_run_events(run.id, test_user.id):
            events.append(evt)
            if evt.get("event") == "supervisor_complete":
                break

    consumer_task = asyncio.create_task(consume_stream())

    call_count = 0

    async def fake_run_thread(_self, _db, _thread):  # noqa: ANN001 - signature matches patched method
        nonlocal call_count
        call_count += 1

        if call_count == 1:
            # Simulate supervisor tool use by directly calling the real spawn_worker tool.
            from zerg.tools.builtin.supervisor_tools import spawn_worker_async

            await spawn_worker_async(task="Check disk space on cube", model=TEST_WORKER_MODEL)
            msg = AsyncMock()
            msg.role = "assistant"
            msg.content = "Delegating this to a worker now to check cube's disk usage."
            return [msg]

        # Continuation synthesis run
        msg = AsyncMock()
        msg.role = "assistant"
        msg.content = "Cube is at 45% disk usage; biggest usage is Docker images/volumes."
        return [msg]

    # Patch must remain active for BOTH the initial supervisor run and the continuation run,
    # since continuations execute in a background task with a fresh DB session.
    with patch("zerg.managers.agent_runner.AgentRunner.run_thread", new=fake_run_thread):
        # Run supervisor; this should now return DEFERRED (waiting_for_worker), not SUCCESS.
        result = await service.run_supervisor(
            owner_id=test_user.id,
            task="can you check disk space on cube",
            run_id=run.id,
            timeout=30,
            return_on_deferred=True,
        )
        assert result.status == "deferred"

        # Verify original run is DEFERRED (this is the regression guard).
        db_session.refresh(run)
        assert run.status == RunStatus.DEFERRED

        # The worker should have been queued and correlated to this run_id.
        job = (
            db_session.query(WorkerJob)
            .filter(WorkerJob.owner_id == test_user.id, WorkerJob.supervisor_run_id == run.id)
            .order_by(WorkerJob.id.desc())
            .first()
        )
        assert job is not None

        # Simulate worker completion events (normally emitted by WorkerRunner.run_worker).
        await emit_run_event(
            db=db_session,
            run_id=run.id,
            event_type="worker_complete",
            payload={
                "job_id": job.id,
                "worker_id": "test-worker-1",
                "status": "success",
                "duration_ms": 1234,
                "owner_id": test_user.id,
            },
        )

        await emit_run_event(
            db=db_session,
            run_id=run.id,
            event_type="worker_summary_ready",
            payload={
                "job_id": job.id,
                "worker_id": "test-worker-1",
                "summary": "Cube at 45% disk; Docker is largest.",
                "owner_id": test_user.id,
            },
        )

        # Trigger continuation (normally invoked automatically inside WorkerRunner.run_worker).
        runner = WorkerRunner()
        await runner._trigger_continuation_if_deferred(  # noqa: SLF001 - integration test needs full plumbing coverage
            db=db_session,
            run_id=run.id,
            job_id=job.id,
            worker_id="test-worker-1",
            status="success",
            result_summary="Cube at 45% disk; Docker is largest.",
        )

        # Wait for the stream to receive the final supervisor_complete (from the continuation run).
        await asyncio.wait_for(consumer_task, timeout=10)

    # Parse and assert key events occurred and stream did not close on supervisor_deferred.
    parsed = []
    for evt in events:
        try:
            data = json.loads(evt.get("data") or "{}")
        except json.JSONDecodeError:
            continue
        parsed.append((evt.get("event"), data.get("payload") or {}, data))

    deferred_payload = None
    for event_name, payload, _wrapper in parsed:
        if event_name == "supervisor_deferred":
            deferred_payload = payload
            break
    assert deferred_payload is not None
    assert deferred_payload.get("close_stream") is False
    assert deferred_payload.get("reason") == "waiting_for_worker"

    # Final result should arrive on the ORIGINAL run stream (events are aliased back to run.id).
    complete_payload = None
    for event_name, payload, _wrapper in parsed:
        if event_name == "supervisor_complete":
            complete_payload = payload
            break
    assert complete_payload is not None
    assert "45% disk usage" in (complete_payload.get("result") or "")
