"""End-to-end tests for the Concierge flow via Jarvis API.

These tests simulate the full flow a user would experience through Jarvis:
1. POST /api/jarvis/concierge - dispatch a task
2. GET /api/jarvis/concierge/events - listen to SSE for progress
3. Verify commis are spawned and results are captured

Note: These tests use mocked LLMs, so they test the infrastructure,
not the actual LLM decision-making.
"""

import asyncio
import tempfile

import pytest

from zerg.events import EventType
from zerg.events import event_bus
from zerg.services.concierge_service import ConciergeService


@pytest.mark.timeout(60)  # Concierge tests need more time, especially in CI with parallel commis
class TestConciergeE2EFlow:
    """End-to-end tests for concierge flow via API."""

    @pytest.fixture
    def temp_artifact_path(self, monkeypatch):
        """Create temporary artifact store path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            monkeypatch.setenv("SWARMLET_DATA_PATH", tmpdir)
            yield tmpdir

    def test_concierge_dispatch_returns_stream_url(self, client, db_session, test_user, temp_artifact_path):
        """Test POST /api/jarvis/concierge returns course_id and stream_url."""
        response = client.post(
            "/api/jarvis/concierge",
            json={"task": "What time is it?"},
        )

        assert response.status_code == 200
        data = response.json()

        # Verify response structure matches JarvisConciergeResponse
        assert "course_id" in data
        assert "thread_id" in data
        assert "status" in data
        assert "stream_url" in data

        # Stream URL should point to unified stream endpoint
        assert f"/api/stream/runs/{data['course_id']}" in data["stream_url"]

    @pytest.mark.xdist_group(name="concierge")
    def test_concierge_creates_one_brain_per_user(self, client, db_session, test_user, temp_artifact_path):
        """Test that multiple dispatches use the same concierge thread."""
        # First dispatch
        response1 = client.post(
            "/api/jarvis/concierge",
            json={"task": "First task"},
        )
        data1 = response1.json()

        # Second dispatch
        response2 = client.post(
            "/api/jarvis/concierge",
            json={"task": "Second task"},
        )
        data2 = response2.json()

        # Thread ID should be the same (one brain per user)
        assert data1["thread_id"] == data2["thread_id"]

        # But run IDs should be different
        assert data1["course_id"] != data2["course_id"]

    @pytest.mark.skip(reason="TestClient doesn't support SSE streaming - use Playwright tests instead")
    def test_concierge_sse_stream_connects(self, client, db_session, test_user, temp_artifact_path):
        """Test SSE stream connects and receives initial event.

        NOTE: This test is skipped because:
        1. TestClient.stream() blocks synchronously waiting for data
        2. By the time we connect to SSE, the concierge run has already completed
        3. No events will arrive because the run finished before subscription

        SSE functionality is properly tested via Playwright E2E tests in apps/zerg/e2e/.
        """
        # First create a run
        response = client.post(
            "/api/jarvis/concierge",
            json={"task": "Test SSE connection"},
        )
        course_id = response.json()["course_id"]

        # Connect to SSE stream (this is synchronous in TestClient)
        # Note: TestClient doesn't fully support SSE streaming, so we test
        # that the endpoint is reachable
        with client.stream("GET", f"/api/stream/runs/{course_id}") as sse_response:
            assert sse_response.status_code == 200

            # Read first event (should be "connected")
            first_line = next(sse_response.iter_lines())
            if first_line:
                assert "connected" in first_line or "event" in first_line

    def test_cancel_endpoint_works(self, client, db_session, test_user, temp_artifact_path):
        """Test that cancel endpoint stops a running concierge."""
        # Create a run
        response = client.post(
            "/api/jarvis/concierge",
            json={"task": "Long running task"},
        )
        course_id = response.json()["course_id"]

        # Cancel it
        cancel_response = client.post(f"/api/jarvis/concierge/{course_id}/cancel")

        # Should succeed (might already be complete from mock)
        assert cancel_response.status_code == 200
        data = cancel_response.json()
        assert data["course_id"] == course_id
        assert data["status"] in ["cancelled", "success", "failed"]


class TestConciergeServiceDirect:
    """Direct tests for ConciergeService without API layer."""

    @pytest.fixture
    def temp_artifact_path(self, monkeypatch):
        """Create temporary artifact store path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            monkeypatch.setenv("SWARMLET_DATA_PATH", tmpdir)
            yield tmpdir

    @pytest.mark.asyncio
    async def test_run_concierge_completes(self, db_session, test_user, temp_artifact_path):
        """Test that run_concierge executes and returns result."""
        service = ConciergeService(db_session)

        result = await service.run_concierge(
            owner_id=test_user.id,
            task="What is 2 + 2?",
            timeout=30,
        )

        # Verify result structure
        assert result.course_id is not None
        assert result.thread_id is not None
        assert result.status in ["success", "failed"]
        assert result.duration_ms >= 0
        assert result.debug_url is not None

        # Debug URL should contain course_id
        assert str(result.course_id) in result.debug_url

    @pytest.mark.asyncio
    async def test_run_concierge_emits_events(self, db_session, test_user, temp_artifact_path):
        """Test that concierge emits SSE events during execution."""
        service = ConciergeService(db_session)

        # Collect events
        events_received = []

        async def capture_event(event_data):
            events_received.append(event_data)

        # Subscribe to events
        event_bus.subscribe(EventType.CONCIERGE_STARTED, capture_event)
        event_bus.subscribe(EventType.CONCIERGE_THINKING, capture_event)
        event_bus.subscribe(EventType.CONCIERGE_COMPLETE, capture_event)

        try:
            result = await service.run_concierge(
                owner_id=test_user.id,
                task="Simple test task",
                timeout=30,
            )

            # Give events time to propagate
            await asyncio.sleep(0.1)

            # Should have received CONCIERGE_STARTED at minimum
            event_types = [e.get("event_type") for e in events_received]
            assert EventType.CONCIERGE_STARTED in event_types or any("CONCIERGE" in str(et) for et in event_types)

        finally:
            # Unsubscribe
            event_bus.unsubscribe(EventType.CONCIERGE_STARTED, capture_event)
            event_bus.unsubscribe(EventType.CONCIERGE_THINKING, capture_event)
            event_bus.unsubscribe(EventType.CONCIERGE_COMPLETE, capture_event)

    @pytest.mark.asyncio
    async def test_concierge_thread_persists_across_calls(self, db_session, test_user, temp_artifact_path):
        """Test that concierge thread accumulates context."""
        service = ConciergeService(db_session)

        # First run
        result1 = await service.run_concierge(
            owner_id=test_user.id,
            task="Remember the number 42",
            timeout=30,
        )

        # Second run
        result2 = await service.run_concierge(
            owner_id=test_user.id,
            task="What number did I mention?",
            timeout=30,
        )

        # Should use same thread
        assert result1.thread_id == result2.thread_id

        # Different runs
        assert result1.course_id != result2.course_id


