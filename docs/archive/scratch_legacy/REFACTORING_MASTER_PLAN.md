# 🔧 Jarvis/Zerg Platform Refactoring Master Plan

**Created**: 2025-11-15
**Status**: IN PROGRESS
**Estimated Duration**: 2-3 days
**Priority**: CRITICAL - Fix architectural debt from langgraph-streaming merge

---

## 📊 Executive Summary

After merging the langgraph-streaming PR (53 commits, 6,260 net lines of actual code), we identified critical architectural issues that need immediate attention. The PR added valuable features but introduced unnecessary complexity that violates the 80/20 principle.

### Key Metrics
- **main.ts**: 2,206 lines (CRITICAL - must be under 500)
- **styles.css**: 1,085 lines (needs splitting)
- **Test coverage**: Good (~30% of additions)
- **Documentation**: Over-documented (1,800+ lines)

---

## 🎯 Priority Matrix

### P0 - CRITICAL (Day 1)
These block everything else and risk system stability.

| Task | Current State | Target State | Effort | Status |
|------|--------------|--------------|--------|--------|
| Split main.ts | 2,206 lines, 79 top-level declarations | <500 lines, 5-6 modules | 3-4 hours | 🔴 PENDING |
| Remove package-lock.json | 10,041 lines tracked | .gitignore'd | 5 mins | 🔴 PENDING |
| Fix God Object pattern | main.ts does everything | Proper separation | 2 hours | 🔴 PENDING |

### P1 - HIGH (Day 1-2)
These cause daily friction and confusion.

| Task | Current State | Target State | Effort | Status |
|------|--------------|--------------|--------|--------|
| Simplify button phases | 11 phases | 3 phases max | 2 hours | 🔴 PENDING |
| Split CSS files | 1,085 lines in one file | 5-6 component files | 2 hours | 🔴 PENDING |
| Extract feedback system | Embedded in main.ts | Plugin/module | 1 hour | 🔴 PENDING |

### P2 - MEDIUM (Day 2)
These improve maintainability but aren't blocking.

| Task | Current State | Target State | Effort | Status |
|------|--------------|--------------|--------|--------|
| Clean documentation | 728 lines for button | <100 lines technical | 1 hour | 🔴 PENDING |
| Consolidate state machines | Over-elaborate | Simple states | 2 hours | 🔴 PENDING |
| Remove redundant tests | Some overlap | Clean suite | 1 hour | 🔴 PENDING |

---

## 📁 Module Extraction Plan for main.ts

### Current Structure (2,206 lines)
```
main.ts
├── Configuration (lines 1-50)
├── State Management (lines 51-300)
├── Voice Handling (lines 301-800)
├── Text Handling (lines 801-1100)
├── UI Updates (lines 1101-1500)
├── WebSocket Management (lines 1501-1700)
├── Feedback System (lines 1701-1900)
├── Session Management (lines 1901-2206)
```

### Target Structure (<500 lines each)
```
main.ts (orchestrator, <500 lines)
├── lib/
│   ├── voice-manager.ts (~400 lines)
│   ├── session-manager.ts (~300 lines)
│   ├── ui-controller.ts (~300 lines)
│   ├── feedback-system.ts (~200 lines)
│   ├── websocket-handler.ts (~250 lines)
│   ├── state-manager.ts (~200 lines)
│   └── config.ts (~50 lines)
```

---

## 🔨 Implementation Tasks

### Task 1: Split main.ts (P0)
**File**: `apps/jarvis/apps/web/main.ts`

1. **Extract Configuration Module** (`lib/config.ts`)
   - Move all CONFIG objects
   - Environment variables
   - Default settings

2. **Extract State Manager** (`lib/state-manager.ts`)
   - Global state variables
   - State mutations
   - State getters

3. **Extract Voice Manager** (`lib/voice-manager.ts`)
   - Voice button handlers
   - PTT logic
   - VAD handling
   - Microphone management

4. **Extract Session Manager** (`lib/session-manager.ts`)
   - Connection logic
   - Session state
   - Reconnection handling

5. **Extract UI Controller** (`lib/ui-controller.ts`)
   - DOM updates
   - Status label management
   - Visual state updates

6. **Extract Feedback System** (`lib/feedback-system.ts`)
   - Haptic feedback
   - Audio feedback
   - Preference management

7. **Extract WebSocket Handler** (`lib/websocket-handler.ts`)
   - Message handling
   - Event processing
   - Stream management

8. **Update main.ts**
   - Import modules
   - Wire together on DOMContentLoaded
   - Keep under 500 lines

### Task 2: Remove package-lock.json (P0)
**File**: `apps/jarvis/package-lock.json`

1. Add to `.gitignore`
2. Remove from tracking: `git rm --cached apps/jarvis/package-lock.json`
3. Commit change

### Task 3: Simplify Button Phases (P1)
**Files**: Various in `apps/jarvis/`

Current 11 phases → 3 phases:
1. **Ready** (disconnected, can connect)
2. **Active** (connected, can interact)
3. **Processing** (busy, wait)

Remove:
- Excessive state transitions
- Over-elaborate feedback
- Philosophical documentation

### Task 4: Split CSS (P1)
**File**: `apps/jarvis/apps/web/styles.css`

Split into:
- `styles/base.css` - Reset, variables
- `styles/layout.css` - Page structure
- `styles/voice-button.css` - Button styles
- `styles/conversation.css` - Chat UI
- `styles/animations.css` - Transitions
- `styles/components.css` - Other components

### Task 5: Documentation Cleanup (P2)
**Files**: `apps/jarvis/docs/*.md`

- Remove philosophical discussions
- Keep only technical implementation details
- Consolidate redundant docs
- Target: <100 lines per feature

---

## ✅ Success Criteria

### Immediate Success (Day 1)
- [ ] main.ts under 500 lines
- [ ] All tests still passing
- [ ] package-lock.json removed
- [ ] No functionality broken

### Full Success (Day 2-3)
- [ ] CSS properly modularized
- [ ] Button simplified to 3 phases
- [ ] Documentation concise
- [ ] Code follows 80/20 principle
- [ ] Clean module boundaries

---

## 🧪 Testing Plan

After each major change:
1. Run Jarvis tests: `cd apps/jarvis && npm test`
2. Run Zerg tests: `cd apps/zerg && npm test`
3. Manual testing: `make jarvis-dev`
4. Verify voice/text separation still works
5. Check button states

---

## 📈 Progress Tracking

### Day 1 Goals
- [x] Create master plan
- [ ] Split main.ts into modules
- [ ] Remove package-lock.json
- [ ] Run initial test suite

### Day 2 Goals
- [ ] Simplify button implementation
- [ ] Split CSS files
- [ ] Extract feedback system
- [ ] Run comprehensive tests

### Day 3 Goals
- [ ] Clean documentation
- [ ] Final testing
- [ ] Create summary report
- [ ] Commit all changes

---

## 🚀 Commands Reference

```bash
# Development
make jarvis-dev     # Test Jarvis locally
make zerg-dev      # Test Zerg locally
make dev           # Test full platform

# Testing
cd apps/jarvis && npm test
cd apps/zerg/backend && ./run_backend_tests.sh
cd apps/zerg/frontend-web && npm test
cd apps/zerg/e2e && npx playwright test

# Git
git add -p         # Stage changes interactively
git commit -m "refactor: [component] - description"
```

---

## 📝 Notes

- Focus on 80/20 principle: maximum value, minimum complexity
- Each module should have single responsibility
- Preserve all functionality while reducing complexity
- Test frequently to catch regressions early
- Document only what's necessary

---

**END OF MASTER PLAN**
