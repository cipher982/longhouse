---
name: slack
description: "Send messages to Slack channels and users."
emoji: "💬"
primary_env: "SLACK_BOT_TOKEN"
requires:
  env:
    - SLACK_BOT_TOKEN
---

# Slack Skill

Send messages to Slack workspaces using the Slack API.

## Available Tools

- `slack_send_message` - Send a message to a channel or user

## Configuration

Requires a Slack Bot Token with the following scopes:

- `chat:write` - Send messages
- `channels:read` - List channels (optional)

Set `SLACK_BOT_TOKEN` in environment or configure in Fiche Settings.

## Example

```python
slack_send_message(
    channel="#general",
    message="Hello from Zerg! :robot_face:"
)
```

## Channel Formats

- `#channel-name` - Public channel
- `@username` - Direct message
- Channel ID (C01234567) - Any channel by ID
