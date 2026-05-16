"""AI-first operational watchman for tenant-local anomaly detection."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI
from sqlalchemy import text
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import Session

from zerg.config import get_settings
from zerg.jobs.ingest_health import compute_ingest_health
from zerg.models.work import OPERATIONAL_INCIDENT_STATUS_OPEN
from zerg.models.work import OPERATIONAL_INCIDENT_STATUS_RESOLVED
from zerg.models.work import OperationalIncident
from zerg.models.work import OpsWatchObservation
from zerg.models.work import OpsWatchRun
from zerg.pricing import get_usd_prices_per_1k
from zerg.services.write_serializer import get_write_serializer
from zerg.shared.email import send_alert_email

logger = logging.getLogger(__name__)

WATCHMAN_SOURCE = "ai_ops_watchman"
WATCHMAN_PROMPT_VERSION = "2026-03-28.v1"
DEFAULT_MODEL_ID = "deepseek/deepseek-v4-pro"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_API_KEY_ENV = "OPENROUTER_API_KEY"
DEFAULT_WINDOW_MINUTES = 10
DEFAULT_SESSION_LIMIT = 5
DEFAULT_PRIOR_OBSERVATIONS = 1
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_REASONING_EFFORT = "low"
ALLOWED_ANALYSIS_STATUSES = {"normal", "watch", "critical"}
SYSTEM_PROMPT = """You are Longhouse AI Ops Watchman.

You analyze tenant-local operational observations and decide whether the recent
system story looks normal or dangerous.

Return valid JSON with exactly these keys:
- status: normal | watch | critical
- title: short string
- summary: short string
- evidence: array of short strings
- should_email: boolean
- recommended_action: short string
- incident_type: short snake_case string
- dedupe_key: stable short string for the same ongoing issue

Rules:
- Be skeptical.
- Do not invent facts outside the observations.
- Prefer normal if the evidence is weak or ambiguous.
- Escalate only when the observations clearly support concern.
- Keep incident_type and dedupe_key stable and concrete when status is watch or critical.
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def _compact_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _estimate_cost_usd(model_id: str, input_tokens: int | None, output_tokens: int | None) -> float | None:
    if input_tokens is None or output_tokens is None:
        return None
    prices = get_usd_prices_per_1k(model_id)
    if not prices:
        return None
    in_price, out_price = prices
    return round(((input_tokens * in_price) + (output_tokens * out_price)) / 1000.0, 8)


def _watchman_enabled() -> bool:
    return os.getenv("OPS_WATCHMAN_ENABLED", "1") != "0"


def _watchman_window_minutes() -> int:
    return max(1, int(os.getenv("OPS_WATCHMAN_WINDOW_MINUTES", str(DEFAULT_WINDOW_MINUTES))))


def _watchman_session_limit() -> int:
    return max(1, int(os.getenv("OPS_WATCHMAN_TOP_SESSIONS", str(DEFAULT_SESSION_LIMIT))))


def _prior_observation_limit() -> int:
    return max(0, int(os.getenv("OPS_WATCHMAN_PRIOR_OBSERVATIONS", str(DEFAULT_PRIOR_OBSERVATIONS))))


