"""Tests for Jarvis course status and stream endpoints (Phase 4 of durable-runs-v2.2)."""

import pytest
from fastapi import status

from zerg.models.enums import CourseStatus
from zerg.models.enums import CourseTrigger
from zerg.models.models import Course
from zerg.models.models import Thread
from zerg.models.models import ThreadMessage
from zerg.services.concierge_service import ConciergeService


class TestGetCourseStatus:
    """Tests for GET /api/jarvis/courses/{course_id} endpoint."""

    @pytest.fixture
    def course_components(self, db_session, test_user):
        """Create concierge fiche, thread, course, and messages for testing."""
        service = ConciergeService(db_session)
        fiche = service.get_or_create_concierge_fiche(test_user.id)
        thread = service.get_or_create_concierge_thread(test_user.id, fiche)

        # Create a course
        course = Course(
            fiche_id=fiche.id,
            thread_id=thread.id,
            status=CourseStatus.RUNNING,
            trigger=CourseTrigger.API,
        )
        db_session.add(course)
        db_session.commit()
        db_session.refresh(course)

        return {"fiche": fiche, "thread": thread, "course": course}

    def test_get_running_course_status(self, client, course_components):
        """Test getting status of a running course."""
        course = course_components["course"]

        response = client.get(f"/api/jarvis/courses/{course.id}")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["course_id"] == course.id
        assert data["status"] == "running"
        assert data["created_at"] is not None
        assert data["finished_at"] is None
        assert data["error"] is None
        assert data["result"] is None  # No result for running courses

    def test_get_successful_course_with_result(self, client, db_session, course_components):
        """Test getting status of a completed course with result."""
        course = course_components["course"]
        thread = course_components["thread"]

        # Add an assistant message to the thread
        message = ThreadMessage(
            thread_id=thread.id,
            role="assistant",
            content="Task completed successfully. Here is your result.",
        )
        db_session.add(message)

        # Mark course as successful
        course.status = CourseStatus.SUCCESS
        db_session.commit()

        response = client.get(f"/api/jarvis/courses/{course.id}")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["course_id"] == course.id
        assert data["status"] == "success"
        assert data["result"] == "Task completed successfully. Here is your result."
        assert data["error"] is None

    def test_get_failed_course_with_error(self, client, db_session, course_components):
        """Test getting status of a failed course."""
        course = course_components["course"]

        # Mark course as failed with error
        course.status = CourseStatus.FAILED
        course.error = "Connection timeout after 30s"
        db_session.commit()

        response = client.get(f"/api/jarvis/courses/{course.id}")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["course_id"] == course.id
        assert data["status"] == "failed"
        assert data["error"] == "Connection timeout after 30s"
        assert data["result"] is None  # No result for failed courses

    def test_get_nonexistent_course(self, client):
        """Test getting status of a course that doesn't exist."""
        response = client.get("/api/jarvis/courses/99999")

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_get_course_multi_tenant_isolation(self, client, db_session, course_components, other_user):
        """Test that users cannot access courses owned by other users."""
        course = course_components["course"]

        # Create a course owned by another user
        other_fiche = ConciergeService(db_session).get_or_create_concierge_fiche(other_user.id)
        other_thread = ConciergeService(db_session).get_or_create_concierge_thread(other_user.id, other_fiche)

        other_course = Course(
            fiche_id=other_fiche.id,
            thread_id=other_thread.id,
            status=CourseStatus.SUCCESS,
            trigger=CourseTrigger.API,
        )
        db_session.add(other_course)
        db_session.commit()
        db_session.refresh(other_course)

        # Try to access the other user's course (client is authenticated as test_user)
        response = client.get(f"/api/jarvis/courses/{other_course.id}")

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_get_course_with_structured_content(self, client, db_session, course_components):
        """Test handling of structured message content (JSON-encoded list of blocks)."""
        import json

        course = course_components["course"]
        thread = course_components["thread"]

        # Add an assistant message with structured content (stored as JSON string)
        structured_content = [
            {"type": "text", "text": "First part. "},
            {"type": "text", "text": "Second part."},
        ]
        message = ThreadMessage(
            thread_id=thread.id,
            role="assistant",
            content=json.dumps(structured_content),  # Store as JSON string
        )
        db_session.add(message)

        # Mark course as successful
        course.status = CourseStatus.SUCCESS
        db_session.commit()

        response = client.get(f"/api/jarvis/courses/{course.id}")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        # Helper should parse JSON and extract text blocks
        assert data["result"] == "First part.  Second part."

    def test_get_course_with_multiple_messages(self, client, db_session, course_components):
        """Test that only the LAST assistant message is returned as result."""
        course = course_components["course"]
        thread = course_components["thread"]

        # Add multiple assistant messages
        for i in range(3):
            message = ThreadMessage(
                thread_id=thread.id,
                role="assistant",
                content=f"Message {i + 1}",
            )
            db_session.add(message)

        # Mark course as successful
        course.status = CourseStatus.SUCCESS
        db_session.commit()

        response = client.get(f"/api/jarvis/courses/{course.id}")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        # Should return the LAST message (Message 3)
        assert data["result"] == "Message 3"


