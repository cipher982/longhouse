# Mobile Drag-and-Drop Implementation Failure - Bug Report

**Status**: CRITICAL - Touch drag non-functional
**Date**: 2025-10-20
**Severity**: P0 (Feature completely broken)
**Commits Affected**: `14925ea`, `f0e109c`, `7c61cc8`

---

## Executive Summary

Mobile drag-and-drop for canvas shelf items is **completely non-functional**. Users cannot drag agents/tools from the shelf onto the canvas on touch devices (iOS/Android). The issue appears to be architectural—the pointer event tracking state is lost between events due to React re-renders and closure stale references.

---

## What Was Attempted

### Implementation (Commit `14925ea`)
1. Created `usePointerDrag` hook with state management
2. Added `onPointerDown` handlers to agent/tool items to call `startDrag()`
3. Added document-level `pointermove` and `pointerup` listeners
4. On drop: retrieve data via `getDragData()` and create nodes

### Expected Flow
```
Touch Agent Item
    ↓
onPointerDown → startDrag(event, data)  [state = data]
    ↓
Touch Move → pointermove → updateDragPosition(event)  [check threshold]
    ↓
Release → pointerup → getDragData() + create node  [state should have data]
```

### Actual Flow (BROKEN)
```
Touch Agent Item
    ↓
onPointerDown fires ✓
startDrag() called ✓ [sets dragState.current]
    ↓
Touch Move → NO pointermove listeners firing ✗
    ↓
Release → pointerup fires ✓
getDragData() returns null ✗ [dragState.current is empty or wrong]
    ↓
No node created
```

---

## Root Cause Analysis

### Critical Issues Identified

#### Issue 1: React PointerEvent ≠ Native PointerEvent (Type Mismatch)
**File**: `src/pages/CanvasPage.tsx:997-1002`

```typescript
onPointerDown={(event) => {
  if (event.isPrimary) {
    startDrag(event as unknown as React.PointerEvent, {  // ← CASTING ISSUE
      type: 'agent',
      id: agent.id.toString(),
      name: agent.name
    });
  }
}}
```

**Problem**:
- React's `SyntheticEvent` (React.PointerEvent) is a **wrapper** around native PointerEvent
- `event.currentTarget` on React event is a **virtual DOM element**, not the actual DOM element
- `setPointerCapture()` is called on a synthetic wrapper, not the real element
- This likely throws or silently fails in the hook

**Evidence**:
- In `usePointerDrag.ts:69`, we do: `(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)`
- But `event.currentTarget` from React synthetic event may not be the real DOM node
- Even if it works, pointer capture is lost when the synthetic event is pooled/reused

---

#### Issue 2: Document Event Listeners Use Native Events, But startDrag Never Fires
**File**: `src/pages/CanvasPage.tsx:584-585`

```typescript
const handlePointerMove = (e: PointerEvent) => {  // ← Native PointerEvent
  updateDragPosition(e);
};

const handlePointerUp = (e: PointerEvent) => {   // ← Native PointerEvent
  const dragData = getDragData();
  if (dragData) {
    // create node
  }
  endDrag(e);
};

document.addEventListener("pointermove", handlePointerMove);
document.addEventListener("pointerup", handlePointerUp);
```

