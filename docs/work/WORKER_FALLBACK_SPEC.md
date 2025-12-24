# Spec: Worker Infrastructure Fallback (Runner → SSH)

**Status:** Complete
**Priority:** High (Blocks basic infrastructure operations)
**Date:** 2025-12-24

## 1. Problem Statement
Workers fail to connect to infrastructure servers when `runner_exec` is unavailable, even though SSH fallback is technically possible. The root cause is the `if/elif` logic in `composer.py:format_servers()` which **hides concrete SSH connection details** when an `ssh_alias` is present. Since Docker containers don't have `~/.ssh/config` with those aliases, workers see "ssh cube" but have no way to resolve it to actual connection parameters.

### Root Cause Analysis
The bug is in `apps/zerg/backend/zerg/prompts/composer.py` lines 69-73:

```python
if ssh_alias:
    line += f"\n  SSH alias: {ssh_alias}"
elif ssh_user and ssh_host:  # ← BUG: elif means this is skipped when alias exists
    port_suffix = f":{ssh_port}" if ssh_port else ""
    line += f"\n  SSH: {ssh_user}@{ssh_host}{port_suffix}"
```

**What happens:**
1. User has `cube` server with both `ssh_alias: "cube"` AND concrete details (`ssh_user: "drose"`, `ip: "100.104.187.47"`, `ssh_port: 2222`)
2. Worker sees in its prompt: "SSH alias: cube" but NO concrete connection string
3. Worker tries `ssh_exec(host="cube", ...)` which fails in Docker (no ~/.ssh/config)
4. Even though the IP/user/port exist in the database, they're never shown to the worker

**Fix:** Change `elif` to `if` so workers see BOTH:
```
SSH alias: cube
SSH: drose@100.104.187.47:2222
```

This lets workers use the alias when available (e.g., on laptop backend) and fall back to concrete details in containers.

## 2. Desired Behavior
If a worker attempts to access infrastructure:
1. It should **prefer `runner_exec`** (standard production path).
2. If `runner_exec` returns a `validation_error` (e.g., "Runner 'X' not found" when no runner is online), the worker should **immediately try `ssh_exec`** using the server's IP and configured SSH user.
3. The worker should only report a "Critical Failure" if **both** methods fail.

---

## 3. Technical Implementation

### 3.1 Context-Aware Error Criticality ✅ COMPLETE
**File:** `apps/zerg/backend/zerg/agents_def/zerg_react_agent.py` (lines 94-97)

`_is_critical_error()` already returns `False` for `runner_exec` tool failures, allowing the LLM to continue and try SSH fallback.

### 3.2 Prompt Instructions ✅ COMPLETE
**File:** `apps/zerg/backend/zerg/prompts/templates.py` (lines 212-218)

Worker prompt already includes "Connector Fallback (Important)" section instructing workers to try `runner_exec` first, then fall back to `ssh_exec` if that fails.

### 3.3 Enhanced Server Metadata ⚠️ PARTIALLY COMPLETE
**Files:**
- Schema: `apps/zerg/backend/zerg/schemas/user_context.py`
- Seeding: `apps/zerg/backend/zerg/services/auto_seed.py`
- Formatting: `apps/zerg/backend/zerg/prompts/composer.py`

**Current state:**
- Schema (`ServerConfig`) allows `ssh_alias`, `ssh_user`, `ssh_port`, `ssh_host` via `extra="allow"` (lines 29-30)
- Local config already uses these fields (e.g., cube has `ssh_alias: "cube"`, `ssh_user: "drose"`, `ssh_port: 2222`)
- **BUG:** `composer.py:format_servers()` uses `if/elif` logic (lines 69-73):
  ```python
  if ssh_alias:
      line += f"\n  SSH alias: {ssh_alias}"
  elif ssh_user and ssh_host:
      port_suffix = f":{ssh_port}" if ssh_port else ""
      line += f"\n  SSH: {ssh_user}@{ssh_host}{port_suffix}"
  ```
  This means when `ssh_alias` is present, workers **never see** the concrete `ssh_user@ssh_host:port` details needed for Docker environments.

