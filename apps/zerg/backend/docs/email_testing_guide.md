# Email Integration Testing Guide

## Overview
Testing email integrations is complex because they involve external services (Gmail API, Pub/Sub), OAuth flows, and asynchronous processing. This guide covers strategies for testing at different levels.

## Testing Levels

### 1. Unit Tests (What We Have)
Located in `backend/tests/`:
- `test_gmail_webhook_trigger.py` - Tests webhook → trigger firing
- `test_gmail_webhook_history_progress.py` - Tests history tracking and deduplication  
- `test_pubsub_webhook.py` - Tests Pub/Sub endpoint and OIDC validation
- `test_connectors_api.py` - Tests connector CRUD operations

These use mocks and stubs to avoid real API calls.

### 2. Integration Tests (Local Development)

#### A. Using Gmail API Test Mode
```python
# In your .env.local
GMAIL_TEST_MODE=1
GMAIL_TEST_EMAIL=your-test@gmail.com
```

This enables test endpoints that simulate Gmail behavior without real emails.

#### B. Using a Test Gmail Account
1. Create a dedicated Gmail account for testing
2. Enable Gmail API in Google Cloud Console
3. Set up OAuth 2.0 credentials
4. Configure test environment:
```bash
# .env.test
GOOGLE_CLIENT_ID=your-client-id
GOOGLE_CLIENT_SECRET=your-client-secret
APP_PUBLIC_URL=https://your-ngrok-url.ngrok.io
```

### 3. End-to-End Testing Strategies

#### Option 1: Gmail + Ngrok (Recommended for Development)
```bash
# 1. Start ngrok tunnel
ngrok http 8000

# 2. Update APP_PUBLIC_URL with ngrok URL
export APP_PUBLIC_URL=https://abc123.ngrok.io

# 3. Connect Gmail via OAuth flow
# 4. Send test emails to your Gmail account
# 5. Verify webhooks are received
```

**Pros:** Real Gmail API, actual push notifications
**Cons:** Requires internet, ngrok URL changes

#### Option 2: Gmail + Cloud Pub/Sub (Production-like)
```bash
# 1. Set up Google Cloud project
gcloud pubsub topics create gmail-push
gcloud pubsub subscriptions create gmail-push-sub \
  --topic=gmail-push \
  --push-endpoint=https://your-app.com/api/email/webhook/google/pubsub

# 2. Configure Gmail to push to Pub/Sub topic
# 3. Deploy to a public server (your VPS)
```

**Pros:** Exactly like production
**Cons:** Requires GCP setup, public server

#### Option 3: Local IMAP Server (Alternative)
```bash
# 1. Install Dovecot on your VPS
sudo apt install dovecot-imapd dovecot-pop3d

# 2. Configure test mailboxes
sudo useradd -m testuser1
echo "testuser1:password" | sudo chpasswd

# 3. Point your app to IMAP server
IMAP_HOST=your-vps.com
IMAP_PORT=993
IMAP_USER=testuser1
IMAP_PASS=password
```

**Pros:** Full control, no API limits
**Cons:** Doesn't test Gmail-specific features

### 4. Automated E2E Test Suite

Create `backend/tests/e2e/test_email_flow.py`:

```python
import asyncio
import os
from datetime import datetime

import pytest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

@pytest.mark.e2e
@pytest.mark.skipif(not os.getenv("RUN_E2E_TESTS"), reason="E2E tests disabled")
async def test_full_email_flow():
    """Test complete email flow from connection to trigger firing."""
    
    # 1. Connect Gmail account
    response = await client.post(
        "/api/auth/google/gmail",
        json={"auth_code": os.getenv("TEST_AUTH_CODE")}
    )
    connector_id = response.json()["connector_id"]
    
    # 2. Create agent and trigger
    agent = await create_test_agent()
    trigger = await create_email_trigger(agent.id, connector_id)
    
    # 3. Send test email via Gmail API
    service = build('gmail', 'v1', credentials=test_credentials)
    message = create_test_email(
        to="me",
        subject=f"Test Email {datetime.now()}",
        body="This should trigger the agent"
    )
    service.users().messages().send(userId='me', body=message).execute()
    
    # 4. Wait for webhook (with timeout)
    triggered = await wait_for_trigger_fire(trigger.id, timeout=30)
    assert triggered, "Trigger did not fire within timeout"
    
    # 5. Verify agent execution
    run = await get_latest_run(agent.id)
    assert run.status == "completed"
    assert "Test Email" in run.context
```

### 5. Testing Checklist

#### For Each Email Provider (Gmail, Outlook, etc):
- [ ] OAuth connection flow
- [ ] Webhook registration 
- [ ] Message filtering (labels, folders)
- [ ] History tracking
- [ ] Deduplication
- [ ] Watch renewal
- [ ] Error handling
- [ ] Rate limiting

