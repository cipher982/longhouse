# Handoff: Workspace Execution MVP

**Created:** 2026-01-23
**Updated:** 2026-01-24
**Status:** Implementation complete, server setup done, pending deployment verification
**Context:** Enables Jarvis to run coding tasks 24/7 via git workspaces, independent of laptop connectivity.

## Background

### The Mental Model

**Jarvis = Foreman** (orchestrator)
- Personal assistant that receives user requests
- Stays responsive - never blocks on long-running work
- Spawns workers to do actual tasks
- Runs on zerg-vps 24/7

**Workers = Contractors** (0 to many in parallel)
- Do the actual work delegated by Jarvis
- Can run up to 5 concurrent jobs per processor
- Two execution paths depending on task type

### Execution Paths

| Mode | Name in Code | What Happens | Use Case |
|------|--------------|--------------|----------|
| **Standard** | `execution_mode="standard"` | In-process `WorkerRunner` with full `AgentRunner` ReAct loop | Tool-based tasks, research, API calls |
| **Workspace** | `execution_mode="workspace"` | Subprocess `hatch` CLI in git workspace | Coding tasks requiring file edits |

Old names `"local"` and `"cloud"` still work for backward compatibility.

**Key insight:** "standard" and "workspace" don't refer to geographic location. Both run on zerg-vps. The distinction is:
- Standard: Zerg's own agentic loop with Zerg tools
- Workspace: External agent CLI (claude-code/codex via `hatch`) with git isolation

### Why External Agent CLIs?

Don't reinvent agentic harnesses. Claude Code and Codex have battle-tested:
- File editing with conflict resolution
- Terminal execution
- Error recovery
- Context management

Zerg's job is **orchestration**, not reimplementing what these tools already do well.

### What About runner_exec?

`runner_exec` is just a **tool**, not an execution mode. It's SSH-over-WebSocket to reach laptop resources (browser, local files) when the laptop runner is connected. Either execution path can use it.

## Implementation Summary

### New Files Created

| File | Purpose |
|------|---------|
| `services/workspace_manager.py` | Git workspace lifecycle (clone, branch, diff) |
| `services/cloud_executor.py` | Runs `hatch` CLI as subprocess |

### Files Modified

| File | Changes |
|------|---------|
| `services/worker_job_processor.py` | Routes by execution_mode, atomic job pickup |
| `services/worker_runner.py` | Made artifact_store optional |
| `tools/builtin/supervisor_tools.py` | Extended `spawn_worker` with execution_mode, git_repo |

## Quirks and Gotchas

### 1. Naming Has Been Clarified
- `execution_mode="standard"` (formerly "local") runs in-process via WorkerRunner
- `execution_mode="workspace"` (formerly "cloud") runs in git workspace via hatch
- Old names still work for backward compatibility

### 2. Workspace Workers Don't Emit Events
Standard workers emit `worker_started`, `tool_call`, `tool_result` events. Workspace workers only emit `worker_complete`. The supervisor won't see tool-by-tool progress.

### 3. Diffs Live in Artifacts, Not Summary
Workspace execution captures git diff as an artifact (`diff.patch`), not in the worker's message stream. The supervisor summarizes based on stdout, not the actual file changes.

### 4. Max 5 Concurrent Jobs
`WorkerJobProcessor` caps at 5 concurrent jobs per processor instance, not "dozens". Plan capacity accordingly.

### 5. git_repo Is Required for Workspace
```python
spawn_worker(
    task="fix the bug",
    execution_mode="workspace",
    git_repo="git@github.com:user/repo.git"  # Required!
)
```
Validation fails without it.

### 6. Process Group Killing
Cloud executor kills the entire process group on timeout/cancellation to prevent orphan child processes from `hatch`.

### 7. URL Validation Is Strict
`workspace_manager.py` validates:
- Allowed schemes: `https://`, `ssh://`, `git@`
- No flag injection (URLs starting with `-`)
- No SSH option injection via host or user
- Percent-encoding bypass prevention

### 8. Branch Names Are Validated
Pattern: `^(?![-.]|.*\.\.)[a-zA-Z0-9/_.-]+(?<!\.lock)$`
- No starting with `-` or `.`
- No `..` sequences
- No ending with `.lock`

## Goals

