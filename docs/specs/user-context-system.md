# User Context System Specification

**Version:** 1.0
**Date:** December 2025
**Status:** Implemented core; prompt sections being revised for v2.1

> **Note:** The user context storage model (JSONB on `users.context`) remains the source of truth.
> The Jarvis “two modes” / `route_to_supervisor` prompt language in this doc is **superseded** by:
> `docs/specs/jarvis-supervisor-unification-v2.1.md`

---

## Overview

This document specifies how user-specific context (servers, preferences, integrations) is stored and injected into AI prompts. The goal is to separate generic prompt logic (what Jarvis/Supervisor/Worker ARE) from user-specific context (WHO they serve and WHAT infrastructure they have).

### Design Principles

1. **Single source of truth**: Database, not config files
2. **Fast iteration**: JSONB over rigid schemas
3. **Solo dev friendly**: Minimal moving parts
4. **Multi-tenant ready**: Per-user context isolation

---

## Data Model

### Storage: JSONB Column on Users Table

```sql
ALTER TABLE users ADD COLUMN IF NOT EXISTS context JSONB DEFAULT '{}';
```

No separate tables, no file-based configs, no environment variables. One JSON blob per user.

### Context Schema

The `context` column stores a flexible JSON object:

```json
{
  "display_name": "David",
  "role": "software engineer",
  "location": "NYC",
  "description": "I manage several servers running Docker containers for web apps and AI projects. I track health with WHOOP and keep notes in Obsidian.",
  "servers": [
    {
      "name": "cube",
      "ip": "100.70.237.79",
      "purpose": "Home GPU server - AI workloads, cameras (Frigate), home automation",
      "platform": "Ubuntu 22.04",
      "notes": "Has RTX GPU, runs Kopia backups"
    },
    {
      "name": "clifford",
      "ip": "5.161.97.53",
      "purpose": "Production VPS - 90% of web apps via Coolify",
      "platform": "Ubuntu 22.04 on Hetzner"
    },
    {
      "name": "zerg",
      "ip": "5.161.92.127",
      "purpose": "Zerg platform, dedicated project workloads",
      "platform": "Ubuntu 22.04 on Hetzner"
    },
    {
      "name": "slim",
      "ip": "135.181.204.0",
      "purpose": "EU VPS, mostly unused",
      "platform": "Ubuntu 22.04 on Hetzner"
    }
  ],
  "integrations": {
    "health_tracker": "WHOOP - tracks recovery, sleep, strain",
    "notes": "Obsidian - personal knowledge base",
    "location": "Traccar - GPS tracking",
    "backups": "Kopia - backs up to Bremen NAS (Synology)"
  },
  "custom_instructions": "I prefer concise responses. Get to the point quickly."
}
```

### Schema Flexibility

- Fields are optional - missing fields use sensible defaults
- New fields can be added without migrations
- Size limit: 64KB per context blob (enforced at API level)

---

## Prompt Architecture

### Base Prompts (Generic)

Base prompts explain WHAT the agent is and HOW it works. They contain:

- Role definition (Supervisor coordinates, Workers execute)
- Delegation logic (when to spawn workers vs answer directly)
- Tool usage patterns
- Response style guidelines
- Error handling approach

Base prompts have `{placeholder}` injection points for user-specific content.

### Prompt Composition

At runtime, final prompt = base template + user context:

```python
def build_supervisor_prompt(user: User, available_tools: list[str]) -> str:
    ctx = user.context or {}

    # Compose the prompt from base template + user context
    return BASE_SUPERVISOR_PROMPT.format(
        user_context=format_user_context(ctx),
        servers=format_servers(ctx.get('servers', [])),
        integrations=format_integrations(ctx.get('integrations', {})),
        tools=format_tools(available_tools),
    )
```

### File Structure

```
apps/zerg/backend/zerg/prompts/
├── __init__.py
├── templates.py          # Base prompt templates with {placeholders}
├── composer.py           # Functions to build final prompts from user context
└── formatters.py         # Helper functions to format context sections
```

