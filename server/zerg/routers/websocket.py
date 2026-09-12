"""WebSocket routing module.

This module provides a FastAPI router for WebSocket connections
using a topic-based subscription system.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Optional
from urllib.parse import urlparse

import jwt
from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import WebSocket
from fastapi import WebSocketDisconnect

from zerg.auth.strategy import SESSION_COOKIE_NAME
from zerg.config import get_settings
from zerg.config import resolve_cors_origins
from zerg.database import reset_test_worker_id
from zerg.database import set_test_worker_id

# Auth helper --------------------------------------------------------------
from zerg.dependencies.auth import validate_ws_jwt
from zerg.generated.ws_messages import Envelope
from zerg.generated.ws_messages import ErrorData
from zerg.websocket.handlers import dispatch_message
from zerg.websocket.manager import topic_manager

router = APIRouter()
logger = logging.getLogger(__name__)


def _origin_is_allowed(origin: str) -> bool:
    """Same-site check for the handshake.

    Browsers exempt WebSocket upgrades from CORS, so ``Origin`` is the only
    same-site signal the handshake carries.  Loopback origins stay allowed:
    dev and E2E serve the frontend from assorted localhost ports that are
    never in a deployment's CORS allowlist.  Non-browser clients (engine, iOS)
    send no ``Origin`` at all and are not checked.
    """
    if origin in resolve_cors_origins(get_settings()):
        return True
    return urlparse(origin).hostname in {"localhost", "127.0.0.1", "::1"}


def _unverified_expiry(token: str | None) -> float | None:
    """Return a validated token's expiry for connection lifetime fencing.

    Authentication already verified the token before this helper runs. This
    second, unverified decode is only a timer input; a forged expiry cannot
    authorize a socket because ``validate_ws_jwt`` remains the gate.
    """
    if not token:
        return None
    try:
        payload = jwt.decode(token, options={"verify_signature": False})
        return float(payload["exp"])
    except (KeyError, TypeError, ValueError, jwt.PyJWTError):
        return None


async def _close_after_expiry(websocket: WebSocket, expires_at: float) -> None:
    delay = max(0.0, expires_at - time.time())
    await asyncio.sleep(delay)
    logger.info("WebSocket auth expired; closing connection")
    await websocket.close(code=4401, reason="Unauthorized")


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    initial_topics: Optional[str] = None,
):
    """WebSocket endpoint supporting topic-based subscriptions.

    Browser clients authenticate with the HttpOnly session cookie. Native/API
    clients must send ``Authorization: Bearer <runtime-token>`` in the
    handshake; credentials in query strings are deliberately unsupported so
    reverse proxies and browser history cannot capture them.

    Args:
        websocket: The WebSocket connection
        initial_topics: Optional comma-separated list of topics to subscribe to
            immediately upon connection (e.g., "user:1,ops:events")
    """
    client_id = str(uuid.uuid4())
    # E2E: capture worker id from query params to route DB sessions.
    worker_id = websocket.query_params.get("worker")
    worker_token = set_test_worker_id(worker_id) if worker_id else None
    logger.info(f"New WebSocket connection attempt from client {client_id}")

    origin = websocket.headers.get("origin")
    if origin and not _origin_is_allowed(origin):
        logger.info("WebSocket rejected cross-origin handshake from %s (client %s)", origin, client_id)
        await websocket.close(code=4403, reason="Forbidden origin")
        if worker_token is not None:
            reset_test_worker_id(worker_token)
        return

    # Authenticate before subscribing to any topic. Accepting solely to send
    # an explicit close frame is intentional: browsers otherwise surface a
    # pre-accept rejection as 1006 and cannot distinguish expired auth from a
    # network outage.
    auth_token = None
    auth_header = websocket.headers.get("authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        auth_token = auth_header[7:].strip()
    if not auth_token:
        auth_token = websocket.cookies.get(SESSION_COOKIE_NAME)

    try:
        user = await asyncio.to_thread(validate_ws_jwt, auth_token)
    except HTTPException as exc:
        if exc.status_code < 500:
            raise
        logger.warning("WebSocket auth authority unavailable for client %s", client_id)
        await websocket.accept()
        await websocket.close(code=4503, reason="Authentication service unavailable")
        if worker_token is not None:
            reset_test_worker_id(worker_token)
        return
    user_id = getattr(user, "id", None) if user is not None else None

    if user is None:
        logger.info("WebSocket auth failed – closing connection for client %s", client_id)
        await websocket.accept()
        await websocket.close(code=4401, reason="Unauthorized")
        if worker_token is not None:
            reset_test_worker_id(worker_token)
        return

    logger.debug("WebSocket auth succeeded for user %s (client %s)", user_id or "?", client_id)

    # Name the caller for the access log, in the same format the HTTP path
    # stamps (auth/strategy.py). ``validate_ws_jwt`` resolves the user above but
    # does not stamp, so without this the browser transcript stream -- the
    # stream most worth auditing -- logged "unattributed" on every authenticated
    # connection. The middleware reads scope["state"], and this write lands
    # there, so it must happen before ``accept()``: that is the message the
    # access log writes its line on.
    if user_id is not None:
        websocket.state.principal = f"user:{user_id}"

    expiry_task: asyncio.Task[None] | None = None

    try:
        await websocket.accept()
        await topic_manager.connect(client_id, websocket, user_id, auto_system=True, principal=user)
        await websocket.send_json({"type": "auth_ready"})
        logger.info(f"WebSocket connection established for client {client_id}")
        expires_at = _unverified_expiry(auth_token)
        if expires_at is not None:
            expiry_task = asyncio.create_task(_close_after_expiry(websocket, expires_at))

        # Handle initial topic subscriptions if provided
        if initial_topics:
            topics = [t.strip() for t in initial_topics.split(",")]
            msg_id = f"auto-subscribe-{uuid.uuid4()}"
            subscribe_envelope = Envelope.create(
                message_type="subscribe",
                topic="system",
                data={"topics": topics, "message_id": msg_id},
                req_id=msg_id,
            )
            await dispatch_message(client_id, subscribe_envelope.model_dump(), None)

        # Main message loop
        while True:
            try:
                # Receive outside db_session - WebSocket close shouldn't trigger DB rollback log
                raw_data = await websocket.receive_text()
                data = json.loads(raw_data)
                await dispatch_message(client_id, data, None)

            except json.JSONDecodeError as e:
                logger.warning(f"Invalid JSON from client {client_id}: {e}")
                error_envelope = Envelope.create(
                    message_type="error", topic="system", data=ErrorData(error="Invalid JSON payload").model_dump()
                )
                await websocket.send_json(error_envelope.model_dump())

    except WebSocketDisconnect:
        logger.info(f"WebSocket connection closed for client {client_id}")
    except Exception as e:
        logger.error(f"WebSocket error for client {client_id}: {str(e)}")
        try:
            error_envelope = Envelope.create(
                message_type="error", topic="system", data=ErrorData(error="Internal server error").model_dump()
            )
            await websocket.send_json(error_envelope.model_dump())
        except Exception as send_error:
            logger.debug("Could not send websocket error envelope to %s: %s", client_id, send_error)

    finally:
        if expiry_task is not None:
            expiry_task.cancel()
        await topic_manager.disconnect(client_id)
        if worker_token is not None:
            reset_test_worker_id(worker_token)
        logger.info(f"Cleaned up connection for client {client_id}")
