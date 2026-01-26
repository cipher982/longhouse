"""Eval runner for executing test cases against ConciergeService.

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
    from zerg.services.concierge_service import ConciergeService
    from zerg.services.concierge_service import ConciergeCourseResult


@dataclass
class EvalMetrics:
    """Metrics captured from a concierge run."""

    status: str  # 'success' | 'failed' | 'deferred'
    latency_ms: int
    result_text: str | None
    error: str | None
    total_tokens: int
    commis_spawned: int
    tools_called: list[str]
    course_id: int
    thread_id: int
    _db_session: object = None  # Injected for commis asserters


class EvalRunner:
    """Runner for executing eval test cases.

    This class wraps ConciergeService and provides:
    - Variant override support (model, reasoning_effort)
    - Metrics collection
    - Hermetic mode enforcement
    """

    def __init__(self, concierge_service: ConciergeService, owner_id: int):
        """Initialize the eval runner.

        Args:
            concierge_service: ConciergeService instance
            owner_id: User ID to run tests as
        """
        self.concierge_service = concierge_service
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
        runner = EvalRunner(self.concierge_service, self.owner_id)
        runner._overrides = {
            "model": variant_config.get("model"),
            "temperature": variant_config.get("temperature", 0.0),
            "reasoning_effort": variant_config.get("reasoning_effort", "none"),
            "prompt_version": variant_config.get("prompt_version"),
            "custom_prompt": variant_config.get("overrides", {}).get("concierge_prompt"),
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
            # Get concierge fiche and thread using service methods
            concierge = self.concierge_service.get_or_create_concierge_fiche(self.owner_id)
            thread = self.concierge_service.get_or_create_concierge_thread(self.owner_id, concierge)

            # Clear existing messages (fresh conversation) - keep system message
            from zerg.models.thread import ThreadMessage

            self.concierge_service.db.query(ThreadMessage).filter(
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
                self.concierge_service.db.add(thread_msg)

            self.concierge_service.db.commit()

            # Last message is the actual task
            final_message = messages[-1]
            if final_message["role"] != "user":
                raise ValueError("Last message in multi-turn conversation must be from 'user' role")

            task = final_message["content"]

        # Run concierge
        result: ConciergeCourseResult = await self.concierge_service.run_concierge(
            owner_id=self.owner_id,
            task=task,
            timeout=timeout,
            model_override=model_override,
            reasoning_effort=reasoning_effort,
        )

        # Drain queued commis jobs in-process so commis/artifact assertions can
        # inspect results deterministically (no background processor in evals).
        # This runs for BOTH hermetic and live mode - live mode uses real LLM for
        # concierge decisions, but still needs commis to complete synchronously.
        await self._process_queued_commis_jobs(concierge_course_id=result.course_id)

        # Calculate latency
        latency_ms = int((time.time() - start_time) * 1000)

        # Get token count from database
        from zerg.models import Course

        run = self.concierge_service.db.query(Course).filter(Course.id == result.course_id).first()
        total_tokens = run.total_tokens if run and run.total_tokens else 0

        # Count commis spawned
        from zerg.models import CommisJob

        commis_spawned = (
            self.concierge_service.db.query(CommisJob).filter(CommisJob.concierge_course_id == result.course_id).count()
        )

        # Collect tools called (from durable run events)
        from zerg.models.course_event import CourseEvent

        events = self.concierge_service.db.query(CourseEvent).filter(CourseEvent.course_id == result.course_id).all()
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
            commis_spawned=commis_spawned,
            tools_called=tools_called,
            course_id=result.course_id,
            thread_id=result.thread_id,
            _db_session=self.concierge_service.db,
        )

    async def _process_queued_commis_jobs(self, concierge_course_id: int) -> None:
        """Run queued commis jobs synchronously (eval-only).

        In production, commis jobs are processed by the CommisJobProcessor loop.
        Eval tests run in-process without that loop, so we execute queued jobs
        directly to make commis artifacts available for assertions.
        """
        from datetime import datetime, timezone

        from zerg.models import CommisJob
        from zerg.services.commis_artifact_store import CommisArtifactStore
        from zerg.services.commis_runner import CommisRunner

        db = self.concierge_service.db

        jobs = (
            db.query(CommisJob)
            .filter(CommisJob.concierge_course_id == concierge_course_id, CommisJob.status == "queued")
            .order_by(CommisJob.created_at)
            .all()
        )

        if not jobs:
            return

        artifact_store = CommisArtifactStore()
        runner = CommisRunner(artifact_store=artifact_store)

        from zerg.utils.time import utc_now_naive

        for job in jobs:
            job.status = "running"
            job.started_at = utc_now_naive()
            db.commit()

            # Opt-in: allow a test case to request a scripted commis run so we can
            # deterministically generate commis tool events in hermetic mode.
            use_scripted = "[eval:scripted_commis]" in (job.task or "")

            fiche_config: dict = {"model": job.model, "owner_id": job.owner_id}
            if use_scripted:
                fiche_config["model"] = "gpt-scripted"
                fiche_config["allowed_tools"] = ["get_current_time"]

            try:
                result = await runner.run_commis(
                    db=db,
                    task=job.task,
                    fiche=None,
                    fiche_config=fiche_config,
                    timeout=60,
                    event_context={"course_id": concierge_course_id},
                    job_id=job.id,
                )

                # Ensure job is still attached to session after run_commis commits
                db.refresh(job)

                job.commis_id = result.commis_id
                job.finished_at = utc_now_naive()

                if result.status == "success":
                    job.status = "success"
                else:
                    job.status = "failed"
                    job.error = result.error or "Unknown error"

                db.commit()
            except Exception as e:
                db.rollback()
                # Re-fetch if needed
                db.refresh(job)
                job.status = "failed"
                job.error = str(e)
                job.finished_at = utc_now_naive()
                db.commit()
