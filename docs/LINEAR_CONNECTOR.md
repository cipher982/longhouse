# Linear Connector Documentation

## Overview

The Linear connector enables Zerg agents to interact with Linear's issue tracking system through the Linear GraphQL API. Agents can create, list, retrieve, update issues, add comments, and list teams.

## Configuration

### API Key Setup

1. **Generate API Key**
   - Navigate to: https://linear.app/settings/api
   - Click "Create new API key"
   - Give it a descriptive name (e.g., "Zerg Agent Platform")
   - Copy the generated key (starts with `lin_api_`)

2. **Configure Agent Connector**
   - In the Zerg dashboard, navigate to Agent Configuration
   - Add a Linear connector with the following configuration:

```json
{
  "type": "linear",
  "api_key": "lin_api_xxxxxxxxxxxxxxxxxxxxx"
}
```

## Available Tools

### 1. `linear_create_issue`

Create a new issue in Linear.

**Parameters:**

- `api_key` (string, required): Linear Personal API Key
- `team_id` (string, required): ID of the team to create the issue in
- `title` (string, required): Issue title
- `description` (string, optional): Issue description/body
- `priority` (integer, optional): Priority level (0-4)
  - 0 = None
  - 1 = Urgent
  - 2 = High
  - 3 = Medium
  - 4 = Low
- `state_id` (string, optional): ID of the workflow state
- `assignee_id` (string, optional): ID of the user to assign to
- `label_ids` (list, optional): List of label IDs to apply

**Returns:**

```json
{
  "success": true,
  "data": {
    "id": "abc-123-def-456",
    "identifier": "ENG-42",
    "title": "Bug: Login not working",
    "url": "https://linear.app/team/issue/ENG-42",
    "priority": 2,
    "state": "Todo",
    "team": "ENG",
    "created_at": "2025-11-29T12:00:00Z"
  }
}
```

**Example:**

```python
result = linear_create_issue(
    api_key="lin_api_xxxxx",
    team_id="abc123",
    title="Bug: Login not working",
    description="Users cannot log in with valid credentials",
    priority=2
)
```

---

### 2. `linear_list_issues`

List issues in Linear with optional filtering.

**Parameters:**

- `api_key` (string, required): Linear Personal API Key
- `team_id` (string, optional): Filter by team ID
- `state` (string, optional): Filter by workflow state name (e.g., "In Progress", "Done")
- `first` (integer, optional): Number of results to return (1-250, default: 50)

**Returns:**

```json
{
  "success": true,
  "data": [
    {
      "id": "abc-123-def-456",
      "identifier": "ENG-42",
      "title": "Bug: Login not working",
      "priority": 2,
      "state": "In Progress",
      "assignee": "John Doe",
      "team": "ENG",
      "url": "https://linear.app/team/issue/ENG-42",
      "created_at": "2025-11-29T12:00:00Z",
      "updated_at": "2025-11-29T14:30:00Z"
    }
  ],
  "count": 1
}
```

**Example:**

```python
result = linear_list_issues(
    api_key="lin_api_xxxxx",
    team_id="abc123",
    state="In Progress",
    first=25
)
```

---

### 3. `linear_get_issue`

Get detailed information about a specific Linear issue.

**Parameters:**

- `api_key` (string, required): Linear Personal API Key
- `issue_id` (string, required): Issue ID (UUID, not identifier like "ENG-42")

**Returns:**

```json
{
  "success": true,
  "data": {
    "id": "abc-123-def-456",
    "identifier": "ENG-42",
    "title": "Bug: Login not working",
    "description": "Users cannot log in with valid credentials",
    "priority": 2,
    "state": "In Progress",
    "assignee": "John Doe",
    "team": "ENG",
    "labels": ["bug", "priority-high"],
    "comments": [
      {
        "id": "comment-123",
        "body": "Investigating the issue",
        "author": "Jane Smith",
        "created_at": "2025-11-29T13:00:00Z"
      }
    ],
    "url": "https://linear.app/team/issue/ENG-42",
    "created_at": "2025-11-29T12:00:00Z",
    "updated_at": "2025-11-29T14:30:00Z"
  }
}
```

**Example:**

```python
result = linear_get_issue(
    api_key="lin_api_xxxxx",
    issue_id="abc-123-def-456"
)
```

---

### 4. `linear_update_issue`

Update an existing Linear issue.

**Parameters:**

- `api_key` (string, required): Linear Personal API Key
- `issue_id` (string, required): Issue ID to update (UUID)
- `title` (string, optional): New issue title
- `description` (string, optional): New issue description
- `state_id` (string, optional): New workflow state ID
- `priority` (integer, optional): New priority level (0-4)

**Note:** At least one field to update must be provided.

**Returns:**

```json
{
  "success": true,
  "data": {
    "id": "abc-123-def-456",
    "identifier": "ENG-42",
    "title": "Bug: Login fixed",
    "description": "Updated description",
    "priority": 4,
    "state": "Done",
    "updated_at": "2025-11-29T15:00:00Z"
  }
}
```

**Example:**

```python
result = linear_update_issue(
    api_key="lin_api_xxxxx",
    issue_id="abc-123-def-456",
    title="Bug: Login fixed",
    priority=4
)
```

---

### 5. `linear_add_comment`

Add a comment to a Linear issue.

**Parameters:**

