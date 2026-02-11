"""Tests for the OikosService - manages oikos fiche and thread lifecycle."""

import tempfile

import pytest

from tests.conftest import TEST_COMMIS_MODEL
from zerg.connectors.context import set_credential_resolver
from zerg.connectors.resolver import CredentialResolver
from zerg.models.enums import RunStatus
from zerg.models.models import Run
from zerg.services.oikos_context import get_next_seq
from zerg.services.oikos_context import get_oikos_context
from zerg.services.oikos_context import reset_oikos_context
from zerg.services.oikos_context import reset_seq
from zerg.services.oikos_context import set_oikos_context
from zerg.services.oikos_service import OIKOS_THREAD_TYPE
from zerg.services.oikos_service import OikosService


@pytest.fixture
def temp_artifact_path(monkeypatch):
    """Create temporary artifact store path and set environment variable."""
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setenv("LONGHOUSE_DATA_PATH", tmpdir)
        yield tmpdir


@pytest.fixture
def credential_context(db_session, test_user):
    """Set up credential resolver context for tools."""
    resolver = CredentialResolver(fiche_id=1, db=db_session, owner_id=test_user.id)
    token = set_credential_resolver(resolver)
    yield resolver
    set_credential_resolver(None)


class TestOikosService:
    """Test suite for OikosService."""

    def test_get_or_create_oikos_fiche_creates_new(self, db_session, test_user):
        """Test that a new oikos fiche is created when none exists."""
        service = OikosService(db_session)

        fiche = service.get_or_create_oikos_fiche(test_user.id)

        assert fiche is not None
        assert fiche.name == "Oikos"
        assert fiche.owner_id == test_user.id
        assert fiche.config.get("is_oikos") is True
        assert "spawn_commis" in fiche.allowed_tools
        assert "list_commiss" in fiche.allowed_tools
        # V1.1: knowledge_search should be available to oikos
        assert "knowledge_search" in fiche.allowed_tools
        # V1.2: web research tools should be available to oikos
        assert "web_search" in fiche.allowed_tools
        assert "web_fetch" in fiche.allowed_tools

    def test_get_or_create_oikos_fiche_returns_existing(self, db_session, test_user):
        """Test that existing oikos fiche is returned on subsequent calls."""
        service = OikosService(db_session)

        # Create first time
        agent1 = service.get_or_create_oikos_fiche(test_user.id)
        agent1_id = agent1.id

        # Get again - should return same fiche
        agent2 = service.get_or_create_oikos_fiche(test_user.id)

        assert agent2.id == agent1_id

    def test_get_or_create_oikos_thread_creates_new(self, db_session, test_user):
        """Test that a new oikos thread is created when none exists."""
        service = OikosService(db_session)

        fiche = service.get_or_create_oikos_fiche(test_user.id)
        thread = service.get_or_create_oikos_thread(test_user.id, fiche)

        assert thread is not None
        assert thread.thread_type == OIKOS_THREAD_TYPE
        assert thread.fiche_id == fiche.id
        assert thread.title == "Oikos"

    def test_get_or_create_oikos_thread_returns_existing(self, db_session, test_user):
        """Test that existing oikos thread is returned on subsequent calls."""
        service = OikosService(db_session)

        fiche = service.get_or_create_oikos_fiche(test_user.id)

        # Create first time
        thread1 = service.get_or_create_oikos_thread(test_user.id, fiche)
        thread1_id = thread1.id

        # Get again - should return same thread (one brain per user)
        thread2 = service.get_or_create_oikos_thread(test_user.id, fiche)

        assert thread2.id == thread1_id

    def test_oikos_per_user_isolation(self, db_session, test_user, other_user):
        """Test that each user gets their own oikos fiche and thread."""
        service = OikosService(db_session)

        # Get oikos for test_user
        fiche1 = service.get_or_create_oikos_fiche(test_user.id)
        thread1 = service.get_or_create_oikos_thread(test_user.id, fiche1)

        # Get oikos for other_user
        fiche2 = service.get_or_create_oikos_fiche(other_user.id)
        thread2 = service.get_or_create_oikos_thread(other_user.id, fiche2)

        # Should be different fiches and threads
        assert fiche1.id != fiche2.id
        assert thread1.id != thread2.id

        # Each owned by their respective user
        assert fiche1.owner_id == test_user.id
        assert fiche2.owner_id == other_user.id

    def test_oikos_fiche_has_correct_tools(self, db_session, test_user):
        """Test that oikos fiche is configured with correct tools."""
        service = OikosService(db_session)

        fiche = service.get_or_create_oikos_fiche(test_user.id)

        expected_tools = [
            "spawn_commis",
            "list_commiss",
            "read_commis_result",
            "read_commis_file",
            "grep_commiss",
            "get_commis_metadata",
            "get_current_time",
            "http_request",
            "send_email",
        ]

        for tool in expected_tools:
            assert tool in fiche.allowed_tools, f"Missing tool: {tool}"

    def test_get_or_create_oikos_thread_creates_fiche_if_needed(self, db_session, test_user):
        """Test that thread creation also creates fiche if not provided."""
        service = OikosService(db_session)

        # Call without providing fiche - should create both
        thread = service.get_or_create_oikos_thread(test_user.id, fiche=None)

        assert thread is not None
        assert thread.thread_type == OIKOS_THREAD_TYPE

        # Verify fiche was created
        fiche = service.get_or_create_oikos_fiche(test_user.id)
        assert thread.fiche_id == fiche.id


