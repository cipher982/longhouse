"""Loop PWA web-push helpers."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from datetime import timezone
from typing import Any

from pywebpush import WebPushException
from pywebpush import webpush
from sqlalchemy.orm import Session

from zerg.config import get_settings
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionTurnReview
from zerg.models.loop_push_subscription import LoopPushSubscription

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_subscription_payload(subscription: dict[str, Any]) -> dict[str, Any]:
    endpoint = str(subscription.get("endpoint") or "").strip()
    keys = subscription.get("keys")
    if not endpoint:
        raise ValueError("subscription endpoint is required")
    if not isinstance(keys, dict):
        raise ValueError("subscription keys are required")

    p256dh = str(keys.get("p256dh") or "").strip()
    auth = str(keys.get("auth") or "").strip()
    if not p256dh or not auth:
        raise ValueError("subscription keys.p256dh and keys.auth are required")

    payload: dict[str, Any] = {
        "endpoint": endpoint,
        "keys": {
            "p256dh": p256dh,
            "auth": auth,
        },
    }
    expiration_time = subscription.get("expirationTime")
    if expiration_time is not None:
        payload["expirationTime"] = expiration_time
    return payload


def hash_push_endpoint(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


def upsert_loop_push_subscription(
    *,
    db: Session,
    owner_id: int,
    subscription: dict[str, Any],
    install_id: str | None = None,
    user_agent: str | None = None,
) -> LoopPushSubscription:
    payload = _normalize_subscription_payload(subscription)
    endpoint_hash = hash_push_endpoint(str(payload["endpoint"]))
    row = db.query(LoopPushSubscription).filter(LoopPushSubscription.endpoint_hash == endpoint_hash).first()

    now = _utcnow()
    if row is None:
        row = LoopPushSubscription(
            owner_id=owner_id,
            endpoint_hash=endpoint_hash,
            subscription_json=payload,
            install_id=(install_id or None),
            user_agent=(user_agent or None),
            revoked_at=None,
            last_error=None,
        )
        db.add(row)
    else:
        row.owner_id = owner_id
        row.subscription_json = payload
        row.install_id = install_id or row.install_id
        row.user_agent = user_agent or row.user_agent
        row.revoked_at = None
        row.last_error = None

    row.updated_at = now
    db.commit()
    db.refresh(row)
    return row


def revoke_loop_push_subscription(
    *,
    db: Session,
    owner_id: int,
    endpoint: str,
) -> bool:
    endpoint_hash = hash_push_endpoint(endpoint.strip())
    row = (
        db.query(LoopPushSubscription)
        .filter(
            LoopPushSubscription.owner_id == owner_id,
            LoopPushSubscription.endpoint_hash == endpoint_hash,
            LoopPushSubscription.revoked_at.is_(None),
        )
        .first()
    )
    if row is None:
        return False
    row.revoked_at = _utcnow()
    row.updated_at = _utcnow()
    db.commit()
    return True


def build_loop_push_payload(*, review: SessionTurnReview, session: AgentSession) -> dict[str, Any]:
    title = str(session.summary_title or review.summary or "Loop approval").strip()
    body = str(review.follow_up_prompt or review.summary or "A coding turn needs your attention.").strip()
    body = body[:220]
    return {
        "title": title,
        "body": body,
        "url": f"/loop/card/{int(review.id)}",
        "tag": f"loop-card-{int(review.id)}",
        "cardId": int(review.id),
        "sessionId": str(review.session_id),
        "decision": str(review.decision or "").strip(),
    }


def send_loop_push_nudge(
    *,
    db: Session,
    owner_id: int,
    review: SessionTurnReview,
    session: AgentSession,
) -> bool:
    settings = get_settings()
    if not settings.loop_push_enabled:
        return False

    rows = (
        db.query(LoopPushSubscription)
        .filter(
            LoopPushSubscription.owner_id == owner_id,
            LoopPushSubscription.revoked_at.is_(None),
        )
        .order_by(LoopPushSubscription.updated_at.desc(), LoopPushSubscription.id.desc())
        .all()
    )
    if not rows:
        return False

    payload = json.dumps(build_loop_push_payload(review=review, session=session))
    any_success = False
    now = _utcnow()

    for row in rows:
        try:
            webpush(
                subscription_info=row.subscription_json,
                data=payload,
                vapid_private_key=settings.loop_push_vapid_private_key,
                vapid_claims={"sub": str(settings.loop_push_vapid_subject)},
                ttl=3600,
            )
            row.last_push_at = now
            row.last_error = None
            row.updated_at = now
            any_success = True
        except WebPushException as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code in {404, 410}:
                row.revoked_at = now
            row.last_error = str(exc)
            row.updated_at = now
            logger.warning(
                "Loop push delivery failed",
                extra={
                    "owner_id": owner_id,
                    "review_id": int(review.id),
                    "subscription_id": int(row.id),
                    "status_code": status_code,
                },
            )
        except Exception as exc:  # pragma: no cover - safety net
            row.last_error = str(exc)
            row.updated_at = now
            logger.warning(
                "Loop push delivery raised unexpected error",
                extra={
                    "owner_id": owner_id,
                    "review_id": int(review.id),
                    "subscription_id": int(row.id),
                },
            )

    db.commit()
    return any_success
