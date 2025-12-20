"""
Workflow Scheduler Service for managing scheduled workflow executions.

This service integrates with the existing SchedulerService to handle:
- Scheduling workflows based on trigger nodes
- Managing manual vs scheduled workflow executions
- Integration with the workflow execution engine
"""

import logging
from typing import Any
from typing import Dict

from apscheduler.triggers.cron import CronTrigger

from zerg.crud import crud
from zerg.database import get_session_factory
from zerg.services.scheduler_service import scheduler_service
from zerg.services.workflow_engine import workflow_engine

logger = logging.getLogger(__name__)


class WorkflowScheduler:
    """Service for managing scheduled workflow executions."""

    def __init__(self):
        self.session_factory = get_session_factory()

    async def schedule_workflow(self, workflow_id: int, cron_expression: str, trigger_config: Dict[str, Any] = None) -> bool:
        """
        Schedule a workflow to run according to a cron expression.

        Args:
            workflow_id: The ID of the workflow to schedule
            cron_expression: The cron expression defining when to run
            trigger_config: Additional configuration from trigger node

        Returns:
            bool: True if scheduled successfully, False otherwise
        """
        try:
            # Validate workflow exists and is active
            with self.session_factory() as db:
                workflow = crud.get_workflow(db, workflow_id)
                if not workflow or not workflow.is_active:
                    logger.error(f"Workflow {workflow_id} not found or inactive")
                    return False

            # Remove any existing schedule for this workflow
            self.unschedule_workflow(workflow_id)

            # Add new scheduled job
            job_id = f"workflow_{workflow_id}"
            scheduler_service.scheduler.add_job(
                self._execute_scheduled_workflow,
                CronTrigger.from_crontab(cron_expression),
                args=[workflow_id, trigger_config or {}],
                id=job_id,
                replace_existing=True,
            )

            logger.info(f"Scheduled workflow {workflow_id} with cron: {cron_expression}")
            return True

        except Exception as e:
            logger.error(f"Error scheduling workflow {workflow_id}: {e}")
            return False

    def unschedule_workflow(self, workflow_id: int) -> bool:
        """
        Remove any existing scheduled execution for a workflow.

        Args:
            workflow_id: The ID of the workflow to unschedule

        Returns:
            bool: True if unscheduled successfully, False otherwise
        """
        try:
            job_id = f"workflow_{workflow_id}"
            if scheduler_service.scheduler.get_job(job_id):
                scheduler_service.scheduler.remove_job(job_id)
                logger.info(f"Unscheduled workflow {workflow_id}")
                return True
            return False
        except Exception as e:
            logger.error(f"Error unscheduling workflow {workflow_id}: {e}")
            return False

    async def _execute_scheduled_workflow(self, workflow_id: int, trigger_config: Dict[str, Any]):
        """
        Execute a workflow that was triggered by a scheduled job.

        This is the function called by APScheduler when a scheduled workflow needs to run.
        """
        logger.info(f"Executing scheduled workflow {workflow_id}")

        try:
            # Add trigger context to indicate this is a scheduled execution
            # This allows the workflow engine to track the execution source
            execution_id = await workflow_engine.execute_workflow(workflow_id, trigger_type="schedule", trigger_config=trigger_config)

            logger.info(f"Scheduled workflow {workflow_id} execution started with ID: {execution_id}")

        except Exception as e:
            logger.error(f"Failed to execute scheduled workflow {workflow_id}: {e}")

    def get_scheduled_workflows(self) -> Dict[int, Dict[str, Any]]:
        """
        Get all currently scheduled workflows.

        Returns:
            Dict mapping workflow_id to schedule info
        """
        scheduled = {}

        for job in scheduler_service.scheduler.get_jobs():
            if job.id.startswith("workflow_"):
                try:
                    workflow_id = int(job.id.replace("workflow_", ""))
                    scheduled[workflow_id] = {
                        "job_id": job.id,
                        "next_run_time": job.next_run_time,
                        "trigger": str(job.trigger),
                    }
                except (ValueError, AttributeError) as e:
                    logger.warning(f"Invalid workflow job ID format: {job.id}, error: {e}")

        return scheduled

    def is_workflow_scheduled(self, workflow_id: int) -> bool:
        """Check if a workflow is currently scheduled."""
        job_id = f"workflow_{workflow_id}"
        return scheduler_service.scheduler.get_job(job_id) is not None


# Global instance of the workflow scheduler
workflow_scheduler = WorkflowScheduler()
