"""Tests for trace coverage reporting."""

from __future__ import annotations

import uuid

from zerg.models.course_event import CourseEvent
from zerg.models.enums import CourseStatus
from zerg.models.enums import CourseTrigger
from zerg.models.llm_audit import LLMAuditLog
from zerg.models.models import Course
from zerg.models.models import CommisJob
from zerg.models_config import TEST_MODEL_ID
from zerg.services.trace_coverage import build_trace_coverage_report


def test_trace_coverage_report_counts(db_session, sample_fiche, sample_thread, _dev_user):
    """Report should count trace_id coverage across core tables and events."""
    trace_id = uuid.uuid4()

    run = Course(
        fiche_id=sample_fiche.id,
        thread_id=sample_thread.id,
        status=CourseStatus.RUNNING,
        trigger=CourseTrigger.API,
        trace_id=trace_id,
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    job = CommisJob(
        owner_id=_dev_user.id,
        concierge_course_id=run.id,
        task="Test task",
        model=TEST_MODEL_ID,
        status="queued",
        trace_id=trace_id,
    )
    db_session.add(job)

    audit = LLMAuditLog(
        course_id=run.id,
        commis_id=None,
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
        CourseEvent(
            course_id=run.id,
            event_type="concierge_started",
            payload={"trace_id": str(trace_id), "course_id": run.id},
        )
    )
    db_session.add(
        CourseEvent(
            course_id=run.id,
            event_type="commis_started",
            payload={"course_id": run.id},
        )
    )
    db_session.commit()

    report = build_trace_coverage_report(db_session)
    buckets = {bucket["name"]: bucket for bucket in report["buckets"]}

    assert buckets["courses"]["total"] == 1
    assert buckets["courses"]["with_trace"] == 1
    assert buckets["commis_jobs"]["total"] == 1
    assert buckets["commis_jobs"]["with_trace"] == 1
    assert buckets["llm_audit_log"]["total"] == 1
    assert buckets["llm_audit_log"]["with_trace"] == 1
    assert buckets["course_events"]["total"] == 2
    assert buckets["course_events"]["with_trace"] == 1
    assert buckets["course_events"]["pct"] == 50.0

    event_types = {bucket["name"]: bucket for bucket in report["event_types"]}
    assert event_types["concierge_started"]["with_trace"] == 1
    assert event_types["commis_started"]["with_trace"] == 0
