# 🔧 Jarvis Refactoring - Remaining Integration Tasks

**Date**: November 17, 2025
**Starting Point**: 65% complete (19 commits, ~6 hours already invested)
**Goal**: Complete the remaining 35% integration work
**Total Estimated Time**: 3-5 hours

---

## 📊 Current State Analysis

### ✅ Completed (65%)
- [x] Main.ts refactoring (2,206 → 333 lines, 85% reduction)
- [x] State Manager integration (centralized state management)
- [x] Session Handler integration (OpenAI Realtime lifecycle)
- [x] Conversation Manager & Vector Store (@jarvis/data-local)
- [x] All 97 tests passing
- [x] Simplified button states (11 → 3)

### 📦 Modules Created But Not Yet Integrated
1. **voice-manager.ts** (235 lines) - PTT, VAD, transcription, hands-free
2. **websocket-handler.ts** (191 lines) - Realtime event processing

---

## 🎯 Master Task List

### P0 - CRITICAL (Required for Completion)

#### Task 1: Wire up voice-manager Module
**File**: `/apps/jarvis/apps/web/lib/voice-manager.ts`
**Lines**: 235 (fully implemented, 0% integrated)
**What it does**:
- Handles PTT button events (mouse/touch/keyboard)
- Manages VAD state changes
- Processes voice transcripts with buffering
- Controls hands-free mode toggle
- Synchronizes with stateManager

**Integration needed in main.ts**:
- Import voiceManager
- Replace inline PTT handlers (lines ~1782-1870)
- Remove duplicate PTT/VAD handling code
- Use voiceManager for all voice interactions

**Estimated time**: 1-1.5 hours

#### Task 2: Wire up websocket-handler Module
**File**: `/apps/jarvis/apps/web/lib/websocket-handler.ts`
**Lines**: 191 (fully implemented, 0% integrated)
**What it does**:
- Routes transport events from OpenAI Realtime
- Handles transcript events (partial & final)
- Processes assistant messages
- Manages error handling
- Cleans up main event loop

**Integration needed in main.ts**:
- Import websocketHandler
- Replace inline transport event handling (lines ~1251-1338)
- Use websocketHandler for event routing
- Remove duplicate event handling code

**Estimated time**: 1-1.5 hours

### P1 - HIGH PRIORITY (Quality)

#### Task 3: Final Cleanup of main.ts
**Goal**: Ensure main.ts is truly just an orchestrator (~300 lines)
**Actions**:
- Verify all inline handlers moved to modules
- Remove unused/v duplicate code paths
- Clean up TODOs and comments
- Ensure clear separation of concerns

**Estimated time**: 30-60 minutes

#### Task 4: Comprehensive Testing & Validation
**Actions**:
- Run full test suite (ensure 97 tests still pass)
- Test voice mode (PTT, hands-free)
- Test text mode switching
- Validate state transitions
- Check for regressions

**Estimated time**: 30-60 minutes

### P2 - MEDIUM PRIORITY (Polish)

#### Task 5: Final Verification & Documentation
**Actions**:
- Update this document with completion status
- Verify all modules properly documented
- Check for any remaining stub implementations
- Final code review

**Estimated time**: 30 minutes

---

## 🚀 Implementation Stages

### Stage 1: Voice Manager Integration
**Commits**:
- `feat: integrate voice-manager module`
- `fix: update PTT handlers to use voiceManager`
- `fix: migrate VAD handling to voice-manager`

**Actions**:
1. Import voiceManager in main.ts
2. Replace onpointerdown/onpointerup handlers with voiceManager
3. Replace onkeydown/onkeyup handlers with voiceManager
4. Update hands-free toggle to use voiceManager
5. Remove inline PTT/VAD code

**Files modified**:
- `apps/jarvis/apps/web/main.ts`

**Testing**:
- Verify PTT works (mouse/touch/keyboard)
- Verify hands-free mode works
- Verify state transitions work

### Stage 2: WebSocket Handler Integration
**Commits**:
- `feat: integrate websocket-handler module`
- `fix: route transport events through websocketHandler`
- `fix: migrate transcript handling to websocketHandler`

**Actions**:
1. Import websocketHandler in main.ts
2. Replace setupSessionEvents with websocketHandler
3. Remove duplicate transport event handling
4. Clean up main event loop
5. Remove inline transcript processing

**Files modified**:
- `apps/jarvis/apps/web/main.ts`

**Testing**:
- Verify voice mode works
- Verify transcripts process correctly
- Verify assistant responses work

### Stage 3: Final Cleanup
**Commits**:
- `refactor: final main.ts cleanup`
- `refactor: remove duplicate code paths`
- `docs: update integration status`

