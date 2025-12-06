# SMS Tool Integration (Twilio)

## Overview

The Zerg platform now includes SMS messaging capability via Twilio's Programmable Messaging API. Agents can send SMS messages to any phone number using the `send_sms` tool.

## Implementation Location

- **Tool Implementation**: `/apps/zerg/backend/zerg/tools/builtin/sms_tools.py`
- **Validation Script**: `/scripts/validate_twilio_sms.py`
- **Registration**: Automatically registered in `/apps/zerg/backend/zerg/tools/builtin/__init__.py`

## Tool: `send_sms`

### Purpose

Send SMS messages via Twilio API to any phone number. Supports delivery status tracking via webhooks.

### Parameters

| Parameter         | Type   | Required | Description                                                         |
| ----------------- | ------ | -------- | ------------------------------------------------------------------- |
| `account_sid`     | string | Yes      | Twilio Account SID (starts with 'AC', 34 characters)                |
| `auth_token`      | string | Yes      | Twilio Auth Token (32 hexadecimal characters)                       |
| `from_number`     | string | Yes      | Sender phone number in E.164 format (must be a Twilio-owned number) |
| `to_number`       | string | Yes      | Recipient phone number in E.164 format                              |
| `message`         | string | Yes      | SMS message body (max 1600 characters)                              |
| `status_callback` | string | No       | Webhook URL to receive delivery status updates                      |

### Phone Number Format (E.164)

All phone numbers must be in E.164 format:

- Start with `+` followed by country code
- No spaces, dashes, or parentheses
- Example: `+14155552671` (US number)
- Example: `+442071838750` (UK number)

### Response Format

**Success Response:**

```json
{
  "success": true,
  "message_sid": "SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "status": "queued",
  "from_number": "+14155552671",
  "to_number": "+14155552672",
  "date_created": "Thu, 29 Nov 2025 12:00:00 +0000",
  "segments": 1,
  "price": "-0.00750",
  "price_unit": "USD"
}
```

**Error Response:**

```json
{
  "success": false,
  "error_code": 21211,
  "error_message": "The 'To' number +1234567890 is not a valid phone number.",
  "status_code": 400,
  "from_number": "+14155552671",
  "to_number": "+1234567890",
  "more_info": "https://www.twilio.com/docs/errors/21211"
}
```

### Message Segmentation

SMS messages are split into segments based on character encoding:

| Encoding        | Single Segment | Multi-Part Segment | Max Total                |
| --------------- | -------------- | ------------------ | ------------------------ |
| GSM-7 (ASCII)   | 160 chars      | 153 chars/segment  | 1600 chars (10 segments) |
| UCS-2 (Unicode) | 70 chars       | 67 chars/segment   | 1600 chars (24 segments) |

**Cost Implication**: Each segment is billed separately by Twilio.

### Rate Limits

- Default: 100 messages per second
- Exceeding rate limits returns HTTP 429 (Too Many Requests)
- For high-volume sending, implement queue systems (Redis, RabbitMQ)

### Common Error Codes

| Code  | Description            | Resolution                                           |
| ----- | ---------------------- | ---------------------------------------------------- |
| 21211 | Invalid phone number   | Verify E.164 format and number validity              |
| 21408 | Permission denied      | Verify Twilio account has SMS capability             |
| 21610 | Unsubscribed recipient | Recipient opted out of SMS                           |
| 21614 | Invalid 'From' number  | Verify 'From' number is owned by your Twilio account |
| 20003 | Authentication failed  | Verify Account SID and Auth Token are correct        |
| 429   | Rate limit exceeded    | Implement backoff/retry logic                        |

Full error reference: https://www.twilio.com/docs/api/errors

## Connector Configuration

To use the SMS tool, agents need Twilio credentials configured via a connector.

### Required Configuration Fields

```json
{
  "type": "sms",
  "provider": "twilio",
  "config": {
    "account_sid": "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "auth_token": "your_32_character_auth_token",
    "from_number": "+14155552671"
  }
}
```

### How to Get Twilio Credentials

1. **Sign up for Twilio**: https://www.twilio.com/try-twilio
2. **Find your credentials**:
   - Log in to Twilio Console: https://console.twilio.com/
   - Navigate to Account → API keys & tokens
   - Copy your Account SID and Auth Token
3. **Get a phone number**:
   - Navigate to Phone Numbers → Manage → Buy a number
   - Choose a number with SMS capability
   - Note: Trial accounts can only send to verified numbers

### Setting Up a Connector (Manual)

Currently, connectors must be created via API or database. Frontend UI for SMS connectors is not yet implemented.

**API Endpoint**: `POST /api/connectors`

```bash
curl -X POST https://api.swarmlet.com/api/connectors \
  -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "sms",
    "provider": "twilio",
    "config": {
      "account_sid": "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
      "auth_token": "your_auth_token_here",
      "from_number": "+14155552671"
    }
  }'
```

**Response**:

