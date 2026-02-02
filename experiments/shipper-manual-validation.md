# Shipper Manual Validation Experiment

**Date:** 2026-01-29
**Status:** PASSED
**Target:** Validate full `zerg auth` → `zerg connect --install` → live session sync flow

---

## Results: 2026-01-29 14:38

### Phase 1: Auth - PASS
- [x] Token created via API (dev mode, auth disabled)
- [x] Token validated by CLI (`zerg auth --token`)
- [x] Files stored correctly at `~/.claude/zerg-device-token`

### Phase 2: Service - PASS
- [x] Plist created at `~/Library/LaunchAgents/com.swarmlet.shipper.plist`
- [x] Service running (PID 12905)
- [x] Logs at `~/.claude/shipper.log` show watcher started

### Phase 3: Shipping - PASS
- [x] Watcher detected 7220 session files
- [x] Events shipped successfully (83 sessions ingested)
- [x] Latency < 2s for new events (when not rate-limited)
- Note: Hit 429s during backlog catch-up, correctly spooled for retry

### Phase 4: UI - PARTIAL
- [x] API sessions endpoint works
- [x] Events correct with accurate counts
- [ ] No dedicated sessions UI page exists yet (API-only)

### Phase 5: Cleanup - PASS
- [x] Service uninstalled cleanly
- [x] Auth cleared with `--clear`
- Note: Spool DB and state file remain (intentional)

---

## Hypothesis

If the shipper is correctly implemented, then:
1. `zerg auth` will store credentials in `~/.claude/`
2. `zerg connect --install` will register a launchd service that auto-starts
3. The watcher will detect Claude Code session changes in sub-second time
4. Sessions will appear in the Zerg UI timeline immediately

---

## Pre-Conditions

Before starting, verify clean slate:

```bash
# 1. No existing service installed
launchctl list | grep swarmlet
# Expected: no output (or "Could not find service")

# 2. No existing credentials
ls -la ~/.claude/zerg-*
# Expected: "No such file or directory" OR old files to remove

# 3. Local dev stack is running
curl -s http://localhost:47300/health | jq .status
# Expected: "ok"

# 4. Frontend accessible
curl -s http://localhost:30080 -o /dev/null -w "%{http_code}"
# Expected: 200
```

### Clean Slate (if needed)

```bash
# Remove old credentials
rm -f ~/.claude/zerg-device-token ~/.claude/zerg-url

# Uninstall old service (if exists)
cd ~/git/zerg/apps/zerg/backend && uv run zerg connect --uninstall

# Remove old plist manually if needed
rm -f ~/Library/LaunchAgents/com.swarmlet.shipper.plist
```

---

## Phase 1: Authentication

### Step 1.1: Run `zerg auth`

```bash
cd ~/git/zerg/apps/zerg/backend
uv run zerg auth --url http://localhost:47300
```

### Expected Behavior

1. CLI prompts: "Open browser to create token?"
2. Browser opens to `http://localhost:30080/dashboard/settings/devices`
3. User creates a device token in UI (copy it)
4. CLI prompts: "Device token"
5. User pastes token
6. CLI validates against API
7. CLI prints: "Authenticated successfully as <hostname>"

### Validation Checkpoints

```bash
# Token file created with correct permissions
ls -la ~/.claude/zerg-device-token
# Expected: -rw------- (600 permissions)

# Token content exists (don't print actual token)
test -s ~/.claude/zerg-device-token && echo "Token stored" || echo "FAIL: No token"

# URL file created
cat ~/.claude/zerg-url
# Expected: http://localhost:47300

# Token validates against API
TOKEN=$(cat ~/.claude/zerg-device-token)
curl -s -H "X-Agents-Token: $TOKEN" http://localhost:47300/api/agents/sessions?limit=1 | jq .
# Expected: JSON response (empty array is fine, just not 401/403)
```

### Failure Scenarios

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| Browser doesn't open | webbrowser module issue | Open URL manually |
| "Invalid token" | Token copy error or API mismatch | Re-copy, check URL matches |
| 401 on validation | Token not in DB | Re-create token in UI |
| 403 on validation | Token revoked | Create new token |

---

## Phase 2: Service Installation

### Step 2.1: Install service

```bash
cd ~/git/zerg/apps/zerg/backend
uv run zerg connect --install
```

### Expected Behavior

1. CLI prints: "Installing shipper service..."
2. CLI prints: "URL: http://localhost:47300"
3. CLI prints: "Mode: watch"
4. CLI prints: "[OK] Service installed and started..."
5. launchd service is registered and running

### Validation Checkpoints

```bash
# 1. Plist file created
cat ~/Library/LaunchAgents/com.swarmlet.shipper.plist
# Expected: XML with ProgramArguments, KeepAlive=true, RunAtLoad=true

# 2. Service is loaded in launchd
launchctl list | grep swarmlet
# Expected: PID number, status 0, com.swarmlet.shipper

# 3. Service status via CLI
uv run zerg connect --status
# Expected: "Status: running"

# 4. Process is running
pgrep -f "zerg connect"
# Expected: PID number

# 5. Log file exists
ls -la ~/.claude/shipper.log
# Expected: File exists

# 6. Log shows startup
tail -20 ~/.claude/shipper.log
# Expected: "Connecting to...", "Mode: file watching"
```

