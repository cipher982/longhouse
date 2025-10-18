# Mobile Responsive Refactor - Task List

**Project:** Zerg React Frontend Mobile Responsiveness
**Created:** 2025-10-18
**Status:** Ready for Implementation
**Estimated Duration:** 10-15 hours
**Priority:** High (P0)

---

## Problem Statement

The frontend has three cascading point-of-origin issues preventing mobile responsiveness:

1. **Root constraint:** `overflow: hidden` on `body` and `#app-container` blocks all page scrolling
2. **Hard minimums:** Dashboard has `min-width: 800px` that overrides responsive card-view CSS
3. **CSS-React disconnect:** Mobile drawer states exist in CSS (`.open`, `.active` classes) but React never activates them

**Result:** Profile page content unreachable, Dashboard forces horizontal scroll, Canvas/Chat drawers permanently hidden on mobile.

---

## Implementation Strategy

### Phase 1: Unblock Page Scrolling (4-6 hours)
Fix root constraint so pages can scroll naturally on small viewports.

#### Task 1.1: Update Global Layout Scrolling
**File:** `src/styles/layout.css`
**Current state:**
```css
body { overflow: hidden; }           /* Line 54 */
#app-container { overflow: hidden; } /* Line 81 */
```

**Change to:**
```css
body { overflow-y: auto; }            /* Allow vertical scroll naturally */
#app-container {
  overflow-y: auto;                  /* Allow pages to scroll */
  overflow-x: hidden;                /* Prevent horizontal scroll */
}
```

**Why:** `overflow: hidden` was designed for single-page app feel on desktop. Removing it lets content naturally scroll when it exceeds viewport height on mobile. The grid layout still provides the header/footer chrome.

**Testing:**
- [ ] Profile page scrolls on mobile (previous blocker)
- [ ] Header/footer remain fixed/visible while content scrolls
- [ ] No horizontal scrolling appears on any page

---

#### Task 1.2: Remove Redundant Chat Header Margin
**File:** `src/styles/chat.css`
**Current state:**
```css
.chat-header {
  margin-top: 70px;  /* Line 71 - double-counting grid layout */
}
```

**Change to:**
```css
.chat-header {
  /* margin-top: 0; - remove, grid layout provides spacing */
}
```

**Why:** The grid layout (`layout.css` line 60) already accounts for header positioning. This margin shrinks the chat viewport on short mobile screens.

**Testing:**
- [ ] Chat header aligns properly, no excess margin
- [ ] Chat messages area has full available height on mobile

---

### Phase 2: Fix Dashboard Width Constraints (2-3 hours)
Remove hard minimums that block responsive behavior.

#### Task 2.1: Replace Hard Min-Widths with Fluid Widths
**File:** `src/styles/css/dashboard.css`
**Current state (lines 144-157):**
```css
.dashboard-container {
  flex: 1;
  padding: 20px;
  width: 100%;
  max-width: 1200px;
  min-width: 800px;   /* ← BLOCKER: Prevents mobile from working */
  margin: 0 auto;
  overflow-y: auto;
  ...
}
```

**Change to:**
```css
.dashboard-container {
  flex: 1;
  padding: 20px;
  width: 100%;
  max-width: 1200px;
  /* min-width removed - allow fluid width on all screens */
  margin: 0 auto;
  overflow-y: auto;
  ...
}

/* NEW: Responsive padding on mobile */
@media (max-width: 480px) {
  .dashboard-container {
    padding: 12px;
  }
}
```

**Why:** Hard `min-width: 800px` forces horizontal scroll on phones. Removing it lets the existing card-view CSS media queries work properly.

**Testing:**
- [ ] Dashboard fits mobile viewport without horizontal scroll
- [ ] Existing card-view media query `@media (max-width: 768px)` now activates
- [ ] Desktop view still looks identical

---

#### Task 2.2: Make Search Bar Responsive
**File:** `src/styles/css/dashboard.css`
**Current state (lines 179-187):**
```css
.search-container {
  display: flex;
  align-items: center;
  background: var(--bg-dark);
  border-radius: var(--radius-sm);
  padding: 8px 16px;
  width: 300px;        /* ← Fixed, blocks on mobile */
  flex-shrink: 0;
}
```

