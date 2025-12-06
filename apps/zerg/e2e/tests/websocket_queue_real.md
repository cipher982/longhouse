# WebSocket Queue E2E Tests - Implementation Guide

## Overview

The tests in `websocket_queue_real.spec.ts` validate the bounded message queue implementation in `useWebSocket.tsx`. These tests exercise the **real** queue logic by observing external effects, since the internal `messageQueueRef` is not directly accessible.

## Key Challenge: Testing Internal Hooks

The message queue is **internal** to the `useWebSocket` hook:

- `messageQueueRef` is a private ref that cannot be accessed from outside
- The queue only activates when `wsRef.current.readyState !== WebSocket.OPEN`
- We need to test FIFO eviction, queue limits, and warnings

## Solution: Test Hook Exposure

We modified `useWebSocket.tsx` to expose `sendMessage` in test mode:

```typescript
// In useWebSocket.tsx (lines 367-372)
useEffect(() => {
  if (
    typeof window !== "undefined" &&
    (window as any).__TEST_WORKER_ID__ !== undefined
  ) {
    (window as any).__testSendMessage = sendMessage;
  }
}, [sendMessage]);
```

This is **only active during E2E tests** (when `__TEST_WORKER_ID__` is set by fixtures.ts).

## Test Strategy

### 1. **Mock WebSocket Creation**

Each test injects a `MockWebSocket` class via `page.addInitScript()`:

```typescript
class MockWebSocket {
  readyState = CONNECTING; // Forces useWebSocket to queue messages

  send(data: string) {
    if (this.readyState === OPEN) {
      // Track sent messages for verification
      (window as any).__sentMessages.push(JSON.parse(data));
    }
  }

  transitionToOpen() {
    this.readyState = OPEN;
    this.triggerEvent("open", {});
  }
}
```

### 2. **Intercept Console Warnings**

Tests capture `console.warn` calls to verify queue-full warnings:

```typescript
const originalWarn = console.warn;
(window as any).__warnings = [];
console.warn = (...args: any[]) => {
  const msg = args.join(" ");
  (window as any).__warnings.push(msg);
  originalWarn.apply(console, args);
};
```

### 3. **Call sendMessage Directly**

Tests use the exposed `window.__testSendMessage` to trigger queue logic:

```typescript
await page.evaluate((count: number) => {
  const sendMsg = (window as any).__testSendMessage;
  for (let i = 0; i < count; i++) {
    sendMsg({ type: "test_message", id: i, payload: `message-${i}` });
  }
}, 150);
```

### 4. **Observe External Effects**

Since the queue is internal, we observe:

- **Console warnings**: Queue full warnings with message count
- **Socket send calls**: Which messages were actually sent when socket opens
- **Message order**: FIFO ordering of flushed messages
- **Page stability**: No crashes or errors

## Test Breakdown

### Test 1: Queue Enforces 100 Message Limit with FIFO Eviction

**What it does:**

1. Creates MockWebSocket stuck in CONNECTING state
2. Sends 150 messages via `window.__testSendMessage` (all queued)
3. Verifies 50+ warnings about "queue full"
4. Transitions socket to OPEN
5. Verifies exactly 100 messages were sent (oldest 50 dropped)
6. Verifies first sent message has `id=50` (0-49 were evicted)
7. Verifies last sent message has `id=149`

**Why it works:**

- When socket is CONNECTING, all 150 calls to `sendMessage()` queue messages
- After 100 messages, each new message triggers FIFO eviction (`.shift()`)
- When socket opens, `handleConnect` flushes the queue (lines 191-196 in useWebSocket.tsx)
- We observe which messages were sent to verify FIFO behavior

**What it validates:**

- ✅ MAX_QUEUED_MESSAGES limit is enforced
- ✅ FIFO eviction (`.shift()`) removes oldest messages
- ✅ Queue doesn't grow unbounded
- ✅ Warnings are logged for each eviction

**Failure modes:**

- If we remove the queue limit check → Test fails (more than 100 messages sent)
- If we use `.pop()` instead of `.shift()` → Test fails (wrong messages sent)
- If we remove warnings → Different test fails

### Test 2: Queue Flushes in Correct Order When Connection Opens

**What it does:**

1. Creates MockWebSocket in CONNECTING state
2. Queues 10 test messages
3. Transitions socket to OPEN
4. Verifies all 10 messages were sent
5. Verifies messages sent in FIFO order (id 0-9)
6. Verifies timestamps are monotonic

**Why it works:**

- `handleConnect` (lines 185-200) iterates through `messageQueueRef.current` and sends each message
- We track timestamps to verify ordering

**What it validates:**

- ✅ Queue flush happens on connection open
- ✅ Messages are sent in FIFO order
- ✅ All queued messages are delivered

### Test 3: Queue Warning Includes Count When Dropping Messages

**What it does:**

1. Queues 120 messages (20 over limit)
2. Verifies warnings contain "queue full" or "100 messages"
3. Verifies exact warning text matches implementation

**Why it works:**

- Each message over 100 triggers `console.warn` (line 331-333)
- We intercept warnings and verify content

**What it validates:**

- ✅ Warning message format is correct
- ✅ Warning mentions the 100 message limit
- ✅ Warnings are logged consistently

