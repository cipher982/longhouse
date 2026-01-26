"""Scenario seeding for deterministic demos and tests."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Iterable
from typing import Optional

import yaml
from sqlalchemy.orm import Session

from zerg.models.course_event import CourseEvent
from zerg.models.enums import CourseStatus
from zerg.models.enums import CourseTrigger
from zerg.models.enums import ThreadType
from zerg.models.models import CommisJob
from zerg.models.models import Course
from zerg.models.models import Thread
from zerg.models.models import ThreadMessage
from zerg.services.concierge_service import ConciergeService
from zerg.utils.time import utc_now

logger = logging.getLogger(__name__)

SCENARIO_DIR = Path(__file__).resolve().parent / "data"
SCENARIO_TITLE_PREFIX = "[scenario:"

_RELATIVE_RE = re.compile(r"^([+-])?\s*(\d+)\s*([smhdw])$", re.IGNORECASE)


class ScenarioError(RuntimeError):
    """Raised when a scenario file is invalid or cannot be seeded."""


def list_scenarios() -> list[str]:
    if not SCENARIO_DIR.exists():
        return []
    return sorted(path.stem for path in SCENARIO_DIR.glob("*.yaml"))


def load_scenario(name: str) -> dict[str, Any]:
    path = SCENARIO_DIR / f"{name}.yaml"
    if not path.exists():
        available = ", ".join(list_scenarios()) or "(none)"
        raise ScenarioError(f"Scenario '{name}' not found. Available: {available}")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ScenarioError(f"Scenario '{name}' must be a mapping at the top level")
    return data


def seed_scenario(
    db: Session,
    scenario_name: str,
    *,
    owner_id: int,
    clean: bool = True,
) -> dict[str, Any]:
    scenario = load_scenario(scenario_name)
    courses = scenario.get("courses") or []
    if not isinstance(courses, list):
        raise ScenarioError("Scenario 'courses' must be a list")

    if clean:
        cleanup_scenario(db, scenario_name)

    concierge_fiche = ConciergeService(db).get_or_create_concierge_fiche(owner_id)

    base_time = utc_now()
    timebase_value = scenario.get("timebase")
    if timebase_value:
        parsed_base = _parse_time(timebase_value, base_time)
        if parsed_base is not None:
            base_time = parsed_base

    counts = {"courses": 0, "messages": 0, "events": 0, "commis_jobs": 0}

    for index, course_data in enumerate(courses, start=1):
        if not isinstance(course_data, dict):
            raise ScenarioError("Each course entry must be a mapping")

        course_ref = course_data.get("id") or f"course-{index}"
        title = course_data.get("title") or course_data.get("thread_title") or course_ref
        thread_type = _coerce_enum(ThreadType, course_data.get("thread_type", ThreadType.CHAT.value)).value
        thread = Thread(
            fiche_id=concierge_fiche.id,
            title=_scenario_title(scenario_name, title),
            active=False,
            thread_type=thread_type,
            fiche_state={"scenario": scenario_name, "scenario_course": course_ref},
        )
        db.add(thread)
        db.flush()

        started_at = _parse_time(course_data.get("started_at"), base_time)
        finished_at = _parse_time(course_data.get("finished_at"), base_time)
        created_at = _parse_time(course_data.get("created_at"), base_time) or started_at or base_time
        updated_at = _parse_time(course_data.get("updated_at"), base_time) or finished_at or created_at

        status = _coerce_enum(CourseStatus, course_data.get("status", CourseStatus.RUNNING.value))
        trigger = _coerce_enum(CourseTrigger, course_data.get("trigger", CourseTrigger.MANUAL.value))

        course = Course(
            fiche_id=concierge_fiche.id,
            thread_id=thread.id,
            status=status,
            trigger=trigger,
            summary=course_data.get("summary"),
            error=course_data.get("error"),
            started_at=_to_naive(started_at),
            finished_at=_to_naive(finished_at),
            created_at=_to_naive(created_at),
            updated_at=_to_naive(updated_at),
            correlation_id=f"scenario:{scenario_name}:{course_ref}",
        )
        if started_at and finished_at:
            course.duration_ms = int((finished_at - started_at).total_seconds() * 1000)
        db.add(course)
        db.flush()

        for event in _as_list(course_data.get("events")):
            if not isinstance(event, dict):
                raise ScenarioError("Course events must be mappings")
            event_type = event.get("type")
            if not event_type:
                raise ScenarioError("Course event missing 'type'")
            event_time = _parse_time(event.get("at") or event.get("created_at"), base_time) or base_time
            payload = dict(event.get("payload") or {})
            message = event.get("message")
            if message:
                payload.setdefault("message", message)
            db.add(
                CourseEvent(
                    course_id=course.id,
                    event_type=str(event_type),
                    payload=payload,
                    created_at=_to_aware(event_time),
                )
            )
            counts["events"] += 1

        for message in _as_list(course_data.get("messages")):
            if not isinstance(message, dict):
                raise ScenarioError("Course messages must be mappings")
            role = message.get("role")
            content = message.get("content")
            if not role or content is None:
                raise ScenarioError("Course message requires 'role' and 'content'")
            sent_at = _parse_time(message.get("at") or message.get("sent_at"), base_time) or base_time
            db.add(
                ThreadMessage(
                    thread_id=thread.id,
                    role=str(role),
                    content=str(content),
                    sent_at=_to_aware(sent_at),
                    processed=bool(message.get("processed", True)),
                    internal=bool(message.get("internal", False)),
                )
            )
            counts["messages"] += 1

        for job_data in _as_list(course_data.get("commis_jobs")):
            if not isinstance(job_data, dict):
                raise ScenarioError("Commis job must be a mapping")
            task = job_data.get("task")
            if not task:
                raise ScenarioError("Commis job requires 'task'")
            job_status = job_data.get("status", "queued")
            job_started_at = _parse_time(job_data.get("started_at"), base_time)
            job_finished_at = _parse_time(job_data.get("finished_at"), base_time)
            job_created_at = _parse_time(job_data.get("created_at"), base_time) or job_started_at or base_time
            job_updated_at = _parse_time(job_data.get("updated_at"), base_time) or job_finished_at or job_created_at
            db.add(
                CommisJob(
                    owner_id=owner_id,
                    concierge_course_id=course.id,
                    task=str(task),
                    status=str(job_status),
                    model=str(job_data.get("model") or "gpt-5-mini"),
                    reasoning_effort=job_data.get("reasoning_effort"),
                    config=dict(job_data.get("config") or {}),
                    commis_id=job_data.get("commis_id"),
                    error=job_data.get("error"),
                    acknowledged=bool(job_data.get("acknowledged", False)),
                    created_at=_to_naive(job_created_at),
                    updated_at=_to_naive(job_updated_at),
                    started_at=_to_naive(job_started_at),
                    finished_at=_to_naive(job_finished_at),
                )
            )
            counts["commis_jobs"] += 1

        counts["courses"] += 1

    db.commit()
    return {"scenario": scenario_name, **counts}


def cleanup_scenario(db: Session, scenario_name: Optional[str]) -> dict[str, int]:
    prefix = _scenario_prefix(scenario_name)
    threads_query = db.query(Thread.id).filter(Thread.title.like(f"{prefix}%"))
    thread_rows = threads_query.all()
    thread_ids = [row[0] for row in thread_rows]

    if not thread_ids:
        return {"courses": 0, "threads": 0, "messages": 0, "commis_jobs": 0}

    course_ids = [row[0] for row in db.query(Course.id).filter(Course.thread_id.in_(thread_ids)).all()]

    commis_jobs_deleted = 0
    if course_ids:
        commis_jobs_deleted = db.query(CommisJob).filter(CommisJob.concierge_course_id.in_(course_ids)).delete(synchronize_session=False)

    courses_deleted = db.query(Course).filter(Course.thread_id.in_(thread_ids)).delete(synchronize_session=False)
    messages_deleted = db.query(ThreadMessage).filter(ThreadMessage.thread_id.in_(thread_ids)).delete(synchronize_session=False)
    threads_deleted = db.query(Thread).filter(Thread.id.in_(thread_ids)).delete(synchronize_session=False)

    logger.info(
        "Scenario cleanup %s: %s courses, %s messages, %s threads, %s commis jobs",
        prefix,
        courses_deleted,
        messages_deleted,
        threads_deleted,
        commis_jobs_deleted,
    )

    return {
        "courses": courses_deleted,
        "threads": threads_deleted,
        "messages": messages_deleted,
        "commis_jobs": commis_jobs_deleted,
    }


def _parse_time(value: Any, base_time: datetime) -> Optional[datetime]:
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return base_time + timedelta(seconds=float(value))

    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        if normalized.lower() in {"now", "0", "0s", "0m", "0h"}:
            return base_time

        match = _RELATIVE_RE.match(normalized)
        if match:
            sign = -1 if match.group(1) == "-" else 1
            amount = int(match.group(2))
            unit = match.group(3).lower()
            delta = _unit_delta(unit, amount)
            return base_time + (delta * sign)

        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ScenarioError(f"Invalid time value '{value}'") from exc

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    raise ScenarioError(f"Unsupported time value '{value}'")


def _unit_delta(unit: str, amount: int) -> timedelta:
    if unit == "s":
        return timedelta(seconds=amount)
    if unit == "m":
        return timedelta(minutes=amount)
    if unit == "h":
        return timedelta(hours=amount)
    if unit == "d":
        return timedelta(days=amount)
    if unit == "w":
        return timedelta(days=amount * 7)
    raise ScenarioError(f"Unsupported time unit '{unit}'")


def _to_naive(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.replace(tzinfo=None)


def _to_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _scenario_prefix(scenario_name: Optional[str]) -> str:
    if not scenario_name:
        return SCENARIO_TITLE_PREFIX
    return f"{SCENARIO_TITLE_PREFIX}{scenario_name}]"


def _scenario_title(scenario_name: str, title: str) -> str:
    return f"{_scenario_prefix(scenario_name)} {title}"


def _coerce_enum(enum_cls: type, value: Any):
    if value is None:
        raise ScenarioError(f"Missing value for enum {enum_cls.__name__}")
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        for member in enum_cls:  # type: ignore[assignment]
            if member.value == normalized or member.name.lower() == normalized:
                return member
    raise ScenarioError(f"Invalid value '{value}' for enum {enum_cls.__name__}")


def _as_list(value: Any) -> Iterable[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    raise ScenarioError("Expected a list")