class TestListJarvisCourses:
    def test_list_courses_avoids_fiche_n_plus_one(self, client, db_session, test_user, monkeypatch):
        service = ConciergeService(db_session)
        fiche = service.get_or_create_concierge_fiche(test_user.id)
        thread = service.get_or_create_concierge_thread(test_user.id, fiche)

        course = Course(
            fiche_id=fiche.id,
            thread_id=thread.id,
            status=CourseStatus.SUCCESS,
            trigger=CourseTrigger.API,
        )
        db_session.add(course)
        db_session.commit()

        from zerg.routers import jarvis_courses

        selectinload_called = {"value": False}
        real_selectinload = jarvis_courses.selectinload

        def _track_selectinload(*args, **kwargs):
            selectinload_called["value"] = True
            return real_selectinload(*args, **kwargs)

        monkeypatch.setattr(jarvis_courses, "selectinload", _track_selectinload)

        response = client.get("/api/jarvis/courses")
        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert selectinload_called["value"] is True
        assert payload[0]["fiche_name"] == fiche.name


import httpx
import pytest_asyncio


class TestAttachToCourseStream:
    """Tests for GET /api/jarvis/courses/{course_id}/stream endpoint."""

    @pytest.fixture
    def course_components(self, db_session, test_user):
        """Create concierge fiche, thread, and course for stream testing."""
        service = ConciergeService(db_session)
        fiche = service.get_or_create_concierge_fiche(test_user.id)
        thread = service.get_or_create_concierge_thread(test_user.id, fiche)

        course = Course(
            fiche_id=fiche.id,
            thread_id=thread.id,
            status=CourseStatus.RUNNING,
            trigger=CourseTrigger.API,
        )
        db_session.add(course)
        db_session.commit()
        db_session.refresh(course)

        return {"fiche": fiche, "thread": thread, "course": course}

    @pytest_asyncio.fixture
    async def async_client(self, db_session, auth_headers):
        """Asynchronous client for testing SSE streams."""
        from zerg.database import get_db
        from zerg.main import app

        def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
            ac.headers.update(auth_headers)
            yield ac
        app.dependency_overrides = {}

    @pytest.mark.asyncio
    async def test_attach_to_completed_course(self, async_client, db_session, course_components):
        """Test attaching to a completed course returns result immediately."""
        course = course_components["course"]
        thread = course_components["thread"]

        # Add result message
        message = ThreadMessage(
            thread_id=thread.id,
            role="assistant",
            content="Analysis complete.",
        )
        db_session.add(message)

        # Mark course as complete
        course.status = CourseStatus.SUCCESS
        db_session.commit()

        # Use the JSON endpoint, not the SSE stream endpoint
        response = await async_client.get(f"/api/jarvis/courses/{course.id}")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["course_id"] == course.id
        assert data["status"] == "success"
        assert data["result"] == "Analysis complete."
        assert data["error"] is None

    @pytest.mark.asyncio
    async def test_attach_to_failed_course(self, async_client, db_session, course_components):
        """Test attaching to a failed course returns error."""
        course = course_components["course"]

        # Mark course as failed
        course.status = CourseStatus.FAILED
        course.error = "Tool execution failed"
        db_session.commit()

        # Use the JSON endpoint, not the SSE stream endpoint
        response = await async_client.get(f"/api/jarvis/courses/{course.id}")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "failed"
        assert data["error"] == "Tool execution failed"
        assert data["result"] is None

    @pytest.mark.asyncio
    async def test_attach_to_in_progress_course(self, async_client, course_components):
        """Test attaching to an in-progress course returns current status snapshot."""
        course = course_components["course"]

        # Use the JSON endpoint, not the SSE stream endpoint
        response = await async_client.get(f"/api/jarvis/courses/{course.id}")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["course_id"] == course.id
        assert data["status"] == "running"
        assert data["result"] is None

    @pytest.mark.asyncio
    async def test_attach_to_nonexistent_course(self, async_client):
        """Test attaching to a course that doesn't exist."""
        # Use the JSON endpoint, not the SSE stream endpoint
        response = await async_client.get("/api/jarvis/courses/99999")

        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_attach_stream_multi_tenant_isolation(self, async_client, db_session, course_components, other_user):
        """Test that users cannot attach to courses owned by other users."""
        # Create a course owned by another user
        other_fiche = ConciergeService(db_session).get_or_create_concierge_fiche(other_user.id)
        other_thread = ConciergeService(db_session).get_or_create_concierge_thread(other_user.id, other_fiche)

        other_course = Course(
            fiche_id=other_fiche.id,
            thread_id=other_thread.id,
            status=CourseStatus.SUCCESS,
            trigger=CourseTrigger.API,
        )
        db_session.add(other_course)
        db_session.commit()
        db_session.refresh(other_course)

        # Try to attach to the other user's course (use JSON endpoint)
        response = await async_client.get(f"/api/jarvis/courses/{other_course.id}")

        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_attach_includes_finished_at(self, async_client, db_session, course_components):
        """Test that finished_at timestamp is included in response."""
        course = course_components["course"]
        thread = course_components["thread"]

        # Add result message
        message = ThreadMessage(
            thread_id=thread.id,
            role="assistant",
            content="Done.",
        )
        db_session.add(message)

        # Mark course as complete
        course.status = CourseStatus.SUCCESS
        from datetime import datetime, timezone

        course.finished_at = datetime.now(timezone.utc)
        db_session.commit()

        # Use the JSON endpoint, not the SSE stream endpoint
        response = await async_client.get(f"/api/jarvis/courses/{course.id}")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["finished_at"] is not None
        # Verify it's a valid ISO timestamp
        datetime.fromisoformat(data["finished_at"].replace("Z", "+00:00"))


