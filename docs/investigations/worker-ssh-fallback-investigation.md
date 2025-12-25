# Worker SSH Fallback Investigation

**Date:** December 22, 2025
**Status:** Investigation Required
**Priority:** High - Blocks basic infrastructure operations

---

## Problem Statement

Workers spawn successfully for infrastructure tasks, but fail when runner_exec fails instead of falling back to ssh_exec.

**Example flow:**
```
User: "check disk space on cube"
Supervisor: ✅ Spawns worker
Worker: ✅ Starts execution
Worker: ✅ Calls runner_exec(target='cube', command='df -h')
Worker: ❌ Fails with "Runner 'cube' not found"
Worker: ❌ STOPS instead of trying ssh_exec fallback
Worker: ❌ Reports failure to supervisor
```

**Expected flow:**
```
Worker: ❌ runner_exec fails
Worker: ✅ Falls back to ssh_exec(host='root@100.70.237.79', command='df -h')
Worker: ✅ Returns disk space results
```

---

## Current State

### What Works
- ✅ Supervisor recognizes infrastructure requests
- ✅ Supervisor spawns workers immediately
- ✅ Workers receive correct task descriptions
- ✅ Workers have both `runner_exec` and `ssh_exec` in their allowlist

### What Doesn't Work
- ❌ Workers don't fall back from runner_exec to ssh_exec
- ❌ Workers give up after first connector failure

### Evidence from Logs
```
worker_tool_started: runner_exec target='cube'
worker_tool_failed: Runner 'cube' not found (11ms)
worker_complete: status=failed
```

No attempt at ssh_exec visible in the logs.

---

## Architecture Deep Dive

### Worker Tool Allowlist

**Location:** `apps/zerg/backend/zerg/services/worker_runner.py` lines 447-460

```python
default_worker_tools = [
    "runner_exec",      # Preferred: user-owned runner daemons
    "ssh_exec",         # Legacy fallback (requires SSH keys)
    "http_request",     # API calls
    "get_current_time", # Time lookups
    "send_email",       # Notifications
    "contact_user",     # Notify owner
    "knowledge_search", # User knowledge base
    "web_search",       # Web search
    "web_fetch",        # Fetch web pages
]
```

Both connectors are available to workers.

### Worker System Prompt

**Location:** `apps/zerg/backend/zerg/prompts/templates.py` lines 231-316

**Key instruction (lines 237-245):**
```markdown
## How to Work

1. **Read the task** - Understand what's being asked
2. **Plan your approach** - What commands will answer this?
3. **Execute commands** - Prefer runner_exec; use ssh_exec only as a legacy fallback
4. **Be thorough but efficient** - Check what's needed, don't over-do it
5. **Synthesize findings** - Report back in clear, actionable language
```

**Problem:** Prompt says "prefer runner_exec" but doesn't explicitly say "if runner_exec fails, try ssh_exec".

### Execution Connectors

**runner_exec:** `apps/zerg/backend/zerg/tools/builtin/runner_tools.py`
- Executes on user-owned Runner daemons
- Returns error envelope: `{"ok": False, "error": {"type": "connector_not_configured", "message": "Runner 'cube' not found"}}`

**ssh_exec:** `apps/zerg/backend/zerg/tools/builtin/ssh_tools.py`
- Executes via SSH from backend
- Requires SSH keys in `~/.ssh/` (id_ed25519 or id_rsa)
- Expects host format: `user@hostname` or `user@hostname:port`

### Tool Error Handling

**Location:** `apps/zerg/backend/zerg/agents_def/zerg_react_agent.py` lines 311-379

Workers check for "critical errors" that should stop execution:
- "not configured"
- "ssh key not found"
- "permission_denied"
- "execution_error"

**Line 338:** "connector_not_configured" is marked as CRITICAL → worker fails fast instead of trying alternatives.

---

## Diagnostic Scripts to Run

### Script 1: Check SSH Key Availability

**Goal:** Verify if backend container has SSH keys for fallback

```bash
docker exec zerg-zerg-backend-1 ls -la ~/.ssh/
docker exec zerg-zerg-backend-1 cat ~/.ssh/config 2>/dev/null || echo "No SSH config"
```

**Expected:**
- If keys exist → ssh_exec fallback should work
- If no keys → ssh_exec will also fail (need to add keys or use runners)

### Script 2: Test runner_exec Tool Directly

**Goal:** Understand exact error response format

