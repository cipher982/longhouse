# Single Source of Truth (SSOT) History Refactor

**Status**: Implementation Plan
**Priority**: High
**Estimated Scope**: 5 files modified, ~200 lines changed

---

## Problem Statement

The current architecture has multiple data flows for conversation history that can diverge, causing:

1. UI showing different messages than what the AI model "remembers"
2. Text messages lost on reload (never persisted)
3. Conversation switching not actually switching the data source
4. Spanish responses when English expected (history hydration diverged from cleared UI)

---

## Engineering Concepts Applied

### 1. Single Source of Truth (SSOT)

Every piece of data lives in exactly one place. All representations derive from it.

**Current (broken)**:

```
IndexedDB ──query──> UI (App.tsx effect)
IndexedDB ──query──> Realtime hydration (sessionHandler)
         ↑ Two separate queries that can diverge
```

**Target**:

```
IndexedDB ──single query──> Bootstrap Result ──> UI
                                             ──> Realtime hydration
                           ↑ Same data guaranteed
```

### 2. Unidirectional Data Flow

Data flows one direction: Source → Transform → Consumers

### 3. Read-Your-Writes Consistency

After a write (clear, new message), subsequent reads reflect that write immediately.

### 4. Observable State

Users can verify what the model "knows" by looking at the UI.

---

## Findings Summary

| Severity   | Issue                                      | File(s)                         |
| ---------- | ------------------------------------------ | ------------------------------- |
| **HIGH**   | Text messages never persisted to IndexedDB | `useTextChannel.ts`             |
| **HIGH**   | History fetched twice (SSOT violation)     | `App.tsx`, `session-handler.ts` |
| **HIGH**   | Conversation switching broken              | `App.tsx`                       |
| **MEDIUM** | History hydration one-shot per page        | `App.tsx`                       |
| **MEDIUM** | Two state systems not aligned              | `App.tsx`, `state-manager.ts`   |

---

## Implementation Plan

### Phase 1: Single Bootstrap Function

**Goal**: Load history once, return to all consumers.

#### Step 1.1: Create bootstrap module

Create new file: `apps/jarvis/apps/web/lib/session-bootstrap.ts`

```typescript
/**
 * Session Bootstrap
 * Single source of truth for session initialization.
 * Loads data once and provides to all consumers.
 */

import { logger, type SessionManager } from "@jarvis/core";
import type { ConversationTurn } from "@jarvis/data-local";
import { sessionHandler } from "./session-handler";
import {
  mapConversationToRealtimeItems,
  trimForRealtime,
} from "./history-mapper";
import type { VoiceAgentConfig } from "../contexts/types";

export interface BootstrapResult {
  session: any; // RealtimeSession
  agent: any; // RealtimeAgent
  conversationId: string | null;
  history: ConversationTurn[];
  hydratedItemCount: number;
}

export interface BootstrapOptions {
  context: VoiceAgentConfig;
  sessionManager: SessionManager;
  mediaStream?: MediaStream;
  tools?: any[];
  onTokenRequest: () => Promise<string>;
  realtimeHistoryTurns?: number;
}

/**
 * Bootstrap a session with SSOT history.
 * Returns the same history data for both UI and Realtime hydration.
 */
export async function bootstrapSession(
  options: BootstrapOptions,
): Promise<BootstrapResult> {
  const { sessionManager, realtimeHistoryTurns = 8 } = options;

  // 1. Get conversation ID (SSOT)
  const conversationId = await sessionManager
    .getConversationManager()
    .getCurrentConversationId();

  // 2. Load history ONCE (SSOT)
  const fullHistory = await sessionManager.getConversationHistory();
  logger.info(
    `📚 Bootstrap: loaded ${fullHistory.length} turns from IndexedDB`,
  );

  // 3. Prepare history for Realtime (subset)
  const realtimeHistory = trimForRealtime(fullHistory, realtimeHistoryTurns);
  const realtimeItems = mapConversationToRealtimeItems(realtimeHistory);

  // 4. Connect session with explicit history (not re-queried)
  const { session, agent } = await sessionHandler.connectWithHistory({
    context: options.context,
    mediaStream: options.mediaStream,
    tools: options.tools,
    onTokenRequest: options.onTokenRequest,
    historyItems: realtimeItems, // Pass explicitly, don't re-query
  });

  logger.info(
    `📜 Bootstrap: hydrated ${realtimeItems.length} items into Realtime`,
  );

  return {
    session,
    agent,
    conversationId,
    history: fullHistory, // Full history for UI
    hydratedItemCount: realtimeItems.length,
  };
}
```

#### Step 1.2: Modify session-handler.ts

Add new method that accepts history instead of querying:

**File**: `apps/jarvis/apps/web/lib/session-handler.ts`

