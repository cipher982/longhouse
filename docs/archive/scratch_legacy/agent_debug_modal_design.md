### 2025-04-27

#### What has been delivered

- Aligned design doc with current codebase:
  - Updated message/state definitions to reflect `u32`/`ApiAgentDetails`.
  - Removed unsupported `logs` field and noted future placeholders.
- No functional code changes yet; remaining Phase 1 tasks still pending.

#### Next immediate steps

1. Hook up tab switching handlers in `agent_debug_modal.rs` to dispatch tab change messages.
2. Add “Edit Agent” button inside modal and dispatch `EditAgent` message.
3. Write wasm-bindgen tests for reducer and component.
4. Enhance modal styling and implement responsive layout.

---
### 2025-04-27

#### What has been delivered

- Aligned design doc with current codebase:
  - Updated message/state definitions to reflect `u32`/`ApiAgentDetails`.
  - Removed unsupported `logs` field and noted future placeholders.
- No functional code changes yet; remaining Phase 1 tasks still pending.

#### Next immediate steps

1. Hook up tab switching handlers in `agent_debug_modal.rs` to dispatch tab change messages.
2. Add “Edit Agent” button inside modal and dispatch `EditAgent` message.
3. Write wasm-bindgen tests for reducer and component.
4. Enhance modal styling and implement responsive layout.

---
# Agent Debug Modal (Agent Info) – Design Document

> **Status:** Draft  |  Option B (Separate Modals) **selected**

This document captures the evolving design and implementation plan for a new **Agent Debug / Info modal** in the frontend.  It is intended to be _living documentation_: update it as design decisions change, requirements grow, or tasks are completed.


### 2025-04-27

#### What has been delivered

- Aligned design doc with current codebase:
  - Updated message/state definitions to reflect `u32`/`ApiAgentDetails`.
  - Removed unsupported `logs` field and noted future placeholders.
- No functional code changes yet; remaining Phase 1 tasks still pending.

#### Next immediate steps

1. Hook up tab switching handlers in `agent_debug_modal.rs` to dispatch tab change messages.
2. Add “Edit Agent” button inside modal and dispatch `EditAgent` message.
3. Write wasm-bindgen tests for reducer and component.
4. Enhance modal styling and implement responsive layout.
---

## 1  Background & Motivation

The current Dashboard shows only high-level agent metadata (name, schedule, status).  When troubleshooting or optimising an agent we need deeper visibility: system prompt, model parameters, recent runs, token/cost usage, error traces, etc.  Surfacing this information will:

* accelerate debugging and support
* prevent accidental edits when a user “just wants to look”
* pave the way for future observability (charts, per-run logs)

---

## 2  High-Level Solution

* Introduce a **read-only Agent Debug Modal** (a.k.a. _AgentInfoModal_).
* Keep the existing **Agent Edit Modal** for mutating data.
* Provide **cross-links** so a user can jump directly between the two modals for the same agent.

Why not a single combined modal?  Separate views keep state management simple (important in our Elm-style architecture) and reduce UX confusion between “observe” and “edit”.

---

## 3  Backend Changes

### 3.1  New REST endpoint

`GET /api/agents/{id}/details`

Query parameters (`include=threads,runs,stats`) allow the client to opt-in to heavier payloads.

```jsonc
{
  "agent": {
    "id": "uuid",
    "name": "…",
    "status": "running|paused|error",
    "schedule": "0 */3 * * *",
    "system_prompt": "You are …",
    "model": "gpt-4o-mini",
    "temperature": 0.7,
    "created_at": "…"
  },
  "threads": [ /* returned only if include=threads */ ],
  "runs":    [ /* returned only if include=runs    */ ],
  "stats":   { /* returned only if include=stats   */ }
}
```

### 3.2  WebSocket additions

Topic `agent:{id}` already emits status updates.  We will extend it to broadcast per-run log events:

```jsonc
{
  "type": "run_log",
  "data": {
    "run_id": "uuid",
    "started_at": "…",
    "duration_ms": 1234,
    "success": true,
    "total_tokens": 543,
    "cost_usd": 0.0123,
    "error": null
  }
}
```

---

## 4  Frontend Architecture

### 4.1  New Messages (frontend/src/messages.rs)

```rust
ShowAgentDebugModal { agent_id: u32 }
HideAgentDebugModal
ReceiveAgentDetails(ApiAgentDetails)
// (Future) ReceiveAgentRunLog { agent_id: u32, log: AgentRunLogEntry }
```

### 4.2  State additions (frontend/src/state.rs)

```rust
pub struct AgentDebugPane {
    pub agent_id: u32,
    pub loading: bool,
    pub details: Option<ApiAgentDetails>,
    pub active_tab: DebugTab,
}

pub enum DebugTab { Overview, RawJson /*, Threads, Runs */ }
```