```python
docker exec zerg-zerg-backend-1 python -c "
from zerg.tools.builtin.runner_tools import runner_exec

# Try to execute on nonexistent runner
result = runner_exec(target='cube', command='echo test', timeout_secs=30)
print('runner_exec result:')
print(result)
print()
print('Error type:', result.get('error', {}).get('type'))
"
```

### Script 3: Test ssh_exec Tool Directly

**Goal:** Verify if SSH fallback works at all

```python
docker exec zerg-zerg-backend-1 python -c "
from zerg.tools.builtin.ssh_tools import ssh_exec

# Try SSH to cube (substitute correct user)
result = ssh_exec(host='root@100.70.237.79', command='echo test', timeout_secs=30)
print('ssh_exec result:')
print(result)
"
```

### Script 4: Trace Worker Tool Execution

**Goal:** See which tools worker calls and in what order

```bash
# Enable verbose logging
docker exec zerg-zerg-backend-1 sh -c "export DEBUG_LLM_INPUT=1 && python scripts/test_worker_spawn.py"

# Or check logs
docker logs zerg-zerg-backend-1 2>&1 | grep -E "tool_call|runner_exec|ssh_exec" | tail -50
```

---

## Root Cause Hypotheses

### Hypothesis 1: Critical Error Detection Too Aggressive

**Location:** `apps/zerg/backend/zerg/agents_def/zerg_react_agent.py` lines 311-379

The `_is_critical_error()` function treats "connector_not_configured" as critical:

```python
# Line 338
"connector_not_configured",  # Marked as critical
```

When runner_exec fails with this error, the worker:
1. Marks it as critical (line 526)
2. Sets `ctx.has_critical_error = True`
3. Stops execution (lines 628-639)
4. Never tries ssh_exec

**Why this is wrong for runner_exec:**
- runner_exec failing is NOT a critical error if ssh_exec exists as fallback
- "connector_not_configured" should only be critical for connectors with NO alternatives

**Fix:** Make error criticality context-aware:
```python
# runner_exec failure is NOT critical if ssh_exec is available
# Only critical if BOTH methods are unavailable
```

### Hypothesis 2: Worker Prompt Doesn't Emphasize Fallback

**Location:** `apps/zerg/backend/zerg/prompts/templates.py` lines 237-245

Current instruction:
```
Execute commands - Prefer runner_exec; use ssh_exec only as a legacy fallback
```

This is too vague. Worker needs explicit fallback logic:
```
Execute commands:
1. Try runner_exec first (preferred)
2. If runner_exec fails with "Runner not found", immediately try ssh_exec
3. Only report failure if BOTH methods fail
```

### Hypothesis 3: Worker Doesn't Know SSH Connection Details

Workers may not know how to construct the ssh_exec call:
- runner_exec takes `target='cube'` (simple)
- ssh_exec takes `host='user@ip'` (requires user + IP lookup)

Worker prompt includes "Available Servers" section with IPs, but worker may not realize it should:
1. Look up cube → 100.70.237.79
2. Determine SSH user (root? zerg? configured somewhere?)
3. Call ssh_exec(host='root@100.70.237.79', command='...')

---

## Recommended Investigation Steps

### Step 1: Verify SSH Keys Exist
```bash
docker exec zerg-zerg-backend-1 ls -la ~/.ssh/
```

If no keys exist, SSH fallback won't work regardless of code changes.

### Step 2: Test Connectors Independently
Run Script 2 and Script 3 above to verify:
- runner_exec returns expected error format
- ssh_exec works when called directly

### Step 3: Examine Worker Thread for Tool Calls
```python
# Check worker artifacts for the failed run
docker exec zerg-zerg-backend-1 python -c "
from zerg.services.worker_artifact_store import WorkerArtifactStore

store = WorkerArtifactStore()
# Use actual worker_id from logs
worker_id = '2025-12-22T01-50-11_check-overall-and-per-filesyst'

# Read thread.jsonl to see full conversation
thread = store.read_worker_file(worker_id, 'thread.jsonl')
print(thread)
"
```

This will show:
- Did the worker see the error from runner_exec?
- What did it decide to do next?
- Why didn't it call ssh_exec?

### Step 4: Check Critical Error Logic
```python
# Test if "connector_not_configured" is flagged as critical
docker exec zerg-zerg-backend-1 python -c "
from zerg.agents_def.zerg_react_agent import _is_critical_error

result = '{\"ok\": false, \"error\": {\"type\": \"connector_not_configured\", \"message\": \"Runner not found\"}}'
is_critical = _is_critical_error(result, 'Runner not found')
print(f'Is connector_not_configured critical? {is_critical}')
"
```

