# Jarvis Legacy Code Removal - Status: MOSTLY COMPLETE (December 2024)

## Summary

The major architectural cleanup has been completed. Jarvis now uses a cleaner React-first architecture with `useJarvisApp` as the unified app hook.

---

## What Was Accomplished

### Phase 1: DOM Manipulation Files (Already Done)

These DOM manipulation files were deleted before this cleanup:

- ~~`main.ts`~~ - legacy entry point
- ~~`conversation-renderer.ts`~~ - direct DOM manipulation
- ~~`conversation-ui.ts`~~ - direct DOM manipulation
- ~~`ui-controller.ts`~~ - direct DOM manipulation
- ~~`ui-enhancements.ts`~~ - direct DOM manipulation
- ~~`radial-visualizer.ts`~~ - direct DOM manipulation
- ~~`VITE_JARVIS_ENABLE_REALTIME_BRIDGE`~~ - feature flag removed from production

### Phase 2: React-First Architecture (December 2024)

#### Created

- **`src/hooks/useJarvisApp.ts`** - Unified hook for app lifecycle
  - Handles initialization (JarvisClient, bootstrap, context)
  - Manages voice connection (mic, session, voice controller)
  - Dispatches directly to React context
  - Listens to stateManager for streaming events

#### Deleted (~1,400 lines of code)

| File                                    | Lines | Why                        |
| --------------------------------------- | ----- | -------------------------- |
| `lib/app-controller.ts`                 | ~520  | Replaced by `useJarvisApp` |
| `lib/task-inbox.ts`                     | ~200  | Unused feature             |
| `lib/task-inbox-integration-example.ts` | ~80   | Example code               |
| `lib/test-helpers.ts`                   | ~260  | Orphaned test utilities    |
| `src/hooks/useRealtimeSession.ts`       | ~360  | Replaced by `useJarvisApp` |

#### Deleted Tests (~800 lines)

| Test                                       | Why                     |
| ------------------------------------------ | ----------------------- |
| `callback-deduplication.test.ts`           | Tested old hook         |
| `text-channel-persistence.test.ts`         | Tested old architecture |
| `server-history-ssot.integration.test.tsx` | Tested old architecture |
| `react-integration.test.tsx`               | Tested old architecture |
| `one-brain-architecture.test.ts`           | Tested old architecture |

#### Updated

- **`useTextChannel.ts`** - Uses `SupervisorChatController` directly (not `appController`)
- **`usePreferences.ts`** - Simplified, reads from React context
- **`App.tsx`** - Uses `useJarvisApp` instead of `useRealtimeSession`
- **`hooks/index.ts`** - Updated exports

#### Type Fixes

- Fixed `SSESupervisorEvent` to include worker event fields
- Made `toolExecution` optional in `apiEndpoints`
- Added `emit` alias to `StateManager`
- Removed deprecated no-op methods from `conversation-controller.ts`

---

## Current Architecture

```
User Action
    │
    ▼
React Component (App.tsx)
    │
    ▼
useJarvisApp (unified hook)
    │
    ├─► voiceController        ◄─► OpenAI Realtime (voice I/O)
    │   (voice state machine)
    │
    └─► supervisorChatController ─► Backend SSE
            │
            ▼
        stateManager (event bus)
            │
            ▼
        useJarvisApp (listener)
            │
            ▼
        dispatch() to React Context
            │
            ▼
        UI Renders
```

**Key insight:** The streaming pipeline still uses `stateManager` as an event bus because `supervisorChatController` emits events through it. A future refactor could have the controller accept callbacks directly.

---

## Files Kept (Still Needed)

| File                                | Why                                |
| ----------------------------------- | ---------------------------------- |
| `lib/state-manager.ts`              | Event bus for SSE streaming events |
| `lib/conversation-controller.ts`    | Streaming text accumulation        |
| `lib/supervisor-chat-controller.ts` | Backend SSE communication          |
| `lib/voice-controller.ts`           | Voice state machine                |
| `lib/audio-controller.ts`           | Mic/speaker management             |
| `lib/session-handler.ts`            | OpenAI session lifecycle           |
| `lib/session-bootstrap.ts`          | Session setup                      |
| `lib/feedback-system.ts`            | Audio chimes                       |
| `lib/event-bus.ts`                  | Internal event infrastructure      |
| `lib/config.ts`                     | Configuration                      |
| `lib/uuid.ts`                       | UUID generation                    |
| `lib/history-mapper.ts`             | History format conversion          |
| `lib/markdown-renderer.ts`          | Message rendering                  |

---

## Remaining Technical Debt

### Medium Priority

1. **Refactor `supervisor-chat-controller` to use callbacks**
   - Currently emits to `stateManager`
   - Could accept `onStreamingText(text)`, `onMessageFinalized(msg)` callbacks
   - Would allow deleting `state-manager.ts` and `conversation-controller.ts`

2. **Move types to shared file**
   - `ChatPreferences`, `ModelInfo`, `BootstrapData` are defined in multiple places
   - Should consolidate into `src/types.ts` or similar

### Low Priority

1. **Fix vitest hoisting issues**
   - Some tests fail due to `vi.mock` not being hoisted correctly
   - Pre-existing infrastructure issue

2. **Consolidate context/config.ts dependency**
   - Still imports from `stateManager.getBootstrap()`
   - Should get bootstrap from React context

---

## Stats

| Metric        | Value                                    |
| ------------- | ---------------------------------------- |
| Lines deleted | ~2,200                                   |
| Files deleted | 11                                       |
| Files created | 1                                        |
| TypeScript    | ✅ Passes                                |
| Tests         | 77 pass (vitest issues are pre-existing) |

---

## Verification

```bash
# TypeScript compiles clean
cd apps/jarvis/apps/web && bunx tsc --noEmit

# Tests run (some pre-existing vitest issues)
cd apps/jarvis && bun test

# App builds
cd apps/jarvis/apps/web && bun run build
```

---

## What's Different Now

### Before (Bridge Mode)

```
User types message
  ↓
useTextChannel calls appController.sendText()
  ↓
appController calls supervisorChatController
  ↓
SSE events → stateManager → useRealtimeSession listens
  ↓
useRealtimeSession dispatches to React
  ↓
UI renders
```

### After (React-First)

```
User types message
  ↓
useTextChannel calls supervisorChatController directly
  ↓
SSE events → stateManager (kept as event bus)
  ↓
useJarvisApp listens and dispatches directly
  ↓
UI renders
```

**The key difference:** `appController` orchestration layer removed. Hooks now talk directly to controllers.

---

## Future Refactor: Callback-Based Architecture

To fully eliminate the stateManager bridge, refactor `supervisor-chat-controller`:

```typescript
// Current (event-based)
class SupervisorChatController {
  async sendMessage(text: string) {
    // ... on SSE event ...
    stateManager.setStreamingText(text); // Indirect
  }
}

// Future (callback-based)
class SupervisorChatController {
  async sendMessage(
    text: string,
    callbacks: {
      onStreamingText: (text: string) => void;
      onMessageFinalized: (msg: ChatMessage) => void;
      onError: (error: Error) => void;
    },
  ) {
    // ... on SSE event ...
    callbacks.onStreamingText(text); // Direct
  }
}
```

This would allow:

- Deleting `state-manager.ts`
- Deleting `conversation-controller.ts`
- Pure React data flow with no event bus

**Estimated effort:** 2-4 hours
**Risk:** Low (well-tested SSE handler)