**Change to:**
```css
.search-container {
  display: flex;
  align-items: center;
  background: var(--bg-dark);
  border-radius: var(--radius-sm);
  padding: 8px 16px;
  width: min(100%, 300px);  /* Adaptive: 100% or 300px */
  flex-shrink: 0;
}

/* On mobile, stack header elements */
@media (max-width: 480px) {
  .dashboard-header {
    flex-direction: column;
    align-items: stretch;
  }

  .search-container {
    width: 100%;
  }

  .button-container {
    width: 100%;
  }
}
```

**Why:** Fixed 300px width forces layout to break on mobile. Using `min()` makes it responsive.

**Testing:**
- [ ] Search bar shrinks on mobile, still usable
- [ ] Dashboard header elements stack vertically on small screens
- [ ] Touch targets remain >= 44px

---

### Phase 3: Create Shared Shelf State Management (4-6 hours)
Wire React state to activate existing CSS drawer states.

#### Task 3.1: Create Shelf State Context
**File:** `src/lib/useShelfState.ts` (NEW)

```typescript
import { createContext, useContext, useState, ReactNode } from 'react';

interface ShelfContextType {
  isShelfOpen: boolean;
  toggleShelf: () => void;
  closeShelf: () => void;
}

export const ShelfContext = createContext<ShelfContextType | null>(null);

export function ShelfProvider({ children }: { children: ReactNode }) {
  const [isShelfOpen, setIsShelfOpen] = useState(false);

  const toggleShelf = () => setIsShelfOpen(prev => !prev);
  const closeShelf = () => setIsShelfOpen(false);

  return (
    <ShelfContext.Provider value={{ isShelfOpen, toggleShelf, closeShelf }}>
      {children}
    </ShelfContext.Provider>
  );
}

export function useShelf() {
  const context = useContext(ShelfContext);
  if (!context) {
    throw new Error('useShelf must be used within ShelfProvider');
  }
  return context;
}
```

**Why:** Centralized state for drawer toggle avoids prop drilling. Both Canvas and Chat pages need independent drawer states (canvas shelf ≠ chat threads).

**Testing:**
- [ ] No errors creating context
- [ ] Hook works with provider wrapping

---

#### Task 3.2: Add ShelfProvider to App Root
**File:** `src/routes/App.tsx`

Find the main layout/provider setup and wrap with ShelfProvider:

```tsx
import { ShelfProvider } from '../lib/useShelfState';

// In your component tree:
<ShelfProvider>
  <Layout>
    {/* rest of app */}
  </Layout>
</ShelfProvider>
```

**Why:** Makes shelf state available to all descendant components.

**Testing:**
- [ ] App starts without errors
- [ ] No console warnings about context

---

#### Task 3.3: Wire Layout Hamburger Button to Shelf State
**File:** `src/components/Layout.tsx`

**Current state (lines 52-62):**
```tsx
<button
  id="shelf-toggle-btn"
  aria-label="Open agent panel"
  aria-controls="agent-shelf"
  aria-expanded="false"
  onClick={() => {
    // TODO: Implement shelf toggle functionality
    console.log("Shelf toggle clicked - not implemented yet");
  }}
>
  <MenuIcon />
</button>
```

**Change to:**
```tsx
import { useShelf } from "../lib/useShelfState";

// In WelcomeHeader component:
const { isShelfOpen, toggleShelf } = useShelf();

<button
  id="shelf-toggle-btn"
  aria-label="Open agent panel"
  aria-controls="agent-shelf"
  aria-expanded={isShelfOpen}
  onClick={toggleShelf}
>
  <MenuIcon />
</button>
```

**Why:** Connects button click to shared state. Both Canvas and Chat can now listen to this state.

**Testing:**
- [ ] Button click logs no errors
- [ ] aria-expanded reflects state

---