If this returns True, we've found the issue.

---

## Proposed Solutions

### Solution 1: Make runner_exec Failures Non-Critical (Recommended)

**File:** `apps/zerg/backend/zerg/agents_def/zerg_react_agent.py`

**Change:** Remove "connector_not_configured" from critical indicators, or make it conditional:

```python
def _is_critical_error(result_content: str, error_msg: str | None, tool_name: str = None) -> bool:
    # Special case: runner_exec failures are NOT critical (ssh_exec fallback exists)
    if tool_name == "runner_exec":
        return False  # Let worker try ssh_exec

    # ... rest of logic
    config_indicators = [
        "not configured",
        # ... etc
    ]
```

### Solution 2: Update Worker Prompt with Explicit Fallback Instructions

**File:** `apps/zerg/backend/zerg/prompts/templates.py`

**Add after line 245:**

```markdown
## Connector Fallback Logic

When accessing infrastructure, try connectors in order:

1. **runner_exec** (preferred)
   - Try this first for the target server
   - If fails with "Runner not found", proceed to step 2

2. **ssh_exec** (fallback)
   - Look up server IP from "Available Servers" section
   - Determine SSH user (try: root, admin, or user from server notes)
   - Format: ssh_exec(host='user@ip', command='...')
   - If fails with "SSH key not found" or "Connection refused", report failure

Only report infrastructure tasks as failed if BOTH methods fail.

**Example:**
```
Task: "Check disk on cube"
Step 1: runner_exec(target='cube', command='df -h')
  → Error: "Runner 'cube' not found"
Step 2: Look up cube → 100.70.237.79
Step 3: ssh_exec(host='root@100.70.237.79', command='df -h')
  → Success or final error
```
```

### Solution 3: Add SSH User Mapping to User Context

**File:** `apps/zerg/backend/scripts/user_context.local.json`

Add SSH user to server definitions:

```json
{
  "servers": [
    {
      "name": "cube",
      "ip": "100.70.237.79",
      "ssh_user": "root",
      "purpose": "Home GPU server..."
    }
  ]
}
```

Then worker can construct: `ssh_exec(host='{ssh_user}@{ip}', ...)`

---

## Testing Strategy

### Test 1: Worker Fallback Behavior (Unit Test)

**File:** `apps/zerg/backend/tests/test_worker_fallback.py` (new)

```python
def test_worker_falls_back_to_ssh_after_runner_failure():
    """Worker should try ssh_exec when runner_exec fails."""
    # Mock runner_exec to fail
    # Mock ssh_exec to succeed
    # Spawn worker
    # Assert ssh_exec was called
    # Assert worker succeeded
```

### Test 2: Critical Error Classification (Unit Test)

**File:** `apps/zerg/backend/tests/unit/test_critical_errors.py`

```python
def test_runner_exec_failure_not_critical():
    """runner_exec 'not found' should not be critical."""
    from zerg.agents_def.zerg_react_agent import _is_critical_error

    result = '{"error": {"type": "connector_not_configured", "message": "Runner not found"}}'
    assert not _is_critical_error(result, "Runner not found", tool_name="runner_exec")

def test_ssh_exec_failure_is_critical():
    """ssh_exec 'not found' should be critical (no fallback)."""
    result = '{"error": {"type": "connector_not_configured", "message": "SSH key not found"}}'
    assert _is_critical_error(result, "SSH key not found", tool_name="ssh_exec")
```

### Test 3: Integration Test (E2E)

**File:** `apps/zerg/backend/tests/live/test_worker_connectivity.py`

```python
async def test_worker_infrastructure_task_with_ssh_fallback():
    """End-to-end test of worker spawning with SSH fallback."""
    # Requires: SSH keys configured in test environment
    # Spawn worker with infrastructure task
    # Assert worker tried runner_exec
    # Assert worker fell back to ssh_exec
    # Assert task completed successfully
```

---

## Investigation Checklist

- [ ] Run Script 1 - Check SSH keys in container
- [ ] Run Script 2 - Test runner_exec error format
- [ ] Run Script 3 - Test ssh_exec directly
- [ ] Run Script 4 - Examine worker thread/artifacts
- [ ] Confirm hypothesis about critical error detection
- [ ] Check if worker prompt has fallback instructions
- [ ] Check if workers know how to map server names to SSH connection strings
- [ ] Implement Solution 1 (remove runner_exec from critical errors)
- [ ] Implement Solution 2 or 3 (prompt or config changes)
- [ ] Write unit tests for fallback behavior
- [ ] Write integration test for full flow
- [ ] Verify with real infrastructure task

