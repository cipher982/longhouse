# Jarvis Refactoring Master Plan
## Complete Carmack Simplification

**Date Started:** November 18, 2025
**Goal:** Reduce from 2,910 LOC to ~1,800 LOC with cleaner architecture
**Status:** In Progress

---

## Current State (Baseline)

### File Structure
```
main.ts:                     2,009 lines
lib/voice-controller.ts:       638 lines
lib/text-channel-controller:   263 lines
lib/state-manager.ts:          279 lines
lib/session-handler.ts:        195 lines
lib/feedback-system.ts:        283 lines
lib/conversation-ui.ts:        245 lines
lib/radial-visualizer.ts:      317 lines
lib/ui-controller.ts:          260 lines
──────────────────────────────────────
TOTAL:                       ~4,500 lines (core logic ~2,910)
```

### Test Coverage
- **73 tests** across 7 files (down from 98)
- Lost 25 tests in consolidation (edge cases not ported)

### Architecture
- ✅ VoiceController consolidated (was 3 modules)
- ✅ No circular dependencies
- ✅ Single source of truth for state
- ❌ Event-bus still everywhere (compatibility layer)
- ❌ main.ts still massive (2,009 lines)
- ❌ Conversation logic not extracted

---

## Target Architecture

### File Structure (Goal)
```
main.ts:                       800 lines  (orchestration only)
lib/voice-controller.ts:       350 lines  (remove compat layer)
lib/conversation-controller:   400 lines  (NEW - extracted)
lib/text-channel-controller:   200 lines  (simplified)
lib/state.ts:                  100 lines  (pure state store)
lib/session-handler.ts:        195 lines  (unchanged)
lib/feedback-system.ts:        283 lines  (unchanged)
──────────────────────────────────────
TOTAL:                       ~1,800 lines
```

### Architecture Principles
1. **Direct function calls** - No event-bus for local state
2. **Single responsibility** - Each module owns one domain
3. **No circular deps** - Clear dependency tree
4. **Minimal abstraction** - Only abstract what varies

---

## Phase Breakdown

### Phase 4: Extract ConversationController (Est: 4-6 hours)

**Goal:** Move conversation/turn management out of main.ts

**What to Extract:**
- `addUserTurnToUI()` / `addAssistantTurnToUI()`
- `recordConversationTurn()` / `loadConversationHistoryIntoUI()`
- `handleConversationItemAdded()` / `handleConversationItemDone()`
- `currentStreamingMessageId` / streaming state

**Expected Reduction:**
- main.ts: 2,009 → ~1,600 lines (-400)
- NEW conversation-controller.ts: ~400 lines

**Risk:** Low (conversation logic is isolated)

**Validation:**
- All 73 tests must pass
- TypeScript clean
- Manual smoke test

---

### Phase 5: Remove Event-Bus for Local Flows (Est: 6-8 hours)

**Goal:** Replace event emissions with direct callbacks

**Changes:**

**5.1: VoiceController Callback API**
```typescript
// Before:
voiceController.startPTT()
→ emits voice_channel:armed
→ listener updates UI

// After:
voiceController = new VoiceController({
  onStateChange: (state) => updateUI(state),
  onFinalTranscript: (text) => addTurn(text),
  onError: (error) => showError(error)
})
```

**5.2: Remove Compatibility Layer**
- Delete VoiceControllerCompat class
- Remove voice_channel:* event emissions
- Remove state:changed event emissions
- Keep event-bus ONLY for cross-app events (Jarvis↔Zerg)

**Expected Reduction:**
- voice-controller.ts: 638 → ~350 lines (-288)
- main.ts: ~1,600 → ~1,400 lines (-200 from event setup)

**Risk:** Medium (touches all UI integration)

**Validation:**
- Update all tests to use callbacks
- All tests must pass
- Manual smoke test of voice, text, hands-free flows

---

### Phase 6: Simplify StateManager (Est: 3-4 hours)

