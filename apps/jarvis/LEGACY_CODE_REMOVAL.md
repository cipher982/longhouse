# Jarvis Legacy Code Removal - Complete React Migration

## Problem Statement

The Jarvis React migration (completed December 2024) created a **dual-system architecture** where legacy vanilla TypeScript controllers coexist with React components. This "bridge mode" was intended as a transitional approach but has proven too complex:

**Symptoms:**

- AI streaming responses play as voice but don't render as text bubbles in React UI
- Controllers update legacy DOM (`#transcript`, `ConversationRenderer`) but not React state
- Two parallel state systems conflict and create bugs
- `VITE_JARVIS_ENABLE_REALTIME_BRIDGE` feature flag adds complexity
- Code duplication between `lib/` (legacy) and `src/` (React)

**Root Cause:**
The bridge mode tries to maintain backward compatibility with the legacy `main.ts` entry point by having controllers support both:

1. Legacy: Direct DOM manipulation via `ConversationRenderer`
2. React: State updates via `stateManager` → event listeners → React dispatch

This creates a mess where streaming works in one system but not the other.

**Solution:**
Remove ALL legacy code. Make Jarvis a pure React application. Controllers should only update React state, never touch the DOM directly.

---

## Current Architecture (Broken)

```
User Action
  ↓
appController (lib/)
  ↓
conversationController.appendStreaming()
  ├→ Updates this.renderer (legacy DOM) ✓ works
  └→ Updates stateManager ✗ broken / incomplete
      ↓
useRealtimeSession (React hook)
  ↓
dispatch({ type: 'SET_STREAMING_CONTENT' })
  ↓
ChatContainer never re-renders ✗
```

---

## Files to Remove (Legacy System)

### Entry Points

- ❌ `apps/jarvis/apps/web/main.ts` - Legacy vanilla TS entry point (310 lines)
- ❌ `apps/jarvis/apps/web/index.html` - Legacy HTML with `#transcript` (if separate from React version)

### DOM Manipulation

- ❌ `lib/conversation-renderer.ts` - Direct DOM manipulation for `#transcript` element
- ❌ `lib/conversation-ui.ts` - Sidebar/conversation list DOM manipulation
- ❌ `lib/ui-controller.ts` - Voice button state via direct className changes
- ❌ `lib/ui-enhancements.ts` - Toast/loading overlays via DOM injection
- ❌ `lib/radial-visualizer.ts` - Canvas-based audio visualizer

### Audio Feedback (if DOM-dependent)

- ⚠️ `lib/feedback-system.ts` - Check if it does DOM manipulation or just plays sounds
  - Keep if it's just `new Audio().play()`
  - Remove if it manipulates DOM for visual feedback

### State/Event Infrastructure (Review)

- ⚠️ `lib/state-manager.ts` - Currently bridges legacy + React, needs refactor
- ⚠️ `lib/event-bus.ts` - Check if needed or can use React Context

---

## Files to Refactor (Keep but Simplify)

### Controllers (Remove DOM Logic, Keep Business Logic)

#### `lib/app-controller.ts`

**Keep:**

- Connection orchestration (`connect()`, `disconnect()`)
- JarvisClient initialization
- Context loading
- Session setup

**Remove:**

- All `uiController` calls (replace with React state updates)
- All `uiEnhancements` calls (replace with React toast component)
- `conversationController` references (refactor to use React state)

**Change:**

- Return connection state instead of updating UI directly
- Let React components call controller methods and handle UI updates

#### `lib/conversation-controller.ts`

**Current problem:**

```typescript
appendStreaming(delta: string): void {
  this.state.streamingText += delta;
  this.renderer.updateMessage(...) // ← DOM manipulation
  stateManager.setStreamingText(...) // ← React state (added but incomplete)
}
```

**Refactor to:**

```typescript
appendStreaming(delta: string): void {
  this.state.streamingText += delta;
  // ONLY update React state - no renderer
  stateManager.setStreamingText(this.state.streamingText);
}
```

**Remove:**

- `setRenderer()` method
- All `this.renderer` calls
- `addUserTurn()`, `addAssistantTurn()` - React handles UI
- Keep: `recordTurn()` for IndexedDB persistence

#### `lib/voice-controller.ts`

**Keep as-is** - Already clean, only manages voice state

#### `lib/audio-controller.ts`

**Review:**

- Keep mic/speaker management
- Remove `RadialVisualizer` dependency (DOM-based canvas)
- Audio monitoring can stay

