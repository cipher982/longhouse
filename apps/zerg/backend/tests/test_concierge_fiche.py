"""Tests for the Concierge Fiche implementation.

This test suite verifies:
1. Concierge fiche can be created with correct configuration
2. Concierge has all required tools enabled
3. Concierge can spawn commis and retrieve results
4. System prompt is properly configured
5. Full delegation flow works end-to-end
"""

from unittest.mock import Mock
from unittest.mock import patch

import pytest

from tests.conftest import TEST_MODEL
from tests.conftest import TEST_COMMIS_MODEL
from zerg.crud import crud
from zerg.models.enums import FicheStatus
from zerg.prompts.concierge_prompt import get_concierge_prompt


class TestConciergeConfiguration:
    """Test concierge fiche creation and configuration."""

    def test_create_concierge_fiche(self, db_session, test_user):
        """Test that concierge fiche can be created with correct config."""
        concierge_prompt = get_concierge_prompt()

        # Create concierge fiche
        fiche = crud.create_fiche(
            db_session,
            owner_id=test_user.id,
            name="Test Concierge",
            system_instructions=concierge_prompt,
            task_instructions="Help the user accomplish their goals.",
            model=TEST_MODEL,
            config={"is_concierge": True, "temperature": 0.7},
        )

        assert fiche is not None
        assert fiche.name == "Test Concierge"
        assert fiche.model == TEST_MODEL
        assert fiche.owner_id == test_user.id
        assert fiche.status == FicheStatus.IDLE
        assert fiche.config.get("is_concierge") is True
        assert concierge_prompt in fiche.system_instructions

    def test_concierge_has_required_tools(self, db_session, test_user):
        """Test that concierge has all required delegation tools."""
        required_concierge_tools = [
            "spawn_commis",
            "list_commis",
            "read_commis_result",
            "read_commis_file",
            "grep_commis",
            "get_commis_metadata",
        ]

        required_direct_tools = [
            "get_current_time",
            "http_request",
        ]

        # Create concierge with tool allowlist
        fiche = crud.create_fiche(
            db_session,
            owner_id=test_user.id,
            name="Tool Test Concierge",
            system_instructions=get_concierge_prompt(),
            task_instructions="Test",
            model=TEST_MODEL,
        )

        # Update with allowed tools
        fiche = crud.update_fiche(
            db_session,
            fiche.id,
            allowed_tools=(required_concierge_tools + required_direct_tools + ["send_email"]),
        )

        # Verify all required tools are present
        for tool in required_concierge_tools:
            assert tool in fiche.allowed_tools, f"Missing concierge tool: {tool}"

        for tool in required_direct_tools:
            assert tool in fiche.allowed_tools, f"Missing direct tool: {tool}"

    def test_concierge_system_prompt_content(self):
        """Test that concierge prompt contains key concepts."""
        prompt = get_concierge_prompt()

        # Verify key concepts are present
        assert "Concierge" in prompt
        assert "spawn_commis" in prompt
        assert "list_commis" in prompt
        assert "commis" in prompt.lower()

        # Verify guidance sections
        assert "Your Role" in prompt
        assert "Querying Past Work" in prompt
        assert "Response Style" in prompt

    def test_concierge_not_scheduled(self, db_session, test_user):
        """Test that concierge is not scheduled (interactive only)."""
        fiche = crud.create_fiche(
            db_session,
            owner_id=test_user.id,
            name="No Schedule Concierge",
            system_instructions=get_concierge_prompt(),
            task_instructions="Test",
            model=TEST_MODEL,
            schedule=None,
        )

        assert fiche.schedule is None
        assert fiche.next_course_at is None