**Problem**:
- If `startDrag()` never actually captured the pointer (due to Issue #1), pointer events never reach the document listeners
- The pointermove/pointerup listeners may be firing, but `dragState.current` is empty
- `getDragData()` checks `isActive`, which is still false if pointer wasn't properly captured

---

#### Issue 3: Pointer Capture Failure = No Move Events
**File**: `src/hooks/usePointerDrag.ts:68-69`

```typescript
// Set capture on the element so we get all pointer events
(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
```

**Problem**:
- If this call fails (silently), subsequent `pointermove` events won't be received
- The element must be a real DOM node for `setPointerCapture` to work
- React synthetic events have different `currentTarget` behavior
- No error handling = silent failure

**What Should Happen**:
```typescript
const element = event.currentTarget as HTMLElement;
try {
  element.setPointerCapture(event.pointerId);
  console.log('Pointer captured on', element.tagName, element.className);
} catch (error) {
  console.error('Pointer capture failed:', error);
}
```

---

#### Issue 4: Threshold Check Prevents getDragData() Return
**File**: `src/hooks/usePointerDrag.ts:121-124`

```typescript
const getDragData = useCallback((): DragData | null => {
  const state = dragState.current;
  return state.isActive ? state.data : null;  // ← Only returns if isActive=true
}, []);
```

**Problem**:
- `getDragData()` returns null if `isActive === false`
- `isActive` only becomes true if pointer moves > 5px (threshold)
- On mobile, if user lifts finger immediately (quick tap), isActive never reaches true
- Even if threshold is passed, if pointer capture failed, move events never fire

---

#### Issue 5: Dependency Array Missing Critical Functions
**File**: `src/pages/CanvasPage.tsx:594`

```typescript
}, [dragPreviewData, reactFlowInstance, resetDragPreview, zoom, getDragData, updateDragPosition, endDrag, setNodes]);
```

**Problem**:
- `getDragData`, `updateDragPosition`, `endDrag` are in dependency array
- These are memoized callbacks from the hook
- If they change, the effect re-runs and re-attaches listeners
- But the event listeners inside close over the original functions (stale closure)

---

## Detailed Failure Scenario

### On Mobile (iOS Safari / Android Chrome)
1. User touches agent item for 500ms
2. `onPointerDown` fires with React.SyntheticPointerEvent
3. `startDrag()` called with React event wrapper
4. Inside `startDrag()`:
   - `event.currentTarget` is NOT the real DOM element
   - `setPointerCapture(pointerId)` likely throws or fails silently
   - `dragState.current = { data, ... }` succeeds (state is set)
5. User moves finger
6. Native `pointermove` event fires on document
7. `handlePointerMove()` calls `updateDragPosition(nativeEvent)`
8. Inside `updateDragPosition()`:
   - `if (!state.data) return` ✓ (we have data)
   - But pointer wasn't captured, so pointer events might be batched differently
9. User releases finger
10. `pointerup` fires
11. `getDragData()` called
12. Returns null because `state.isActive` is still false (or data is corrupted)
13. Node is NOT created

---

## Code Path Issues

### Hook Initialization
```typescript
const { startDrag, updateDragPosition, endDrag, getDragData, isPointerDragging } = usePointerDrag();
```

**Missing**: The hook has no way to synchronize state across React renders.

### Event Handler Binding
```typescript
onPointerDown={(event) => {  // This is React.PointerEvent
  startDrag(event as unknown as React.PointerEvent, data);  // Wrong event type!
}}
```

**Problem**: The `as unknown as` cast masks a type error. Should be:
```typescript
onPointerDown={(event) => {
  // Get the real DOM element
  const element = event.currentTarget as HTMLElement;
  // Convert synthetic event to native-like object
  startDrag({
    button: event.button,
    isPrimary: event.isPrimary,
    clientX: event.clientX,
    clientY: event.clientY,
    currentTarget: element,
    pointerId: event.pointerId,
    // ... other fields
  } as React.PointerEvent, data);
}}
```

But this still wouldn't solve the capture issue.

---

## What's NOT Working

### ❌ Pointer Capture
- `setPointerCapture()` not being called on real DOM element
- Pointer events not staying focused on the dragged item

### ❌ State Persistence
- `dragState.current` set in `startDrag()` may not persist to `updateDragPosition()`
- React re-renders might clear refs (shouldn't, but could due to closure issues)

### ❌ Document Listeners
- Even if they fire, `getDragData()` returns null
- Event handlers close over stale function references

### ❌ Threshold Detection
- Needs ≥5px movement to set `isActive = true`
- But if pointer capture failed, movement events never reach handler

---

## Evidence

### What We Know Works
- ✅ Desktop mouse drag (HTML5 API still works)
- ✅ Shelf open/close with touch
- ✅ Canvas pan/zoom (when shelf closed)
- ✅ Event propagation is blocked (`touch-action: none` working)

### What's Broken
- ❌ Touch drag from shelf
- ❌ No nodes appear on canvas after touch drag attempt
- ❌ No console errors visible (silent failure)

---

## Architectural Problems

### Problem 1: React Synthetic Events + Native DOM APIs
- **Mismatch**: usePointerDrag expects native PointerEvent with real DOM element
- **Reality**: CanvasPage passes React.SyntheticEvent with virtual DOM element
- **Solution**: Must convert synthetic event before passing to hook

### Problem 2: Hook Uses useRef for State That Needs Re-render Synchronization
- **Issue**: `dragState.current` persists across renders
- **But**: Listeners are recreated/reattached each render (dependency array)
- **Result**: Stale closures over stale state

### Problem 3: No Cross-browser Compatibility Testing
- HTML5 drag works: desktop browsers
- Pointer API: varies by platform
  - iOS: Limited pointer event support
  - Android Chrome: Good support
  - Firefox Mobile: May differ
- No feature detection

### Problem 4: Dual Event System Creates Race Conditions
- HTML5 drag events + pointer events both listening
- Order of execution unpredictable
- State conflicts possible

---

## Recommended Actions

### Immediate (Quick Fix)
1. **Add debugging**:
   ```typescript
   const startDrag = useCallback((event: React.PointerEvent, data: DragData) => {
     console.log('[startDrag] event.currentTarget:', event.currentTarget);
     console.log('[startDrag] pointerId:', event.pointerId);
     console.log('[startDrag] setting data:', data);
     // ... rest of function
   });
   ```

2. **Verify pointer capture**:
   ```typescript
   try {
     (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
     console.log('[startDrag] Pointer captured ✓');
   } catch (error) {
     console.error('[startDrag] Pointer capture failed:', error);
   }
   ```

3. **Check getDragData on pointerup**:
   ```typescript
   const dragData = getDragData();
   console.log('[pointerup] dragData:', dragData);
   ```

### Short Term (Proper Fix Required)
Need senior dev to address:

1. **Unwrap React Events**
   - Convert React.SyntheticEvent to native PointerEvent data
   - Or use `event.nativeEvent` directly
   - Extract real DOM element via `event.currentTarget`

2. **Separate Touch from HTML5 Drag**
   - Keep HTML5 drag for desktop
   - Use pointer-only for touch (simplifies logic)
   - Don't mix both APIs

3. **Fix State Management**
   - Use React state instead of useRef for cross-render sync
   - Or implement proper event-driven state machine
   - Ensure closure over latest state

4. **Add Proper Error Handling**
   - Try-catch around setPointerCapture
   - Console logs for debugging
   - Fallback behavior if pointer capture fails

5. **Test on Real Devices**
   - iOS Safari with touch
   - Android Chrome with touch
   - Test with long-press detection
   - Verify threshold behavior

---

## Questions for Senior Dev

1. Should we keep HTML5 drag and pointer events together, or separate by platform?
2. Is using `useRef` for drag state appropriate, or should we use React state?
3. How do we properly extract native DOM elements from React synthetic events?
4. Should threshold detection be per-platform (5px too aggressive on touch)?
5. Do we need gesture recognition (long-press) instead of immediate drag start?
6. Should we simplify to only support one API per platform?

---

## Rollback Plan

If major refactor needed:
```bash
git revert 14925ea  # Remove pointer integration
git revert f0e109c  # Remove pan prevention
git revert 7c61cc8  # Remove usePointerDrag hook
# Keep other improvements (accessibility, responsive, state persistence)
```

---

## Files to Review

- `src/hooks/usePointerDrag.ts` - Hook implementation
- `src/pages/CanvasPage.tsx:950-1070` - Integration points
- `src/pages/CanvasPage.tsx:545-594` - Event listener setup
- `src/pages/CanvasPage.tsx:195` - Hook initialization

---

## Next Steps

1. Run with console debugging to identify exact failure point
2. Verify pointer capture is working on real mobile device
3. Check if getDragData() ever returns non-null value
4. Evaluate if HTML5 drag API fallback should be primary (simpler)
5. Consider alternative: long-press + tap-to-place instead of drag

---

*This report requires hands-on debugging on mobile device with DevTools to identify exact failure point. Console logs will be critical to next steps.*