class TestCommisSpawning:
    """Tests for commis spawning from concierge."""

    @pytest.fixture
    def temp_artifact_path(self, monkeypatch):
        """Create temporary artifact store path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            monkeypatch.setenv("SWARMLET_DATA_PATH", tmpdir)
            yield tmpdir

    def test_spawn_commis_creates_job(self, db_session, test_user, temp_artifact_path):
        """Test that spawn_commis tool creates a CommisJob."""
        from tests.conftest import TEST_COMMIS_MODEL
        from zerg.connectors.context import set_credential_resolver
        from zerg.connectors.resolver import CredentialResolver
        from zerg.models.models import CommisJob
        from zerg.tools.builtin.concierge_tools import spawn_commis

        # Set up credential context
        resolver = CredentialResolver(fiche_id=1, db=db_session, owner_id=test_user.id)
        token = set_credential_resolver(resolver)

        try:
            result = spawn_commis(
                task="Check disk usage on cube",
                model=TEST_COMMIS_MODEL,
            )

            assert "queued successfully" in result

            # Verify job was created
            job = db_session.query(CommisJob).filter(CommisJob.task == "Check disk usage on cube").first()

            assert job is not None
            assert job.status == "queued"
            assert job.owner_id == test_user.id

        finally:
            set_credential_resolver(None)

    @pytest.mark.asyncio
    async def test_commis_job_has_correct_tools(self, db_session, test_user, temp_artifact_path):
        """Test that commis fiches are created with infrastructure tools."""
        from tests.conftest import TEST_COMMIS_MODEL
        from zerg.services.commis_artifact_store import CommisArtifactStore
        from zerg.services.commis_runner import CommisRunner

        store = CommisArtifactStore(base_path=temp_artifact_path)
        runner = CommisRunner(artifact_store=store)

        # Create a temporary fiche to check its tools
        temp_agent = await runner._create_temporary_agent(
            db=db_session,
            task="test infrastructure tools",
            config={"owner_id": test_user.id, "model": TEST_COMMIS_MODEL},
        )

        try:
            # Verify infrastructure tools are present
            assert "ssh_exec" in temp_agent.allowed_tools
            assert "http_request" in temp_agent.allowed_tools
            assert "get_current_time" in temp_agent.allowed_tools

        finally:
            # Clean up
            from zerg.crud import crud

            crud.delete_fiche(db_session, temp_agent.id)
            db_session.commit()
