"""WebSocket routing module.

This module provides a FastAPI router for WebSocket connections
using a topic-based subscription system.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter
from fastapi import WebSocket
from fastapi import WebSocketDisconnect

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


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    initial_topics: Optional[str] = None,
    token: Optional[str] = None,
):
    """WebSocket endpoint supporting topic-based subscriptions.

    Args:
        websocket: The WebSocket connection
        initial_topics: Optional comma-separated list of topics to subscribe to
            immediately upon connection (e.g., "user:1,ops:events")
        token: Optional JWT from query param (for non-browser clients)

    Auth order:
    1. Query param token (for API clients)
    2. longhouse_session cookie (for browser auth)
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

    # ------------------------------------------------------------------
    # Authenticate BEFORE accepting the WebSocket handshake.  If auth fails
    # we close with code 4401 and return early (Stage-8 hardening).
    # ------------------------------------------------------------------

    # Extract token: prefer query param, fall back to cookie
    auth_token = token
    if not auth_token:
        # Try to get token from session cookie (browser auth)
        auth_token = websocket.cookies.get("longhouse_session")

    user = await asyncio.to_thread(validate_ws_jwt, auth_token)
    user_id = getattr(user, "id", None) if user is not None else None

    if user is None:
        # Auth failed and AUTH_DISABLED is *not* enabled.  We close the
        # connection *before* accepting the handshake so the browser sees a
        # clean 4401 closure code.  (4401 chosen to mirror HTTP 401.)
        logger.info("WebSocket auth failed – closing connection for client %s", client_id)
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

    try:
        await websocket.accept()
        await topic_manager.connect(client_id, websocket, user_id, auto_system=True, principal=user)
        logger.info(f"WebSocket connection established for client {client_id}")

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
        await topic_manager.disconnect(client_id)
        if worker_token is not None:
            reset_test_worker_id(worker_token)
        logger.info(f"Cleaned up connection for client {client_id}")