**Actions**:
1. Audit main.ts for remaining inline code
2. Move any stragglers to appropriate modules
3. Remove unused imports/variables
4. Clean up TODOs and debug code

**Files modified**:
- `apps/jarvis/apps/web/main.ts`
- `REFACTORING_SUMMARY.md` (update)

### Stage 4: Comprehensive Testing
**Commits**:
- `test: run comprehensive validation suite`
- `test: verify all test cases pass`

**Actions**:
1. Run full test suite
2. Manual testing of all features
3. Check for regressions
4. Performance validation

**Files tested**:
- All test files in `apps/jarvis/apps/web/tests/`

### Stage 5: Final Verification
**Commits**:
- `docs: mark integration complete`
- `chore: final cleanup and verification`

**Actions**:
1. Update this document
2. Verify all modules integrated
3. Code review
4. Final commit

---

## 📝 Integration Checkpoints

### After Stage 1
- [ ] voiceManager imported in main.ts
- [ ] PTT handlers use voiceManager
- [ ] VAD handling uses voiceManager
- [ ] Hands-free toggle uses voiceManager
- [ ] Inline PTT/VAD code removed

### After Stage 2
- [ ] websocketHandler imported in main.ts
- [ ] Transport events route through websocketHandler
- [ ] Transcript handling uses websocketHandler
- [ ] Inline event handling code removed

### After Stage 3
- [ ] main.ts < 350 lines
- [ ] All inline code moved to modules
- [ ] Clear separation of concerns
- [ ] No duplicate code paths

### After Stage 4
- [ ] All 97 tests passing
- [ ] Voice mode working (PTT, hands-free)
- [ ] Text mode working
- [ ] State transitions smooth
- [ ] No regressions

### After Stage 5
- [ ] Documentation updated
- [ ] Code review complete
- [ ] Ready for production

---

## 🔍 Code Locations to Modify

### main.ts Lines to Replace

#### PTT Handlers (Stage 1)
```typescript
// OLD (lines ~1782-1870)
pttBtn.onpointerdown = async (e) => { /* inline handler */ };
pttBtn.onpointerup = () => { /* inline handler */ };
pttBtn.onkeydown = async (e) => { /* inline handler */ };
pttBtn.onkeyup = (e) => { /* inline handler */ };

// NEW
import { voiceManager } from './lib/voice-manager';
voiceManager.setupVoiceButton(pttBtn);
```

#### Transport Events (Stage 2)
```typescript
// OLD (lines ~1251-1338)
session?.on('transport_event', async (event: any) => {
  const t = event.type || '';
  if (t.includes('input_audio_buffer') && t.includes('speech_started')) { /* inline */ }
  if (t.includes('speech_stopped')) { /* inline */ }
  // ... 80+ more lines
});

// NEW
import { websocketHandler } from './lib/websocket-handler';
websocketHandler.setupSessionHandlers(session);
```

---

## 📊 Success Metrics

| Metric | Target | Current | Goal |
|--------|--------|---------|------|
| main.ts lines | < 350 | 333 | ✅ |
| voice-manager integration | 100% | 0% | → 100% |
| websocket-handler integration | 100% | 0% | → 100% |
| Inline code in main.ts | 0% | ~30% | → 0% |
| Test pass rate | 100% | 100% | Maintain |
| Duplicate code paths | 0 | ~10 | → 0 |

---

## ⚠️ Risk Mitigation

### Risks
1. **Breaking existing functionality** - Mitigate: Commit after each change, run tests frequently
2. **State synchronization issues** - Mitigate: Use stateManager for all state updates
3. **Performance regressions** - Mitigate: Test voice/text modes thoroughly

### Rollback Plan
- Each stage is a separate commit
- Can roll back to any checkpoint
- Test suite catches regressions early

---

## 🎓 Learning Objectives

After completing this work:
1. ✅ Complete modular architecture understanding
2. ✅ Event-driven pattern mastery
3. ✅ Clean separation of concerns
4. ✅ State management best practices
5. ✅ Voice/text separation architecture

---

## 📚 References

- **Refactoring Summary**: `/Users/davidrose/git/zerg/REFACTORING_SUMMARY.md`
- **Original Commit**: `28c7b0b` - "docs: add comprehensive refactoring summary"
- **State Manager**: `/apps/jarvis/apps/web/lib/state-manager.ts`
- **Session Handler**: `/apps/jarvis/apps/web/lib/session-handler.ts`
- **Voice Manager**: `/apps/jarvis/apps/web/lib/voice-manager.ts`
- **WebSocket Handler**: `/apps/jarvis/apps/web/lib/websocket-handler.ts`

---

**Ready to begin implementation!** 🚀