```json
{
  "id": 1,
  "owner_id": 123,
  "type": "sms",
  "provider": "twilio",
  "config": {
    "account_sid": "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "auth_token": "***REDACTED***",
    "from_number": "+14155552671"
  },
  "created_at": "2025-11-29T12:00:00Z",
  "updated_at": "2025-11-29T12:00:00Z"
}
```

### Using the Connector in Agent Workflows

When agents are configured with SMS connector access, they can call the tool:

```python
# Agent will automatically inject credentials from connector
result = send_sms(
    account_sid=connector.config["account_sid"],
    auth_token=connector.config["auth_token"],
    from_number=connector.config["from_number"],
    to_number="+14155552672",
    message="Hello from Zerg! Your task has been completed.",
)
```

## Status Callbacks (Delivery Tracking)

Twilio can send delivery status updates to a webhook endpoint.

### Status Callback Flow

1. When sending SMS, provide `status_callback` parameter:

   ```python
   result = send_sms(
       # ... other params ...
       status_callback="https://api.swarmlet.com/webhook/sms/delivery/123"
   )
   ```

2. Twilio will POST to this URL with status updates:
   - `queued` - Message accepted by Twilio
   - `sent` - Message sent to carrier
   - `delivered` - Message delivered to recipient
   - `undelivered` - Message failed to deliver
   - `failed` - Message failed (billing or other error)

3. Example webhook payload from Twilio:
   ```
   MessageSid=SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   MessageStatus=delivered
   From=+14155552671
   To=+14155552672
   ```

### Implementing Status Webhooks

To implement delivery tracking:

1. Create a webhook endpoint at `/api/webhooks/sms/delivery/{agent_run_id}`
2. Parse Twilio's form-encoded POST data
3. Update agent run metadata with delivery status
4. Log delivery failures for investigation

**Security Note**: Validate webhook requests are from Twilio by checking:

- Request signature (X-Twilio-Signature header)
- Source IP is from Twilio's IP ranges

## Security Considerations

1. **Credential Storage**: Twilio credentials are encrypted at rest using Fernet encryption (requires `FERNET_SECRET` env var)
2. **Credential Transmission**: Always use HTTPS for API calls
3. **Webhook Security**: Validate Twilio webhook signatures
4. **Rate Limiting**: Implement application-level rate limiting to prevent abuse
5. **Cost Control**: Monitor SMS usage and set spending limits in Twilio console

## Cost Considerations

- **SMS Pricing**: Varies by country (US: ~$0.0075/segment outbound, ~$0.0075/segment inbound)
- **Phone Number**: ~$1.15/month for US number
- **International SMS**: Significantly more expensive than domestic
- **Recommendation**: Set spending alerts in Twilio console

**Cost Calculator**: https://www.twilio.com/en-us/sms/pricing

## Testing

### Validation Script

Run the validation script to test SMS tool logic without sending real messages:

```bash
cd /Users/davidrose/git/zerg
python scripts/validate_twilio_sms.py
```

This validates:

- Phone number formatting
- Message validation
- Credential format checking
- Payload building
- Segment estimation

### Test Accounts

Twilio provides test credentials for development:

- Test Account SID: `ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`
- Test Auth Token: Test tokens work only in test mode
- Test numbers: Use Twilio's magic numbers for testing (e.g., `+15005550006` always succeeds)

**Magic Numbers**: https://www.twilio.com/docs/iam/test-credentials#test-sms-messages

### Integration Testing

To test with real Twilio account:

```python
# In Python REPL or test script
from zerg.tools.builtin.sms_tools import send_sms

result = send_sms(
    account_sid="YOUR_ACCOUNT_SID",
    auth_token="YOUR_AUTH_TOKEN",
    from_number="+YOUR_TWILIO_NUMBER",
    to_number="+YOUR_VERIFIED_NUMBER",
    message="Test from Zerg platform",
)

print(result)
```

## Future Enhancements

1. **Frontend UI**: Add SMS connector creation/management in dashboard
2. **Batch Sending**: Support sending to multiple recipients
3. **Templates**: Pre-defined message templates with variable substitution
4. **Scheduled SMS**: Schedule messages for future delivery
5. **Two-Way SMS**: Receive and process incoming SMS messages
6. **MMS Support**: Send multimedia messages (images, videos)
7. **Other Providers**: Support for Vonage, AWS SNS, etc.

## References

- Twilio Programmable Messaging API: https://www.twilio.com/docs/messaging
- Twilio Python Quickstart: https://www.twilio.com/docs/sms/quickstart/python
- E.164 Phone Number Format: https://en.wikipedia.org/wiki/E.164
- Twilio Error Codes: https://www.twilio.com/docs/api/errors
- Twilio Webhook Security: https://www.twilio.com/docs/usage/security#validating-requests

## Support

For issues with the SMS tool implementation:

1. Check validation script passes: `python scripts/validate_twilio_sms.py`
2. Verify Twilio credentials are correct
3. Check phone numbers are in E.164 format
4. Review Twilio error codes in response
5. Check Twilio console logs: https://console.twilio.com/logs
