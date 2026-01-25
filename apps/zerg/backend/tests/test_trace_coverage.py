"""Tests for trace coverage reporting."""

from __future__ import annotations

import uuid

from zerg.models.agent_run_event import AgentRunEvent
from zerg.models.enums import RunStatus
from zerg.models.enums import RunTrigger
from zerg.models.llm_audit import LLMAuditLog
from zerg.models.models import AgentRun
from zerg.models.models import WorkerJob
from zerg.models_config import TEST_MODEL_ID
from zerg.services.trace_coverage import build_trace_coverage_report


def test_trace_coverage_report_counts(db_session, sample_agent, sample_thread, _dev_user):
    """Report should count trace_id coverage across core tables and events."""
    trace_id = uuid.uuid4()

    run = AgentRun(
        agent_id=sample_agent.id,
        thread_id=sample_thread.id,
        status=RunStatus.RUNNING,
        trigger=RunTrigger.API,
        trace_id=trace_id,
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    job = WorkerJob(
        owner_id=_dev_user.id,
        supervisor_run_id=run.id,
        task="Test task",
        model=TEST_MODEL_ID,
        status="queued",
        trace_id=trace_id,
    )
    db_session.add(job)

    audit = LLMAuditLog(
        run_id=run.id,
        worker_id=None,
        thread_id=sample_thread.id,
        owner_id=_dev_user.id,
        trace_id=trace_id,
        span_id=uuid.uuid4(),
        phase="initial",
        model=TEST_MODEL_ID,
        messages=[],
        message_count=0,
        input_tokens=0,
        response_content="ok",
        response_tool_calls=None,
        output_tokens=0,
        reasoning_tokens=0,
        duration_ms=12,
        checkpoint_id=None,
        error=None,
    )
    db_session.add(audit)

    db_session.add(
        AgentRunEvent(
            run_id=run.id,
            event_type="concierge_started",
            payload={"trace_id": str(trace_id), "run_id": run.id},
        )
    )
    db_session.add(
        AgentRunEvent(
            run_id=run.id,
            event_type="commis_started",
            payload={"run_id": run.id},
        )
    )
    db_session.commit()

    report = build_trace_coverage_report(db_session)
    buckets = {bucket["name"]: bucket for bucket in report["buckets"]}

    assert buckets["agent_runs"]["total"] == 1
    assert buckets["agent_runs"]["with_trace"] == 1
    assert buckets["worker_jobs"]["total"] == 1
    assert buckets["worker_jobs"]["with_trace"] == 1
    assert buckets["llm_audit_log"]["total"] == 1
    assert buckets["llm_audit_log"]["with_trace"] == 1
    assert buckets["agent_run_events"]["total"] == 2
    assert buckets["agent_run_events"]["with_trace"] == 1
    assert buckets["agent_run_events"]["pct"] == 50.0

    event_types = {bucket["name"]: bucket for bucket in report["event_types"]}
    assert event_types["concierge_started"]["with_trace"] == 1
    assert event_types["commis_started"]["with_trace"] == 0
