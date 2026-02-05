# Gmail-Based Digest Delivery

## Problem

The current `daily-digest` job requires users to configure AWS SES credentials, domain verification, and env vars - terrible UX for an OSS tool.

## Solution

Use the user's connected Gmail account to send digest emails. Users who connect Gmail for email triggers automatically get digest capability with zero additional config.

## Architecture

```
User connects Gmail OAuth
       ↓
Connector stores encrypted refresh_token
       ↓
Daily digest job runs
       ↓
Check: User has Gmail connector + digest enabled?
       ↓
Exchange refresh_token → access_token (cached)
       ↓
Send email via Gmail API (from user to user)
```

## Implementation

### 1. Add Gmail Send Function (`gmail_api.py`)

```python
def send_email(
    access_token: str,
    to: str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
) -> str | None:
    """Send email via Gmail API.

    Args:
        access_token: OAuth access token with gmail.send scope
        to: Recipient email address
        subject: Email subject
        body_text: Plain text body
        body_html: Optional HTML body

    Returns:
        Message ID if sent, None on failure
    """
```

Implementation:
- Build MIME message (multipart if HTML provided)
- Base64url encode
- POST to `https://gmail.googleapis.com/gmail/v1/users/me/messages/send`
- Return message ID

Also add async wrapper: `async_send_email()`

### 2. Add User Digest Preferences

Add to `User` model or create `UserPreferences`:

```python
# Option A: Add columns to User model
digest_enabled = Column(Boolean, default=False)
digest_email = Column(String(255), nullable=True)  # Override recipient (default: self)
digest_cron = Column(String(50), default="0 8 * * *")

# Option B: JSON preferences column (more flexible)
preferences = Column(JSON, default={})
# {"digest": {"enabled": true, "email": null, "cron": "0 8 * * *"}}
```

Recommendation: Option A for simplicity - digests are a core feature.

### 3. Update Daily Digest Job

Modify `daily_digest.py`:

```python
async def run() -> dict[str, Any]:
    """Run daily digest for all users with digests enabled."""

    with db_session() as db:
        # Find users with digest enabled
        users = db.query(User).filter(User.digest_enabled == True).all()

    results = []
    for user in users:
        result = await send_user_digest(user.id)
        results.append(result)

    return {"users_processed": len(results), "results": results}


async def send_user_digest(user_id: int) -> dict:
    """Send digest for a single user."""

    with db_session() as db:
        user = db.query(User).get(user_id)

        # Get Gmail connector
        connector = db.query(Connector).filter(
            Connector.owner_id == user_id,
            Connector.type == "email",
            Connector.provider == "gmail",
        ).first()

        if not connector:
            return {"user_id": user_id, "error": "No Gmail connector"}

        # Get refresh token
        config = connector.config or {}
        enc_token = config.get("refresh_token")
        if not enc_token:
            return {"user_id": user_id, "error": "No refresh token"}

        refresh_token = crypto.decrypt(enc_token)

    # Exchange for access token
    access_token = await gmail_api.async_exchange_refresh_token(refresh_token)

    # Get user's email from Gmail profile
    profile = await gmail_api.async_get_profile(access_token)
    user_email = profile.get("emailAddress")

    # Fetch and summarize sessions (existing logic)
    # ...

    # Send via Gmail
    recipient = user.digest_email or user_email
    message_id = await gmail_api.async_send_email(
        access_token=access_token,
        to=recipient,
        subject=f"AI Coding Digest - {date_str}",
        body_text=plain_text,
        body_html=html,
    )

    return {"user_id": user_id, "success": True, "message_id": message_id}
```

### 4. OAuth Scope Update

Ensure Gmail OAuth requests `gmail.send` scope in addition to existing scopes.

Check `GOOGLE_GMAIL_SCOPES` in config - should include:
- `https://www.googleapis.com/auth/gmail.readonly` (existing)
- `https://www.googleapis.com/auth/gmail.send` (add if missing)

### 5. API Endpoints for Preferences

```python
# GET /api/users/me/preferences
# Returns user preferences including digest settings

# PATCH /api/users/me/preferences
# Update preferences
# Body: {"digest_enabled": true, "digest_email": "other@example.com"}
```

### 6. Remove SES Dependency (Optional)

After Gmail delivery works:
- Remove `send_digest_email` from `shared/email.py` usage in digest
- Keep SES for alerts (system-level, not user-facing)

## File Changes Summary

| File | Change |
|------|--------|
| `zerg/services/gmail_api.py` | Add `send_email`, `async_send_email`, `get_profile`, `async_get_profile` |
| `zerg/models/user.py` | Add `digest_enabled`, `digest_email`, `digest_cron` columns |
| `zerg/jobs/daily_digest.py` | Rewrite to iterate users, use Gmail connector |
| `zerg/routers/users.py` | Add preferences endpoints |
| `zerg/config/__init__.py` | Verify `gmail.send` scope included |
| Alembic migration | Add user preference columns |

## Testing

1. **Unit tests**: Mock Gmail API, verify send_email builds correct MIME
2. **Integration test**:
   - Create user with Gmail connector
   - Enable digest
   - Run job
   - Verify Gmail API called with correct params
3. **Manual test**:
   - Connect real Gmail account
   - Enable digest
   - Trigger job manually
   - Check email arrives

## Rollout

1. Add Gmail send function (backwards compatible)
2. Add user preference columns (migration)
3. Update digest job
4. Add API endpoints
5. Frontend toggle (separate PR)

## Open Questions

1. Should digest be opt-in (default false) or opt-out (default true for users with Gmail)?
2. Should we support multiple digest recipients?
3. Weekly digest option in addition to daily?