class TestOikosContext:
    """Tests for oikos context (run_id threading)."""

    def test_oikos_context_default_is_none(self):
        """Test that oikos context defaults to None."""
        assert get_oikos_context() is None

    def test_oikos_context_set_and_get(self):
        """Test setting and getting oikos context."""
        token = set_oikos_context(run_id=123, owner_id=1, message_id="test-msg-1")
        try:
            ctx = get_oikos_context()
            assert ctx is not None
            assert ctx.run_id == 123
            assert ctx.owner_id == 1
            assert ctx.message_id == "test-msg-1"
        finally:
            reset_oikos_context(token)

        # After reset, should be back to default
        assert get_oikos_context() is None

    def test_oikos_context_reset_restores_previous(self):
        """Test that reset restores previous value."""
        # Set first value
        token1 = set_oikos_context(run_id=100, owner_id=1, message_id="msg-100")
        ctx1 = get_oikos_context()
        assert ctx1 is not None
        assert ctx1.run_id == 100

        # Set second value
        token2 = set_oikos_context(run_id=200, owner_id=1, message_id="msg-200")
        ctx2 = get_oikos_context()
        assert ctx2 is not None
        assert ctx2.run_id == 200

        # Reset second - should restore first
        reset_oikos_context(token2)
        ctx_after = get_oikos_context()
        assert ctx_after is not None
        assert ctx_after.run_id == 100

        # Reset first - should restore None
        reset_oikos_context(token1)
        assert get_oikos_context() is None

    def test_seq_counter_starts_at_one(self):
        """Test that seq counter starts at 1 for a new run_id."""
        run_id = 999
        try:
            assert get_next_seq(run_id) == 1
        finally:
            reset_seq(run_id)

    def test_seq_counter_increments(self):
        """Test that seq counter increments monotonically."""
        run_id = 1001
        try:
            assert get_next_seq(run_id) == 1
            assert get_next_seq(run_id) == 2
            assert get_next_seq(run_id) == 3
        finally:
            reset_seq(run_id)

    def test_seq_counter_per_run_isolation(self):
        """Test that different run_ids have separate counters."""
        run_id_a = 2001
        run_id_b = 2002
        try:
            # Both should start at 1
            assert get_next_seq(run_id_a) == 1
            assert get_next_seq(run_id_b) == 1

            # Incrementing one doesn't affect the other
            assert get_next_seq(run_id_a) == 2
            assert get_next_seq(run_id_a) == 3
            assert get_next_seq(run_id_b) == 2
        finally:
            reset_seq(run_id_a)
            reset_seq(run_id_b)

    def test_seq_reset_clears_counter(self):
        """Test that reset_seq clears the counter for a run_id."""
        run_id = 3001
        try:
            assert get_next_seq(run_id) == 1
            assert get_next_seq(run_id) == 2
            reset_seq(run_id)
            # After reset, should start at 1 again
            assert get_next_seq(run_id) == 1
        finally:
            reset_seq(run_id)


