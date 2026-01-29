# Shipper: Real-Time Session Sync

**Status:** Phase 2C in progress (fixes needed)
**Protocol:** SDP-1
**Vision:** VISION.md lines 320-361

## Executive Summary

The shipper syncs Claude Code (and other CLI agent) sessions to Zerg in real-time. It's the bridge between local dev and the unified session timeline.

**Magic moment:** User types in Claude Code → shipper fires → session appears in Zerg before they switch tabs.

---

## Decision Log

### Decision: File watching over polling (2B)
**Context:** Need sub-second sync latency
**Choice:** Use watchdog for FSEvents/inotify, fall back to polling
**Rationale:** Polling at 30s is too slow for "magic moment"

### Decision: SQLite spool for offline (2C)
**Context:** Need resilience when API unreachable
**Choice:** Local SQLite queue, replay on reconnect
**Rationale:** Simple, no extra deps, survives restarts

### Decision: Spool failed items after max_retries
**Context:** Items stuck in retry limbo forever
**Choice:** Set status='failed' after 5 retries
**Rationale:** Prevents infinite queue growth

---

## Phases

### Phase 2A: Polling Shipper ✅ COMPLETE
- Scan ~/.claude/projects/ for JSONL files
- Parse events incrementally (byte offset tracking)
- Ship to /api/agents/ingest
- State persistence for resume

**Commits:** 4ced48ff, 67fbed6b, e55726ac, 326cf318, 268d4c1c

---

### Phase 2B: Real-Time Watching ✅ COMPLETE
- watchdog for file system events
- Debounce rapid writes (500ms)
- Fallback scan every 5 minutes
- `zerg connect` defaults to watch mode

**Files:** watcher.py, connect.py updates

---

### Phase 2C: Offline Resilience ✅ COMPLETE (fixes needed)
- SQLite spool at ~/.claude/zerg-shipper-spool.db
- Enqueue on API failure, replay on reconnect
- Background replay every 30s

**Files:** spool.py, shipper.py updates

**Fixes needed:**
1. [ ] Spool status transitions (mark_failed → status='failed')
2. [ ] Config path consistency (respect CLAUDE_CONFIG_DIR)
3. [ ] Auth error handling (don't spool 401/403)

**Test command:** `uv run pytest tests/services/shipper/ -v`

---

### Phase 2D: Service Installation (NOT STARTED)
- launchd plist for macOS
- systemd unit for Linux
- `zerg connect --install` to set up
- `zerg connect --uninstall` to remove

**Acceptance criteria:**
- [ ] `zerg connect --install` creates and starts service
- [ ] Service auto-starts on boot
- [ ] Service restarts on failure
- [ ] `zerg connect --uninstall` stops and removes service
- [ ] Works on macOS (launchd) and Linux (systemd)

---

### Phase 3: Per-Device Tokens (NOT STARTED)
- Issue device-specific tokens during `zerg connect`
- Token scoped to user's instance
- Revocable if device compromised
- Store in ~/.claude/zerg-device-token

**Acceptance criteria:**
- [ ] `zerg connect <url>` prompts for auth if no token
- [ ] Token persisted locally
- [ ] Token validated on each ingest
- [ ] API to list/revoke device tokens
- [ ] Expired/revoked tokens fail gracefully

---

### Phase 4: Ingest Protocol Hardening (NOT STARTED)
Per VISION.md lines 339-361:

- [ ] Batch events (up to 1s or 100 events)
- [ ] Gzip compress payloads
- [ ] Rate limits (1000 events/min soft cap)
- [ ] HTTP 429 backpressure handling

---

### Phase 5: OSS Packaging (NOT STARTED)
Per VISION.md lines 421-467:

- [ ] `zerg ship` one-time manual sync
- [ ] `zerg connect <url>` for remote instances
- [ ] Local auto-detect (no explicit connect needed)
- [ ] Homebrew formula

---

## Current Task: Phase 2C Fixes

Three bugs identified in review:

### Fix 1: Spool status transitions
**Problem:** `mark_failed()` never sets status='failed'
**Solution:** Set status='failed' when retry_count >= max_retries
**File:** spool.py

### Fix 2: Config path consistency
**Problem:** Spool/state default to ~/.claude, ignoring CLAUDE_CONFIG_DIR
**Solution:** Pass claude_config_dir from ShipperConfig to spool/state
**Files:** spool.py, state.py, shipper.py

### Fix 3: Auth error handling
**Problem:** 401/403 errors get spooled (will never succeed)
**Solution:** Only spool 5xx/timeout errors; hard-fail on auth errors
**File:** shipper.py

---

## Test Commands

```bash
# Shipper unit tests
uv run pytest tests/services/shipper/ -v

# Full backend tests
make test
```

---

## Files

| File | Purpose |
|------|---------|
| services/shipper/parser.py | Parse JSONL session files |
| services/shipper/state.py | Track shipped offsets |
| services/shipper/shipper.py | Core ship logic |
| services/shipper/watcher.py | File system watching |
| services/shipper/spool.py | Offline queue |
| cli/connect.py | CLI commands |
