# Jira Connector for Zerg Agents

## Overview

The Jira connector enables Zerg agents to interact with Jira Cloud projects through the REST API v3. Agents can create issues, search for issues, add comments, transition workflows, and update issue fields.

## Implementation Details

- **Location**: `/apps/zerg/backend/zerg/tools/builtin/jira_tools.py`
- **API Version**: Jira Cloud REST API v3
- **Authentication**: Basic Auth with email + API token
- **Text Format**: Atlassian Document Format (ADF) for descriptions and comments

## Configuration Requirements

Agents using the Jira connector need the following configuration parameters:

| Parameter | Required | Description | Example |
|-----------|----------|-------------|---------|
| `domain` | Yes | Jira Cloud domain | `yourcompany.atlassian.net` |
| `email` | Yes | User email for authentication | `agent@yourcompany.com` |
| `api_token` | Yes | API token from Atlassian account | `abc123xyz...` |

### Generating API Tokens

1. Navigate to: https://id.atlassian.com/manage-profile/security/api-tokens
2. Click "Create API token"
3. Give it a descriptive name (e.g., "Zerg Agent")
4. Copy the token (it will only be shown once)
5. Store securely in the agent's connector configuration

## Available Tools

### 1. jira_create_issue

Create a new issue in a Jira project.

**Parameters:**
- `domain` (required): Jira Cloud domain
- `email` (required): User email for authentication
- `api_token` (required): API token
- `project_key` (required): Project key (e.g., "PROJ", "TEAM")
- `issue_type` (required): Issue type name (e.g., "Task", "Bug", "Story")
- `summary` (required): Issue title/summary
- `description` (optional): Issue description in plain text
- `priority` (optional): Priority name (e.g., "High", "Medium", "Low")
- `labels` (optional): List of label strings
- `assignee` (optional): Assignee account ID (NOT email or display name)

**Returns:**
```json
{
  "success": true,
  "issue_key": "PROJ-123",
  "issue_id": "10001",
  "url": "https://company.atlassian.net/browse/PROJ-123"
}
```

**Example Usage:**
```python
result = jira_create_issue(
    domain="company.atlassian.net",
    email="user@company.com",
    api_token="abc123",
    project_key="PROJ",
    issue_type="Task",
    summary="Implement new feature",
    description="Feature description here",
    priority="High",
    labels=["automation", "agent"]
)
```

---

### 2. jira_list_issues

Search for issues in a Jira project using JQL (Jira Query Language).

**Parameters:**
- `domain` (required): Jira Cloud domain
- `email` (required): User email for authentication
- `api_token` (required): API token
- `project_key` (required): Project key to search within
- `jql` (optional): Custom JQL query (defaults to all project issues)
- `max_results` (optional): Maximum results to return (default: 50, max: 100)

**Returns:**
```json
{
  "success": true,
  "total": 15,
  "returned": 15,
  "issues": [
    {
      "key": "PROJ-123",
      "id": "10001",
      "summary": "Issue title",
      "status": "In Progress",
      "issue_type": "Task",
      "priority": "High",
      "assignee": "John Doe",
      "created": "2025-11-29T10:00:00Z",
      "updated": "2025-11-29T12:00:00Z"
    }
  ]
}
```

**Example JQL Queries:**
```python
# All issues in project
jql = "project = PROJ ORDER BY created DESC"

# Open issues assigned to current user
jql = "project = PROJ AND status != Done AND assignee = currentUser()"

# High priority bugs
jql = "project = PROJ AND issuetype = Bug AND priority = High"

# Issues updated in last 7 days
jql = "project = PROJ AND updated >= -7d"
```

---

### 3. jira_get_issue

Get detailed information about a specific Jira issue.

**Parameters:**
- `domain` (required): Jira Cloud domain
- `email` (required): User email for authentication
- `api_token` (required): API token
- `issue_key` (required): Issue key (e.g., "PROJ-123")

