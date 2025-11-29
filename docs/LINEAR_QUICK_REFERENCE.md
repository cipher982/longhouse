# Linear Connector - Quick Reference

## Setup
```bash
# 1. Get API key from https://linear.app/settings/api
# 2. Configure agent with:
{
  "type": "linear",
  "api_key": "lin_api_xxxxx"
}
```

## Tools Overview

| Tool | Purpose | Key Parameters |
|------|---------|----------------|
| `linear_list_teams` | Get team IDs | `api_key` |
| `linear_create_issue` | Create issue | `api_key`, `team_id`, `title` |
| `linear_list_issues` | List issues | `api_key`, `team_id` (optional), `state` (optional) |
| `linear_get_issue` | Get issue details | `api_key`, `issue_id` |
| `linear_update_issue` | Update issue | `api_key`, `issue_id`, (at least one field) |
| `linear_add_comment` | Add comment | `api_key`, `issue_id`, `body` |

## Priority Values
```
0 = None/No Priority
1 = Urgent
2 = High
3 = Medium
4 = Low
```

## Common Workflows

### Create an Issue
```python
# 1. Get team ID
teams = linear_list_teams(api_key="lin_api_xxxxx")
team_id = teams["data"][0]["id"]

# 2. Create issue
result = linear_create_issue(
    api_key="lin_api_xxxxx",
    team_id=team_id,
    title="Bug: Login not working",
    description="Users cannot log in",
    priority=2
)

# 3. Store issue ID
issue_id = result["data"]["id"]
issue_identifier = result["data"]["identifier"]  # e.g., "ENG-42"
```

### Update and Comment
```python
# 1. Update issue
linear_update_issue(
    api_key="lin_api_xxxxx",
    issue_id=issue_id,
    state_id="state-done-id",
    priority=4
)

# 2. Add comment
linear_add_comment(
    api_key="lin_api_xxxxx",
    issue_id=issue_id,
    body="Issue resolved!"
)
```

### List and Filter
```python
# List all issues for a team
issues = linear_list_issues(
    api_key="lin_api_xxxxx",
    team_id=team_id,
    state="In Progress",
    first=50
)
```

## Error Handling

### Check Success
```python
result = linear_create_issue(...)
if result["success"]:
    issue_id = result["data"]["id"]
else:
    print(f"Error: {result['error']}")
```

### Common Errors
- **401**: Invalid API key
- **403**: Rate limit exceeded
- **404**: Resource not found
- **Validation**: Missing required fields

## Rate Limits
- **Requests**: 1,500/hour
- **Complexity**: 250,000 points/hour
- Check headers: `x-ratelimit-requests-remaining`

## Important Notes

1. **Issue IDs vs Identifiers**
   - API uses UUID: `"abc-123-def-456"` ← Use this in function calls
   - Display uses: `"ENG-42"` ← Human-readable only

2. **Team ID Required**
   - Always call `linear_list_teams()` first
   - Store team IDs for reuse

3. **State IDs**
   - Workflow states need IDs, not names
   - Get state IDs from issue objects

## Files
- **Implementation**: `apps/zerg/backend/zerg/tools/builtin/linear_tools.py`
- **Full Docs**: `docs/LINEAR_CONNECTOR.md`
- **Validation**: `scripts/validate_linear_connector.py`

## Testing
```bash
# Validate implementation
python scripts/validate_linear_connector.py

# Syntax check
python -m py_compile apps/zerg/backend/zerg/tools/builtin/linear_tools.py
```

## Resources
- API Docs: https://linear.app/docs/api/graphql
- API Keys: https://linear.app/settings/api
- GraphQL Explorer: https://linear.app/docs/graphql/explorer
