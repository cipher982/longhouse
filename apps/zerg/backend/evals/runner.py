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
    tools_called: list[str]
    run_id: int
    thread_id: int
    _db_session: object = None  # Injected for worker asserters


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
            variants: Dict of variant configs from YAML (already converted to dicts)

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
            "prompt_version": variant_config.get("prompt_version"),
            "custom_prompt": variant_config.get("overrides", {}).get("supervisor_prompt"),
        }
        return runner

    async def run_case(
        self,
        task: str | None = None,
        messages: list[dict] | None = None,
        timeout: int = 120,
    ) -> EvalMetrics:
        """Execute a single eval case.

        Args:
            task: The task/question to execute (single-turn)
            messages: Full conversation history (multi-turn)
            timeout: Maximum execution time in seconds

        Returns:
            EvalMetrics with captured metrics
        """
        # Validate input
        if task is None and messages is None:
            raise ValueError("Either 'task' or 'messages' must be provided")
        if task is not None and messages is not None:
            raise ValueError("Cannot provide both 'task' and 'messages'")

        # Apply overrides from variant
        model_override = self._overrides.get("model")
        reasoning_effort = self._overrides.get("reasoning_effort", "none")

        # Record start time
        start_time = time.time()

        # Handle multi-turn conversation
        if messages:
            # Get supervisor agent and thread using service methods
            supervisor = self.supervisor_service.get_or_create_supervisor_agent(self.owner_id)
            thread = self.supervisor_service.get_or_create_supervisor_thread(self.owner_id, supervisor)

            # Clear existing messages (fresh conversation) - keep system message
            from zerg.models.thread import ThreadMessage

            self.supervisor_service.db.query(ThreadMessage).filter(
                ThreadMessage.thread_id == thread.id,
                ThreadMessage.role != "system",
            ).delete()

            # Inject conversation history (all but last message)
            from datetime import datetime, timezone

            for msg in messages[:-1]:
                thread_msg = ThreadMessage(
                    thread_id=thread.id,
                    role=msg["role"],
                    content=msg["content"],
                    sent_at=datetime.now(timezone.utc),
                    processed=True,  # Mark as processed (not new)
                )
                self.supervisor_service.db.add(thread_msg)

            self.supervisor_service.db.commit()

            # Last message is the actual task
            final_message = messages[-1]
            if final_message["role"] != "user":
                raise ValueError("Last message in multi-turn conversation must be from 'user' role")

            task = final_message["content"]

        # Run supervisor
        result: SupervisorRunResult = await self.supervisor_service.run_supervisor(
            owner_id=self.owner_id,
            task=task,
            timeout=timeout,
            model_override=model_override,
            reasoning_effort=reasoning_effort,
        )

        # Drain queued worker jobs in-process so worker/artifact assertions can
        # inspect results deterministically (no background processor in evals).
        # This runs for BOTH hermetic and live mode - live mode uses real LLM for
        # supervisor decisions, but still needs workers to complete synchronously.
        await self._process_queued_worker_jobs(supervisor_run_id=result.run_id)

        # Calculate latency
        latency_ms = int((time.time() - start_time) * 1000)

        # Get token count from database
        from zerg.models import AgentRun

        run = self.supervisor_service.db.query(AgentRun).filter(AgentRun.id == result.run_id).first()
        total_tokens = run.total_tokens if run and run.total_tokens else 0

        # Count workers spawned
        from zerg.models import WorkerJob

        workers_spawned = (
            self.supervisor_service.db.query(WorkerJob).filter(WorkerJob.supervisor_run_id == result.run_id).count()
        )

        # Collect tools called (from durable run events)
        from zerg.models.agent_run_event import AgentRunEvent

        events = self.supervisor_service.db.query(AgentRunEvent).filter(AgentRunEvent.run_id == result.run_id).all()
        tools_called: list[str] = []
        for event in events:
            payload = event.payload or {}
            tool_name = payload.get("tool_name")
            if tool_name:
                tools_called.append(tool_name)

        # Deduplicate while preserving order
        tools_called = list(dict.fromkeys(tools_called))

        return EvalMetrics(
            status=result.status,
            latency_ms=latency_ms,
            result_text=result.result,
            error=result.error,
            total_tokens=total_tokens,
            workers_spawned=workers_spawned,
            tools_called=tools_called,
            run_id=result.run_id,
            thread_id=result.thread_id,
            _db_session=self.supervisor_service.db,
        )

    async def _process_queued_worker_jobs(self, supervisor_run_id: int) -> None:
        """Run queued worker jobs synchronously (eval-only).

        In production, worker jobs are processed by the WorkerJobProcessor loop.
        Eval tests run in-process without that loop, so we execute queued jobs
        directly to make worker artifacts available for assertions.
        """
        from datetime import datetime, timezone

        from zerg.models import WorkerJob
        from zerg.services.worker_artifact_store import WorkerArtifactStore
        from zerg.services.worker_runner import WorkerRunner

        db = self.supervisor_service.db

        jobs = (
            db.query(WorkerJob)
            .filter(WorkerJob.supervisor_run_id == supervisor_run_id, WorkerJob.status == "queued")
            .order_by(WorkerJob.created_at)
            .all()
        )

        if not jobs:
            return

        artifact_store = WorkerArtifactStore()
        runner = WorkerRunner(artifact_store=artifact_store)

        for job in jobs:
            job.status = "running"
            job.started_at = datetime.now(timezone.utc)
            db.commit()

            # Opt-in: allow a test case to request a scripted worker run so we can
            # deterministically generate worker tool events in hermetic mode.
            use_scripted = "[eval:scripted_worker]" in (job.task or "")

            agent_config: dict = {"model": job.model, "owner_id": job.owner_id}
            if use_scripted:
                agent_config["model"] = "gpt-scripted"
                agent_config["allowed_tools"] = ["get_current_time"]

            try:
                result = await runner.run_worker(
                    db=db,
                    task=job.task,
                    agent=None,
                    agent_config=agent_config,
                    timeout=60,
                    event_context={"run_id": supervisor_run_id},
                    job_id=job.id,
                )

                job.worker_id = result.worker_id
                job.finished_at = datetime.now(timezone.utc)

                if result.status == "success":
                    job.status = "success"
                else:
                    job.status = "failed"
                    job.error = result.error or "Unknown error"

                db.commit()
            except Exception as e:
                job.status = "failed"
                job.error = str(e)
                job.finished_at = datetime.now(timezone.utc)
                db.commit()