```typescript
// Add new interface
export interface SessionConnectionWithHistoryOptions extends SessionConnectionOptions {
  historyItems: RealtimeMessageItem[];  // Pre-loaded, don't re-query
}

// Add new method (keep old one for backwards compat temporarily)
async connectWithHistory(options: SessionConnectionWithHistoryOptions): Promise<{ session: RealtimeSession; agent: RealtimeAgent }> {
  // ... same setup as connect() ...

  // INSTEAD OF:
  // const turns = await this.config.getConversationHistory();
  // const items = mapConversationToRealtimeItems(turns);

  // USE:
  const items = options.historyItems;  // Already mapped, passed in

  if (items.length > 0) {
    session.updateHistory(items);
    logger.info(`📜 Hydrated ${items.length} history items into Realtime session`);
  }

  // ... rest of connect() ...
}
```

#### Step 1.3: Update App.tsx to consume bootstrap result

**File**: `apps/jarvis/apps/web/src/App.tsx`

Remove the separate history loading effect. Instead, receive history from bootstrap:

```typescript
// REMOVE this effect:
useEffect(() => {
  if (!isInitialized || historyLoadedRef.current) return
  const loadHistory = async () => { ... }
  loadHistory()
}, [isInitialized, dispatch, state.messages.length])

// INSTEAD: App receives history from useRealtimeSession hook
const realtimeSession = useRealtimeSession({
  onConnected: (bootstrapResult) => {
    console.log('[App] Session connected with history:', bootstrapResult.history.length)
    setIsInitialized(true)

    // Seed UI from the SAME data used for hydration
    if (bootstrapResult.history.length > 0) {
      const messages = bootstrapResult.history.map(turnToMessage)
      dispatch({ type: 'SET_MESSAGES', messages })
    }
    dispatch({ type: 'SET_CONVERSATION_ID', id: bootstrapResult.conversationId })
  },
  // ...
})
```

---

### Phase 2: Persist Text Messages

**Goal**: Text channel writes to IndexedDB like voice does.

#### Step 2.1: Update useTextChannel.ts

**File**: `apps/jarvis/apps/web/src/hooks/useTextChannel.ts`

```typescript
// Current (broken): only updates React state
const sendMessage = useCallback(
  async (content: string) => {
    const userMessage = { id, role: "user", content, timestamp };
    dispatch({ type: "ADD_MESSAGE", message: userMessage }); // UI only!
    await appController.sendText(content);
    // ... response handling
  },
  [dispatch],
);

// Fixed: persist to IndexedDB too
const sendMessage = useCallback(
  async (content: string) => {
    const userMessage = { id, role: "user", content, timestamp };
    dispatch({ type: "ADD_MESSAGE", message: userMessage });

    // ADD: Persist user message (same as voice path)
    await conversationController.addUserTurn(content);

    await appController.sendText(content);

    // Response handling - also persist assistant response
    // ... when response complete:
    await conversationController.addAssistantTurn(assistantResponse);
  },
  [dispatch],
);
```

#### Step 2.2: Create shared persistence helper (optional but cleaner)

**File**: `apps/jarvis/apps/web/lib/message-persistence.ts`

```typescript
/**
 * Unified message persistence for both voice and text channels.
 */
export async function persistUserMessage(content: string): Promise<void> {
  await conversationController.addUserTurn(content);
}

export async function persistAssistantMessage(content: string): Promise<void> {
  await conversationController.addAssistantTurn(content);
}
```

---

### Phase 3: Fix Conversation Switching

**Goal**: Selecting a conversation actually switches the data source.

#### Step 3.1: Update handleSelectConversation in App.tsx

**File**: `apps/jarvis/apps/web/src/App.tsx`

```typescript
// Current (broken): only updates React state
const handleSelectConversation = useCallback(
  (id: string) => {
    console.log("[App] Select conversation:", id);
    dispatch({ type: "SET_CONVERSATION_ID", id }); // React only!
  },
  [dispatch],
);

// Fixed: update persistence layer AND reconnect session
const handleSelectConversation = useCallback(
  async (id: string) => {
    console.log("[App] Switching to conversation:", id);

    const sessionManager = stateManager.getState().sessionManager;
    if (!sessionManager) {
      console.warn("[App] Cannot switch - sessionManager not ready");
      return;
    }

    // 1. Switch persistence layer
    await sessionManager.switchToConversation(id);
    conversationController.setConversationId(id);
    stateManager.setConversationId(id);

    // 2. Update React state
    dispatch({ type: "SET_CONVERSATION_ID", id });

    // 3. Load this conversation's history
    const history = await sessionManager.getConversationHistory();
    const messages = history.map(turnToMessage);
    dispatch({ type: "SET_MESSAGES", messages });

    // 4. Reconnect session to hydrate new history
    console.log("[App] Reconnecting session for new conversation context...");
    await realtimeSession.reconnect();
  },
  [dispatch, realtimeSession],
);
```

#### Step 3.2: Fix handleNewConversation

