"""WebSocket routing module.

This module provides a FastAPI router for WebSocket connections
using a topic-based subscription system.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

from fastapi import APIRouter
from fastapi import WebSocket
from fastapi import WebSocketDisconnect

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from zerg.database import db_session
from zerg.database import get_session_factory

# Auth helper --------------------------------------------------------------
from zerg.dependencies.auth import validate_ws_jwt
from zerg.generated.ws_messages import Envelope
from zerg.generated.ws_messages import ErrorData
from zerg.websocket.handlers import dispatch_message
from zerg.websocket.manager import topic_manager

router = APIRouter()
logger = logging.getLogger(__name__)


def get_websocket_session(session_factory: Optional[sessionmaker] = None) -> Session:
    """Create a new database session for WebSocket handlers.

    DEPRECATED: Use db_session() context manager instead.

    Args:
        session_factory: Optional custom session factory to use

    Returns:
        A SQLAlchemy Session object that must be closed by the caller
    """
    factory = session_factory or get_session_factory()
    return factory()


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
            immediately upon connection (e.g., "fiche:123,thread:45")
        token: Optional JWT from query param (for non-browser clients)

    Auth order:
    1. Query param token (for API clients)
    2. swarmlet_session cookie (for browser auth)
    """
    client_id = str(uuid.uuid4())
    logger.info(f"New WebSocket connection attempt from client {client_id}")

    # ------------------------------------------------------------------
    # Authenticate BEFORE accepting the WebSocket handshake.  If auth fails
    # we close with code 4401 and return early (Stage-8 hardening).
    # ------------------------------------------------------------------

    # Extract token: prefer query param, fall back to cookie
    auth_token = token
    if not auth_token:
        # Try to get token from session cookie (browser auth)
        auth_token = websocket.cookies.get("swarmlet_session")

    with db_session() as db_for_auth:
        user = validate_ws_jwt(auth_token, db_for_auth)
        # Extract user ID while still in session context to avoid DetachedInstanceError
        user_id = getattr(user, "id", None) if user is not None else None

    if user is None:
        # Auth failed and AUTH_DISABLED is *not* enabled.  We close the
        # connection *before* accepting the handshake so the browser sees a
        # clean 4401 closure code.  (4401 chosen to mirror HTTP 401.)
        logger.info("WebSocket auth failed – closing connection for client %s", client_id)
        await websocket.close(code=4401, reason="Unauthorized")
        return

    logger.debug("WebSocket auth succeeded for user %s (client %s)", user_id or "?", client_id)

    try:
        await websocket.accept()
        await topic_manager.connect(client_id, websocket, user_id, auto_system=True)
        logger.info(f"WebSocket connection established for client {client_id}")

        # Handle initial topic subscriptions if provided
        if initial_topics:
            with db_session() as db:
                topics = [t.strip() for t in initial_topics.split(",")]
                subscribe_msg = {
                    "type": "subscribe",
                    "topics": topics,
                    "message_id": f"auto-subscribe-{uuid.uuid4()}",
                }
                await dispatch_message(client_id, subscribe_msg, db)

        # Main message loop
        while True:
            try:
                # Receive outside db_session - WebSocket close shouldn't trigger DB rollback log
                raw_data = await websocket.receive_text()
                data = json.loads(raw_data)
                # Get a fresh DB session only for message processing
                with db_session() as db:
                    await dispatch_message(client_id, data, db)

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
        except Exception:
            pass
    finally:
        await topic_manager.disconnect(client_id)
        logger.info(f"Cleaned up connection for client {client_id}")
