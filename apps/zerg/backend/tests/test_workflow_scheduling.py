"""
Test suite for workflow scheduling functionality.
"""

from unittest.mock import Mock
from unittest.mock import patch

import pytest

from zerg.models.models import Workflow
from zerg.models.models import WorkflowExecution
from zerg.services.workflow_engine import workflow_engine as workflow_execution_engine
from zerg.services.workflow_scheduler import workflow_scheduler


class TestWorkflowScheduler:
    """Test workflow scheduling service."""

    @pytest.mark.asyncio
    async def test_schedule_workflow_success(self, db_session):
        """Test scheduling a workflow successfully."""
        # Create a test workflow
        workflow = Workflow(
            name="Test Workflow",
            description="Test workflow for scheduling",
            canvas={"nodes": [], "edges": []},
            owner_id=1,
            is_active=True,
        )
        db_session.add(workflow)
        db_session.commit()

        # Mock the scheduler service
        with patch("zerg.services.workflow_scheduler.scheduler_service") as mock_scheduler:
            mock_scheduler.scheduler.add_job = Mock()
            mock_scheduler.scheduler.get_job = Mock(return_value=None)

            # Schedule the workflow
            result = await workflow_scheduler.schedule_workflow(
                workflow_id=workflow.id,
                cron_expression="0 9 * * *",  # Daily at 9 AM
                trigger_config={"test": "config"},
            )

            assert result is True
            mock_scheduler.scheduler.add_job.assert_called_once()

    @pytest.mark.asyncio
    async def test_schedule_nonexistent_workflow(self, db_session):
        """Test scheduling a workflow that doesn't exist."""
        result = await workflow_scheduler.schedule_workflow(workflow_id=99999, cron_expression="0 9 * * *")

        assert result is False

    def test_unschedule_workflow(self, db_session):
        """Test unscheduling a workflow."""
        # Mock the scheduler service
        with patch("zerg.services.workflow_scheduler.scheduler_service") as mock_scheduler:
            mock_job = Mock()
            mock_scheduler.scheduler.get_job = Mock(return_value=mock_job)
            mock_scheduler.scheduler.remove_job = Mock()

            # Unschedule the workflow
            result = workflow_scheduler.unschedule_workflow(workflow_id=1)

            assert result is True
            mock_scheduler.scheduler.remove_job.assert_called_once_with("workflow_1")

    def test_is_workflow_scheduled(self, db_session):
        """Test checking if a workflow is scheduled."""
        with patch("zerg.services.workflow_scheduler.scheduler_service") as mock_scheduler:
            # Test workflow is scheduled
            mock_scheduler.scheduler.get_job = Mock(return_value=Mock())
            assert workflow_scheduler.is_workflow_scheduled(1) is True

            # Test workflow is not scheduled
            mock_scheduler.scheduler.get_job = Mock(return_value=None)
            assert workflow_scheduler.is_workflow_scheduled(1) is False


class TestWorkflowEngineScheduling:
    """Test workflow execution engine with scheduling context."""

    @pytest.mark.asyncio
    async def test_execute_workflow_with_trigger_type(self, db_session):
        """Test executing workflow with trigger type tracking."""
        # Create a test workflow
        workflow = Workflow(
            name="Test Workflow",
            description="Test workflow for execution",
            canvas={"nodes": [], "edges": []},
            owner_id=1,
            is_active=True,
        )
        db_session.add(workflow)
        db_session.commit()

        # Execute workflow with schedule trigger
        execution_id = await workflow_execution_engine.execute_workflow(
            workflow_id=workflow.id, trigger_type="schedule"
        )

        # Verify execution was created with correct trigger type
        execution = db_session.query(WorkflowExecution).filter_by(id=execution_id).first()
        assert execution is not None
        assert execution.triggered_by == "schedule"
        assert execution.workflow_id == workflow.id

    @pytest.mark.skip(reason="Scheduler test needs rewrite for LangGraph engine")
    @pytest.mark.asyncio
    async def test_schedule_trigger_node_execution(self, db_session):
        """Test execution of schedule trigger node in workflow."""
        # Create a workflow with a schedule trigger node
        workflow = Workflow(
            name="Scheduled Workflow",
            description="Workflow with schedule trigger",
            canvas={
                "nodes": [
                    {
                        "id": "trigger1",
                        "type": "trigger",
                        "trigger": {
                            "type": "schedule",
                            "config": {"enabled": True, "params": {"cron": "0 9 * * *"}, "filters": []},
                        },
                        "schedule_type": "workflow",
                        "cron_expression": "0 9 * * *",
                    }
                ],
                "edges": [],
            },
            owner_id=1,
            is_active=True,
        )
        db_session.add(workflow)
        db_session.commit()

        # Mock the workflow scheduler
        with patch("zerg.services.langgraph_workflow_engine.workflow_scheduler") as mock_wf_scheduler:
            # Make the async method return a coroutine that resolves to True
            async def mock_schedule_workflow(*args, **kwargs):
                return True

            mock_wf_scheduler.schedule_workflow = mock_schedule_workflow

            # Execute the workflow
            execution_id = await workflow_execution_engine.execute_workflow(
                workflow_id=workflow.id, trigger_type="manual"
            )

        # Verify execution completed
        execution = db_session.query(WorkflowExecution).filter_by(id=execution_id).first()
        assert execution is not None
        assert execution.phase == "finished"
        assert execution.result == "success"
