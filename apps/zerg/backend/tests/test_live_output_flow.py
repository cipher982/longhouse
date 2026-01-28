"""Integration tests for live commis output flow.

Tests the full path from runner exec_chunk (WebSocket) -> OutputBuffer -> EventBus (SSE).
"""

import asyncio
import time
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from zerg.crud import runner_crud
from zerg.events import EventType
from zerg.events.event_bus import event_bus
from zerg.models.models import Runner
from zerg.models.models import User
from zerg.models.models import CommisJob
from zerg.services.commis_output_buffer import get_commis_output_buffer
from zerg.tools.builtin.oikos_tools import peek_commis_output


async def _wait_for(predicate, *, timeout: float = 2.0, interval: float = 0.05) -> None:
    """Wait for a predicate to become true within a timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("Timed out waiting for condition")


@pytest.fixture
def test_runner_and_commis(db: Session, test_user: User) -> tuple[Runner, str, CommisJob]:
    """Create a test runner and a commis job linked to it."""
    secret = runner_crud.generate_token()
    runner = runner_crud.create_runner(
        db=db,
        owner_id=test_user.id,
        name="live-test-runner",
        auth_secret=secret,
    )

    commis_id = f"test-commis-live-{uuid.uuid4().hex}"
    commis_job = CommisJob(
        owner_id=test_user.id,
        task="Test live output",
        status="running",
        commis_id=commis_id,
    )
    db.add(commis_job)
    db.commit()
    db.refresh(commis_job)

    return runner, secret, commis_job


@pytest.mark.asyncio
async def test_live_output_flow_ws_to_sse(client: TestClient, db: Session, test_runner_and_commis):
    """Test that runner exec_chunk updates buffer and publishes SSE."""
    runner, secret, commis_job = test_runner_and_commis
    commis_id = commis_job.commis_id

    # Create a runner job linked to the commis
    runner_job = runner_crud.create_runner_job(
        db=db,
        runner_id=runner.id,
        owner_id=runner.owner_id,
        commis_id=commis_id,
        run_id="1",
        command="echo hello",
        timeout_secs=60,
    )

    received_sse = []

    async def sse_listener(payload):
        received_sse.append(payload)

    event_bus.subscribe(EventType.COMMIS_OUTPUT_CHUNK, sse_listener)

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
            await _wait_for(lambda: chunk_data in get_commis_output_buffer().get_tail(commis_id))

            # 3. Verify buffer
            buffer = get_commis_output_buffer()
            tail = buffer.get_tail(commis_id)
            assert chunk_data in tail

            # 4. Verify SSE event
            await _wait_for(lambda: len(received_sse) >= 1)
            payload = received_sse[0]
            assert payload["commis_id"] == commis_id
            assert payload["job_id"] == commis_job.id
            assert payload["data"] == chunk_data
            assert payload["stream"] == "stdout"
            assert payload["run_id"] == 1

            # 5. Verify peek tool works (using the job ID)
            # Need to mock credential context for peek_commis_output
            from zerg.connectors.resolver import CredentialResolver
            from zerg.connectors.context import set_credential_resolver, reset_credential_resolver

            resolver = CredentialResolver(fiche_id=1, db=db, owner_id=runner.owner_id)
            token = set_credential_resolver(resolver)

            try:
                peek_result = peek_commis_output(str(commis_job.id))
                assert "Live commis output" in peek_result
                assert chunk_data in peek_result
            finally:
                reset_credential_resolver(token)

    finally:
        event_bus.unsubscribe(EventType.COMMIS_OUTPUT_CHUNK, sse_listener)


@pytest.mark.asyncio
async def test_live_output_chunk_truncation_sse(client: TestClient, db: Session, test_runner_and_commis):
    """Test that massive chunks are truncated in the SSE payload but fully added to buffer."""
    runner, secret, commis_job = test_runner_and_commis
    commis_id = commis_job.commis_id

    runner_job = runner_crud.create_runner_job(
        db=db,
        runner_id=runner.id,
        owner_id=runner.owner_id,
        commis_id=commis_id,
        run_id="1",
        command="large output",
        timeout_secs=60,
    )

    received_sse = []
    async def sse_listener(payload):
        received_sse.append(payload)

    event_bus.subscribe(EventType.COMMIS_OUTPUT_CHUNK, sse_listener)

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
            await _wait_for(lambda: len(get_commis_output_buffer().get_tail(commis_id)) >= 5000)

            # Buffer should have full 5000
            buffer = get_commis_output_buffer()
            tail = buffer.get_tail(commis_id)
            assert len(tail) >= 5000

            # SSE should have truncated 4000 (tail of it)
            await _wait_for(lambda: len(received_sse) >= 1)
            payload = received_sse[0]
            assert len(payload["data"]) == 4000
            assert payload["data"] == "A" * 4000

    finally:
        event_bus.unsubscribe(EventType.COMMIS_OUTPUT_CHUNK, sse_listener)