---

## Key Files Reference

| File | Lines | Purpose |
|------|-------|---------|
| `prompts/templates.py` | 231-316 | BASE_WORKER_PROMPT |
| `agents_def/zerg_react_agent.py` | 311-379 | Critical error detection |
| `services/worker_runner.py` | 447-460 | Worker tool allowlist |
| `tools/builtin/runner_tools.py` | - | runner_exec implementation |
| `tools/builtin/ssh_tools.py` | - | ssh_exec implementation |

---

## Expected Outcomes

After implementing fixes:

1. **Workers gracefully fall back**
   - runner_exec fails → try ssh_exec
   - Only report failure if both fail

2. **Clear error messages**
   - "Tried runner_exec (no runner 'cube' connected) and ssh_exec (connection refused). Please connect a runner or configure SSH keys."

3. **Test coverage**
   - Unit tests for critical error classification
   - Unit tests for fallback behavior
   - Integration tests for real worker execution

4. **User experience**
   - "check disk space on cube" works immediately if SSH is configured
   - Helpful error if neither method works

---

## Notes for Implementation

### SSH User Determination Strategy

When falling back to ssh_exec, workers need to construct `user@ip`. Options:

**Option A:** Default to `root`
- Simple, works for most home servers
- May fail on hardened servers

**Option B:** Add `ssh_user` field to server config
- Explicit, reliable
- Requires config update

**Option C:** Try common users in sequence
- `root` → `admin` → server name
- Flexible but slower

**Recommendation:** Start with Option A (default to root), add Option B as enhancement.

### Connector Protocol Documentation

Workers receive "connector protocols" prepended to their system prompt. Check:

**File:** `apps/zerg/backend/zerg/prompts/connector_protocols.py`

This may already document the fallback strategy. If so, workers should be following it.

### LangGraph Checkpointing Consideration

Workers use checkpointing - if a worker fails mid-execution, restarting it should resume from the last checkpoint, not retry runner_exec. Verify this doesn't interfere with fallback logic.

---

## Success Criteria

- [ ] "check disk space on cube" spawns worker
- [ ] Worker tries runner_exec first
- [ ] Worker falls back to ssh_exec when runner not found
- [ ] Worker returns disk space results (or clear error if SSH also fails)
- [ ] Unit tests pass for fallback logic
- [ ] Integration test passes with SSH configured

---

## Open Questions

1. Should workers always try both methods in parallel (race them)?
2. Should runner preference be configurable per-server?
3. What's the SSH user convention (root? match server name? configurable)?
4. Should failed runners be cached to avoid retry storms?
5. Does the "connector_not_configured" error type need sub-types (transient vs permanent)?

---

## Additional Context

### Why This Matters

This blocks ALL infrastructure operations when runners aren't connected:
- Disk space checks
- Log inspection
- Docker container status
- Process monitoring
- System health checks

Users should be able to use SSH fallback immediately without setting up runners.

### Related Issues

- Workers should probably also fall back for other connector pairs (if any exist)
- Error messages should be more actionable ("try X or Y")
- Supervisor should offer to help set up runners after worker reports SSH failure

### Performance Considerations

- Trying runner_exec first adds ~10-50ms overhead when runners aren't available
- Consider caching "no runners" state for 60s to skip futile attempts
- SSH connections take ~100-500ms (acceptable for infrastructure tasks)

---

## Debug Commands for Developer

```bash
# Enable LLM input logging
docker compose --project-name zerg --env-file .env -f docker/docker-compose.dev.yml \
  exec zerg-backend bash -c "export DEBUG_LLM_INPUT=1 && ..."

# Check worker artifacts
docker exec zerg-zerg-backend-1 ls -la /app/data/workers/

# Read worker thread for specific job
docker exec zerg-zerg-backend-1 python -c "
from zerg.services.worker_artifact_store import WorkerArtifactStore
store = WorkerArtifactStore()
print(store.read_worker_file('WORKER_ID', 'thread.jsonl'))
"

# Run debug script
docker exec zerg-zerg-backend-1 python scripts/debug_supervisor_spawn.py
```

---

**Handoff to:** [Developer Name]
**Expected Completion:** [Estimate]
**Questions/Blockers:** Contact David or post in #zerg-dev