### Phase 4: Wire Canvas Agent Shelf to State (2-3 hours)
Activate canvas drawer CSS by connecting React state.

#### Task 4.1: Subscribe Canvas Shelf to Shelf State
**File:** `src/pages/CanvasPage.tsx`

Find the agent shelf rendering (around line 885):

**Current state:**
```tsx
<div
  id="agent-shelf"
  data-testid="agent-shelf"
  className="agent-shelf"  /* No .open class activation */
>
```

**Change to:**
```tsx
import { useShelf } from "../lib/useShelfState";

// In CanvasPage component:
const { isShelfOpen, closeShelf } = useShelf();

<div
  id="agent-shelf"
  data-testid="agent-shelf"
  className={clsx("agent-shelf", { open: isShelfOpen })}
>

/* Also add scrim overlay */
<div
  className={clsx("shelf-scrim", { "shelf-scrim--visible": isShelfOpen })}
  onClick={closeShelf}
/>
```

**Why:** The CSS class `.open` already exists in `src/styles/css/agent_shelf.css:327`. This activates it.

**Testing:**
- [ ] On mobile, clicking hamburger button shows agent shelf as drawer
- [ ] Clicking scrim closes drawer
- [ ] Desktop (>767px): Shelf always visible, drawer state irrelevant (CSS handles via media query)

---

#### Task 4.2: Add Scrim Overlay Styles (if missing)
**File:** `src/styles/css/agent_shelf.css`

Verify scrim styling exists (around lines 331-353). If missing, add:

```css
.shelf-scrim {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.35);
  opacity: 0;
  pointer-events: none;
  transition: opacity 200ms ease;
  z-index: 1400;
  display: none;
}

body.shelf-open .shelf-scrim,
.shelf-scrim--visible {
  opacity: 1;
  pointer-events: auto;
  display: block;
}

@media (max-width: 767px) {
  .shelf-scrim { display: block; }
}
```

**Testing:**
- [ ] Scrim appears when shelf is open
- [ ] Scrim clicking closes shelf
- [ ] Scrim doesn't appear on desktop

---

### Phase 5: Wire Chat Thread Sidebar to State (2-3 hours)
Activate chat drawer CSS by connecting React state.

#### Task 5.1: Subscribe Chat Thread Sidebar to State
**File:** `src/pages/ChatPage.tsx`

Find the thread sidebar rendering (around line 582):

**Current state:**
```tsx
<aside className="thread-sidebar">
  {/* CSS expects .active class, never set */}
```

**Change to:**
```tsx
import { useShelf } from "../lib/useShelfState";

// In ChatPage component:
const { isShelfOpen, closeShelf } = useShelf();

<aside className={clsx("thread-sidebar", { active: isShelfOpen })}>
  {/* Content */}
</aside>

/* Also add scrim */
<div
  className={clsx("thread-scrim", { "thread-scrim--visible": isShelfOpen })}
  onClick={closeShelf}
/>
```

**Why:** CSS at `src/styles/chat.css:497` defines `.thread-sidebar.active { left: 0; }`. This activates it.

**Testing:**
- [ ] On mobile, clicking hamburger shows thread list as drawer
- [ ] Clicking scrim closes drawer
- [ ] Thread list still accessible on desktop (CSS handles it)
- [ ] Chat input and messages remain usable

---

#### Task 5.2: Add Chat Scrim Styles (if missing)
**File:** `src/styles/chat.css`

Add scrim styling after thread-sidebar styles:

```css
.thread-scrim {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.35);
  opacity: 0;
  pointer-events: none;
  transition: opacity 200ms ease;
  z-index: 5;  /* Below thread-sidebar's z-index */
  display: none;
}

.thread-scrim--visible {
  opacity: 1;
  pointer-events: auto;
  display: block;
}

@media (max-width: 768px) {
  .thread-scrim { display: block; }
}
```

**Testing:**
- [ ] Scrim appears on mobile when sidebar open
- [ ] Scrim closing works
- [ ] No scrim on desktop

---

### Phase 6: Validate and Test (2-3 hours)
Comprehensive testing across viewports and features.