**What's needed:**
- Change `elif` to `if` so BOTH alias AND concrete SSH details are shown
- Workers can then use the concrete details when running in containers without ~/.ssh/config

---

## 4. Decision Points

### Decision 4.1: Default SSH User ✅ RESOLVED
**Resolution:** Use `root` as the default with per-server override capability via `ssh_user` field. Already implemented in local config (cube uses `drose`).

### Decision 4.2: Execution Strategy ✅ RESOLVED
**Resolution:** Serial fallback. Worker tries `runner_exec` first, and only falls back to `ssh_exec` if that fails. Already implemented via non-critical error handling in `zerg_react_agent.py`.

### Decision 4.3: Error Visibility ✅ RESOLVED
**Resolution:** Workers will naturally show both attempts in their reasoning/tool calls. User can see the fallback happened by reading the worker's execution log.

---

## 5. Remaining Work

### Phase 1: Foundation (COMPLETE ✅)
- [x] Make `runner_exec` failures non-critical (`zerg_react_agent.py`)
- [x] Add fallback instructions to worker prompt (`templates.py`)
- [x] Support SSH fields in schema via `extra="allow"`

### Phase 2: Concrete SSH Details (COMPLETE ✅)
- [x] **Fix `composer.py:format_servers()`** - Changed `elif` to `if` so both alias AND concrete SSH details are shown
- [x] **Formalize SSH fields in `ServerConfig` schema** - Made `ssh_user`, `ssh_host`, `ssh_port`, `ssh_alias` explicit optional fields with docstrings
- [x] **Update `user_context.example.json`** - Added SSH field examples to guide users
- [x] **Add tests** - Added `test_format_servers_with_both_ssh_alias_and_concrete()` to verify both outputs appear

### Phase 3: Validation (COMPLETE ✅)
- [x] **Unit test:** `test_runner_exec_missing_runner_is_not_critical()` in `tests/test_critical_tool_error.py` already verifies this
- [x] **Prompt composer test:** `test_format_servers_with_both_ssh_alias_and_concrete()` verifies both SSH formats appear
- [ ] **Live test (optional):** With no runner connected, ask Jarvis to check disk space on a server, verify SSH fallback works

## 6. Acceptance Criteria
- [x] Workers can connect to servers using concrete SSH details even when `ssh_alias` is present
- [ ] Asking "check disk on [server]" spawns a worker that successfully falls back to SSH when runner unavailable *(manual test)*
- [ ] Logs show `runner_exec` attempt, then `ssh_exec` fallback, then successful result *(manual test)*
- [x] Schema validation accepts servers with `ssh_alias`, `ssh_user`, `ssh_port`, `ssh_host` fields
- [x] Example config file documents all SSH-related fields

---

## 7. Implementation Summary

### All Implementation Complete ✅

**Commits:**
1. `f452e7b` - docs: update worker fallback spec with actual implementation status
2. `eb2f1b4` - fix(prompts): show both SSH alias AND concrete details in format_servers
3. `83e4e1d` - feat(schema): formalize SSH fields in ServerConfig
4. `0bcdb93` - test(prompts): add test for dual SSH alias + concrete details

**Key changes:**
1. **Fixed compositor bug** - Changed `elif` to `if` in `composer.py:71` so workers see both SSH alias AND concrete `user@host:port`
2. **Formalized SSH schema** - Added explicit `ssh_alias`, `ssh_user`, `ssh_host`, `ssh_port` fields to `ServerConfig`
3. **Updated examples** - `user_context.example.json` now documents all SSH fields
4. **Added regression test** - `test_format_servers_with_both_ssh_alias_and_concrete()` prevents future breakage

**Remaining:** Manual live test to verify end-to-end fallback behavior
