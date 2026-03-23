---
name: slack
description: "Send messages to Slack channels via incoming webhook."
emoji: "💬"
---

# Slack Skill

Send messages to Slack channels via incoming webhooks.

## Available Tools

- `send_slack_webhook` - Send a message to a Slack channel via webhook

## Configuration

Requires a Slack Incoming Webhook URL. Create one at https://api.slack.com/messaging/webhooks

Configure the webhook URL in Settings > Connectors > Slack, or provide it directly via the `webhook_url` parameter.

## Example

```python
send_slack_webhook(
    text="Hello from Longhouse!"
)
```

## Rich Formatting

Supports Slack Block Kit for rich messages:

```python
send_slack_webhook(
    text="Deployment completed",
    blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "*Status:* Success"}}]
)
```
