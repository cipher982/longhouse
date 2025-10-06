"""Pub/Sub endpoint for Gmail push notifications.

This module handles the production-ready Gmail push flow using Cloud Pub/Sub
with OIDC token validation.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any
from typing import Dict

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session

from zerg.config import get_settings
from zerg.crud import crud
from zerg.database import get_db
from zerg.models.models import Connector

logger = logging.getLogger(__name__)
# Align path with existing webhook routes under "/email/webhook" so the
# final endpoint becomes "/api/email/webhook/google/pubsub" once mounted
# with the global API prefix in main.py.
router = APIRouter(prefix="/email/webhook", tags=["email-webhooks"])


def validate_pubsub_token(authorization: str | None) -> bool:
    """Validate the OIDC token from Pub/Sub push.

    Args:
        authorization: The Authorization header value

    Returns:
        True if valid, False otherwise
    """
    if not authorization or not authorization.startswith("Bearer "):
        return False

    # Always validate in both dev and prod; tests explicitly monkeypatch this
    # function when they want to bypass validation.

    # Extract token
    token = authorization[7:]  # Remove "Bearer " prefix

    try:
        import jwt
        from jwt import PyJWKClient

        # Google's OIDC discovery endpoint
        jwks_client = PyJWKClient("https://www.googleapis.com/oauth2/v3/certs")

        # Decode and validate
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        decoded = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=getattr(get_settings(), "pubsub_audience", None),
            issuer="https://accounts.google.com",
            options={"verify_exp": True},
        )

        # Verify service account email if configured; otherwise accept any
        # service account principal (email ends with gserviceaccount.com).
        sa_email_cfg = getattr(get_settings(), "pubsub_sa_email", None)
        email_claim = decoded.get("email")
        if sa_email_cfg:
            if email_claim == sa_email_cfg:
                return True
        else:
            # Best-effort heuristic: service account principals end with this domain
            if isinstance(email_claim, str) and email_claim.endswith("gserviceaccount.com"):
                return True

    except Exception as e:
        logger.warning("OIDC token validation failed", exc_info=e)

    return False


@router.post("/google/pubsub", status_code=status.HTTP_202_ACCEPTED)
async def gmail_pubsub_webhook(
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Handle Gmail push notifications via Cloud Pub/Sub.

    This endpoint receives Pub/Sub push messages containing Gmail notification
    data. The message includes the email address and history ID which we use
    to map to the appropriate connector.

    Returns:
        202 Accepted with status message
    """
    # Validate OIDC token
    authorization = request.headers.get("authorization")
    if not validate_pubsub_token(authorization):
        logger.warning("Invalid OIDC token in Pub/Sub push")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
        )

    # Parse Pub/Sub message
    try:
        body = await request.json()
        message = body.get("message", {})

        # Decode the data payload
        if "data" in message:
            data_bytes = base64.b64decode(message["data"])
            data = json.loads(data_bytes)
        else:
            data = {}

        # Extract Gmail notification data
        email_address = data.get("emailAddress")
        history_id = data.get("historyId")

        if not email_address:
            logger.warning("Pub/Sub message missing emailAddress")
            return {"status": "rejected", "reason": "missing_email"}

    except Exception as e:
        logger.error("Failed to parse Pub/Sub message", exc_info=e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid message format",
        )

    # Find connector by email address
    # We need to search through connectors for matching emailAddress in config
    connectors = (
        db.query(Connector)
        .filter(
            Connector.type == "email",
            Connector.provider == "gmail",
        )
        .all()
    )

    matching_connector = None
    for conn in connectors:
        config = conn.config or {}
        if config.get("emailAddress") == email_address:
            matching_connector = conn
            break

    if not matching_connector:
        logger.info(
            "No connector found for email address",
            extra={"email_address": email_address},
        )
        return {"status": "ignored", "reason": "no_connector"}

    # Update history_id if provided
    if history_id:
        config = dict(matching_connector.config or {})
        current_history_id = int(config.get("history_id", 0))
        new_history_id = int(history_id)

        # Only update if newer
        if new_history_id > current_history_id:
            config["history_id"] = new_history_id
            crud.update_connector(db, matching_connector.id, config=config)

    # Trigger async processing
    from zerg.email.providers import get_provider

    gmail_provider = get_provider("gmail")
    if not gmail_provider:
        logger.error("Gmail provider not registered")
        return {"status": "error", "reason": "provider_missing"}

    import asyncio

    async def _process_connector():
        # Track processing with metrics
        from zerg.metrics import pubsub_webhook_processing

        pubsub_webhook_processing.inc()

        try:
            await gmail_provider.process_connector(matching_connector.id)
            logger.info(
                "Pub/Sub triggered connector processing",
                extra={"connector_id": matching_connector.id},
            )
        except Exception as e:
            logger.exception(
                "Failed to process connector from Pub/Sub",
                exc_info=e,
                extra={"connector_id": matching_connector.id},
            )
            # Increment error metric
            from zerg.metrics import gmail_webhook_error_total

            gmail_webhook_error_total.inc()
        finally:
            # Decrement processing gauge
            pubsub_webhook_processing.dec()

    asyncio.create_task(_process_connector())

    # Return 202 to acknowledge receipt
    return {
        "status": "accepted",
        "connector_id": matching_connector.id,
        "email_address": email_address,
    }
