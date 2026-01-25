"""Integration tests for live worker output flow.

Tests the full path from runner exec_chunk (WebSocket) -> OutputBuffer -> EventBus (SSE).
"""

import asyncio
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from zerg.crud import runner_crud
from zerg.events import EventType
from zerg.events.event_bus import event_bus
from zerg.models.models import Runner
from zerg.models.models import User
from zerg.models.models import WorkerJob
from zerg.services.worker_output_buffer import get_worker_output_buffer
from zerg.tools.builtin.supervisor_tools import peek_worker_output


@pytest.fixture
def test_runner_and_worker(db: Session, test_user: User) -> tuple[Runner, str, WorkerJob]:
    """Create a test runner and a worker job linked to it."""
    secret = runner_crud.generate_token()
    runner = runner_crud.create_runner(
        db=db,
        owner_id=test_user.id,
        name="live-test-runner",
        auth_secret=secret,
    )

    worker_id = "test-worker-live-123"
    worker_job = WorkerJob(
        owner_id=test_user.id,
        task="Test live output",
        status="running",
        worker_id=worker_id,
    )
    db.add(worker_job)
    db.commit()
    db.refresh(worker_job)

    return runner, secret, worker_job


@pytest.mark.asyncio
async def test_live_output_flow_ws_to_sse(client: TestClient, db: Session, test_runner_and_worker):
    """Test that runner exec_chunk updates buffer and publishes SSE."""
    runner, secret, worker_job = test_runner_and_worker
    worker_id = worker_job.worker_id

    # Create a runner job linked to the worker
    runner_job = runner_crud.create_runner_job(
        db=db,
        runner_id=runner.id,
        owner_id=runner.owner_id,
        worker_id=worker_id,
        run_id="1",
        command="echo hello",
        timeout_secs=60,
    )

    received_sse = []

    async def sse_listener(payload):
        received_sse.append(payload)

    event_bus.subscribe(EventType.WORKER_OUTPUT_CHUNK, sse_listener)

    try:
        with client.websocket_connect("/api/runners/ws") as ws:
            # 1. Hello
            ws.send_json({
                "type": "hello",
                "runner_id": runner.id,
                "secret": secret,
            })
            await asyncio.sleep(0.1)

            # 2. Send chunk
            chunk_data = "hello world from runner"
            ws.send_json({
                "type": "exec_chunk",
                "job_id": str(runner_job.id),
                "stream": "stdout",
                "data": chunk_data,
            })
            await asyncio.sleep(0.2)

            # 3. Verify buffer
            buffer = get_worker_output_buffer()
            tail = buffer.get_tail(worker_id)
            assert chunk_data in tail

            # 4. Verify SSE event
            assert len(received_sse) == 1
            payload = received_sse[0]
            assert payload["worker_id"] == worker_id
            assert payload["job_id"] == worker_job.id
            assert payload["data"] == chunk_data
            assert payload["stream"] == "stdout"
            assert payload["run_id"] == 1

            # 5. Verify peek tool works (using the job ID)
            # Need to mock credential context for peek_worker_output
            from zerg.connectors.resolver import CredentialResolver
            from zerg.connectors.context import set_credential_resolver, reset_credential_resolver

            resolver = CredentialResolver(agent_id=1, db=db, owner_id=runner.owner_id)
            token = set_credential_resolver(resolver)

            try:
                peek_result = peek_worker_output(str(worker_job.id))
                assert "Live worker output" in peek_result
                assert chunk_data in peek_result
            finally:
                reset_credential_resolver(token)

    finally:
        event_bus.unsubscribe(EventType.WORKER_OUTPUT_CHUNK, sse_listener)


@pytest.mark.asyncio
async def test_live_output_chunk_truncation_sse(client: TestClient, db: Session, test_runner_and_worker):
    """Test that massive chunks are truncated in the SSE payload but fully added to buffer."""
    runner, secret, worker_job = test_runner_and_worker
    worker_id = worker_job.worker_id

    runner_job = runner_crud.create_runner_job(
        db=db,
        runner_id=runner.id,
        owner_id=runner.owner_id,
        worker_id=worker_id,
        run_id="1",
        command="large output",
        timeout_secs=60,
    )

    received_sse = []
    async def sse_listener(payload):
        received_sse.append(payload)

    event_bus.subscribe(EventType.WORKER_OUTPUT_CHUNK, sse_listener)

    try:
        with client.websocket_connect("/api/runners/ws") as ws:
            ws.send_json({"type": "hello", "runner_id": runner.id, "secret": secret})
            await asyncio.sleep(0.1)

            # Send 5000 chars (over 4000 limit)
            large_data = "A" * 5000
            ws.send_json({
                "type": "exec_chunk",
                "job_id": str(runner_job.id),
                "stream": "stdout",
                "data": large_data,
            })
            await asyncio.sleep(0.2)

            # Buffer should have full 5000
            buffer = get_worker_output_buffer()
            tail = buffer.get_tail(worker_id)
            assert len(tail) >= 5000

            # SSE should have truncated 4000 (tail of it)
            assert len(received_sse) == 1
            payload = received_sse[0]
            assert len(payload["data"]) == 4000
            assert payload["data"] == "A" * 4000

    finally:
        event_bus.unsubscribe(EventType.WORKER_OUTPUT_CHUNK, sse_listener)
