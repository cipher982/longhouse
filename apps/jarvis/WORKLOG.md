# Jarvis E2E Test Failure Investigation - Work Log

**Date**: December 9-10, 2025
**Status**: Infrastructure improved, root cause investigation paused
**Next Developer**: Continue from "Next Steps" section below

---

## Executive Summary

**What was attempted**: Fix failing E2E tests for text message flow in Jarvis PWA
**What was accomplished**: Significantly improved test iteration speed (2-3min → 2sec)
**What remains**: Text messages not appearing in UI - root cause still unknown

### Key Deliverables

1. ✅ Fast test iteration tools (volume mounts, granular make targets)
2. ✅ Comprehensive testing guide (`TESTING.md`)
3. ⚠️ Partial fix (best-effort persistence) - untested
4. ❌ E2E tests still failing

---

## Original Problem Statement

### User Report (from console logs)

```
[App] Realtime session connected
[App] Realtime session connected    <-- DUPLICATE
transcript: hello
transcript: hello                    <-- DUPLICATE TRANSCRIPT
```

**Symptoms**:

- Double callback invocations (`onConnected`, `onTranscript`)
- AI response messages appearing twice
- Potential state manager event listener duplication

### E2E Test Failures

```bash
# All 3 text message E2E tests failing
tests/text-message-happy-path.e2e.spec.ts:
  ✗ should send text message and display AI response
  ✗ should handle multiple message exchanges
  ✗ should show streaming indicator during response generation

# Error: Message not appearing in UI
Expected: "Say hello back in exactly 3 words"
Received: "System Ready - Tap the microphone or type a message to begin"
```

---

## Investigation Timeline

### Phase 1: Initial Bug Fix (commits ec60776, 17c8cad)

**Problem**: Duplicate callbacks
**Root cause**: `onConnected` called from TWO places:

- SESSION_CHANGED event listener
- Direct call in `connect()` method

**Fix applied**:

```typescript
// apps/web/src/hooks/useRealtimeSession.ts
await appController.connect();
// REMOVED: onConnected()  // Was causing duplicate
// SESSION_CHANGED listener is now single source of truth
```

**Tests created**:

- `tests/callback-deduplication.test.ts` (5 tests) ✅ Passing
- `tests/streaming-response.test.ts` (10 tests) ✅ Passing

### Phase 2: Secondary Issue Discovery

**User report**: "Still broken - AI response messages never appear"

**Symptoms from E2E tests**:

- Send button clicked ✓
- Input cleared ✓ (indicates `sendMessage` was called)
- Message never renders in `.transcript` ✗

**Screenshot analysis** (`test-results/before-send.png`):

- User can type message
- Voice session is connected ("READY - HOLD TO TALK")
- After clicking send: input clears but chat shows "System Ready"

### Phase 3: Deep Dive - Persistence Race Condition

**Hypothesis**: `sessionManager` not initialized when first message sent

**Evidence**:

```typescript
// useTextChannel.ts - OLD CODE
const persisted = await conversationController.addUserTurn(trimmedText)
if (!persisted) {
  throw new Error('Unable to save message')  // THROWS
}

// Catch block:
dispatch({ type: 'SET_MESSAGES', messages: messages.filter(...) })  // ROLLBACK
```

**The issue**: If `sessionManager` is null (still initializing), persistence fails, message gets rolled back.

**Fix applied** (currently in stash):

1. **Best-effort persistence**: Don't throw if persistence fails

   ```typescript
   const persisted = await conversationController.addUserTurn(trimmedText);
   if (!persisted) {
     console.warn(
       "[useTextChannel] Message persistence failed - sessionManager may not be ready",
     );
     // Continue sending anyway
   }
   ```

2. **Smarter rollback**: Only remove message if SEND fails, not persistence

   ```typescript
   const isSendError = err.message?.includes('sendText') ||
                       err.message?.includes('not initialized')
   if (isSendError) {
     // Rollback only on send failure
     dispatch({ type: 'SET_MESSAGES', messages: messages.filter(...) })
   }
   ```