### 4.3  Command executors (frontend/src/command_executors.rs)

1. `Command::FetchAgentDetails(agent_id)` – triggers `ApiClient::get_agent_details(agent_id)` and dispatches `ReceiveAgentDetails`.
2. (Later) WebSocket subscription for live run logs via topic `agent:{id}`.

### 4.4  Component tree

```
components/
  └─ agent_debug_modal.rs   (new)
```

*Initial tab set*

| Tab      | Notes                                                |
|----------|------------------------------------------------------|
| Overview | name, status badge, schedule, model, temperature     |
| Raw JSON | pretty-printed full payload                          |

Later phases add **Threads** and **Runs / Logs** tabs.

### 4.5  Dashboard integration

Add a 🐞 “Debug” icon/button beside the existing ✎ “Edit” button.

---

## 5  Implementation Phases & Task List

> Mark tasks with ✅ when merged into `main`.

### Phase 0  – Preparation

- [ ] Decide file names / folder placement (✔ agreed in this doc)

### Phase 1  – Minimal Vertical Slice

- [x] **Backend**: implement `/details` endpoint (overview only)
- [x] **Frontend**: introduce messages, state, command executor stubs
- [x] **Component**: create `AgentDebugModal` with Overview & Raw JSON tabs
- [x] **Dashboard**: add “Debug” button → ShowAgentDebugModal
- [ ] **Linking**: in Debug modal add “Edit Agent” button (deep-link)

### Phase 2  – Threads Tab

- [ ] Backend: add `threads` include + DTO
- [ ] Frontend: render Threads table

### Phase 3  – Runs / Logs Tab (live updating)

- [ ] Persist run results (#backend)
- [ ] Publish `run_log` WebSocket events
- [ ] Frontend: display log stream & basic charts

### Phase 4  – Cross-link from Edit modal

- [ ] Add “View Debug Info” button inside `AgentEditModal`

---

## 6  Open Questions

1. **Pagination / limits** — how many runs & threads to load by default?
2. **Access control** — will the debug view be restricted to certain roles later?
3. **Mobile UX** — should the modal be full-screen on small devices?

---

## 7  Changelog

| Date       | Author   | Notes                              |
|------------|----------|------------------------------------|
| 2025-04-25 | chatGPT  | Initial draft committed            |

---

_Feel free to edit, extend, and tick off tasks as we ship!_

---

## 8  Progress Log

### 2025-04-26

#### What has been delivered

1. **Backend**
   • New Pydantic wrapper `AgentDetails`.
   • `GET /api/agents/{id}/details` implemented (returns `{"agent": ...}`; honours `include=` param but currently sends empty placeholders).
   • Added `backend/tests/test_agent_details.py` – basic & `include`-param cases pass.

2. **Frontend**
   • Message/command/state scaffolding (`ShowAgentDebugModal`, `ReceiveAgentDetails`, `FetchAgentDetails`, etc.).
   • `ApiAgentDetails` model mirrors backend wrapper.
   • `AgentDebugPane` added to global state, with simple enum `DebugTab`.
   • Command executor wired to new `ApiClient::get_agent_details` helper.
   • Minimal `AgentDebugModal` component renders Overview + Raw JSON (read-only) and graceful loading placeholder.
   • 🐞 button added beside ✎ Edit button in dashboard; opens modal.
   • Modal hides on outside click; also hide via `HideAgentDebugModal` message.


#### Things we learned / issues to revisit

- Elm-style architecture still works but **update.rs** is getting unwieldy (>1.5 k LOC).  We bolted the new handlers near the end; consider refactor.
- Large number of compiler warnings in the WASM build (unused imports, dead code).  None break functionality but noise is growing.
- `AgentDebugModal` is currently *self-contained* JS/DOM; later we should adopt shared style system / CSS classes.
- No dedicated CSS yet – relies on inline styles ⇒ accessibility & theme issues.
- We did **not** add the “Edit Agent” deep-link in the modal (task left open).
- Tab buttons don’t switch tabs yet – only creation helper exists.  Needs click wiring.
- End-to-end flow not user-tested; modal overlay dimensions, scrolling etc. might need tweaks.

#### Next immediate steps

1. Hook up tab switching (Overview <-> Raw JSON) + basic styling.
2. Add “Edit Agent” button inside modal (dispatch existing `EditAgent`).
3. Unit-test frontend reducer & component (wasm-bindgen test harness).
4. General CSS cleanup + responsive layout.
5. After smoke-testing Phase 1, start Phase 2 (Threads tab) – requires backend include logic and pagination decisions.

### Open questions (updated)

* Where to place modal component styling? Global styles vs shadow-DOM?
* Should the `/details` endpoint embed *agent messages* as part of “runs” or separate include?
* We now expose empty arrays for requested heavy includes – good for forward compatibility, but do we want 204 instead when empty?

---
