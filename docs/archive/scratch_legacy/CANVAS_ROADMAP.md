# Canvas & Workflow Revamp – Living Road-Map

**Last updated:** 2025-05-04

This document consolidates the recent discussion about making the Canvas the primary place to compose and run agent-centric workflows.  It is **source-controlled** so we can check items off in PRs and keep newcomers oriented.

---

## 1. Project context (TL;DR)

| Layer | Purpose | Tech |
|-------|---------|------|
| Backend | Store Agents / Threads / Messages, execute LangGraph agents, expose REST & WS | Python 3.12, FastAPI, LangGraph, SQLAlchemy, APScheduler |
| Frontend | Dashboard (table) + Canvas (visual workflows) + Chat UI | Rust + wasm-bindgen, Elm-style state, web-sys |
| Realtime | Push DB/agent events → browser topics | In-proc `EventBus` + WS hub |

Why the Canvas? – Promote **composable**, **observable** automations: Trigger → Agent (LLM) → Tool → …  Linear today, DAG tomorrow.

---

## 2. Current pain-points

* Canvas UI drifted after many refactors (broken buttons, unclear flow)
* No obvious way to place existing agents on canvas
* Redundant "Create Agent" in Canvas (should live in Dashboard)
* Internals still follow Elm-pattern, but DOM mutations crept into event handlers
* **Layout issues:** Overlapping/floating controls, sidebar not a true sidebar, vertical line peeking through in dashboard view, agent pills hidden or inconsistent
* **CSS fragility:** Inline styles used instead of proper CSS classes, causing inconsistent styling

---

## 3. Target UX (MVP)

1. Split view: **Agent Shelf** (left, 240 px, true sidebar with dedicated CSS) + infinite Canvas (right, fills space)
2. Shelf lists every agent as draggable pill; dropping one creates an AgentNode
3. Palette for Trigger / Tool nodes will use same drag pattern later
4. Toolbar (input-panel) sits above canvas, not floating, as a flex child
5. No elements overlap; sidebar and toolbar only visible in canvas view
6. Responsive design for sidebar/canvas on smaller screens
7. Linear execution engine on backend can walk nodes sequentially

---

## 4. Task list  ✅/🟡/❌

Legend: ✅ done – 🟡 in-progress – ❌ not started

### 4.1  UI Refactor

- [ ] Replace toolbar dropdown/button with **Agent Shelf**
    - [ ] components/agent_shelf.rs – render list (❌)
    - [ ] ui/main.rs – implement true flex row layout for sidebar + main area (❌)
    - [ ] Add dedicated CSS for #agent-shelf and .agent-pill in styles.css (❌)
    - [ ] Move all inline styles for agent shelf/pills into CSS (❌)
    - [ ] Only show sidebar in canvas view; hide in dashboard (❌)
    - [ ] Drag → dispatch `AddAgentNode` (❌)
- [ ] Improve canvas layout structure
    - [ ] Move input-panel toolbar above canvas, as a flex child, not floating/fixed (❌)
    - [ ] Ensure #canvas-container fills remaining space in main area (❌)
    - [ ] Remove vertical line (sidebar border) in dashboard view (❌)
    - [ ] Add responsive CSS for sidebar and main area (❌)
- [ ] Remove model-select dropdown from Canvas (❌)
- [ ] Clear All → `Message::ClearCanvas` + Command::RedrawCanvas (❌)

### 4.2  State & Message plumbing

- [ ] messages.rs – ensure `AgentsLoaded` triggers Command::RefreshAgentShelf (❌)
- [ ] command_executors.rs – implement RefreshAgentShelf → `agent_shelf::update()` (❌)
- [ ] Add view switching logic to properly hide/show sidebar and toolbar (❌)
- [ ] Toast helper for UX warnings (❌)

### 4.3  Backend hooks *(no immediate change needed for shelf MVP)*

- ✅ `/api/agents` already returns all agents

### 4.4  Follow-ups

- [ ] Palette & nodes for Trigger / Tool / Condition (❌)
- [ ] Save workflow JSON to Agent.workflow_definition (❌)
- [ ] LinearRunner backend execution (❌)
- [ ] WS node-highlight events (❌)

---

## 5. File guide (hot-spots)

| Area | File(s) | Notes |
|------|---------|-------|
| Canvas shell | `frontend/src/ui/main.rs` | Builds DOM skeleton for Canvas page |
| Global state | `frontend/src/state.rs`, `messages.rs`, `update.rs` | Elm-style pattern |
| Event handlers | `frontend/src/ui/events.rs` | Only dispatch Messages! |
| Agent listing | `frontend/src/components/agent_shelf.rs` *(new)* | Renders draggable agents |
| Node add logic | `update.rs` `Message::AddAgentNode` | Creates Node + redraw |
| Drawing | `state.rs::draw_nodes()` | Raw canvas rendering |
| CSS | `frontend/www/styles.css` | All layout and component styles |

---

## 6. Contributing guidelines (Canvas area)

1. **No direct state mutation** outside `update.rs`.
2. DOM operations live in Command executors or Component helpers.
3. Keep wasm-bindgen memory leaks in mind – call `forget()` on closures as needed.
4. Touch only minimal files per PR; check boxes above when merged.
5. **Prefer CSS over inline styles**: Define proper CSS rules for components rather than inline styles.
6. **Follow proper layout principles**: Use flex/grid layouts appropriately; avoid position:fixed for content.

---

Happy building!  Edit this doc in PRs to keep the roadmap live.
