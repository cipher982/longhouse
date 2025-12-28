# Email Integration Testing Guide

Email integrations are hard to test end-to-end (OAuth, webhooks, async delivery). This doc focuses on what’s real in this repo today.

## What exists (today)

### Unit tests (default)

Located under `apps/zerg/backend/tests/`. These should be your first line of defense.

### Manual Gmail end-to-end script

If you want to test Gmail with a real account, use:

- `apps/zerg/backend/scripts/test_gmail_integration.py`

This script runs outside the app server and validates Gmail API auth + basic operations.

## Running the Gmail script

### 1) Install backend deps

```bash
cd apps/zerg/backend
uv sync
```

If the script prompts for missing Google libraries, add them to the backend environment with `uv` (don’t use `pip`):

```bash
cd apps/zerg/backend
uv add google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client
```

### 2) Provide OAuth credentials

Place your Google OAuth client file at:

- `apps/zerg/backend/scripts/credentials.json`

### 3) Run commands

```bash
cd apps/zerg/backend

# Initial OAuth setup (opens browser)
uv run python scripts/test_gmail_integration.py setup

# Send a test email to self
uv run python scripts/test_gmail_integration.py send

# Configure a Gmail watch (Pub/Sub side still requires GCP setup)
uv run python scripts/test_gmail_integration.py watch

# Check for new messages
uv run python scripts/test_gmail_integration.py check
```

## When you need webhooks (ngrok / public URL)

Webhook-driven flows (Gmail push → your `/api/email/*` endpoints) require a public URL. In dev, the easiest way is to tunnel the backend port and set `APP_PUBLIC_URL` accordingly.

For local dev stack, the backend is typically reachable at `http://localhost:47300` (host port mapped to the backend container). Point your tunnel at the host-exposed port you’re using.

## Notes

- Prefer adding coverage with unit/integration tests before relying on live OAuth/webhook flows.
- For the current “how to run tests in this repo” workflow, start at `AGENTS.md` and `docs/DEVELOPMENT.md`.
