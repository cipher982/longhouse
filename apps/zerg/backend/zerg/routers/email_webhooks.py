"""Legacy Gmail HTTPS webhook kept only for tests/local compatibility.

Production Gmail push for Zerg uses Cloud Pub/Sub via
``/email/webhook/google/pubsub``. This direct HTTPS callback path is retained
only so older tests and local experiments do not need a live Pub/Sub setup.
It should not be mounted into the normal first-party runtime surface.
"""

# typing helpers
from __future__ import annotations

import logging
from typing import Dict
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Header
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session

from zerg.config import get_settings
from zerg.database import get_db

# Replace direct env look-up with unified Settings helper
_settings = get_settings()

# Event publication and fiche execution are handled by the Gmail provider.
# The router keeps webhook latency minimal by enqueuing connector processing.

logger = logging.getLogger(__name__)


router = APIRouter(tags=["email-webhooks"])

# ---------------------------------------------------------------------------
# Helper – clamp request body size before any heavy processing/HMAC.
# ---------------------------------------------------------------------------

MAX_BODY_BYTES = 128 * 1024  # 128 KiB


async def _clamp_body_size(request: Request):  # noqa: D401 – dependency
    """Reject requests with bodies larger than *MAX_BODY_BYTES*."""

    # Prefer Content-Length header to avoid reading the body twice.  If the
    # header is missing we read the body anyways (stream consumed only once
    # by FastAPI) and compare len().

    cl_header = request.headers.get("content-length")
    if cl_header and cl_header.isdigit():
        if int(cl_header) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Request body too large")
        return  # Size acceptable – don't consume body.

    # Fallback – read body bytes *once* and stash in request.state so the
    # route handler can access it without re-awaiting .body().  FastAPI docs
    # allow this pattern.

    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Request body too large")

    request.state.raw_body = raw  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Gmail push notification callback
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Helper – optional JWT validation
# ---------------------------------------------------------------------------


# Import Optional for 3.9-compatible type hints


def _validate_google_jwt(auth_header: Optional[str]):  # noqa: D401 – helper
    """Validate Google-signed JWT contained in ``Authorization: Bearer …``.

    Validation is **always ON** in dev/staging/prod.  The check is skipped
    only when the environment variable `TESTING=1` is present (unit-test
    runner) *or* when the optional `google-auth` dependency is not installed
    in a local *dev* environment.
    """

    # During automated unit-tests we run without Google-signed requests.
    # Skip validation when the **TESTING** env var is set so the suite does
    # not need to embed real JWTs.  This keeps runtime behaviour unchanged
    # for dev & prod which never set TESTING.

    if _settings.testing:
        return

    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    parts = auth_header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid Authorization header format")

    token = parts[1]

    try:
        from google.auth.transport import requests as google_requests  # type: ignore
        from google.oauth2 import id_token  # type: ignore

        id_token.verify_oauth2_token(token, google_requests.Request())
    except ModuleNotFoundError:  # pragma: no cover – dependency missing in dev
        # Allow missing dependency in local dev; production images vendor the
        # wheel so the import succeeds.  Skipping validation is acceptable
        # on localhost given the attacker would have to reach the machine
        # directly.
        return
    except Exception as exc:  # broad – any verification error
        raise HTTPException(status_code=401, detail="Invalid Google JWT") from exc


# Declare *Authorization* header so FastAPI docs list it.  The runtime helper
# enforces presence except when `TESTING=1`.


@router.post(
    "/email/webhook/google",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(_clamp_body_size)],
)
async def gmail_webhook(
    *,
    x_goog_channel_token: str = Header(..., alias="X-Goog-Channel-Token"),
    x_goog_resource_id: Optional[str] = Header(None, alias="X-Goog-Resource-Id"),
    x_goog_message_number: Optional[str] = Header(None, alias="X-Goog-Message-Number"),
    authorization: Optional[str] = Header(None, alias="Authorization"),
    payload: Optional[Dict] = None,  # Google sends no body – future-proof
    db: Session = Depends(get_db),
):
    """Handle legacy Gmail HTTPS watch callbacks in testing/local mode only."""

    logger.debug(
        "Gmail webhook: token=%s, resource_id=%s, msg_no=%s",
        x_goog_channel_token,
        x_goog_resource_id,
        x_goog_message_number,
    )

    # ------------------------------------------------------------------
    # Validate Google-signed JWT (optional)
    # ------------------------------------------------------------------

    try:
        _validate_google_jwt(authorization)
    except HTTPException:
        raise  # re-raise so FastAPI sends proper response
    except Exception as exc:  # pragma: no cover – should not happen
        logger.exception("Unexpected JWT validation error: %s", exc)
        raise HTTPException(status_code=500, detail="JWT validation internal error") from exc

    # For now we require *some* channel token so accidental public hits are
    # rejected with 400 rather than executing arbitrary triggers.
    if not x_goog_channel_token:
        raise HTTPException(status_code=400, detail="Missing X-Goog-Channel-Token header")

    # ------------------------------------------------------------------
    # Connector-centric processing: token contains connector_id
    # ------------------------------------------------------------------

    try:
        connector_id = int(x_goog_channel_token)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid X-Goog-Channel-Token")

    # Dedupe by message number at connector level
    from sqlalchemy.orm.attributes import flag_modified

    from zerg.models.models import Connector as ConnectorModel

    conn = db.query(ConnectorModel).filter(ConnectorModel.id == connector_id).first()
    if conn is None:
        raise HTTPException(status_code=404, detail="Connector not found")

    msg_no_int: Optional[int] = None
    if x_goog_message_number and x_goog_message_number.isdigit():
        msg_no_int = int(x_goog_message_number)

    cfg = dict(conn.config or {})
    last_seen = int(cfg.get("last_msg_no", 0))
    if msg_no_int is not None and msg_no_int <= last_seen:
        return {"status": "accepted", "trigger_count": 0}

    if msg_no_int is not None:
        cfg["last_msg_no"] = msg_no_int
        conn.config = cfg  # type: ignore[assignment]
        try:
            flag_modified(conn, "config")
        except Exception:
            pass
        db.add(conn)
        db.commit()

    from zerg.email.providers import get_provider

    gmail_provider = get_provider("gmail")
    if gmail_provider is None:
        logger.error("Gmail provider missing from registry – cannot process connector %s", connector_id)
        return {"status": "accepted", "trigger_count": 0}

    # Offload connector processing to background to keep webhook fast.
    import asyncio

    async def _bg() -> None:
        try:
            await gmail_provider.process_connector(connector_id)
        except Exception as exc:  # pragma: no cover – background guard
            logger.exception(
                "gmail-connector-process-failed",
                exc_info=exc,
                connector_id=connector_id,
                extra={"connector_id": connector_id, "error_type": type(exc).__name__},
            )
            # Increment error metric for monitoring
            from zerg.metrics import gmail_webhook_error_total

            gmail_webhook_error_total.inc()

    asyncio.create_task(_bg())

    # Processing happens asynchronously; trigger_count is not known here.
    return {"status": "accepted", "trigger_count": 0}