#### `lib/session-handler.ts`

**Keep as-is** - Session lifecycle management is good

#### `lib/text-channel-controller.ts`

**Keep but simplify** - Remove UI update logic, just manage sending

---

## New React-Only Architecture

```
User Action (TextInput component)
  ↓
appController.sendText()
  ↓
OpenAI API (via session)
  ↓
appController.setupSessionEvents()
  ↓ (on transport_event)
conversationController.appendStreaming()
  ↓
stateManager.setStreamingText()
  ↓
stateManager.notifyListeners({ type: 'STREAMING_TEXT_CHANGED' })
  ↓
useRealtimeSession hook (subscribed to stateManager)
  ↓
dispatch({ type: 'SET_STREAMING_CONTENT', content: text })
  ↓
AppContext reducer updates state.streamingContent
  ↓
ChatContainer re-renders with streaming text ✓
```

---

## Implementation Plan

### Step 1: Create New React State Management Hook

Create `src/hooks/useConversationController.ts`:

```typescript
/**
 * Pure React hook for conversation management
 * Replaces lib/conversation-controller.ts DOM manipulation
 */
export function useConversationController() {
  const dispatch = useAppDispatch();
  const { sessionManager } = useAppState();

  const appendStreaming = useCallback(
    (delta: string) => {
      // Just update React state - no DOM
      dispatch({ type: "APPEND_STREAMING", delta });
    },
    [dispatch],
  );

  const finalizeStreaming = useCallback(
    async (text: string) => {
      const message = {
        id: crypto.randomUUID(),
        role: "assistant",
        content: text,
        timestamp: new Date(),
      };

      // Add to React state
      dispatch({ type: "ADD_MESSAGE", message });
      dispatch({ type: "SET_STREAMING_CONTENT", content: "" });

      // Persist to IndexedDB
      if (sessionManager) {
        await sessionManager.addConversationTurn({
          id: message.id,
          timestamp: message.timestamp,
          assistantResponse: text,
        });
      }
    },
    [dispatch, sessionManager],
  );

  return { appendStreaming, finalizeStreaming };
}
```

### Step 2: Refactor AppController

Remove all DOM dependencies:

- Remove `uiController`, `uiEnhancements`, `conversationController`, `feedbackSystem` imports
- Remove `conversationController.appendStreaming()` calls
- Instead, emit events that React hooks subscribe to

**Option A: Keep controllers, make them state-only**

```typescript
// app-controller.ts becomes pure state machine
class AppController {
  async connect() {
    // ... connection logic ...
    // Emit events, don't update DOM
    this.emit({ type: "connected", session });
  }
}

// React hook subscribes
useEffect(() => {
  const handler = (event) => {
    if (event.type === "connected") {
      dispatch({ type: "SET_CONNECTED", connected: true });
    }
  };
  appController.addListener(handler);
}, []);
```

**Option B: Move logic into React hooks entirely**

```typescript
// src/hooks/useRealtimeConnection.ts
export function useRealtimeConnection() {
  const [session, setSession] = useState(null);

  const connect = useCallback(async () => {
    const mic = await navigator.mediaDevices.getUserMedia({ audio: true });
    const session = await createRealtimeSession({ ... });
    setSession(session);
    // All logic in React hooks
  }, []);

  return { session, connect, disconnect };
}
```

**Recommendation: Option B** - Pure React is simpler than maintaining controller layer

### Step 3: Remove Files

Delete these files entirely:

```bash
rm apps/jarvis/apps/web/main.ts
rm apps/jarvis/apps/web/lib/conversation-renderer.ts
rm apps/jarvis/apps/web/lib/conversation-ui.ts
rm apps/jarvis/apps/web/lib/ui-controller.ts
rm apps/jarvis/apps/web/lib/ui-enhancements.ts
rm apps/jarvis/apps/web/lib/radial-visualizer.ts

# Consider removing:
rm apps/jarvis/apps/web/lib/conversation-controller.ts  # Logic moves to React hook
rm apps/jarvis/apps/web/lib/app-controller.ts  # Logic moves to React hooks
```

### Step 4: Simplify State Management

Current: `lib/state-manager.ts` + `src/context/AppContext.tsx` (two state systems)

Refactor to: **React Context only**

- Move all state to `AppContext`
- Remove `stateManager` singleton
- Controllers emit events, don't manage state

### Step 5: Remove Bridge Mode Flag

Delete everywhere:

