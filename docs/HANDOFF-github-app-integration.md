# Handoff: GitHub App Integration for Cloud Execution

**Created:** 2026-01-22
**Context:** Cloud execution MVP is complete but requires hardcoded `git_repo`. This doc outlines the next phase.

## Problem Statement

Current cloud execution flow:
```python
spawn_worker(
    "fix the auth bug",
    execution_mode="cloud",
    git_repo="git@github.com:davidrose/zerg.git"  # ← Must be explicit
)
```

Desired flow:
```
User: "fix the auth bug in zerg"
Jarvis: (resolves "zerg" → repo URL, spawns cloud worker)
```

For autonomous agents that respond to PR comments, we also need webhooks.

## Solution: GitHub App

### Why App vs OAuth

| Capability | OAuth | GitHub App |
|------------|-------|------------|
| Clone repos | ✅ | ✅ |
| Create PRs | ✅ | ✅ |
| Comment on PRs | ✅ (as user) | ✅ (as bot) |
| **Webhooks** (get notified) | ❌ | ✅ |
| Auto-refresh tokens | ❌ | ✅ |
| User picks specific repos | ❌ (broad scope) | ✅ |

**Webhooks are key** - they let Jarvis respond autonomously when:
- PR review comment is added
- Issue is created
- Push happens

## Implementation Plan

### Phase 1: GitHub App Setup

1. **Create GitHub App** at github.com/settings/apps/new
   - Name: `Swarmlet` or `Jarvis`
   - Homepage: `https://swarmlet.com`
   - Webhook URL: `https://api.swarmlet.com/webhooks/github`
   - Permissions:
     - Contents: Read & Write (clone, push)
     - Pull requests: Read & Write (create, comment)
     - Issues: Read & Write
     - Metadata: Read
   - Events to subscribe:
     - Pull request
     - Pull request review comment
     - Issue comment
     - Push

2. **Store App credentials** in Zerg config:
   - `GITHUB_APP_ID`
   - `GITHUB_APP_PRIVATE_KEY` (PEM file contents)
   - `GITHUB_WEBHOOK_SECRET`

### Phase 2: Connector Integration

1. **Installation flow** (`/api/connectors/github-app/install`)
   ```
   User clicks "Connect GitHub"
     → Redirect to github.com/apps/swarmlet/installations/new
     → User selects repos
     → GitHub redirects back with installation_id
     → Store installation_id in connector_credentials
   ```

2. **Token management**
   - Installation tokens expire in 1 hour
   - On API call: check expiry, refresh if needed
   - Use `PyGithub` or `httpx` + JWT signing

3. **Repo listing endpoint** (`/api/github/repos`)
   - Lists repos the user has granted access to
   - Used for resolution: "zerg" → full URL

### Phase 3: Webhook Handler

1. **Endpoint**: `POST /api/webhooks/github`

2. **Signature verification**:
   ```python
   import hmac
   expected = hmac.new(WEBHOOK_SECRET, body, 'sha256').hexdigest()
   if not hmac.compare_digest(f"sha256={expected}", signature_header):
       raise HTTPException(401)
   ```

3. **Event routing**:
   ```python
   event_type = request.headers["X-GitHub-Event"]

   if event_type == "pull_request_review_comment":
       # Someone commented on PR - trigger agent response
       await handle_pr_comment(payload)

   if event_type == "issue_comment" and "pull_request" in payload["issue"]:
       # Comment on PR (not review) - also trigger
       await handle_pr_comment(payload)
   ```

4. **Agent triggering**:
   ```python
   async def handle_pr_comment(payload):
       # Don't respond to own comments (avoid loops)
       if payload["sender"]["login"] == "swarmlet[bot]":
           return

       # Create supervisor run to respond
       # ... spawn worker with cloud execution
   ```

### Phase 4: Repo Resolution

1. **Supervisor tool**: `resolve_repo(name: str) -> str`
   - Checks GitHub connector for matching repo
   - Returns clone URL with installation token

2. **Context enrichment**: When user says "fix X in zerg"
   - Supervisor calls `resolve_repo("zerg")`
   - Gets `https://x-access-token:TOKEN@github.com/user/zerg.git`
   - Passes to `spawn_worker(execution_mode="cloud", git_repo=...)`

## Database Changes

```sql
-- GitHub App installations per user
CREATE TABLE github_app_installations (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    installation_id BIGINT NOT NULL,
    account_login VARCHAR(255),  -- GitHub username or org
    account_type VARCHAR(50),    -- "User" or "Organization"
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(user_id, installation_id)
);

-- Repos accessible via installation (cached, refresh periodically)
CREATE TABLE github_installation_repos (
    id SERIAL PRIMARY KEY,
    installation_id BIGINT NOT NULL,
    repo_full_name VARCHAR(255),  -- "owner/repo"
    repo_private BOOLEAN,
    updated_at TIMESTAMP DEFAULT NOW()
);
```

## File Structure

```
apps/zerg/backend/zerg/
├── connectors/
│   └── github_app.py          # Token management, API calls
├── routers/
│   └── github_webhooks.py     # POST /webhooks/github
├── services/
│   └── github_repo_resolver.py # "zerg" → URL resolution
└── tools/builtin/
    └── github_tools.py        # resolve_repo, list_repos, create_pr
```

## Testing Plan

1. **Unit tests**: Token refresh, signature verification, event routing
2. **Integration test**: Install app on test repo, trigger webhook, verify agent responds
3. **E2E**: "fix typo in README" → PR created → comment on PR → Jarvis responds

## Security Considerations

- Webhook signature verification is mandatory
- Installation tokens are short-lived (1 hour)
- Store private key in Fernet-encrypted credentials
- Rate limit webhook processing to prevent abuse
- Don't respond to own bot comments (infinite loop prevention)

## Open Questions

1. Should Jarvis auto-respond to ALL PR comments, or only when @mentioned?
2. Multi-org support: one user with multiple GitHub orgs?
3. Should resolved repos be cached in memory or always API call?

## References

- [GitHub App Docs](https://docs.github.com/en/apps)
- [Creating a GitHub App](https://docs.github.com/en/apps/creating-github-apps)
- [Webhook Events](https://docs.github.com/en/webhooks/webhook-events-and-payloads)
- Existing connector pattern: `zerg/connectors/google.py`
