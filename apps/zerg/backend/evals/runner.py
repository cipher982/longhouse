"""Eval runner for executing test cases against SupervisorService.

This module provides the EvalRunner class which:
- Executes eval cases in hermetic mode
- Captures metrics (latency, status, tokens)
- Returns results for assertion
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zerg.services.supervisor_service import SupervisorService
    from zerg.services.supervisor_service import SupervisorRunResult


@dataclass
class EvalMetrics:
    """Metrics captured from a supervisor run."""

    status: str  # 'success' | 'failed' | 'deferred'
    latency_ms: int
    result_text: str | None
    error: str | None
    total_tokens: int
    workers_spawned: int
    run_id: int
    thread_id: int


class EvalRunner:
    """Runner for executing eval test cases.

    This class wraps SupervisorService and provides:
    - Variant override support (model, reasoning_effort)
    - Metrics collection
    - Hermetic mode enforcement
    """

    def __init__(self, supervisor_service: SupervisorService, owner_id: int):
        """Initialize the eval runner.

        Args:
            supervisor_service: SupervisorService instance
            owner_id: User ID to run tests as
        """
        self.supervisor_service = supervisor_service
        self.owner_id = owner_id
        self._overrides = {}

    def with_variant(self, variant_name: str, variants: dict) -> EvalRunner:
        """Return NEW instance with variant overrides applied (immutable).

        Args:
            variant_name: Name of variant to use
            variants: Dict of variant configs from YAML

        Returns:
            New EvalRunner instance with overrides
        """
        variant_config = variants.get(variant_name, {})

        # Create new instance (no mutation)
        runner = EvalRunner(self.supervisor_service, self.owner_id)
        runner._overrides = {
            "model": variant_config.get("model"),
            "temperature": variant_config.get("temperature", 0.0),
            "reasoning_effort": variant_config.get("reasoning_effort", "none"),
        }
        return runner

    async def run_case(
        self,
        task: str,
        timeout: int = 120,
    ) -> EvalMetrics:
        """Execute a single eval case.

        Args:
            task: The task/question to execute
            timeout: Maximum execution time in seconds

        Returns:
            EvalMetrics with captured metrics
        """
        # Apply overrides from variant
        model_override = self._overrides.get("model")
        reasoning_effort = self._overrides.get("reasoning_effort", "none")

        # Record start time
        start_time = time.time()

        # Run supervisor
        result: SupervisorRunResult = await self.supervisor_service.run_supervisor(
            owner_id=self.owner_id,
            task=task,
            timeout=timeout,
            model_override=model_override,
            reasoning_effort=reasoning_effort,
        )

        # Calculate latency
        latency_ms = int((time.time() - start_time) * 1000)

        # Get token count from database
        from zerg.models.models import AgentRun

        run = self.supervisor_service.db.query(AgentRun).filter(AgentRun.id == result.run_id).first()
        total_tokens = run.total_tokens if run and run.total_tokens else 0

        # Count workers spawned
        from zerg.models.models import WorkerJob

        workers_spawned = (
            self.supervisor_service.db.query(WorkerJob).filter(WorkerJob.supervisor_run_id == result.run_id).count()
        )

        return EvalMetrics(
            status=result.status,
            latency_ms=latency_ms,
            result_text=result.result,
            error=result.error,
            total_tokens=total_tokens,
            workers_spawned=workers_spawned,
            run_id=result.run_id,
            thread_id=result.thread_id,
        )
