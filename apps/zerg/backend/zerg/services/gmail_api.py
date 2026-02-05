"""Light-weight Gmail REST helpers used by the Gmail connector flow.

All functions are *safe* for the CI environment which has **no external
network access**:

* If the outgoing HTTPS request fails (e.g. due to DNS block) we log and
  return a *neutral* value so the caller can gracefully skip processing.

* Unit-tests that need deterministic responses can monkey-patch the public
  helpers – this pattern is already used widely across the test-suite.

The module purposefully keeps **zero third-party dependencies** – we re-use
`urllib.request` for the tiny HTTP calls to avoid bloating the runtime.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
import urllib.request
from typing import Any
from typing import Dict
from typing import List

from zerg.config import get_settings
from zerg.utils.retry import async_retry

# Unified settings snapshot for the module
_settings = get_settings()

logger = logging.getLogger(__name__)
# ---------------------------------------------------------------------------
# Push *watch* helpers
# ---------------------------------------------------------------------------

# Gmail delivers push notifications to Cloud Pub/Sub *or* direct HTTPS
# endpoints.  For the Fiche Platform we only support the **HTTPS** variant so
# users do not need a GCP project with Pub/Sub enabled.  The relevant REST
# endpoint is:
#
#     POST https://gmail.googleapis.com/gmail/v1/users/me/watch
#
# Request body example (JSON):
#     {
#         "topicName": "projects/my-project/topics/gmail",
#         "labelIds": ["INBOX"],
#         "labelFilterAction": "include"
#     }
#
# The response JSON contains two fields we care about:
#     • ``historyId``  – baseline to start *history* diff from.
#     • ``expiration`` – UNIX ms timestamp when the watch expires (max 7 days).
#
# In addition to creating the initial watch we also expose a small helper that
# simply **re-creates** the watch to renew it once the expiration comes
# close.  Gmail does not currently offer an explicit *renew* endpoint – the
# recommended workflow is to call *watch* again which implicitly stops the
# old channel and starts a new one.  Therefore ``renew_watch`` is merely an
# alias that calls ``start_watch``.


def _post_json(url: str, access_token: str, payload: Dict[str, Any]) -> Dict[str, Any]:  # noqa: D401 – helper
    data_bytes = json.dumps(payload, separators=(",", ":")).encode()
    req = urllib.request.Request(
        url,
        data=data_bytes,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310 – trusted URL
        return json.loads(resp.read().decode())


def start_watch(
    *,
    access_token: str,
    topic_name: str | None = None,
    callback_url: str | None = None,
    label_ids: List[str] | None = None,
) -> Dict[str, Any]:  # noqa: D401 – helper
    """Register (or re-register) a Gmail *watch*.

    Parameters
    ----------
    access_token
        Short-lived OAuth access token with the ``gmail.readonly`` scope.
    callback_url
        Public HTTPS URL that Google will POST push notifications to.
    label_ids
        If provided, limit notifications to specific labels (default:
        ``["INBOX"]``).

    Returns
    -------
    dict
        ``{"history_id": <int>, "watch_expiry": <int>}``
    """

    url = "https://gmail.googleapis.com/gmail/v1/users/me/watch"

    # Gmail requires a Pub/Sub topic for push notifications. In production,
    # pass a fully-qualified topic name (projects/<id>/topics/<name>).
    # For legacy/local test flows, some tests patch this function and use the
    # older callback_url param; if topic_name is not provided, we fall back
    # to using callback_url value for the field to keep tests/dev stubs simple.
    body = {
        "topicName": topic_name or (callback_url or ""),
        "labelFilterAction": "include",
        "labelIds": label_ids or ["INBOX"],
    }

    try:
        payload = _post_json(url, access_token, body)
    except Exception as exc:  # pragma: no cover – network / auth failure
        logger.warning("start_watch network failure: %s", exc)
        raise RuntimeError("gmail watch request failed") from exc

    try:
        history_id = int(payload["historyId"])
        expiry_ms = int(payload["expiration"])
    except Exception as exc:  # noqa: BLE001 – robust parsing
        raise RuntimeError(f"unexpected watch response: {payload}") from exc

    return {"history_id": history_id, "watch_expiry": expiry_ms}


def renew_watch(
    *,
    access_token: str,
    callback_url: str,
    label_ids: List[str] | None = None,
) -> Dict[str, Any]:  # noqa: D401 – helper
    """Renew an existing watch by creating a fresh one.

    Gmail recommends calling *watch* again once the current channel expires
    – no dedicated *renew* endpoint exists.  Therefore this helper simply
    delegates to ``start_watch``.
    """

    return start_watch(access_token=access_token, callback_url=callback_url, label_ids=label_ids)


# ---------------------------------------------------------------------------
# Stop Gmail push notifications helper
# ---------------------------------------------------------------------------


def stop_watch(*, access_token: str) -> bool:  # noqa: D401 – helper
    """Stop the current push channel for the Gmail *user*.

    Google’s API treats *stop* as idempotent – it clears all existing
    push channels for the authorised user and returns HTTP 204 on success.

    We return ``True`` when the request succeeds (HTTP 2xx) and ``False`` on
    any network / auth failure so callers can decide whether to retry or
    fall back silently.
    """

    url = "https://gmail.googleapis.com/gmail/v1/users/me/stop"

    try:
        # Re-use the helper so we inherit consistent headers & timeout.
        _post_json(url, access_token, {})  # body is empty JSON
        return True
    except Exception as exc:  # pragma: no cover – offline / auth error
        logger.warning("stop_watch network failure: %s", exc)
        return False


# ---------------------------------------------------------------------------
# OAuth helpers
# ---------------------------------------------------------------------------


def exchange_refresh_token(refresh_token: str) -> str:  # noqa: D401 – helper
    """Swap a *refresh_token* for a short-lived *access_token*.

    Follows Google OAuth 2.0 spec.  Raises ``RuntimeError`` on failure so the
    caller can handle back-off or retry.  The function **does not** attempt
    automatic retries because the service layer already loops regularly.
    """

    client_id = _settings.google_client_id
    client_secret = _settings.google_client_secret

    if not client_id or not client_secret:
        raise RuntimeError(
            "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET must be set to refresh tokens",
        )

    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }

    encoded = urllib.parse.urlencode(data).encode()

    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=encoded,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310
            payload: Dict[str, Any] = json.loads(resp.read().decode())
    except Exception as exc:  # pragma: no cover – network error
        logger.warning("Token refresh network failure: %s", exc)
        raise RuntimeError("token endpoint request failed") from exc

    access_token = payload.get("access_token")
    if not access_token:
        raise RuntimeError(f"invalid token response: {payload}")

    return access_token  # noqa: WPS331 – explicit return value


# ---------------------------------------------------------------------------
#   Async wrappers with retry – used by GmailProvider ------------------------
# ---------------------------------------------------------------------------


@async_retry(provider="gmail")
async def async_exchange_refresh_token(refresh_token: str) -> str:  # noqa: D401
    """Async wrapper for :func:`exchange_refresh_token` with retry."""

    return await asyncio.to_thread(exchange_refresh_token, refresh_token)


@async_retry(provider="gmail")
async def async_list_history(access_token: str, start_history_id: int):  # noqa: D401
    """Async wrapper for :func:`list_history` with retry."""

    return await asyncio.to_thread(list_history, access_token, start_history_id)


@async_retry(provider="gmail")
async def async_get_message_metadata(access_token: str, msg_id: str):  # noqa: D401
    """Async wrapper for :func:`get_message_metadata` with retry."""

    return await asyncio.to_thread(get_message_metadata, access_token, msg_id)


# ---------------------------------------------------------------------------
# Gmail History helpers
# ---------------------------------------------------------------------------


def _make_request(url: str, access_token: str):  # noqa: D401 – tiny helper
    from time import perf_counter

    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})

    start_ts = perf_counter()

    try:
        return urllib.request.urlopen(req, timeout=10)  # nosec B310 – trusted URL
    finally:
        # Prometheus histogram – ignore if metrics disabled
        try:
            from zerg.metrics import gmail_http_latency_seconds  # noqa: WPS433

            gmail_http_latency_seconds.observe(perf_counter() - start_ts)
        except Exception:  # pragma: no cover – metrics disabled or import fail
            pass


def list_history(access_token: str, start_history_id: int) -> List[Dict[str, Any]]:  # noqa: D401 – helper
    """Return *raw* history records newer than ``start_history_id``.

    The function automatically handles paging via ``nextPageToken`` and stops
    as soon as no further pages are indicated.  If any network error occurs
    an *empty list* is returned so the caller can degrade gracefully.
    """

    url_base = (
        "https://gmail.googleapis.com/gmail/v1/users/me/history"
        f"?startHistoryId={start_history_id}"
        "&historyTypes=messageAdded"
        "&maxResults=100"
    )

    history: List[Dict[str, Any]] = []
    page_token: str | None = None

    while True:  # pagination loop
        url = url_base + (f"&pageToken={page_token}" if page_token else "")

        try:
            with _make_request(url, access_token) as resp:  # type: ignore[attr-defined]
                payload: Dict[str, Any] = json.loads(resp.read().decode())
        except Exception as exc:  # pragma: no cover – network offline
            logger.warning("list_history network failure: %s", exc)
            return []  # graceful degradation

        history.extend(payload.get("history", []))

        page_token = payload.get("nextPageToken")
        if not page_token:
            break

    return history


def get_message_metadata(access_token: str, msg_id: str) -> Dict[str, Any]:  # noqa: D401 – helper
    """Fetch *minimal* metadata (From, Subject, labelIds) for ``msg_id``.

    On any error we return an **empty dict** so higher-level code can treat it
    as non-matching and continue.
    """

    url = (
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/" f"{msg_id}?format=metadata&metadataHeaders=From&metadataHeaders=Subject"
    )

    try:
        with _make_request(url, access_token) as resp:  # type: ignore[attr-defined]
            payload: Dict[str, Any] = json.loads(resp.read().decode())
    except Exception as exc:  # pragma: no cover – offline
        logger.warning("get_message_metadata network failure: %s", exc)
        return {}

    # Normalise headers list into a dict {Header: value}
    headers_list = payload.get("payload", {}).get("headers", [])
    headers_dict: Dict[str, str] = {h.get("name"): h.get("value") for h in headers_list if h.get("name")}

    return {
        "id": payload.get("id"),
        "labelIds": payload.get("labelIds", []),
        "headers": headers_dict,
    }


# ---------------------------------------------------------------------------
# Gmail Send helpers
# ---------------------------------------------------------------------------


def get_profile(access_token: str) -> Dict[str, Any]:
    """Get the authenticated user's Gmail profile.

    Returns dict with emailAddress, messagesTotal, threadsTotal, historyId.
    Returns empty dict on failure.
    """
    url = "https://gmail.googleapis.com/gmail/v1/users/me/profile"

    try:
        with _make_request(url, access_token) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        logger.warning("get_profile network failure: %s", exc)
        return {}


def send_email(
    access_token: str,
    to: str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
    from_email: str | None = None,
) -> str | None:
    """Send an email via Gmail API.

    Args:
        access_token: OAuth access token with gmail.send scope
        to: Recipient email address
        subject: Email subject
        body_text: Plain text body
        body_html: Optional HTML body (creates multipart message)
        from_email: Sender email (must match authenticated user or alias)

    Returns:
        Message ID if sent successfully, None on failure.

    Note:
        Requires the gmail.send OAuth scope. If the token doesn't have this
        scope, the API will return 403.
    """
    import base64
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    # Build MIME message
    if body_html:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body_text, "plain", "utf-8"))
        msg.attach(MIMEText(body_html, "html", "utf-8"))
    else:
        msg = MIMEText(body_text, "plain", "utf-8")

    msg["To"] = to
    msg["Subject"] = subject
    if from_email:
        msg["From"] = from_email

    # Base64url encode the message
    raw_message = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

    # Send via Gmail API
    url = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

    try:
        result = _post_json(url, access_token, {"raw": raw_message})
        message_id = result.get("id")
        logger.info("Gmail message sent: %s", message_id)
        return message_id
    except Exception as exc:
        logger.error("send_email failed: %s", exc)
        return None


@async_retry(provider="gmail")
async def async_get_profile(access_token: str) -> Dict[str, Any]:
    """Async wrapper for get_profile with retry."""
    return await asyncio.to_thread(get_profile, access_token)


@async_retry(provider="gmail")
async def async_send_email(
    access_token: str,
    to: str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
    from_email: str | None = None,
) -> str | None:
    """Async wrapper for send_email with retry."""
    return await asyncio.to_thread(
        send_email,
        access_token,
        to,
        subject,
        body_text,
        body_html,
        from_email,
    )
