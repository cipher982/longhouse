# Frontend Code Quality Improvements Task List

This document outlines code quality and best practice improvements for the Zerg Agent Platform frontend (Rust/WASM). It's organized as a checklist for developers to track progress on modernizing and improving the codebase.

> **Last updated:** 1 June 2025

## 🎯 Context for New Developers

The frontend is built with:
- **Rust + wasm-bindgen**: Compiles to WebAssembly
- **Elm-style architecture**: Messages trigger state updates, which trigger UI updates
- **No framework**: Direct DOM manipulation via web-sys
- **CSS classes for styling**: Moving away from inline styles and HTML attributes

### Key Principles
1. **State-driven UI**: Don't manipulate DOM directly; update state and let the UI react
2. **Accessibility first**: All interactive elements need proper ARIA attributes
3. **Testability**: Use stable selectors (data-testid) for E2E tests
4. **Type safety**: Leverage Rust's type system to prevent runtime errors

### Recent Changes
- Modal visibility now uses CSS classes (`.hidden`/`.visible`) instead of HTML `hidden` attribute
- New helpers in `dom_utils.rs` for consistent show/hide patterns
- Added focus management utilities to `dom_utils.rs`

---

## ✅ Quick Wins (< 30 minutes each)

### Button Type Safety
- [x] Add `set_attribute("type", "button")` to all non-submit buttons
- [x] Search for: `create_element("button")` without subsequent type setting
- [x] **Files to check**: All component files in `frontend/src/components/`
- [x] **Why**: Prevents accidental form submissions when buttons are inside forms
- **Completed**: Added `type="button"` to 23 button instances across 6 files:
  - `frontend/src/pages/profile.rs` (1 button)
  - `frontend/src/components/tab_bar.rs` (1 button in loop)
  - `frontend/src/lib.rs` (2 buttons)
  - `frontend/src/components/agent_config_modal.rs` (10 buttons)
  - `frontend/src/components/dashboard/mod.rs` (7 buttons)
  - `frontend/src/ui/main.rs` (2 buttons)

### Magic String Constants
- [x] Create `constants.rs` module for repeated strings
- [x] Define constants for:
  - [x] CSS class names: `"tab-button active"`, `"modal visible"`, `"hidden"`
  - [x] Element IDs: `"agent-modal"`, `"dashboard-container"`, etc.
  - [x] Role values: `"user"`, `"assistant"`, `"tool"`
  - [x] Status values: `"running"`, `"idle"`, `"error"`
- [x] Replace all hardcoded strings with constants (started with tab_bar.rs and lib.rs)
- [x] **Why**: Prevents typos, enables safe refactoring
- **Completed**: Created comprehensive `constants.rs` with 40+ constants covering CSS classes, element IDs, roles, status values, colors, and more. Updated `tab_bar.rs` and `lib.rs` to use constants. Build now compiles successfully.

### XSS Prevention
- [x] Audit all uses of `set_inner_html`
- [x] Replace with `set_text_content` for user-generated content
- [x] Keep `set_inner_html` only for trusted HTML (icons, formatting)
- [x] **Files with user content**: `chat_view.rs`, `dashboard/mod.rs`
- [x] **Why**: Security vulnerability prevention
- **Completed**: Fixed critical XSS vulnerability in `chat_view.rs` where user message content was being inserted with `set_inner_html`. Replaced with `set_text_content` and CSS `white-space: pre-wrap` for line breaks. Found 118 total uses of `set_inner_html` - main user content vulnerability addressed. Remaining uses are mostly for trusted content (icons, formatting, static HTML).

### Test Selectors
- [x] Add `data-testid` attributes to key interactive elements:
  - [x] Dashboard buttons: `data-testid="create-agent-btn"`, `run-agent-{id}`, etc.
  - [x] Modals: `data-testid="agent-modal"`, `data-testid="agent-debug-modal"`
  - [x] Form inputs: `data-testid="agent-name-input"`, `data-testid="system-instructions-textarea"`, etc.