```bash
grep -r "VITE_JARVIS_ENABLE_REALTIME_BRIDGE" apps/jarvis/
```

- Remove from `App.tsx` conditional logic
- Remove from `.env.example`
- Remove from docker-compose files
- Remove from test setup
- React is now the ONLY mode

### Step 6: Update Tests

- Remove bridge mode conditionals from tests
- Update tests to use pure React patterns
- No more `window.voiceController` - test via UI interactions only

---

## Files That Are Good (Keep)

These follow React patterns and don't need changes:

- ✅ `src/components/ChatContainer.tsx`
- ✅ `src/components/TextInput.tsx`
- ✅ `src/components/VoiceControls.tsx`
- ✅ `src/components/Header.tsx`
- ✅ `src/components/Sidebar.tsx`
- ✅ `src/context/AppContext.tsx`
- ✅ `src/hooks/useTextChannel.ts`
- ✅ `lib/voice-controller.ts` (state-only, no DOM)
- ✅ `lib/session-handler.ts` (OpenAI session lifecycle)

---

## Refactoring Pattern

**Before (Bridge Mode):**

```typescript
// Controller updates both DOM and React
conversationController.appendStreaming(delta);
// → Updates DOM via renderer
// → Updates stateManager
// → useRealtimeSession listens
// → Dispatches to React
// → ChatContainer *might* render

// Text send goes through controller
appController.sendText(text);
// → textChannelController.sendText()
// → Lots of complexity
```

**After (React Only):**

```typescript
// Streaming: session event → React hook → context → UI
session.on("transport_event", (event) => {
  if (event.type === "response.output_text.delta") {
    dispatch({ type: "APPEND_STREAMING", delta: event.delta });
  }
});

// Text send: Component → hook → API
const { sendMessage } = useTextChannel();
sendMessage(text); // Returns promise, updates state directly
```

---

## Testing Strategy

### Tests That Need Updating

1. **`bridge-mode.e2e.test.tsx`**
   - Rename to `jarvis.e2e.test.tsx` (no more "bridge mode")
   - Remove `BRIDGE_ENABLED` conditional
   - Update to test React UI directly

2. **`text-message-happy-path.e2e.spec.ts`**
   - Should work as-is once React is properly wired

3. **Unit tests for controllers**
   - Remove tests for DOM manipulation methods
   - Test state management only

### New Tests Needed

- `useRealtimeConnection.test.ts` - Test the React hook directly
- Integration test for streaming text flow (React TestingLibrary)

---

## Migration Checklist

- [ ] Document current working features (voice, text, streaming)
- [ ] Create `src/hooks/useRealtimeConnection.ts` (replace app-controller)
- [ ] Create `src/hooks/useConversation.ts` (replace conversation-controller)
- [ ] Move session setup logic from controllers to hooks
- [ ] Update `App.tsx` to use new hooks
- [ ] Remove `VITE_JARVIS_ENABLE_REALTIME_BRIDGE` flag everywhere
- [ ] Delete legacy files (main.ts, renderers, ui-controllers)
- [ ] Update tests to remove bridge mode conditionals
- [ ] Run test suite - ensure `text-message-happy-path.e2e` passes
- [ ] Manual verification: send message, see response bubble

---

## Expected Outcome

**Before:**

- 310 lines in `main.ts` (legacy entry point)
- ~15 controller files with mixed DOM/React logic
- Bridge mode flag adds complexity
- Streaming responses don't render

**After:**

- Single React entry point (`src/main.tsx`)
- ~8 hook files with pure React patterns
- No feature flags
- Clean data flow: session events → hooks → context → components
- Streaming responses render correctly

**LOC Reduction:** ~1,500 lines of legacy code removed
**Complexity:** Bridge mode eliminated, single system
**Bugs Fixed:** Streaming responses render, no more dual-state confusion

---

## Risk Mitigation

### Backup Current State

```bash
git checkout -b legacy-removal-backup
git push origin legacy-removal-backup
```

### Incremental Approach

Don't delete everything at once:

1. **Commit 1:** Create new React hooks alongside legacy
2. **Commit 2:** Update App.tsx to use new hooks, keep legacy imported
3. **Commit 3:** Remove bridge mode flag
4. **Commit 4:** Delete unused legacy files
5. **Commit 5:** Clean up remaining references

### Rollback Plan

If something breaks:

```bash
git revert HEAD~5..HEAD  # Revert last 5 commits
# Or
git reset --hard legacy-removal-backup
```

---