class TestCommisOikosCorrelation:
    """Tests for commis-oikos correlation via run_id."""

    def test_spawn_commis_stores_oikos_run_id(self, db_session, test_user, credential_context, temp_artifact_path):
        """Test that spawn_commis stores oikos_run_id from context."""
        from tests.conftest import TEST_COMMIS_MODEL
        from zerg.models.models import CommisJob
        from zerg.tools.builtin.oikos_tools import spawn_commis

        # Create a real oikos fiche and run for FK constraint
        service = OikosService(db_session)
        fiche = service.get_or_create_oikos_fiche(test_user.id)
        thread = service.get_or_create_oikos_thread(test_user.id, fiche)

        # Create a run
        from zerg.models.enums import RunTrigger

        run = Run(
            fiche_id=fiche.id,
            thread_id=thread.id,
            status=RunStatus.RUNNING,
            trigger=RunTrigger.API,
        )
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)

        # Set oikos context with real run_id
        token = set_oikos_context(run_id=run.id, owner_id=test_user.id, message_id="test-message-id")
        try:
            result = spawn_commis(task="Test task", model=TEST_COMMIS_MODEL)
            assert "queued successfully" in result

            # Find the created job
            job = db_session.query(CommisJob).filter(CommisJob.task == "Test task").first()
            assert job is not None
            assert job.oikos_run_id == run.id
        finally:
            reset_oikos_context(token)

    def test_spawn_commis_without_context_has_null_oikos_run_id(
        self, db_session, test_user, credential_context, temp_artifact_path
    ):
        """Test that spawn_commis without context sets oikos_run_id to None."""
        from tests.conftest import TEST_COMMIS_MODEL
        from zerg.models.models import CommisJob
        from zerg.tools.builtin.oikos_tools import spawn_commis

        # Ensure no oikos context
        assert get_oikos_context() is None

        result = spawn_commis(task="Standalone task", model=TEST_COMMIS_MODEL)
        assert "queued successfully" in result

        # Find the created job
        job = db_session.query(CommisJob).filter(CommisJob.task == "Standalone task").first()
        assert job is not None
        assert job.oikos_run_id is None

    # NOTE: test_run_continuation_inherits_model was removed during the oikos
    # resume refactor (Jan 2026). The continuation pattern now uses
    # FicheInterrupted + FicheRunner.run_continuation instead of separate runs.