### Failure Scenarios

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| "Failed to load launchd service" | Bad plist XML | Check plist, fix and reload |
| Service shows "stopped" | Crashed on startup | Check `~/.claude/shipper.log` |
| No PID in launchctl list | KeepAlive not working | `launchctl start com.swarmlet.shipper` |
| "uv: command not found" in log | PATH not in launchd env | Edit plist to use absolute path |

---

## Phase 3: Session Shipping

### Step 3.1: Trigger a session change

Create activity in a Claude Code session so the watcher has something to detect.

```bash
# Option A: Use current session (this one!)
# Just keep chatting - each message writes to JSONL

# Option B: Start a new Claude Code session in another terminal
cd /tmp && claude-code "echo test"
```

### Expected Behavior

1. Claude Code writes to `~/.claude/projects/<encoded-path>/<session>.jsonl`
2. Watcher detects file change (FSEvents on macOS)
3. Shipper parses new events from JSONL
4. Shipper POSTs to `/api/agents/ingest`
5. Events appear in database within ~1 second

### Validation Checkpoints

```bash
# 1. Watch the shipper log for activity
tail -f ~/.claude/shipper.log
# Expected: "[1] Shipped X events from Y sessions" or watch events

# 2. Check API for recent sessions
TOKEN=$(cat ~/.claude/zerg-device-token)
curl -s -H "X-Agents-Token: $TOKEN" \
  "http://localhost:47300/api/agents/sessions?limit=5" | jq '.[].started_at'
# Expected: Recent timestamps

# 3. Check specific session exists
curl -s -H "X-Agents-Token: $TOKEN" \
  "http://localhost:47300/api/agents/sessions?limit=1" | jq '.[0] | {id, project, user_messages}'
# Expected: Session with message count > 0
```

### Latency Test

```bash
# Get current event count
COUNT_BEFORE=$(curl -s -H "X-Agents-Token: $TOKEN" \
  "http://localhost:47300/api/agents/sessions?limit=1" | jq '.[0].tool_calls // 0')

# Send a message in Claude Code (do something that creates tool calls)
# Wait 2 seconds

# Get new count
COUNT_AFTER=$(curl -s -H "X-Agents-Token: $TOKEN" \
  "http://localhost:47300/api/agents/sessions?limit=1" | jq '.[0].tool_calls // 0')

echo "Before: $COUNT_BEFORE, After: $COUNT_AFTER"
# Expected: COUNT_AFTER > COUNT_BEFORE within 2 seconds
```

### Failure Scenarios

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| No log activity | Watcher not detecting | Check watchdog import, try polling mode |
| "API unreachable" in log | URL wrong or service down | Check `~/.claude/zerg-url`, check backend |
| "Spooled X events" | API rejecting or unreachable | Check shipper.log errors, check spool DB |
| Events in DB but old | Offset tracking stale | Check `~/.claude/zerg-shipper-state.json` |

---

## Phase 4: UI Verification

### Step 4.1: Check timeline

Open browser: `http://localhost:30080/dashboard`

### Expected Behavior

1. Sessions list shows recent sessions
2. Session from Phase 3 appears
3. Clicking session shows events (messages, tool calls)
4. Events have correct timestamps and content

### Validation Checkpoints

- [ ] Sessions page loads without errors
- [ ] Recent session appears in list
- [ ] Session detail shows correct project name
- [ ] Message count matches what was sent
- [ ] Tool calls appear (Read, Bash, etc.)
- [ ] Timestamps are recent (not stale)

### Step 4.2: Check Oikos integration (if applicable)

If the session picker is wired to Oikos:

1. Go to `http://localhost:30080/chat`
2. Ask Oikos about recent sessions
3. Verify it can see shipped data

---

## Phase 5: Cleanup & Teardown

### Uninstall service

```bash
cd ~/git/zerg/apps/zerg/backend
uv run zerg connect --uninstall
# Expected: "[OK] Service stopped and removed"
```

### Verify clean

```bash
# Service removed
launchctl list | grep swarmlet
# Expected: no output

# Plist removed
ls ~/Library/LaunchAgents/com.swarmlet.shipper.plist
# Expected: "No such file or directory"

# Credentials still exist (intentional - auth separate from service)
ls ~/.claude/zerg-device-token
# Expected: File exists
```

### Full clean (optional)

```bash
uv run zerg auth --clear
# Expected: "Cleared stored token and URL"
```

---

## Results Template

```markdown
## Run: YYYY-MM-DD HH:MM

### Phase 1: Auth
- [ ] Token created in UI
- [ ] Token validated by CLI
- [ ] Files stored correctly
- Notes:

### Phase 2: Service
- [ ] Plist created
- [ ] Service running
- [ ] Logs look correct
- Notes:

### Phase 3: Shipping
- [ ] Watcher detected changes
- [ ] Events shipped successfully
- [ ] Latency < 2s
- Notes:

### Phase 4: UI
- [ ] Sessions visible
- [ ] Events correct
- [ ] No errors
- Notes:

### Phase 5: Cleanup
- [ ] Service uninstalled
- [ ] Clean state restored
- Notes:

### Overall: PASS / FAIL
Issues found:
```

---

## Quick Reference

| File | Purpose |
|------|---------|
| `~/.claude/zerg-device-token` | API auth token |
| `~/.claude/zerg-url` | API base URL |
| `~/.claude/shipper.log` | Service logs |
| `~/.claude/zerg-shipper-state.json` | Offset tracking |
| `~/.claude/zerg-shipper-spool.db` | Offline queue |
| `~/Library/LaunchAgents/com.swarmlet.shipper.plist` | Service definition |
