# Implementation Roadmap: Connector Tools

> Status tracking for agent connector implementations

---

## ✅ Completed (Sprint 1 - Nov 2024)

### Output/Notification Tools

| Tool                | Status  | Notes                                       |
| ------------------- | ------- | ------------------------------------------- |
| **Slack Webhook**   | ✅ Done | `send_slack_webhook` - messages + Block Kit |
| **Discord Webhook** | ✅ Done | `send_discord_webhook` - messages + embeds  |
| **Email (Resend)**  | ✅ Done | `send_email` - text/HTML via Resend API     |
| **SMS (Twilio)**    | ✅ Done | `send_sms` - SMS via Twilio API             |

### Project Management Connectors

| Connector  | Status  | Tools Included                                                                                                                                |
| ---------- | ------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| **GitHub** | ✅ Done | `github_create_issue`, `github_list_issues`, `github_get_issue`, `github_add_comment`, `github_list_pull_requests`, `github_get_pull_request` |
| **Jira**   | ✅ Done | `jira_create_issue`, `jira_list_issues`, `jira_get_issue`, `jira_add_comment`, `jira_transition_issue`, `jira_update_issue`                   |
| **Linear** | ✅ Done | `linear_create_issue`, `linear_list_issues`, `linear_get_issue`, `linear_update_issue`, `linear_add_comment`, `linear_list_teams`             |
| **Notion** | ✅ Done | `notion_create_page`, `notion_get_page`, `notion_update_page`, `notion_search`, `notion_query_database`, `notion_append_blocks`               |

### Test Coverage

- **104 unit tests** with mocked HTTP (all passing)
- **Integration test infrastructure** with strict credential requirements
- Credentials stored in `.env.test` (gitignored)

---

## 🔜 Next Up (Prioritized)

### Priority 1: State/Memory Tools

| Feature          | Effort | Description                      |
| ---------------- | ------ | -------------------------------- |
| Key-Value Store  | 1 day  | `agent_state` table + CRUD tools |
| State TTL/Expiry | 2 hrs  | Add `expires_at` column          |

```python
# Tools to add
kv_get(key: str) -> Any
kv_set(key: str, value: Any, ttl_seconds: int = None) -> bool
kv_delete(key: str) -> bool
kv_list(prefix: str = None) -> List[str]
```

### Priority 2: File Operations

| Feature      | Effort | Description              |
| ------------ | ------ | ------------------------ |
| Write File   | 4 hrs  | Write to agent workspace |
| Read File    | 2 hrs  | Read from workspace      |
| Generate CSV | 2 hrs  | Python csv module        |
| Generate PDF | 3 days | WeasyPrint or Playwright |

### Priority 3: Enhanced Workflows

| Feature           | Effort | Description                       |
| ----------------- | ------ | --------------------------------- |
| Scheduled Actions | 4 days | Queue future tool executions      |
| Multi-Agent Calls | 3 days | Tool to invoke another agent      |
| Human Approval    | 7 days | Approval nodes with notifications |

### Priority 4: OAuth Connectors

| Feature         | Effort | Description                |
| --------------- | ------ | -------------------------- |
| Google OAuth    | 5 days | OAuth2 flow, token refresh |
| Google Calendar | 3 days | Calendar read/write        |
| Google Sheets   | 3 days | Sheets read/write          |
| Slack App OAuth | 5 days | Full Slack bot integration |

---

## Tool Inventory

### ✅ Implemented (32 tools)

```
Notifications:
  send_slack_webhook     - Slack messages via webhook
  send_discord_webhook   - Discord messages + embeds
  send_email             - Email via Resend API
  send_sms               - SMS via Twilio

GitHub (6 tools):
  github_create_issue, github_list_issues, github_get_issue,
  github_add_comment, github_list_pull_requests, github_get_pull_request

Jira (6 tools):
  jira_create_issue, jira_list_issues, jira_get_issue,
  jira_add_comment, jira_transition_issue, jira_update_issue

Linear (6 tools):
  linear_create_issue, linear_list_issues, linear_get_issue,
  linear_update_issue, linear_add_comment, linear_list_teams

Notion (6 tools):
  notion_create_page, notion_get_page, notion_update_page,
  notion_search, notion_query_database, notion_append_blocks
```

### 🔜 Planned

```
State:      kv_get, kv_set, kv_delete, kv_list
Files:      write_file, read_file, list_files, generate_csv, generate_pdf
Cloud:      upload_s3, upload_gcs
Workflow:   schedule_action, invoke_agent, request_approval
OAuth:      google_calendar_*, google_sheets_*, slack_*
```

---

## File Locations

```
apps/zerg/backend/zerg/tools/
├── builtin/
│   ├── __init__.py          # Tool registry
│   ├── discord_tools.py     # ✅
│   ├── slack_tools.py       # ✅
│   ├── email_tools.py       # ✅
│   ├── sms_tools.py         # ✅
│   ├── github_tools.py      # ✅
│   ├── jira_tools.py        # ✅
│   ├── linear_tools.py      # ✅
│   └── notion_tools.py      # ✅
└── tests/
    └── test_*_tools.py      # 104 unit tests
```

---

## Credential Setup

Copy `.env.test.example` to `.env.test` and fill in:

```bash
# Notifications
TEST_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
TEST_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
TEST_RESEND_API_KEY=re_...
TEST_TWILIO_ACCOUNT_SID=AC...
TEST_TWILIO_AUTH_TOKEN=...
TEST_TWILIO_FROM_NUMBER=+1...
TEST_TWILIO_TO_NUMBER=+1...

# Project Management
TEST_GITHUB_TOKEN=ghp_...
TEST_GITHUB_REPO=owner/repo
TEST_JIRA_DOMAIN=company.atlassian.net
TEST_JIRA_EMAIL=...
TEST_JIRA_API_TOKEN=...
TEST_JIRA_PROJECT_KEY=...
TEST_LINEAR_API_KEY=lin_api_...
TEST_NOTION_API_KEY=secret_...
TEST_NOTION_PAGE_ID=...
```

Run integration tests: `pytest tests/integration/ -v`

---

_Last updated: November 2024_