#### Task 6.1: Profile Page Scrolling
**Test on:** Mobile, tablet, desktop

```
[ ] Long profile form scrolls vertically
[ ] Header stays visible
[ ] No horizontal scrolling
[ ] Footer visible when scrolled to bottom
```

**URL:** `/profile`

---

#### Task 6.2: Dashboard Responsiveness
**Test on:** Mobile (320px), tablet (768px), desktop (1200px+)

```
[ ] Mobile: Table converts to card view, no horizontal scroll
[ ] Mobile: Search bar shrinks, stays usable
[ ] Mobile: Buttons stack vertically
[ ] Tablet: Layout adapts nicely
[ ] Desktop: Original layout unchanged
```

**URL:** `/dashboard`

---

#### Task 6.3: Canvas Shelf Drawer
**Test on:** Mobile, tablet, desktop

```
[ ] Mobile: Hamburger button visible
[ ] Mobile: Click hamburger → shelf slides in
[ ] Mobile: Scrim appears, click closes drawer
[ ] Mobile: Can drag agents from drawer to canvas
[ ] Tablet: Shelf visible or drawer depending on width
[ ] Desktop (>767px): Shelf always visible, no drawer
```

**URL:** `/canvas`

---

#### Task 6.4: Chat Thread Sidebar Drawer
**Test on:** Mobile, tablet, desktop

```
[ ] Mobile: Hamburger button visible
[ ] Mobile: Click hamburger → thread list slides in
[ ] Mobile: Scrim appears, click closes drawer
[ ] Mobile: Can select threads and continue chatting
[ ] Tablet: Sidebar visible or drawer
[ ] Desktop (>768px): Sidebar always visible
[ ] Chat messages remain readable on mobile (no shrinking)
```

**URL:** `/chat/:agentId/:threadId`

---

#### Task 6.5: Cross-Browser Testing
**Devices to test:**
- [ ] iPhone 12 (390px, Safari)
- [ ] Android phone (360px, Chrome)
- [ ] iPad (768px, Safari)
- [ ] Desktop (1920px, Chrome/Safari)

**Browsers:**
- [ ] Safari (mobile)
- [ ] Chrome (mobile)
- [ ] Firefox (desktop)

---

#### Task 6.6: E2E Test Verification
**Run existing test suite:**
```bash
cd e2e
PLAYWRIGHT_USE_RUST_UI=0 npx playwright test
```

```
[ ] All chat tests pass
[ ] All canvas tests pass
[ ] All dashboard tests pass
[ ] No new regressions
```

---

## Implementation Notes

### CSS Classes Already Defined
These CSS states exist and just need React to activate them:

| Feature | CSS Class | File | Line | Notes |
|---------|-----------|------|------|-------|
| Canvas shelf drawer | `#agent-shelf.open` | `agent_shelf.css` | 327 | `transform: translateX(0)` |
| Canvas scrim | `.shelf-scrim` | `agent_shelf.css` | 331-353 | Overlay when shelf open |
| Chat sidebar drawer | `.thread-sidebar.active` | `chat.css` | 497-499 | `left: 0` |
| Chat responsive | `@media (max-width: 768px)` | `chat.css` | 485-520 | Sidebar positioning |
| Dashboard card view | `@media (max-width: 768px)` | `dashboard.css` | Card conversion | Exists, blocked by min-width |

### Media Query Behavior
- **Desktop (>767px):** Shelf/sidebar always visible, drawer classes ignored
- **Mobile (≤767px):** Shelf/sidebar off-canvas, drawer classes apply
- **No in-between:** Current CSS jumps at 768px boundary

This is acceptable for now. Future: Consider tablet-specific layout (2-column on 768-1024px).

### Performance Notes
- No heavy redraws: Using CSS transforms (GPU-accelerated)
- State updates minimal: Only toggle boolean
- No layout thrashing: CSS handles animations

### Accessibility
- Buttons have `aria-expanded` for screen readers
- Scrim is keyboard accessible (click handler)
- Consider: Escape key to close drawer (Task 7+)