**File**: `apps/jarvis/apps/web/src/App.tsx`

```typescript
// Current (broken): creates conversation but then sets ID to null
const handleNewConversation = useCallback(async () => {
  // ...
  const newId = await sessionManager.createNewConversation();
  // ...
  dispatch({ type: "SET_CONVERSATION_ID", id: null }); // BUG: should be newId!
}, [dispatch, textChannel]);

// Fixed:
const handleNewConversation = useCallback(async () => {
  console.log("[App] Creating new conversation");
  const sessionManager = stateManager.getState().sessionManager;
  if (!sessionManager) {
    console.warn("[App] Cannot create - sessionManager not ready");
    return;
  }

  // 1. Create new conversation
  const newId = await sessionManager.createNewConversation();

  // 2. Update all layers with the new ID
  conversationController.setConversationId(newId);
  stateManager.setConversationId(newId);

  // 3. Clear UI and set new ID
  textChannel.clearMessages();
  dispatch({ type: "SET_MESSAGES", messages: [] });
  dispatch({ type: "SET_CONVERSATION_ID", id: newId }); // Fixed!

  // 4. Reconnect session with empty history
  await realtimeSession.reconnect();

  console.log("[App] New conversation created:", newId);
}, [dispatch, textChannel, realtimeSession]);
```

---

### Phase 4: Reset History Guard on Context Change

**Goal**: Allow history to reload when conversation changes.

#### Step 4.1: Remove historyLoadedRef or make it conversation-aware

If keeping the separate effect (not recommended after Phase 1), make it reset:

```typescript
// Track which conversation was loaded
const loadedConversationRef = useRef<string | null>(null)

useEffect(() => {
  // Reset if conversation changed
  if (state.conversationId !== loadedConversationRef.current) {
    loadedConversationRef.current = null
  }

  if (loadedConversationRef.current) return

  // ... load history ...
  loadedConversationRef.current = state.conversationId
}, [state.conversationId, ...])
```

**Better**: After Phase 1, this effect is deleted entirely because bootstrap handles it.

---

### Phase 5: Align State Systems (Optional, Lower Priority)

**Goal**: Single source of truth for messages.

This is more invasive. Options:

1. **Keep both, ensure sync**: stateManager drives persistence, React drives UI, they coordinate via events (current approach, just fix the bugs)

2. **React as SSOT**: Remove message state from stateManager, React context is authoritative

3. **stateManager as SSOT**: React subscribes to stateManager, doesn't maintain separate message array

Recommendation: Fix bugs first (Phases 1-4), then evaluate if consolidation is needed based on pain points.

---

## File Change Summary

| File                              | Changes                             |
| --------------------------------- | ----------------------------------- |
| `lib/session-bootstrap.ts`        | **NEW** - Bootstrap function        |
| `lib/session-handler.ts`          | Add `connectWithHistory()` method   |
| `src/App.tsx`                     | Remove history effect, fix handlers |
| `src/hooks/useTextChannel.ts`     | Add persistence calls               |
| `src/hooks/useRealtimeSession.ts` | Use bootstrap, expose result        |

---

## Testing Checklist

### Manual Tests

- [ ] Say something via voice → refresh → history appears in UI AND model remembers
- [ ] Type something via text → refresh → history appears in UI AND model remembers
- [ ] Click "Clear all" → model responds fresh (no old context)
- [ ] Create new conversation → model has no history from previous
- [ ] Switch conversations → UI shows that conversation's history, model context matches

### Automated Tests (to add)

- [ ] `session-bootstrap.test.ts`: Bootstrap returns same data to UI and hydration
- [ ] `useTextChannel.test.ts`: Text messages persist to IndexedDB
- [ ] `conversation-switching.test.ts`: Switching updates all layers
- [ ] `clear-conversations.test.ts`: Clear actually clears IndexedDB + session context

---

## Success Criteria

1. **Observable**: User can see exactly what the model knows by looking at the UI
2. **Consistent**: Refresh preserves conversation state for both voice and text
3. **Isolated**: Each conversation has its own history, switching works
4. **Clearable**: "Clear all" results in fresh model context immediately

---

## Implementation Order

```
Phase 1 (SSOT Bootstrap)     ─── Most impactful, fixes root cause
    ↓
Phase 2 (Text Persistence)   ─── Quick fix, high value
    ↓
Phase 3 (Conversation Switch) ─── Makes multi-conversation work
    ↓
Phase 4 (History Guard)      ─── Cleanup after Phase 1
    ↓
Phase 5 (State Alignment)    ─── Optional, evaluate later
```

Start with Phase 1 + 2 together, then Phase 3. Phases 4-5 are cleanup.

---

## Questions for Review

1. Should `reconnect()` after conversation switch be automatic or user-triggered?
2. Do we want a visual indicator showing "Model context: N messages"?
3. Should text channel show a "saving..." state while persisting?
