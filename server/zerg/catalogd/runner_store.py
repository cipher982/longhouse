"""Catalogd-owned persistence operations for the optional Runner subsystem."""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from zerg.crud import runner_crud
from zerg.models.models import Runner
from zerg.models.models import RunnerHealthIncident
from zerg.models.user import User


def _row(value) -> dict[str, Any] | None:
    if value is None:
        return None
    result: dict[str, Any] = {}
    for column in value.__table__.columns:
        item = getattr(value, column.name)
        result[column.name] = item.isoformat() if isinstance(item, datetime) else item
    return result


def execute_runner_operation(engine: Engine, operation: str, params: dict[str, Any]) -> dict[str, Any]:
    """Execute one bounded Runner operation on catalogd's serialized writer."""

    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    with SessionLocal() as db:
        if operation == "create_enroll_token":
            token, plaintext = runner_crud.create_enroll_token(db, owner_id=int(params["owner_id"]), ttl_minutes=int(params["ttl_minutes"]))
            return {"token": _row(token), "plaintext_token": plaintext}
        if operation == "register":
            token = runner_crud.validate_and_consume_enroll_token(db, str(params["enroll_token"]))
            if token is None:
                db.rollback()
                return {"status": "invalid_token"}
            owner_id = int(token.owner_id)
            name = str(params["name"])
            existing = runner_crud.get_runner_by_name(db, owner_id, name)
            if existing is not None and existing.status == "revoked":
                db.commit()
                return {"status": "revoked", "runner": _row(existing)}
            secret = runner_crud.generate_token()
            capabilities = runner_crud.normalize_capabilities(params.get("capabilities"))
            if existing is not None:
                existing.auth_secret_hash = runner_crud.hash_token(secret)
                existing.status = "offline"
                db.commit()
                db.refresh(existing)
                return {"status": "reenrolled", "runner": _row(existing), "runner_secret": secret}
            runner = runner_crud.create_runner(
                db,
                owner_id=owner_id,
                name=name,
                auth_secret=secret,
                availability_policy=params.get("availability_policy"),
                labels=params.get("labels"),
                capabilities=capabilities,
                metadata=params.get("metadata"),
            )
            return {"status": "created", "runner": _row(runner), "runner_secret": secret}
        if operation == "list":
            owner_id = int(params["owner_id"])
            if owner_id == 0:
                rows = db.query(Runner).offset(int(params.get("skip", 0))).limit(int(params.get("limit", 100))).all()
            else:
                rows = runner_crud.get_runners(db, owner_id, skip=int(params.get("skip", 0)), limit=int(params.get("limit", 100)))
            return {"runners": [_row(item) for item in rows]}
        if operation == "get":
            return {"runner": _row(runner_crud.get_runner(db, int(params["runner_id"])))}
        if operation == "get_by_name":
            return {"runner": _row(runner_crud.get_runner_by_name(db, int(params["owner_id"]), str(params["name"])))}
        if operation == "authenticate":
            secret_hash = str(params["secret_hash"])
            runner = None
            if params.get("runner_id") is not None:
                runner = runner_crud.get_runner(db, int(params["runner_id"]))
            else:
                candidates = db.execute(select(Runner).where(Runner.name == str(params["runner_name"]))).scalars().all()
                runner = next(
                    (candidate for candidate in candidates if secrets.compare_digest(secret_hash, candidate.auth_secret_hash)),
                    candidates[0] if candidates else None,
                )
            return {"runner": _row(runner)}
        if operation == "update":
            runner = runner_crud.update_runner(
                db,
                int(params["runner_id"]),
                name=params.get("name"),
                availability_policy=params.get("availability_policy"),
                labels=params.get("labels"),
                capabilities=params.get("capabilities"),
            )
            return {"runner": _row(runner)}
        if operation == "set_connection":
            runner = runner_crud.get_runner(db, int(params["runner_id"]))
            if runner is not None:
                runner.status = str(params["status"])
                if params.get("last_seen_at") is not None:
                    runner.last_seen_at = datetime.fromisoformat(str(params["last_seen_at"]))
                if params.get("metadata") is not None:
                    runner.runner_metadata = params["metadata"]
                db.commit()
                db.refresh(runner)
            return {"runner": _row(runner)}
        if operation == "revoke":
            return {"runner": _row(runner_crud.revoke_runner(db, int(params["runner_id"])))}
        if operation == "delete":
            return {"deleted": runner_crud.delete_runner(db, int(params["runner_id"]))}
        if operation == "rotate_secret":
            result = runner_crud.rotate_runner_secret(db, int(params["runner_id"]))
            if result is None:
                return {"runner": None}
            runner, plaintext = result
            runner.status = "offline"
            db.commit()
            return {"runner": _row(runner), "runner_secret": plaintext}
        if operation == "jobs":
            jobs = runner_crud.get_runner_jobs(
                db, int(params["runner_id"]), skip=int(params.get("skip", 0)), limit=int(params.get("limit", 100))
            )
            return {"jobs": [_row(item) for item in jobs]}
        if operation == "job_get":
            return {"job": _row(runner_crud.get_job(db, str(params["job_id"])))}
        if operation == "job_create":
            job = runner_crud.create_runner_job(
                db,
                owner_id=int(params["owner_id"]),
                runner_id=int(params["runner_id"]),
                command=str(params["command"]),
                timeout_secs=int(params["timeout_secs"]),
                correlation_id=params.get("correlation_id"),
                run_id=params.get("run_id"),
            )
            return {"job": _row(job)}
        if operation == "job_started":
            return {"job": _row(runner_crud.update_job_started(db, str(params["job_id"])))}
        if operation == "job_output":
            return {"job": _row(runner_crud.update_job_output(db, str(params["job_id"]), str(params["stream"]), str(params["data"])))}
        if operation == "job_completed":
            return {
                "job": _row(
                    runner_crud.update_job_completed(db, str(params["job_id"]), int(params["exit_code"]), int(params["duration_ms"]))
                )
            }
        if operation == "job_error":
            return {"job": _row(runner_crud.update_job_error(db, str(params["job_id"]), str(params["error"])))}
        if operation == "job_timeout":
            return {"job": _row(runner_crud.update_job_timeout(db, str(params["job_id"])))}
        if operation == "reset_online":
            count = db.query(Runner).filter(Runner.status == "online").update({Runner.status: "offline"})
            db.commit()
            return {"updated": int(count)}
        if operation == "health_apply":
            runner = runner_crud.get_runner(db, int(params["runner_id"]))
            if runner is None:
                return {"runner": None, "incident": None, "owner": None}
            observed_at = datetime.fromisoformat(str(params["observed_at"]))
            desired_status = runner.status if runner.status == "revoked" else str(params["effective_status"])
            runner.status = desired_status
            incident = (
                db.query(RunnerHealthIncident)
                .filter(
                    RunnerHealthIncident.runner_id == runner.id,
                    RunnerHealthIncident.incident_type == "offline",
                    RunnerHealthIncident.status == "open",
                )
                .order_by(RunnerHealthIncident.opened_at.desc())
                .first()
            )
            actionable_offline = desired_status == "offline" and runner.last_seen_at is not None and bool(params["proactive_attention"])
            if actionable_offline:
                if incident is None:
                    incident = RunnerHealthIncident(
                        owner_id=runner.owner_id,
                        runner_id=runner.id,
                        incident_type="offline",
                        status="open",
                        reason_code=str(params["reason_code"]),
                        summary=str(params["summary"]),
                        context=params.get("context") or {},
                        opened_at=observed_at,
                        last_observed_at=observed_at,
                    )
                    db.add(incident)
                else:
                    incident.reason_code = str(params["reason_code"])
                    incident.summary = str(params["summary"])
                    incident.last_observed_at = observed_at
                    incident.context = {**dict(incident.context or {}), **dict(params.get("context") or {})}
            elif incident is not None:
                incident.status = "resolved"
                incident.resolved_at = observed_at
                incident.last_observed_at = observed_at
                incident.reason_code = str(params["reason_code"])
                incident.summary = str(params["summary"])
                incident.context = {
                    **dict(incident.context or {}),
                    "resolved_at": observed_at.isoformat(),
                    "resolved_status_reason": str(params["reason_code"]),
                    "resolved_status_summary": str(params["summary"]),
                    "runner_name": runner.name,
                }
            db.commit()
            if incident is not None:
                db.refresh(incident)
            owner = db.query(User).filter(User.id == runner.owner_id).first()
            return {"runner": _row(runner), "incident": _row(incident), "owner": _row(owner)}
        if operation == "health_alert_sent":
            incident = db.get(RunnerHealthIncident, int(params["incident_id"]))
            if incident is not None and incident.alert_sent_at is None:
                observed_at = datetime.fromisoformat(str(params["observed_at"]))
                incident.alert_sent_at = observed_at
                incident.alert_channel = "email"
                incident.alert_count = int(incident.alert_count or 0) + 1
                incident.context = {
                    **dict(incident.context or {}),
                    "alert_sent_at": observed_at.isoformat(),
                    "alert_channel": "email",
                }
                db.commit()
                db.refresh(incident)
            return {"incident": _row(incident)}
        raise ValueError(f"unknown runner operation: {operation}")