#### Security Testing:
- [ ] OIDC token validation (Pub/Sub)
- [ ] Webhook signature verification
- [ ] Token encryption/decryption
- [ ] Permission scoping

#### Performance Testing:
```python
# Load test with multiple webhooks
async def test_webhook_load():
    tasks = []
    for i in range(100):
        task = send_webhook_async(message_id=f"msg-{i}")
        tasks.append(task)
    
    results = await asyncio.gather(*tasks)
    assert all(r.status_code == 202 for r in results)
```

## Local Development Setup

### Quick Start with Test Email
```bash
# 1. Create test Gmail account
# 2. Enable Gmail API in Google Cloud Console
# 3. Download credentials.json
# 4. Run OAuth flow
python scripts/gmail_oauth_test.py

# 5. Start local server with ngrok
ngrok http 8000 &
uvicorn zerg.main:app --reload

# 6. Connect Gmail in UI
# 7. Send test email
python scripts/send_test_email.py
```

### Test Email Script
```python
# scripts/send_test_email.py
import pickle
from googleapiclient.discovery import build
from email.mime.text import MIMEText
import base64

def send_test_email():
    # Load credentials from OAuth flow
    with open('token.pickle', 'rb') as token:
        creds = pickle.load(token)
    
    service = build('gmail', 'v1', credentials=creds)
    
    message = MIMEText('Test email body')
    message['to'] = 'me'
    message['subject'] = 'Test Trigger Email'
    
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    
    result = service.users().messages().send(
        userId='me',
        body={'raw': raw}
    ).execute()
    
    print(f"Sent message ID: {result['id']}")

if __name__ == '__main__':
    send_test_email()
```

## Debugging Tips

### 1. Check Webhook Receipt
```bash
# Watch logs for webhook calls
tail -f logs/app.log | grep webhook

# Check connector state
curl localhost:8000/api/connectors
```

### 2. Verify Gmail Watch
```python
# Check watch status
from zerg.services import gmail_api

watch_info = gmail_api.get_watch_status(access_token)
print(f"Expires: {watch_info['expiration']}")
```

### 3. Test Pub/Sub Locally
```bash
# Use Pub/Sub emulator
gcloud beta emulators pubsub start
export PUBSUB_EMULATOR_HOST=localhost:8085

# Create test topic/subscription
python scripts/setup_pubsub_emulator.py
```

### 4. Monitor Metrics
```bash
# Check Prometheus metrics
curl localhost:8000/metrics | grep gmail

# Key metrics:
# - gmail_connector_history_id
# - gmail_webhook_error_total
# - pubsub_webhook_processing_total
```

## CI/CD Integration

### GitHub Actions
```yaml
name: Email Integration Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    
    services:
      postgres:
        image: postgres:14
        env:
          POSTGRES_PASSWORD: test
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
    
    steps:
    - uses: actions/checkout@v3
    
    - name: Set up Python
      uses: actions/setup-python@v4
      with:
        python-version: '3.11'
    
    - name: Install dependencies
      run: |
        pip install uv
        uv pip install -r requirements.txt
    
    - name: Run unit tests
      run: |
        pytest tests/test_gmail*.py tests/test_pubsub*.py
    
    - name: Run integration tests (mocked)
      env:
        GMAIL_TEST_MODE: 1
      run: |
        pytest tests/integration/
```

## Troubleshooting

### Common Issues

1. **"No connector found for email"**
   - Check emailAddress is stored in connector config
   - Verify Pub/Sub message contains correct email

2. **"OIDC validation failed"**
   - Check PUBSUB_AUDIENCE setting
   - Verify service account email

3. **"Watch expired"**
   - Check watch_renewal_service is running
   - Manually renew: `curl -X POST /api/admin/renew-watches`

4. **Duplicate triggers firing**
   - Check deduplication (last_msg_no in connector)
   - Verify history_id is updating

5. **No webhooks received**
   - Check ngrok is running and URL is correct
   - Verify Gmail watch is active
   - Check firewall rules

## Production Deployment

### Pre-deployment Checklist
- [ ] Set APP_PUBLIC_URL to production domain
- [ ] Configure Pub/Sub topic and subscription
- [ ] Set up OIDC authentication
- [ ] Enable watch renewal service
- [ ] Configure monitoring alerts
- [ ] Test with production Gmail account
- [ ] Verify SSL certificates
- [ ] Set up log aggregation

### Monitoring
```python
# Add custom alerts
if gmail_webhook_error_total > 10:
    send_alert("High Gmail webhook error rate")

if gmail_connector_watch_expiry < now + 3600:
    send_alert("Gmail watch expiring soon")
```