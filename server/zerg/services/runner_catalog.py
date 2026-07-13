"""Runtime-side typed facade for catalogd-owned Runner rows."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime

from zerg.catalogd.client import call_catalogd_sync
from zerg.models.models import Runner
from zerg.models.models import RunnerEnrollToken
from zerg.models.models import RunnerHealthIncident
from zerg.models.models import RunnerJob
from zerg.models.user import User
from zerg.services.catalogd_supervisor import catalogd_paths


def _call(operation: str, **params: Any) -> dict[str, Any]:
    _database_path, socket_path = catalogd_paths()
    return call_catalogd_sync(
        socket_path,
        "runner.operation.v2",
        params={"operation": operation, "params": params},
        timeout_seconds=2.0,
    )


def _model(model_type, payload: dict[str, Any] | None):
    if payload is None:
        return None
    values = dict(payload)
    for column in model_type.__table__.columns:
        if isinstance(column.type, DateTime) and isinstance(values.get(column.name), str):
            values[column.name] = datetime.fromisoformat(values[column.name])
    return model_type(**values)


def runner(payload):
    return _model(Runner, payload)


def job(payload):
    return _model(RunnerJob, payload)


def token(payload):
    return _model(RunnerEnrollToken, payload)


def incident(payload):
    return _model(RunnerHealthIncident, payload)


def user(payload):
    return _model(User, payload)


def operation(operation_name: str, **params: Any) -> dict[str, Any]:
    return _call(operation_name, **params)