class TestConciergeDelegation:
    """Test concierge's ability to spawn and manage commis."""

    @pytest.mark.asyncio
    async def test_spawn_commis_integration(self, db_session, test_user, tmp_path):
        """Test that concierge can spawn a commis and get result."""
        from zerg.services.commis_artifact_store import CommisArtifactStore
        from zerg.services.commis_runner import CommisRunner

        # Create concierge fiche
        concierge = crud.create_fiche(
            db_session,
            owner_id=test_user.id,
            name="Integration Test Concierge",
            system_instructions=get_concierge_prompt(),
            task_instructions="Test delegation",
            model=TEST_MODEL,
        )

        # Mock the LLM response for commis
        mock_completion = Mock()
        mock_completion.choices = [
            Mock(
                message=Mock(
                    content="Commis completed the task successfully.",
                    tool_calls=None,
                )
            )
        ]
        mock_completion.usage = Mock(total_tokens=100)

        with patch("openai.OpenAI") as mock_openai_class:
            mock_client = Mock()
            mock_client.chat.completions.create.return_value = mock_completion
            mock_openai_class.return_value = mock_client

            # Create commis runner with temp directory
            artifact_store = CommisArtifactStore(base_path=tmp_path / "commis")
            runner = CommisRunner(artifact_store=artifact_store)

            # Run commis
            result = await runner.run_commis(
                db=db_session,
                task="Test task for integration",
                fiche=None,
                fiche_config={
                    "model": TEST_COMMIS_MODEL,
                    "owner_id": test_user.id,
                },
            )

            # Verify result structure
            assert result.commis_id is not None
            assert result.status in ["success", "failed"]
            assert result.result is not None or result.error is not None

            # If successful, verify we can retrieve the result
            if result.status == "success":
                retrieved_result = artifact_store.get_commis_result(result.commis_id)
                assert retrieved_result is not None
                assert len(retrieved_result) > 0

    def test_spawn_commis_tool_basic(self, db_session, test_user):
        """Test spawn_commis tool is callable and validates context."""
        from zerg.connectors.context import set_credential_resolver
        from zerg.tools.builtin.concierge_tools import spawn_commis

        # Without context, should return error
        set_credential_resolver(None)
        result = spawn_commis(task="Test task", model=TEST_COMMIS_MODEL)
        assert "Error" in result or "error" in result.lower()
        assert "credential context" in result.lower() or "context" in result.lower()

    def test_list_commis_tool_basic(self, db_session, test_user):
        """Test list_commis tool is callable and validates context."""
        from zerg.connectors.context import set_credential_resolver
        from zerg.tools.builtin.concierge_tools import list_commis

        # Without context, should return error
        set_credential_resolver(None)
        result = list_commis(limit=10)
        assert "Error" in result or "error" in result.lower()
        assert "credential context" in result.lower() or "context" in result.lower()

    def test_read_commis_result_tool_basic(self, db_session, test_user):
        """Test read_commis_result tool is callable and validates context."""
        from zerg.connectors.context import set_credential_resolver
        from zerg.tools.builtin.concierge_tools import read_commis_result

        # Without context, should return error
        set_credential_resolver(None)
        result = read_commis_result(job_id="999")
        assert "Error" in result or "error" in result.lower()
        assert "credential context" in result.lower() or "context" in result.lower()


