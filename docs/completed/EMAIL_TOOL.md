# ✅ COMPLETED / HISTORICAL REFERENCE ONLY

> **Note:** This feature has been implemented. Implementation details may have evolved since this document was written.
> For current documentation, see the root `docs/` directory.

---

# Email Tool Documentation

## Overview

The `send_email` tool allows Zerg agents to send emails via the [Resend API](https://resend.com). Resend provides a modern, developer-friendly email API with excellent deliverability and a generous free tier.

## Features

- Send plain text and HTML emails
- Support for CC and BCC recipients
- Custom reply-to addresses
- File attachments (via URL or base64 content)
- Comprehensive error handling
- Email validation
- Rate limit handling

## Prerequisites

### 1. Create a Resend Account

1. Go to [resend.com](https://resend.com) and sign up
2. Navigate to the API Keys section
3. Create a new API key (starts with `re_`)

### 2. Verify Your Domain

**Important:** Resend requires domain verification before you can send emails from your domain.

1. In the Resend dashboard, go to Domains
2. Add your domain (e.g., `mydomain.com`)
3. Add the provided DNS records (SPF, DKIM, DMARC)
4. Wait for verification (usually takes a few minutes)

For development/testing, you can use Resend's test email addresses:

- From: `onboarding@resend.dev`
- To: `delivered@resend.dev`

## Configuration

The email tool requires an API key to be configured in the agent's connector configuration. Here's how to set it up:

### Option 1: Agent Connector Configuration (Recommended)

When creating or editing an agent, add a connector with the email tool:

```json
{
  "tool_name": "send_email",
  "config": {
    "api_key": "re_your_api_key_here"
  }
}
```

### Option 2: Dynamic API Key (Advanced)

Agents can pass the API key at runtime if they have access to it through other means:

```python
# Agent retrieves API key from secure storage or config
api_key = get_secret("resend_api_key")

# Use in tool invocation
result = send_email(
    api_key=api_key,
    from_email="noreply@mydomain.com",
    to="user@example.com",
    subject="Welcome!",
    html="<h1>Welcome!</h1>"
)
```

## Usage Examples

### Basic Text Email

```python
result = send_email(
    api_key="re_your_api_key",
    from_email="noreply@mydomain.com",
    to="user@example.com",
    subject="Hello from Zerg!",
    text="This is a plain text email."
)

if result["success"]:
    print(f"Email sent! Message ID: {result['message_id']}")
else:
    print(f"Failed to send: {result['error']}")
```

### HTML Email

```python
result = send_email(
    api_key="re_your_api_key",
    from_email="noreply@mydomain.com",
    to="user@example.com",
    subject="HTML Email Example",
    html="""
        <h1>Welcome to Our Service!</h1>
        <p>Thank you for signing up.</p>
        <p><a href="https://example.com">Get Started</a></p>
    """
)
```

### Multiple Recipients with CC/BCC

```python
result = send_email(
    api_key="re_your_api_key",
    from_email="noreply@mydomain.com",
    to=["user1@example.com", "user2@example.com"],
    cc="manager@mydomain.com",
    bcc=["archive@mydomain.com", "backup@mydomain.com"],
    subject="Team Announcement",
    text="Important team update..."
)
```

### With Reply-To Address

```python
result = send_email(
    api_key="re_your_api_key",
    from_email="noreply@mydomain.com",
    to="customer@example.com",
    reply_to="support@mydomain.com",
    subject="Support Ticket #12345",
    text="Your support ticket has been created. Reply to this email to add comments."
)
```

### With Attachments

```python
result = send_email(
    api_key="re_your_api_key",
    from_email="noreply@mydomain.com",
    to="user@example.com",
    subject="Your Report",
    text="Please find your monthly report attached.",
    attachments=[
        {
            "filename": "report.pdf",
            "path": "https://example.com/reports/2024-01.pdf"
        },
        {
            "filename": "data.csv",
            "content": "name,value\nfoo,123\nbar,456"
        }
    ]
)
```

### Both Text and HTML (Best Practice)

```python
result = send_email(
    api_key="re_your_api_key",
    from_email="noreply@mydomain.com",
    to="user@example.com",
    subject="Welcome!",
    text="Welcome! Visit: https://example.com",  # Fallback for plain text clients
    html="<h1>Welcome!</h1><p><a href='https://example.com'>Get Started</a></p>"
)
```

## Error Handling

The tool returns a dictionary with the following structure:

```python
# Success response
{
    "success": True,
    "message_id": "abc123..."
}

# Error response
{
    "success": False,
    "error": "Error description"
}
```

### Common Errors

| Error                  | Cause                                              | Solution                                   |
| ---------------------- | -------------------------------------------------- | ------------------------------------------ |
| Invalid API key format | API key doesn't start with `re_`                   | Check your API key from Resend dashboard   |
| Invalid API key        | API key is incorrect or revoked                    | Generate a new API key                     |
| Access forbidden       | Domain not verified                                | Verify your domain in Resend               |
| Validation error       | Invalid email addresses or missing required fields | Check email format and required parameters |
| Rate limit exceeded    | Too many requests                                  | Slow down or upgrade plan                  |

## Rate Limits and Pricing

### Free Tier

- 100 emails per day
- 3,000 emails per month
- 2 API requests per second

### Pro Plan ($20/month)

- 50,000 emails per month
- No daily limit
- 2+ requests per second

### Important Limits

- Must maintain < 4% bounce rate
- Must maintain < 0.08% spam rate
- Exceeding these may pause sending temporarily

## Best Practices

### 1. Domain Verification

Always verify your domain before sending production emails. Using unverified domains will result in errors.

### 2. Email Content

- Always provide both `text` and `html` versions when possible
- Use semantic HTML for better email client compatibility
- Test emails in multiple clients (Gmail, Outlook, etc.)

### 3. From Address

- Use a valid email from your verified domain
- Use descriptive display names: `"Team Name <noreply@mydomain.com>"`
- Avoid using `noreply@` if you expect responses (use `reply_to` instead)

### 4. Error Handling

Always check the `success` field and handle errors gracefully:

```python
result = send_email(...)

if not result["success"]:
    logger.error(f"Failed to send email: {result['error']}")
    # Retry logic, fallback notification, etc.
```

### 5. Rate Limiting

Implement backoff strategies if sending many emails:

```python
import time

for email in email_list:
    result = send_email(...)
    if not result["success"] and "rate limit" in result["error"].lower():
        time.sleep(1)  # Wait and retry
```

## Testing

### Validation Script (Mock Mode)

~~Test your email logic without making API calls:~~

**Note**: Validation scripts were never created. Test via backend with real Resend API key.

```bash
# Scripts no longer exist
# python scripts/validate_email_resend.py
# cd apps/zerg/backend && uv run python ../../../scripts/test_email_tool.py
```

### Integration Test

~~Test the tool registration and invocation~~

### Send Test Email

Use the Resend test addresses for end-to-end testing:

```python
result = send_email(
    api_key="re_your_real_api_key",
    from_email="onboarding@resend.dev",  # Test sender
    to="delivered@resend.dev",           # Test recipient
    subject="Test Email",
    text="This is a test."
)
```

## Security Considerations

### API Key Storage

- Never commit API keys to version control
- Store API keys securely (environment variables, secrets manager)
- Use separate API keys for development and production
- Rotate keys periodically

### Email Validation

The tool validates email addresses, but always sanitize user input:

- Validate email format before passing to tool
- Sanitize subject lines and content
- Prevent email injection attacks

### Content Security

- Escape HTML content to prevent XSS
- Validate attachment URLs
- Limit attachment sizes
- Scan attachments for malware if accepting user uploads

## Troubleshooting

### "Domain not verified" Error

**Problem:** Trying to send from unverified domain

**Solution:**

1. Go to Resend dashboard > Domains
2. Add your domain
3. Add DNS records to your domain provider
4. Wait for verification

### "Rate limit exceeded" Error

**Problem:** Sending too many emails too quickly

**Solution:**

- Implement exponential backoff
- Batch email sends
- Upgrade to Pro plan for higher limits

### "Invalid email address" Error

**Problem:** Email format validation failed

**Solution:**

- Check email format is valid
- Remove extra spaces
- Ensure domain has valid TLD

## Additional Resources

- [Resend Documentation](https://resend.com/docs)
- [Resend API Reference](https://resend.com/docs/api-reference/emails/send-email)
- [Resend Email Best Practices](https://resend.com/docs/knowledge-base/email-best-practices)

## Alternative: SendGrid

While this tool is implemented for Resend, SendGrid is also a popular option:

**Resend vs SendGrid:**

- Resend: Simpler API, modern design, better DX, free tier suitable for most use cases
- SendGrid: More enterprise features, larger scale, more complex API, established platform

If you need SendGrid instead, the implementation would be similar but using:

- Endpoint: `https://api.sendgrid.com/v3/mail/send`
- Different payload structure (see SendGrid docs)
- Bearer token authentication

## Support

For issues with the email tool:

1. Check this documentation
2. Review error messages (they're descriptive)
3. Test with validation scripts
4. Check Resend dashboard for delivery status

For Resend service issues:

- Resend Status: https://status.resend.com
- Resend Support: support@resend.com
