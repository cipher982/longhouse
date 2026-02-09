# Shipper: Real-Time Session Sync

**Status:** Phase 5 complete (ALL PHASES DONE)
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
- `longhouse connect` defaults to watch mode

**Files:** watcher.py, connect.py updates

---

### Phase 2C: Offline Resilience ✅ COMPLETE
- SQLite spool at ~/.claude/zerg-shipper-spool.db
- Enqueue on API failure, replay on reconnect
- Background replay every 30s

**Files:** spool.py, shipper.py updates

**Fixes completed:**
1. [x] Spool status transitions (mark_failed → status='failed') - Already implemented
2. [x] Config path consistency (respect CLAUDE_CONFIG_DIR) - 461f3d8c
3. [x] Auth error handling (don't spool 401/403) - 0ffcbf7e

**Test command:** `uv run pytest tests/services/shipper/ -v`

---

### Phase 2D: Service Installation ✅ COMPLETE
- launchd plist for macOS
- systemd unit for Linux
- `zerg connect --install` to set up
- `zerg connect --uninstall` to remove
- `zerg connect --status` to check status

**Files:** service.py, connect.py updates

**Acceptance criteria:**
- [x] `zerg connect --install` creates and starts service
- [x] Service auto-starts on boot
- [x] Service restarts on failure
- [x] `zerg connect --uninstall` stops and removes service
- [x] Works on macOS (launchd) and Linux (systemd)

**Commits:** 8efd6779, 98c46708 (KeepAlive fix), + CLI interval/status fixes

---

### Phase 3: Per-Device Tokens ✅ COMPLETE
- Issue device-specific tokens during `longhouse auth`
- Token scoped to user's instance
- Revocable if device compromised
- Store in ~/.claude/longhouse-device-token

**Files:**
- models/device_token.py - DeviceToken SQLAlchemy model
- routers/device_tokens.py - CRUD API endpoints
- services/shipper/token.py - Local token storage
- cli/connect.py - `longhouse auth` command

**Acceptance criteria:**
- [x] `longhouse auth` command to obtain device token
- [x] Token persisted locally at ~/.claude/longhouse-device-token
- [x] Token validated on each ingest (X-Agents-Token header)
- [x] API to list/revoke device tokens (/api/devices/tokens)
- [x] Revoked tokens fail gracefully (401)

**Note:** Tokens do not time-expire; they remain valid until explicitly revoked.
Time-based expiry can be added in a future phase if needed.

---

### Phase 4: Ingest Protocol Hardening ✅ COMPLETE
Per VISION.md lines 339-361:

- [x] Batch events (handled via 500ms debouncing at file level)
- [x] Gzip compress payloads (configurable, on by default)
- [x] Rate limits (1000 events/min per device, soft cap)
- [x] HTTP 429 backpressure handling (exponential backoff, spools on exhaustion)

**Files:**
- shipper.py - Gzip compression, 429 handling with backoff and spooling
- routers/agents.py - Rate limiting, gzip decompression

**Tests:** test_shipper.py, test_agents_ratelimit.py

---

### Phase 5: OSS Packaging ✅ COMPLETE
Per VISION.md lines 421-467:

- [x] `longhouse ship` one-time manual sync
- [x] `longhouse connect <url>` for remote instances
- [x] Local auto-detect (default: localhost:47300)
- [ ] Homebrew formula (future release task)

**Note:** Homebrew formula is a packaging/release infrastructure task that depends on
release builds and distribution setup. The code is complete for OSS usage.

---

---

## Test Commands

```bash
# Shipper unit tests
uv run pytest tests/services/shipper/ -v

# Full backend tests
make test
```

## E2E Validation (Demo Readiness)

```bash
# Prereqs (migrations + device_tokens table check)
make shipper-e2e-prereqs

# API + CLI + watcher E2E
make test-shipper-e2e

# Live smoke test (ship + revoke flow)
make shipper-smoke-test
```

Status: Manual E2E validated 2026-01-29 (see AI-Sessions note).

**Results:**
- Auth flow: PASS (token created via API, validated with CLI)
- Service install: PASS (launchd started, stayed running)
- Session shipping: PASS (83 sessions shipped, near-real-time)
- Cleanup: PASS (uninstall + auth clear)
- UI: PARTIAL (API works, no frontend page yet)

---

## Files

| File | Purpose |
|------|---------|
| services/shipper/parser.py | Parse JSONL session files |
| services/shipper/state.py | Track shipped offsets |
| services/shipper/shipper.py | Core ship logic |
| services/shipper/watcher.py | File system watching |
| services/shipper/spool.py | Offline queue |
| services/shipper/service.py | Service installation (launchd/systemd) |
| services/shipper/token.py | Local token storage |
| models/device_token.py | DeviceToken model |
| routers/device_tokens.py | Device token API |
| cli/connect.py | CLI commands |