class TestRecentCommisHistoryInjection:
    """Tests for v2.0 recent commis history auto-injection.

    This feature injects recent commis results into oikos context
    to prevent redundant commis spawns.
    """

    def test_build_recent_commis_context_no_commis(self, db_session, test_user):
        """Should return None when no recent commis exist."""
        service = OikosService(db_session)
        context, jobs_to_ack = service._build_recent_commis_context(test_user.id)
        assert context is None
        assert jobs_to_ack == []

    def test_build_recent_commis_context_with_commis(self, db_session, test_user, temp_artifact_path):
        """Should return formatted context when recent commis exist."""
        from datetime import datetime
        from datetime import timezone

        from zerg.models.models import CommisJob

        # Create a recent commis job
        job = CommisJob(
            owner_id=test_user.id,
            task="Check disk usage on cube",
            model=TEST_COMMIS_MODEL,
            status="success",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        service = OikosService(db_session)
        context, jobs_to_ack = service._build_recent_commis_context(test_user.id)

        assert context is not None
        assert "Commis Inbox" in context
        assert f"Job {job.id}" in context
        assert "SUCCESS" in context
        assert "Check disk usage" in context
        # Unacknowledged job should be in the list to acknowledge
        assert job.id in jobs_to_ack

    def test_build_recent_commis_context_respects_limit(self, db_session, test_user, temp_artifact_path):
        """Should only return up to RECENT_COMMIS_HISTORY_LIMIT commis."""
        from datetime import datetime
        from datetime import timezone

        from zerg.models.models import CommisJob
        from zerg.services.oikos_service import RECENT_COMMIS_HISTORY_LIMIT

        # Create more commis than the limit
        for i in range(RECENT_COMMIS_HISTORY_LIMIT + 3):
            job = CommisJob(
                owner_id=test_user.id,
                task=f"Task {i}",
                model=TEST_COMMIS_MODEL,
                status="success",
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(job)
        db_session.commit()

        service = OikosService(db_session)
        context, jobs_to_ack = service._build_recent_commis_context(test_user.id)

        assert context is not None
        # Count how many "Job X" entries
        job_count = context.count("Job ")
        assert job_count == RECENT_COMMIS_HISTORY_LIMIT
        # Should have RECENT_COMMIS_HISTORY_LIMIT jobs to acknowledge
        assert len(jobs_to_ack) == RECENT_COMMIS_HISTORY_LIMIT

    def test_build_recent_commis_context_includes_running(self, db_session, test_user, temp_artifact_path):
        """Should include running commis in context."""
        from datetime import datetime
        from datetime import timezone

        from zerg.models.models import CommisJob

        job = CommisJob(
            owner_id=test_user.id,
            task="Long running investigation",
            model=TEST_COMMIS_MODEL,
            status="running",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(job)
        db_session.commit()

        service = OikosService(db_session)
        context, jobs_to_ack = service._build_recent_commis_context(test_user.id)

        assert context is not None
        assert "RUNNING" in context
        assert "Long running investigation" in context
        # Running jobs are not acknowledged (only completed jobs)
        assert jobs_to_ack == []

    def test_build_recent_commis_context_includes_marker(self, db_session, test_user, temp_artifact_path):
        """Context should include marker for cleanup identification."""
        from datetime import datetime
        from datetime import timezone

        from zerg.models.models import CommisJob
        from zerg.services.oikos_service import RECENT_COMMIS_CONTEXT_MARKER

        job = CommisJob(
            owner_id=test_user.id,
            task="Test task",
            model=TEST_COMMIS_MODEL,
            status="success",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(job)
        db_session.commit()

        service = OikosService(db_session)
        context, jobs_to_ack = service._build_recent_commis_context(test_user.id)

        assert context is not None
        assert RECENT_COMMIS_CONTEXT_MARKER in context
        # Completed job should be in acknowledgement list
        assert job.id in jobs_to_ack

    def test_cleanup_stale_commis_context(self, db_session, test_user, temp_artifact_path):
        """Should delete messages containing the marker (older than min_age)."""
        from zerg.crud import crud
        from zerg.models.models import ThreadMessage
        from zerg.services.oikos_service import RECENT_COMMIS_CONTEXT_MARKER

        # Create oikos fiche and thread
        service = OikosService(db_session)
        fiche = service.get_or_create_oikos_fiche(test_user.id)
        thread = service.get_or_create_oikos_thread(test_user.id, fiche)

        # Add a stale context message
        crud.create_thread_message(
            db=db_session,
            thread_id=thread.id,
            role="system",
            content=f"{RECENT_COMMIS_CONTEXT_MARKER}\n## Stale context",
            processed=True,
        )
        db_session.commit()

        # Verify message exists
        messages_before = (
            db_session.query(ThreadMessage)
            .filter(
                ThreadMessage.thread_id == thread.id,
                ThreadMessage.content.contains(RECENT_COMMIS_CONTEXT_MARKER),
            )
            .all()
        )
        assert len(messages_before) == 1

        # Cleanup with min_age_seconds=0 to delete immediately (for testing)
        deleted_count = service._cleanup_stale_commis_context(thread.id, min_age_seconds=0)
        db_session.commit()

        assert deleted_count == 1

        # Verify message is gone
        messages_after = (
            db_session.query(ThreadMessage)
            .filter(
                ThreadMessage.thread_id == thread.id,
                ThreadMessage.content.contains(RECENT_COMMIS_CONTEXT_MARKER),
            )
            .all()
        )
        assert len(messages_after) == 0

    def test_cleanup_does_not_affect_other_messages(self, db_session, test_user, temp_artifact_path):
        """Cleanup should only delete messages with the marker."""
        from zerg.crud import crud
        from zerg.models.models import ThreadMessage
        from zerg.services.oikos_service import RECENT_COMMIS_CONTEXT_MARKER

        # Create oikos fiche and thread
        service = OikosService(db_session)
        fiche = service.get_or_create_oikos_fiche(test_user.id)
        thread = service.get_or_create_oikos_thread(test_user.id, fiche)

        # Count existing messages (thread may have a system prompt)
        initial_count = (
            db_session.query(ThreadMessage)
            .filter(
                ThreadMessage.thread_id == thread.id,
            )
            .count()
        )

        # Add a normal system message (no marker)
        crud.create_thread_message(
            db=db_session,
            thread_id=thread.id,
            role="system",
            content="Important system instructions",
            processed=True,
        )
        # Add a stale context message (with marker)
        crud.create_thread_message(
            db=db_session,
            thread_id=thread.id,
            role="system",
            content=f"{RECENT_COMMIS_CONTEXT_MARKER}\n## Stale context",
            processed=True,
        )
        db_session.commit()

        # Verify marker message exists
        marker_messages = (
            db_session.query(ThreadMessage)
            .filter(
                ThreadMessage.thread_id == thread.id,
                ThreadMessage.content.contains(RECENT_COMMIS_CONTEXT_MARKER),
            )
            .all()
        )
        assert len(marker_messages) == 1

        # Cleanup with min_age_seconds=0 to delete immediately (for testing)
        deleted_count = service._cleanup_stale_commis_context(thread.id, min_age_seconds=0)
        db_session.commit()

        assert deleted_count == 1

        # Verify marker message is gone
        marker_messages_after = (
            db_session.query(ThreadMessage)
            .filter(
                ThreadMessage.thread_id == thread.id,
                ThreadMessage.content.contains(RECENT_COMMIS_CONTEXT_MARKER),
            )
            .all()
        )
        assert len(marker_messages_after) == 0

        # Verify our "Important system instructions" message still exists
        important_msg = (
            db_session.query(ThreadMessage)
            .filter(
                ThreadMessage.thread_id == thread.id,
                ThreadMessage.content.contains("Important system instructions"),
            )
            .first()
        )
        assert important_msg is not None

    def test_cleanup_respects_min_age_for_race_condition_protection(self, db_session, test_user, temp_artifact_path):
        """Fresh context messages (< min_age) should NOT be deleted.

        This prevents race conditions where concurrent requests could
        delete each other's freshly injected context.
        """
        from zerg.crud import crud
        from zerg.models.models import ThreadMessage
        from zerg.services.oikos_service import RECENT_COMMIS_CONTEXT_MARKER

        # Create oikos fiche and thread
        service = OikosService(db_session)
        fiche = service.get_or_create_oikos_fiche(test_user.id)
        thread = service.get_or_create_oikos_thread(test_user.id, fiche)

        # Add a context message (just created, so fresh)
        crud.create_thread_message(
            db=db_session,
            thread_id=thread.id,
            role="system",
            content=f"{RECENT_COMMIS_CONTEXT_MARKER}\n## Fresh context",
            processed=True,
        )
        db_session.commit()

        # Cleanup with default min_age_seconds=5.0
        # Message was just created, so it should NOT be deleted
        deleted_count = service._cleanup_stale_commis_context(thread.id)
        db_session.commit()

        # Should not delete fresh messages (race condition protection)
        assert deleted_count == 0

        # Message should still exist
        messages = (
            db_session.query(ThreadMessage)
            .filter(
                ThreadMessage.thread_id == thread.id,
                ThreadMessage.content.contains(RECENT_COMMIS_CONTEXT_MARKER),
            )
            .all()
        )
        assert len(messages) == 1

    def test_cleanup_removes_older_duplicates_but_keeps_fresh(self, db_session, test_user, temp_artifact_path):
        """Back-to-back requests should not accumulate multiple context blocks.

        When there are multiple context messages and the newest is fresh,
        only the newest should be kept (all older ones deleted).
        """
        from zerg.crud import crud
        from zerg.models.models import ThreadMessage
        from zerg.services.oikos_service import RECENT_COMMIS_CONTEXT_MARKER

        # Create oikos fiche and thread
        service = OikosService(db_session)
        fiche = service.get_or_create_oikos_fiche(test_user.id)
        thread = service.get_or_create_oikos_thread(test_user.id, fiche)

        # Simulate back-to-back requests by adding multiple context messages
        # First message (older)
        msg1 = crud.create_thread_message(
            db=db_session,
            thread_id=thread.id,
            role="system",
            content=f"{RECENT_COMMIS_CONTEXT_MARKER}\n## Old context 1",
            processed=True,
        )
        # Second message (newer, fresh - should be kept)
        msg2 = crud.create_thread_message(
            db=db_session,
            thread_id=thread.id,
            role="system",
            content=f"{RECENT_COMMIS_CONTEXT_MARKER}\n## Fresh context 2",
            processed=True,
        )
        db_session.commit()

        # Verify both exist
        before = (
            db_session.query(ThreadMessage)
            .filter(
                ThreadMessage.thread_id == thread.id,
                ThreadMessage.content.contains(RECENT_COMMIS_CONTEXT_MARKER),
            )
            .all()
        )
        assert len(before) == 2

        # Cleanup with default min_age - newest is fresh so kept, older deleted
        deleted_count = service._cleanup_stale_commis_context(thread.id)
        db_session.commit()

        # Should have deleted the older one
        assert deleted_count == 1

        # Only the fresh one should remain
        after = (
            db_session.query(ThreadMessage)
            .filter(
                ThreadMessage.thread_id == thread.id,
                ThreadMessage.content.contains(RECENT_COMMIS_CONTEXT_MARKER),
            )
            .all()
        )
        assert len(after) == 1
        assert "Fresh context 2" in after[0].content
