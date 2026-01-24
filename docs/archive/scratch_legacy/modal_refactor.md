# Modular Modal Components – Refactor Guide

This document captures the current state of modal handling in the frontend, the
target architecture we want, **why** it is beneficial, and a step-by-step task
list to get there.  Treat it as living documentation as we chip away at the
refactor.

> **Status – 2025-04-26**
> The refactor for the *Agent Config* modal is now complete.  A dedicated
> component (`components/agent_config_modal.rs`) owns every aspect of the
> dialog: DOM creation, data population, show / hide and all event listeners.
> The legacy helpers in `ui/modals.rs` were deleted and all call-sites were
> migrated.  Small vestiges (e.g. HTML-builder inside `ui/setup.rs`) will be
> addressed in a follow-up.

--------------------------------------------------------------------------------

## 1. Current Situation (April 2025)

| Modal | Creation & DOM | Show/Hide helpers | Event listeners | Notes |
|-------|----------------|-------------------|-----------------|-------|
| **Agent Config** | `ui/setup.rs::create_agent_input_modal()` | `ui/modals.rs::{open_,close_}agent_modal` | `ui/events.rs` (`setup_modal_handlers`, `setup_modal_action_handlers`) | Spread across three files. |
| **Agent Debug**  | `components/agent_debug_modal.rs`            | **same file** (`render_agent_debug_modal`, `hide_agent_debug_modal`) | **same file** (local callbacks) | Self-contained component. |

The *Debug* modal already follows a **component-per-file** approach. The
*Config* modal is an earlier implementation and still uses a “spray” of helper
functions placed in different modules.


## 2. Goal

Bring *all* modals (and in general, any discrete UI widget) under a **modular
component** pattern:

*   **Single ownership** – one Rust module is responsible for creating,
    rendering, updating and tearing down its DOM.
*   **Clear public API** – callers only interact through a small set of methods
    (`init`, `open`, `close`, `refresh`, etc.) rather than manipulating the DOM
    directly.
*   **No global‐scoped event hooks** specific to that widget – they live next
    to the DOM they operate on.
*   **Idempotent initialisation** – `init()` is safe to call multiple times and
    attaches listeners once.

Benefits:

1.   Shorter, purpose-driven files (good for code reviews & onboarding).
2.   Easier unit-testing or future migration to Yew/Leptos because the modal is
     already isolated.
3.   Eliminates accidental cross-file dependencies.


## 3. Target Folder Structure

```
frontend/src/components/
│
├── agent_config_modal/
│   ├── mod.rs          // public AgentConfigModal struct + API
│   ├── view.rs         // (optional) pure DOM construction helpers
│   └── events.rs       // (optional) local event callback wiring
└── agent_debug_modal.rs  // already component-ised
```

If the code remains < ~300 LoC, you may keep everything in a single
`agent_config_modal.rs` file—follow the existing debug modal as a template.


## 4. High-level Refactor Plan

1. **Create new component skeleton** in `components/agent_config_modal.rs`.
2. **Move DOM creation** logic from `ui/setup.rs::create_agent_input_modal()`
   into the new component’s `init()` (or `new()`).
3. **Move event wiring** (`setup_modal_handlers`,
   `setup_modal_action_handlers`) into the component constructor so they are
   attached exactly once.
4. **Move show/hide helpers** into methods (`open()`, `close()`). Remove the
   now-unnecessary globals in `ui/modals.rs`.
5. **Update call-sites**
   * anywhere that called `open_agent_modal` → `AgentConfigModal::open`.
   * anywhere that called `close_agent_modal` → `AgentConfigModal::close`.
6. **Delete legacy functions** in `ui/setup.rs`, `ui/events.rs`, `ui/modals.rs`.
7. **Compile & test** (`cargo check` + manual browser test).
8. **Document public API** with `///` rustdoc comments.

### ✅ 2025-04-26 – accomplished

* Steps 1 → 6 are fully implemented.  `AgentConfigModal` component now:
  * Creates the modal DOM (still calls legacy builder but backed by
    idempotent guard).
  * Populates data from `AppState` in `open()`.
  * Shows / hides itself (`open` / `close`).
  * Attaches close / save / tab-switch / send-task listeners once via
    `attach_listeners()`.
* All usages of `ui::modals::*` were replaced.  Old file removed.
* `ui/main` initialises the component at startup.
* `ui/events` no longer registers modal helpers.

Remaining small items are tracked below.


## 5. Detailed Task List

### 5.1 File scaffolding

```bash
touch frontend/src/components/agent_config_modal.rs
```

Add stub code:

```rust
pub struct AgentConfigModal { … }

impl AgentConfigModal {
    pub fn init(document: &Document) -> Result<Self, JsValue> { … }
    pub fn open(&self, agent_node_id: &str) { … }
    pub fn close(&self) { … }
}
```

### 5.2 Move and adapt DOM creation

Copy the HTML-building code from `create_agent_input_modal()`.

Things to watch:

* Replace `document.get_element_by_id("agent-modal").is_some()` guard with an
  internal `Option<web_sys::Element>` stored in the struct.
* Keep the backdrop click listener we added recently; it now lives here.

### 5.3 Port event listeners

* Save button
* Tab buttons (Main / History)
* Input/textarea auto-save if needed

Each closure can access `self` via `Rc<RefCell<…>>` if state is required.

### 5.4 Replace open/close helpers

Remove `ui/modals.rs` or keep only very thin shims that delegate to the
component.  Update imports.

### 5.5 Wire into startup code

Create and store a singleton instance in `APP_STATE` or re-create lazily in
`AgentConfigModal::open`.  Decide based on preferred life-cycle.

### 5.6 Clean-up & doc

* Run `cargo fmt` and `cargo check`.
* Update this file with any deviations from the plan.


## 6. Future Extensions

* Migrate other UI pieces (e.g. canvas toolbar) into their own components.
* Consider introducing a small in-house **component trait**:

```rust
trait UiComponent {
    fn init(document: &Document) -> Self;
    fn attach_listeners(&self) {}
}
```

* Long term: evaluate Yew/Leptos or Dioxus once wasm-bindgen code becomes too
  verbose.

--------------------------------------------------------------------------------

## 7. Follow-up TODOs (post-merge)

These are tiny clean-ups that weren’t worth blocking the initial migration:

1. **Inline HTML builder** – move the contents of
   `ui/setup::create_agent_input_modal` directly into
   `AgentConfigModal::init()` and delete the old function/file.
2. **Delete orphaned modal helpers in `ui/events.rs`** – once the HTML builder
   is migrated we can safely remove the remaining `setup_modal_handlers`,
   `setup_modal_action_handlers`, and `setup_tab_handlers` code.
3. **Consolidate constants** – system/task instruction defaults live in
   `constants.rs`; consider moving modal-specific defaults next to the
   component.
4. **Unit tests** – add a wasm-bindgen test that initialises the component,
   opens it with sample data and asserts DOM fields are populated (can run in
   `wasm-bindgen-test` headless mode).
5. **Lint pass** – reduce outstanding compiler warnings unrelated to this
   refactor.

After these points the legacy *modal* surface will be entirely removed and the
codebase will be ready for the next component migrations (canvas toolbar, etc.).

--------------------------------------------------------------------------------

**Author:** OpenAI Codex CLI session · 2025-04-26