**Returns:**
```json
{
  "success": true,
  "issue": {
    "key": "PROJ-123",
    "id": "10001",
    "summary": "Issue title",
    "description": "Full description text",
    "status": "In Progress",
    "issue_type": "Task",
    "priority": "High",
    "assignee": "John Doe",
    "reporter": "Jane Smith",
    "created": "2025-11-29T10:00:00Z",
    "updated": "2025-11-29T12:00:00Z",
    "labels": ["automation", "agent"]
  }
}
```

---

### 4. jira_add_comment

Add a comment to an existing Jira issue.

**Parameters:**
- `domain` (required): Jira Cloud domain
- `email` (required): User email for authentication
- `api_token` (required): API token
- `issue_key` (required): Issue key (e.g., "PROJ-123")
- `body` (required): Comment text in plain text

**Returns:**
```json
{
  "success": true,
  "comment_id": "10050"
}
```

**Example Usage:**
```python
result = jira_add_comment(
    domain="company.atlassian.net",
    email="user@company.com",
    api_token="abc123",
    issue_key="PROJ-123",
    body="Agent completed the task successfully. All tests passed."
)
```

---

### 5. jira_transition_issue

Transition an issue to a new status (e.g., "To Do" → "In Progress" → "Done").

**Parameters:**
- `domain` (required): Jira Cloud domain
- `email` (required): User email for authentication
- `api_token` (required): API token
- `issue_key` (required): Issue key (e.g., "PROJ-123")
- `transition_id` (required): Transition ID as string

**Returns:**
```json
{
  "success": true
}
```

**Finding Transition IDs:**

Transition IDs are workflow-specific and vary by project. To find available transitions:

1. **Via API**: Query `/rest/api/3/issue/{issueKey}/transitions`
2. **Common IDs** (may vary):
   - `"11"`: To Do → In Progress
   - `"21"`: In Progress → Done
   - `"31"`: In Progress → To Do

**Example Usage:**
```python
# Move issue from "In Progress" to "Done"
result = jira_transition_issue(
    domain="company.atlassian.net",
    email="user@company.com",
    api_token="abc123",
    issue_key="PROJ-123",
    transition_id="21"
)
```

---

### 6. jira_update_issue

Update fields on an existing Jira issue.

**Parameters:**
- `domain` (required): Jira Cloud domain
- `email` (required): User email for authentication
- `api_token` (required): API token
- `issue_key` (required): Issue key (e.g., "PROJ-123")
- `fields` (required): Dictionary of field updates

**Returns:**
```json
{
  "success": true
}
```

**Example Usage:**
```python
# Update summary and priority
result = jira_update_issue(
    domain="company.atlassian.net",
    email="user@company.com",
    api_token="abc123",
    issue_key="PROJ-123",
    fields={
        "summary": "Updated issue title",
        "priority": {"name": "Low"},
        "labels": ["updated", "agent"]
    }
)
```

---

## Error Handling

All tools return a consistent error structure:

```json
{
  "success": false,
  "status_code": 401,
  "error": "Authentication failed - check email and API token",
  "details": "Detailed error message from Jira API"
}
```

### Common Error Codes

| Code | Error | Meaning |
|------|-------|---------|
| 401 | Authentication failed | Invalid email or API token |
| 403 | Permission denied | User lacks required permissions |
| 404 | Resource not found | Invalid project key, issue key, or field name |
| 429 | Rate limit exceeded | Too many requests, includes `rate_limit_retry_after` |
| 500+ | Server error | Jira service issue, retry later |

### Rate Limiting

Jira Cloud enforces per-tenant burst rate limits:

- **Detection**: Tools return HTTP 429 with `rate_limit_retry_after` field
- **Response**: Agent should implement exponential backoff
- **Headers**: Check `Retry-After` header when available
- **Best Practice**: Batch operations and avoid tight loops

**Example Rate Limit Response:**
```json
{
  "success": false,
  "status_code": 429,
  "error": "Rate limit exceeded - retry after 60 seconds",
  "rate_limit_retry_after": "60",
  "details": "Rate limit message from Jira"
}
```