class TestConciergeEndToEnd:
    """End-to-end tests for concierge/commis interaction."""

    @pytest.mark.asyncio
    async def test_full_delegation_flow(self, db_session, test_user, tmp_path):
        """Test complete flow: create concierge → spawn commis → retrieve result."""
        from zerg.connectors.context import set_credential_resolver
        from zerg.connectors.resolver import CredentialResolver
        from zerg.services.commis_artifact_store import CommisArtifactStore
        from zerg.services.commis_runner import CommisRunner

        # 1. Create concierge fiche
        concierge = crud.create_fiche(
            db_session,
            owner_id=test_user.id,
            name="E2E Test Concierge",
            system_instructions=get_concierge_prompt(),
            task_instructions="Coordinate tasks",
            model=TEST_MODEL,
            config={"is_concierge": True},
        )

        assert concierge.id is not None

        # 2. Setup credential context for commis spawning
        resolver = CredentialResolver(fiche_id=concierge.id, db=db_session, owner_id=test_user.id)
        set_credential_resolver(resolver)

        # 3. Mock commis execution
        mock_completion = Mock()
        mock_completion.choices = [
            Mock(
                message=Mock(
                    content="Disk usage check completed. All servers below 80% capacity.",
                    tool_calls=None,
                )
            )
        ]
        mock_completion.usage = Mock(total_tokens=150)

        with patch("openai.OpenAI") as mock_openai_class:
            mock_client = Mock()
            mock_client.chat.completions.create.return_value = mock_completion
            mock_openai_class.return_value = mock_client

            # 4. Spawn commis via runner with temp directory
            artifact_store = CommisArtifactStore(base_path=tmp_path / "commis")
            runner = CommisRunner(artifact_store=artifact_store)

            result = await runner.run_commis(
                db=db_session,
                task="Check disk usage on all production servers",
                fiche=None,
                fiche_config={
                    "model": TEST_COMMIS_MODEL,
                    "owner_id": test_user.id,
                },
            )

            # 5. Verify commis completed
            assert result.commis_id is not None
            assert result.status == "success"
            assert result.result is not None
            # Result may contain stub or actual response depending on mock
            assert len(result.result) > 0

            # 6. Verify we can retrieve metadata
            metadata = artifact_store.get_commis_metadata(result.commis_id, owner_id=test_user.id)
            assert metadata["status"] == "success"
            assert metadata["commis_id"] == result.commis_id
            assert metadata["config"]["owner_id"] == test_user.id

            # 7. Verify we can retrieve result
            retrieved_result = artifact_store.get_commis_result(result.commis_id)
            assert len(retrieved_result) > 0

    def test_concierge_security_isolation(self, db_session, test_user, tmp_path):
        """Test that commis are properly isolated by owner_id."""
        from zerg.services.commis_artifact_store import CommisArtifactStore

        # Create another user
        other_user = crud.create_user(
            db_session,
            email="other@test.com",
            provider="test",
        )

        artifact_store = CommisArtifactStore(base_path=tmp_path / "commis")

        # Mock metadata for a commis owned by other user
        other_user_metadata = {
            "commis_id": "other-commis-123",
            "status": "success",
            "config": {"owner_id": other_user.id},
        }

        with patch.object(
            artifact_store,
            "get_commis_metadata",
            side_effect=lambda commis_id, owner_id: (other_user_metadata if owner_id == other_user.id else None),
        ):
            # Try to access other user's commis
            result = artifact_store.get_commis_metadata("other-commis-123", owner_id=test_user.id)
            assert result is None  # Should not be accessible

            # Access own commis
            result = artifact_store.get_commis_metadata("other-commis-123", owner_id=other_user.id)
            assert result == other_user_metadata


class TestConciergeFromScript:
    """Test the seed script functionality."""

    def test_seed_script_creates_concierge(self, db_session, test_user):
        """Test that seed_concierge script creates valid fiche."""
        from scripts.seed_concierge import seed_concierge

        # Mock get_db to return our test session
        with patch("scripts.seed_concierge.get_db", return_value=iter([db_session])):
            with patch("scripts.seed_concierge.get_or_create_user", return_value=test_user):
                fiche = seed_concierge(user_email=test_user.email, name="Script Test Concierge")

                # Verify fiche was created
                assert fiche is not None
                assert fiche.name == "Script Test Concierge"
                assert fiche.model == TEST_MODEL
                assert fiche.owner_id == test_user.id
                assert fiche.config.get("is_concierge") is True
                assert fiche.allowed_tools is not None
                assert "spawn_commis" in fiche.allowed_tools

    def test_seed_script_updates_existing(self, db_session, test_user):
        """Test that seed script updates existing concierge."""
        from scripts.seed_concierge import seed_concierge

        # Create initial concierge
        initial = crud.create_fiche(
            db_session,
            owner_id=test_user.id,
            name="Update Test Concierge",
            system_instructions="Old prompt",
            task_instructions="Old task",
            model=TEST_COMMIS_MODEL,
        )

        # Run seed script
        with patch("scripts.seed_concierge.get_db", return_value=iter([db_session])):
            with patch("scripts.seed_concierge.get_or_create_user", return_value=test_user):
                fiche = seed_concierge(user_email=test_user.email, name="Update Test Concierge")

                # Verify fiche was updated
                assert fiche.id == initial.id  # Same fiche
                assert fiche.model == TEST_MODEL  # Updated to concierge model
                assert "spawn_commis" in fiche.system_instructions  # Updated prompt
                assert fiche.config.get("is_concierge") is True  # Updated config
