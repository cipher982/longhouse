"""OAuth routes for third-party connector authorization.

Implements OAuth 2.0 Authorization Code flow for services like GitHub.
Users click "Connect" -> redirect to provider -> callback with code -> exchange for token.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import status
from fastapi.responses import HTMLResponse
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from zerg.config import get_settings
from zerg.database import get_db
from zerg.dependencies.auth import get_current_user
from zerg.models.models import AccountConnectorCredential
from zerg.models.models import User
from zerg.utils.crypto import encrypt

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/oauth", tags=["oauth"])

_settings = get_settings()

# ---------------------------------------------------------------------------
# GitHub OAuth Configuration
# ---------------------------------------------------------------------------

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"

# Scopes needed for GitHub integration
# - repo: Full control of private repositories (read/write issues, PRs, code)
# - read:user: Read user profile data
GITHUB_SCOPES = "repo read:user"


# ---------------------------------------------------------------------------
# State management (in-memory for simplicity, use Redis in production)
# ---------------------------------------------------------------------------

# Maps state token -> user_id for CSRF protection
_oauth_states: dict[str, int] = {}


def _generate_state(user_id: int) -> str:
    """Generate a secure random state token and associate with user."""
    state = secrets.token_urlsafe(32)
    _oauth_states[state] = user_id
    return state


def _validate_state(state: str) -> int | None:
    """Validate state token and return associated user_id, consuming the token."""
    return _oauth_states.pop(state, None)


# ---------------------------------------------------------------------------
# GitHub OAuth Routes
# ---------------------------------------------------------------------------


@router.get("/github/authorize")
def github_authorize(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> RedirectResponse:
    """Initiate GitHub OAuth flow.

    Redirects user to GitHub's authorization page. After approval,
    GitHub redirects back to /oauth/github/callback with an authorization code.
    """
    if not _settings.github_client_id:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="GitHub OAuth not configured (missing GITHUB_CLIENT_ID)",
        )

    # Generate CSRF state token
    state = _generate_state(current_user.id)

    # Build callback URL from request
    # In production, use APP_PUBLIC_URL; in dev, derive from request
    if _settings.app_public_url:
        callback_url = f"{_settings.app_public_url.rstrip('/')}/api/oauth/github/callback"
    else:
        callback_url = str(request.url_for("github_callback"))

    # Build GitHub authorization URL
    params = {
        "client_id": _settings.github_client_id,
        "redirect_uri": callback_url,
        "scope": GITHUB_SCOPES,
        "state": state,
    }

    auth_url = f"{GITHUB_AUTHORIZE_URL}?{urlencode(params)}"

    logger.info("Redirecting user %d to GitHub OAuth", current_user.id)
    return RedirectResponse(url=auth_url, status_code=302)


@router.get("/github/callback")
def github_callback(
    code: str = Query(...),
    state: str = Query(...),
    error: str | None = Query(None),
    error_description: str | None = Query(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Handle GitHub OAuth callback.

    GitHub redirects here with an authorization code. We exchange it for
    an access token and store it encrypted in the database.

    Returns an HTML page that closes the popup and notifies the parent window.
    """
    # Handle OAuth errors from GitHub
    if error:
        logger.warning("GitHub OAuth error: %s - %s", error, error_description)
        return _oauth_result_page(
            success=False,
            error=error_description or error,
        )

    # Validate CSRF state token
    user_id = _validate_state(state)
    if user_id is None:
        logger.warning("Invalid or expired OAuth state token")
        return _oauth_result_page(
            success=False,
            error="Invalid or expired authorization request. Please try again.",
        )

    if not _settings.github_client_id or not _settings.github_client_secret:
        logger.error("GitHub OAuth credentials not configured")
        return _oauth_result_page(
            success=False,
            error="GitHub OAuth not configured on server",
        )

    # Build callback URL (must match the one used in authorize)
    if _settings.app_public_url:
        callback_url = f"{_settings.app_public_url.rstrip('/')}/api/oauth/github/callback"
    else:
        # Fallback - reconstruct from current request context
        callback_url = "http://localhost:8000/api/oauth/github/callback"

    # Exchange authorization code for access token
    try:
        token_response = httpx.post(
            GITHUB_TOKEN_URL,
            data={
                "client_id": _settings.github_client_id,
                "client_secret": _settings.github_client_secret,
                "code": code,
                "redirect_uri": callback_url,
            },
            headers={"Accept": "application/json"},
            timeout=10.0,
        )
        token_response.raise_for_status()
        token_data = token_response.json()
    except httpx.HTTPError as e:
        logger.exception("GitHub token exchange failed")
        return _oauth_result_page(
            success=False,
            error=f"Failed to exchange code for token: {str(e)}",
        )

    if "error" in token_data:
        logger.warning("GitHub token error: %s", token_data)
        return _oauth_result_page(
            success=False,
            error=token_data.get("error_description", token_data.get("error")),
        )

    access_token = token_data.get("access_token")
    if not access_token:
        logger.error("No access_token in GitHub response: %s", token_data)
        return _oauth_result_page(
            success=False,
            error="GitHub did not return an access token",
        )

    # Fetch user info to get username
    try:
        user_response = httpx.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=10.0,
        )
        user_response.raise_for_status()
        github_user = user_response.json()
        github_login = github_user.get("login", "Unknown")
        github_name = github_user.get("name")

        # Get scopes from response headers
        scopes = user_response.headers.get("X-OAuth-Scopes", "")
        scope_list = [s.strip() for s in scopes.split(",") if s.strip()]
    except httpx.HTTPError:
        logger.exception("Failed to fetch GitHub user info")
        github_login = "Unknown"
        github_name = None
        scope_list = []

    # Store credentials in database
    credentials = {"token": access_token}
    encrypted = encrypt(json.dumps(credentials))

    # Upsert: check if credential exists for this user
    existing = (
        db.query(AccountConnectorCredential)
        .filter(
            AccountConnectorCredential.owner_id == user_id,
            AccountConnectorCredential.connector_type == "github",
        )
        .first()
    )

    metadata = {
        "login": github_login,
        "name": github_name,
        "scopes": scope_list,
        "connected_via": "oauth",
    }

    if existing:
        existing.encrypted_value = encrypted
        existing.display_name = f"@{github_login}"
        existing.test_status = "success"
        existing.last_tested_at = datetime.utcnow()
        existing.connector_metadata = metadata
        logger.info("Updated GitHub credentials for user %d via OAuth", user_id)
    else:
        cred = AccountConnectorCredential(
            owner_id=user_id,
            connector_type="github",
            encrypted_value=encrypted,
            display_name=f"@{github_login}",
            test_status="success",
            last_tested_at=datetime.utcnow(),
            connector_metadata=metadata,
        )
        db.add(cred)
        logger.info("Created GitHub credentials for user %d via OAuth", user_id)

    db.commit()

    return _oauth_result_page(
        success=True,
        provider="github",
        username=github_login,
    )