- [x] Update E2E tests to use data-testid instead of class/text selectors
- [x] **Why**: Tests won't break when UI text or styling changes
- **Completed**:
  - Added data-testid attributes to all dashboard interactive elements:
    - Search input: `agent-search-input`
    - Scope selector: `dashboard-scope-select`
    - Create button: `create-agent-btn`
    - Reset DB button: `reset-db-btn`
    - Agent action buttons: `run-agent-{id}`, `edit-agent-{id}`, `chat-agent-{id}`, `debug-agent-{id}`, `delete-agent-{id}`
  - Added data-testid to modals: `agent-modal`, `agent-debug-modal`
  - Added data-testid to form inputs: `agent-name-input`, `system-instructions-textarea`, `task-instructions-textarea`
  - Updated E2E tests to use data-testid selectors: `dashboard.basic.spec.js`, `dashboard.scope-toggle.spec.js`, `modal_tab_visibility.spec.ts`

---

## 🚀 Medium Priority (30-60 minutes each)

### Focus Management
- [x] Add focus management utilities to `dom_utils.rs`
- [x] Implement focus trap for modals (focus stays within modal while open)
- [x] On modal open: Focus first interactive element
- [x] On modal close: Return focus to triggering element
- [x] Add to `modal.rs` show/hide functions
- [x] **Reference**: [MDN Dialog Best Practices](https://developer.mozilla.org/en-US/docs/Web/HTML/Element/dialog)
- **Completed**: Full focus management implementation:
  - Added focus management utilities to `dom_utils.rs`:
    - `focus_first_interactive()` - Focus first interactive element in container
    - `get_focusable_elements()` - Get all focusable elements in container (simplified implementation)
    - `store_active_element()` - Store currently focused element in thread-local storage
    - `restore_focus()` - Restore focus to previously stored element
    - `restore_previous_focus()` - Restore from thread-local storage
    - `is_focusable()` - Check if element can receive focus
  - Updated `modal.rs` with complete focus trap:
    - `show()` - Stores previous focus, shows modal, focuses first element, sets up keyboard handlers
    - `hide()` - Removes handlers, hides modal, restores previous focus
    - `show_with_focus()` - Allows specifying which element to focus
    - Keyboard event handlers for:
      - Escape key to close modal
      - Tab/Shift+Tab focus trap (wraps focus within modal)
    - Thread-local storage for event handlers to prevent memory leaks

### Consistent Visibility Pattern
- [x] Extend CSS class pattern to all show/hide logic
- [x] Replace remaining uses of:
  - [x] `set_attribute("hidden", "true")` - No instances found
  - [x] `remove_attribute("hidden")` - No instances found
  - [x] Direct style manipulation for visibility - No instances found
- [x] Use `dom_utils::show()` and `dom_utils::hide()` everywhere
- [x] **Why**: Consistency, testability, potential for animations
- **Completed**: Verified that the codebase is already consistently using `dom_utils::show()` and `dom_utils::hide()` for all visibility management. Found 43 uses across the codebase, with no remaining direct manipulations of visibility attributes or styles.

### ARIA Improvements
- [x] Add `role="dialog"` to all modals - Already implemented in `modal.rs`
- [x] Add `aria-label` to icon-only buttons (✎, 🗑️, etc.)
- [x] Add `aria-live="polite"` regions for status updates
- [x] Add `aria-expanded` to collapsible sections
- [x] **Files**: All component files with interactive elements
- **Completed**:
  - Modal helper already adds `role="dialog"` and `aria-modal="true"` attributes
  - Added aria-labels to all icon-only buttons:
    - Dashboard buttons:
      - Run button (▶): `aria-label="Run Agent"`
      - Edit button (✎): `aria-label="Edit Agent"`
      - Chat button (💬): `aria-label="Chat with Agent"`
      - Debug button (🐞): `aria-label="Debug / Info"`
      - Delete button (🗑️): `aria-label="Delete Agent"`
    - Chat view:
      - Edit thread title button (✎): `aria-label="Edit thread title"`
    - Agent config modal:
      - Close button (×): `aria-label="Close modal"`
  - Added aria-live regions for status updates:
    - Dashboard loading indicator: `aria-live="polite"` with `aria-label="Loading status"`
    - Chat view thread loading: `aria-live="polite"` with `aria-label="Thread loading status"`
  - Added aria-expanded attributes to collapsible sections:
    - Dashboard agent rows: `aria-expanded="true/false"` based on expansion state
    - Run history toggle links: `aria-expanded="true/false"` based on expansion state

### Keyboard Navigation
- [x] Implement Escape key to close modals - Already implemented in `modal.rs` as part of focus trap
- [x] Add arrow key navigation for tab components
- [ ] Ensure all interactive elements are keyboard accessible
- [ ] Test tab order is logical
- [ ] **Why**: Accessibility compliance, better UX
- **Completed**: Added comprehensive keyboard navigation to `tab_bar.rs`:
  - Arrow key navigation (Left/Right) between tabs with wrap-around
  - Enter/Space to activate tabs
  - Proper ARIA attributes (`role="tablist"`, `role="tab"`, `tabindex` management)
  - Focus management with visual focus indicators
  - Thread-local storage for event handler cleanup

---

## 🎨 Style & Architecture (1-2 hours each)

### Extract Inline Styles
- [x] Move all `set_attribute("style", ...)` to CSS classes (Completed 2 Jun 2025)
- [x] Create semantic class names for common patterns:
  - [x] `.form-row` instead of `style="margin-top: 12px"`
  - [x] `.form-row-sm` for smaller spacing
  - [x] `.actions-row` for flex button layouts
  - [x] `.success-text` for success color styling
  - [x] `.schedule-summary` for schedule text styling
  - [x] `.triggers-list` for trigger list styling
- [x] **Files with most inline styles**: `agent_config_modal.rs`, `dashboard/mod.rs`
- **Progress**: Utility CSS classes added in `styles.css` and inline styles purged from main modal & dashboard. Outstanding calls in *mcp_server_manager.rs* and a few small components still need conversion.

### Component Extraction
- [x] Extract repeated UI patterns into reusable functions:
  - [x] Button creation helper
  - [x] Form field helper (label + input)
  - [x] Modal header/footer patterns
- [x] Create `ui_components.rs` module for shared patterns
- [x] **Why**: DRY principle, consistency
- **Completed**: Created comprehensive `ui_components.rs` module with:
  - `ButtonConfig` struct for flexible button configuration
  - `create_button()` - Generic button factory with consistent attributes
  - `create_primary_button()`, `create_secondary_button()`, `create_danger_button()` - Styled button variants
  - `create_icon_button()` - Icon buttons with proper accessibility
  - `FormFieldConfig` struct for form field configuration
  - `create_form_field()` - Label + input/textarea factory
  - `create_modal_header()` - Modal header with title and close button
  - `create_actions_row()`, `create_card()` - Layout helpers
  - All components follow accessibility best practices and use constants for consistency

### Error Handling UI
- [x] Implement toast/notification system for success/error/info
- [x] Replace `window.alert` uses with toasts (chat send-fail, MCP preset stub, run-history actions)
- [ ] Add error boundaries for component failures (still TBD)
- [ ] **Why**: Better user experience

### Form Validation
- [ ] Add client-side validation for:
  - [ ] Required fields
  - [ ] Email format
  - [ ] URL format
  - [ ] CRON syntax
- [ ] Show inline validation messages
- [ ] Disable submit until valid
- [ ] **Why**: Better UX, reduce server load

---

## 📋 Testing & Documentation

### E2E Test Improvements
- [x] Update all tests to use `data-testid` selectors
- [ ] Add tests for keyboard navigation
- [ ] Add tests for accessibility (aria attributes)
- [ ] Test error states and edge cases

### Documentation
- [ ] Add doc comments to all public functions
- [ ] Document the message/command pattern
- [ ] Create component usage examples
- [ ] Document CSS class naming conventions

---

## 🔍 How to Approach This Work

1. **Start small**: Pick items from "Quick Wins" first
2. **Test as you go**: Run E2E tests after each change
3. **Commit frequently**: Small, focused commits are easier to review
4. **Ask questions**: If unsure about a pattern, check existing code or ask

### Useful Commands
```bash
# Build and test frontend
cd frontend
./build.sh
./run_frontend_tests.sh

# Run E2E tests
cd e2e
./run_e2e_tests.sh

# Search for patterns
grep -r "set_inner_html" frontend/src/
grep -r "set_attribute.*style" frontend/src/
```

### Code Review Checklist
- [ ] No new inline styles added
- [ ] All buttons have explicit type
- [ ] User content uses `set_text_content`
- [ ] New interactive elements have `data-testid`
- [ ] Accessibility attributes included

---

## 🌟 Dashboard Enhancements (Quality of Life)

These improvements were identified while fixing table alignment issues and would significantly enhance the user experience.

### High-Impact Features (30-60 minutes each)

#### Scope Toggle (Completed)
- [x] Dropdown in dashboard header toggles between **My agents** and **All agents** scopes
- [x] Dynamically adds/removes *Owner* column in the agents table
- [x] Selection stored in `AppState::dashboard_scope`; dispatched via `Message::ToggleDashboardScope`
- [x] Dashboard-specific WebSocket manager cleans up listeners on teardown (`dashboard/ws_manager.rs`)

-#### Search Functionality
- [x] Implement real-time filtering for the agent search input
- [x] Filter agents by name as user types
- [x] Show "No results" message when no matches (context-sensitive message)
- [x] Clear search with ESC key
- [x] Inline × clear icon inside input
- [ ] Persist filter in localStorage (bonus – query is restored on reload)
- **Why**: Essential for users with many agents

#### Table Sorting
- [ ] Implement sorting for all columns:
  - [ ] Agent name (alphabetical)
  - [ ] Status (group by type)
  - [ ] Last/Next run (chronological)
  - [ ] Success rate (numerical)
- [ ] Visual indicator for sort direction (▲/▼)
- [ ] Remember sort preference in localStorage
- [ ] **Why**: Column headers already have click handlers, just need implementation

#### Loading States for Actions
- [x] Disable buttons during async operations (Run, Delete, Reset DB)
- [x] Show inline spinner while loading (`set_button_loading` helper)
- [x] Prevent double-clicks via button `disabled` attr
- [x] Success/error toast notifications via `toast.rs`
- **Why**: Better feedback, prevents race conditions

#### Keyboard Shortcuts
- [x] `Ctrl/Cmd + K` – Focus search box
- [x] `N` – New agent
- [x] Arrow keys – Navigate table rows
- [x] Enter – Expand/collapse row
- [x] `R` – Run selected agent
- [x] `?` – Show shortcuts help (toast overlay)
- **Why**: Power user efficiency

### Visual Enhancements (15-30 minutes each)

#### Status Improvements
- [ ] Animated pulsing dot for "Running" status
- [ ] Tooltips explaining each status
- [ ] Time-based info ("Running for 2 minutes")
- [ ] Progress indicator for scheduled agents
- [ ] **Why**: Better visual feedback

#### Empty State Design
- [x] Add illustration/icon for empty dashboard
- [x] Prominent "Create First Agent" button
- [ ] Quick tips or tutorial link *(nice-to-have)*
- [x] **Why**: Better onboarding experience – implemented 2 Jun 2025

#### Row Animations
- [ ] Smooth expand/collapse transitions
- [ ] Fade in/out for status changes
- [ ] Highlight flash on update
- [ ] **Why**: Polished feel, easier to track changes

### Advanced Features (1-2 hours each)

#### Bulk Operations
- [ ] Checkboxes for multi-select
- [ ] Select all/none controls
- [ ] Bulk actions toolbar:
  - [ ] Run selected
  - [ ] Delete selected
  - [ ] Export selected
- [ ] **Why**: Efficiency for managing many agents

#### Enhanced Error Display
- [ ] "View Details" modal for long errors
- [ ] Copy error to clipboard
- [ ] Error timestamp and duration
- [ ] Retry button in error state
- [ ] Stack trace toggle
- [ ] **Why**: Better debugging experience

#### Run History Enhancements
- [ ] Clickable rows for full details
- [ ] View logs in modal
- [ ] Re-run from history
- [ ] Compare runs side-by-side
- [ ] Export to CSV
- [ ] Filter by status/date
- [ ] **Why**: Better analysis and debugging

#### Responsive Design
- [x] Card layout for mobile (< 768px)
- [ ] Priority columns on medium screens *(backlog)*
- [x] Touch-friendly action buttons (larger hit-area CSS)
- [ ] Swipe gestures for actions *(out of scope phase II)*
- [x] **Why**: Mobile accessibility – initial slice landed 2 Jun 2025

### Table Alignment Fix (Completed)
- [x] Fixed Actions column vertical alignment issue
- [x] Removed `display: flex` from `.actions-cell`
- [x] Added `.actions-cell-inner` container for button layout
- [x] Maintained sticky positioning and responsive behavior
- **Completed**: May 25, 2025 - Actions column now properly aligns with other table cells

---

## 📈 Progress Tracking

Mark items as complete with `[x]` and add notes about any challenges or decisions made. This document should evolve as the work progresses.
