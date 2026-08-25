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


# Every credential rejection looks identical to the caller. Both callers are
# unauthenticated surfaces (preflight, runner websocket), so telling "unknown
# runner" apart from "wrong secret" apart from "revoked" would let anyone
# enumerate which runners exist. The specific cause is logged server-side.
_CREDENTIALS_REJECTED = RunnerAuthResult(
    authenticated=False,
    reason_code="invalid_credentials",
    summary="Longhouse rejected these runner credentials.",
)


def authenticate_runner_identity(
    db: Session | None,
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

    if db is None:
        from zerg.services import runner_catalog

        result = runner_catalog.operation(
            "authenticate",
            runner_id=runner_id,
            runner_name=runner_name,
            secret_hash=computed_hash,
        )
        runner = runner_catalog.runner(result["runner"])

    elif runner_id:
        runner = runner_crud.get_runner(db, runner_id)
    elif runner_name:
        stmt = select(Runner).where(Runner.name == runner_name)
        candidates = db.execute(stmt).scalars().all()
        matching = [candidate for candidate in candidates if secrets.compare_digest(computed_hash, candidate.auth_secret_hash)]
        if len(matching) > 1:
            logger.warning("Multiple runners matched name '%s' and the same secret hash; using first match", runner_name)
        runner = matching[0] if matching else None

    if runner is None:
        logger.info("Runner auth rejected: no runner matches id=%s name=%s", runner_id, runner_name)
        return _CREDENTIALS_REJECTED

    if not secrets.compare_digest(computed_hash, runner.auth_secret_hash):
        logger.info("Runner auth rejected: wrong secret for runner %s", runner.id)
        return _CREDENTIALS_REJECTED

    if runner.status == "revoked":
        logger.info("Runner auth rejected: runner %s is revoked", runner.id)
        return _CREDENTIALS_REJECTED

    return RunnerAuthResult(
        authenticated=True,
        reason_code="authenticated",
        summary="Longhouse accepted the configured runner credentials.",
        runner=runner,
    )