# ---------------------------------------------------------------------------
# Helper: OAuth result page
# ---------------------------------------------------------------------------


def _oauth_result_page(
    success: bool,
    provider: str = "github",
    username: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    """Return an HTML page that communicates OAuth result to parent window.

    This page is shown in the OAuth popup. It uses postMessage to notify
    the parent window of success/failure, then closes itself.
    """
    result = {
        "success": success,
        "provider": provider,
    }
    if username:
        result["username"] = username
    if error:
        result["error"] = error

    result_json = json.dumps(result)

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>{"Connected!" if success else "Connection Failed"}</title>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                display: flex;
                justify-content: center;
                align-items: center;
                height: 100vh;
                margin: 0;
                background: #0d1117;
                color: #c9d1d9;
            }}
            .container {{
                text-align: center;
                padding: 2rem;
            }}
            .icon {{
                font-size: 4rem;
                margin-bottom: 1rem;
            }}
            h1 {{
                font-size: 1.5rem;
                margin-bottom: 0.5rem;
            }}
            p {{
                color: #8b949e;
                margin-bottom: 1rem;
            }}
            .error {{
                color: #f85149;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="icon">{"✓" if success else "✗"}</div>
            <h1>{"GitHub Connected!" if success else "Connection Failed"}</h1>
            <p>{f"Connected as @{username}" if success and username else error or "This window will close automatically."}</p>
        </div>
        <script>
            // Send result to parent window
            if (window.opener) {{
                window.opener.postMessage({result_json}, '*');
            }}
            // Close popup after brief delay
            setTimeout(function() {{
                window.close();
            }}, {1500 if success else 3000});
        </script>
    </body>
    </html>
    """

    return HTMLResponse(content=html)