def _watchman_timeout_seconds() -> float:
    return max(5.0, float(os.getenv("OPS_WATCHMAN_LLM_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))))


def _watchman_reasoning_effort() -> str | None:
    raw = os.getenv("OPS_WATCHMAN_REASONING_EFFORT", DEFAULT_REASONING_EFFORT).strip().lower()
    if raw in {"", "off", "none", "disable", "disabled", "false", "0"}:
        return None
    if raw in {"low", "medium", "high"}:
        return raw
    logger.warning("Invalid OPS_WATCHMAN_REASONING_EFFORT=%r; disabling explicit reasoning control", raw)
    return None


def _watchman_model_config() -> tuple[str, str, str, str | None, str | None]:
    model_id = os.getenv("OPS_WATCHMAN_MODEL", DEFAULT_MODEL_ID).strip() or DEFAULT_MODEL_ID
    base_url = os.getenv("OPS_WATCHMAN_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
    api_key_env = os.getenv("OPS_WATCHMAN_API_KEY_ENV", DEFAULT_API_KEY_ENV).strip() or DEFAULT_API_KEY_ENV
    api_key = os.getenv(api_key_env, "").strip() or None
    reasoning_effort = _watchman_reasoning_effort()
    return model_id, base_url, api_key_env, api_key, reasoning_effort


def _db_file_paths() -> tuple[Path, Path] | None:
    settings = get_settings()
    try:
        parsed = make_url(settings.database_url)
    except Exception:
        return None
    db_raw = parsed.database or ""
    if not db_raw:
        return None
    db_path = Path(db_raw).expanduser()
    wal_path = Path(f"{db_path}-wal")
    return db_path, wal_path


def _make_observation(
    *,
    observed_at: datetime,
    window_start_at: datetime | None,
    window_end_at: datetime | None,
    entity_type: str,
    entity_id: str,
    source: str,
    payload_json: dict[str, Any] | None,
) -> OpsWatchObservation:
    payload_text = _compact_json(payload_json) if payload_json is not None else None
    return OpsWatchObservation(
        observed_at=observed_at,
        window_start_at=window_start_at,
        window_end_at=window_end_at,
        entity_type=entity_type,
        entity_id=entity_id,
        source=source,
        payload_json=payload_json,
        payload_text=payload_text,
    )


def _collect_db_file_stats(db: Session, now: datetime, window_start: datetime) -> list[OpsWatchObservation]:
    paths = _db_file_paths()
    if paths is None:
        return []

    db_path, wal_path = paths
    db_bytes = db_path.stat().st_size if db_path.exists() else None

    # Compute growth delta vs previous observation.
    prev = (
        db.query(OpsWatchObservation)
        .filter(
            OpsWatchObservation.entity_type == "tenant",
            OpsWatchObservation.entity_id == "self",
            OpsWatchObservation.source == "db_file_stats",
        )
        .order_by(OpsWatchObservation.id.desc())
        .first()
    )
    prev_bytes = prev.payload_json.get("db_bytes") if prev and prev.payload_json else None
    db_bytes_delta = (db_bytes - prev_bytes) if db_bytes is not None and prev_bytes is not None else None

    payload = {
        "database_url": get_settings().database_url,
        "db_path": str(db_path),
        "db_exists": db_path.exists(),
        "db_bytes": db_bytes,
        "db_bytes_delta": db_bytes_delta,
        "wal_path": str(wal_path),
        "wal_exists": wal_path.exists(),
        "wal_bytes": wal_path.stat().st_size if wal_path.exists() else 0,
    }
    return [
        _make_observation(
            observed_at=now,
            window_start_at=window_start,
            window_end_at=now,
            entity_type="tenant",
            entity_id="self",
            source="db_file_stats",
            payload_json=payload,
        )
    ]


def _collect_write_serializer_metrics(now: datetime, window_start: datetime) -> list[OpsWatchObservation]:
    ws = get_write_serializer()
    payload = {"configured": bool(ws.is_configured)}
    if ws.is_configured:
        payload.update(ws.get_metrics())
    return [
        _make_observation(
            observed_at=now,
            window_start_at=window_start,
            window_end_at=now,
            entity_type="tenant",
            entity_id="self",
            source="write_serializer",
            payload_json=payload,
        )
    ]


def _collect_ingest_health(db: Session, now: datetime, window_start: datetime) -> list[OpsWatchObservation]:
    payload = compute_ingest_health(db)
    if payload.get("last_session_at"):
        payload["last_session_at"] = _iso(payload["last_session_at"])
    return [
        _make_observation(
            observed_at=now,
            window_start_at=window_start,
            window_end_at=now,
            entity_type="tenant",
            entity_id="self",
            source="ingest_health",
            payload_json=payload,
        )
    ]


def _collect_open_incidents(db: Session, now: datetime, window_start: datetime) -> list[OpsWatchObservation]:
    rows = (
        db.query(OperationalIncident)
        .filter(
            OperationalIncident.status == OPERATIONAL_INCIDENT_STATUS_OPEN,
            OperationalIncident.source != WATCHMAN_SOURCE,
        )
        .order_by(OperationalIncident.opened_at.desc())
        .limit(5)
        .all()
    )
    payload = {
        "count": len(rows),
        "incidents": [
            {
                "incident_type": row.incident_type,
                "source": row.source,
                "dedupe_key": row.dedupe_key,
                "summary": row.summary,
                "opened_at": _iso(row.opened_at),
                "last_observed_at": _iso(row.last_observed_at),
            }
            for row in rows
        ],
    }
    return [
        _make_observation(
            observed_at=now,
            window_start_at=window_start,
            window_end_at=now,
            entity_type="tenant",
            entity_id="self",
            source="open_incidents",
            payload_json=payload,
        )
    ]


def _collect_recent_session_activity(db: Session, now: datetime, window_start: datetime) -> list[OpsWatchObservation]:
    rows = (
        db.execute(
            text(
                """
                WITH recent AS (
                    SELECT
                        e.session_id AS session_id,
                        COUNT(*) AS new_events,
                        SUM(CASE WHEN e.role = 'user' THEN 1 ELSE 0 END) AS new_user_messages,
                        SUM(
                            CASE WHEN e.role = 'assistant' AND e.tool_name IS NULL THEN 1 ELSE 0 END
                        ) AS new_assistant_messages,
                        SUM(CASE WHEN e.tool_name IS NOT NULL THEN 1 ELSE 0 END) AS new_tool_calls,
                        MIN(e.timestamp) AS first_event_at,
                        MAX(e.timestamp) AS last_event_at
                    FROM events e
                    WHERE e.timestamp >= :window_start
                    GROUP BY e.session_id
                ),
                branch_counts AS (
                    SELECT
                        sb.session_id AS session_id,
                        COUNT(*) AS branch_count,
                        SUM(CASE WHEN sb.branch_reason = 'rewrite' THEN 1 ELSE 0 END) AS rewrite_branches
                    FROM session_branches sb
                    GROUP BY sb.session_id
                ),
                source_counts AS (
                    SELECT
                        e.session_id AS session_id,
                        COUNT(DISTINCT e.source_path) AS distinct_source_paths
                    FROM events e
                    WHERE e.source_path IS NOT NULL
                    GROUP BY e.session_id
                )
                SELECT
                    r.session_id,
                    s.provider,
                    s.project,
                    s.started_at,
                    s.ended_at,
                    r.new_events,
                    r.new_user_messages,
                    r.new_assistant_messages,
                    r.new_tool_calls,
                    r.first_event_at,
                    r.last_event_at,
                    COALESCE(b.branch_count, 0) AS branch_count,
                    COALESCE(b.rewrite_branches, 0) AS rewrite_branches,
                    COALESCE(sc.distinct_source_paths, 0) AS distinct_source_paths
                FROM recent r
                JOIN sessions s ON s.id = r.session_id
                LEFT JOIN branch_counts b ON b.session_id = r.session_id
                LEFT JOIN source_counts sc ON sc.session_id = r.session_id
                ORDER BY r.new_events DESC, r.last_event_at DESC
                LIMIT :limit
                """
            ),
            {"window_start": window_start, "limit": _watchman_session_limit()},
        )
        .mappings()
        .all()
    )
    observations: list[OpsWatchObservation] = []
    for row in rows:
        payload = {
            "provider": row["provider"],
            "project": row["project"],
            "started_at": _iso(row["started_at"]),
            "ended_at": _iso(row["ended_at"]),
            "new_events": int(row["new_events"] or 0),
            "new_user_messages": int(row["new_user_messages"] or 0),
            "new_assistant_messages": int(row["new_assistant_messages"] or 0),
            "new_tool_calls": int(row["new_tool_calls"] or 0),
            "first_event_at": _iso(row["first_event_at"]),
            "last_event_at": _iso(row["last_event_at"]),
            "branch_count": int(row["branch_count"] or 0),
            "rewrite_branches": int(row["rewrite_branches"] or 0),
            "distinct_source_paths": int(row["distinct_source_paths"] or 0),
        }
        observations.append(
            _make_observation(
                observed_at=now,
                window_start_at=window_start,
                window_end_at=now,
                entity_type="session",
                entity_id=str(row["session_id"]),
                source="recent_session_activity",
                payload_json=payload,
            )
        )
    return observations


def collect_observations(db: Session, *, now: datetime | None = None) -> list[OpsWatchObservation]:
    """Collect the current raw observation set for the watchman."""
    observed_at = now or _utc_now()
    window_start = observed_at - timedelta(minutes=_watchman_window_minutes())
    observations: list[OpsWatchObservation] = []
    observations.extend(_collect_db_file_stats(db, observed_at, window_start))
    observations.extend(_collect_write_serializer_metrics(observed_at, window_start))
    observations.extend(_collect_ingest_health(db, observed_at, window_start))
    observations.extend(_collect_open_incidents(db, observed_at, window_start))
    observations.extend(_collect_recent_session_activity(db, observed_at, window_start))
    return observations


def _serialize_observation(row: OpsWatchObservation) -> dict[str, Any]:
    return {
        "id": row.id,
        "observed_at": _iso(row.observed_at),
        "window_start_at": _iso(row.window_start_at),
        "window_end_at": _iso(row.window_end_at),
        "entity_type": row.entity_type,
        "entity_id": row.entity_id,
        "source": row.source,
        "payload_text": row.payload_text,
        "payload": row.payload_json,
    }


def build_analysis_context(db: Session, current_rows: list[OpsWatchObservation]) -> dict[str, Any]:
    """Build a compact prompt context: current rows plus a tiny amount of history."""
    prior_limit = _prior_observation_limit()
    history: list[dict[str, Any]] = []
    if prior_limit > 0:
        for row in current_rows:
            previous = (
                db.query(OpsWatchObservation)
                .filter(
                    OpsWatchObservation.id < row.id,
                    OpsWatchObservation.source == row.source,
                    OpsWatchObservation.entity_type == row.entity_type,
                    OpsWatchObservation.entity_id == row.entity_id,
                )
                .order_by(OpsWatchObservation.id.desc())
                .limit(prior_limit)
                .all()
            )
            history.extend(_serialize_observation(item) for item in reversed(previous))

    open_watchman = (
        db.query(OperationalIncident)
        .filter(
            OperationalIncident.source == WATCHMAN_SOURCE,
            OperationalIncident.status == OPERATIONAL_INCIDENT_STATUS_OPEN,
        )
        .order_by(OperationalIncident.opened_at.desc())
        .first()
    )
    current_incident = None
    if open_watchman is not None:
        current_incident = {
            "incident_type": open_watchman.incident_type,
            "dedupe_key": open_watchman.dedupe_key,
            "summary": open_watchman.summary,
            "opened_at": _iso(open_watchman.opened_at),
            "last_observed_at": _iso(open_watchman.last_observed_at),
            "context": open_watchman.context,
        }

    return {
        "prompt_version": WATCHMAN_PROMPT_VERSION,
        "generated_at": _iso(_utc_now()),
        "current_observations": [_serialize_observation(row) for row in current_rows],
        "prior_observations": history,
        "open_watchman_incident": current_incident,
    }


def _extract_usage(response: Any) -> dict[str, Any]:
    usage = response.usage
    if not usage:
        return {
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "reasoning_tokens": None,
            "provider_cost_in_usd_ticks": None,
        }

    completion_details = getattr(usage, "completion_tokens_details", None)
    return {
        "input_tokens": getattr(usage, "prompt_tokens", None),
        "output_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
        "reasoning_tokens": getattr(completion_details, "reasoning_tokens", None) if completion_details else None,
        "provider_cost_in_usd_ticks": getattr(usage, "cost_in_usd_ticks", None),
    }


def _validate_analysis_result(payload: dict[str, Any]) -> dict[str, Any]:
    required = {
        "status",
        "title",
        "summary",
        "evidence",
        "should_email",
        "recommended_action",
        "incident_type",
        "dedupe_key",
    }
    missing = required - set(payload.keys())
    if missing:
        raise ValueError(f"Watchman response missing keys: {sorted(missing)}")

    status = str(payload.get("status") or "").strip().lower()
    if status not in ALLOWED_ANALYSIS_STATUSES:
        raise ValueError(f"Invalid watchman status: {status!r}")

    evidence = payload.get("evidence")
    if not isinstance(evidence, list):
        raise ValueError("Watchman evidence must be a list")

    should_email = payload.get("should_email")
    if not isinstance(should_email, bool):
        raise ValueError("Watchman should_email must be a boolean")

    cleaned = {
        "status": status,
        "title": str(payload.get("title") or "").strip(),
        "summary": str(payload.get("summary") or "").strip(),
        "evidence": [str(item).strip() for item in evidence if str(item).strip()],
        "should_email": should_email,
        "recommended_action": str(payload.get("recommended_action") or "").strip(),
        "incident_type": str(payload.get("incident_type") or "").strip() or "watchman_anomaly",
        "dedupe_key": str(payload.get("dedupe_key") or "").strip(),
    }
    if status != "normal" and not cleaned["dedupe_key"]:
        raise ValueError("Watchman dedupe_key is required for watch/critical results")
    return cleaned


async def analyze_context(
    context: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any], str | None, str | None]:
    """Run the LLM analysis over the current observation context.

    Returns:
        (analysis_result_or_none, usage_dict, model_id, skip_reason_or_none)
    """
    settings = get_settings()
    if settings.llm_disabled:
        return None, {}, None, "LLM_DISABLED=1"

    model_id, base_url, api_key_env, api_key, reasoning_effort = _watchman_model_config()
    if not api_key:
        return None, {}, model_id, f"{api_key_env} not set"

    client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=_watchman_timeout_seconds())
    started = time.monotonic()
    try:
        request: dict[str, Any] = {
            "model": model_id,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _compact_json(context)},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "max_completion_tokens": 768,
        }
        if reasoning_effort is not None:
            request["reasoning_effort"] = reasoning_effort

        response = await client.chat.completions.create(
            **request,
        )
    finally:
        await client.close()

    raw_content = response.choices[0].message.content or ""
    if not raw_content:
        raise RuntimeError("Watchman model returned empty content")
    parsed = json.loads(raw_content)
    if not isinstance(parsed, dict):
        raise ValueError("Watchman model response must be a JSON object")

    usage = _extract_usage(response)
    usage["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    usage["estimated_cost_usd"] = _estimate_cost_usd(model_id, usage.get("input_tokens"), usage.get("output_tokens"))
    return _validate_analysis_result(parsed), usage, model_id, None


def _incident_context(analysis: dict[str, Any], usage: dict[str, Any], now: datetime) -> dict[str, Any]:
    return {
        "analysis_status": analysis["status"],
        "title": analysis["title"],
        "summary": analysis["summary"],
        "evidence": analysis["evidence"],
        "recommended_action": analysis["recommended_action"],
        "should_email": analysis["should_email"],
        "usage": usage,
        "observed_at": _iso(now),
    }


def _resolve_open_watchman_incidents(db: Session, now: datetime, *, resolved_summary: str) -> tuple[int, list[str]]:
    rows = (
        db.query(OperationalIncident)
        .filter(
            OperationalIncident.source == WATCHMAN_SOURCE,
            OperationalIncident.status == OPERATIONAL_INCIDENT_STATUS_OPEN,
        )
        .all()
    )
    prev_summaries = [row.summary for row in rows if row.summary]
    for row in rows:
        row.status = OPERATIONAL_INCIDENT_STATUS_RESOLVED
        row.summary = resolved_summary
        row.last_observed_at = now
        row.resolved_at = now
        context = dict(row.context or {})
        context["resolved_at"] = _iso(now)
        context["resolved_by_watchman"] = True
        row.context = context
    return len(rows), prev_summaries


def reconcile_incident(
    db: Session,
    *,
    analysis: dict[str, Any],
    usage: dict[str, Any],
    now: datetime,
) -> tuple[str, int | None, list[str]]:
    """Open/update/resolve watchman incidents for the current analysis."""
    if analysis["status"] == "normal":
        resolved, prev_summaries = _resolve_open_watchman_incidents(
            db,
            now,
            resolved_summary="AI ops watchman returned to normal",
        )
        return ("resolved" if resolved else "none"), None, prev_summaries

    existing = (
        db.query(OperationalIncident)
        .filter(
            OperationalIncident.source == WATCHMAN_SOURCE,
            OperationalIncident.dedupe_key == analysis["dedupe_key"],
            OperationalIncident.status == OPERATIONAL_INCIDENT_STATUS_OPEN,
        )
        .order_by(OperationalIncident.opened_at.desc())
        .first()
    )
    if existing is None:
        incident = OperationalIncident(
            incident_type=analysis["incident_type"],
            source=WATCHMAN_SOURCE,
            dedupe_key=analysis["dedupe_key"],
            status=OPERATIONAL_INCIDENT_STATUS_OPEN,
            summary=analysis["summary"],
            context=_incident_context(analysis, usage, now),
            opened_at=now,
            last_observed_at=now,
        )
        db.add(incident)
        db.flush()
        incident_id = incident.id
        action = "opened"
    else:
        existing.incident_type = analysis["incident_type"]
        existing.summary = analysis["summary"]
        existing.last_observed_at = now
        new_ctx = _incident_context(analysis, usage, now)
        # Preserve email cooldown tracking so re-notification logic stays accurate.
        last_email_at = (existing.context or {}).get("last_email_at")
        if last_email_at:
            new_ctx["last_email_at"] = last_email_at
        existing.context = new_ctx
        incident_id = existing.id
        action = "updated"

    stale_rows = (
        db.query(OperationalIncident)
        .filter(
            OperationalIncident.source == WATCHMAN_SOURCE,
            OperationalIncident.status == OPERATIONAL_INCIDENT_STATUS_OPEN,
            OperationalIncident.dedupe_key != analysis["dedupe_key"],
        )
        .all()
    )
    for row in stale_rows:
        row.status = OPERATIONAL_INCIDENT_STATUS_RESOLVED
        row.summary = "AI ops watchman focus moved to a different issue"
        row.last_observed_at = now
        row.resolved_at = now
        context = dict(row.context or {})
        context["resolved_at"] = _iso(now)
        context["resolved_by_watchman"] = True
        row.context = context

    return action, incident_id, []


def _cooldown_elapsed(db: Session, incident_id: int, cooldown_minutes: int) -> bool:
    """Return True if enough time has passed since the last email for this incident."""
    incident = db.query(OperationalIncident).filter(OperationalIncident.id == incident_id).first()
    if incident is None:
        return False
    last_email_str = (incident.context or {}).get("last_email_at")
    if not last_email_str:
        return False  # Never emailed via cooldown path — "opened" handles the first send
    try:
        last_email_at = datetime.fromisoformat(last_email_str)
        if last_email_at.tzinfo is None:
            last_email_at = last_email_at.replace(tzinfo=timezone.utc)
        return (_utc_now() - last_email_at).total_seconds() / 60 >= cooldown_minutes
    except Exception:
        return False


def _stamp_email_sent(db: Session, incident_id: int | None) -> None:
    """Record last_email_at in the incident context after a successful send."""
    if incident_id is None:
        return
    incident = db.query(OperationalIncident).filter(OperationalIncident.id == incident_id).first()
    if incident is None:
        return
    ctx = dict(incident.context or {})
    ctx["last_email_at"] = _iso(_utc_now())
    incident.context = ctx


def maybe_send_watchman_email(
    db: Session,
    *,
    analysis: dict[str, Any],
    usage: dict[str, Any],
    incident_action: str,
    incident_id: int | None,
) -> bool:
    """Send an operator email for a watchman incident when warranted.

    Emails on incident open, and re-notifies for critical incidents after the
    cooldown period (OPS_WATCHMAN_CRITICAL_RESEND_MINUTES, default 30 min).
    """
    if not analysis["should_email"]:
        return False
    if analysis["status"] == "watch" and os.getenv("OPS_WATCHMAN_EMAIL_ON_WATCH", "0") != "1":
        return False

    if incident_action == "opened":
        should_send = True
    elif analysis["status"] == "critical" and incident_id is not None:
        cooldown = int(os.getenv("OPS_WATCHMAN_CRITICAL_RESEND_MINUTES", "30"))
        should_send = _cooldown_elapsed(db, incident_id, cooldown)
    else:
        should_send = False

    if not should_send:
        return False

    subject = analysis["title"] or f"AI ops watchman {analysis['status']}"
    body_lines = [
        f"AI Ops Watchman detected a {analysis['status']} condition.",
        "",
        f"Summary: {analysis['summary']}",
        f"Recommended action: {analysis['recommended_action']}",
        f"Incident action: {incident_action}",
    ]
    if incident_id is not None:
        body_lines.append(f"Incident id: {incident_id}")
    if analysis["evidence"]:
        body_lines.extend(["", "Evidence:"])
        body_lines.extend(f"- {item}" for item in analysis["evidence"])
    body_lines.extend(
        [
            "",
            "Usage:",
            f"- input_tokens: {usage.get('input_tokens')}",
            f"- output_tokens: {usage.get('output_tokens')}",
            f"- reasoning_tokens: {usage.get('reasoning_tokens')}",
            f"- estimated_cost_usd: {usage.get('estimated_cost_usd')}",
        ]
    )
    sent = bool(
        send_alert_email(
            subject,
            "\n".join(body_lines),
            level=analysis["status"].upper(),
            alert_type="ai_ops_watchman",
            job_id="ai-ops-watchman",
            metadata={"incident_id": incident_id, "analysis_status": analysis["status"]},
        )
    )
    if sent:
        _stamp_email_sent(db, incident_id)
    return sent


def maybe_send_resolution_email(*, resolved_summaries: list[str]) -> bool:
    """Send a resolution notification when the watchman clears back to normal."""
    if not resolved_summaries:
        return False
    body_lines = [
        "AI Ops Watchman: system returned to normal.",
        "",
        "Resolved:",
    ]
    body_lines.extend(f"- {s}" for s in resolved_summaries)
    return bool(
        send_alert_email(
            "RESOLVED (LONGHOUSE): AI ops watchman back to normal",
            "\n".join(body_lines),
            level="INFO",
            alert_type="ai_ops_watchman_resolved",
            job_id="ai-ops-watchman",
            metadata={"resolved_summaries": resolved_summaries},
        )
    )


async def run_watchman_cycle(*, db_session_factory) -> dict[str, Any]:
    """Collect observations, analyze them, persist run metadata, and reconcile incidents."""
    now = _utc_now()
    with db_session_factory() as db:
        run = OpsWatchRun(
            started_at=now,
            status="running",
            prompt_version=WATCHMAN_PROMPT_VERSION,
        )
        db.add(run)
        db.commit()
        db.refresh(run)

        observations = collect_observations(db, now=now)
        db.add_all(observations)
        db.commit()
        for row in observations:
            db.refresh(row)
        context = build_analysis_context(db, observations)
        run_id = run.id

    try:
        analysis, usage, model_id, skip_reason = await analyze_context(context)
    except Exception as exc:  # noqa: BLE001
        logger.exception("AI ops watchman analysis failed")
        finished = _utc_now()
        with db_session_factory() as db:
            run = db.query(OpsWatchRun).filter(OpsWatchRun.id == run_id).first()
            if run is not None:
                run.finished_at = finished
                run.status = "error"
                run.analysis_status = None
                run.model = _watchman_model_config()[0]
                run.error = str(exc)
            db.commit()
        return {"status": "failure", "analysis_status": "error", "error": str(exc), "observations": len(observations)}

    finished = _utc_now()
    with db_session_factory() as db:
        run = db.query(OpsWatchRun).filter(OpsWatchRun.id == run_id).first()
        if run is None:
            raise RuntimeError(f"OpsWatchRun {run_id} disappeared during watchman cycle")

        run.finished_at = finished
        run.model = model_id

        if analysis is None:
            run.status = "skipped"
            run.analysis_status = "skipped"
            run.result_json = {"skip_reason": skip_reason}
            run.usage_json = usage or None
            db.commit()
            return {
                "status": "success",
                "analysis_status": "skipped",
                "reason": skip_reason,
                "observations": len(observations),
            }

        run.status = "success"
        run.analysis_status = analysis["status"]
        run.input_tokens = usage.get("input_tokens")
        run.output_tokens = usage.get("output_tokens")
        run.total_tokens = usage.get("total_tokens")
        run.reasoning_tokens = usage.get("reasoning_tokens")
        run.estimated_cost_usd = usage.get("estimated_cost_usd")
        run.usage_json = usage

        incident_action, incident_id, resolved_summaries = reconcile_incident(
            db,
            analysis=analysis,
            usage=usage,
            now=finished,
        )
        email_sent = maybe_send_watchman_email(
            db,
            analysis=analysis,
            usage=usage,
            incident_action=incident_action,
            incident_id=incident_id,
        )
        resolution_email_sent = False
        if incident_action == "resolved":
            resolution_email_sent = maybe_send_resolution_email(resolved_summaries=resolved_summaries)
        run.result_json = {
            **analysis,
            "incident_action": incident_action,
            "incident_id": incident_id,
            "email_sent": email_sent,
            "resolution_email_sent": resolution_email_sent,
            "observation_count": len(observations),
        }
        db.commit()

    return {
        "status": "success",
        "analysis_status": analysis["status"],
        "incident_action": incident_action,
        "incident_id": incident_id,
        "email_sent": email_sent,
        "resolution_email_sent": resolution_email_sent,
        "observations": len(observations),
        "input_tokens": usage.get("input_tokens"),
        "estimated_cost_usd": usage.get("estimated_cost_usd"),
    }