3. **Initialization tracking**: Added `isInitialized` state

   ```typescript
   // types.ts
   isInitialized: boolean; // True when appController.initialize() completes

   // useRealtimeSession.ts
   appController.initialize().then(() => {
     dispatch({ type: "SET_INITIALIZED", initialized: true });
   });
   ```

**Status**: These fixes are in `git stash` and **untested**. Unclear if they solve the problem.

---

## Major Time Sink: Docker Iteration Hell

### The Problem

Every code change required:

1. Modify source code
2. Rebuild Docker image: `docker compose build --no-cache` (2-3 minutes)
3. Run test: `docker compose run playwright ...` (30-60 seconds)
4. Repeat

**Total**: 3-4 minutes per iteration
**Impact**: Spent ~90 minutes on rebuilds instead of debugging

### The Mistake

- Didn't realize web container uses COPY, not volume mounts
- Kept rebuilding without verifying changes were in container
- Never tried running tests locally with visible browser
- Added debug logs but couldn't see them (no console capture working)

---

## Solutions Implemented

### 1. Volume Mounts for Hot Reload ⚡

**File**: `docker-compose.test.yml`

```yaml
jarvis-web:
  volumes:
    - ../../apps/jarvis/apps/web/src:/app/apps/web/src:ro
    - ../../apps/jarvis/apps/web/lib:/app/apps/web/lib:ro
    - ../../apps/jarvis/packages:/app/packages:ro
    - ./tests:/app/tests:ro
```

**Impact**: Code changes now reload automatically via Vite HMR
**Speed**: 2-3 minutes → 2 seconds per iteration

### 2. Granular Make Targets 🎯

**File**: `Makefile`

```bash
make test-jarvis-unit        # Fast unit tests (3 sec)
make test-jarvis-watch       # TDD watch mode
make test-jarvis-text        # Specific E2E test
make test-jarvis-history     # Another specific test
make test-jarvis-grep GREP="test name"  # Search by name
make test-jarvis-e2e-ui      # Interactive UI
```

**Previous**: `make test-jarvis` ran ALL 213 tests
**Now**: Run exactly what you need

### 3. Local Testing Script 🔍

**File**: `apps/jarvis/test-local.sh`

```bash
./test-local.sh e2e text-message    # Visible browser
./test-local.sh e2e-debug           # Step-through debugger
./test-local.sh e2e-ui              # Interactive UI
```

**Key benefit**: See the browser, inspect console, set breakpoints

### 4. Comprehensive Guide 📚

**File**: `apps/jarvis/TESTING.md`

Complete reference with:

- Quick start commands
- Speed comparison table
- Debugging workflows
- Common patterns
- Troubleshooting guide

---

## Current State

### What Works ✅

- Unit tests: 204 passing
- New callback deduplication tests: 5 passing
- New streaming response tests: 10 passing
- Fast iteration infrastructure
- Volume mounts for hot reload

### What Fails ❌

- E2E text message tests (3 tests)
- History hydration tests (3 tests)

### What's Unknown ❓

**Critical question**: Why don't messages appear in the UI after clicking send?

**Evidence**:

1. Send button click works (input clears)
2. `dispatch({ type: 'ADD_MESSAGE' })` presumably called
3. Message never renders in `.transcript`

**Possible causes**:

1. Dispatch isn't reaching reducer
2. Reducer runs but ChatContainer doesn't re-render
3. React state closure issue
4. Message IS added but immediately removed (rollback)
5. Different state tree than expected

---

## Files Modified (Uncommitted)

### In Git Stash: "Core fixes: best-effort persistence + isInitialized tracking"

