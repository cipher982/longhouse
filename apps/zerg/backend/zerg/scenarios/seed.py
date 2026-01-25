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

from zerg.models.agent_run_event import AgentRunEvent
from zerg.models.enums import RunStatus
from zerg.models.enums import RunTrigger
from zerg.models.enums import ThreadType
from zerg.models.models import AgentRun
from zerg.models.models import Thread
from zerg.models.models import ThreadMessage
from zerg.services.supervisor_service import SupervisorService
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
    runs = scenario.get("runs") or []
    if not isinstance(runs, list):
        raise ScenarioError("Scenario 'runs' must be a list")

    if clean:
        cleanup_scenario(db, scenario_name)

    supervisor_agent = SupervisorService(db).get_or_create_supervisor_agent(owner_id)

    base_time = utc_now()
    timebase_value = scenario.get("timebase")
    if timebase_value:
        parsed_base = _parse_time(timebase_value, base_time)
        if parsed_base is not None:
            base_time = parsed_base

    counts = {"runs": 0, "messages": 0, "events": 0}

    for index, run_data in enumerate(runs, start=1):
        if not isinstance(run_data, dict):
            raise ScenarioError("Each run entry must be a mapping")

        run_ref = run_data.get("id") or f"run-{index}"
        title = run_data.get("title") or run_data.get("thread_title") or run_ref
        thread_type = _coerce_enum(ThreadType, run_data.get("thread_type", ThreadType.CHAT.value)).value
        thread = Thread(
            agent_id=supervisor_agent.id,
            title=_scenario_title(scenario_name, title),
            active=False,
            thread_type=thread_type,
            agent_state={"scenario": scenario_name, "scenario_run": run_ref},
        )
        db.add(thread)
        db.flush()

        started_at = _parse_time(run_data.get("started_at"), base_time)
        finished_at = _parse_time(run_data.get("finished_at"), base_time)
        created_at = _parse_time(run_data.get("created_at"), base_time) or started_at or base_time
        updated_at = _parse_time(run_data.get("updated_at"), base_time) or finished_at or created_at

        status = _coerce_enum(RunStatus, run_data.get("status", RunStatus.RUNNING.value))
        trigger = _coerce_enum(RunTrigger, run_data.get("trigger", RunTrigger.MANUAL.value))

        run = AgentRun(
            agent_id=supervisor_agent.id,
            thread_id=thread.id,
            status=status,
            trigger=trigger,
            summary=run_data.get("summary"),
            error=run_data.get("error"),
            started_at=_to_naive(started_at),
            finished_at=_to_naive(finished_at),
            created_at=_to_naive(created_at),
            updated_at=_to_naive(updated_at),
            correlation_id=f"scenario:{scenario_name}:{run_ref}",
        )
        if started_at and finished_at:
            run.duration_ms = int((finished_at - started_at).total_seconds() * 1000)
        db.add(run)
        db.flush()

        for event in _as_list(run_data.get("events")):
            if not isinstance(event, dict):
                raise ScenarioError("Run events must be mappings")
            event_type = event.get("type")
            if not event_type:
                raise ScenarioError("Run event missing 'type'")
            event_time = _parse_time(event.get("at") or event.get("created_at"), base_time) or base_time
            payload = dict(event.get("payload") or {})
            message = event.get("message")
            if message:
                payload.setdefault("message", message)
            db.add(
                AgentRunEvent(
                    run_id=run.id,
                    event_type=str(event_type),
                    payload=payload,
                    created_at=_to_aware(event_time),
                )
            )
            counts["events"] += 1

        for message in _as_list(run_data.get("messages")):
            if not isinstance(message, dict):
                raise ScenarioError("Run messages must be mappings")
            role = message.get("role")
            content = message.get("content")
            if not role or content is None:
                raise ScenarioError("Run message requires 'role' and 'content'")
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

        counts["runs"] += 1

    db.commit()
    return {"scenario": scenario_name, **counts}


def cleanup_scenario(db: Session, scenario_name: Optional[str]) -> dict[str, int]:
    prefix = _scenario_prefix(scenario_name)
    threads_query = db.query(Thread.id).filter(Thread.title.like(f"{prefix}%"))
    thread_rows = threads_query.all()
    thread_ids = [row[0] for row in thread_rows]

    if not thread_ids:
        return {"runs": 0, "threads": 0, "messages": 0}

    runs_deleted = db.query(AgentRun).filter(AgentRun.thread_id.in_(thread_ids)).delete(synchronize_session=False)
    messages_deleted = db.query(ThreadMessage).filter(ThreadMessage.thread_id.in_(thread_ids)).delete(synchronize_session=False)
    threads_deleted = db.query(Thread).filter(Thread.id.in_(thread_ids)).delete(synchronize_session=False)

    logger.info(
        "Scenario cleanup %s: %s runs, %s messages, %s threads",
        prefix,
        runs_deleted,
        messages_deleted,
        threads_deleted,
    )

    return {"runs": runs_deleted, "threads": threads_deleted, "messages": messages_deleted}


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