- `api_key` (string, required): Linear Personal API Key
- `issue_id` (string, required): Issue ID to comment on (UUID)
- `body` (string, required): Comment text

**Returns:**

```json
{
  "success": true,
  "data": {
    "id": "comment-456",
    "body": "Thanks for reporting this! We'll look into it.",
    "author": "Agent Bot",
    "issue_identifier": "ENG-42",
    "url": "https://linear.app/team/issue/ENG-42#comment-456",
    "created_at": "2025-11-29T15:30:00Z"
  }
}
```

**Example:**

```python
result = linear_add_comment(
    api_key="lin_api_xxxxx",
    issue_id="abc-123-def-456",
    body="Thanks for reporting this! We'll look into it."
)
```

---

### 6. `linear_list_teams`

List all teams accessible to the API key.

**Parameters:**

- `api_key` (string, required): Linear Personal API Key

**Returns:**

```json
{
  "success": true,
  "data": [
    {
      "id": "team-abc-123",
      "name": "Engineering",
      "key": "ENG",
      "description": "Engineering team"
    },
    {
      "id": "team-def-456",
      "name": "Product",
      "key": "PROD",
      "description": "Product team"
    }
  ],
  "count": 2
}
```

**Example:**

```python
result = linear_list_teams(
    api_key="lin_api_xxxxx"
)
```

## Rate Limits

Linear enforces rate limits to ensure fair usage:

### Request Limits

- **API Key Authentication**: 1,500 requests per hour per user
- **OAuth Application**: 500 requests per hour per user/application

### Complexity Limits

Linear uses complexity-based rate limiting where each query is assigned complexity points:

- **API Key Authentication**: 250,000 complexity points per hour per user
- **OAuth Application**: 200,000 complexity points per hour per user/application

### Rate Limit Headers

Response headers include rate limit information:

- `x-ratelimit-requests-remaining`: Remaining requests in the current window
- `x-ratelimit-requests-reset`: Unix timestamp when the rate limit resets
- `x-complexity-remaining`: Remaining complexity points
- `x-complexity-reset`: Unix timestamp when complexity resets

### Handling Rate Limits

When rate limit is exceeded, the API returns:

```json
{
  "success": false,
  "error": "Linear API rate limit exceeded. Resets at: 1732896000",
  "status_code": 403
}
```

## Error Handling

### Common Errors

**Authentication Failed (401)**

```json
{
  "success": false,
  "error": "Linear authentication failed. Check your API key.",
  "status_code": 401
}
```

**Resource Not Found (404)**

```json
{
  "success": false,
  "error": "Issue not found",
  "graphql_errors": [...]
}
```

**Invalid Input (400)**

```json
{
  "success": false,
  "error": "Issue title is required and cannot be empty"
}
```

**Rate Limit Exceeded (403)**

```json
{
  "success": false,
  "error": "Linear API rate limit exceeded. Resets at: 1732896000",
  "status_code": 403
}
```

### GraphQL Errors

Linear's GraphQL API may return specific error details:

```json
{
  "success": false,
  "error": "Team not found; Invalid priority value",
  "graphql_errors": [
    {
      "message": "Team not found",
      "path": ["issueCreate"],
      "extensions": {
        "code": "NOT_FOUND"
      }
    }
  ]
}
```

## Best Practices

1. **Use Team List First**
   - Call `linear_list_teams()` to discover team IDs before creating issues
   - Store team IDs in agent memory for future reference

2. **Store Issue IDs**
   - Linear uses UUIDs for issue IDs (e.g., "abc-123-def-456")
   - Issue identifiers (e.g., "ENG-42") are human-readable but not used in API calls
   - Store both the ID and identifier for agent reference

3. **Handle Rate Limits**
   - Monitor rate limit headers in responses
   - Implement exponential backoff when approaching limits
   - Cache team and issue data when possible

4. **Error Recovery**
   - Always check the `success` field in responses
   - Parse the `error` field for user-friendly messages
   - Check `graphql_errors` for detailed error information

5. **Priority Values**
   - Use consistent priority values across your organization
   - Document your team's priority conventions
   - Consider using priority 3 (Medium) as default if not specified

## Implementation Files

- **Tools Definition**: `/Users/davidrose/git/zerg/apps/zerg/backend/zerg/tools/builtin/linear_tools.py`
- **Integration**: `/Users/davidrose/git/zerg/apps/zerg/backend/zerg/tools/builtin/__init__.py`
- **Validation Script**: `/Users/davidrose/git/zerg/scripts/validate_linear_connector.py`
- **Documentation**: `/Users/davidrose/git/zerg/docs/LINEAR_CONNECTOR.md`

## Testing

### Validation Script

Run the validation script to verify GraphQL patterns without making API calls:

```bash
python scripts/validate_linear_connector.py
```

### Manual Testing

1. Generate a test API key from Linear
2. Export it: `export LINEAR_API_KEY='lin_api_xxxxx'`
3. Use the Zerg dashboard to configure an agent with the Linear connector
4. Test each tool through the agent interface

## Additional Resources

- [Linear API Documentation](https://linear.app/docs/api/graphql)
- [Linear GraphQL Explorer](https://linear.app/docs/graphql/explorer)
- [Linear API Keys](https://linear.app/settings/api)
- [Rate Limiting Guide](https://rollout.com/integration-guides/linear/api-essentials)