```
M apps/jarvis/apps/web/src/context/types.ts
M apps/jarvis/apps/web/src/hooks/useRealtimeSession.ts
M apps/jarvis/apps/web/src/hooks/useTextChannel.ts
M apps/jarvis/apps/web/tests/text-channel-persistence.test.ts
```

**Summary**: Makes persistence best-effort, tracks initialization, updates tests

**Status**: NOT TESTED - may or may not fix the issue

### Committed: Fast iteration tools (commit 4ec1c33)

```
M Makefile
M apps/jarvis/docker-compose.test.yml
A apps/jarvis/TESTING.md
A apps/jarvis/test-local.sh
```

---

## Next Steps (For Next Developer)

### Step 1: Use the New Tools 🚀

```bash
# Start services with hot reload enabled
cd apps/jarvis
docker compose -f docker-compose.test.yml up jarvis-server jarvis-web

# In another terminal, run test with visible browser
./test-local.sh e2e text-message
```

**Why**: You'll see exactly what happens in real-time

### Step 2: Verify Message Flow

Add temporary logging to trace the issue:

```typescript
// useTextChannel.ts - line 51
dispatch({ type: 'ADD_MESSAGE', message: userMessage })
console.log('🔍 DISPATCHED ADD_MESSAGE:', userMessage.content)

// AppContext.tsx - line 56
case 'ADD_MESSAGE':
  console.log('🔍 REDUCER ADD_MESSAGE:', action.message.content, 'current:', state.messages.length)
  return { ...state, messages: [...state.messages, action.message] }

// ChatContainer.tsx - line 26
console.log('🔍 RENDER ChatContainer, messages:', messages.length)
```

**Run test**: `make test-jarvis-text`
**Check**: Do all 3 logs appear? Are message counts correct?

### Step 3: Apply Stashed Fixes (Maybe)

```bash
git stash list  # Should show "Core fixes: best-effort persistence..."
git stash pop   # Apply the fixes
```

**Test them**:

```bash
make test-jarvis-unit        # Verify unit tests still pass
make test-jarvis-text        # Check if E2E tests now pass
```

**Decision point**:

- If tests pass → commit the fixes ✅
- If tests still fail → revert and investigate deeper

### Step 4: Debugging Workflow

**If message still doesn't appear**:

1. **Check dispatch is called**

   ```bash
   ./test-local.sh e2e text-message
   # Look for console.log statements in browser console
   ```

2. **Check reducer receives it**
   - Add `console.log` in reducer (see Step 2)
   - Verify message count increases

3. **Check ChatContainer re-renders**
   - Add `console.log` at top of ChatContainer
   - Verify it renders with updated messages

4. **Check render condition**

   ```typescript
   // ChatContainer.tsx line 35
   const hasContent =
     messages.length > 0 || isStreaming || userTranscriptPreview;
   console.log("🔍 hasContent:", hasContent, "messages:", messages.length);
   ```

5. **Inspect React DevTools**
   ```bash
   ./test-local.sh e2e-ui
   # Click test → Opens browser → Install React DevTools
   # Inspect <ChatContainer> props.messages
   ```

### Step 5: Alternative Theories to Test

**Theory 1**: Message added then immediately removed

- Check if catch block fires: add `console.error('CATCH BLOCK HIT:', error)`
- Check if SET_MESSAGES with filter is called

**Theory 2**: Wrong state tree

- Log entire state: `console.log('FULL STATE:', JSON.stringify(state))`
- Compare to expected structure

**Theory 3**: Async timing issue

- Add delays: `await new Promise(r => setTimeout(r, 1000))`
- See if message appears then disappears

**Theory 4**: React batching

- Dispatch not triggering render due to batching
- Try `flushSync()` from react-dom

---

## Key Lessons Learned

### What Went Wrong

1. **Didn't use local testing first** - Wasted 90 minutes on Docker rebuilds
2. **No console output strategy** - Added logs but couldn't see them
3. **Test command confusion** - Playwright syntax different from vitest
4. **Misunderstood test architecture** - Didn't realize real tests were in different location
5. **Lost focus** - Got fixated on persistence instead of core rendering issue