---

## Success Criteria

### Functional Requirements
- [ ] Profile page scrolls on mobile without horizontal scroll
- [ ] Dashboard table converts to card view on mobile
- [ ] Search bar responsive on small screens
- [ ] Canvas shelf drawer opens/closes on hamburger click (mobile only)
- [ ] Chat thread sidebar opens/closes on hamburger click (mobile only)
- [ ] Desktop experience unchanged
- [ ] No console errors or warnings
- [ ] All E2E tests pass

### Performance Requirements
- [ ] Page load time unchanged (within 5%)
- [ ] No layout shift when toggling drawer
- [ ] Smooth animations (60fps target)

### Accessibility Requirements
- [ ] WCAG 2.1 AA compliance
- [ ] Screen reader announces drawer state changes
- [ ] Touch targets >= 44x44px
- [ ] Color contrast maintained

---

## Rollback Plan

If issues arise:

1. **Revert specific file:**
   ```bash
   git checkout src/styles/layout.css
   git checkout src/styles/css/dashboard.css
   # etc.
   ```

2. **Quick rollback to before state management:**
   ```bash
   git revert <commit-hash>
   ```

3. **If state management breaks:** Remove `ShelfProvider` from `App.tsx` and `useShelf` calls will throw. Revert file and commit fresh.

---

## Files Modified Summary

| File | Type | Changes | Complexity |
|------|------|---------|-----------|
| `src/styles/layout.css` | CSS | Remove `overflow: hidden` | Low |
| `src/styles/chat.css` | CSS | Remove margin-top, add scrim | Low |
| `src/styles/css/dashboard.css` | CSS | Replace min-width, responsive search | Low |
| `src/lib/useShelfState.ts` | TypeScript | NEW: Context hook | Medium |
| `src/routes/App.tsx` | TypeScript | Wrap with provider | Low |
| `src/components/Layout.tsx` | TypeScript | Wire button to state | Medium |
| `src/pages/CanvasPage.tsx` | TypeScript | Activate shelf CSS class | Medium |
| `src/pages/ChatPage.tsx` | TypeScript | Activate sidebar CSS class | Medium |

**Total files: 8** (3 CSS, 5 TypeScript)
**New files: 1** (useShelfState.ts)

---

## Timeline Estimate

| Phase | Hours | Owner Notes |
|-------|-------|-----------|
| Phase 1: Scrolling | 4-6 | Low risk, high impact |
| Phase 2: Dashboard | 2-3 | Low risk |
| Phase 3: State mgmt | 4-6 | Medium risk, foundational |
| Phase 4: Canvas | 2-3 | Medium risk |
| Phase 5: Chat | 2-3 | Medium risk |
| Phase 6: Testing | 2-3 | Validation |
| **TOTAL** | **10-15** | |

---

## Post-Implementation Improvements (Future)

These are out of scope but should be tracked:

- [ ] Escape key to close drawers
- [ ] Tablet-specific layout (2-column)
- [ ] Execution results panel as bottom drawer on mobile
- [ ] Responsive font sizing with `clamp()`
- [ ] Landscape orientation support
- [ ] PWA offline support

---

## Questions Before Starting

1. **Canvas editor on mobile:** Current design shows shelf as drawer, assumes canvas editor is usable. If not, should it be hidden entirely on mobile?
2. **Execution panel mobile:** Should results panel become a bottom drawer on mobile (separate from shelf state)?
3. **Escape key:** Should Escape close drawers?
4. **Tablet mode:** At 768px, should layout adapt (e.g., 2 columns instead of full 3-column)?

Document answers below for reference:

```
Q1:
Q2:
Q3:
Q4:
```

---

## Approval Checklist

- [ ] Task list reviewed and understood
- [ ] Implementation approach approved
- [ ] Success criteria agreed
- [ ] Ready to begin Phase 1

**Reviewer:** _______________
**Date:** _______________

---

*Last updated: 2025-10-18*
*Status: READY FOR IMPLEMENTATION*