## Reference: What Was Discovered

**Bug 1:** `voiceController` was `undefined`

- **Why:** Declared as `export let voiceController` but never initialized
- **Fix:** Changed to `export const voiceController = new VoiceController({})`
- **Commit:** `c4b6f30`

**Bug 2:** "No active context loaded"

- **Why:** React path never called `contextLoader.loadContext()`
- **Fix:** Added `initializeContext()` to `AppController.initialize()`
- **Commit:** `c4b6f30`

**Bug 3:** Streaming responses don't render in React UI

- **Why:** `conversationController.appendStreaming()` updates DOM but not React messages
- **Status:** Attempted fix with custom events, but too hacky - needs proper refactor
- **Proper fix:** Remove DOM manipulation entirely

---

## Success Criteria

After legacy removal:

- ✅ `bun test` - all tests pass
- ✅ `make dev` - app starts without errors
- ✅ Load http://localhost:30080/chat - React UI appears
- ✅ Send text message - message bubble appears immediately
- ✅ AI response - streaming text appears in real-time
- ✅ AI response - final message persists after streaming stops
- ✅ Voice mode - PTT button works
- ✅ Conversation history - persists across page refresh
- ✅ E2E test passes: `docker compose -f docker-compose.test.yml run --rm playwright`

---

## Additional Context

**Recent Commits:**

- `8c0ef97` - immersive sci-fi ui upgrade
- `e0d4021` - bridge mode e2e smoke tests
- `92e5b7f` - disable PTT when not connected, add connection states
- `c4b6f30` - (WIP) bridge mode bug fixes and e2e test infrastructure

**Related Docs:**

- `apps/jarvis/MIGRATION.md` - Original React migration plan
- `PHASE_2_READY.md` - Shared packages plan (fix models.json permanently)
- `apps/jarvis/apps/web/tests/E2E.md` - E2E testing infrastructure
- `apps/jarvis/apps/web/lib/README.md` - Legacy controllers documentation

**Technologies:**

- React 19 (PWA)
- OpenAI Realtime API
- IndexedDB for persistence (@jarvis/data-local)
- Bun workspace

---

## Questions for Implementation

1. **Keep state-manager.ts?**
   - Option A: Keep as thin event emitter for session events
   - Option B: Remove, use React Context for everything

2. **Keep lib/ directory at all?**
   - Option A: Keep for session-handler, voice-controller (non-UI logic)
   - Option B: Move everything to src/hooks/, flatten structure

3. **Audio feedback?**
   - Current: `feedback-system.ts` plays chimes via `new Audio()`
   - Keep or move to React hook?

4. **Service worker integration?**
   - Currently in `src/main.tsx` (good)
   - Ensure it stays after refactor

---

## Recommended Implementation Approach

**Phase 1: Create Pure React Hooks** (1-2 hours)

- `src/hooks/useRealtimeConnection.ts` - Replace app-controller
- `src/hooks/useStreamingResponse.ts` - Replace conversation-controller
- `src/hooks/useAudioFeedback.ts` - Replace feedback-system

**Phase 2: Update App.tsx** (30 mins)

- Remove bridge mode conditional
- Use new hooks
- Verify it compiles

**Phase 3: Delete Legacy Files** (15 mins)

- Run `rm` commands above
- Fix TypeScript errors

**Phase 4: Test** (30 mins)

- Run unit tests
- Run e2e tests
- Manual smoke test

**Total: 3-4 hours**

---

## Point of Origin Analysis (Per Engineering Protocol)

**Where did the complexity originate?**

The React migration (commits `b0f851e` through `c7427be`) was done incrementally to avoid breaking the working app. The decision was made to:

1. Keep legacy `main.ts` working
2. Add React alongside it
3. Use feature flag to toggle between modes

This was a pragmatic choice for zero-downtime migration, but created technical debt.

**The fundamental mistake:** Trying to support both systems simultaneously instead of committing to React-only from the start.

**Lesson:** For future migrations, prefer:

- Feature branch with breaking changes
- Full cutover in one commit
- Accept short downtime over long-term complexity

---

## Contact / Questions

If you encounter issues during implementation:

- Check `apps/jarvis/MIGRATION.md` for historical context
- Review commit `c4b6f30` for bug fixes attempted
- The e2e test `text-message-happy-path.e2e.spec.ts` documents expected behavior
- Streaming response rendering is the critical path to get working

Good luck! This refactor will make Jarvis much cleaner and more maintainable.
