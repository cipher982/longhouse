"""Idempotency claim store for inbound surface events."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from zerg.models.surface_ingress import SurfaceIngressClaim


class SurfaceIdempotencyError(RuntimeError):
    """Raised when claim storage fails unexpectedly."""


@dataclass
class SurfaceIngressClaimStore:
    """Database-backed idempotency claim helper."""

    db: Session

    def claim(
        self,
        *,
        owner_id: int,
        surface_id: str,
        dedupe_key: str,
        conversation_id: str,
        source_event_id: str | None,
        source_message_id: str | None,
    ) -> bool:
        """Return True if claim inserted, False if duplicate.

        Raises SurfaceIdempotencyError on non-duplicate database failures.
        """
        claim = SurfaceIngressClaim(
            owner_id=owner_id,
            surface_id=surface_id,
            dedupe_key=dedupe_key,
            conversation_id=conversation_id,
            source_event_id=source_event_id,
            source_message_id=source_message_id,
        )
        self.db.add(claim)
        try:
            self.db.commit()
            return True
        except IntegrityError:
            self.db.rollback()
            return False
        except Exception as exc:  # noqa: BLE001
            self.db.rollback()
            raise SurfaceIdempotencyError("surface ingress claim failed") from exc