### What Went Right

1. **Fixed actual callback bug** - Tests prove it works
2. **Built excellent infrastructure** - 10x faster iteration now
3. **Created good documentation** - Next dev has clear path
4. **Stashed partial work** - Core fixes preserved but not committed untested

---

## Technical Context

### Architecture Overview

```
User types message
  ↓
TextInput.handleSend()
  ↓
useTextChannel.sendMessage()
  ↓
dispatch({ type: 'ADD_MESSAGE' }) ← Optimistic update
  ↓
AppContext reducer
  ↓
ChatContainer re-renders
  ↓
Message appears in .transcript
```

**Failure point**: Somewhere between dispatch and render

### Key Files

```
apps/jarvis/apps/web/
├── src/
│   ├── components/
│   │   ├── ChatContainer.tsx    # Renders messages
│   │   └── TextInput.tsx        # Sends messages
│   ├── context/
│   │   ├── AppContext.tsx       # State reducer
│   │   └── types.ts             # State types
│   └── hooks/
│       └── useTextChannel.ts    # Message sending logic
├── lib/
│   ├── app-controller.ts        # OpenAI transport events
│   └── conversation-controller.ts  # Persistence
└── tests/
    └── text-channel-persistence.test.ts  # Unit tests

apps/jarvis/tests/
└── text-message-happy-path.e2e.spec.ts  # E2E tests (Playwright)
```

### Test Environment

```
docker-compose.test.yml
├── jarvis-server  (port 8787)  # Backend API
├── jarvis-web     (port 8080)  # Vite dev server with HMR
└── playwright                  # Test runner
    └── tests/                  # Mounted, hot reload enabled
```

---

## Quick Reference

### Run Tests Fast

```bash
# Unit tests (3 seconds)
make test-jarvis-unit

# Specific E2E test with hot reload
make test-jarvis-text

# Debug with visible browser
./test-local.sh e2e text-message

# Interactive UI
./test-local.sh e2e-ui
```

### Inspect Failures

```bash
# View screenshot
open test-results/text-message-happy-path*/test-failed-1.png

# View full trace
npx playwright show-trace test-results/text-message-happy-path*/trace.zip
```

### Edit and Retest (No Rebuild)

```bash
# Terminal 1: Keep services running
docker compose -f docker-compose.test.yml up jarvis-server jarvis-web

# Terminal 2: Edit code, rerun (2 sec)
make test-jarvis-text
```

---

## Resources

- **Testing Guide**: `apps/jarvis/TESTING.md`
- **Test Script**: `apps/jarvis/test-local.sh`
- **Make Targets**: Run `make help | grep jarvis`
- **Stashed Fixes**: `git stash show -p` to preview
- **Previous Commits**:
  - `ec60776` - Streaming response fixes
  - `17c8cad` - Callback deduplication fixes
  - `4ec1c33` - Fast iteration tools (this work)

---

## Estimated Time to Resolution

**With new tools**: 30-60 minutes

- 5 min: Apply stash and test
- 10 min: Add logging if needed
- 15 min: Debug with visible browser
- 10 min: Fix and verify

**Without new tools**: 3-4 hours

- Still rebuilding Docker every change
- No visibility into browser console
- Trial and error without clear feedback

---

## Contact / Handoff Notes

**What worked well**:

- Unit tests are solid
- Test infrastructure is now excellent
- Volume mounts make iteration fast

**What needs attention**:

- E2E tests are the source of truth
- Don't trust unit tests alone
- Always run with visible browser first
- Add console.logs liberally - you can see them now

**Red flags to watch for**:

- If messages appear then disappear (timing issue)
- If reducer never fires (dispatch broken)
- If ChatContainer doesn't re-render (React issue)

**Good luck!** The infrastructure is ready. The bug should be quick to find now.
