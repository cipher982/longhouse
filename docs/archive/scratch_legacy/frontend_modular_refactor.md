# Project Overview & Next Steps

This document provides context for the AI Agent Platform frontend, restates our objectives, captures key lessons learned, lists relevant files and functions, and presents a modular, step-by-step task list to follow.

---

## 1. Context

- We maintain two main views: **Agent Dashboard** (table of agents) and **Canvas Editor** (visual workflow builder).
- Successfully refactored from coexisting views with `display: none` to proper mount/unmount pattern.
- The Canvas view includes: sidebar (Agent Shelf), toolbar (Auto-Fit, Center, Clear), and rendering surface (`<canvas>`).
- Implemented clean flexbox layout and modularized DOM creation in Rust/WASM.

## 2. Goals

✅ Render only one main view in the DOM at a time (unmount inactive views).
✅ Use a clean flexbox layout for Canvas (sidebar + main area) without overlapping elements.
✅ Modularize code: each UI component in its own file; each page/view in its own module.
✅ Maintain minimal, conflict-free CSS rules, building up complexity only as needed.
✅ Establish an easy-to-follow file structure (components/, pages/, main.rs).

## 3. Things Learned

- **Mount/Unmount Pattern**: Complete view removal prevents z-index/layout conflicts and improves performance.
- **Flexbox Layout**: Using `flex: 1; min-height: 0;` prevents overflow in nested flex containers.
- **Rust/WASM Architecture**:
  - Separate page modules with mount/unmount functions improve maintainability
  - Clear separation between UI setup and view management
  - WebSocket initialization and callbacks in a dedicated section
  - Avoid borrowing state in UI event handlers by using command pattern
  - Clone values before moving them when dealing with closures
- **CSS Organization**:
  - Scoped variables for colors, spacing, and breakpoints
  - Proper z-index layering for modals and overlays
  - Mobile-first responsive design with flexbox
- **Rust Ownership Patterns**:
  - Values should be cloned before moving if they need to be used again
  - Prefer returning Commands for UI updates instead of manipulating state directly
  - Use `AppStateRef::None` pattern to avoid borrowing issues
  - Be careful with closure captures to prevent borrow checker errors

## 4. Key Files & Functions

- `frontend/src/pages/mod.rs` – New module system for page components
- `frontend/src/pages/dashboard.rs` – Dashboard view mount/unmount logic
- `frontend/src/pages/canvas.rs` – Canvas view mount/unmount with layout
- `frontend/src/views.rs` – Updated view-switching with proper unmounting
- `frontend/src/lib.rs` – Refactored initialization and WebSocket setup
- `frontend/src/update.rs` – Message handlers with command pattern
- `frontend/www/styles.css` – Organized CSS with proper scoping

## 5. Completed Tasks

### 5.1 Folder & Module Structure
✅ Created `frontend/src/pages/` directory
✅ Moved Dashboard view logic to `pages/dashboard.rs`
✅ Moved Canvas view logic to `pages/canvas.rs`
✅ Updated main.rs to use new page modules

### 5.2 Single-View Mounting
✅ Refactored view-switching to unmount previous view
✅ Removed all `display: none` toggles
✅ Implemented proper mount/unmount functions

### 5.3 Component Modularization
✅ Created proper component hierarchy
✅ Each component manages its own DOM elements
✅ Clear separation between pages and components

### 5.4 CSS Cleanup & Organization
✅ Organized CSS with proper scoping
✅ Implemented responsive flexbox layout
✅ Fixed z-index and positioning issues

## 6. Remaining Tasks

### 6.1 Testing & Verification
- [ ] Test view transitions under different network conditions
- [ ] Verify memory usage with Chrome DevTools
- [ ] Test responsive behavior on various screen sizes
- [x] Check for and fix DOM-related issues during view switching

### 6.2 Future Improvements
- [ ] Consider splitting CSS into component-specific files
- [ ] Add loading states for view transitions
- [ ] Implement error boundaries for view mounting
- [ ] Add animation transitions between views
- [ ] Add more targeted debug logging for view transitions

### 6.3 Documentation
- [x] Document key Rust patterns used in the codebase
- [ ] Add inline documentation for mount/unmount functions
- [ ] Create component lifecycle documentation
- [ ] Document CSS organization and variables

## 7. Recent Challenges & Solutions

### 7.1 View Switching Issues
- **Problem**: Dashboard was visible in Canvas tab, and sidebar didn't disappear
- **Root Cause**: Incomplete unmounting, borrowing issues in state manipulation
- **Solution**:
  - Enhanced the view switching logic in `render_active_view_by_type`
  - Improved mount/unmount functions to fully remove elements
  - Added detailed logging to track DOM manipulation

### 7.2 Rust Ownership Challenges
- **Problem**: Borrow checker errors in `update.rs` when switching views
- **Root Cause**: Value moved before being cloned in closure
- **Solution**:
  - Clone values before moving them (`view_clone = view.clone()` before assignment)
  - Use command pattern to defer UI updates until after state operations

### 7.3 Function Signature Mismatches
- **Problem**: `resize_canvas_simple` function not found error
- **Root Cause**: Referenced non-existent function
- **Solution**:
  - Used correct function signature `resize_canvas` with `AppStateRef::None`
  - Standardized approach for canvas operations without borrowing state

---

*This document will be updated as we continue to improve the codebase.*