---

## Security Best Practices

1. **API Token Storage**
   - Store API tokens in secure agent configuration (encrypted)
   - Never log or expose API tokens in error messages
   - Rotate tokens periodically

2. **User Permissions**
   - Create dedicated Jira user accounts for agents
   - Grant minimum required permissions
   - Use project-specific permissions where possible

3. **Authentication**
   - API tokens are preferred over OAuth for automation
   - Each agent should have its own token for audit trails
   - Revoke tokens immediately if compromised

---

## Validation Script

A validation script is available at `/scripts/validate_jira_connector.py` to test the implementation logic without requiring real credentials:

```bash
python scripts/validate_jira_connector.py
```

This script validates:
- Authentication header format
- Payload structures for all operations
- URL construction
- Error handling patterns
- JQL query formation

---

## Integration Example

**Agent Configuration (JSON):**
```json
{
  "agent_id": "project-manager-agent",
  "tools": ["jira_create_issue", "jira_list_issues", "jira_add_comment"],
  "connectors": {
    "jira": {
      "domain": "mycompany.atlassian.net",
      "email": "agent@mycompany.com",
      "api_token": "${JIRA_API_TOKEN}"
    }
  }
}
```

**Agent Workflow Example:**
```python
# 1. Search for open bugs
issues = jira_list_issues(
    domain=config.jira.domain,
    email=config.jira.email,
    api_token=config.jira.api_token,
    project_key="PROJ",
    jql="issuetype = Bug AND status != Done",
    max_results=10
)

# 2. Create a new task
new_issue = jira_create_issue(
    domain=config.jira.domain,
    email=config.jira.email,
    api_token=config.jira.api_token,
    project_key="PROJ",
    issue_type="Task",
    summary="Weekly bug triage",
    description=f"Found {issues['total']} open bugs requiring triage"
)

# 3. Add progress comment
jira_add_comment(
    domain=config.jira.domain,
    email=config.jira.email,
    api_token=config.jira.api_token,
    issue_key=new_issue['issue_key'],
    body="Agent is processing the bug list now"
)
```

---

## API Reference

For complete Jira Cloud REST API v3 documentation:
- **Base URL**: `https://{domain}/rest/api/3/`
- **API Docs**: https://developer.atlassian.com/cloud/jira/platform/rest/v3/
- **ADF Format**: https://developer.atlassian.com/cloud/jira/platform/apis/document/structure/
- **JQL Reference**: https://support.atlassian.com/jira-software-cloud/docs/use-advanced-search-with-jira-query-language-jql/

---

## Troubleshooting

### "Authentication failed" Error
- Verify email is correct
- Ensure API token is valid and not expired
- Check that domain includes `.atlassian.net`

### "Permission denied" Error
- Verify user has permission to perform action
- Check project permissions in Jira settings
- Ensure user is part of the project

### "Resource not found" Error
- Verify project key is correct and exists
- Check issue key format (PROJ-123)
- Ensure field names match Jira configuration

### Transition Not Working
- Transition IDs are workflow-specific
- Query `/issue/{issueKey}/transitions` to find valid IDs
- Ensure issue's current status allows the transition

---

## Future Enhancements

Potential additions to the connector:

1. **Advanced Features**
   - Support for custom fields
   - Attachment upload/download
   - Issue linking
   - Sprint management (for Scrum projects)

2. **Batch Operations**
   - Bulk create issues
   - Bulk update issues
   - Bulk transition issues

3. **Webhooks**
   - Receive real-time notifications
   - React to issue changes

4. **Advanced Queries**
   - Saved filter execution
   - Dashboard data retrieval
   - Project analytics

---

## Support

For issues or questions about the Jira connector:
1. Check this documentation
2. Review the validation script output
3. Consult Jira Cloud REST API documentation
4. Check agent logs for detailed error messages