**Goal:** Pure state store, no logic

**Current Issues:**
- StateManager has 279 lines
- Contains logic for state transitions
- Overlaps with voiceController state

**Changes:**
```typescript
// Before:
class StateManager {
  setVoiceButtonState(state) { /* 50 lines of logic */ }
}

// After:
interface AppState {
  voiceButtonState: VoiceButtonState
  session: RealtimeSession | null
  agent: RealtimeAgent | null
}

const state = new Proxy({}, { /* simple reactive */ })
```

**Expected Reduction:**
- state-manager.ts: 279 → ~100 lines (-179)
- OR: Delete entirely, use voiceController.getState()

**Risk:** Medium (UI depends on state updates)

**Validation:**
- All tests must pass
- UI still reactive to state changes

---

### Phase 7: Final Cleanup & Documentation (Est: 2-3 hours)

**7.1: Backfill Test Coverage**
- Port mic retry tests
- Port event ordering tests
- Port VAD timing tests
- Target: 85+ tests

**7.2: Update Documentation**
- Rewrite REMAINING_INTEGRATION_TASKS.md
- Document new architecture
- Create module dependency diagram

**7.3: Final Polish**
- Remove dead code
- Consolidate duplicate logic
- Clean up comments referencing old modules

---

## Success Criteria

### Quantitative
- ✅ Total LOC: ~1,800 lines (core logic)
- ✅ main.ts: <1,000 lines
- ✅ voice-controller.ts: <400 lines
- ✅ Test coverage: 85+ tests
- ✅ TypeScript: Clean compilation
- ✅ No circular dependencies

### Qualitative
- ✅ Can trace any user action through code linearly
- ✅ No event-bus for local state (only cross-app)
- ✅ Each module has single clear responsibility
- ✅ New dev can understand flow in <30 minutes
- ✅ "Would Carmack approve?" = Yes

---

## Risk Management

### High-Risk Changes
1. Event-bus removal (Phase 5) - touches all UI
2. StateManager simplification (Phase 6) - UI reactivity

### Mitigation
- Commit after every working step
- Run tests after every change
- Manual smoke test after each phase
- Keep backup commits for rollback

### Rollback Points
- After Phase 4: Conversation extracted, event-bus still works
- After Phase 5.2: Callbacks wired, old events still emit
- After Phase 5.3: Compatibility layer removed (point of no return)

---

## Timeline Estimate

| Phase | Estimated Time | Risk Level |
|-------|---------------|------------|
| Phase 4 | 4-6 hours | Low |
| Phase 5 | 6-8 hours | Medium |
| Phase 6 | 3-4 hours | Medium |
| Phase 7 | 2-3 hours | Low |
| **TOTAL** | **15-21 hours** | **2-3 days** |

---

## Current Commits (Progress Log)

1. `f888659` - Fix duplicate state and double event emissions ✅
2. `e18c097` - Remove websocketHandler abstraction ✅
3. `095b4ee` - Eliminate InteractionStateMachine ✅
4. `91c26e5` - Fix hands-free and state-machine feedback ✅
5. `8fe1129` - Resolve 5 critical refactoring regressions ✅
6. `b4fbbf7` - Add backward compatibility for tests ✅
7. `ac3a38b` - Delete old voice modules ✅
8. `5f98274` - Migrate to unified VoiceController ✅

**Next:** Phase 4.1 - Begin ConversationController extraction

---

## Notes & Learnings

### What Worked
- Systematic approach with tests after each step
- Compatibility layer allowed gradual migration
- Clear commit messages for audit trail

### What Didn't Work
- Consolidation without simplification increased LOC
- Dropping tests without porting edge cases was risky
- Didn't measure line count until too late

### Key Insight
**Refactoring ≠ Simplifying**. We made architecture clearer but kept the same complexity. True simplification requires removing the event-driven pattern entirely.

---

**Last Updated:** November 18, 2025
**Current Phase:** Preparing Phase 4 execution