---

## Implementation Details

### 1. Database Migration

Add context column to users table:

```python
# alembic migration
def upgrade():
    op.add_column('users', sa.Column('context', JSONB, server_default='{}'))

def downgrade():
    op.drop_column('users', 'context')
```

### 2. Base Prompt Templates

Location: `apps/zerg/backend/zerg/prompts/templates.py`

```python
BASE_SUPERVISOR_PROMPT = '''You are the Supervisor - an AI that coordinates complex tasks for your user.

## Your Role

You're the "brain" that coordinates work. Jarvis (voice interface) routes complex tasks to you. You decide:
1. Can I answer this from memory/context? → Answer directly
2. Does this need server access or investigation? → Spawn a worker
3. Have we checked this recently? → Query past workers first

## Worker Lifecycle

When you call `spawn_worker(task)`:
1. A worker agent is created with SSH access
2. Worker receives your task and figures out what commands to run
3. Worker SSHs to servers, runs commands, interprets results
4. Worker returns a natural language summary
5. You read the result and synthesize for the user

**Workers are disposable.** They complete one task and terminate.
**Workers are autonomous.** Give them a task, they figure out the commands.

## Querying Past Work

Before spawning a new worker, check if we already have the answer:
- `list_workers(limit=10)` - Recent workers with summaries
- `grep_workers("pattern")` - Search across all worker artifacts
- `read_worker_result(worker_id)` - Full result from a specific worker

## Your Tools

**Delegation:**
- `spawn_worker(task, model)` - Create a worker to investigate
- `list_workers(limit, status)` - Query past workers
- `read_worker_result(worker_id)` - Get worker findings
- `grep_workers(pattern)` - Search across workers

**Direct:**
- `get_current_time()` - Current timestamp
- `http_request(url, method)` - Simple HTTP calls
- `send_email(to, subject, body)` - Notifications

**You do NOT have SSH access.** Only workers can run commands on servers.

## Response Style

Be concise and direct. No bureaucratic fluff.

Good: "Cube is at 78% disk - mostly Docker volumes. Not urgent but worth cleaning up."
Bad: "I will now proceed to analyze the results returned by the worker agent..."

## Error Handling

If a worker fails, read the error and explain it in plain English. Suggest fixes.

---

## User Context

{user_context}

## Available Servers

{servers}

## User Integrations

{integrations}
'''


BASE_WORKER_PROMPT = '''You are a Worker agent - an autonomous executor with SSH access.

## Your Mission

The Supervisor delegated a task to you. Figure out what commands to run, execute them, interpret the results, and report back clearly.

## How to Work

1. **Read the task** - Understand what's being asked
2. **Plan your approach** - What commands will answer this?
3. **Execute commands** - Use ssh_exec, interpret output
4. **Be thorough but efficient** - Check what's needed, don't over-do it
5. **Synthesize findings** - Report back in clear, actionable language

## Useful Commands

**Disk & Storage:**
- `df -h` - Disk usage overview
- `du -sh /path/*` - Size of directories

**Docker:**
- `docker ps` - Running containers
- `docker logs --tail 100 <container>` - Recent logs
- `docker stats --no-stream` - Resource usage

**System:**
- `free -h` - Memory usage
- `uptime` - Load averages
- `systemctl status <service>` - Service status

## Response Format

End with a clear summary:

Good: "Cube disk at 78% (156GB/200GB). Largest: Docker volumes (45GB), AI models (32GB). Recommend clearing old logs."
Bad: "Here is the raw output of df -h: [dump]"

## Error Handling

If a command fails, note it, try an alternative if reasonable, report what worked and what didn't.

---

## Available Servers

{servers}

## Additional Context

{user_context}
'''


BASE_JARVIS_PROMPT = '''You are Jarvis, a personal AI assistant. You're conversational, concise, and actually useful.

## Who You Serve

{user_context}

## Your Architecture

You have two modes of operation:

**1. Direct Tools (instant, < 2 seconds)**
{direct_tools}

**2. Supervisor Delegation (5-60 seconds)**
For anything requiring server access, investigation, or multi-step work, use `route_to_supervisor`. The Supervisor has workers that can:
- SSH into servers ({server_names})
- Check disk space, docker containers, logs, backups
- Run shell commands and analyze output
- Investigate issues and report findings

## When to Delegate vs Answer Directly

**Use route_to_supervisor for:**
- Checking servers, disk space, containers, logs
- "Are my backups working?" → needs commands
- "Why is X slow?" → needs investigation
- Anything mentioning servers, docker, debugging

**Answer directly for:**
- Direct tool queries (location, health data, notes)
- General knowledge, conversation, jokes
- Time, date, simple facts

## Response Style

**Be conversational and concise.**

**When using tools:**
1. Say a brief acknowledgment FIRST ("Let me check that")
2. THEN call the tool
3. Never go silent while a tool runs

## What You Cannot Do

Be honest about limitations:
{limitations}

If asked about something you can't do, say so clearly.
'''
```

### 3. Prompt Composer

Location: `apps/zerg/backend/zerg/prompts/composer.py`

```python
from zerg.prompts.templates import (
    BASE_SUPERVISOR_PROMPT,
    BASE_WORKER_PROMPT,
    BASE_JARVIS_PROMPT,
)

def format_user_context(ctx: dict) -> str:
    """Format user context section for prompt injection."""
    parts = []

    if name := ctx.get('display_name'):
        role = ctx.get('role', 'user')
        location = ctx.get('location')
        loc_str = f" based in {location}" if location else ""
        parts.append(f"{name} - {role}{loc_str}")

    if desc := ctx.get('description'):
        parts.append(desc)

    if instructions := ctx.get('custom_instructions'):
        parts.append(f"\nUser preferences: {instructions}")

    return "\n".join(parts) if parts else "(No user context configured)"


def format_servers(servers: list[dict]) -> str:
    """Format server list for prompt injection."""
    if not servers:
        return "(No servers configured)"

    lines = []
    for s in servers:
        name = s.get('name', 'unknown')
        ip = s.get('ip', '')
        purpose = s.get('purpose', '')
        platform = s.get('platform', '')
        notes = s.get('notes', '')

        line = f"**{name}**"
        if ip:
            line += f" ({ip})"
        if purpose:
            line += f" - {purpose}"
        if platform:
            line += f" [{platform}]"
        if notes:
            line += f"\n  Notes: {notes}"

        lines.append(line)

    return "\n".join(lines)


def format_server_names(servers: list[dict]) -> str:
    """Format just server names for inline reference."""
    if not servers:
        return "no servers configured"
    return ", ".join(s.get('name', 'unknown') for s in servers)


def format_integrations(integrations: dict) -> str:
    """Format integrations section for prompt injection."""
    if not integrations:
        return "(No integrations configured)"

    lines = []
    for key, value in integrations.items():
        lines.append(f"- **{key}**: {value}")

    return "\n".join(lines)


def build_supervisor_prompt(user) -> str:
    """Build complete supervisor prompt with user context."""
    ctx = user.context or {}

    return BASE_SUPERVISOR_PROMPT.format(
        user_context=format_user_context(ctx),
        servers=format_servers(ctx.get('servers', [])),
        integrations=format_integrations(ctx.get('integrations', {})),
    )


def build_worker_prompt(user) -> str:
    """Build complete worker prompt with user context."""
    ctx = user.context or {}

    return BASE_WORKER_PROMPT.format(
        servers=format_servers(ctx.get('servers', [])),
        user_context=format_user_context(ctx),
    )


def build_jarvis_prompt(user, enabled_tools: list[dict]) -> str:
    """Build complete Jarvis prompt with user context and tools."""
    ctx = user.context or {}

    # Format direct tools
    if enabled_tools:
        tool_lines = [f"- **{t['name']}** - {t.get('description', '')}" for t in enabled_tools]
        direct_tools = "\n".join(tool_lines)
    else:
        direct_tools = "(No direct tools currently enabled)"

    # Format limitations based on what's NOT available
    limitations = []
    if 'calendar' not in [t['name'] for t in enabled_tools]:
        limitations.append("- Calendar/reminders (no tool configured)")
    if 'smart_home' not in [t['name'] for t in enabled_tools]:
        limitations.append("- Smart home control (no tool configured)")
    limitations_str = "\n".join(limitations) if limitations else "None currently"

    return BASE_JARVIS_PROMPT.format(
        user_context=format_user_context(ctx),
        direct_tools=direct_tools,
        server_names=format_server_names(ctx.get('servers', [])),
        limitations=limitations_str,
    )
```

### 4. API Endpoints

Location: `apps/zerg/backend/zerg/routers/users.py` (or similar)

```python
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Any

router = APIRouter(prefix="/api/me", tags=["user"])

class ContextUpdate(BaseModel):
    context: dict[str, Any]

@router.get("/context")
async def get_context(current_user: User = Depends(get_current_user)):
    """Get current user's context configuration."""
    return {"context": current_user.context or {}}

@router.patch("/context")
async def update_context(
    update: ContextUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update user's context (merges with existing)."""
    # Size limit check (64KB)
    import json
    if len(json.dumps(update.context)) > 65536:
        raise HTTPException(400, "Context too large (max 64KB)")

    # Merge with existing context
    existing = current_user.context or {}
    existing.update(update.context)
    current_user.context = existing

    db.commit()
    return {"context": current_user.context}

@router.put("/context")
async def replace_context(
    update: ContextUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Replace user's entire context."""
    import json
    if len(json.dumps(update.context)) > 65536:
        raise HTTPException(400, "Context too large (max 64KB)")

    current_user.context = update.context
    db.commit()
    return {"context": current_user.context}
```

### 5. Integration Points

Update these existing files to use the new composer:

**Supervisor Service** (`services/supervisor_service.py`):

```python
from zerg.prompts.composer import build_supervisor_prompt

# Replace: get_supervisor_prompt()
# With: build_supervisor_prompt(user)
```

**Worker Runner** (`services/worker_runner.py`):

```python
from zerg.prompts.composer import build_worker_prompt

# Replace: get_worker_prompt()
# With: build_worker_prompt(user)
```

**Jarvis Session** (`routers/jarvis.py` or session handler):

```python
from zerg.prompts.composer import build_jarvis_prompt

# Pass user and enabled tools to build the prompt
```

---

## Jarvis Frontend

### Option: Backend-Composed Prompts

The cleanest approach: backend composes all prompts, frontend just receives them.

Update the session/token endpoint to return the composed prompt:

```python
@router.post("/api/jarvis/session")
async def create_session(
    current_user: User = Depends(get_current_user),
):
    # Get user's enabled tools
    enabled_tools = get_enabled_tools(current_user)

    # Build the prompt server-side
    instructions = build_jarvis_prompt(current_user, enabled_tools)

    # Return token + instructions
    return {
        "token": create_realtime_token(),
        "instructions": instructions,
        "tools": enabled_tools,
    }
```

Frontend uses the returned `instructions` directly instead of generating them.

---

## Migration & Seeding

### Database Migration

```python
"""Add context column to users table

Revision ID: xxx
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

def upgrade():
    op.add_column('users', sa.Column('context', JSONB, server_default='{}', nullable=False))

def downgrade():
    op.drop_column('users', 'context')
```

### Seed David's Context

After migration, seed the initial user context:

```python
# One-time script or in migration
david_context = {
    "display_name": "David",
    "role": "software engineer",
    "location": "NYC",
    "description": "I manage several servers running Docker containers for web apps and AI projects. I use Kopia for backups to a Synology NAS, track health with WHOOP, and keep notes in Obsidian.",
    "servers": [
        {
            "name": "cube",
            "ip": "100.70.237.79",
            "purpose": "Home GPU server - AI workloads, cameras (Frigate), Stop Sign Nanny, home automation",
            "platform": "Ubuntu 22.04",
            "notes": "Has RTX GPU, 32GB RAM. Runs Kopia backups to Bremen NAS."
        },
        {
            "name": "clifford",
            "ip": "5.161.97.53",
            "purpose": "Production VPS - 90% of web apps via Coolify",
            "platform": "Ubuntu 22.04 on Hetzner"
        },
        {
            "name": "zerg",
            "ip": "5.161.92.127",
            "purpose": "Zerg platform itself, dedicated project workloads",
            "platform": "Ubuntu 22.04 on Hetzner"
        },
        {
            "name": "slim",
            "ip": "135.181.204.0",
            "purpose": "EU VPS, mostly unused ($5/month placeholder)",
            "platform": "Ubuntu 22.04 on Hetzner"
        }
    ],
    "integrations": {
        "health_tracker": "WHOOP - tracks recovery score, sleep quality, strain",
        "notes": "Obsidian - personal knowledge base and project docs",
        "location": "Traccar - GPS tracking",
        "backups": "Kopia - backs up to Bremen NAS (Synology) with MinIO S3"
    },
    "custom_instructions": "I prefer concise responses. Get to the point quickly. Don't be verbose or bureaucratic."
}

# Apply to user
user = db.query(User).filter(User.email == "david@...").first()
user.context = david_context
db.commit()
```

---

## Testing Requirements

### Unit Tests

1. **Prompt Composer Tests** (`tests/test_prompt_composer.py`):
   - `test_format_user_context_full` - All fields present
   - `test_format_user_context_minimal` - Only required fields
   - `test_format_user_context_empty` - Empty context dict
   - `test_format_servers_multiple` - List of servers
   - `test_format_servers_empty` - No servers
   - `test_build_supervisor_prompt` - Full composition
   - `test_build_worker_prompt` - Full composition
   - `test_build_jarvis_prompt` - Full composition with tools

2. **API Tests** (`tests/test_user_context_api.py`):
   - `test_get_context` - Returns current context
   - `test_patch_context_merge` - Merges with existing
   - `test_put_context_replace` - Replaces entirely
   - `test_context_size_limit` - Rejects >64KB
   - `test_context_auth_required` - 401 without auth

### Integration Tests

1. **Supervisor with Context**:
   - Create user with context
   - Trigger supervisor task
   - Verify prompt includes user's servers
   - Verify worker can SSH to user's servers

2. **Jarvis Session**:
   - Create session for user
   - Verify returned instructions include user context
   - Verify tools list matches user's enabled tools

### Manual Verification

Before merging:

1. Run full test suite: `make test`
2. Start dev environment: `make dev`
3. Create a test user with context via API
4. Trigger a supervisor task, verify logs show correct prompt
5. Connect Jarvis, verify it references user's servers
6. Spawn a worker, verify it has correct server list

---

## Future Enhancements

These are NOT in scope for initial implementation:

1. **Settings UI** - Form to edit context (for now, API or direct DB)
2. **Document uploads** - PDFs/images stored in S3, RAG retrieval
3. **Context validation** - JSON schema validation for context structure
4. **Context templates** - Pre-built contexts for common setups (DevOps, homelab, etc.)
5. **Context sharing** - Export/import context configs

---

## Summary

| Component | Location                     | Purpose                            |
| --------- | ---------------------------- | ---------------------------------- |
| Storage   | `users.context` JSONB column | Single source of truth             |
| Templates | `prompts/templates.py`       | Generic base prompts               |
| Composer  | `prompts/composer.py`        | Build prompts from user + template |
| API       | `routers/users.py`           | CRUD for user context              |
| Migration | Alembic                      | Add context column                 |

**Key principle**: The database is the source of truth. Prompts are composed at runtime from templates + user context. No config files, no environment variables, no hardcoded personal information in source code.