### Completed
1. ✅ Set up workspace directory on zerg-vps (`/var/jarvis/workspaces`)
2. ✅ Configure SSH deploy key for GitHub access
3. ✅ Install `hatch` CLI on zerg-vps
4. ✅ Rename `local` → `standard`, `cloud` → `workspace` (with backward compat)
5. ✅ Add unit tests for cloud_executor and workspace_manager

### Remaining
1. Deploy and test end-to-end
2. Verify AWS credentials available for bedrock backend (or configure z.ai/codex)

### Future (GitHub App)
See `docs/HANDOFF-github-app-integration.md` for:
- Repo resolution ("zerg" → URL)
- Webhook-triggered agents
- Installation token management

## Tasks

### Server Setup (✅ Completed)

```bash
# On zerg-vps - already done
sudo mkdir -p /var/jarvis/workspaces
sudo chown $(whoami):$(whoami) /var/jarvis/workspaces

# SSH key for GitHub access - already done
ssh-keygen -t ed25519 -f ~/.ssh/jarvis_deploy -N "" -C "jarvis@zerg-vps"
# Deploy key added to cipher982/zerg and cipher982/hatch repos

# hatch CLI - already done
uv tool install -e ~/git/hatch  # Provides 'hatch' command
```

### Container Setup (✅ Completed)

Added to `docker-compose.prod.backend.yml`:
- `JARVIS_WORKSPACE_PATH=/var/jarvis/workspaces` env var
- `/var/jarvis/workspaces` volume mount
- `hatch-agent` added to `pyproject.toml` as git dependency (auto-installed via `uv sync`)

No manual PATH config needed - hatch is in the venv at `/app/.venv/bin/hatch`.

Optional env var for notifications:
```bash
NOTIFICATION_WEBHOOK=https://discord.com/api/webhooks/...
```

### Verification

```bash
# Unit tests (✅ passing)
cd apps/zerg/backend && uv run pytest tests -k "cloud_executor or workspace_manager"

# E2E test (manual, after deployment)
# 1. Start Zerg: make dev
# 2. In Jarvis: "Fix the typo in README.md" with execution_mode=workspace
# 3. Verify branch jarvis/<run_id> created
# 4. Verify diff artifact captured
# 5. Check Discord notification (if configured)
```

## Code Flow Reference

```
User: "Fix the auth bug in zerg"
         │
         ▼
┌─────────────────────┐
│ SupervisorService   │
│ (Jarvis/Foreman)    │
└─────────┬───────────┘
          │ spawn_worker(task, execution_mode="workspace", git_repo="...")
          ▼
┌─────────────────────┐
│ WorkerJobProcessor  │
│ (Job Router)        │
└─────────┬───────────┘
          │ execution_mode == "workspace"?
          ▼
┌─────────────────────┐
│ WorkspaceManager    │
│ - Clone repo        │
│ - Create branch     │
│   jarvis/<run_id>   │
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│ CloudExecutor       │
│ - Run hatch CLI     │
│ - Capture stdout    │
│ - Enforce timeout   │
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│ WorkspaceManager    │
│ - Capture git diff  │
│ - Save artifact     │
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│ SupervisorService   │
│ - Resume run        │
│ - Notify user       │
└─────────────────────┘
```

## Security Considerations

All addressed in implementation:

- **URL validation:** Scheme whitelist, no flag injection
- **SSH injection:** Host/user validation, percent-encoding decode
- **Branch validation:** Strict regex, no `..` or special chars
- **run_id validation:** Alphanumeric + hyphen/underscore only
- **Process isolation:** New session for subprocess, group kill on cleanup
- **Timeout enforcement:** Configurable, default 1 hour

## Files Quick Reference

```
apps/zerg/backend/zerg/
├── services/
│   ├── cloud_executor.py      # NEW - subprocess agent-run
│   ├── workspace_manager.py   # NEW - git workspace lifecycle
│   ├── worker_job_processor.py # MODIFIED - routing logic
│   └── worker_runner.py       # MODIFIED - optional artifact_store
└── tools/builtin/
    └── supervisor_tools.py    # MODIFIED - spawn_worker params
```

## Open Questions

1. **Cleanup policy:** When to delete workspace directories? Timer? After PR merge?
2. **Multi-repo:** Current MVP requires explicit git_repo. GitHub App will enable resolution.
3. **Progress visibility:** Workspace workers don't emit tool events. Add streaming from hatch?
