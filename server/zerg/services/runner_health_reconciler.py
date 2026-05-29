"""Runner health reconciliation, incidents, alerts, and wakeups."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from zerg.models.models import Runner
from zerg.models.models import RunnerHealthIncident
from zerg.models.user import User
from zerg.services.runner_connection_manager import get_runner_connection_manager
from zerg.services.runner_health import RunnerHealthAssessment
from zerg.services.runner_health import assess_runner_health
from zerg.services.runner_health import runner_requires_proactive_attention
from zerg.services.telegram_format import format_for_telegram
from zerg.shared.email import send_email
from zerg.utils.time import utc_now_naive

logger = logging.getLogger(__name__)

OFFLINE_INCIDENT_TYPE = "offline"
OPEN_INCIDENT_STATUS = "open"
RESOLVED_INCIDENT_STATUS = "resolved"
ALERT_AFTER = timedelta(minutes=int(os.getenv("RUNNER_OFFLINE_ALERT_AFTER_MINUTES", "5")))
NON_PROACTIVE_SUPPRESSED_REASON = "non_proactive_availability"


def _format_duration(delta: timedelta) -> str:
    total_seconds = max(0, int(delta.total_seconds()))
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes, seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def _runner_host_label(runner: Runner) -> str:
    metadata = runner.runner_metadata if isinstance(runner.runner_metadata, dict) else {}
    hostname = metadata.get("hostname")
    if isinstance(hostname, str) and hostname.strip():
        return hostname.strip()
    return runner.name


def _open_incident_context(runner: Runner, health: RunnerHealthAssessment, now: datetime) -> dict[str, Any]:
    return {
        "runner_name": runner.name,
        "host_label": _runner_host_label(runner),
        "availability_policy": health.availability_policy,
        "status_reason": health.status_reason,
        "status_summary": health.status_summary,
        "install_mode": health.install_mode,
        "runner_version": health.runner_version,
        "latest_runner_version": health.latest_runner_version,
        "version_status": health.version_status,
        "opened_at": now.isoformat(),
    }


def _get_open_incident(db: Session, runner_id: int) -> RunnerHealthIncident | None:
    return (
        db.query(RunnerHealthIncident)
        .filter(
            RunnerHealthIncident.runner_id == runner_id,
            RunnerHealthIncident.incident_type == OFFLINE_INCIDENT_TYPE,
            RunnerHealthIncident.status == OPEN_INCIDENT_STATUS,
        )
        .order_by(RunnerHealthIncident.opened_at.desc())
        .first()
    )


def _ensure_open_incident(
    db: Session,
    *,
    runner: Runner,
    health: RunnerHealthAssessment,
    now: datetime,
) -> tuple[RunnerHealthIncident, bool]:
    incident = _get_open_incident(db, runner.id)
    if incident is None:
        incident = RunnerHealthIncident(
            owner_id=runner.owner_id,
            runner_id=runner.id,
            incident_type=OFFLINE_INCIDENT_TYPE,
            status=OPEN_INCIDENT_STATUS,
            reason_code=health.status_reason,
            summary=health.status_summary,
            context=_open_incident_context(runner, health, now),
            opened_at=now,
            last_observed_at=now,
        )
        db.add(incident)
        db.flush()
        return incident, True

    incident.reason_code = health.status_reason
    incident.summary = health.status_summary
    incident.last_observed_at = now
    context = dict(incident.context or {})
    context.update(_open_incident_context(runner, health, now))
    incident.context = context
    return incident, False


def _resolve_open_incident(
    incident: RunnerHealthIncident,
    *,
    runner: Runner,
    health: RunnerHealthAssessment,
    now: datetime,
) -> None:
    incident.status = RESOLVED_INCIDENT_STATUS
    incident.resolved_at = now
    incident.last_observed_at = now
    incident.reason_code = health.status_reason
    incident.summary = health.status_summary
    context = dict(incident.context or {})
    context.update(
        {
            "resolved_at": now.isoformat(),
            "resolved_status_reason": health.status_reason,
            "resolved_status_summary": health.status_summary,
            "runner_name": runner.name,
        }
    )
    incident.context = context


def _resolve_open_incident_as_non_actionable(
    incident: RunnerHealthIncident,
    *,
    runner: Runner,
    health: RunnerHealthAssessment,
    now: datetime,
) -> None:
    incident.status = RESOLVED_INCIDENT_STATUS
    incident.resolved_at = now
    incident.last_observed_at = now
    incident.reason_code = health.status_reason
    incident.summary = (
        "Offline is expected for this on-demand runner."
        if health.availability_policy == "on_demand"
        else "Offline is expected for this ephemeral runner."
    )
    context = dict(incident.context or {})
    context.update(
        {
            "resolved_at": now.isoformat(),
            "resolved_status_reason": "availability_policy_non_actionable",
            "resolved_status_summary": incident.summary,
            "resolved_by_policy": health.availability_policy,
            "runner_name": runner.name,
            "alert_suppressed_at": now.isoformat(),
            "alert_suppressed_reason": NON_PROACTIVE_SUPPRESSED_REASON,
            "wakeup_suppressed_at": now.isoformat(),
            "wakeup_suppressed_reason": NON_PROACTIVE_SUPPRESSED_REASON,
        }
    )
    incident.context = context


async def _send_telegram_alert(user: User, text: str) -> bool:
    chat_id = str((user.context or {}).get("telegram_chat_id", "")).strip()
    if not chat_id:
        return False

    from zerg.channels.registry import get_registry
    from zerg.channels.types import ChannelMessage

    channel = get_registry().get("telegram")
    if not channel:
        return False

    result = await channel.send_message(
        ChannelMessage(
            channel_id="telegram",
            to=chat_id,
            text=format_for_telegram(text),
            parse_mode="html",
        )
    )
    return bool(result.get("success"))


def _send_email_alert(user: User, subject: str, body: str) -> bool:
    email = str(getattr(user, "email", "") or "").strip()
    if not email:
        return False
    return bool(
        send_email(
            subject,
            body,
            to_email=email,
            alert_type="runner_offline",
        )
    )


def _build_external_alert_copy(
    runner: Runner,
    health: RunnerHealthAssessment,
    incident: RunnerHealthIncident,
    now: datetime,
) -> tuple[str, str, str]:
    offline_for = _format_duration(now - incident.opened_at)
    host_label = _runner_host_label(runner)
    version_line = ""
    if health.runner_version and health.latest_runner_version and health.version_status == "outdated":
        version_line = f"\nVersion: v{health.runner_version} (latest v{health.latest_runner_version})"

    subject = f"Runner offline: {runner.name}"
    telegram_text = (
        f"Runner <b>{runner.name}</b> on <b>{host_label}</b> has been offline for <b>{offline_for}</b>.\n"
        f"{health.status_summary}\n"
        "Next step: restart the runner service. If it does not reconnect, generate a repair command in "
        "Longhouse and re-run the installer."
    )
    body = (
        "Longhouse detected a runner outage.\n\n"
        f"Runner: {runner.name}\n"
        f"Host: {host_label}\n"
        f"Offline for: {offline_for}\n"
        f"Reason: {health.status_reason}\n"
        f"Status: {health.status_summary}\n"
        f"Install mode: {health.install_mode or 'unknown'}"
        f"{version_line}\n\n"
        "Recommended action:\n"
        "1. Restart the runner service on the machine.\n"
        "2. If it does not reconnect, generate a repair command in Longhouse and re-run the installer.\n"
    )
    return subject, telegram_text, body


def _external_attention_allowed(health: RunnerHealthAssessment) -> tuple[bool, str | None]:
    """Return whether this runner should page external attention channels."""
    if not runner_requires_proactive_attention(health.availability_policy):
        return False, NON_PROACTIVE_SUPPRESSED_REASON
    return True, None


async def _maybe_send_external_alert(
    db: Session,
    *,
    incident: RunnerHealthIncident,
    user: User,
    runner: Runner,
    health: RunnerHealthAssessment,
    now: datetime,
) -> bool:
    if incident.alert_sent_at is not None:
        return False
    allowed, suppressed_reason = _external_attention_allowed(health)
    if not allowed:
        context = dict(incident.context or {})
        if context.get("alert_suppressed_reason") != suppressed_reason:
            context.update(
                {
                    "alert_suppressed_at": now.isoformat(),
                    "alert_suppressed_reason": suppressed_reason,
                }
            )
            incident.context = context
            db.flush()
        return False
    if now - incident.opened_at < ALERT_AFTER:
        return False

    subject, telegram_text, body = _build_external_alert_copy(runner, health, incident, now)
    channel: str | None = None
    if await _send_telegram_alert(user, telegram_text):
        channel = "telegram"
    elif _send_email_alert(user, subject, body):
        channel = "email"

    if not channel:
        return False

    incident.alert_sent_at = now
    incident.alert_channel = channel
    incident.alert_count = int(incident.alert_count or 0) + 1
    context = dict(incident.context or {})
    context.update(
        {
            "alert_sent_at": now.isoformat(),
            "alert_channel": channel,
        }
    )
    incident.context = context
    db.flush()
    return True


async def reconcile_runner_health(
    db: Session,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reconcile cached runner state, incidents, and attention side effects."""
    now = now or utc_now_naive()
    connection_manager = get_runner_connection_manager()
    user_cache: dict[int, User | None] = {}

    result = {
        "checked": 0,
        "cached_status_updates": 0,
        "incidents_opened": 0,
        "incidents_resolved": 0,
        "alerts_sent": 0,
        "errors": 0,
        "checked_at": now.isoformat(),
    }

    runners = db.query(Runner).all()
    for runner in runners:
        try:
            is_connected = connection_manager.is_online(runner.owner_id, runner.id)
            health = assess_runner_health(runner, now=now, is_connected=is_connected)
            result["checked"] += 1

            desired_status = runner.status if runner.status == "revoked" else health.effective_status
            if runner.status != desired_status:
                runner.status = desired_status
                result["cached_status_updates"] += 1

            incident = _get_open_incident(db, runner.id)
            if health.effective_status == "offline" and runner.last_seen_at is not None:
                if not runner_requires_proactive_attention(health.availability_policy):
                    if incident is not None:
                        _resolve_open_incident_as_non_actionable(incident, runner=runner, health=health, now=now)
                        result["incidents_resolved"] += 1
                    db.commit()
                    continue

                incident, created = _ensure_open_incident(db, runner=runner, health=health, now=now)
                if created:
                    result["incidents_opened"] += 1

                # Persist status/incident changes before any awaited side effects.
                # Otherwise a flush from the open incident can hold SQLite's writer
                # lock across alert delivery or runner wakeups.
                db.commit()

                owner = user_cache.get(runner.owner_id)
                if runner.owner_id not in user_cache:
                    owner = db.query(User).filter(User.id == runner.owner_id).first()
                    user_cache[runner.owner_id] = owner

                if owner is not None and await _maybe_send_external_alert(
                    db,
                    incident=incident,
                    user=owner,
                    runner=runner,
                    health=health,
                    now=now,
                ):
                    result["alerts_sent"] += 1
                db.commit()
                continue
            elif incident is not None:
                _resolve_open_incident(incident, runner=runner, health=health, now=now)
                result["incidents_resolved"] += 1

            db.commit()
        except Exception:
            db.rollback()
            result["errors"] += 1
            logger.exception("Runner health reconcile failed for runner %s", runner.id)

    return result
