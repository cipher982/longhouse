"""Shared runner credential checks for websocket and doctor preflight flows."""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from zerg.crud import runner_crud
from zerg.models.models import Runner

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunnerAuthResult:
    """Outcome of authenticating a runner by id or name + secret."""

    authenticated: bool
    reason_code: str
    summary: str
    runner: Runner | None = None


def authenticate_runner_identity(
    db: Session,
    *,
    runner_id: int | None = None,
    runner_name: str | None = None,
    secret: str | None = None,
) -> RunnerAuthResult:
    """Authenticate runner credentials without mutating state."""
    if not secret:
        return RunnerAuthResult(
            authenticated=False,
            reason_code="missing_secret",
            summary="Runner secret is missing.",
        )

    if not runner_id and not runner_name:
        return RunnerAuthResult(
            authenticated=False,
            reason_code="missing_identity",
            summary="Runner identity is missing. Provide runner_id or runner_name.",
        )

    computed_hash = runner_crud.hash_token(secret)
    runner: Runner | None = None

    if runner_id:
        runner = runner_crud.get_runner(db, runner_id)
        if not runner:
            return RunnerAuthResult(
                authenticated=False,
                reason_code="runner_not_found",
                summary=f"Longhouse does not know runner id {runner_id}.",
            )
    elif runner_name:
        stmt = select(Runner).where(Runner.name == runner_name)
        candidates = db.execute(stmt).scalars().all()
        if not candidates:
            return RunnerAuthResult(
                authenticated=False,
                reason_code="runner_not_found",
                summary=f"Longhouse does not know runner '{runner_name}'.",
            )

        matching = [candidate for candidate in candidates if secrets.compare_digest(computed_hash, candidate.auth_secret_hash)]
        if len(matching) > 1:
            logger.warning("Multiple runners matched name '%s' and the same secret hash; using first match", runner_name)
        runner = matching[0] if matching else candidates[0]

    if runner is None:
        return RunnerAuthResult(
            authenticated=False,
            reason_code="runner_not_found",
            summary="Longhouse could not resolve this runner.",
        )

    if not secrets.compare_digest(computed_hash, runner.auth_secret_hash):
        return RunnerAuthResult(
            authenticated=False,
            reason_code="invalid_secret",
            summary="Longhouse rejected the configured runner secret.",
            runner=runner,
        )

    if runner.status == "revoked":
        return RunnerAuthResult(
            authenticated=False,
            reason_code="runner_revoked",
            summary="This runner was revoked in Longhouse and cannot reconnect.",
            runner=runner,
        )

    return RunnerAuthResult(
        authenticated=True,
        reason_code="authenticated",
        summary="Longhouse accepted the configured runner credentials.",
        runner=runner,
    )