**Failure modes:**

- If we remove `console.warn` → Test fails (no warnings captured)
- If we change warning text → Exact text assertion fails

### Test 4: Handles Subscribe Messages While Disconnected

**What it does:**

1. Creates MockWebSocket in CLOSED state
2. Attempts to send subscribe messages
3. Verifies page doesn't crash

**Why it works:**

- When `readyState !== OPEN`, messages are queued (lines 329-343)
- Test verifies graceful handling of disconnected state

**What it validates:**

- ✅ App doesn't crash when queueing messages while disconnected
- ✅ Subscribe messages can be queued

### Test 5: Page Remains Functional with Bounded Queue (No Crash)

**What it does:**

1. Creates MockWebSocket that eventually opens
2. Loads Dashboard page
3. Verifies no JavaScript errors
4. Verifies Dashboard UI is visible

**Why it works:**

- Smoke test for overall stability
- Ensures bounded queue doesn't break the app

**What it validates:**

- ✅ No memory leaks or crashes
- ✅ App remains functional with queue active

## Verification: Tests Catch Real Bugs

We verified tests detect implementation bugs:

### Broken FIFO Eviction

```typescript
// Changed .shift() to .pop()
messageQueueRef.current.pop(); // LIFO instead of FIFO
```

**Result:** Test 1 fails with `Expected: 50, Received: 0`

### Missing Warning

```typescript
// Removed console.warn
if (messageQueueRef.current.length >= MAX_QUEUED_MESSAGES) {
  messageQueueRef.current.shift();
}
```

**Result:** Test 3 fails with `Expected: > 0, Received: 0`

### Missing Queue Limit

```typescript
// Removed the bounds check
messageQueueRef.current.push(message);
```

**Result:** Test 1 fails (more than 100 messages sent)

## How Each Test Works

All tests follow this pattern:

1. **Setup Phase** (`addInitScript`):
   - Inject MockWebSocket class
   - Set up monitoring (console.warn, sent messages)
   - Configure socket behavior (CONNECTING/OPEN/CLOSED)

2. **Wait Phase**:
   - Navigate to page
   - Wait for `window.__testSendMessage` to be exposed
   - Poll with retry loop (avoids CSP issues)

3. **Action Phase**:
   - Call `window.__testSendMessage` many times
   - Trigger queue logic
   - Optionally transition socket state

4. **Verification Phase**:
   - Check console warnings
   - Check sent messages
   - Verify ordering, counts, content

## CSP Considerations

We **don't use** `page.waitForFunction()` because it triggers CSP violations:

```
Refused to evaluate a string as JavaScript because 'unsafe-eval' is not allowed
```

Instead, we use a polling loop with `page.evaluate()`:

```typescript
let sendMessageAvailable = false;
for (let i = 0; i < 50; i++) {
  sendMessageAvailable = await page.evaluate(
    () => typeof (window as any).__testSendMessage === "function",
  );
  if (sendMessageAvailable) break;
  await page.waitForTimeout(100);
}
expect(sendMessageAvailable).toBe(true);
```

## Running the Tests

```bash
# Run all queue tests
cd apps/zerg/e2e
npx playwright test websocket_queue_real.spec.ts

# Run specific test
npx playwright test websocket_queue_real.spec.ts -g "FIFO eviction"

# Run with UI (headed mode)
npx playwright test websocket_queue_real.spec.ts --headed

# Debug a test
npx playwright test websocket_queue_real.spec.ts --debug
```

## Test Output Example

```
✓ queue enforces 100 message limit with FIFO eviction (1.2s)
✓ queue flushes in correct order when connection opens (0.8s)
✓ queue warning includes count when dropping messages (0.7s)
✓ handles subscribe messages while disconnected (0.6s)
✓ page remains functional with bounded queue (no crash) (2.1s)

5 passed (4.3s)
```

## Maintenance Notes

### When to Update These Tests

1. **Changing MAX_QUEUED_MESSAGES**: Update test expectations for message counts
2. **Changing warning text**: Update Test 3's exact text assertion
3. **Changing queue behavior**: Update FIFO/LIFO verification logic
4. **Adding new queue features**: Add corresponding test cases

### Common Issues

**Issue:** `window.__testSendMessage` is undefined

- **Cause:** `useWebSocket` hook hasn't mounted yet
- **Fix:** Increase polling timeout or add more retries

**Issue:** Tests are flaky

- **Cause:** Race condition between socket state and message sending
- **Fix:** Add `await page.waitForTimeout()` after state transitions

**Issue:** CSP violations

- **Cause:** Using `page.waitForFunction()` or `new Function()`
- **Fix:** Use `page.evaluate()` with polling loop instead

## Summary

These tests successfully validate:

- ✅ Queue is bounded at 100 messages
- ✅ FIFO eviction removes oldest messages
- ✅ Console warnings are logged
- ✅ Queue flushes when connection opens
- ✅ App remains stable with active queue
- ✅ All queue logic uses the **real useWebSocket implementation**

The tests achieve this by:

1. Exposing `sendMessage` in test mode only
2. Mocking WebSocket to control connection state
3. Observing external effects (warnings, sent messages, ordering)
4. Verifying behavior through multiple state transitions