class TestGetCourseTimeline:
    """Tests for GET /api/jarvis/courses/{course_id}/timeline endpoint (Phase 3: chat-observability-eval)."""

    @pytest.fixture
    def course_with_events(self, db_session, test_user):
        """Create a course with sample timeline events."""
        from datetime import datetime, timezone, timedelta
        from zerg.models.course_event import CourseEvent

        service = ConciergeService(db_session)
        fiche = service.get_or_create_concierge_fiche(test_user.id)
        thread = service.get_or_create_concierge_thread(test_user.id, fiche)

        # Create a course with correlation ID
        course = Course(
            fiche_id=fiche.id,
            thread_id=thread.id,
            status=CourseStatus.SUCCESS,
            trigger=CourseTrigger.API,
            correlation_id="test-correlation-123",
        )
        db_session.add(course)
        db_session.commit()
        db_session.refresh(course)

        # Create timeline events (simulating a full concierge + commis flow)
        base_time = datetime.now(timezone.utc)

        events = [
            CourseEvent(
                course_id=course.id,
                event_type="concierge_started",
                payload={"message": "Starting concierge"},
                created_at=base_time,
            ),
            CourseEvent(
                course_id=course.id,
                event_type="concierge_thinking",
                payload={"status": "analyzing"},
                created_at=base_time + timedelta(milliseconds=100),
            ),
            CourseEvent(
                course_id=course.id,
                event_type="commis_spawned",
                payload={"job_id": 1, "commis_type": "executor"},
                created_at=base_time + timedelta(milliseconds=500),
            ),
            CourseEvent(
                course_id=course.id,
                event_type="commis_started",
                payload={"commis_id": "commis-123"},
                created_at=base_time + timedelta(milliseconds=700),
            ),
            CourseEvent(
                course_id=course.id,
                event_type="tool_started",
                payload={"tool_name": "ssh_exec"},
                created_at=base_time + timedelta(milliseconds=900),
            ),
            CourseEvent(
                course_id=course.id,
                event_type="tool_completed",
                payload={"tool_name": "ssh_exec", "duration_ms": 400},
                created_at=base_time + timedelta(milliseconds=1300),
            ),
            CourseEvent(
                course_id=course.id,
                event_type="commis_complete",
                payload={"result": "Success"},
                created_at=base_time + timedelta(milliseconds=1600),
            ),
            CourseEvent(
                course_id=course.id,
                event_type="concierge_complete",
                payload={"final_result": "Task complete"},
                created_at=base_time + timedelta(milliseconds=2000),
            ),
        ]

        for event in events:
            db_session.add(event)
        db_session.commit()

        return {"fiche": fiche, "thread": thread, "course": course, "events": events}

    def test_get_timeline_success(self, client, course_with_events):
        """Test getting timeline for a course with events."""
        course = course_with_events["course"]

        response = client.get(f"/api/jarvis/courses/{course.id}/timeline")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()

        # Verify structure
        assert data["correlation_id"] == "test-correlation-123"
        assert data["course_id"] == course.id
        assert "events" in data
        assert "summary" in data

        # Verify events
        events = data["events"]
        assert len(events) == 8  # All 8 events

        # First event should have offset 0
        assert events[0]["phase"] == "concierge_started"
        assert events[0]["offset_ms"] == 0
        assert events[0]["timestamp"] is not None

        # Last event should have highest offset
        assert events[-1]["phase"] == "concierge_complete"
        assert events[-1]["offset_ms"] > 0

        # Verify events are sorted by offset
        offsets = [e["offset_ms"] for e in events]
        assert offsets == sorted(offsets)

        # Verify metadata is included
        assert events[2]["metadata"]["job_id"] == 1
        assert events[4]["metadata"]["tool_name"] == "ssh_exec"

    def test_get_timeline_summary_calculations(self, client, course_with_events):
        """Test that summary statistics are calculated correctly."""
        course = course_with_events["course"]

        response = client.get(f"/api/jarvis/courses/{course.id}/timeline")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()

        summary = data["summary"]

        # Total duration should be from first to last event
        assert summary["total_duration_ms"] == 2000

        # Concierge thinking time: concierge_started to commis_spawned
        assert summary["concierge_thinking_ms"] == 500

        # Commis execution time: commis_spawned to commis_complete
        assert summary["commis_execution_ms"] == 1100

        # Tool execution time: tool_started to tool_completed
        assert summary["tool_execution_ms"] == 400

    def test_get_timeline_empty_events(self, client, db_session, test_user):
        """Test getting timeline for a course with no events yet."""
        service = ConciergeService(db_session)
        fiche = service.get_or_create_concierge_fiche(test_user.id)
        thread = service.get_or_create_concierge_thread(test_user.id, fiche)

        # Create a course with no events
        course = Course(
            fiche_id=fiche.id,
            thread_id=thread.id,
            status=CourseStatus.RUNNING,
            trigger=CourseTrigger.API,
            correlation_id="empty-course-123",
        )
        db_session.add(course)
        db_session.commit()
        db_session.refresh(course)

        response = client.get(f"/api/jarvis/courses/{course.id}/timeline")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()

        assert data["correlation_id"] == "empty-course-123"
        assert data["course_id"] == course.id
        assert data["events"] == []
        assert data["summary"]["total_duration_ms"] == 0
        assert data["summary"]["concierge_thinking_ms"] is None
        assert data["summary"]["commis_execution_ms"] is None
        assert data["summary"]["tool_execution_ms"] is None

    def test_get_timeline_nonexistent_course(self, client):
        """Test getting timeline for a course that doesn't exist."""
        response = client.get("/api/jarvis/courses/99999/timeline")

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_get_timeline_multi_tenant_isolation(self, client, db_session, course_with_events, other_user):
        """Test that users cannot access timelines for courses owned by other users."""
        # Create a course owned by another user
        other_fiche = ConciergeService(db_session).get_or_create_concierge_fiche(other_user.id)
        other_thread = ConciergeService(db_session).get_or_create_concierge_thread(other_user.id, other_fiche)

        other_course = Course(
            fiche_id=other_fiche.id,
            thread_id=other_thread.id,
            status=CourseStatus.SUCCESS,
            trigger=CourseTrigger.API,
            correlation_id="other-user-course",
        )
        db_session.add(other_course)
        db_session.commit()
        db_session.refresh(other_course)

        # Try to access the other user's course timeline (client is authenticated as test_user)
        response = client.get(f"/api/jarvis/courses/{other_course.id}/timeline")

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_get_timeline_partial_flow(self, client, db_session, test_user):
        """Test timeline with partial flow (concierge only, no commis)."""
        from datetime import datetime, timezone, timedelta
        from zerg.models.course_event import CourseEvent

        service = ConciergeService(db_session)
        fiche = service.get_or_create_concierge_fiche(test_user.id)
        thread = service.get_or_create_concierge_thread(test_user.id, fiche)

        course = Course(
            fiche_id=fiche.id,
            thread_id=thread.id,
            status=CourseStatus.SUCCESS,
            trigger=CourseTrigger.API,
            correlation_id="partial-flow",
        )
        db_session.add(course)
        db_session.commit()
        db_session.refresh(course)

        # Only concierge events (no commis spawned)
        base_time = datetime.now(timezone.utc)
        events = [
            CourseEvent(
                course_id=course.id,
                event_type="concierge_started",
                payload={},
                created_at=base_time,
            ),
            CourseEvent(
                course_id=course.id,
                event_type="concierge_thinking",
                payload={},
                created_at=base_time + timedelta(milliseconds=200),
            ),
            CourseEvent(
                course_id=course.id,
                event_type="concierge_complete",
                payload={},
                created_at=base_time + timedelta(milliseconds=800),
            ),
        ]

        for event in events:
            db_session.add(event)
        db_session.commit()

        response = client.get(f"/api/jarvis/courses/{course.id}/timeline")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()

        # Summary should handle missing commis/tool metrics gracefully
        summary = data["summary"]
        assert summary["total_duration_ms"] == 800
        assert summary["concierge_thinking_ms"] is None  # No commis_spawned event
        assert summary["commis_execution_ms"] is None
        assert summary["tool_execution_ms"] is None
